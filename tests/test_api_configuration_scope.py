from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart

from github_audit.api import main
from github_audit.api.service import ProjectGuardChatService, ProjectSnapshot
from github_audit.config import Settings
from github_audit.github_client import GitHubError
from github_audit.llm_evaluator import ProjectAgentResult
from github_audit.models import AuditFinding, IssueCommentPlan


def _save_config(path: Path, **changes: str) -> None:
    values = {
        "GITHUB_ORG": "first",
        "GITHUB_TOKEN": "synthetic-first",
        "GITHUB_PROJECT_NUMBERS": "1",
        "GITHUB_REPOSITORY_ALLOWLIST": "fixture",
        "REQUIRE_TARGET_ASSIGNEE": "false",
        "AUTO_APPLY": "true",
        "LLM_ENABLED": "true",
        "LLM_PROVIDER": "ollama",
        "LLM_MODEL_NAME": "synthetic-model",
    }
    values.update(changes)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def _settings(org: str) -> Settings:
    return Settings.model_validate(
        {
            "github_org": org,
            "github_token": f"synthetic-{org}",
            "github_project_numbers_raw": "1",
            "github_repository_allowlist_raw": "fixture",
            "require_target_assignee": False,
        }
    )


def _snapshot(settings: Settings, now: datetime) -> ProjectSnapshot:
    finding = AuditFinding(
        project_number=1,
        content_id="I_1",
        repository=f"{settings.github_org}/fixture",
        item_type="issue",
        number=1,
        title="Synthetic private issue",
        url="https://example.invalid/issue/1",
        assignees=[],
        missing_fields=["Priority"],
        development_status="none",
    )
    return ProjectSnapshot(
        created_at=now,
        audits=[],
        findings={"selected": finding},
        project_ids={1: "P_1"},
        fields={1: []},
        context=f"Synthetic data for {settings.github_org}",
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_ORG", "second"),
        ("GITHUB_TOKEN", "synthetic-second"),
        ("GITHUB_PROJECT_NUMBERS", "2"),
        ("GITHUB_REPOSITORY_ALLOWLIST", "other"),
        ("LLM_MODEL_NAME", "other-model"),
        ("LLM_PROVIDER", "openai-compatible"),
        ("LLM_BASE_URL", "http://127.0.0.1:11435"),
        ("LLM_API_KEY", "synthetic-key"),
    ],
)
def test_context_route_reloads_saved_scope_but_reuses_unchanged_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    service = ProjectGuardChatService()
    monkeypatch.setattr(main, "service", service)
    _save_config(tmp_path / ".env")
    with (
        patch.object(service, "_run_scan", side_effect=_snapshot) as scan,
        TestClient(main.app) as client,
    ):
        first = client.get("/context")
        repeated = client.get("/context")
        assert first.status_code == repeated.status_code == 200
        assert first.json() == repeated.json()
        assert scan.call_count == 1
        _save_config(tmp_path / ".env", **{key: value})
        changed = client.get("/context")
    assert changed.status_code == 200
    assert scan.call_count == 2
    if key == "GITHUB_ORG":
        assert "second/fixture" in changed.text
        assert "first/fixture" not in changed.text


@pytest.mark.parametrize("old_fails", [False, True])
def test_late_prior_scope_refresh_cannot_replace_new_scope(old_fails: bool) -> None:
    service = ProjectGuardChatService()
    current = [_settings("first")]
    started = Event()
    release = Event()
    scans: list[str] = []

    def scan(settings: Settings, now: datetime) -> ProjectSnapshot:
        scans.append(settings.github_org)
        if settings.github_org == "first":
            started.set()
            assert release.wait(5)
            if old_fails:
                raise GitHubError("old scope failed")
        return _snapshot(settings, now)

    with (
        patch("github_audit.api.service.load_settings", side_effect=lambda: current[0]),
        patch.object(service, "_run_scan", side_effect=scan),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        old = pool.submit(service.context_options)
        try:
            assert started.wait(5)
            current[0] = _settings("second")
            new = service.context_options()
            assert "second/fixture" in new[0]["label"]
        finally:
            release.set()
        if old_fails:
            with pytest.raises(GitHubError, match="old scope failed"):
                old.result(timeout=5)
        else:
            assert "first/fixture" in old.result(timeout=5)[0]["label"]
        assert service.context_options() == new
    assert scans == ["first", "second"]


def test_failed_new_scope_never_returns_previous_scope_and_can_retry() -> None:
    service = ProjectGuardChatService()
    first, second = _settings("first"), _settings("second")
    now = datetime.now(UTC)
    with (
        patch("github_audit.api.service.load_settings", side_effect=[first, second, second]),
        patch.object(
            service,
            "_run_scan",
            side_effect=[_snapshot(first, now), GitHubError("denied"), _snapshot(second, now)],
        ) as scan,
    ):
        service.context_options()
        with pytest.raises(GitHubError, match="denied"):
            service.context_options()
        assert "second/fixture" in service.context_options()[0]["label"]
    assert scan.call_count == 3


def test_concurrent_same_scope_requests_share_one_refresh() -> None:
    service = ProjectGuardChatService()
    settings = _settings("first")
    started, second_admitted, release = Event(), Event(), Event()
    loads = 0

    def load() -> Settings:
        nonlocal loads
        loads += 1
        if loads == 2:
            second_admitted.set()
        return settings

    def scan(settings: Settings, now: datetime) -> ProjectSnapshot:
        started.set()
        assert release.wait(5)
        return _snapshot(settings, now)

    with (
        patch("github_audit.api.service.load_settings", side_effect=load),
        patch.object(service, "_run_scan", side_effect=scan) as fetch,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(service.context_options)
        second = pool.submit(service.context_options)
        try:
            assert started.wait(5)
            assert second_admitted.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    fetch.assert_called_once()


def _queued_write() -> ProjectAgentResult:
    return ProjectAgentResult(
        reply="Prepared",
        project_id="P_1",
        fields=[],
        pending_writes=[
            IssueCommentPlan(
                subject_id="I_1",
                repository="first/fixture",
                item_type="issue",
                number=1,
                body="Synthetic",
            )
        ],
        new_messages=[ModelResponse(parts=[TextPart(content="Synthetic previous history")])],
    )


@pytest.mark.parametrize("mode", ["reply", "stream", "apply"])
def test_stale_conversation_rejected_before_provider_or_write_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    service = ProjectGuardChatService()
    monkeypatch.setattr(main, "service", service)
    _save_config(tmp_path / ".env")
    with (
        patch.object(service, "_run_scan", side_effect=_snapshot),
        patch("github_audit.api.service.GitHubClient") as github,
        patch("github_audit.api.service.fetch_repo_labels", return_value={}),
        patch("github_audit.api.service.fetch_repo_milestones", return_value={}),
        patch("github_audit.api.service.fetch_assignable_users", return_value={}),
        patch("github_audit.api.service.project_agent_chat", return_value=_queued_write()),
        patch("github_audit.api.service.general_chat") as chat,
        patch("github_audit.api.service.general_chat_stream") as stream,
        patch("github_audit.api.service.apply_pending_write") as write,
        TestClient(main.app) as client,
    ):
        queued = client.post(
            "/chat?stream=false", json={"message": "prepare comment", "context": "selected"}
        )
        assert queued.status_code == 200
        conversation = queued.json()["conversationId"]
        github.reset_mock()
        _save_config(tmp_path / ".env", GITHUB_TOKEN="synthetic-second", LLM_MODEL_NAME="other")
        response = client.post(
            f"/chat?stream={'true' if mode == 'stream' else 'false'}",
            json={
                "message": "apply it" if mode == "apply" else "continue",
                "conversationId": conversation,
            },
        )
        assert response.status_code == 400
        assert "Start a new conversation" in response.text
        github.assert_not_called()
        chat.assert_not_called()
        stream.assert_not_called()
        write.assert_not_called()


def test_same_scope_write_invalidates_an_inflight_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ProjectGuardChatService()
    monkeypatch.setattr(main, "service", service)
    _save_config(tmp_path / ".env")
    refresh_started, release = Event(), Event()
    calls = 0
    now = datetime.now(UTC)

    def scan(settings: Settings, started: datetime) -> ProjectSnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            refresh_started.set()
            assert release.wait(5)
        return _snapshot(settings, started)

    with (
        patch.object(service, "_run_scan", side_effect=scan),
        patch("github_audit.api.service.GitHubClient"),
        patch("github_audit.api.service.fetch_repo_labels", return_value={}),
        patch("github_audit.api.service.fetch_repo_milestones", return_value={}),
        patch("github_audit.api.service.fetch_assignable_users", return_value={}),
        patch("github_audit.api.service.project_agent_chat", return_value=_queued_write()),
        patch("github_audit.api.service.apply_pending_write") as write,
        patch("github_audit.api.service.datetime") as clock,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        clock.now.return_value = now
        queued = service.reply("prepare comment", context="selected")
        clock.now.return_value = now + timedelta(seconds=61)
        refresh = pool.submit(service.context_options)
        try:
            assert refresh_started.wait(5)
            assert service.reply("apply it", queued.conversation_id).answer == "Applied 1 write(s)."
        finally:
            release.set()
        refresh.result(timeout=5)
        service.context_options()
    write.assert_called_once()
    assert calls == 3
