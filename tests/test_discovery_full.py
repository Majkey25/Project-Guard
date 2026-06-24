from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from github_audit.config import Settings
from github_audit.discovery import discover_all, discover_repositories
from github_audit.models import ProjectFieldDefinition


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo-a",
            "target_assignees_raw": "alice",
            "llm_provider": "",
            "llm_base_url": "",
            "llm_api_version": "",
        }
    )


def _field(name: str) -> ProjectFieldDefinition:
    return ProjectFieldDefinition(id=f"f-{name.lower()}", name=name, data_type="TEXT", kind="field")


def _mock_project(number: int = 1) -> dict[str, Any]:
    return {
        "id": "PVT_1",
        "number": number,
        "title": "Test Project",
        "url": f"https://github.com/orgs/org/projects/{number}",
    }


# ── discover_repositories ────────────────────────────────────────────────────


def test_discover_repositories_applies_denylist() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo-a,repo-b",
            "github_repository_denylist_raw": "repo-b",
            "target_assignees_raw": "alice",
            "llm_provider": "",
            "llm_base_url": "",
            "llm_api_version": "",
        }
    )
    with patch("github_audit.discovery.fetch_repositories", return_value=["org/repo-a", "org/repo-b"]):
        client = MagicMock()
        repos = discover_repositories(client, settings)
    assert repos == ["org/repo-a"]


def test_discover_repositories_include_all() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_include_all_repositories": True,
            "target_assignees_raw": "alice",
            "llm_provider": "",
            "llm_base_url": "",
            "llm_api_version": "",
            "require_target_assignee": False,
        }
    )
    with patch("github_audit.discovery.fetch_repositories", return_value=["org/repo-x"]):
        client = MagicMock()
        repos = discover_repositories(client, settings)
    assert repos == ["org/repo-x"]


# ── discover_all ──────────────────────────────────────────────────────────────


def test_discover_all_returns_one_result_per_project() -> None:
    fields = [_field("Estimate"), _field("Priority")]
    with (
        patch("github_audit.discovery.fetch_repositories", return_value=["org/repo-a"]),
        patch("github_audit.discovery.search_items", return_value=[]),
        patch("github_audit.discovery.probe_branch_links", return_value=(False, "no probe")),
        patch("github_audit.discovery.fetch_project_fields", return_value=(_mock_project(), fields)),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        client = MagicMock()
        results = discover_all(client, _settings())

    assert len(results) == 1
    result = results[0]
    assert result.project_number == 1
    assert result.organization == "OKsystem"
    assert result.repositories == ["org/repo-a"]
    assert len(result.fields) == 2


def test_discover_all_reports_missing_required_fields() -> None:
    fields = [_field("Estimate")]  # Priority missing from project
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "required_project_fields_raw": "Estimate,Priority",
            "llm_provider": "",
            "llm_base_url": "",
            "llm_api_version": "",
        }
    )
    with (
        patch("github_audit.discovery.fetch_repositories", return_value=["org/repo"]),
        patch("github_audit.discovery.search_items", return_value=[]),
        patch("github_audit.discovery.probe_branch_links", return_value=(False, "no probe")),
        patch("github_audit.discovery.fetch_project_fields", return_value=(_mock_project(), fields)),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        results = discover_all(MagicMock(), settings)

    assert "Priority" in results[0].required_fields_missing


def test_discover_all_multiple_projects() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_numbers_raw": "10,20",
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "llm_provider": "",
            "llm_base_url": "",
            "llm_api_version": "",
        }
    )
    with (
        patch("github_audit.discovery.fetch_repositories", return_value=["org/repo"]),
        patch("github_audit.discovery.search_items", return_value=[]),
        patch("github_audit.discovery.probe_branch_links", return_value=(True, "ok")),
        patch(
            "github_audit.discovery.fetch_project_fields",
            side_effect=[
                (_mock_project(10), []),
                (_mock_project(20), []),
            ],
        ),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        results = discover_all(MagicMock(), settings)

    assert len(results) == 2
    assert results[0].project_number == 10
    assert results[1].project_number == 20


def test_discover_all_branch_link_limitation_included() -> None:
    with (
        patch("github_audit.discovery.fetch_repositories", return_value=["org/repo"]),
        patch("github_audit.discovery.search_items", return_value=[]),
        patch("github_audit.discovery.probe_branch_links", return_value=(False, "probe failed: 403")),
        patch("github_audit.discovery.fetch_project_fields", return_value=(_mock_project(), [])),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        results = discover_all(MagicMock(), _settings())

    limitations = results[0].development_limitations
    assert any("probe failed" in lim for lim in limitations)


def test_discover_all_counts_sample_issues_and_prs() -> None:
    from github_audit.models import GitHubIssue, GitHubPullRequest

    issue = GitHubIssue(
        id="I_1", repository="org/repo", number=1, title="T",
        url="u", state="OPEN", body="", assignees=["alice"],
    )
    pr = GitHubPullRequest(
        id="PR_1", repository="org/repo", number=2, title="P",
        url="u", state="OPEN", body="",
    )
    with (
        patch("github_audit.discovery.fetch_repositories", return_value=["org/repo"]),
        patch("github_audit.discovery.search_items", return_value=[issue, pr]),
        patch("github_audit.discovery.probe_branch_links", return_value=(False, "no")),
        patch("github_audit.discovery.fetch_project_fields", return_value=(_mock_project(), [])),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        results = discover_all(MagicMock(), _settings())

    assert results[0].issue_sample_count == 1
    assert results[0].pull_request_sample_count == 1
