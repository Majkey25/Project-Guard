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
    ProjectItem,
)
from github_audit.project_fields import fetch_project_items, search_items
from github_audit.rules import evaluate_item


def scan_all(
    client: GitHubClient, settings: Settings, discoveries: list[DiscoveryResult]
) -> list[AuditResult]:
    if not discoveries:
        return []
    searched_items = search_items(
        client,
        discoveries[0].repositories,
        settings.target_assignees,
        include_issues=settings.include_issues,
        include_pull_requests=settings.include_pull_requests,
        include_closed_issues=settings.include_closed_issues,
        include_closed_pull_requests=settings.include_closed_pull_requests,
        include_unassigned=settings.include_unassigned,
    )
    return [scan(client, settings, discovery, searched_items) for discovery in discoveries]


def scan(
    client: GitHubClient,
    settings: Settings,
    discovery: DiscoveryResult,
    searched_items: list[GitHubContent] | None = None,
) -> AuditResult:
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
        searched_items = search_items(
            client,
            discovery.repositories,
            settings.target_assignees,
            include_issues=settings.include_issues,
            include_pull_requests=settings.include_pull_requests,
            include_closed_issues=settings.include_closed_issues,
        )
    for item in searched_items:
        content_by_id[item.id] = item

    findings: list[AuditFinding] = []
    issue_count = 0
    pull_request_count = 0
    for content in content_by_id.values():
        if not in_updated_range(content, settings):
            continue
        if isinstance(content, GitHubIssue):
            issue_count += 1
        if isinstance(content, GitHubPullRequest):
            pull_request_count += 1
        finding = evaluate_item(content, project_by_content_id.get(content.id), settings)
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
            state="",
            body="",
            assignees=item.assignees,
            labels=item.labels,
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
            state="",
            body="",
            assignees=item.assignees,
            labels=item.labels,
            milestone=item.milestone,
            updated_at=item.updated_at,
            closing_issues_count=item.closing_issues_count,
        )
    return None


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
