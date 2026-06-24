from __future__ import annotations

from github_audit.llm_evaluator import build_prompt
from github_audit.models import AuditFinding


def test_pydantic_ai_import_paths_exist() -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.azure import AzureProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    assert Agent
    assert OpenAIChatModel
    assert AzureProvider
    assert OpenAIProvider


def test_build_prompt_contains_missing_fields() -> None:
    finding = AuditFinding(
        repository="OKsystem/repo",
        item_type="issue",
        number=1,
        title="Title",
        url="https://github.com/OKsystem/repo/issues/1",
        assignees=["alice"],
        missing_fields=["Estimate"],
        development_status="linked_pull_requests=0",
    )
    assert "Missing fields: Estimate" in build_prompt(finding)
