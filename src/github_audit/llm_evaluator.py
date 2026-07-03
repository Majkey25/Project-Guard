from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel
from pydantic_ai import RunContext, UsageLimits

from github_audit.agent_chat import FieldRequest, build_field_plan
from github_audit.applier import describe_pending_write
from github_audit.config import Settings
from github_audit.models import (
    AddToProjectPlan,
    ApplyPlan,
    AssigneeUpdatePlan,
    AuditFinding,
    BatchTriageResult,
    IssueCommentPlan,
    IssueEditPlan,
    LabelUpdatePlan,
    LLMSuggestion,
    MilestoneUpdatePlan,
    NLFilterResult,
    PendingWrite,
    ProjectFieldDefinition,
    PullRequestMergePlan,
    ReviewerRequestPlan,
    RuleExplanation,
    SeverityScore,
    SeverityScoreList,
    StateUpdatePlan,
)

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.openai import OpenAIChatModel

_logger = logging.getLogger(__name__)

_RETRY_MAX = 2
_RETRY_DELAY_S = 1.5
# Caps total LLM sub-calls in the project agent to prevent runaway loops.
_PROJECT_AGENT_USAGE_LIMITS = UsageLimits(request_limit=12)

# Agents (and the HTTP client/connection pool each one owns) are expensive to build, so they're
# cached and reused across calls. Cached per-thread (not process-wide): pydantic-ai's run_sync()
# binds to the calling thread's event loop, so sharing one Agent/AsyncOpenAI client across threads
# (e.g. FastAPI's threadpool-executed sync routes) would risk cross-event-loop reuse of pooled
# connections. A thread-local cache is safe and still gives the full win for single-threaded
# callers (the CLI's per-finding suggest loop) and for repeated requests landing on the same
# worker thread.
_agent_cache = threading.local()


def _cache_key(settings: Settings, discriminator: str) -> tuple[object, ...]:
    return (
        settings.llm_provider_name,
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model_name,
        settings.llm_api_version,
        settings.llm_timeout_seconds,
        discriminator,
    )


def _cached_agent[T](key: tuple[object, ...], build: Callable[[], T]) -> T:
    cache = cast("dict[tuple[object, ...], object] | None", getattr(_agent_cache, "values", None))
    if cache is None:
        cache = {}
        _agent_cache.values = cache
    if key not in cache:
        cache[key] = build()
    return cast(T, cache[key])


def reset_agent_cache() -> None:
    """Clear the calling thread's cached Agents. For test isolation; production never needs this."""
    _agent_cache.values = {}


def _is_retryable_llm_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "rate limit",
            "429",
            "too many requests",
            "timeout",
            "timed out",
            "503",
            "service unavailable",
        )
    )


def _run_agent_sync[T](fn: Callable[[], T], label: str) -> T:
    """Run an agent call with logging and retry on transient errors."""
    for attempt in range(_RETRY_MAX + 1):
        _logger.debug("┌─ LLM %s attempt=%d", label, attempt + 1)
        try:
            result = fn()
            _logger.debug("└─ LLM %s ok", label)
            return result
        except Exception as exc:
            if _is_retryable_llm_error(exc) and attempt < _RETRY_MAX:
                delay = _RETRY_DELAY_S * (2**attempt)
                _logger.warning(
                    "LLM %s transient error (attempt %d), retry in %.1fs: %s",
                    label,
                    attempt + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                _logger.error("LLM %s failed (attempt %d): %s", label, attempt + 1, exc)
                raise
    raise AssertionError("unreachable")


_SUGGEST_INSTRUCTIONS = """
Suggest missing GitHub workflow metadata. Do not claim a field is present or missing.
Use only the supplied issue or pull request data. Return conservative values.
"""

_TRIAGE_INSTRUCTIONS = """
You are a GitHub project health analyst. Analyze audit findings and identify systemic patterns.
Focus on root causes, not individual items. Be concise and actionable.
"""

_SEVERITY_INSTRUCTIONS = """
Score the urgency of each audit finding. HIGH = blocks delivery or critically violates process.
MEDIUM = needs attention soon. LOW = nice to fix but not urgent.
Base scoring on item age, type, and which fields are missing.
Return exactly one score per finding in the same order as given.
"""

_EXPLAIN_INSTRUCTIONS = """
Explain why a specific audit finding matters for this particular item.
Be concrete and specific — reference the item title, assignees, and missing fields.
Keep each field to 1-2 sentences.
"""

_NL_FILTER_INSTRUCTIONS = """
Parse a natural language search query into structured filter criteria for a GitHub audit tool.
Only use values from the available options provided. Return empty lists if nothing matches.
"""

_CHAT_INSTRUCTIONS = """
You are an AI assistant embedded in Project Guard, a GitHub project audit tool.
Answer questions about the scan data shown in context. Be direct and concise (1-3 sentences).
Only reference what is visible in the scan results. Do NOT tell the user to go do things
manually in GitHub — if something cannot be done from this tool, say so in one sentence and stop.
Available in-tool commands: `explain`, Project field updates on a selected item, comments,
and `run scan`.
"""

_PROJECT_AGENT_INSTRUCTIONS = """
You are the Project Guard assistant. Use tools to inspect the selected GitHub issue or PR and to
queue GitHub writes. Supported writes: adding the item to the project board, Project V2 field
updates, new comments, title/body edits (full replacement, not a patch - always restate the exact
new text before the user confirms), label add/remove, assignee add/remove, close/reopen (with an
optional reason for issues), setting or clearing the milestone, merging a pull request (state the
merge method and note that merging is not easily reversible), and requesting PR reviewers (this
adds to, never replaces, the existing reviewer set). When the item is not on the project board
yet, queue prepare_add_to_project first - field updates for the same batch are then allowed and
run after the item is added. All writes are previews only until the user replies `apply it` (that
exact phrase) and write access is enabled - never claim a write already happened, and never
invent a different confirmation phrase such as CONFIRM.
Still unsupported: creating new issues/PRs, editing or deleting existing comments, changing a PR's
base branch, and draft/ready-for-review toggling - say those cannot be done yet.
Use exact Project field names, label names, milestone titles, and login names from the tool
output. If a name you were asked to use isn't found, say so instead of guessing or inventing one.
"""


class _ChatReply(BaseModel):
    reply: str


def _empty_name_map() -> dict[str, str]:
    return {}


def _empty_pending_writes() -> list[PendingWrite]:
    return []


@dataclass
class ProjectAgentDeps:
    finding: AuditFinding
    project_id: str | None
    fields: list[ProjectFieldDefinition]
    labels: dict[str, str] = dataclass_field(default_factory=_empty_name_map)
    milestones: dict[str, str] = dataclass_field(default_factory=_empty_name_map)
    assignable_users: dict[str, str] = dataclass_field(default_factory=_empty_name_map)
    pending_writes: list[PendingWrite] = dataclass_field(default_factory=_empty_pending_writes)


@dataclass(frozen=True)
class ProjectAgentResult:
    reply: str
    project_id: str | None
    fields: list[ProjectFieldDefinition]
    pending_writes: list[PendingWrite]
    new_messages: list[ModelMessage]


def _make_agent[LLMOutputT: BaseModel](
    settings: Settings,
    output_type: type[LLMOutputT],
    instructions: str,
) -> Agent[object, LLMOutputT]:
    settings.validate_llm()
    from pydantic_ai import Agent

    key = _cache_key(settings, f"agent:{output_type.__qualname__}:{instructions}")
    return _cached_agent(
        key,
        lambda: Agent(_make_model(settings), output_type=output_type, instructions=instructions),
    )


def _make_model(settings: Settings) -> OpenAIChatModel:
    from pydantic_ai.models import create_async_http_client

    http_client = create_async_http_client(timeout=settings.llm_timeout_seconds)
    provider_name = settings.llm_provider_name
    if provider_name == "azure":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.azure import AzureProvider

        provider = AzureProvider(
            azure_endpoint=settings.llm_base_url,
            api_key=settings.llm_api_key,
            api_version=settings.llm_api_version or None,
            http_client=http_client,
        )
        model = OpenAIChatModel(settings.llm_model_name, provider=provider)
    elif provider_name in {"openai", "openai-compatible"}:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
            http_client=http_client,
        )
        model = OpenAIChatModel(settings.llm_model_name, provider=provider)
    elif provider_name == "ollama":
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider

        base_url = settings.llm_base_url or "http://localhost:11434/v1"
        model = OllamaModel(
            settings.llm_model_name,
            provider=OllamaProvider(base_url=base_url, http_client=http_client),
        )
    else:
        msg = (
            f"unsupported LLM_PROVIDER={settings.llm_provider!r}; "
            "supported: openai, azure, openai-compatible, ollama"
        )
        raise ValueError(msg)
    return model


def suggest_for_finding(finding: AuditFinding, settings: Settings) -> LLMSuggestion:
    agent = _make_agent(settings, LLMSuggestion, _SUGGEST_INSTRUCTIONS)
    prompt = build_prompt(finding)
    return _run_agent_sync(lambda: agent.run_sync(prompt).output, "suggest")


def batch_triage(findings: list[AuditFinding], settings: Settings) -> BatchTriageResult:
    agent = _make_agent(settings, BatchTriageResult, _TRIAGE_INSTRUCTIONS)
    prompt = build_triage_prompt(findings)
    return _run_agent_sync(lambda: agent.run_sync(prompt).output, "triage")


def score_severities(findings: list[AuditFinding], settings: Settings) -> list[SeverityScore]:
    agent = _make_agent(settings, SeverityScoreList, _SEVERITY_INSTRUCTIONS)
    prompt = build_severity_prompt(findings)
    return _run_agent_sync(lambda: agent.run_sync(prompt).output.scores, "severity")


def explain_finding(finding: AuditFinding, rule: str, settings: Settings) -> RuleExplanation:
    agent = _make_agent(settings, RuleExplanation, _EXPLAIN_INSTRUCTIONS)
    prompt = build_explain_prompt(finding, rule)
    return _run_agent_sync(lambda: agent.run_sync(prompt).output, "explain")


def nl_to_filters(
    query: str,
    available_repos: list[str],
    available_assignees: list[str],
    available_fields: list[str],
    settings: Settings,
) -> NLFilterResult:
    agent = _make_agent(settings, NLFilterResult, _NL_FILTER_INSTRUCTIONS)
    prompt = build_nl_prompt(query, available_repos, available_assignees, available_fields)
    return _run_agent_sync(lambda: agent.run_sync(prompt).output, "nl_filter")


def general_chat(
    prompt: str,
    context: str,
    settings: Settings,
    *,
    message_history: list[ModelMessage] | None = None,
) -> tuple[str, list[ModelMessage]]:
    """Return (reply, new_messages) for multi-turn conversation support."""
    agent = _make_agent(settings, _ChatReply, _CHAT_INSTRUCTIONS)
    full_prompt = f"{context}\n\nUser: {prompt}"
    result = _run_agent_sync(
        lambda: agent.run_sync(full_prompt, message_history=message_history or []),
        "chat",
    )
    return result.output.reply, list(result.new_messages())


def general_chat_stream(
    prompt: str,
    context: str,
    settings: Settings,
    *,
    message_history: list[ModelMessage] | None = None,
) -> tuple[Iterator[str], Callable[[], list[ModelMessage]]]:
    """Return (token stream, get_new_messages). Call get_new_messages() after exhausting tokens."""
    settings.validate_llm()
    from pydantic_ai import Agent

    key = _cache_key(settings, f"agent_stream:{_CHAT_INSTRUCTIONS}")
    agent = _cached_agent(
        key, lambda: Agent(_make_model(settings), instructions=_CHAT_INSTRUCTIONS)
    )
    full_prompt = f"{context}\n\nUser: {prompt}"
    _logger.debug("┌─ LLM chat_stream")
    result = agent.run_stream_sync(full_prompt, message_history=message_history or [])

    def tokens() -> Iterator[str]:
        yield from result.stream_text(delta=True, debounce_by=None)
        _logger.debug("└─ LLM chat_stream ok")

    return tokens(), lambda: list(result.new_messages())


def project_agent_chat(
    prompt: str,
    context: str,
    finding: AuditFinding,
    fields: list[ProjectFieldDefinition],
    project_id: str | None,
    settings: Settings,
    *,
    labels: dict[str, str] | None = None,
    milestones: dict[str, str] | None = None,
    assignable_users: dict[str, str] | None = None,
    existing_writes: list[PendingWrite] | None = None,
    message_history: list[ModelMessage] | None = None,
) -> ProjectAgentResult:
    agent = _make_project_agent(settings)
    deps = ProjectAgentDeps(
        finding=finding,
        project_id=project_id,
        fields=fields,
        labels=labels or {},
        milestones=milestones or {},
        assignable_users=assignable_users or {},
        # deep-copied so tool mutations never alias the caller's prior-turn list
        pending_writes=[write.model_copy(deep=True) for write in existing_writes or []],
    )
    full_prompt = f"{context}\n\nUser: {prompt}"
    result = _run_agent_sync(
        lambda: agent.run_sync(
            full_prompt,
            deps=deps,
            message_history=message_history or [],
            usage_limits=_PROJECT_AGENT_USAGE_LIMITS,
        ),
        "project_agent",
    )
    _logger.debug(
        "project_agent messages=%d usage=%s",
        len(result.new_messages()),
        result.usage,
    )
    return ProjectAgentResult(
        reply=result.output.reply,
        project_id=project_id,
        fields=fields,
        pending_writes=deps.pending_writes,
        new_messages=list(result.new_messages()),
    )


def read_selected_item(ctx: RunContext[ProjectAgentDeps]) -> str:
    """Read selected issue/PR details, current Project fields, and writable options."""
    finding = ctx.deps.finding
    lines = [
        f"Item: {finding.item_type} #{finding.number}",
        f"Repository: {finding.repository}",
        f"Title: {finding.title}",
        f"URL: {finding.url}",
        f"Assignees: {', '.join(finding.assignees) or 'none'}",
        f"Labels: {', '.join(finding.labels) or 'none'}",
        f"Milestone: {finding.milestone or 'none'}",
        f"Missing fields: {', '.join(finding.missing_fields) or 'none'}",
        f"Current Project fields: {finding.current_project_fields or 'none'}",
        f"Body:\n{finding.body or '(empty)'}",
        "",
        f"Comments: showing {len(finding.comments)} of {finding.comments_total_count}",
        *_comment_lines(finding),
        "",
        "Writable Project fields:",
        *_field_lines(ctx.deps.fields),
        "",
        f"Available labels: {', '.join(sorted(ctx.deps.labels)) or 'none'}",
        f"Available milestones: {', '.join(sorted(ctx.deps.milestones)) or 'none'}",
        f"Assignable users (for assignees/reviewers): "
        f"{', '.join(sorted(ctx.deps.assignable_users)) or 'none'}",
    ]
    if finding.project_item_id is None:
        lines.append(
            "NOT on the selected project board - call prepare_add_to_project before"
            " queuing Project field updates."
        )
    if finding.content_id is None:
        lines.append("Writes cannot be queued because the GitHub node id is missing.")
    return "\n".join(lines)


def prepare_add_to_project(ctx: RunContext[ProjectAgentDeps]) -> str:
    """Queue adding the selected issue/PR to the project board (required before field updates)."""
    finding = ctx.deps.finding
    if ctx.deps.project_id is None:
        return "Cannot queue add-to-project: project id is missing."
    if finding.content_id is None:
        return "Cannot queue add-to-project: GitHub node id is missing."
    if finding.project_item_id is not None:
        return "The item is already on the project board."
    plan = AddToProjectPlan(
        project_id=ctx.deps.project_id,
        content_id=finding.content_id,
        repository=finding.repository,
        item_type=finding.item_type,
        number=finding.number,
        project_title=finding.project_title,
    )
    _replace_write(ctx.deps.pending_writes, AddToProjectPlan, plan)
    return "\n".join(["Queued add-to-project preview:", *describe_pending_write(plan)])


def _has_queued_board_add(ctx: RunContext[ProjectAgentDeps]) -> bool:
    queued = _find_write(ctx.deps.pending_writes, AddToProjectPlan)
    return queued is not None and queued.content_id == ctx.deps.finding.content_id


def prepare_project_field_update(
    ctx: RunContext[ProjectAgentDeps],
    field_name: str,
    value: str,
) -> str:
    """Queue one Project V2 field update preview for the selected issue/PR."""
    finding = ctx.deps.finding
    if ctx.deps.project_id is None:
        return "Cannot queue Project field update: project id is missing."
    pending_board_add = _has_queued_board_add(ctx)
    if finding.project_item_id is None and not pending_board_add:
        return (
            "The item is not on the project board. Call prepare_add_to_project first;"
            " then queue the field updates for the same batch."
        )
    plan = build_field_plan(
        finding,
        ctx.deps.fields,
        FieldRequest(field_name, value),
        replace_existing=True,
        allow_pending_board_add=pending_board_add,
    )
    if not plan.changes:
        return "\n".join(plan.skipped or ["No Project field update was queued."])
    existing = _find_write(ctx.deps.pending_writes, ApplyPlan)
    if existing is None:
        existing = ApplyPlan(changes=[])
        ctx.deps.pending_writes.append(existing)
    change_names = {change.field_name for change in plan.changes}
    existing.changes = [c for c in existing.changes if c.field_name not in change_names]
    existing.changes.extend(plan.changes)
    existing.skipped.extend(plan.skipped)
    lines = ["Queued Project field update preview:"]
    for change in plan.changes:
        before = finding.current_project_fields.get(change.field_name)
        before_text = f"{before!r} -> " if before is not None else ""
        lines.append(
            f"dry-run: {finding.repository}#{finding.number}"
            f" set {change.field_name}={before_text}{change.value}"
        )
    return "\n".join(lines)


def prepare_issue_comment(ctx: RunContext[ProjectAgentDeps], body: str) -> str:
    """Queue a new issue/PR comment preview for the selected item."""
    finding = ctx.deps.finding
    body = body.strip()
    if finding.content_id is None:
        return "Cannot queue comment: GitHub node id is missing."
    if not body:
        return "Cannot queue comment: body is empty."
    plan = IssueCommentPlan(
        subject_id=finding.content_id,
        repository=finding.repository,
        item_type=finding.item_type,
        number=finding.number,
        body=body,
    )
    _replace_write(ctx.deps.pending_writes, IssueCommentPlan, plan)
    return f"Queued comment preview for {finding.repository}#{finding.number}."


def prepare_issue_edit(
    ctx: RunContext[ProjectAgentDeps],
    title: str | None = None,
    body: str | None = None,
) -> str:
    """Queue a title and/or body replacement for the selected issue/PR (None = leave unchanged)."""
    finding = ctx.deps.finding
    if finding.content_id is None:
        return "Cannot queue edit: GitHub node id is missing."
    if title is not None and not title.strip():
        return "Cannot queue edit: title cannot be blank."
    if title is None and body is None:
        return "Nothing to edit: provide a title and/or body."
    existing = _find_write(ctx.deps.pending_writes, IssueEditPlan)
    if existing is None:
        existing = IssueEditPlan(
            content_id=finding.content_id,
            repository=finding.repository,
            item_type=finding.item_type,
            number=finding.number,
        )
        ctx.deps.pending_writes.append(existing)
    if title is not None:
        existing.title = title
    if body is not None:
        existing.body = body
    return "\n".join(["Queued edit preview:", *describe_pending_write(existing)])


def prepare_label_update(
    ctx: RunContext[ProjectAgentDeps],
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Queue adding and/or removing labels on the selected issue/PR."""
    finding = ctx.deps.finding
    if finding.content_id is None:
        return "Cannot queue label update: GitHub node id is missing."
    resolved_add, unknown_add = _resolve_names(add or [], ctx.deps.labels)
    resolved_remove, unknown_remove = _resolve_names(remove or [], ctx.deps.labels)
    unknown = sorted({*unknown_add, *unknown_remove})
    if not resolved_add and not resolved_remove:
        available = ", ".join(sorted(ctx.deps.labels)) or "none"
        return (
            f"No label change queued. Unknown label(s): {', '.join(unknown)}."
            f" Available: {available}"
        )
    existing = _find_write(ctx.deps.pending_writes, LabelUpdatePlan)
    if existing is None:
        existing = LabelUpdatePlan(
            content_id=finding.content_id,
            repository=finding.repository,
            item_type=finding.item_type,
            number=finding.number,
        )
        ctx.deps.pending_writes.append(existing)
    for name, label_id in resolved_add.items():
        existing.remove_label_ids.pop(name, None)
        existing.add_label_ids[name] = label_id
    for name, label_id in resolved_remove.items():
        existing.add_label_ids.pop(name, None)
        existing.remove_label_ids[name] = label_id
    lines = ["Queued label update preview:", *describe_pending_write(existing)]
    if unknown:
        lines.append(f"Unknown label(s) ignored: {', '.join(unknown)}")
    return "\n".join(lines)


def prepare_assignee_update(
    ctx: RunContext[ProjectAgentDeps],
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Queue adding and/or removing assignees on the selected issue/PR."""
    finding = ctx.deps.finding
    if finding.content_id is None:
        return "Cannot queue assignee update: GitHub node id is missing."
    resolved_add, unknown_add = _resolve_names(add or [], ctx.deps.assignable_users)
    resolved_remove, unknown_remove = _resolve_names(remove or [], ctx.deps.assignable_users)
    unknown = sorted({*unknown_add, *unknown_remove})
    if not resolved_add and not resolved_remove:
        available = ", ".join(sorted(ctx.deps.assignable_users)) or "none"
        return (
            f"No assignee change queued. Unknown user(s): {', '.join(unknown)}."
            f" Available: {available}"
        )
    existing = _find_write(ctx.deps.pending_writes, AssigneeUpdatePlan)
    if existing is None:
        existing = AssigneeUpdatePlan(
            content_id=finding.content_id,
            repository=finding.repository,
            item_type=finding.item_type,
            number=finding.number,
        )
        ctx.deps.pending_writes.append(existing)
    for login, user_id in resolved_add.items():
        existing.remove_user_ids.pop(login, None)
        existing.add_user_ids[login] = user_id
    for login, user_id in resolved_remove.items():
        existing.add_user_ids.pop(login, None)
        existing.remove_user_ids[login] = user_id
    lines = ["Queued assignee update preview:", *describe_pending_write(existing)]
    if unknown:
        lines.append(f"Unknown user(s) ignored: {', '.join(unknown)}")
    return "\n".join(lines)


def prepare_state_update(
    ctx: RunContext[ProjectAgentDeps],
    action: Literal["close", "reopen"],
    reason: Literal["COMPLETED", "NOT_PLANNED", "DUPLICATE"] | None = None,
) -> str:
    """Queue closing or reopening the selected issue/PR. reason only applies to issues."""
    finding = ctx.deps.finding
    if finding.content_id is None:
        return "Cannot queue state change: GitHub node id is missing."
    effective_reason = reason if finding.item_type == "issue" else None
    plan = StateUpdatePlan(
        content_id=finding.content_id,
        repository=finding.repository,
        item_type=finding.item_type,
        number=finding.number,
        action=action,
        reason=effective_reason,
    )
    _replace_write(ctx.deps.pending_writes, StateUpdatePlan, plan)
    lines = ["Queued state change preview:", *describe_pending_write(plan)]
    if reason is not None and effective_reason is None:
        lines.append("Note: close reasons only apply to issues, not pull requests - ignored.")
    return "\n".join(lines)


def prepare_milestone_update(
    ctx: RunContext[ProjectAgentDeps],
    milestone_title: str | None = None,
) -> str:
    """Set the selected issue/PR's milestone, or clear it when milestone_title is omitted."""
    finding = ctx.deps.finding
    if finding.content_id is None:
        return "Cannot queue milestone change: GitHub node id is missing."
    milestone_id: str | None = None
    resolved_title = milestone_title
    if milestone_title is not None:
        resolved, _unknown = _resolve_names([milestone_title], ctx.deps.milestones)
        if not resolved:
            available = ", ".join(sorted(ctx.deps.milestones)) or "none"
            return f"Milestone {milestone_title!r} not found. Available: {available}"
        resolved_title, milestone_id = next(iter(resolved.items()))
    plan = MilestoneUpdatePlan(
        content_id=finding.content_id,
        repository=finding.repository,
        item_type=finding.item_type,
        number=finding.number,
        milestone_id=milestone_id,
        milestone_title=resolved_title,
    )
    _replace_write(ctx.deps.pending_writes, MilestoneUpdatePlan, plan)
    return "\n".join(["Queued milestone change preview:", *describe_pending_write(plan)])


def prepare_pr_merge(
    ctx: RunContext[ProjectAgentDeps],
    merge_method: Literal["MERGE", "SQUASH", "REBASE"],
) -> str:
    """Queue merging the selected pull request. Not available for issues."""
    finding = ctx.deps.finding
    if finding.item_type != "pull_request":
        return "Cannot merge: the selected item is not a pull request."
    if finding.content_id is None:
        return "Cannot queue merge: GitHub node id is missing."
    plan = PullRequestMergePlan(
        content_id=finding.content_id,
        repository=finding.repository,
        number=finding.number,
        merge_method=merge_method,
    )
    _replace_write(ctx.deps.pending_writes, PullRequestMergePlan, plan)
    return "\n".join(
        ["Queued merge preview (merging is not easily reversible):", *describe_pending_write(plan)]
    )


def prepare_reviewer_request(ctx: RunContext[ProjectAgentDeps], logins: list[str]) -> str:
    """Queue a PR reviewer request (adds to, never replaces, the existing reviewer set)."""
    finding = ctx.deps.finding
    if finding.item_type != "pull_request":
        return "Cannot request reviewers: the selected item is not a pull request."
    if finding.content_id is None:
        return "Cannot queue reviewer request: GitHub node id is missing."
    resolved, unknown = _resolve_names(logins, ctx.deps.assignable_users)
    if not resolved:
        available = ", ".join(sorted(ctx.deps.assignable_users)) or "none"
        return (
            f"No reviewer request queued. Unknown user(s): {', '.join(unknown)}."
            f" Available: {available}"
        )
    existing = _find_write(ctx.deps.pending_writes, ReviewerRequestPlan)
    if existing is None:
        existing = ReviewerRequestPlan(
            content_id=finding.content_id, repository=finding.repository, number=finding.number
        )
        ctx.deps.pending_writes.append(existing)
    existing.user_ids.update(resolved)
    lines = ["Queued reviewer request preview:", *describe_pending_write(existing)]
    if unknown:
        lines.append(f"Unknown user(s) ignored: {', '.join(unknown)}")
    return "\n".join(lines)


def _find_write[T](pending_writes: list[PendingWrite], cls: type[T]) -> T | None:
    for write in pending_writes:
        if isinstance(write, cls):
            return write
    return None


def _replace_write[T](pending_writes: list[PendingWrite], cls: type[T], new: T) -> None:
    for index, write in enumerate(pending_writes):
        if isinstance(write, cls):
            pending_writes[index] = new  # type: ignore[assignment]
            return
    pending_writes.append(new)  # type: ignore[arg-type]


def _resolve_names(names: list[str], mapping: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Case-insensitively resolve display names to node ids; report names not found."""
    lower_map = {key.casefold(): (key, value) for key, value in mapping.items()}
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    for name in names:
        match = lower_map.get(name.casefold())
        if match is None:
            unknown.append(name)
        else:
            resolved[match[0]] = match[1]
    return resolved, unknown


def _make_project_agent(settings: Settings) -> Agent[ProjectAgentDeps, _ChatReply]:
    settings.validate_llm()
    from pydantic_ai import Agent, Tool

    key = _cache_key(settings, "project_agent")
    return _cached_agent(
        key,
        lambda: Agent(
            _make_model(settings),
            output_type=_ChatReply,
            instructions=_PROJECT_AGENT_INSTRUCTIONS,
            deps_type=ProjectAgentDeps,
            tools=[
                Tool(read_selected_item),
                Tool(prepare_add_to_project),
                Tool(prepare_project_field_update),
                Tool(prepare_issue_comment),
                Tool(prepare_issue_edit),
                Tool(prepare_label_update),
                Tool(prepare_assignee_update),
                Tool(prepare_state_update),
                Tool(prepare_milestone_update),
                Tool(prepare_pr_merge),
                Tool(prepare_reviewer_request),
            ],
        ),
    )


def _field_lines(fields: list[ProjectFieldDefinition]) -> list[str]:
    lines: list[str] = []
    for field_definition in fields:
        detail = field_definition.data_type
        if field_definition.kind == "single_select":
            detail += f" options={list(field_definition.options)}"
        if field_definition.kind == "iteration":
            detail += f" iterations={list(field_definition.iterations)}"
        lines.append(f"- {field_definition.name}: {detail}")
    return lines or ["- none"]


def _comment_lines(finding: AuditFinding) -> list[str]:
    if not finding.comments:
        return ["- none"]
    lines: list[str] = []
    for comment in finding.comments:
        author = comment.author or "unknown"
        updated = f" {comment.updated_at}" if comment.updated_at else ""
        body = " ".join(comment.body.split())
        if len(body) > 500:
            body = body[:497] + "..."
        lines.append(f"- {author}{updated}: {body}")
    return lines


# ── prompt builders ───────────────────────────────────────────────────────────


def build_prompt(finding: AuditFinding) -> str:
    return "\n".join(
        [
            f"Repository: {finding.repository}",
            f"Item: {finding.item_type} #{finding.number}",
            f"Title: <title>{finding.title}</title>",
            f"URL: {finding.url}",
            f"Assignees: {', '.join(finding.assignees) or 'none'}",
            f"Missing fields: {', '.join(finding.missing_fields)}",
            f"Current project fields: {finding.current_project_fields}",
            f"Development status: {finding.development_status}",
        ]
    )


def build_triage_prompt(findings: list[AuditFinding]) -> str:
    missing_counts: Counter[str] = Counter()
    repo_counts: Counter[str] = Counter()
    for f in findings:
        repo_counts[f.repository.split("/")[-1]] += 1
        for field in f.missing_fields:
            missing_counts[field] += 1

    lines = [
        f"Total findings: {len(findings)}",
        "",
        "Missing field frequency:",
        *[f"  {field}: {count}" for field, count in missing_counts.most_common(10)],
        "",
        "Top repositories by finding count:",
        *[f"  {repo}: {count}" for repo, count in repo_counts.most_common(5)],
        "",
        "Sample findings (first 15):",
        *[
            f"  [{f.item_type} #{f.number}] {f.title[:60]} — missing: {', '.join(f.missing_fields)}"
            for f in findings[:15]
        ],
    ]
    return "\n".join(lines)


def build_severity_prompt(findings: list[AuditFinding]) -> str:
    lines = [
        f"Score severity (HIGH/MEDIUM/LOW) for each of these {len(findings)} findings.",
        "Return one score per finding in the same order.",
        "",
    ]
    for i, f in enumerate(findings, 1):
        updated = f.updated_at[:10] if f.updated_at else "unknown"
        lines.append(
            f"{i}. [{f.item_type} #{f.number}] {f.title[:60]}"
            f" (repo: {f.repository.split('/')[-1]}, updated: {updated},"
            f" missing: {', '.join(f.missing_fields)})"
        )
    return "\n".join(lines)


def build_explain_prompt(finding: AuditFinding, rule: str) -> str:
    return "\n".join(
        [
            f"Rule triggered: {rule}",
            f"Item: {finding.item_type} #{finding.number} — <title>{finding.title}</title>",
            f"Repository: {finding.repository}",
            f"Assignees: {', '.join(finding.assignees) or 'none'}",
            f"All missing fields: {', '.join(finding.missing_fields)}",
            f"Updated: {finding.updated_at or 'unknown'}",
        ]
    )


def build_nl_prompt(
    query: str,
    available_repos: list[str],
    available_assignees: list[str],
    available_fields: list[str],
) -> str:
    return "\n".join(
        [
            f"User query: <query>{query[:500]}</query>",
            "",
            f"Available repositories: {', '.join(available_repos) or 'none'}",
            f"Available assignees: {', '.join(available_assignees) or 'none'}",
            f"Available missing fields: {', '.join(available_fields) or 'none'}",
            "Available item types: Issue, PR",
            "",
            "Map the query to filter criteria using only the available values above.",
        ]
    )
