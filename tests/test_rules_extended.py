from __future__ import annotations

from github_audit.config import Settings
from github_audit.models import GitHubIssue, GitHubPullRequest, ProjectItem
from github_audit.rules import development_status, evaluate_item, has_development_link


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "github_token": "token",
        "github_project_number": 1,
        "github_repository_allowlist_raw": "repo",
        "target_assignees_raw": "alice",
        "required_project_fields_raw": "",
        "require_development_link": False,
        "require_linked_pr_or_branch": False,
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _issue(*, linked_prs: int = 0, assignees: list[str] | None = None) -> GitHubIssue:
    return GitHubIssue(
        id="issue-1",
        repository="org/repo",
        number=1,
        title="Title",
        url="https://github.com/org/repo/issues/1",
        state="OPEN",
        body="",
        assignees=assignees or ["alice"],
        linked_pull_requests_count=linked_prs,
    )


def _pr(*, closing_issues: int = 0, assignees: list[str] | None = None) -> GitHubPullRequest:
    return GitHubPullRequest(
        id="pr-1",
        repository="org/repo",
        number=2,
        title="PR title",
        url="https://github.com/org/repo/pull/2",
        state="OPEN",
        body="",
        assignees=assignees or ["alice"],
        closing_issues_count=closing_issues,
    )


def _item(
    *,
    linked_prs: int = 0,
    closing_issues: int = 0,
    content_type: str = "issue",
) -> ProjectItem:
    return ProjectItem(
        id="item-1",
        content_id="issue-1",
        content_type=content_type,  # type: ignore[arg-type]
        repository="org/repo",
        number=1,
        title="Title",
        url="https://github.com/org/repo/issues/1",
        linked_pull_requests_count=linked_prs,
        closing_issues_count=closing_issues,
    )


# ── has_development_link ─────────────────────────────────────────────────────


def test_issue_has_dev_link_via_content() -> None:
    assert has_development_link(_issue(linked_prs=1), None) is True


def test_issue_has_dev_link_via_project_item() -> None:
    assert has_development_link(_issue(), _item(linked_prs=1)) is True


def test_issue_no_dev_link() -> None:
    assert has_development_link(_issue(), _item(linked_prs=0)) is False


def test_pr_has_dev_link_via_content() -> None:
    assert has_development_link(_pr(closing_issues=1), None) is True


def test_pr_has_dev_link_via_project_item() -> None:
    assert has_development_link(_pr(), _item(closing_issues=1, content_type="pull_request")) is True


def test_pr_no_dev_link() -> None:
    assert has_development_link(_pr(), None) is False


# ── development_status ───────────────────────────────────────────────────────


def test_development_status_issue_count() -> None:
    status = development_status(_issue(linked_prs=2), None)
    assert status == "linked_pull_requests=2"


def test_development_status_issue_prefers_max() -> None:
    status = development_status(_issue(linked_prs=1), _item(linked_prs=3))
    assert status == "linked_pull_requests=3"


def test_development_status_pr_count() -> None:
    status = development_status(_pr(closing_issues=1), None)
    assert status == "closing_issues=1"


def test_development_status_pr_prefers_max() -> None:
    status = development_status(
        _pr(closing_issues=0), _item(closing_issues=2, content_type="pull_request")
    )
    assert status == "closing_issues=2"


# ── evaluate_item for pull requests ─────────────────────────────────────────


def test_evaluate_pr_no_closing_issues_flagged() -> None:
    settings = _settings(require_development_link=True)
    finding = evaluate_item(_pr(), None, settings)
    assert finding is not None
    assert finding.item_type == "pull_request"
    assert "Development link" in finding.missing_fields


def test_evaluate_pr_with_closing_issues_passes_dev_link() -> None:
    settings = _settings(require_development_link=True)
    finding = evaluate_item(_pr(closing_issues=1), None, settings)
    # Should still be a finding because there's no project item, but dev link is present
    if finding is not None:
        assert "Development link" not in finding.missing_fields


def test_evaluate_pr_fully_compliant_returns_none() -> None:
    settings = _settings(
        require_assignee=True,
        require_target_assignee=True,
        require_project_item=True,
        require_development_link=True,
    )
    pr = _pr(closing_issues=1)
    item = _item(content_type="pull_request", closing_issues=1)
    # Must have all required project fields set (none required in this settings)
    finding = evaluate_item(pr, item, settings)
    assert finding is None


def test_evaluate_item_no_missing_fields_returns_none() -> None:
    settings = _settings(
        require_assignee=True,
        require_target_assignee=True,
        require_project_item=False,
        require_development_link=False,
        require_linked_pr_or_branch=False,
    )
    issue = _issue()
    finding = evaluate_item(issue, None, settings)
    assert finding is None


def test_evaluate_item_missing_target_assignee() -> None:
    settings = _settings(
        require_target_assignee=True,
        target_assignees_raw="bob",
    )
    finding = evaluate_item(_issue(assignees=["charlie"]), None, settings)
    assert finding is not None
    assert "target assignee" in finding.missing_fields
