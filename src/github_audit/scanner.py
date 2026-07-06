from __future__ import annotations

from datetime import date, datetime

from github_audit.config import Settings
from github_audit.github_client import GitHubClient
from github_audit.models import (
    AuditFinding,
    AuditResult,
    DiscoveryResult,
    GitHubContent,
    GitHubIssue,
    GitHubPullRequest,
    MyWorkItem,
    MyWorkResult,
    ProjectItem,
)
from github_audit.project_fields import fetch_project_items, search_items
from github_audit.rules import evaluate_item


def scan_all(
    client: GitHubClient,
    settings: Settings,
    discoveries: list[DiscoveryResult],
    searched_items: list[GitHubContent] | None = None,
) -> list[AuditResult]:
    if not discoveries:
        return []
    if searched_items is None:
        searched_items = _search_items(client, settings, discoveries[0].repositories)
    project_items_by_number: dict[int, list[ProjectItem]] = {}
    known_project_content_ids: set[str] | None = None
    if settings.require_project_item and len(discoveries) > 1:
        project_items_by_number = {
            discovery.project_number: fetch_project_items(
                client, settings.github_org, discovery.project_number
            )
            for discovery in discoveries
        }
        known_project_content_ids = {
            item.content_id
            for project_items in project_items_by_number.values()
            for item in project_items
            if item.content_id is not None
        }
    return [
        scan(
            client,
            settings,
            discovery,
            searched_items,
            project_items=project_items_by_number.get(discovery.project_number),
            known_project_content_ids=known_project_content_ids,
        )
        for discovery in discoveries
    ]


def scan(
    client: GitHubClient,
    settings: Settings,
    discovery: DiscoveryResult,
    searched_items: list[GitHubContent] | None = None,
    *,
    project_items: list[ProjectItem] | None = None,
    known_project_content_ids: set[str] | None = None,
) -> AuditResult:
    if project_items is None:
        project_items = fetch_project_items(client, settings.github_org, discovery.project_number)
    allowed_repositories = set(discovery.repositories)
    project_by_content_id = {
        item.content_id: item
        for item in project_items
        if item.content_id is not None and item.repository in allowed_repositories
    }
    content_by_id: dict[str, GitHubContent] = {}
    for item in project_items:
        content = content_from_project_item(item, allowed_repositories)
        if content is not None:
            content_by_id[content.id] = content
    if searched_items is None:
        searched_items = _search_items(client, settings, discovery.repositories)
    for item in searched_items:
        content_by_id[item.id] = item

    findings: list[AuditFinding] = []
    issue_count = 0
    pull_request_count = 0
    for content in content_by_id.values():
        if not in_updated_range(content, settings):
            continue
        # Board-sourced items bypass the search filters, so type/state inclusion
        # must be enforced here (e.g. closed issues sitting on the project board).
        if not type_and_state_included(content, settings):
            continue
        project_item = project_by_content_id.get(content.id)
        if (
            project_item is None
            and known_project_content_ids is not None
            and content.id in known_project_content_ids
        ):
            continue
        if isinstance(content, GitHubIssue):
            issue_count += 1
        if isinstance(content, GitHubPullRequest):
            pull_request_count += 1
        finding = evaluate_item(content, project_item, settings)
        if finding is not None:
            finding.project_number = discovery.project_number
            finding.project_title = discovery.project_title
            findings.append(finding)

    return AuditResult(
        organization=settings.github_org,
        project_number=discovery.project_number,
        project_title=discovery.project_title,
        repositories=discovery.repositories,
        findings=sorted(findings, key=lambda item: (item.repository, item.item_type, item.number)),
        scanned_issue_count=issue_count,
        scanned_pull_request_count=pull_request_count,
        limitations=discovery.development_limitations,
    )


def _search_items(
    client: GitHubClient, settings: Settings, repositories: list[str]
) -> list[GitHubContent]:
    return search_items(
        client,
        repositories,
        settings.target_assignees,
        include_issues=settings.include_issues,
        include_pull_requests=settings.include_pull_requests,
        include_closed_issues=settings.include_closed_issues,
        include_closed_pull_requests=settings.include_closed_pull_requests,
        include_unassigned=settings.include_unassigned,
    )


def content_from_project_item(
    item: ProjectItem, allowed_repositories: set[str]
) -> GitHubContent | None:
    if (
        item.content_id is None
        or item.repository not in allowed_repositories
        or item.number is None
        or item.url is None
    ):
        return None
    if item.content_type == "issue":
        return GitHubIssue(
            id=item.content_id,
            repository=item.repository,
            number=item.number,
            title=item.title,
            url=item.url,
            state=item.state,
            body=item.body,
            assignees=item.assignees,
            labels=item.labels,
            comments=item.comments,
            comments_total_count=item.comments_total_count,
            milestone=item.milestone,
            updated_at=item.updated_at,
            linked_pull_requests_count=item.linked_pull_requests_count,
        )
    if item.content_type == "pull_request":
        return GitHubPullRequest(
            id=item.content_id,
            repository=item.repository,
            number=item.number,
            title=item.title,
            url=item.url,
            state=item.state,
            body=item.body,
            assignees=item.assignees,
            labels=item.labels,
            comments=item.comments,
            comments_total_count=item.comments_total_count,
            milestone=item.milestone,
            updated_at=item.updated_at,
            closing_issues_count=item.closing_issues_count,
        )
    return None


def type_and_state_included(content: GitHubContent, settings: Settings) -> bool:
    """Apply the include-issues/PRs and include-closed toggles to one item."""
    state = content.state.upper()
    if isinstance(content, GitHubIssue):
        if not settings.include_issues:
            return False
        return settings.include_closed_issues or state != "CLOSED"
    if not settings.include_pull_requests:
        return False
    return settings.include_closed_pull_requests or state not in {"CLOSED", "MERGED"}


def in_updated_range(content: GitHubContent, settings: Settings) -> bool:
    if settings.github_updated_from is None and settings.github_updated_to is None:
        return True
    updated = parse_github_date(content.updated_at)
    if updated is None:
        return False
    if settings.github_updated_from is not None and updated < settings.github_updated_from:
        return False
    return settings.github_updated_to is None or updated <= settings.github_updated_to


def parse_github_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def build_my_work(
    client: GitHubClient,
    settings: Settings,
    repositories: list[str],
) -> MyWorkResult:
    items = search_items(
        client,
        repositories,
        settings.target_assignees,
        include_issues=settings.include_issues,
        include_pull_requests=settings.include_pull_requests,
        include_closed_issues=False,
        include_closed_pull_requests=False,
        include_unassigned=False,
    )
    status_by_content_id: dict[str, str] = {}
    for project_number in settings.github_project_numbers:
        for project_item in fetch_project_items(client, settings.github_org, project_number):
            if project_item.content_id and "Status" in project_item.field_values:
                status_by_content_id[project_item.content_id] = str(
                    project_item.field_values["Status"].value
                )
    target_set = set(settings.target_assignees)
    result_items = [
        MyWorkItem(
            repository=item.repository,
            item_type="issue" if isinstance(item, GitHubIssue) else "pull_request",
            number=item.number,
            title=item.title,
            url=item.url,
            updated_at=item.updated_at,
            assignees=[a for a in item.assignees if a in target_set],
            labels=item.labels,
            milestone=item.milestone,
            project_status=status_by_content_id.get(item.id),
        )
        for item in items
        if any(a in target_set for a in item.assignees)
    ]
    result_items.sort(key=lambda x: (x.repository, x.item_type, x.number))
    return MyWorkResult(
        organization=settings.github_org,
        assignees=settings.target_assignees,
        items=result_items,
    )
