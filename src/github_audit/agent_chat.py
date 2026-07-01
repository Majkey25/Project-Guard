from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from github_audit.applier import add_suggested_change
from github_audit.models import ApplyChange, ApplyPlan, AuditFinding, ProjectFieldDefinition

ControlName = Literal[
    "include_issues",
    "include_pull_requests",
    "include_closed_issues",
    "include_closed_pull_requests",
]


@dataclass(frozen=True)
class ControlUpdate:
    name: ControlName
    value: bool


@dataclass(frozen=True)
class FieldRequest:
    field_name: str
    value: str | int | float | bool


@dataclass(frozen=True)
class AgentCommand:
    control_updates: tuple[ControlUpdate, ...] = ()
    run_scan: bool = False
    apply_pending: bool = False
    explain: bool = False


def parse_agent_command(text: str) -> AgentCommand:
    normalized = " ".join(text.casefold().split())
    updates: list[ControlUpdate] = []

    if "closed issue" in normalized:
        updates.append(ControlUpdate("include_issues", True))
        updates.append(ControlUpdate("include_closed_issues", True))
    if "closed pr" in normalized or "closed pull request" in normalized:
        updates.append(ControlUpdate("include_pull_requests", True))
        updates.append(ControlUpdate("include_closed_pull_requests", True))
    if "only pr" in normalized or "only pull request" in normalized:
        updates.append(ControlUpdate("include_issues", False))
        updates.append(ControlUpdate("include_pull_requests", True))
    if "only issue" in normalized:
        updates.append(ControlUpdate("include_issues", True))
        updates.append(ControlUpdate("include_pull_requests", False))
    if "issues and pr" in normalized or "issues and pull request" in normalized:
        updates.append(ControlUpdate("include_issues", True))
        updates.append(ControlUpdate("include_pull_requests", True))

    return AgentCommand(
        control_updates=tuple(_dedupe_updates(updates)),
        run_scan=_wants_scan(normalized),
        apply_pending=_wants_apply(normalized),
        explain=_wants_explain(normalized),
    )


def build_field_plan(
    finding: AuditFinding,
    fields: list[ProjectFieldDefinition],
    request: FieldRequest,
    *,
    replace_existing: bool = False,
) -> ApplyPlan:
    changes: list[ApplyChange] = []
    skipped: list[str] = []
    if finding.project_item_id is None:
        skipped.append(f"{finding.repository}#{finding.number}: no project item")
        return ApplyPlan(changes=changes, skipped=skipped)

    field_name = _resolve_field_name(fields, request)
    value = request.value

    add_suggested_change(
        changes,
        skipped,
        finding.repository,
        finding.item_type,
        finding.number,
        finding.project_item_id,
        {field.name: field for field in fields},
        field_name,
        value,
        finding.current_project_fields,
        replace_existing=replace_existing,
    )
    return ApplyPlan(changes=changes, skipped=skipped)


def summarize_findings(total_rows: int, visible_rows: int, stats: Mapping[str, int] | None) -> str:
    if stats is None:
        return "No scan results yet. Run a scan first."
    return (
        f"Scan has {stats['findings']} findings from {stats['issues']} issues "
        f"and {stats['prs']} PRs. Table shows {visible_rows} of {total_rows} rows."
    )


def _resolve_field_name(fields: list[ProjectFieldDefinition], request: FieldRequest) -> str:
    requested = request.field_name.casefold()
    for field in fields:
        if field.name.casefold() == requested:
            return field.name
    return request.field_name


def _dedupe_updates(updates: list[ControlUpdate]) -> list[ControlUpdate]:
    deduped: dict[ControlName, bool] = {}
    for update in updates:
        deduped[update.name] = update.value
    return [ControlUpdate(name, value) for name, value in deduped.items()]


def _wants_scan(normalized: str) -> bool:
    return any(word in normalized for word in ("run", "rerun", "rescan", "scan again"))


def _wants_apply(normalized: str) -> bool:
    return normalized in {"apply", "apply it", "confirm", "confirm it", "write it", "do it"}


def _wants_explain(normalized: str) -> bool:
    return any(word in normalized for word in ("explain", "why", "tell me about", "summarize"))
