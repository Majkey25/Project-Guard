from __future__ import annotations

from unittest.mock import MagicMock, patch

from github_audit.config import Settings
from github_audit.models import (
    AuditResult,
    DiscoveryResult,
    GitHubIssue,
    GitHubPullRequest,
    ProjectFieldDefinition,
    ProjectFieldValue,
    ProjectItem,
)
from github_audit.scanner import content_from_project_item, scan, scan_all


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "github_token": "token",
        "github_project_number": 1,
        "github_project_numbers_raw": "",
        "github_repository_allowlist_raw": "repo",
        "target_assignees_raw": "alice",
        "required_project_fields_raw": "Estimate",
        "require_development_link": False,
        "require_linked_pr_or_branch": False,
        "llm_provider": "",
        "llm_base_url": "",
        "llm_api_version": "",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _discovery(repos: list[str] | None = None, *, project_number: int = 1) -> DiscoveryResult:
    return DiscoveryResult(
        organization="OKsystem",
        project_id=f"PVT_{project_number}",
        project_number=project_number,
        project_title=f"Project {project_number}",
        project_url=f"https://github.com/orgs/OKsystem/projects/{project_number}",
        repositories=repos or ["OKsystem/repo"],
        fields=[
            ProjectFieldDefinition(id="f-est", name="Estimate", data_type="NUMBER", kind="field")
        ],
        required_fields_missing=[],
        issue_sample_count=0,
        pull_request_sample_count=0,
        project_item_sample_count=0,
        content_types=["issue"],
        development_strategy="closing references",
        development_limitations=[],
    )


def _issue(
    number: int = 1,
    assignees: list[str] | None = None,
    linked_prs: int = 0,
    updated_at: str | None = None,
) -> GitHubIssue:
    return GitHubIssue(
        id=f"I_{number}",
        repository="OKsystem/repo",
        number=number,
        title=f"Issue {number}",
        url=f"https://github.com/OKsystem/repo/issues/{number}",
        state="OPEN",
        body="",
        assignees=assignees or ["alice"],
        updated_at=updated_at,
        linked_pull_requests_count=linked_prs,
    )


def _project_item(
    content_id: str = "I_1",
    *,
    has_estimate: bool = True,
    updated_at: str | None = None,
) -> ProjectItem:
    field_values: dict[str, ProjectFieldValue] = {}
    if has_estimate:
        field_values["Estimate"] = ProjectFieldValue(
            field_id="f-est", field_name="Estimate", value=3
        )
    return ProjectItem(
        id="PVTI_1",
        content_id=content_id,
        content_type="issue",
        repository="OKsystem/repo",
        number=1,
        title="Issue 1",
        url="https://github.com/OKsystem/repo/issues/1",
        updated_at=updated_at,
        field_values=field_values,
    )


# ── scan ──────────────────────────────────────────────────────────────────────


def test_scan_compliant_issue_produces_no_finding() -> None:
    issue = _issue(assignees=["alice"])
    item = _project_item(has_estimate=True)
    client = MagicMock()
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[item]),
        patch("github_audit.scanner.search_items", return_value=[issue]),
    ):
        result = scan(client, _settings(), _discovery(), searched_items=None)

    assert result.findings == []
    assert result.scanned_issue_count == 1


def test_scan_filters_by_updated_range() -> None:
    old_issue = _issue(1, updated_at="2026-05-01T00:00:00Z")
    new_issue = _issue(2, updated_at="2026-06-15T00:00:00Z")
    client = MagicMock()
    with patch("github_audit.scanner.fetch_project_items", return_value=[]):
        result = scan(
            client,
            _settings(
                github_updated_from="2026-06-01",
                github_updated_to="2026-06-30",
                required_project_fields_raw="",
                require_assignee=False,
                require_target_assignee=False,
                require_project_item=False,
            ),
            _discovery(),
            searched_items=[old_issue, new_issue],
        )

    assert result.scanned_issue_count == 1


def test_scan_missing_estimate_produces_finding() -> None:
    issue = _issue(assignees=["alice"])
    item = _project_item(has_estimate=False)
    client = MagicMock()
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[item]),
        patch("github_audit.scanner.search_items", return_value=[issue]),
    ):
        result = scan(client, _settings(), _discovery())

    assert len(result.findings) == 1
    assert "Estimate" in result.findings[0].missing_fields


def test_scan_issue_not_in_project_produces_finding() -> None:
    issue = _issue()
    client = MagicMock()
    settings = _settings(require_project_item=True)
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[]),
        patch("github_audit.scanner.search_items", return_value=[issue]),
    ):
        result = scan(client, settings, _discovery())

    assert any("Project item" in f.missing_fields for f in result.findings)


def test_scan_out_of_repo_item_has_no_project_item_context() -> None:
    # The scanner does not filter searched_items by repo — it evaluates all of them.
    # An item from outside discovery.repositories will simply have no project_item
    # (project_by_content_id only indexes items whose repository is in the allowed set).
    # With require_project_item=False the item produces no finding.
    issue = GitHubIssue(
        id="I_99",
        repository="OKsystem/other-repo",
        number=99,
        title="Other",
        url="u",
        state="OPEN",
        body="",
        assignees=["alice"],
    )
    client = MagicMock()
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[]),
        patch("github_audit.scanner.search_items", return_value=[issue]),
    ):
        result = scan(
            client,
            _settings(require_project_item=False),
            _discovery(["OKsystem/repo"]),
        )

    assert result.findings == []


def test_content_from_project_item_preserves_updated_at() -> None:
    content = content_from_project_item(
        _project_item(updated_at="2026-06-15T00:00:00Z"),
        {"OKsystem/repo"},
    )

    assert content is not None
    assert content.updated_at == "2026-06-15T00:00:00Z"


def test_scan_counts_prs_separately() -> None:
    pr = GitHubPullRequest(
        id="PR_1",
        repository="OKsystem/repo",
        number=10,
        title="PR",
        url="u",
        state="OPEN",
        body="",
        assignees=["alice"],
        closing_issues_count=1,
    )
    client = MagicMock()
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[]),
        patch("github_audit.scanner.search_items", return_value=[pr]),
    ):
        result = scan(client, _settings(), _discovery())

    assert result.scanned_pull_request_count == 1


def test_scan_uses_provided_searched_items() -> None:
    issue = _issue()
    client = MagicMock()
    with patch("github_audit.scanner.fetch_project_items", return_value=[]):
        # Pass pre-searched items; search_items should NOT be called
        result = scan(client, _settings(), _discovery(), searched_items=[issue])

    assert result.scanned_issue_count == 1


def test_scan_result_inherits_limitations() -> None:
    discovery = _discovery()
    discovery.development_limitations.append("branch links not probed")
    client = MagicMock()
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[]),
        patch("github_audit.scanner.search_items", return_value=[]),
    ):
        result = scan(client, _settings(), discovery)

    assert "branch links not probed" in result.limitations


# ── scan_all ──────────────────────────────────────────────────────────────────


def test_scan_all_returns_empty_for_no_discoveries() -> None:
    result = scan_all(MagicMock(), _settings(), [])
    assert result == []


def test_scan_all_runs_once_per_discovery() -> None:
    discoveries = [_discovery(), _discovery()]
    issue = _issue()
    client = MagicMock()
    with (
        patch("github_audit.scanner.fetch_project_items", return_value=[]),
        patch("github_audit.scanner.search_items", return_value=[issue]),
    ):
        results = scan_all(client, _settings(), discoveries)

    assert len(results) == 2
    assert all(isinstance(r, AuditResult) for r in results)


def test_scan_all_skips_project_item_noise_when_item_exists_on_another_project() -> None:
    issue = _issue()
    item = _project_item(has_estimate=True)
    client = MagicMock()
    discoveries = [_discovery(project_number=1), _discovery(project_number=2)]
    with patch("github_audit.scanner.fetch_project_items", side_effect=[[], [item]]):
        results = scan_all(
            client,
            _settings(require_project_item=True),
            discoveries,
            searched_items=[issue],
        )

    assert [finding for result in results for finding in result.findings] == []


def test_scan_all_reports_real_project_field_missing_on_matching_project() -> None:
    issue = _issue()
    item = _project_item(has_estimate=False)
    client = MagicMock()
    discoveries = [_discovery(project_number=1), _discovery(project_number=2)]
    with patch("github_audit.scanner.fetch_project_items", side_effect=[[], [item]]):
        results = scan_all(
            client,
            _settings(require_project_item=True),
            discoveries,
            searched_items=[issue],
        )

    findings = [finding for result in results for finding in result.findings]
    assert len(findings) == 1
    assert findings[0].project_number == 2
    assert findings[0].missing_fields == ["Estimate"]


def test_scan_all_keeps_missing_project_item_when_item_is_on_no_project() -> None:
    issue = _issue()
    client = MagicMock()
    discoveries = [_discovery(project_number=1), _discovery(project_number=2)]
    with patch("github_audit.scanner.fetch_project_items", side_effect=[[], []]):
        results = scan_all(
            client,
            _settings(
                require_assignee=False,
                require_target_assignee=False,
                require_project_item=True,
                required_project_fields_raw="",
            ),
            discoveries,
            searched_items=[issue],
        )

    findings = [finding for result in results for finding in result.findings]
    assert len(findings) == 2
    assert all(finding.missing_fields == ["Project item"] for finding in findings)
