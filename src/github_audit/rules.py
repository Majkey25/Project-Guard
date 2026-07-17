from __future__ import annotations

from github_audit.config import Settings
from github_audit.models import (
    AuditFinding,
    GitHubContent,
    GitHubIssue,
    ProjectItem,
)


def evaluate_item(
    content: GitHubContent,
    project_item: ProjectItem | None,
    settings: Settings,
) -> AuditFinding | None:
    missing: list[str] = []
    if settings.require_assignee and not content.assignees:
        missing.append("assignee")
    if settings.require_target_assignee:
        targets = set(settings.target_assignees)
        if not targets.intersection(content.assignees):
            missing.append("target assignee")
    # PRs only need a board item when explicitly required; issues always do.
    if (
        settings.require_project_item
        and project_item is None
        and (isinstance(content, GitHubIssue) or settings.require_project_item_pull_requests)
    ):
        missing.append("Project item")
    current_values: dict[str, str] = {}
    if project_item is not None:
        current_values = {
            name: str(value.value) for name, value in sorted(project_item.field_values.items())
        }
        present_names = {name.casefold() for name in project_item.field_values}
        for field_name in settings.required_project_fields:
            if field_name.casefold() not in present_names:
                missing.append(field_name)
    if (
        settings.require_development_link or settings.require_linked_pr_or_branch
    ) and not has_development_link(content, project_item):
        missing.append("Development link")
    if not missing:
        return None
    item_type = "issue" if isinstance(content, GitHubIssue) else "pull_request"
    return AuditFinding(
        content_id=content.id,
        repository=content.repository,
        item_type=item_type,
        number=content.number,
        title=content.title,
        body=content.body,
        comments=content.comments,
        comments_total_count=content.comments_total_count,
        url=content.url,
        state=content.state,
        is_draft=not isinstance(content, GitHubIssue) and content.is_draft,
        assignees=content.assignees,
        labels=content.labels,
        milestone=content.milestone,
        updated_at=content.updated_at,
        missing_fields=missing,
        current_project_fields=current_values,
        development_status=development_status(content, project_item),
        project_item_id=project_item.id if project_item else None,
    )


def has_development_link(content: GitHubContent, project_item: ProjectItem | None) -> bool:
    if isinstance(content, GitHubIssue):
        return content.linked_pull_requests_count > 0 or (
            project_item is not None and project_item.linked_pull_requests_count > 0
        )
    return content.closing_issues_count > 0 or (
        project_item is not None and project_item.closing_issues_count > 0
    )


def development_status(content: GitHubContent, project_item: ProjectItem | None) -> str:
    if isinstance(content, GitHubIssue):
        count = content.linked_pull_requests_count
        if project_item is not None:
            count = max(count, project_item.linked_pull_requests_count)
        return f"linked_pull_requests={count}"
    count = content.closing_issues_count
    if project_item is not None:
        count = max(count, project_item.closing_issues_count)
    return f"closing_issues={count}"
