from __future__ import annotations

from github_audit.applier import (
    apply_plan,
    build_apply_plan,
    build_update_value,
    describe_changes,
)
from github_audit.config import Settings
from github_audit.models import (
    ApplyChange,
    ApplyPlan,
    AuditFinding,
    AuditResult,
    LLMSuggestion,
    ProjectFieldDefinition,
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
