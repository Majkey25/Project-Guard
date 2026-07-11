from __future__ import annotations

from collections import Counter
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import Future
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from github_audit.agent_chat import should_apply_now
from github_audit.applier import (
    PartialApplyError,
    apply_pending_write,
    describe_pending_write,
    resolve_created_item_ids,
)
from github_audit.config import Settings, load_settings
from github_audit.discovery import discover_all, discover_repositories
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.llm_evaluator import (
    general_chat,
    general_chat_stream,
    project_agent_chat,
    trim_message_history,
)
from github_audit.models import (
    AddToProjectPlan,
    ApplyPlan,
    AuditFinding,
    AuditResult,
    PendingWrite,
    ProjectFieldDefinition,
)
from github_audit.project_fields import (
    fetch_assignable_users,
    fetch_repo_labels,
    fetch_repo_milestones,
    search_items,
)
from github_audit.report import audit_text
from github_audit.scanner import scan_all

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

_MAX_HISTORY_MESSAGES = 20


class ChatServiceError(RuntimeError):
    status_code = 500


class ChatInputError(ChatServiceError):
    status_code = 400


class ChatUnavailableError(ChatServiceError):
    status_code = 503


def _empty_questions() -> list[str]:
    return []


def _empty_pending_writes() -> list[PendingWrite]:
    return []


def _empty_message_history() -> list[ModelMessage]:
    return []


@dataclass(frozen=True)
class ChatResult:
    conversation_id: str
    answer: str
    next_questions: list[str] = field(default_factory=_empty_questions)


@dataclass
class ConversationState:
    pending_writes: list[PendingWrite] = field(default_factory=_empty_pending_writes)
    pending_project_id: str | None = None
    pending_fields: list[ProjectFieldDefinition] | None = None
    # which selected item the queued writes belong to; used to detect a context switch
    pending_content_id: str | None = None
    message_history: list[ModelMessage] = field(default_factory=_empty_message_history)
    touched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ProjectSnapshot:
    created_at: datetime
    audits: list[AuditResult]
    findings: dict[str, AuditFinding]
    project_ids: dict[int, str]
    fields: dict[int, list[ProjectFieldDefinition]]
    context: str


class ConversationStore:
    def __init__(
        self,
        *,
        max_sessions: int = 100,
        ttl: timedelta = timedelta(hours=2),
    ) -> None:
        self._max_sessions = max_sessions
        self._ttl = ttl
        self._lock = Lock()
        self._items: dict[str, ConversationState] = {}

    def get(self, conversation_id: str | None) -> tuple[str, ConversationState]:
        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            key = conversation_id or uuid4().hex
            state = self._items.get(key)
            if state is None:
                state = ConversationState()
                self._items[key] = state
                self._prune_to_limit()
            state.touched_at = now
            return key, state

    def _prune(self, now: datetime) -> None:
        expired = [key for key, state in self._items.items() if now - state.touched_at > self._ttl]
        for key in expired:
            del self._items[key]

    def _prune_to_limit(self) -> None:
        overflow = len(self._items) - self._max_sessions
        if overflow <= 0:
            return
        oldest = sorted(self._items, key=lambda key: self._items[key].touched_at)
        for key in oldest[:overflow]:
            del self._items[key]


class ProjectGuardChatService:
    def __init__(self) -> None:
        self._sessions = ConversationStore()
        self._snapshot: ProjectSnapshot | None = None
        self._snapshot_lock = Lock()
        self._snapshot_refresh: Future[ProjectSnapshot] | None = None

    def status(self) -> dict[str, object]:
        try:
            settings = load_settings()
        except ValueError as exc:
            return {
                "ok": True,
                "configured": False,
                "llm_ready": False,
                "configuration_error": str(exc),
                "project_guard": "api",
            }
        return {
            "ok": True,
            "configured": True,
            "llm_ready": _llm_ready(settings),
            "project_guard": "api",
        }

    def context_options(self) -> list[dict[str, str]]:
        snapshot = self._scan_snapshot(load_settings())
        return [
            {"value": key, "label": _finding_label(finding)}
            for key, finding in list(snapshot.findings.items())[:100]
        ]

    def reply(
        self,
        message: str,
        conversation_id: str | None = None,
        context: str | None = None,
    ) -> ChatResult:
        message = message.strip()
        if not message:
            raise ChatInputError("message is empty")
        settings = load_settings()
        session_id, state = self._sessions.get(conversation_id)
        if should_apply_now(message):
            if context and state.pending_writes:
                # The queued writes belong to one item; confirming while a different
                # item is selected must never write to the previously selected one.
                finding = self._scan_snapshot(settings).findings.get(context)
                if finding is None or finding.content_id != state.pending_content_id:
                    count = len(state.pending_writes)
                    state.pending_writes = []
                    state.pending_project_id = None
                    state.pending_fields = None
                    state.pending_content_id = None
                    return ChatResult(
                        session_id,
                        f"Discarded {count} queued write(s) because you switched items."
                        " Nothing was applied. Ask again on the currently selected item.",
                    )
            return ChatResult(session_id, self._apply_pending(settings, state))

        snapshot = self._scan_snapshot(settings)
        if context:
            return self._reply_with_finding(settings, snapshot, state, session_id, message, context)
        if not _llm_ready(settings):
            raise ChatUnavailableError("LLM is not configured")
        reply, new_messages = general_chat(
            message,
            snapshot.context,
            settings,
            message_history=state.message_history,
            in_tool_commands=False,
        )
        state.message_history = trim_message_history(
            state.message_history + new_messages, _MAX_HISTORY_MESSAGES
        )
        return ChatResult(session_id, reply)

    def stream(
        self,
        message: str,
        conversation_id: str | None = None,
        context: str | None = None,
    ) -> tuple[AsyncGenerator[str, None], Callable[[], ChatResult]] | None:
        message = message.strip()
        if not message:
            raise ChatInputError("message is empty")
        if context:
            return None
        if conversation_id is None:
            # a brand-new conversation cannot have queued writes; don't create a
            # session here or the reply() fallback would create a second, orphaned one
            if should_apply_now(message):
                return None
            session_id, state = self._sessions.get(None)
        else:
            session_id, state = self._sessions.get(conversation_id)
            if should_apply_now(message):
                return None
        settings = load_settings()
        if not _llm_ready(settings):
            raise ChatUnavailableError("LLM is not configured")
        snapshot = self._scan_snapshot(settings)
        chunks: list[str] = []
        token_iter, get_new_messages = general_chat_stream(
            message,
            snapshot.context,
            settings,
            message_history=state.message_history,
            in_tool_commands=False,
        )

        async def tokens() -> AsyncGenerator[str, None]:
            # aclosing: closing this generator must also close the LLM stream.
            async with aclosing(token_iter):
                async for chunk in token_iter:
                    chunks.append(chunk)
                    yield chunk

        def finalise() -> ChatResult:
            new_messages = get_new_messages()
            state.message_history = trim_message_history(
                state.message_history + new_messages, _MAX_HISTORY_MESSAGES
            )
            return ChatResult(session_id, "".join(chunks))

        return tokens(), finalise

    def _reply_with_finding(
        self,
        settings: Settings,
        snapshot: ProjectSnapshot,
        state: ConversationState,
        session_id: str,
        message: str,
        context: str,
    ) -> ChatResult:
        finding = snapshot.findings.get(context)
        if finding is None:
            raise ChatInputError("selected context was not found")
        if not _llm_ready(settings):
            raise ChatUnavailableError("LLM is not configured")
        project_number = finding.project_number
        project_id = (
            snapshot.project_ids.get(project_number) if project_number is not None else None
        )
        fields = snapshot.fields.get(project_number, []) if project_number is not None else []

        discarded_notice = ""
        if state.pending_writes and state.pending_content_id != finding.content_id:
            discarded_notice = (
                f"\n\n(Note: {len(state.pending_writes)} unapplied write(s) queued for a"
                " different item were discarded because you switched items.)"
            )
            state.pending_writes = []
            state.pending_project_id = None
            state.pending_fields = None
            state.pending_content_id = None

        with GitHubClient(settings.github_token) as client:
            labels = fetch_repo_labels(client, finding.repository)
            milestones = fetch_repo_milestones(client, finding.repository)
            assignable_users = fetch_assignable_users(client, finding.repository)

        result = project_agent_chat(
            message,
            snapshot.context,
            finding,
            fields,
            project_id,
            settings,
            labels=labels,
            milestones=milestones,
            assignable_users=assignable_users,
            existing_writes=state.pending_writes,
            message_history=state.message_history,
        )
        state.message_history = trim_message_history(
            state.message_history + result.new_messages, _MAX_HISTORY_MESSAGES
        )
        preview: list[str] = []
        if result.pending_writes:
            state.pending_writes = result.pending_writes
            state.pending_project_id = result.project_id
            state.pending_fields = result.fields
            state.pending_content_id = finding.content_id
            for write in result.pending_writes:
                preview.extend(describe_pending_write(write))
        answer = result.reply + discarded_notice
        if preview:
            answer = "\n\n".join(
                [
                    answer,
                    "Prepared write preview:",
                    "\n".join(preview),
                    "Reply `apply it` to write. Requires `AUTO_APPLY=true`.",
                ]
            )
        return ChatResult(session_id, answer)

    def _apply_pending(self, settings: Settings, state: ConversationState) -> str:
        if not state.pending_writes:
            return "No pending write."
        if not settings.auto_apply:
            return "Write blocked: AUTO_APPLY must be true."
        # Board adds must run first — later field updates resolve their project
        # item id from the add's result via created_item_ids.
        state.pending_writes = [
            w for w in state.pending_writes if isinstance(w, AddToProjectPlan)
        ] + [w for w in state.pending_writes if not isinstance(w, AddToProjectPlan)]
        created_item_ids: dict[str, str] = {}
        applied = 0
        with GitHubClient(settings.github_token) as client:
            for index, write in enumerate(state.pending_writes):
                try:
                    apply_pending_write(
                        client,
                        write,
                        project_id=state.pending_project_id,
                        fields=state.pending_fields,
                        created_item_ids=created_item_ids,
                    )
                except PartialApplyError as exc:
                    state.pending_writes = _trim_partial_apply(
                        write, exc, state.pending_writes[index + 1 :]
                    )
                    # keep the retry queue self-contained: adds already consumed
                    # can no longer resolve empty project item ids on the next run
                    resolve_created_item_ids(state.pending_writes, created_item_ids)
                    self._clear_snapshot()
                    return (
                        f"Applied {applied} write(s) fully, then failed on write {index + 1}"
                        f" after {len(exc.applied)} field change(s) went through: {exc}."
                        f" {len(state.pending_writes)} write(s) remain queued -"
                        " say `apply it` to retry."
                    )
                except (GitHubError, ValueError) as exc:
                    state.pending_writes = state.pending_writes[index:]
                    resolve_created_item_ids(state.pending_writes, created_item_ids)
                    self._clear_snapshot()
                    return (
                        f"Applied {applied} write(s), then failed on write {index + 1}: {exc}."
                        f" {len(state.pending_writes)} write(s) remain queued -"
                        " say `apply it` to retry."
                    )
                applied += 1
        state.pending_writes = []
        state.pending_project_id = None
        state.pending_fields = None
        state.pending_content_id = None
        self._clear_snapshot()
        return f"Applied {applied} write(s)."

    def _scan_snapshot(self, settings: Settings) -> ProjectSnapshot:
        now = datetime.now(UTC)
        with self._snapshot_lock:
            if self._snapshot is not None and now - self._snapshot.created_at < timedelta(
                seconds=60
            ):
                return self._snapshot
            in_flight = self._snapshot_refresh
            am_leader = in_flight is None
            if am_leader:
                in_flight = self._snapshot_refresh = Future()
        if not am_leader:
            # Someone else is already refreshing; wait on their result instead of
            # starting a second full scan and instead of blocking everyone else's
            # unrelated /chat and /context requests behind this thread's lock.
            assert in_flight is not None
            return in_flight.result()
        try:
            snapshot = self._run_scan(settings, now)
        except BaseException as exc:
            with self._snapshot_lock:
                self._snapshot_refresh = None
            in_flight.set_exception(exc)
            raise
        with self._snapshot_lock:
            self._snapshot = snapshot
            self._snapshot_refresh = None
        in_flight.set_result(snapshot)
        return snapshot

    def _run_scan(self, settings: Settings, now: datetime) -> ProjectSnapshot:
        with GitHubClient(settings.github_token) as client:
            repositories = discover_repositories(client, settings)
            searched_items = search_items(
                client,
                repositories,
                settings.target_assignees,
                include_issues=settings.include_issues,
                include_pull_requests=settings.include_pull_requests,
                include_closed_issues=settings.include_closed_issues,
                include_closed_pull_requests=settings.include_closed_pull_requests,
                include_unassigned=settings.include_unassigned,
            )
            discoveries = discover_all(
                client, settings, repositories=repositories, searched_items=searched_items
            )
            audits = scan_all(client, settings, discoveries, searched_items)
        findings = {
            _finding_key(finding): finding for audit in audits for finding in audit.findings
        }
        return ProjectSnapshot(
            created_at=now,
            audits=audits,
            findings=findings,
            project_ids={item.project_number: item.project_id for item in discoveries},
            fields={item.project_number: item.fields for item in discoveries},
            context=_scan_context(audits),
        )

    def _clear_snapshot(self) -> None:
        with self._snapshot_lock:
            self._snapshot = None


def _scan_context(audits: list[AuditResult]) -> str:
    findings = [finding for audit in audits for finding in audit.findings]
    missing = Counter(field for finding in findings for field in finding.missing_fields)
    lines = [
        f"Projects: {len(audits)}",
        f"Findings: {len(findings)}",
        "Missing fields: "
        + (", ".join(f"{field}={count}" for field, count in missing.most_common()) or "none"),
        "",
    ]
    lines.extend(audit_text(audit) for audit in audits)
    return "\n\n".join(lines)


def _finding_key(finding: AuditFinding) -> str:
    parts = (finding.project_number or 0, finding.repository, finding.item_type, finding.number)
    return "|".join(str(part) for part in parts)


def _finding_label(finding: AuditFinding) -> str:
    missing = ", ".join(finding.missing_fields)
    return f"{finding.repository} #{finding.number}: {missing}"


def _trim_partial_apply(
    write: PendingWrite, exc: PartialApplyError, rest: list[PendingWrite]
) -> list[PendingWrite]:
    """Rebuild the failed ApplyPlan write with only its unapplied changes, keeping later writes."""
    assert isinstance(write, ApplyPlan)
    remaining_changes = [change for change in write.changes if change not in exc.applied]
    if not remaining_changes:
        return rest
    trimmed = ApplyPlan(changes=remaining_changes, skipped=exc.skipped)
    return [trimmed, *rest]


def _llm_ready(settings: Settings) -> bool:
    try:
        settings.validate_llm()
    except ValueError:
        return False
    return True
