from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import uuid4

from github_audit.agent_chat import parse_agent_command
from github_audit.applier import add_comment, apply_plan, describe_changes
from github_audit.config import Settings, load_settings
from github_audit.discovery import discover_all
from github_audit.github_client import GitHubClient
from github_audit.llm_evaluator import general_chat, general_chat_stream, project_agent_chat
from github_audit.models import (
    ApplyPlan,
    AuditFinding,
    AuditResult,
    IssueCommentPlan,
    ProjectFieldDefinition,
)
from github_audit.report import audit_text
from github_audit.scanner import scan_all


class ChatServiceError(RuntimeError):
    status_code = 500


class ChatInputError(ChatServiceError):
    status_code = 400


class ChatUnavailableError(ChatServiceError):
    status_code = 503


def _empty_questions() -> list[str]:
    return []


@dataclass(frozen=True)
class ChatResult:
    conversation_id: str
    answer: str
    next_questions: list[str] = field(default_factory=_empty_questions)


@dataclass
class ConversationState:
    pending_plan: ApplyPlan | None = None
    pending_project_id: str | None = None
    pending_fields: list[ProjectFieldDefinition] | None = None
    pending_comment: IssueCommentPlan | None = None
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
        if parse_agent_command(message).apply_pending:
            return ChatResult(session_id, self._apply_pending(settings, state))

        snapshot = self._scan_snapshot(settings)
        if context:
            return self._reply_with_finding(settings, snapshot, state, session_id, message, context)
        if not _llm_ready(settings):
            raise ChatUnavailableError("LLM is not configured")
        reply, _ = general_chat(message, snapshot.context, settings)
        return ChatResult(session_id, reply)

    def stream(
        self,
        message: str,
        conversation_id: str | None = None,
        context: str | None = None,
    ) -> tuple[Iterator[str], Callable[[], ChatResult]] | None:
        message = message.strip()
        if not message:
            raise ChatInputError("message is empty")
        if context or parse_agent_command(message).apply_pending:
            return None
        settings = load_settings()
        if not _llm_ready(settings):
            raise ChatUnavailableError("LLM is not configured")
        session_id, _ = self._sessions.get(conversation_id)
        snapshot = self._scan_snapshot(settings)
        chunks: list[str] = []

        def tokens() -> Iterator[str]:
            for chunk in general_chat_stream(message, snapshot.context, settings):
                chunks.append(chunk)
                yield chunk

        def finalise() -> ChatResult:
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
        result = project_agent_chat(
            message,
            snapshot.context,
            finding,
            fields,
            project_id,
            settings,
        )
        preview: list[str] = []
        if result.project_plan is not None and result.fields is not None:
            state.pending_plan = result.project_plan
            state.pending_project_id = result.project_id
            state.pending_fields = result.fields
            preview.extend(describe_changes(result.project_plan.changes))
        if result.comment_plan is not None:
            state.pending_comment = result.comment_plan
            preview.append(_comment_preview(result.comment_plan))
        answer = result.reply
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
        has_fields = state.pending_plan is not None and bool(state.pending_plan.changes)
        if not has_fields and state.pending_comment is None:
            return "No pending write."
        if not settings.auto_apply:
            return "Write blocked: AUTO_APPLY must be true."
        applied = 0
        skipped: list[str] = []
        field_write_done = False
        with GitHubClient(settings.github_token) as client:
            if has_fields:
                if state.pending_project_id is None or state.pending_fields is None:
                    raise ChatUnavailableError("pending project metadata is missing")
                result = apply_plan(
                    client,
                    state.pending_plan or ApplyPlan(changes=[]),
                    state.pending_project_id,
                    state.pending_fields,
                    dry_run=False,
                    allow_write=True,
                )
                applied = len(result.applied)
                skipped.extend(result.skipped)
                field_write_done = True
            if state.pending_comment is not None:
                try:
                    add_comment(
                        client,
                        state.pending_comment.subject_id,
                        state.pending_comment.body,
                    )
                except Exception:
                    if field_write_done:
                        state.pending_plan = None
                        state.pending_project_id = None
                        state.pending_fields = None
                        self._clear_snapshot()
                    raise
        state.pending_plan = None
        state.pending_project_id = None
        state.pending_fields = None
        state.pending_comment = None
        self._clear_snapshot()
        skipped_text = "; ".join(skipped) if skipped else "none"
        return f"Applied {applied} field change(s). Skipped: {skipped_text}."

    def _scan_snapshot(self, settings: Settings) -> ProjectSnapshot:
        now = datetime.now(UTC)
        with self._snapshot_lock:
            if self._snapshot is not None and now - self._snapshot.created_at < timedelta(
                seconds=60
            ):
                return self._snapshot
            with GitHubClient(settings.github_token) as client:
                discoveries = discover_all(client, settings)
                audits = scan_all(client, settings, discoveries)
            findings = {
                _finding_key(finding): finding for audit in audits for finding in audit.findings
            }
            snapshot = ProjectSnapshot(
                created_at=now,
                audits=audits,
                findings=findings,
                project_ids={item.project_number: item.project_id for item in discoveries},
                fields={item.project_number: item.fields for item in discoveries},
                context=_scan_context(audits),
            )
            self._snapshot = snapshot
            return snapshot

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


def _comment_preview(comment: IssueCommentPlan) -> str:
    body = comment.body.replace("\n", " ")
    if len(body) > 100:
        body = body[:97] + "..."
    return f"dry-run: {comment.repository}#{comment.number} add comment={body!r}"


def _llm_ready(settings: Settings) -> bool:
    try:
        settings.validate_llm()
    except ValueError:
        return False
    return True
