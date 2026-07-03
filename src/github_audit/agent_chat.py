from __future__ import annotations

import re
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
    allow_pending_board_add: bool = False,
) -> ApplyPlan:
    """Build a one-field ApplyPlan.

    With allow_pending_board_add, an item that is not on the board yet is queued
    with an empty project_item_id — resolved at apply time from the project item
    created by an AddToProjectPlan earlier in the same batch.
    """
    changes: list[ApplyChange] = []
    skipped: list[str] = []
    project_item_id = finding.project_item_id
    if project_item_id is None:
        if not (allow_pending_board_add and finding.content_id):
            skipped.append(f"{finding.repository}#{finding.number}: no project item")
            return ApplyPlan(changes=changes, skipped=skipped)
        project_item_id = ""

    field_name = _resolve_field_name(fields, request)
    value = request.value

    add_suggested_change(
        changes,
        skipped,
        finding.repository,
        finding.item_type,
        finding.number,
        project_item_id,
        {field.name: field for field in fields},
        field_name,
        value,
        finding.current_project_fields,
        replace_existing=replace_existing,
        content_id=finding.content_id,
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


# Confirmation-shaped messages only: "apply", "apply it", "yes, apply the changes",
# "ok apply it now". Anything else containing "apply" (questions, new requests) must
# fall through to the agent — the safe direction, since worst case is a re-preview.
_CONFIRM_APPLY = re.compile(
    r"(?:(?:ok|okay|yes|sure|please|go ahead)[,.! ]+)*"
    r"apply(?: it| them| that| this| all| the changes?| now| please)*[.!]*"
)


def should_apply_now(text: str, *, has_pending_writes: bool) -> bool:
    """True when the message should trigger applying queued GitHub writes.

    Only a whole-message confirmation counts — a sentence that merely contains
    "apply" ("does this rule apply to closed PRs?") never triggers writes.
    Without queued writes, just the exact `apply it` phrase short-circuits
    (to show the "nothing pending" hint).
    """
    normalized = " ".join(text.casefold().split())
    if not _wants_apply(normalized):
        return False
    return has_pending_writes or normalized == "apply it"


def _wants_scan(normalized: str) -> bool:
    if re.search(r"\b(rescan|rerun)\b", normalized) or "scan again" in normalized:
        return True
    return bool(
        re.search(r"\brun\b", normalized) and re.search(r"\b(scan|again|table)\b", normalized)
    )


def _wants_apply(normalized: str) -> bool:
    return _CONFIRM_APPLY.fullmatch(normalized) is not None


def _wants_explain(normalized: str) -> bool:
    return normalized.startswith(("explain", "why", "tell me about", "summarize"))
