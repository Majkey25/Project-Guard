from __future__ import annotations

from github_audit.cli import merge_audits
from github_audit.models import AuditFinding, AuditResult, GitHubComment, ProjectItem
from github_audit.scanner import content_from_project_item


def _item(
    *,
    content_id: str | None = "issue-1",
    content_type: str = "issue",
    repository: str | None = "org/repo",
    number: int | None = 1,
    url: str | None = "https://github.com/org/repo/issues/1",
) -> ProjectItem:
    return ProjectItem(
        id="pv-item-1",
        content_id=content_id,
        content_type=content_type,  # type: ignore[arg-type]
        repository=repository,
        number=number,
        title="Title",
        body="Body",
        url=url,
        comments=[GitHubComment(author="alice", body="Comment")],
        comments_total_count=1,
    )


ALLOWED = {"org/repo"}


# ── content_from_project_item ────────────────────────────────────────────────


def test_content_from_project_item_issue() -> None:
    content = content_from_project_item(_item(), ALLOWED)
    assert content is not None
    from github_audit.models import GitHubIssue

    assert isinstance(content, GitHubIssue)
    assert content.repository == "org/repo"
    assert content.number == 1
    assert content.body == "Body"
    assert content.comments[0].body == "Comment"


def test_content_from_project_item_pull_request() -> None:
    content = content_from_project_item(_item(content_type="pull_request"), ALLOWED)
    assert content is not None
    from github_audit.models import GitHubPullRequest

    assert isinstance(content, GitHubPullRequest)


def test_content_from_project_item_none_for_unknown_type() -> None:
    content = content_from_project_item(_item(content_type="draft_issue"), ALLOWED)
    assert content is None


def test_content_from_project_item_none_when_not_in_allowlist() -> None:
    content = content_from_project_item(_item(), {"org/other-repo"})
    assert content is None


def test_content_from_project_item_none_when_no_content_id() -> None:
    content = content_from_project_item(_item(content_id=None), ALLOWED)
    assert content is None


def test_content_from_project_item_none_when_no_number() -> None:
    content = content_from_project_item(_item(number=None), ALLOWED)
    assert content is None


def test_content_from_project_item_none_when_no_url() -> None:
    content = content_from_project_item(_item(url=None), ALLOWED)
    assert content is None


# ── merge_audits ──────────────────────────────────────────────────────────────


def _finding(repo: str = "org/repo", number: int = 1, project_number: int = 1) -> AuditFinding:
    return AuditFinding(
        repository=repo,
        item_type="issue",
        number=number,
        project_number=project_number,
        title=f"Issue {number}",
        url=f"https://github.com/{repo}/issues/{number}",
        assignees=[],
        missing_fields=["Estimate"],
        development_status="linked_pull_requests=0",
    )


def _audit(
    org: str = "org",
    repos: list[str] | None = None,
    findings: list[AuditFinding] | None = None,
    project_number: int = 1,
) -> AuditResult:
    return AuditResult(
        organization=org,
        project_number=project_number,
        repositories=repos or ["org/repo"],
        findings=findings or [],
        scanned_issue_count=1,
        scanned_pull_request_count=0,
        limitations=["limit-a"],
    )


def test_merge_audits_single_passthrough() -> None:
    audit = _audit()
    result = merge_audits([audit])
    assert result is audit


def test_merge_audits_merges_findings() -> None:
    a = _audit(repos=["org/repo-a"], findings=[_finding("org/repo-a", 1, 1)], project_number=1)
    b = _audit(repos=["org/repo-b"], findings=[_finding("org/repo-b", 2, 2)], project_number=2)
    result = merge_audits([a, b])
    assert len(result.findings) == 2


def test_merge_audits_deduplicates_repos() -> None:
    a = _audit(repos=["org/repo-a", "org/repo-b"])
    b = _audit(repos=["org/repo-b", "org/repo-c"])
    result = merge_audits([a, b])
    assert sorted(result.repositories) == ["org/repo-a", "org/repo-b", "org/repo-c"]


def test_merge_audits_deduplicates_limitations() -> None:
    a = _audit()
    b = _audit()
    result = merge_audits([a, b])
    assert result.limitations == ["limit-a"]


def test_merge_audits_sorts_findings() -> None:
    a = _audit(repos=["org/repo"], findings=[_finding("org/repo", 5, 1)], project_number=1)
    b = _audit(repos=["org/repo"], findings=[_finding("org/repo", 3, 1)], project_number=1)
    result = merge_audits([a, b])
    numbers = [f.number for f in result.findings]
    assert numbers == sorted(numbers)


def test_merge_audits_takes_max_counts() -> None:
    # each per-project scan iterates the same search results, so summing would
    # count every item once per project
    a = AuditResult(
        organization="org",
        repositories=[],
        findings=[],
        scanned_issue_count=3,
        scanned_pull_request_count=1,
    )
    b = AuditResult(
        organization="org",
        repositories=[],
        findings=[],
        scanned_issue_count=5,
        scanned_pull_request_count=2,
    )
    result = merge_audits([a, b])
    assert result.scanned_issue_count == 5
    assert result.scanned_pull_request_count == 2
