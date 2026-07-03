from __future__ import annotations

from datetime import date
from math import isfinite

from github_audit.config import Settings
from github_audit.github_client import GitHubClient, GitHubError, JsonObject
from github_audit.models import (
    AddToProjectPlan,
    ApplyChange,
    ApplyPlan,
    ApplyResult,
    AssigneeUpdatePlan,
    AuditResult,
    IssueCommentPlan,
    IssueEditPlan,
    ItemType,
    LabelUpdatePlan,
    MilestoneUpdatePlan,
    PendingWrite,
    ProjectFieldDefinition,
    PullRequestMergePlan,
    ReviewerRequestPlan,
    StateUpdatePlan,
)

UPDATE_FIELD_MUTATION = """
mutation UpdateProjectField($input: UpdateProjectV2ItemFieldValueInput!) {
  updateProjectV2ItemFieldValue(input: $input) {
    projectV2Item { id }
  }
}
"""

ADD_PROJECT_ITEM_MUTATION = """
mutation AddProjectItem($input: AddProjectV2ItemByIdInput!) {
  addProjectV2ItemById(input: $input) {
    item { id }
  }
}
"""

ADD_COMMENT_MUTATION = """
mutation AddComment($input: AddCommentInput!) {
  addComment(input: $input) {
    commentEdge { node { id } }
  }
}
"""

UPDATE_ISSUE_MUTATION = """
mutation UpdateIssue($input: UpdateIssueInput!) {
  updateIssue(input: $input) { __typename }
}
"""

UPDATE_PR_MUTATION = """
mutation UpdatePullRequest($input: UpdatePullRequestInput!) {
  updatePullRequest(input: $input) { __typename }
}
"""

ADD_LABELS_MUTATION = """
mutation AddLabels($input: AddLabelsToLabelableInput!) {
  addLabelsToLabelable(input: $input) { __typename }
}
"""

REMOVE_LABELS_MUTATION = """
mutation RemoveLabels($input: RemoveLabelsFromLabelableInput!) {
  removeLabelsFromLabelable(input: $input) { __typename }
}
"""

ADD_ASSIGNEES_MUTATION = """
mutation AddAssignees($input: AddAssigneesToAssignableInput!) {
  addAssigneesToAssignable(input: $input) { __typename }
}
"""

REMOVE_ASSIGNEES_MUTATION = """
mutation RemoveAssignees($input: RemoveAssigneesFromAssignableInput!) {
  removeAssigneesFromAssignable(input: $input) { __typename }
}
"""

CLOSE_ISSUE_MUTATION = """
mutation CloseIssue($input: CloseIssueInput!) {
  closeIssue(input: $input) { __typename }
}
"""

REOPEN_ISSUE_MUTATION = """
mutation ReopenIssue($input: ReopenIssueInput!) {
  reopenIssue(input: $input) { __typename }
}
"""

CLOSE_PR_MUTATION = """
mutation ClosePullRequest($input: ClosePullRequestInput!) {
  closePullRequest(input: $input) { __typename }
}
"""

REOPEN_PR_MUTATION = """
mutation ReopenPullRequest($input: ReopenPullRequestInput!) {
  reopenPullRequest(input: $input) { __typename }
}
"""

MERGE_PR_MUTATION = """
mutation MergePullRequest($input: MergePullRequestInput!) {
  mergePullRequest(input: $input) { __typename }
}
"""

REQUEST_REVIEWS_MUTATION = """
mutation RequestReviews($input: RequestReviewsInput!) {
  requestReviews(input: $input) { __typename }
}
"""


class PartialApplyError(RuntimeError):
    """apply_plan() failed partway through; carries the changes that already succeeded."""

    def __init__(
        self, applied: list[ApplyChange], skipped: list[str], cause: BaseException
    ) -> None:
        super().__init__(str(cause))
        self.applied = applied
        self.skipped = skipped
        self.cause = cause


def build_apply_plan(
    audit: AuditResult,
    fields: list[ProjectFieldDefinition],
    settings: Settings,
) -> ApplyPlan:
    fields_by_name = {field.name: field for field in fields}
    changes: list[ApplyChange] = []
    skipped: list[str] = []
    for finding in audit.findings:
        suggestion = finding.llm_suggestion
        if suggestion is None or finding.project_item_id is None:
            skipped.append(f"{finding.repository}#{finding.number}: no suggestion or project item")
            continue
        if (
            not suggestion.should_auto_apply
            or suggestion.confidence < settings.auto_apply_min_confidence
        ):
            skipped.append(f"{finding.repository}#{finding.number}: suggestion confidence too low")
            continue
        add_suggested_change(
            changes,
            skipped,
            finding.repository,
            finding.item_type,
            finding.number,
            finding.project_item_id,
            fields_by_name,
            "Estimate",
            suggestion.estimated_points,
            finding.current_project_fields,
        )
        add_suggested_change(
            changes,
            skipped,
            finding.repository,
            finding.item_type,
            finding.number,
            finding.project_item_id,
            fields_by_name,
            "Difficulty",
            suggestion.difficulty,
            finding.current_project_fields,
        )
        add_suggested_change(
            changes,
            skipped,
            finding.repository,
            finding.item_type,
            finding.number,
            finding.project_item_id,
            fields_by_name,
            "Priority",
            suggestion.priority,
            finding.current_project_fields,
        )
        add_suggested_change(
            changes,
            skipped,
            finding.repository,
            finding.item_type,
            finding.number,
            finding.project_item_id,
            fields_by_name,
            "Iteration (sprint)",
            suggestion.suggested_iteration,
            finding.current_project_fields,
        )
    return ApplyPlan(changes=changes, skipped=skipped)


def add_suggested_change(
    changes: list[ApplyChange],
    skipped: list[str],
    repository: str,
    item_type: ItemType,
    number: int,
    project_item_id: str,
    fields_by_name: dict[str, ProjectFieldDefinition],
    field_name: str,
    value: str | int | float | bool | None,
    current_project_fields: dict[str, str],
    *,
    replace_existing: bool = False,
    content_id: str | None = None,
) -> None:
    if value in (None, ""):
        return
    if field_name in current_project_fields and not replace_existing:
        skipped.append(f"{repository}#{number}: {field_name} already set")
        return
    field = fields_by_name.get(field_name)
    if field is None:
        skipped.append(f"{repository}#{number}: {field_name} not found")
        return
    option_id = field.options.get(str(value)) if field.kind == "single_select" else None
    iteration_id = field.iterations.get(str(value)) if field.kind == "iteration" else None
    if field.kind == "single_select" and option_id is None:
        skipped.append(f"{repository}#{number}: option {value!r} not found for {field_name}")
        return
    if field.kind == "iteration" and iteration_id is None:
        skipped.append(f"{repository}#{number}: iteration {value!r} not found")
        return
    value = normalize_field_value(field, value, repository, number, skipped)
    if value is None:
        return
    if item_type not in {"issue", "pull_request"}:
        skipped.append(f"{repository}#{number}: unsupported item type")
        return
    changes.append(
        ApplyChange(
            repository=repository,
            item_type=item_type,
            number=number,
            project_item_id=project_item_id,
            field_name=field_name,
            value=value,
            option_id=option_id,
            iteration_id=iteration_id,
            content_id=content_id,
        )
    )


def apply_plan(
    client: GitHubClient,
    plan: ApplyPlan,
    project_id: str,
    fields: list[ProjectFieldDefinition],
    *,
    dry_run: bool,
    allow_write: bool,
    created_item_ids: dict[str, str] | None = None,
) -> ApplyResult:
    if dry_run or not allow_write:
        skipped = list(plan.skipped)
        if not allow_write:
            skipped.append("writes disabled; require AUTO_APPLY=true and --yes")
        return ApplyResult(
            dry_run=True,
            applied=[],
            skipped=skipped + describe_changes(plan.changes),
        )
    fields_by_name = {field.name: field for field in fields}
    applied: list[ApplyChange] = []
    skipped = list(plan.skipped)
    for change in plan.changes:
        field = fields_by_name[change.field_name]
        item_id = change.project_item_id or (created_item_ids or {}).get(change.content_id or "")
        if not item_id:
            cause = ValueError(
                f"{change.repository}#{change.number} is not on the project board;"
                " add it to the project first"
            )
            raise PartialApplyError(applied, skipped, cause)
        variables: JsonObject = {
            "input": {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": field.id,
                "value": build_update_value(change, field),
            }
        }
        try:
            client.graphql(UPDATE_FIELD_MUTATION, variables)
        except GitHubError as exc:
            raise PartialApplyError(applied, skipped, exc) from exc
        applied.append(change)
    return ApplyResult(dry_run=False, applied=applied, skipped=skipped)


def build_update_value(
    change: ApplyChange,
    field: ProjectFieldDefinition | None = None,
) -> JsonObject:
    if change.option_id:
        return {"singleSelectOptionId": change.option_id}
    if change.iteration_id:
        return {"iterationId": change.iteration_id}
    if field is not None and field.data_type.upper() == "DATE":
        return {"date": str(change.value)}
    if isinstance(change.value, int | float):
        return {"number": change.value}
    return {"text": str(change.value)}


def apply_add_to_project(client: GitHubClient, plan: AddToProjectPlan) -> str:
    """Add the item to the project board; returns the new project item id."""
    data = client.graphql(
        ADD_PROJECT_ITEM_MUTATION,
        {"input": {"projectId": plan.project_id, "contentId": plan.content_id}},
    )
    payload = data.get("addProjectV2ItemById")
    item = payload.get("item") if isinstance(payload, dict) else None
    item_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(item_id, str) or not item_id:
        msg = "addProjectV2ItemById returned no item id"
        raise GitHubError(msg)
    return item_id


def add_comment(client: GitHubClient, subject_id: str, body: str) -> None:
    body = body.strip()
    if not body:
        msg = "comment body is empty"
        raise ValueError(msg)
    client.graphql(ADD_COMMENT_MUTATION, {"input": {"subjectId": subject_id, "body": body}})


def normalize_field_value(
    field: ProjectFieldDefinition,
    value: str | int | float | bool,
    repository: str,
    number: int,
    skipped: list[str],
) -> str | int | float | bool | None:
    if field.kind != "field":
        return value
    data_type = field.data_type.upper()
    if data_type == "NUMBER":
        return _number_value(value, repository, number, field.name, skipped)
    if data_type == "DATE":
        if isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError:
                skipped.append(f"{repository}#{number}: {field.name} needs YYYY-MM-DD date")
                return None
            return value
        skipped.append(f"{repository}#{number}: {field.name} needs YYYY-MM-DD date")
        return None
    if data_type == "TEXT":
        return str(value)
    skipped.append(f"{repository}#{number}: {field.name} type {field.data_type} is not writable")
    return None


def _number_value(
    value: str | int | float | bool,
    repository: str,
    number: int,
    field_name: str,
    skipped: list[str],
) -> int | float | None:
    if isinstance(value, bool):
        skipped.append(f"{repository}#{number}: {field_name} needs a number")
        return None
    if isinstance(value, int | float):
        if isinstance(value, float) and not isfinite(value):
            skipped.append(f"{repository}#{number}: {field_name} needs a finite number")
            return None
        return value
    try:
        parsed = float(value)
    except ValueError:
        skipped.append(f"{repository}#{number}: {field_name} needs a number")
        return None
    if not isfinite(parsed):
        skipped.append(f"{repository}#{number}: {field_name} needs a finite number")
        return None
    return int(parsed) if parsed.is_integer() else parsed


def describe_changes(changes: list[ApplyChange]) -> list[str]:
    return [
        f"dry-run: {change.repository}#{change.number} set {change.field_name}={change.value}"
        for change in changes
    ]


def apply_issue_edit(client: GitHubClient, plan: IssueEditPlan) -> None:
    is_issue = plan.item_type == "issue"
    input_obj: JsonObject = {"id" if is_issue else "pullRequestId": plan.content_id}
    if plan.title is not None:
        input_obj["title"] = plan.title
    if plan.body is not None:
        input_obj["body"] = plan.body
    client.graphql(UPDATE_ISSUE_MUTATION if is_issue else UPDATE_PR_MUTATION, {"input": input_obj})


def apply_label_update(client: GitHubClient, plan: LabelUpdatePlan) -> None:
    if plan.add_label_ids:
        client.graphql(
            ADD_LABELS_MUTATION,
            {
                "input": {
                    "labelableId": plan.content_id,
                    "labelIds": list(plan.add_label_ids.values()),
                }
            },
        )
    if plan.remove_label_ids:
        client.graphql(
            REMOVE_LABELS_MUTATION,
            {
                "input": {
                    "labelableId": plan.content_id,
                    "labelIds": list(plan.remove_label_ids.values()),
                }
            },
        )


def apply_assignee_update(client: GitHubClient, plan: AssigneeUpdatePlan) -> None:
    if plan.add_user_ids:
        client.graphql(
            ADD_ASSIGNEES_MUTATION,
            {
                "input": {
                    "assignableId": plan.content_id,
                    "assigneeIds": list(plan.add_user_ids.values()),
                }
            },
        )
    if plan.remove_user_ids:
        client.graphql(
            REMOVE_ASSIGNEES_MUTATION,
            {
                "input": {
                    "assignableId": plan.content_id,
                    "assigneeIds": list(plan.remove_user_ids.values()),
                }
            },
        )


def apply_state_update(client: GitHubClient, plan: StateUpdatePlan) -> None:
    if plan.item_type == "issue":
        if plan.action == "close":
            input_obj: JsonObject = {"issueId": plan.content_id}
            if plan.reason is not None:
                input_obj["stateReason"] = plan.reason
            client.graphql(CLOSE_ISSUE_MUTATION, {"input": input_obj})
        else:
            client.graphql(REOPEN_ISSUE_MUTATION, {"input": {"issueId": plan.content_id}})
    elif plan.action == "close":
        client.graphql(CLOSE_PR_MUTATION, {"input": {"pullRequestId": plan.content_id}})
    else:
        client.graphql(REOPEN_PR_MUTATION, {"input": {"pullRequestId": plan.content_id}})


def apply_milestone_update(client: GitHubClient, plan: MilestoneUpdatePlan) -> None:
    is_issue = plan.item_type == "issue"
    key = "id" if is_issue else "pullRequestId"
    # milestoneId is always sent explicitly (including null to clear) - GitHub's schema treats
    # an omitted key as "leave unchanged" and an explicit null as "clear", so the two must not
    # be conflated.
    client.graphql(
        UPDATE_ISSUE_MUTATION if is_issue else UPDATE_PR_MUTATION,
        {"input": {key: plan.content_id, "milestoneId": plan.milestone_id}},
    )


def apply_pr_merge(client: GitHubClient, plan: PullRequestMergePlan) -> None:
    client.graphql(
        MERGE_PR_MUTATION,
        {"input": {"pullRequestId": plan.content_id, "mergeMethod": plan.merge_method}},
    )


def apply_reviewer_request(client: GitHubClient, plan: ReviewerRequestPlan) -> None:
    client.graphql(
        REQUEST_REVIEWS_MUTATION,
        {
            "input": {
                "pullRequestId": plan.content_id,
                "userIds": list(plan.user_ids.values()),
                "union": True,
            }
        },
    )


def apply_pending_write(
    client: GitHubClient,
    write: PendingWrite,
    *,
    project_id: str | None = None,
    fields: list[ProjectFieldDefinition] | None = None,
    created_item_ids: dict[str, str] | None = None,
) -> None:
    """Execute one queued write. Raises PartialApplyError or GitHubError on failure.

    created_item_ids (content id -> new project item id) carries the result of
    AddToProjectPlan writes to later field updates in the same batch; pass the
    same dict for every write of the batch.
    """
    if isinstance(write, ApplyPlan):
        if project_id is None or fields is None:
            msg = "project id and fields are required to apply a project field update"
            raise ValueError(msg)
        apply_plan(
            client,
            write,
            project_id,
            fields,
            dry_run=False,
            allow_write=True,
            created_item_ids=created_item_ids,
        )
    elif isinstance(write, AddToProjectPlan):
        new_item_id = apply_add_to_project(client, write)
        if created_item_ids is not None:
            created_item_ids[write.content_id] = new_item_id
    elif isinstance(write, IssueCommentPlan):
        add_comment(client, write.subject_id, write.body)
    elif isinstance(write, IssueEditPlan):
        apply_issue_edit(client, write)
    elif isinstance(write, LabelUpdatePlan):
        apply_label_update(client, write)
    elif isinstance(write, AssigneeUpdatePlan):
        apply_assignee_update(client, write)
    elif isinstance(write, StateUpdatePlan):
        apply_state_update(client, write)
    elif isinstance(write, MilestoneUpdatePlan):
        apply_milestone_update(client, write)
    elif isinstance(write, PullRequestMergePlan):
        apply_pr_merge(client, write)
    else:
        apply_reviewer_request(client, write)


def describe_pending_write(write: PendingWrite) -> list[str]:
    if isinstance(write, ApplyPlan):
        return describe_changes(write.changes)
    if isinstance(write, AddToProjectPlan):
        target = write.project_title or write.project_id
        return [f"dry-run: {write.repository}#{write.number} add to project {target}"]
    if isinstance(write, IssueCommentPlan):
        return [
            f"dry-run: {write.repository}#{write.number} add comment",
            f"  body -> {write.body!r}",
        ]
    if isinstance(write, IssueEditPlan):
        lines = [f"dry-run: {write.repository}#{write.number} edit"]
        if write.title is not None:
            lines.append(f"  title -> {write.title!r}")
        if write.body is not None:
            lines.append(f"  body -> {write.body!r}")
        if len(lines) == 1:
            lines.append("  (no changes)")
        return lines
    if isinstance(write, LabelUpdatePlan):
        parts: list[str] = []
        if write.add_label_ids:
            parts.append(f"add={sorted(write.add_label_ids)}")
        if write.remove_label_ids:
            parts.append(f"remove={sorted(write.remove_label_ids)}")
        return [f"dry-run: {write.repository}#{write.number} labels {', '.join(parts)}"]
    if isinstance(write, AssigneeUpdatePlan):
        parts: list[str] = []
        if write.add_user_ids:
            parts.append(f"add={sorted(write.add_user_ids)}")
        if write.remove_user_ids:
            parts.append(f"remove={sorted(write.remove_user_ids)}")
        return [f"dry-run: {write.repository}#{write.number} assignees {', '.join(parts)}"]
    if isinstance(write, StateUpdatePlan):
        reason = f" ({write.reason})" if write.reason else ""
        return [f"dry-run: {write.repository}#{write.number} {write.action}{reason}"]
    if isinstance(write, MilestoneUpdatePlan):
        target = write.milestone_title or "(clear)"
        return [f"dry-run: {write.repository}#{write.number} milestone -> {target}"]
    if isinstance(write, PullRequestMergePlan):
        return [
            f"dry-run: {write.repository}#{write.number} merge via {write.merge_method}"
            " (not easily reversible)"
        ]
    reviewers = sorted(write.user_ids)
    return [f"dry-run: {write.repository}#{write.number} request review from {reviewers}"]
