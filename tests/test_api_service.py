# pyright: reportPrivateUsage=false
# Exercises _apply_pending/_sessions directly: these are the write-execution safety path and
# session store, worth covering without wiring a full GitHubClient + LLM agent through reply().
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from github_audit.api.service import (
    ChatServiceError,
    ConversationState,
    ConversationStore,
    ProjectGuardChatService,
    ProjectSnapshot,
    _settings_scope,
)
from github_audit.applier import PartialApplyError
from github_audit.config import Settings
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.llm_evaluator import ProjectAgentResult
from github_audit.models import (
    ApplyChange,
    ApplyPlan,
    AuditFinding,
    AuditResult,
    IssueCommentPlan,
    PendingWrite,
    ProjectFieldDefinition,
)


def _fields() -> list[ProjectFieldDefinition]:
    return [
        ProjectFieldDefinition(id="f-estimate", name="Estimate", data_type="NUMBER", kind="field")
    ]


def _settings(*, auto_apply: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "github_token": "token",
            "github_project_numbers_raw": "1",
            "github_include_all_repositories": True,
            "require_target_assignee": False,
            "auto_apply": auto_apply,
        }
    )


def _change(field_name: str = "Estimate", value: object = 3) -> ApplyChange:
    return ApplyChange(
        repository="org/repo",
        item_type="issue",
        number=1,
        project_item_id="item-1",
        field_name=field_name,
        value=value,  # type: ignore[arg-type]
    )


def _comment(body: str = "hi") -> IssueCommentPlan:
    return IssueCommentPlan(
        subject_id="I_1", repository="org/repo", item_type="issue", number=1, body=body
    )


def _finding(number: int, content_id: str) -> AuditFinding:
    return AuditFinding(
        content_id=content_id,
        repository="org/repo",
        item_type="issue",
        number=number,
        title=f"Issue {number}",
        url=f"https://github.com/org/repo/issues/{number}",
        assignees=["alice"],
        missing_fields=["Estimate"],
        development_status="linked_pull_requests=0",
    )


def _snapshot(*findings: AuditFinding) -> ProjectSnapshot:
    return ProjectSnapshot(
        created_at=datetime.now(UTC),
        audits=[
            AuditResult(
                organization="org",
                repositories=["org/repo"],
                findings=list(findings),
                scanned_issue_count=len(findings),
                scanned_pull_request_count=0,
            )
        ],
        findings={str(finding.number): finding for finding in findings},
        project_ids={},
        fields={},
        context="context",
    )


# ── _apply_pending ───────────────────────────────────────────────────────────


def test_apply_pending_with_nothing_queued() -> None:
    service = ProjectGuardChatService()
    state = ConversationState()
    assert service._apply_pending(_settings(), state) == "No pending write."


def test_apply_pending_blocked_without_auto_apply() -> None:
    service = ProjectGuardChatService()
    state = ConversationState(pending_writes=[_comment()])
    result = service._apply_pending(_settings(auto_apply=False), state)
    assert "AUTO_APPLY" in result
    assert state.pending_writes  # left untouched for a later retry


def test_apply_pending_applies_all_writes_and_clears_state() -> None:
    service = ProjectGuardChatService()
    state = ConversationState(
        pending_writes=[ApplyPlan(changes=[_change()]), _comment()],
        pending_project_id="PVT_1",
        pending_fields=_fields(),
        pending_content_id="I_1",
    )
    with patch("github_audit.api.service.GitHubClient") as client_cls:
        client_cls.return_value.__enter__.return_value = MagicMock()
        result = service._apply_pending(_settings(), state)
    assert result == "Applied 2 write(s)."
    assert state.pending_writes == []
    assert state.pending_project_id is None
    assert state.pending_content_id is None


def test_apply_pending_keeps_unapplied_remainder_on_github_error() -> None:
    service = ProjectGuardChatService()
    first = _comment("first")
    second = _comment("second")
    state = ConversationState(pending_writes=[first, second])
    with patch("github_audit.api.service.GitHubClient") as client_cls:
        client_cls.return_value.__enter__.return_value = MagicMock()
        with patch(
            "github_audit.api.service.apply_pending_write",
            side_effect=[None, GitHubError("boom")],
        ):
            result = service._apply_pending(_settings(), state)
    assert "Applied 1 write(s)" in result
    assert "1 write(s) remain queued" in result
    assert state.pending_writes == [second]


def test_apply_pending_trims_partial_apply_plan_to_unapplied_changes() -> None:
    service = ProjectGuardChatService()
    applied_change = _change("Estimate", 3)
    unapplied_change = _change("Priority", "P1")
    plan = ApplyPlan(changes=[applied_change, unapplied_change])
    trailing = _comment("later")
    state = ConversationState(
        pending_writes=[plan, trailing], pending_project_id="PVT_1", pending_fields=[]
    )
    error = PartialApplyError(applied=[applied_change], skipped=[], cause=GitHubError("boom"))
    with patch("github_audit.api.service.GitHubClient") as client_cls:
        client_cls.return_value.__enter__.return_value = MagicMock()
        with patch("github_audit.api.service.apply_pending_write", side_effect=error):
            result = service._apply_pending(_settings(), state)
    assert "1 field change(s) went through" in result
    remaining_plan = state.pending_writes[0]
    assert isinstance(remaining_plan, ApplyPlan)
    assert remaining_plan.changes == [unapplied_change]
    assert state.pending_writes[1] == trailing


def test_apply_pending_drops_apply_plan_entirely_once_fully_applied() -> None:
    service = ProjectGuardChatService()
    only_change = _change()
    plan = ApplyPlan(changes=[only_change])
    state = ConversationState(pending_writes=[plan], pending_project_id="PVT_1", pending_fields=[])
    error = PartialApplyError(applied=[only_change], skipped=[], cause=GitHubError("boom"))
    with patch("github_audit.api.service.GitHubClient") as client_cls:
        client_cls.return_value.__enter__.return_value = MagicMock()
        with patch("github_audit.api.service.apply_pending_write", side_effect=error):
            service._apply_pending(_settings(), state)
    assert state.pending_writes == []


def test_reply_with_finding_discards_stale_pending_write_on_context_switch() -> None:
    service = ProjectGuardChatService()
    state = ConversationState(pending_writes=[_comment()], pending_content_id="I_1")
    snapshot = _snapshot(_finding(2, "I_2"))
    with (
        patch("github_audit.api.service._llm_ready", return_value=True),
        patch("github_audit.api.service.GitHubClient") as client_cls,
        patch("github_audit.api.service.fetch_repo_labels", return_value={}),
        patch("github_audit.api.service.fetch_repo_milestones", return_value={}),
        patch("github_audit.api.service.fetch_assignable_users", return_value={}),
        patch(
            "github_audit.api.service.project_agent_chat",
            return_value=ProjectAgentResult(
                reply="ok",
                project_id=None,
                fields=[],
                pending_writes=[],
                new_messages=[],
            ),
        ),
    ):
        client_cls.return_value.__enter__.return_value = MagicMock()
        result = service._reply_with_finding(_settings(), snapshot, state, "s1", "explain", "2")
    assert "discarded" in result.answer
    assert state.pending_writes == []
    assert state.pending_content_id is None


# ── conversation store / session isolation ──────────────────────────────────


def test_conversation_store_returns_fresh_state_for_new_session() -> None:
    service = ProjectGuardChatService()
    session_id, state = service._sessions.get(None)
    assert session_id
    assert state.pending_writes == []


def test_conversation_store_reuses_state_for_same_conversation_id() -> None:
    service = ProjectGuardChatService()
    _, state = service._sessions.get("abc")
    state.pending_writes.append(_comment())
    _, state_again = service._sessions.get("abc")
    assert len(state_again.pending_writes) == 1


@pytest.mark.parametrize("message,context", [("apply it", None), ("prepare", "2")])
def test_inflight_apply_rejects_same_conversation_turn(message: str, context: str | None) -> None:
    service = ProjectGuardChatService()
    settings = _settings()
    _, state = service._sessions.get("same", scope_key=_settings_scope(settings))
    state.pending_writes = [_comment()]
    state.pending_content_id = "I_1"
    started, release = Event(), Event()
    writes: list[str] = []

    def write(
        client: GitHubClient,
        pending: PendingWrite,
        *,
        project_id: str | None,
        fields: list[ProjectFieldDefinition] | None,
        created_item_ids: dict[str, str],
    ) -> None:
        writes.append("comment")
        if len(writes) == 1:
            started.set()
            assert release.wait(5)

    with (
        patch("github_audit.api.service.load_settings", return_value=settings),
        patch("github_audit.api.service.GitHubClient"),
        patch("github_audit.api.service.apply_pending_write", side_effect=write),
        patch.object(service, "_scan_snapshot", return_value=_snapshot(_finding(2, "I_2"))),
        patch.object(service, "_reply_with_finding") as plan,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        first = pool.submit(service.reply, "apply it", "same")
        try:
            assert started.wait(5)
            with pytest.raises(ChatServiceError) as error:
                service.reply(message, "same", context)
            assert error.value.status_code == 409
            assert state.pending_writes == [_comment()]
            plan.assert_not_called()
            assert service.reply("apply it", "independent").answer == "No pending write."
        finally:
            release.set()
        assert first.result(timeout=5).answer == "Applied 1 write(s)."
        assert service.reply("apply it", "same").answer == "No pending write."
    assert writes == ["comment"]


def test_failed_apply_releases_turn_and_retries_only_remaining_writes() -> None:
    service = ProjectGuardChatService()
    settings = _settings()
    _, state = service._sessions.get("retry", scope_key=_settings_scope(settings))
    first, second = _comment("first"), _comment("second")
    state.pending_writes = [first, second]
    with (
        patch("github_audit.api.service.load_settings", return_value=settings),
        patch("github_audit.api.service.GitHubClient"),
        patch(
            "github_audit.api.service.apply_pending_write",
            side_effect=[None, GitHubError("failed"), None],
        ) as write,
    ):
        assert "1 write(s) remain queued" in service.reply("apply it", "retry").answer
        assert service.reply("apply it", "retry").answer == "Applied 1 write(s)."
    assert [call.args[1] for call in write.call_args_list] == [first, second, second]
    assert state.pending_writes == []


def test_active_session_survives_expiry_and_capacity_eviction() -> None:
    store = ConversationStore(max_sessions=2, ttl=timedelta(seconds=1))
    with store.turn("active") as (_, active):
        active.pending_writes = [_comment()]
        active.touched_at = datetime.now(UTC) - timedelta(seconds=2)
        store.get("idle")
        store.get("replacement")
        assert len(store._items) == 2
        with pytest.raises(ChatServiceError) as error:
            store.get("active")
        assert error.value.status_code == 409
    assert store.get("active")[1] is active
    active.touched_at = datetime.now(UTC) - timedelta(seconds=2)
    assert store.get("active")[1].pending_writes == []


def test_full_active_store_rejects_admission_without_growing_or_replacing_owners() -> None:
    store = ConversationStore(max_sessions=2)
    with store.turn("first") as (_, first):
        with store.turn("second"):
            with pytest.raises(ChatServiceError) as error:
                store.get("third")
            assert error.value.status_code == 503
            assert len(store._items) == 2
        store.get("third")
        assert store._items["first"] is first
        assert len(store._items) == 2


@pytest.mark.parametrize(
    "ending", ["complete", "close", "cancel", "error", "unopened", "setup_error"]
)
def test_stream_turn_ownership_and_cleanup(ending: str) -> None:
    service = ProjectGuardChatService()
    settings = _settings()
    _, state = service._sessions.get("stream", scope_key=_settings_scope(settings))
    closed = Event()
    messages = [ModelResponse(parts=[TextPart(content="hello!")])]

    async def provider_tokens() -> AsyncGenerator[str, None]:
        try:
            yield "hello"
            if ending == "error":
                raise GitHubError("stream failed")
            if ending == "cancel":
                await asyncio.Event().wait()
            yield "!"
        finally:
            closed.set()

    async def scenario() -> None:
        bundle = service.stream("hello", "stream")
        assert bundle is not None
        tokens, finalise = bundle
        if ending == "unopened":
            await tokens.aclose()
        elif ending == "setup_error":
            with pytest.raises(ValueError, match="setup failed"):
                await anext(tokens)
        else:
            assert await anext(tokens) == "hello"
            with pytest.raises(ChatServiceError) as error:
                service.reply("apply it", "stream")
            assert error.value.status_code == 409
            with pytest.raises(ChatServiceError):
                service.stream("another", "stream")
            if ending == "complete":
                assert [chunk async for chunk in tokens] == ["!"]
                assert finalise().answer == "hello!"
                assert finalise().answer == "hello!"
            elif ending == "close":
                await tokens.aclose()
            elif ending == "cancel":
                task = asyncio.create_task(anext(tokens))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(GitHubError, match="stream failed"):
                    await anext(tokens)
            assert closed.is_set()
        assert service.reply("apply it", "stream").answer == "No pending write."

    with (
        patch("github_audit.api.service.load_settings", return_value=settings),
        patch("github_audit.api.service._llm_ready", return_value=True),
        patch.object(service, "_scan_snapshot", return_value=_snapshot()),
        patch(
            "github_audit.api.service.general_chat_stream",
            side_effect=ValueError("setup failed") if ending == "setup_error" else None,
            return_value=(provider_tokens(), lambda: messages),
        ) as stream,
    ):
        asyncio.run(scenario())
        if ending == "unopened":
            stream.assert_not_called()
    assert state.message_history == (messages if ending == "complete" else [])
