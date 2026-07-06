"""Regression tests for the 2026-07 whole-project audit fixes.

Each test pins one previously confirmed bug; see the test names for the defect.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from github_audit.agent_chat import parse_agent_command
from github_audit.applier import resolve_created_item_ids
from github_audit.cli import merge_audits
from github_audit.config import Settings, load_settings
from github_audit.discovery import discover_project, discover_repositories
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.llm_evaluator import trim_message_history
from github_audit.models import (
    ApplyChange,
    ApplyPlan,
    AuditFinding,
    AuditResult,
    DiscoveryResult,
    ProjectFieldDefinition,
)
from github_audit.project_fields import fetch_repositories
from github_audit.report import write_csv
from github_audit.scanner import scan


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "github_token": "tok",
        "github_project_numbers_raw": "1",
        "github_include_all_repositories": True,
        "target_assignees_raw": "alice",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _finding(number: int, *, project_item_id: str | None, missing: list[str]) -> AuditFinding:
    return AuditFinding(
        repository="OKsystem/repo",
        item_type="issue",
        number=number,
        title=f"t{number}",
        url=f"https://github.com/OKsystem/repo/issues/{number}",
        assignees=["alice"],
        missing_fields=missing,
        development_status="none",
        project_item_id=project_item_id,
    )


def _audit(findings: list[AuditFinding], **counts: int) -> AuditResult:
    return AuditResult(
        organization="OKsystem",
        repositories=["OKsystem/repo"],
        findings=findings,
        scanned_issue_count=counts.get("issues", 0),
        scanned_pull_request_count=counts.get("prs", 0),
    )


# ── github_client: transport/JSON failures must surface as GitHubError ────────


def _ok_response(body: object) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = body
    return response


def test_graphql_wraps_transport_error_as_github_error() -> None:
    instance = MagicMock()
    instance.post.side_effect = httpx.ConnectError("connection refused")
    with (
        patch("github_audit.github_client.httpx.Client", return_value=instance),
        patch("time.sleep"),
        pytest.raises(GitHubError, match="GitHub request failed"),
    ):
        GitHubClient("tok").graphql("query {}")


def test_graphql_retries_transport_error_then_succeeds() -> None:
    instance = MagicMock()
    instance.post.side_effect = [
        httpx.ReadTimeout("timed out"),
        _ok_response({"data": {"ok": True}}),
    ]
    with (
        patch("github_audit.github_client.httpx.Client", return_value=instance),
        patch("time.sleep"),
    ):
        assert GitHubClient("tok").graphql("query {}") == {"ok": True}
    assert instance.post.call_count == 2


def test_graphql_invalid_json_raises_github_error() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.side_effect = ValueError("not json")
    instance = MagicMock()
    instance.post.return_value = response
    with (
        patch("github_audit.github_client.httpx.Client", return_value=instance),
        pytest.raises(GitHubError, match="invalid JSON"),
    ):
        GitHubClient("tok").graphql("query {}")


# ── discovery/project_fields: org/name repo list entries ──────────────────────


def test_denylist_accepts_org_slash_name_form() -> None:
    settings = _settings(github_repository_denylist_raw="OKsystem/secret-repo,plain-repo")
    with patch(
        "github_audit.discovery.fetch_repositories",
        return_value=["OKsystem/secret-repo", "OKsystem/plain-repo", "OKsystem/kept"],
    ):
        repos = discover_repositories(MagicMock(), settings)
    assert repos == ["OKsystem/kept"]


def test_fetch_repositories_allowlist_accepts_org_slash_name_form() -> None:
    client = MagicMock()
    client.graphql.return_value = {
        "organization": {"repository": {"isArchived": False, "nameWithOwner": "OKsystem/repo"}}
    }
    result = fetch_repositories(client, "OKsystem", ["OKsystem/repo"], False)
    assert result == ["OKsystem/repo"]
    assert client.graphql.call_args[0][1]["name"] == "repo"


def test_discover_project_matches_required_fields_casefolded() -> None:
    field = ProjectFieldDefinition(
        id="F1", name="priority", data_type="SINGLE_SELECT", kind="field"
    )
    with (
        patch(
            "github_audit.discovery.fetch_project_fields",
            return_value=(
                {"id": "P1", "number": 1, "title": "T", "url": "https://example.test"},
                [field],
            ),
        ),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        result = discover_project(
            MagicMock(),
            _settings(required_project_fields_raw="Priority"),
            1,
            [],
            0,
            0,
            True,
            "",
        )
    assert result.required_fields_missing == []


# ── scanner: single-project scan fallback must honor all include flags ────────


def test_scan_fallback_search_passes_all_include_flags() -> None:
    discovery = DiscoveryResult(
        organization="OKsystem",
        project_id="P1",
        project_number=1,
        project_title="T",
        project_url="https://example.test",
        repositories=["OKsystem/repo"],
        fields=[],
        required_fields_missing=[],
        issue_sample_count=0,
        pull_request_sample_count=0,
        project_item_sample_count=0,
        content_types=[],
        development_strategy="",
        development_limitations=[],
    )
    settings = _settings(include_closed_pull_requests=True, include_unassigned=True)
    with patch("github_audit.scanner.search_items", return_value=[]) as search:
        scan(MagicMock(), settings, discovery, None, project_items=[])
    assert search.call_args.kwargs["include_closed_pull_requests"] is True
    assert search.call_args.kwargs["include_unassigned"] is True


# ── cli.merge_audits: empty input and cross-project duplicates ────────────────


def test_merge_audits_empty_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no projects discovered"):
        merge_audits([])


def test_merge_audits_dedupes_cross_project_findings() -> None:
    on_board = _finding(1, project_item_id="ITEM1", missing=["Priority", "Estimate"])
    off_board = _finding(1, project_item_id=None, missing=["Development link"])
    merged = merge_audits([_audit([off_board]), _audit([on_board])])
    assert len(merged.findings) == 1
    # the board version wins even though it has more missing fields
    assert merged.findings[0].project_item_id == "ITEM1"


# ── config: REQUIRED_PROJECT_FIELDS sentinel and MY_WORK_MODE restore ─────────


def test_required_project_fields_none_sentinel_disables_checks() -> None:
    assert _settings(required_project_fields_raw="none").required_project_fields == []
    assert _settings(required_project_fields_raw="None").required_project_fields == []


def test_load_settings_restores_preexisting_my_work_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "GITHUB_TOKEN=tok\nGITHUB_INCLUDE_ALL_REPOSITORIES=true\nTARGET_ASSIGNEES=alice\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_WORK_MODE", "true")
    load_settings(my_work_mode=True)
    assert os.environ.get("MY_WORK_MODE") == "true"


# ── agent_chat: command parsing must not fire on longer words ─────────────────


def test_closed_projects_does_not_enable_closed_prs() -> None:
    command = parse_agent_command("include closed projects in the scan")
    assert command.control_updates == ()


def test_only_project_fields_does_not_disable_issues() -> None:
    command = parse_agent_command("show only project fields")
    assert command.control_updates == ()


def test_closed_prs_still_recognized() -> None:
    names = {u.name for u in parse_agent_command("include closed prs").control_updates}
    assert "include_closed_pull_requests" in names


def test_only_pull_requests_still_recognized() -> None:
    names = {u.name for u in parse_agent_command("show only pull requests").control_updates}
    assert "include_pull_requests" in names
    assert "include_issues" in names


# ── applier: retry queue must stay resolvable after a consumed board add ──────


def test_resolve_created_item_ids_fills_empty_ids() -> None:
    change = ApplyChange(
        repository="OKsystem/repo",
        item_type="issue",
        number=1,
        project_item_id="",
        field_name="Estimate",
        value=5,
        content_id="C1",
    )
    untouched = ApplyChange(
        repository="OKsystem/repo",
        item_type="issue",
        number=2,
        project_item_id="ITEM2",
        field_name="Estimate",
        value=3,
        content_id="C2",
    )
    resolve_created_item_ids([ApplyPlan(changes=[change, untouched])], {"C1": "ITEM1"})
    assert change.project_item_id == "ITEM1"
    assert untouched.project_item_id == "ITEM2"


# ── llm_evaluator: history trimming must cut at run boundaries ────────────────


def _tool_run() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content="question")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="r", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="answer")]),
    ]


def test_trim_message_history_keeps_short_history() -> None:
    history = _tool_run()
    assert trim_message_history(history, 20) == history


def test_trim_message_history_never_starts_with_orphaned_tool_return() -> None:
    history = _tool_run() + _tool_run() + _tool_run()
    trimmed = trim_message_history(history, 6)
    # a naive [-6:] slice would start with run 2's orphaned tool return
    assert len(trimmed) == 4
    first = trimmed[0]
    assert isinstance(first, ModelRequest)
    assert any(isinstance(part, UserPromptPart) for part in first.parts)


def test_trim_message_history_without_boundary_returns_empty() -> None:
    orphan: list[ModelMessage] = [
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="r", tool_call_id="c")]),
        ModelResponse(parts=[TextPart(content="a")]),
    ]
    assert trim_message_history(orphan * 3, 2) == []


# ── report: CSV must neutralize formula injection ─────────────────────────────


def test_write_csv_escapes_formula_titles(tmp_path: Path) -> None:
    audit = _audit([_finding(1, project_item_id=None, missing=["Estimate"])])
    audit.findings[0].title = "-1+1"
    out = tmp_path / "findings.csv"
    write_csv(out, audit)
    text = out.read_text(encoding="utf-8-sig")
    assert "'-1+1" in text
