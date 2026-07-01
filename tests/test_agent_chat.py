from __future__ import annotations

from github_audit.agent_chat import FieldRequest, build_field_plan, parse_agent_command
from github_audit.models import AuditFinding, ProjectFieldDefinition


def _finding(project_item_id: str | None = "item-1") -> AuditFinding:
    return AuditFinding(
        repository="org/repo",
        item_type="issue",
        number=7,
        title="Issue",
        url="https://github.com/org/repo/issues/7",
        assignees=[],
        missing_fields=["Estimate", "Iteration (sprint)"],
        development_status="linked_pull_requests=0",
        project_item_id=project_item_id,
    )


def _fields() -> list[ProjectFieldDefinition]:
    return [
        ProjectFieldDefinition(
            id="estimate-id",
            name="Estimate",
            data_type="NUMBER",
            kind="field",
        ),
        ProjectFieldDefinition(
            id="iteration-id",
            name="Iteration (sprint)",
            data_type="ITERATION",
            kind="iteration",
            iterations={"Sprint B": "iter-b", "Sprint A": "iter-a"},
        ),
    ]


def test_parse_controls_closed_issues_and_pr_only() -> None:
    command = parse_agent_command("run the table again with closed issues and only PRs")
    assert command.run_scan is True
    assert {update.name: update.value for update in command.control_updates} == {
        "include_issues": False,
        "include_closed_issues": True,
        "include_pull_requests": True,
    }


def test_parse_does_not_hardcode_field_updates() -> None:
    command = parse_agent_command("complete the estimate space with value 20")
    assert not command.control_updates
    assert command.run_scan is False
    assert command.apply_pending is False
    assert command.explain is False


def test_build_estimate_plan() -> None:
    plan = build_field_plan(_finding(), _fields(), FieldRequest("Estimate", 20))
    assert not plan.skipped
    assert len(plan.changes) == 1
    assert plan.changes[0].field_name == "Estimate"
    assert plan.changes[0].value == 20


def test_build_iteration_plan_uses_named_iteration() -> None:
    plan = build_field_plan(
        _finding(),
        _fields(),
        FieldRequest("Iteration (sprint)", "Sprint A"),
    )
    assert not plan.skipped
    assert len(plan.changes) == 1
    assert plan.changes[0].iteration_id == "iter-a"


def test_build_field_plan_can_replace_existing_value() -> None:
    finding = _finding()
    finding.current_project_fields["Estimate"] = "3"
    plan = build_field_plan(
        finding,
        _fields(),
        FieldRequest("estimate", "8"),
        replace_existing=True,
    )
    assert not plan.skipped
    assert len(plan.changes) == 1
    assert plan.changes[0].field_name == "Estimate"
    assert plan.changes[0].value == 8


def test_build_field_plan_skips_without_project_item() -> None:
    plan = build_field_plan(_finding(project_item_id=None), _fields(), FieldRequest("Estimate", 20))
    assert not plan.changes
    assert any("no project item" in item for item in plan.skipped)
