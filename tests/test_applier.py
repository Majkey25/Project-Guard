from __future__ import annotations

from unittest.mock import MagicMock

from github_audit.applier import (
    PartialApplyError,
    add_comment,
    add_suggested_change,
    apply_assignee_update,
    apply_issue_edit,
    apply_label_update,
    apply_milestone_update,
    apply_pending_write,
    apply_plan,
    apply_pr_merge,
    apply_reviewer_request,
    apply_state_update,
    build_apply_plan,
    build_update_value,
    describe_changes,
    describe_pending_write,
)
from github_audit.config import Settings
from github_audit.github_client import GitHubError
from github_audit.models import (
    ApplyChange,
    ApplyPlan,
    AssigneeUpdatePlan,
    AuditFinding,
    AuditResult,
    IssueCommentPlan,
    IssueEditPlan,
    LabelUpdatePlan,
    LLMSuggestion,
    MilestoneUpdatePlan,
    ProjectFieldDefinition,
    PullRequestMergePlan,
    ReviewerRequestPlan,
    StateUpdatePlan,
)


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "required_project_fields_raw": "Estimate,Priority",
            "auto_apply_min_confidence": 0.85,
            "require_development_link": False,
            "require_linked_pr_or_branch": False,
        }
    )


def _finding(
    *,
    project_item_id: str | None = "item-1",
    suggestion: LLMSuggestion | None = None,
    current: dict[str, str] | None = None,
) -> AuditFinding:
    return AuditFinding(
        repository="org/repo",
        item_type="issue",
        number=1,
        title="Issue",
        url="https://github.com/org/repo/issues/1",
        assignees=["alice"],
        missing_fields=["Estimate", "Priority"],
        development_status="linked_pull_requests=0",
        project_item_id=project_item_id,
        llm_suggestion=suggestion,
        current_project_fields=current or {},
    )


def _fields() -> list[ProjectFieldDefinition]:
    return [
        ProjectFieldDefinition(
            id="f-estimate",
            name="Estimate",
            data_type="NUMBER",
            kind="field",
        ),
        ProjectFieldDefinition(
            id="f-priority",
            name="Priority",
            data_type="SINGLE_SELECT",
            kind="single_select",
            options={"P1": "opt-p1", "P2": "opt-p2"},
        ),
        ProjectFieldDefinition(
            id="f-iteration",
            name="Iteration (sprint)",
            data_type="ITERATION",
            kind="iteration",
            iterations={"Sprint 1": "iter-1"},
        ),
        ProjectFieldDefinition(
            id="f-start",
            name="Start date",
            data_type="DATE",
            kind="field",
        ),
    ]


def _audit(*findings: AuditFinding) -> AuditResult:
    return AuditResult(
        organization="org",
        repositories=["org/repo"],
        findings=list(findings),
        scanned_issue_count=1,
        scanned_pull_request_count=0,
    )


# ── build_apply_plan ────────────────────────────────────────────────────────


def test_build_apply_plan_skips_without_suggestion() -> None:
    plan = build_apply_plan(_audit(_finding()), _fields(), _settings())
    assert not plan.changes
    assert any("no suggestion" in s for s in plan.skipped)


def test_build_apply_plan_skips_without_project_item() -> None:
    suggestion = LLMSuggestion(
        estimated_points=3,
        confidence=0.9,
        rationale="ok",
        should_auto_apply=True,
    )
    plan = build_apply_plan(
        _audit(_finding(project_item_id=None, suggestion=suggestion)),
        _fields(),
        _settings(),
    )
    assert not plan.changes
    assert any("no suggestion or project item" in s for s in plan.skipped)


def test_build_apply_plan_skips_low_confidence() -> None:
    suggestion = LLMSuggestion(
        estimated_points=3,
        confidence=0.5,
        rationale="uncertain",
        should_auto_apply=True,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    assert not plan.changes
    assert any("confidence too low" in s for s in plan.skipped)


def test_build_apply_plan_skips_should_not_auto_apply() -> None:
    suggestion = LLMSuggestion(
        estimated_points=3,
        confidence=0.95,
        rationale="manual",
        should_auto_apply=False,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    assert not plan.changes


def test_build_apply_plan_adds_number_change() -> None:
    suggestion = LLMSuggestion(
        estimated_points=5,
        confidence=0.95,
        rationale="clear",
        should_auto_apply=True,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    estimate_changes = [c for c in plan.changes if c.field_name == "Estimate"]
    assert len(estimate_changes) == 1
    assert estimate_changes[0].value == 5
    assert estimate_changes[0].option_id is None
    assert estimate_changes[0].iteration_id is None


def test_build_apply_plan_adds_single_select_change() -> None:
    suggestion = LLMSuggestion(
        priority="P1",
        confidence=0.95,
        rationale="clear",
        should_auto_apply=True,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    priority_changes = [c for c in plan.changes if c.field_name == "Priority"]
    assert len(priority_changes) == 1
    assert priority_changes[0].option_id == "opt-p1"


def test_build_apply_plan_skips_unknown_option() -> None:
    suggestion = LLMSuggestion(
        priority="P99",
        confidence=0.95,
        rationale="clear",
        should_auto_apply=True,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    assert not any(c.field_name == "Priority" for c in plan.changes)
    assert any("P99" in s for s in plan.skipped)


def test_build_apply_plan_skips_already_set_field() -> None:
    suggestion = LLMSuggestion(
        estimated_points=3,
        confidence=0.95,
        rationale="clear",
        should_auto_apply=True,
    )
    plan = build_apply_plan(
        _audit(_finding(suggestion=suggestion, current={"Estimate": "3"})),
        _fields(),
        _settings(),
    )
    assert not any(c.field_name == "Estimate" for c in plan.changes)
    assert any("Estimate already set" in s for s in plan.skipped)


def test_build_apply_plan_adds_iteration_change() -> None:
    suggestion = LLMSuggestion(
        suggested_iteration="Sprint 1",
        confidence=0.95,
        rationale="clear",
        should_auto_apply=True,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    iter_changes = [c for c in plan.changes if c.field_name == "Iteration (sprint)"]
    assert len(iter_changes) == 1
    assert iter_changes[0].iteration_id == "iter-1"


def test_build_apply_plan_skips_unknown_iteration() -> None:
    suggestion = LLMSuggestion(
        suggested_iteration="Sprint 99",
        confidence=0.95,
        rationale="clear",
        should_auto_apply=True,
    )
    plan = build_apply_plan(_audit(_finding(suggestion=suggestion)), _fields(), _settings())
    assert not any(c.field_name == "Iteration (sprint)" for c in plan.changes)


# ── build_update_value ───────────────────────────────────────────────────────


def _change(**kwargs: object) -> ApplyChange:
    defaults: dict[str, object] = {
        "repository": "org/repo",
        "item_type": "issue",
        "number": 1,
        "project_item_id": "item-1",
        "field_name": "Estimate",
        "value": 5,
        "option_id": None,
        "iteration_id": None,
    }
    defaults.update(kwargs)
    return ApplyChange.model_validate(defaults)


def test_build_update_value_number() -> None:
    assert build_update_value(_change(value=5)) == {"number": 5}


def test_build_update_value_single_select() -> None:
    assert build_update_value(_change(option_id="opt-1")) == {"singleSelectOptionId": "opt-1"}


def test_build_update_value_iteration() -> None:
    assert build_update_value(_change(iteration_id="iter-1")) == {"iterationId": "iter-1"}


def test_build_update_value_text() -> None:
    assert build_update_value(_change(value="note text")) == {"text": "note text"}


def test_build_update_value_date() -> None:
    field = next(field for field in _fields() if field.name == "Start date")
    assert build_update_value(_change(field_name="Start date", value="2026-07-01"), field) == {
        "date": "2026-07-01"
    }


def test_add_suggested_change_replaces_existing_and_coerces_number() -> None:
    changes: list[ApplyChange] = []
    skipped: list[str] = []
    add_suggested_change(
        changes,
        skipped,
        "org/repo",
        "issue",
        1,
        "item-1",
        {field.name: field for field in _fields()},
        "Estimate",
        "8",
        {"Estimate": "3"},
        replace_existing=True,
    )
    assert not skipped
    assert changes[0].value == 8


def test_add_suggested_change_rejects_bad_date() -> None:
    changes: list[ApplyChange] = []
    skipped: list[str] = []
    add_suggested_change(
        changes,
        skipped,
        "org/repo",
        "issue",
        1,
        "item-1",
        {field.name: field for field in _fields()},
        "Start date",
        "tomorrow",
        {},
        replace_existing=True,
    )
    assert not changes
    assert any("YYYY-MM-DD" in item for item in skipped)


# ── describe_changes ─────────────────────────────────────────────────────────


def test_describe_changes_formats_correctly() -> None:
    result = describe_changes([_change(value=3)])
    assert len(result) == 1
    assert "org/repo#1" in result[0]
    assert "Estimate=3" in result[0]
    assert "dry-run" in result[0]


# ── apply_plan dry run ───────────────────────────────────────────────────────


def test_apply_plan_dry_run_no_writes() -> None:
    change = _change()
    plan = ApplyPlan(changes=[change], skipped=["something"])
    result = apply_plan(
        None,  # type: ignore[arg-type]
        plan,
        project_id="proj-1",
        fields=_fields(),
        dry_run=True,
        allow_write=False,
    )
    assert result.dry_run is True
    assert result.applied == []
    assert any("org/repo#1" in s for s in result.skipped)


def test_apply_plan_dry_run_true_even_if_allow_write() -> None:
    plan = ApplyPlan(changes=[_change()])
    result = apply_plan(
        None,  # type: ignore[arg-type]
        plan,
        project_id="proj-1",
        fields=_fields(),
        dry_run=True,
        allow_write=True,
    )
    assert result.dry_run is True
    assert result.applied == []


def test_add_comment_sends_graphql_mutation() -> None:
    client = MagicMock()
    add_comment(client, "I_1", " hello ")  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"subjectId": "I_1", "body": "hello"}}


# ── apply_plan partial failure ──────────────────────────────────────────────


def test_apply_plan_raises_partial_apply_error_and_keeps_already_applied() -> None:
    client = MagicMock()
    client.graphql.side_effect = [None, GitHubError("boom")]
    plan = ApplyPlan(
        changes=[_change(field_name="Estimate", value=3), _change(field_name="Priority")]
    )
    try:
        apply_plan(
            client, plan, project_id="proj-1", fields=_fields(), dry_run=False, allow_write=True
        )
    except PartialApplyError as exc:
        assert len(exc.applied) == 1
        assert exc.applied[0].field_name == "Estimate"
    else:
        raise AssertionError("expected PartialApplyError")


# ── new mutation builders ────────────────────────────────────────────────────


def test_apply_issue_edit_sends_update_issue_for_issue() -> None:
    client = MagicMock()
    plan = IssueEditPlan(
        content_id="I_1", repository="org/repo", item_type="issue", number=1, title="New"
    )
    apply_issue_edit(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"id": "I_1", "title": "New"}}


def test_apply_issue_edit_sends_update_pr_for_pr() -> None:
    client = MagicMock()
    plan = IssueEditPlan(
        content_id="PR_1",
        repository="org/repo",
        item_type="pull_request",
        number=1,
        body="New body",
    )
    apply_issue_edit(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"pullRequestId": "PR_1", "body": "New body"}}


def test_apply_label_update_sends_add_and_remove() -> None:
    client = MagicMock()
    plan = LabelUpdatePlan(
        content_id="I_1",
        repository="org/repo",
        item_type="issue",
        number=1,
        add_label_ids={"bug": "L_1"},
        remove_label_ids={"docs": "L_2"},
    )
    apply_label_update(client, plan)  # type: ignore[arg-type]
    assert client.graphql.call_count == 2
    add_call, remove_call = client.graphql.call_args_list
    assert add_call.args[1] == {"input": {"labelableId": "I_1", "labelIds": ["L_1"]}}
    assert remove_call.args[1] == {"input": {"labelableId": "I_1", "labelIds": ["L_2"]}}


def test_apply_assignee_update_sends_add_only_when_no_removals() -> None:
    client = MagicMock()
    plan = AssigneeUpdatePlan(
        content_id="I_1",
        repository="org/repo",
        item_type="issue",
        number=1,
        add_user_ids={"alice": "U_1"},
    )
    apply_assignee_update(client, plan)  # type: ignore[arg-type]
    client.graphql.assert_called_once()
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"assignableId": "I_1", "assigneeIds": ["U_1"]}}


def test_apply_state_update_close_issue_with_reason() -> None:
    client = MagicMock()
    plan = StateUpdatePlan(
        content_id="I_1",
        repository="org/repo",
        item_type="issue",
        number=1,
        action="close",
        reason="COMPLETED",
    )
    apply_state_update(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"issueId": "I_1", "stateReason": "COMPLETED"}}


def test_apply_state_update_reopen_pr_has_no_reason() -> None:
    client = MagicMock()
    plan = StateUpdatePlan(
        content_id="PR_1",
        repository="org/repo",
        item_type="pull_request",
        number=1,
        action="reopen",
    )
    apply_state_update(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"pullRequestId": "PR_1"}}


def test_apply_milestone_update_sends_explicit_null_to_clear() -> None:
    client = MagicMock()
    plan = MilestoneUpdatePlan(content_id="I_1", repository="org/repo", item_type="issue", number=1)
    apply_milestone_update(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"id": "I_1", "milestoneId": None}}
    assert "milestoneId" in variables["input"]  # explicit key, not omitted


def test_apply_pr_merge_sends_merge_method() -> None:
    client = MagicMock()
    plan = PullRequestMergePlan(
        content_id="PR_1", repository="org/repo", number=1, merge_method="SQUASH"
    )
    apply_pr_merge(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"pullRequestId": "PR_1", "mergeMethod": "SQUASH"}}


def test_apply_reviewer_request_uses_union_true() -> None:
    client = MagicMock()
    plan = ReviewerRequestPlan(
        content_id="PR_1", repository="org/repo", number=1, user_ids={"bob": "U_2"}
    )
    apply_reviewer_request(client, plan)  # type: ignore[arg-type]
    _, variables = client.graphql.call_args.args
    assert variables == {"input": {"pullRequestId": "PR_1", "userIds": ["U_2"], "union": True}}


# ── apply_pending_write dispatch ─────────────────────────────────────────────


def test_apply_pending_write_dispatches_project_field_update() -> None:
    client = MagicMock()
    plan = ApplyPlan(changes=[_change()])
    apply_pending_write(client, plan, project_id="proj-1", fields=_fields())  # type: ignore[arg-type]
    client.graphql.assert_called_once()


def test_apply_pending_write_requires_project_context_for_field_update() -> None:
    client = MagicMock()
    plan = ApplyPlan(changes=[_change()])
    try:
        apply_pending_write(client, plan)  # type: ignore[arg-type]
    except ValueError as exc:
        assert "project id" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_apply_pending_write_dispatches_comment() -> None:
    client = MagicMock()
    plan = IssueCommentPlan(
        subject_id="I_1", repository="org/repo", item_type="issue", number=1, body="hi"
    )
    apply_pending_write(client, plan)  # type: ignore[arg-type]
    client.graphql.assert_called_once()


# ── describe_pending_write ───────────────────────────────────────────────────


def test_describe_pending_write_state_update_includes_reason() -> None:
    plan = StateUpdatePlan(
        content_id="I_1",
        repository="org/repo",
        item_type="issue",
        number=1,
        action="close",
        reason="DUPLICATE",
    )
    lines = describe_pending_write(plan)
    assert len(lines) == 1
    assert "org/repo#1" in lines[0]
    assert "close" in lines[0]
    assert "DUPLICATE" in lines[0]


def test_describe_pending_write_comment_includes_exact_body() -> None:
    plan = IssueCommentPlan(
        subject_id="I_1",
        repository="org/repo",
        item_type="issue",
        number=1,
        body="first line\nsecond line",
    )
    lines = describe_pending_write(plan)
    assert lines == [
        "dry-run: org/repo#1 add comment",
        "  body -> 'first line\\nsecond line'",
    ]


def test_describe_pending_write_edit_includes_exact_body() -> None:
    plan = IssueEditPlan(
        content_id="I_1",
        repository="org/repo",
        item_type="issue",
        number=1,
        title="New title",
        body="Full replacement body",
    )
    lines = describe_pending_write(plan)
    assert "  title -> 'New title'" in lines
    assert "  body -> 'Full replacement body'" in lines


def test_describe_pending_write_merge_warns_not_reversible() -> None:
    plan = PullRequestMergePlan(
        content_id="PR_1", repository="org/repo", number=1, merge_method="MERGE"
    )
    lines = describe_pending_write(plan)
    assert "not easily reversible" in lines[0]


def test_describe_pending_write_milestone_clear() -> None:
    plan = MilestoneUpdatePlan(content_id="I_1", repository="org/repo", item_type="issue", number=1)
    lines = describe_pending_write(plan)
    assert "(clear)" in lines[0]
