from __future__ import annotations

from github_audit.config import Settings
from github_audit.models import (
    GitHubIssue,
    GitHubPullRequest,
    ProjectFieldValue,
    ProjectItem,
)
from github_audit.rules import evaluate_item


def test_evaluate_item_detects_missing_project_fields() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "required_project_fields_raw": "Estimate,Priority",
            "require_development_link": False,
            "require_linked_pr_or_branch": False,
        }
    )
    issue = GitHubIssue(
        id="issue-1",
        repository="org/repo",
        number=1,
        title="Issue",
        url="https://github.com/org/repo/issues/1",
        state="OPEN",
        body="Full issue body",
        assignees=["alice"],
    )
    item = ProjectItem(
        id="item-1",
        content_id="issue-1",
        content_type="issue",
        repository="org/repo",
        number=1,
        title="Issue",
        url="https://github.com/org/repo/issues/1",
        assignees=["alice"],
        field_values={"Estimate": ProjectFieldValue(field_id="f1", field_name="Estimate", value=3)},
    )
    finding = evaluate_item(issue, item, settings)
    assert finding is not None
    assert finding.missing_fields == ["Priority"]
    assert finding.content_id == "issue-1"
    assert finding.body == "Full issue body"
    assert finding.comments == []


def test_evaluate_item_detects_unassigned_issue() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "required_project_fields_raw": "",
            "require_development_link": False,
            "require_linked_pr_or_branch": False,
        }
    )
    issue = GitHubIssue(
        id="issue-1",
        repository="org/repo",
        number=1,
        title="Issue",
        url="https://github.com/org/repo/issues/1",
        state="OPEN",
        body="",
        assignees=[],
    )
    finding = evaluate_item(issue, None, settings)
    assert finding is not None
    assert finding.missing_fields == ["assignee", "target assignee", "Project item"]


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "github_token": "token",
        "github_project_number": 1,
        "github_repository_allowlist_raw": "repo",
        "target_assignees_raw": "alice",
        "required_project_fields_raw": "",
        "require_assignee": False,
        "require_target_assignee": False,
        "require_development_link": False,
        "require_linked_pr_or_branch": False,
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _pull_request() -> GitHubPullRequest:
    return GitHubPullRequest(
        id="pr-1",
        repository="org/repo",
        number=2,
        title="PR",
        url="https://github.com/org/repo/pull/2",
        state="OPEN",
        body="",
        assignees=["alice"],
    )


def test_required_fields_match_case_insensitively() -> None:
    settings = _settings(required_project_fields_raw="estimate,ITERATION (SPRINT)")
    issue = GitHubIssue(
        id="issue-1",
        repository="org/repo",
        number=1,
        title="Issue",
        url="https://github.com/org/repo/issues/1",
        state="OPEN",
        body="",
        assignees=["alice"],
    )
    item = ProjectItem(
        id="item-1",
        content_id="issue-1",
        content_type="issue",
        repository="org/repo",
        number=1,
        title="Issue",
        url="https://github.com/org/repo/issues/1",
        field_values={
            "Estimate": ProjectFieldValue(field_id="f1", field_name="Estimate", value=3),
            "Iteration (sprint)": ProjectFieldValue(
                field_id="f2", field_name="Iteration (sprint)", value="Sprint A"
            ),
        },
    )
    assert evaluate_item(issue, item, settings) is None


def test_pull_requests_exempt_from_project_item_by_default() -> None:
    finding = evaluate_item(_pull_request(), None, _settings())
    assert finding is None


def test_pull_requests_require_project_item_when_enabled() -> None:
    finding = evaluate_item(
        _pull_request(), None, _settings(require_project_item_pull_requests=True)
    )
    assert finding is not None
    assert finding.missing_fields == ["Project item"]
