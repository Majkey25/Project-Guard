from __future__ import annotations

from typing import Literal

import pytest

from github_audit.llm_evaluator import (
    ProjectAgentDeps,
    build_explain_prompt,
    build_nl_prompt,
    build_prompt,
    build_severity_prompt,
    build_triage_prompt,
    prepare_assignee_update,
    prepare_issue_comment,
    prepare_issue_edit,
    prepare_label_update,
    prepare_milestone_update,
    prepare_pr_merge,
    prepare_project_field_update,
    prepare_reviewer_request,
    prepare_state_update,
    read_selected_item,
)
from github_audit.models import (
    ApplyPlan,
    AssigneeUpdatePlan,
    AuditFinding,
    GitHubComment,
    IssueCommentPlan,
    IssueEditPlan,
    LabelUpdatePlan,
    MilestoneUpdatePlan,
    ProjectFieldDefinition,
    PullRequestMergePlan,
    ReviewerRequestPlan,
    StateUpdatePlan,
)

ItemType = Literal["issue", "pull_request"]


def _finding(
    repository: str = "OKsystem/repo",
    item_type: ItemType = "issue",
    number: int = 1,
    title: str = "Fix the bug",
    url: str = "https://github.com/OKsystem/repo/issues/1",
    assignees: list[str] | None = None,
    missing_fields: list[str] | None = None,
    development_status: str = "linked_pull_requests=0",
) -> AuditFinding:
    return AuditFinding(
        content_id="I_1",
        repository=repository,
        item_type=item_type,
        number=number,
        title=title,
        body="Full body text",
        comments=[
            GitHubComment(
                author="bob",
                body="Needs sizing.",
                url="https://github.com/OKsystem/repo/issues/1#issuecomment-1",
                updated_at="2026-07-01T08:00:00Z",
            )
        ],
        comments_total_count=1,
        url=url,
        assignees=assignees if assignees is not None else ["alice"],
        missing_fields=missing_fields if missing_fields is not None else ["Estimate", "Priority"],
        development_status=development_status,
        project_item_id="PVTI_1",
    )


def _fields() -> list[ProjectFieldDefinition]:
    return [
        ProjectFieldDefinition(
            id="f-priority",
            name="Priority",
            data_type="SINGLE_SELECT",
            kind="single_select",
            options={"P1": "opt-p1", "P2": "opt-p2"},
        )
    ]


class _Ctx:
    def __init__(self, deps: ProjectAgentDeps) -> None:
        self.deps = deps


def test_pydantic_ai_import_paths_exist() -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.azure import AzureProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    assert Agent
    assert OpenAIChatModel
    assert AzureProvider
    assert OpenAIProvider


# ── build_prompt ──────────────────────────────────────────────────────────────


def test_build_prompt_contains_missing_fields() -> None:
    finding = _finding()
    prompt = build_prompt(finding)
    assert "Missing fields: Estimate, Priority" in prompt


def test_read_selected_item_includes_body_and_field_options() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    text = read_selected_item(_Ctx(deps))  # type: ignore[arg-type]
    assert "Full body text" in text
    assert "Needs sizing." in text
    assert "Priority" in text
    assert "P1" in text


def _pending(deps: ProjectAgentDeps, cls: type) -> object:
    return next((w for w in deps.pending_writes if isinstance(w, cls)), None)


def test_prepare_project_field_update_queues_generic_field() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    result = prepare_project_field_update(_Ctx(deps), "priority", "P1")  # type: ignore[arg-type]
    assert "Queued" in result
    plan = _pending(deps, ApplyPlan)
    assert isinstance(plan, ApplyPlan)
    assert plan.changes[0].field_name == "Priority"
    assert plan.changes[0].option_id == "opt-p1"


def test_prepare_project_field_update_shows_before_after() -> None:
    finding = _finding()
    finding.current_project_fields = {"Priority": "P2"}
    deps = ProjectAgentDeps(finding=finding, project_id="PVT_1", fields=_fields())
    result = prepare_project_field_update(_Ctx(deps), "priority", "P1")  # type: ignore[arg-type]
    assert "'P2' -> P1" in result


def test_prepare_project_field_update_merges_across_calls() -> None:
    fields = [
        *_fields(),
        ProjectFieldDefinition(id="f-2", name="Status", data_type="TEXT", kind="field"),
    ]
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=fields)
    prepare_project_field_update(_Ctx(deps), "priority", "P1")  # type: ignore[arg-type]
    prepare_project_field_update(_Ctx(deps), "status", "In Progress")  # type: ignore[arg-type]
    plan = _pending(deps, ApplyPlan)
    assert isinstance(plan, ApplyPlan)
    assert {c.field_name for c in plan.changes} == {"Priority", "Status"}


def test_prepare_issue_comment_queues_comment() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    result = prepare_issue_comment(_Ctx(deps), " Looks good. ")  # type: ignore[arg-type]
    assert "Queued" in result
    plan = _pending(deps, IssueCommentPlan)
    assert isinstance(plan, IssueCommentPlan)
    assert plan.body == "Looks good."


def test_prepare_issue_edit_queues_title_and_body_separately() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    prepare_issue_edit(_Ctx(deps), title="New title")  # type: ignore[arg-type]
    prepare_issue_edit(_Ctx(deps), body="New body")  # type: ignore[arg-type]
    plan = _pending(deps, IssueEditPlan)
    assert isinstance(plan, IssueEditPlan)
    assert plan.title == "New title"
    assert plan.body == "New body"


def test_prepare_issue_edit_rejects_blank_title() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    result = prepare_issue_edit(_Ctx(deps), title="   ")  # type: ignore[arg-type]
    assert "blank" in result
    assert _pending(deps, IssueEditPlan) is None


def test_prepare_label_update_resolves_and_reports_unknown() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(),
        project_id="PVT_1",
        fields=_fields(),
        labels={"bug": "L_1", "docs": "L_2"},
    )
    result = prepare_label_update(_Ctx(deps), add=["Bug", "nonexistent"])  # type: ignore[arg-type]
    plan = _pending(deps, LabelUpdatePlan)
    assert isinstance(plan, LabelUpdatePlan)
    assert plan.add_label_ids == {"bug": "L_1"}
    assert "nonexistent" in result


def test_prepare_label_update_add_then_remove_same_label_is_move() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(), project_id="PVT_1", fields=_fields(), labels={"bug": "L_1"}
    )
    prepare_label_update(_Ctx(deps), add=["bug"])  # type: ignore[arg-type]
    prepare_label_update(_Ctx(deps), remove=["bug"])  # type: ignore[arg-type]
    plan = _pending(deps, LabelUpdatePlan)
    assert isinstance(plan, LabelUpdatePlan)
    assert plan.add_label_ids == {}
    assert plan.remove_label_ids == {"bug": "L_1"}


def test_prepare_assignee_update_queues_add_and_remove() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(),
        project_id="PVT_1",
        fields=_fields(),
        assignable_users={"alice": "U_1", "bob": "U_2"},
    )
    prepare_assignee_update(_Ctx(deps), add=["bob"], remove=["alice"])  # type: ignore[arg-type]
    plan = _pending(deps, AssigneeUpdatePlan)
    assert isinstance(plan, AssigneeUpdatePlan)
    assert plan.add_user_ids == {"bob": "U_2"}
    assert plan.remove_user_ids == {"alice": "U_1"}


def test_prepare_state_update_close_issue_with_reason() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    prepare_state_update(_Ctx(deps), "close", "COMPLETED")  # type: ignore[arg-type]
    plan = _pending(deps, StateUpdatePlan)
    assert isinstance(plan, StateUpdatePlan)
    assert plan.action == "close"
    assert plan.reason == "COMPLETED"


def test_prepare_state_update_drops_reason_for_pull_request() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(item_type="pull_request"), project_id="PVT_1", fields=_fields()
    )
    result = prepare_state_update(_Ctx(deps), "close", "COMPLETED")  # type: ignore[arg-type]
    plan = _pending(deps, StateUpdatePlan)
    assert isinstance(plan, StateUpdatePlan)
    assert plan.reason is None
    assert "only apply to issues" in result


def test_prepare_milestone_update_resolves_title() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(), project_id="PVT_1", fields=_fields(), milestones={"v1": "M_1"}
    )
    prepare_milestone_update(_Ctx(deps), "v1")  # type: ignore[arg-type]
    plan = _pending(deps, MilestoneUpdatePlan)
    assert isinstance(plan, MilestoneUpdatePlan)
    assert plan.milestone_id == "M_1"
    assert plan.milestone_title == "v1"


def test_prepare_milestone_update_clears_when_none() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(), project_id="PVT_1", fields=_fields(), milestones={"v1": "M_1"}
    )
    prepare_milestone_update(_Ctx(deps))  # type: ignore[arg-type]
    plan = _pending(deps, MilestoneUpdatePlan)
    assert isinstance(plan, MilestoneUpdatePlan)
    assert plan.milestone_id is None


def test_prepare_milestone_update_unknown_title_not_queued() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields(), milestones={})
    result = prepare_milestone_update(_Ctx(deps), "ghost")  # type: ignore[arg-type]
    assert "not found" in result
    assert _pending(deps, MilestoneUpdatePlan) is None


def test_prepare_pr_merge_rejects_issue() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(item_type="issue"), project_id="PVT_1", fields=_fields()
    )
    result = prepare_pr_merge(_Ctx(deps), "SQUASH")  # type: ignore[arg-type]
    assert "not a pull request" in result
    assert _pending(deps, PullRequestMergePlan) is None


def test_prepare_pr_merge_queues_for_pull_request() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(item_type="pull_request"), project_id="PVT_1", fields=_fields()
    )
    result = prepare_pr_merge(_Ctx(deps), "SQUASH")  # type: ignore[arg-type]
    plan = _pending(deps, PullRequestMergePlan)
    assert isinstance(plan, PullRequestMergePlan)
    assert plan.merge_method == "SQUASH"
    assert "not easily reversible" in result


def test_prepare_reviewer_request_rejects_issue() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(item_type="issue"), project_id="PVT_1", fields=_fields()
    )
    result = prepare_reviewer_request(_Ctx(deps), ["alice"])  # type: ignore[arg-type]
    assert "not a pull request" in result


def test_prepare_reviewer_request_queues_resolved_logins() -> None:
    deps = ProjectAgentDeps(
        finding=_finding(item_type="pull_request"),
        project_id="PVT_1",
        fields=_fields(),
        assignable_users={"alice": "U_1"},
    )
    prepare_reviewer_request(_Ctx(deps), ["alice"])  # type: ignore[arg-type]
    plan = _pending(deps, ReviewerRequestPlan)
    assert isinstance(plan, ReviewerRequestPlan)
    assert plan.user_ids == {"alice": "U_1"}


def test_read_selected_item_lists_labels_milestone_and_available_options() -> None:
    finding = _finding()
    finding.labels = ["bug"]
    finding.milestone = "v1"
    deps = ProjectAgentDeps(
        finding=finding,
        project_id="PVT_1",
        fields=_fields(),
        labels={"bug": "L_1", "docs": "L_2"},
        milestones={"v1": "M_1"},
        assignable_users={"alice": "U_1"},
    )
    text = read_selected_item(_Ctx(deps))  # type: ignore[arg-type]
    assert "Labels: bug" in text
    assert "Milestone: v1" in text
    assert "Available labels: bug, docs" in text
    assert "Available milestones: v1" in text
    assert "alice" in text


def test_build_prompt_contains_assignees() -> None:
    prompt = build_prompt(_finding(assignees=["bob", "carol"]))
    assert "bob, carol" in prompt


def test_build_prompt_no_assignees() -> None:
    prompt = build_prompt(_finding(assignees=[]))
    assert "none" in prompt


# ── build_triage_prompt ──────────────────────────────────────────────────────


def test_triage_prompt_shows_total() -> None:
    findings = [_finding(number=i) for i in range(5)]
    prompt = build_triage_prompt(findings)
    assert "Total findings: 5" in prompt


def test_triage_prompt_shows_field_frequency() -> None:
    findings = [
        _finding(missing_fields=["Estimate"]),
        _finding(missing_fields=["Estimate", "Priority"]),
    ]
    prompt = build_triage_prompt(findings)
    assert "Estimate: 2" in prompt


def test_triage_prompt_caps_sample_at_15() -> None:
    findings = [_finding(number=i, title=f"Issue {i}") for i in range(20)]
    prompt = build_triage_prompt(findings)
    assert "Issue 14" in prompt
    assert "Issue 15" not in prompt


# ── build_severity_prompt ────────────────────────────────────────────────────


def test_severity_prompt_numbers_findings() -> None:
    findings = [_finding(number=1), _finding(number=2)]
    prompt = build_severity_prompt(findings)
    assert "1." in prompt
    assert "2." in prompt


def test_severity_prompt_includes_repo() -> None:
    prompt = build_severity_prompt([_finding()])
    assert "repo" in prompt


# ── build_explain_prompt ─────────────────────────────────────────────────────


def test_explain_prompt_contains_rule() -> None:
    prompt = build_explain_prompt(_finding(), "Missing Estimate")
    assert "Missing Estimate" in prompt


def test_explain_prompt_contains_item_info() -> None:
    prompt = build_explain_prompt(_finding(title="My Issue"), "rule")
    assert "My Issue" in prompt


# ── build_nl_prompt ──────────────────────────────────────────────────────────


def test_nl_prompt_contains_query() -> None:
    prompt = build_nl_prompt("urgent PRs", ["my-repo"], ["alice"], ["Priority"])
    assert "urgent PRs" in prompt


def test_nl_prompt_lists_options() -> None:
    prompt = build_nl_prompt("q", ["repo-a", "repo-b"], ["user1"], ["Estimate"])
    assert "repo-a, repo-b" in prompt
    assert "user1" in prompt
    assert "Estimate" in prompt


# ── config security validators ────────────────────────────────────────────────


def test_invalid_assignee_name_rejected() -> None:
    from pydantic import ValidationError

    from github_audit.config import Settings

    with pytest.raises(ValidationError, match="Invalid GitHub username"):
        Settings.model_validate(
            {
                "github_token": "tok",
                "github_org": "org",
                "github_project_numbers_raw": "1",
                "github_include_all_repositories": True,
                "require_target_assignee": False,
                "target_assignees_raw": "alice OR is:public",
            }
        )


def test_valid_assignee_names_accepted() -> None:
    from github_audit.config import Settings

    s = Settings.model_validate(
        {
            "github_token": "tok",
            "github_org": "org",
            "github_project_numbers_raw": "1",
            "github_include_all_repositories": True,
            "require_target_assignee": False,
            "target_assignees_raw": "alice,bob-smith,carol123",
        }
    )
    assert s.target_assignees == ["alice", "bob-smith", "carol123"]


def test_assignee_limit_enforced() -> None:
    from pydantic import ValidationError

    from github_audit.config import Settings

    too_many = ",".join(f"user{i}" for i in range(51))
    with pytest.raises(ValidationError, match="maximum 50"):
        Settings.model_validate(
            {
                "github_token": "tok",
                "github_org": "org",
                "github_project_numbers_raw": "1",
                "github_include_all_repositories": True,
                "require_target_assignee": False,
                "target_assignees_raw": too_many,
            }
        )


def test_invalid_repo_name_rejected() -> None:
    from pydantic import ValidationError

    from github_audit.config import Settings

    with pytest.raises(ValidationError, match="Invalid repository name"):
        Settings.model_validate(
            {
                "github_token": "tok",
                "github_org": "org",
                "github_project_numbers_raw": "1",
                "github_include_all_repositories": True,
                "require_target_assignee": False,
                "github_repository_allowlist_raw": "my-repo is:public",
            }
        )


def test_llm_api_key_stripped() -> None:
    from github_audit.config import Settings

    s = Settings.model_validate(
        {
            "github_token": "tok",
            "github_org": "org",
            "github_project_numbers_raw": "1",
            "github_include_all_repositories": True,
            "require_target_assignee": False,
            "llm_api_key": "  sk-abc  ",
        }
    )
    assert s.llm_api_key == "sk-abc"
