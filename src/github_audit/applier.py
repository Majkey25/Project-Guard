from __future__ import annotations

from github_audit.config import Settings
from github_audit.github_client import GitHubClient, JsonObject
from github_audit.models import (
    ApplyChange,
    ApplyPlan,
    ApplyResult,
    AuditResult,
    ItemType,
    ProjectFieldDefinition,
)

UPDATE_FIELD_MUTATION = """
mutation UpdateProjectField($input: UpdateProjectV2ItemFieldValueInput!) {
  updateProjectV2ItemFieldValue(input: $input) {
    projectV2Item { id }
  }
}
"""


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
    value: str | int | None,
    current_project_fields: dict[str, str],
) -> None:
    if value in (None, ""):
        return
    if field_name in current_project_fields:
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
        variables: JsonObject = {
            "input": {
                "projectId": project_id,
                "itemId": change.project_item_id,
                "fieldId": field.id,
                "value": build_update_value(change),
            }
        }
        client.graphql(
            UPDATE_FIELD_MUTATION,
            variables,
        )
        applied.append(change)
    return ApplyResult(dry_run=False, applied=applied, skipped=skipped)


def build_update_value(change: ApplyChange) -> JsonObject:
    if change.option_id:
        return {"singleSelectOptionId": change.option_id}
    if change.iteration_id:
        return {"iterationId": change.iteration_id}
    if isinstance(change.value, int | float):
        return {"number": change.value}
    return {"text": str(change.value)}


def describe_changes(changes: list[ApplyChange]) -> list[str]:
    return [
        f"dry-run: {change.repository}#{change.number} set {change.field_name}={change.value}"
        for change in changes
    ]
