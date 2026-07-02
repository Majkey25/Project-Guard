from __future__ import annotations

from github_audit.config import Settings
from github_audit.github_client import GitHubClient, required_int, required_str
from github_audit.models import DiscoveryResult, GitHubContent, GitHubIssue, GitHubPullRequest
from github_audit.project_fields import (
    fetch_project_fields,
    fetch_project_items,
    fetch_project_numbers,
    fetch_repositories,
    probe_branch_links,
    search_items,
)


def discover(client: GitHubClient, settings: Settings) -> DiscoveryResult:
    return discover_all(client, settings)[0]


def discover_all(
    client: GitHubClient,
    settings: Settings,
    *,
    repositories: list[str] | None = None,
    searched_items: list[GitHubContent] | None = None,
) -> list[DiscoveryResult]:
    if repositories is None:
        repositories = discover_repositories(client, settings)
    samples = (
        searched_items
        if searched_items is not None
        else search_items(
            client,
            repositories,
            settings.target_assignees,
            include_issues=settings.include_issues,
            include_pull_requests=settings.include_pull_requests,
            include_closed_issues=settings.include_closed_issues,
        )
    )
    issue_sample_count = sum(isinstance(item, GitHubIssue) for item in samples)
    pull_request_sample_count = sum(isinstance(item, GitHubPullRequest) for item in samples)
    branch_available, branch_detail = probe_branch_links(client, repositories)
    project_numbers = (
        fetch_project_numbers(
            client,
            settings.github_org,
            include_closed=settings.github_include_closed_projects,
        )
        if settings.github_include_all_projects
        else settings.github_project_numbers
    )
    return [
        discover_project(
            client,
            settings,
            project_number,
            repositories,
            issue_sample_count,
            pull_request_sample_count,
            branch_available,
            branch_detail,
        )
        for project_number in project_numbers
    ]


def discover_repositories(client: GitHubClient, settings: Settings) -> list[str]:
    repositories = fetch_repositories(
        client,
        settings.github_org,
        settings.repository_allowlist,
        settings.github_include_all_repositories,
    )
    denylist = set(settings.repository_denylist)
    return [
        repository for repository in repositories if repository.split("/", 1)[1] not in denylist
    ]


def discover_project(
    client: GitHubClient,
    settings: Settings,
    project_number: int,
    repositories: list[str],
    issue_sample_count: int,
    pull_request_sample_count: int,
    branch_available: bool,
    branch_detail: str,
) -> DiscoveryResult:
    project, fields = fetch_project_fields(client, settings.github_org, project_number)
    project_items = fetch_project_items(client, settings.github_org, project_number)
    field_names = {field.name for field in fields}
    required_missing = [
        name for name in settings.required_project_fields if name not in field_names
    ]
    limitations: list[str] = []
    if not branch_available:
        limitations.append(f"Branch link probe: {branch_detail}")
    content_types = sorted({item.content_type for item in project_items})
    return DiscoveryResult(
        organization=settings.github_org,
        project_id=required_str(project.get("id"), "project.id"),
        project_number=required_int(project.get("number"), "project.number"),
        project_title=required_str(project.get("title"), "project.title"),
        project_url=required_str(project.get("url"), "project.url"),
        repositories=repositories,
        fields=fields,
        required_fields_missing=required_missing,
        issue_sample_count=issue_sample_count,
        pull_request_sample_count=pull_request_sample_count,
        project_item_sample_count=min(len(project_items), 50),
        content_types=content_types,
        development_strategy="closing references via GraphQL; branch links reported as limitation",
        development_limitations=limitations,
    )
