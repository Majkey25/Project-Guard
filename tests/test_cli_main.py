from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from github_audit.cli import main
from github_audit.models import (
    AuditFinding,
    AuditResult,
    BrowserScanResult,
    DiscoveryResult,
    ProjectFieldDefinition,
)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.github_token = "tok"
    s.github_org = "OKsystem"
    s.github_project_numbers = [1]
    s.target_assignees = ["alice"]
    s.required_project_fields = ["Estimate"]
    s.include_issues = True
    s.include_pull_requests = True
    s.include_closed_issues = False
    s.auto_apply = False
    s.auto_apply_min_confidence = 0.85
    s.llm_enabled = True
    s.llm_model_name = "gpt-4"
    s.llm_api_key = "key"
    return s


def _discovery() -> DiscoveryResult:
    return DiscoveryResult(
        organization="OKsystem",
        project_id="PVT_1",
        project_number=1,
        project_title="Project",
        project_url="https://github.com/orgs/OKsystem/projects/1",
        repositories=["OKsystem/repo"],
        fields=[ProjectFieldDefinition(id="f1", name="Estimate", data_type="NUMBER", kind="field")],
        required_fields_missing=[],
        issue_sample_count=0,
        pull_request_sample_count=0,
        project_item_sample_count=0,
        content_types=["issue"],
        development_strategy="closing references",
        development_limitations=[],
    )


def _audit(findings: int = 0) -> AuditResult:
    fs = [
        AuditFinding(
            repository="OKsystem/repo",
            item_type="issue",
            number=i + 1,
            title=f"Issue {i + 1}",
            url=f"https://github.com/OKsystem/repo/issues/{i + 1}",
            assignees=["alice"],
            missing_fields=["Estimate"],
            development_status="linked_pull_requests=0",
        )
        for i in range(findings)
    ]
    return AuditResult(
        organization="OKsystem",
        project_number=1,
        project_title="Project",
        repositories=["OKsystem/repo"],
        findings=fs,
        scanned_issue_count=findings,
        scanned_pull_request_count=0,
    )


# ── discover ──────────────────────────────────────────────────────────────────


def test_cli_discover_exits_zero() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
    ):
        code = main(["discover"])
    assert code == 0


def test_cli_discover_writes_markdown(tmp_path: Path) -> None:
    md = tmp_path / "discovery.md"
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
    ):
        main(["discover", "--markdown", str(md)])
    assert md.exists()
    assert "GitHub Audit Discovery" in md.read_text(encoding="utf-8")


def test_cli_discover_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "discovery.json"
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
    ):
        main(["discover", "--json", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, (dict, list))


# ── scan ──────────────────────────────────────────────────────────────────────


def test_cli_scan_exits_zero() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(1)]),
    ):
        code = main(["scan"])
    assert code == 0


def test_cli_scan_json_flag() -> None:
    from io import StringIO

    buf = StringIO()
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(0)]),
        patch("sys.stdout", buf),
    ):
        main(["scan", "--json"])
    output = buf.getvalue()
    parsed = json.loads(output)
    assert "findings" in parsed


def test_cli_scan_writes_markdown(tmp_path: Path) -> None:
    md = tmp_path / "report.md"
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(1)]),
    ):
        main(["scan", "--markdown", str(md)])
    assert md.exists()


def test_cli_scan_writes_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "report.csv"
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(2)]),
    ):
        main(["scan", "--csv", str(csv_path)])
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8")
    assert "repository" in content


# ── suggest ───────────────────────────────────────────────────────────────────


def test_cli_suggest_exits_zero() -> None:
    from github_audit.models import LLMSuggestion

    suggestion = LLMSuggestion(confidence=0.9, rationale="ok", should_auto_apply=False)
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(1)]),
        patch("github_audit.cli.suggest_for_finding", return_value=suggestion),
    ):
        code = main(["suggest"])
    assert code == 0


def test_cli_suggest_llm_error_returns_two() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(1)]),
        patch("github_audit.cli.suggest_for_finding", side_effect=ValueError("LLM_ENABLED=false")),
    ):
        code = main(["suggest"])
    assert code == 2


# ── apply ─────────────────────────────────────────────────────────────────────


def test_cli_apply_dry_run_exits_zero() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(0)]),
        patch("github_audit.cli.suggest_for_finding", side_effect=ValueError("LLM_ENABLED=false")),
    ):
        code = main(["apply", "--dry-run"])
    assert code == 0


def test_cli_apply_multiple_projects_returns_two() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch(
            "github_audit.cli.discover_all",
            return_value=[_discovery(), _discovery()],
        ),
    ):
        code = main(["apply", "--dry-run"])
    assert code == 2


# ── config error ──────────────────────────────────────────────────────────────


def test_cli_returns_two_on_github_error() -> None:
    from github_audit.github_client import GitHubError

    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", side_effect=GitHubError("token invalid")),
    ):
        code = main(["scan"])
    assert code == 2


# ── browser-scan ──────────────────────────────────────────────────────────────


def test_cli_browser_scan_exits_zero() -> None:
    result = BrowserScanResult(
        url="https://github.com/orgs/OKsystem/projects/1",
        title="Project",
        signed_in=True,
        headers=["Title", "Estimate"],
        findings=[],
        missing_headers=[],
    )
    with patch("github_audit.cli.run_browser_scan", return_value=result):
        code = main(["browser-scan"])
    assert code == 0


def test_cli_browser_scan_json_flag() -> None:
    from io import StringIO

    result = BrowserScanResult(
        url="https://u",
        title="T",
        signed_in=True,
        headers=[],
        findings=[],
        missing_headers=[],
    )
    buf = StringIO()
    with patch("github_audit.cli.run_browser_scan", return_value=result), patch("sys.stdout", buf):
        main(["browser-scan", "--json"])
    parsed = json.loads(buf.getvalue())
    assert "findings" in parsed


# ── logging ───────────────────────────────────────────────────────────────────


def test_cli_verbose_flag_accepted() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings_mock()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_all", return_value=[_discovery()]),
        patch("github_audit.cli.scan_all", return_value=[_audit(0)]),
    ):
        code = main(["--verbose", "scan"])
    assert code == 0
