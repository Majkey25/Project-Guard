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
    prepare_issue_comment,
    prepare_project_field_update,
    read_selected_item,
)
from github_audit.models import AuditFinding, GitHubComment, ProjectFieldDefinition

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


def test_prepare_project_field_update_queues_generic_field() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    result = prepare_project_field_update(_Ctx(deps), "priority", "P1")  # type: ignore[arg-type]
    assert "Queued" in result
    assert deps.project_plan.changes[0].field_name == "Priority"
    assert deps.project_plan.changes[0].option_id == "opt-p1"


def test_prepare_issue_comment_queues_comment() -> None:
    deps = ProjectAgentDeps(finding=_finding(), project_id="PVT_1", fields=_fields())
    result = prepare_issue_comment(_Ctx(deps), " Looks good. ")  # type: ignore[arg-type]
    assert "Queued" in result
    assert deps.comment_plan is not None
    assert deps.comment_plan.body == "Looks good."


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
