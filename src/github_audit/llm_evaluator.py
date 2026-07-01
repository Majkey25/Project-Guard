from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic_ai import RunContext

from github_audit.agent_chat import FieldRequest, build_field_plan
from github_audit.applier import describe_changes
from github_audit.config import Settings
from github_audit.models import (
    ApplyPlan,
    AuditFinding,
    BatchTriageResult,
    IssueCommentPlan,
    LLMSuggestion,
    NLFilterResult,
    ProjectFieldDefinition,
    RuleExplanation,
    SeverityScore,
    SeverityScoreList,
)

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel

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
You are the Project Guard assistant. Use tools to inspect the selected GitHub issue or PR and
to queue supported GitHub writes. Supported writes are GitHub Project V2 field updates and new
issue/PR comments. Writes are previews only until the user confirms with `apply it`.
If the user asks for unsupported writes such as editing title, body, labels, assignees, state,
milestone, branches, or creating items, say that this tool cannot do that yet.
Use exact Project field names and available option names from the tool output.
Never claim a write already happened.
"""


class _ChatReply(BaseModel):
    reply: str


@dataclass
class ProjectAgentDeps:
    finding: AuditFinding
    project_id: str | None
    fields: list[ProjectFieldDefinition]
    project_plan: ApplyPlan = dataclass_field(default_factory=lambda: ApplyPlan(changes=[]))
    comment_plan: IssueCommentPlan | None = None


@dataclass(frozen=True)
class ProjectAgentResult:
    reply: str
    project_plan: ApplyPlan | None
    project_id: str | None
    fields: list[ProjectFieldDefinition] | None
    comment_plan: IssueCommentPlan | None


def _make_agent[LLMOutputT: BaseModel](
    settings: Settings,
    output_type: type[LLMOutputT],
    instructions: str,
) -> Agent[object, LLMOutputT]:
    settings.validate_llm()
    from pydantic_ai import Agent

    return Agent(_make_model(settings), output_type=output_type, instructions=instructions)


def _make_model(settings: Settings) -> OpenAIChatModel:
    provider_name = settings.llm_provider_name
    if provider_name == "azure":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.azure import AzureProvider

        provider = AzureProvider(
            azure_endpoint=settings.llm_base_url,
            api_key=settings.llm_api_key,
            api_version=settings.llm_api_version or None,
        )
        model = OpenAIChatModel(settings.llm_model_name, provider=provider)
    elif provider_name in {"openai", "openai-compatible"}:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
        )
        model = OpenAIChatModel(settings.llm_model_name, provider=provider)
    elif provider_name == "ollama":
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider

        base_url = settings.llm_base_url or "http://localhost:11434/v1"
        model = OllamaModel(settings.llm_model_name, provider=OllamaProvider(base_url=base_url))
    else:
        msg = (
            f"unsupported LLM_PROVIDER={settings.llm_provider!r}; "
            "supported: openai, azure, openai-compatible, ollama"
        )
        raise ValueError(msg)
    return model


def suggest_for_finding(finding: AuditFinding, settings: Settings) -> LLMSuggestion:
    agent = _make_agent(settings, LLMSuggestion, _SUGGEST_INSTRUCTIONS)
    return agent.run_sync(build_prompt(finding)).output


def batch_triage(findings: list[AuditFinding], settings: Settings) -> BatchTriageResult:
    agent = _make_agent(settings, BatchTriageResult, _TRIAGE_INSTRUCTIONS)
    return agent.run_sync(build_triage_prompt(findings)).output


def score_severities(findings: list[AuditFinding], settings: Settings) -> list[SeverityScore]:
    agent = _make_agent(settings, SeverityScoreList, _SEVERITY_INSTRUCTIONS)
    return agent.run_sync(build_severity_prompt(findings)).output.scores


def explain_finding(finding: AuditFinding, rule: str, settings: Settings) -> RuleExplanation:
    agent = _make_agent(settings, RuleExplanation, _EXPLAIN_INSTRUCTIONS)
    return agent.run_sync(build_explain_prompt(finding, rule)).output


def nl_to_filters(
    query: str,
    available_repos: list[str],
    available_assignees: list[str],
    available_fields: list[str],
    settings: Settings,
) -> NLFilterResult:
    agent = _make_agent(settings, NLFilterResult, _NL_FILTER_INSTRUCTIONS)
    return agent.run_sync(
        build_nl_prompt(query, available_repos, available_assignees, available_fields)
    ).output


def general_chat(prompt: str, context: str, settings: Settings) -> str:
    agent = _make_agent(settings, _ChatReply, _CHAT_INSTRUCTIONS)
    return agent.run_sync(f"{context}\n\nUser: {prompt}").output.reply


def general_chat_stream(prompt: str, context: str, settings: Settings) -> Iterator[str]:
    settings.validate_llm()
    from pydantic_ai import Agent

    agent = Agent(_make_model(settings), instructions=_CHAT_INSTRUCTIONS)
    result = agent.run_stream_sync(f"{context}\n\nUser: {prompt}")
    yield from result.stream_text(delta=True, debounce_by=None)


def project_agent_chat(
    prompt: str,
    context: str,
    finding: AuditFinding,
    fields: list[ProjectFieldDefinition],
    project_id: str | None,
    settings: Settings,
) -> ProjectAgentResult:
    agent = _make_project_agent(settings)
    deps = ProjectAgentDeps(finding=finding, project_id=project_id, fields=fields)
    result = agent.run_sync(f"{context}\n\nUser: {prompt}", deps=deps)
    project_plan = deps.project_plan if deps.project_plan.changes else None
    return ProjectAgentResult(
        reply=result.output.reply,
        project_plan=project_plan,
        project_id=project_id if project_plan else None,
        fields=fields if project_plan else None,
        comment_plan=deps.comment_plan,
    )


def read_selected_item(ctx: RunContext[ProjectAgentDeps]) -> str:
    """Read selected issue/PR details, current Project fields, and writable field options."""
    finding = ctx.deps.finding
    lines = [
        f"Item: {finding.item_type} #{finding.number}",
        f"Repository: {finding.repository}",
        f"Title: {finding.title}",
        f"URL: {finding.url}",
        f"Assignees: {', '.join(finding.assignees) or 'none'}",
        f"Missing fields: {', '.join(finding.missing_fields) or 'none'}",
        f"Current Project fields: {finding.current_project_fields or 'none'}",
        f"Body:\n{finding.body or '(empty)'}",
        "",
        f"Comments: showing {len(finding.comments)} of {finding.comments_total_count}",
        *_comment_lines(finding),
        "",
        "Writable Project fields:",
        *_field_lines(ctx.deps.fields),
    ]
    if finding.content_id is None:
        lines.append("Comments cannot be added because the GitHub node id is missing.")
    return "\n".join(lines)


def prepare_project_field_update(
    ctx: RunContext[ProjectAgentDeps],
    field_name: str,
    value: str,
) -> str:
    """Queue one Project V2 field update preview for the selected issue/PR."""
    finding = ctx.deps.finding
    if ctx.deps.project_id is None:
        return "Cannot queue Project field update: project id is missing."
    plan = build_field_plan(
        finding,
        ctx.deps.fields,
        FieldRequest(field_name, value),
        replace_existing=True,
    )
    if not plan.changes:
        return "\n".join(plan.skipped or ["No Project field update was queued."])
    change_names = {change.field_name for change in plan.changes}
    ctx.deps.project_plan.changes = [
        change for change in ctx.deps.project_plan.changes if change.field_name not in change_names
    ]
    ctx.deps.project_plan.changes.extend(plan.changes)
    ctx.deps.project_plan.skipped.extend(plan.skipped)
    return "\n".join(["Queued Project field update preview:", *describe_changes(plan.changes)])


def prepare_issue_comment(ctx: RunContext[ProjectAgentDeps], body: str) -> str:
    """Queue a new issue/PR comment preview for the selected item."""
    finding = ctx.deps.finding
    body = body.strip()
    if finding.content_id is None:
        return "Cannot queue comment: GitHub node id is missing."
    if not body:
        return "Cannot queue comment: body is empty."
    ctx.deps.comment_plan = IssueCommentPlan(
        subject_id=finding.content_id,
        repository=finding.repository,
        item_type=finding.item_type,
        number=finding.number,
        body=body,
    )
    return f"Queued comment preview for {finding.repository}#{finding.number}."


def _make_project_agent(settings: Settings) -> Agent[ProjectAgentDeps, _ChatReply]:
    settings.validate_llm()
    from pydantic_ai import Agent, Tool

    return Agent(
        _make_model(settings),
        output_type=_ChatReply,
        instructions=_PROJECT_AGENT_INSTRUCTIONS,
        deps_type=ProjectAgentDeps,
        tools=[
            Tool(read_selected_item),
            Tool(prepare_project_field_update),
            Tool(prepare_issue_comment),
        ],
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
