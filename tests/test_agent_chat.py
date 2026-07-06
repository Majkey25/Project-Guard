from __future__ import annotations

from github_audit.agent_chat import (
    FieldRequest,
    build_field_plan,
    parse_agent_command,
    should_apply_now,
)
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
    assert command.explain is False


def test_should_apply_now_requires_exact_phrase() -> None:
    assert should_apply_now("apply it") is True
    for text in (
        "apply",
        "Apply it!",
        "apply the changes",
        "yes, apply the changes",
        "do it",
        "confirm",
        "set estimate to 5",
        "now apply the bug label",
        "apply estimate 5 too",
        "apply a comment about the delay",
        "does this rule apply to closed PRs?",
        "can I apply a filter to the table?",
        "how do I apply for access?",
        "apply sprint 3 as well",
        "apply the same change to #124",
        "ok apply it now",
    ):
        assert should_apply_now(text) is False


def test_parse_scan_ignores_run_inside_words_and_field_requests() -> None:
    assert parse_agent_command("set estimate to 5 and add label trunk").run_scan is False
    assert parse_agent_command("prune the backlog description").run_scan is False
    assert parse_agent_command("rerun the scan").run_scan is True
    assert parse_agent_command("run scan").run_scan is True
    assert parse_agent_command("rescan").run_scan is True
    assert parse_agent_command("scan again").run_scan is True


def test_parse_explain_only_when_prompt_leads_with_it() -> None:
    assert parse_agent_command("explain").explain is True
    assert parse_agent_command("why is this missing an estimate?").explain is True
    assert parse_agent_command("summarize the findings").explain is True
    assert parse_agent_command("set estimate to 5 and explain why").explain is False
    assert parse_agent_command("add a comment explaining the delay").explain is False


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


def test_build_field_plan_can_queue_before_board_add() -> None:
    finding = _finding(project_item_id=None)
    finding.content_id = "I_7"
    plan = build_field_plan(
        finding,
        _fields(),
        FieldRequest("Estimate", 10),
        replace_existing=True,
        allow_pending_board_add=True,
    )
    assert not plan.skipped
    assert len(plan.changes) == 1
    assert plan.changes[0].project_item_id == ""
    assert plan.changes[0].content_id == "I_7"


def test_build_field_plan_still_skips_without_board_add_permission() -> None:
    finding = _finding(project_item_id=None)
    finding.content_id = "I_7"
    plan = build_field_plan(finding, _fields(), FieldRequest("Estimate", 10))
    assert not plan.changes
    assert any("no project item" in item for item in plan.skipped)
