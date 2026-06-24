from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from github_audit.config import Settings
from github_audit.llm_evaluator import suggest_for_finding
from github_audit.models import AuditFinding, LLMSuggestion


def _settings(provider: str = "openai") -> Settings:
    return Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "llm_provider": provider,
            "llm_base_url": "",
            "llm_api_version": "",
            "llm_api_key": "test-key",
            "llm_model_name": "gpt-4",
        }
    )


def _finding() -> AuditFinding:
    return AuditFinding(
        repository="OKsystem/repo",
        item_type="issue",
        number=1,
        title="Fix the thing",
        url="https://github.com/OKsystem/repo/issues/1",
        assignees=["alice"],
        missing_fields=["Estimate", "Priority"],
        development_status="linked_pull_requests=0",
    )


def _mock_agent_returning(suggestion: LLMSuggestion) -> MagicMock:
    agent_instance = MagicMock()
    run_result = MagicMock()
    run_result.output = suggestion
    agent_instance.run_sync.return_value = run_result
    return agent_instance


# ── openai provider ───────────────────────────────────────────────────────────


def test_suggest_openai_returns_llm_suggestion() -> None:
    expected = LLMSuggestion(
        estimated_points=3,
        priority="P2",
        confidence=0.85,
        rationale="Moderate complexity task",
        should_auto_apply=True,
    )
    with patch("pydantic_ai.Agent", return_value=_mock_agent_returning(expected)):
        result = suggest_for_finding(_finding(), _settings("openai"))

    assert result.estimated_points == 3
    assert result.priority == "P2"
    assert result.confidence == 0.85


def test_suggest_openai_compatible_provider() -> None:
    expected = LLMSuggestion(confidence=0.7, rationale="ok", should_auto_apply=False)
    with patch("pydantic_ai.Agent", return_value=_mock_agent_returning(expected)):
        result = suggest_for_finding(_finding(), _settings("openai-compatible"))

    assert result.rationale == "ok"


# ── azure provider ────────────────────────────────────────────────────────────


def test_suggest_azure_provider() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "llm_provider": "azure",
            "llm_base_url": "https://my.openai.azure.com/",
            "llm_api_version": "2024-02-01",
            "llm_api_key": "azure-key",
            "llm_model_name": "gpt-4o",
        }
    )
    expected = LLMSuggestion(confidence=0.9, rationale="azure ok", should_auto_apply=True)
    with patch("pydantic_ai.Agent", return_value=_mock_agent_returning(expected)):
        result = suggest_for_finding(_finding(), settings)

    assert result.rationale == "azure ok"


# ── validation errors ─────────────────────────────────────────────────────────


def test_suggest_raises_when_llm_disabled() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "llm_enabled": False,
            "llm_provider": "openai",
            "llm_base_url": "",
            "llm_api_version": "",
            "llm_api_key": "key",
            "llm_model_name": "gpt-4",
        }
    )
    with pytest.raises(ValueError, match="LLM_ENABLED=false"):
        suggest_for_finding(_finding(), settings)


def test_suggest_raises_on_unknown_provider() -> None:
    settings = Settings.model_validate(
        {
            "github_token": "token",
            "github_project_number": 1,
            "github_project_numbers_raw": "",
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "llm_provider": "anthropic",
            "llm_base_url": "",
            "llm_api_version": "",
            "llm_api_key": "key",
            "llm_model_name": "claude-3",
        }
    )
    with pytest.raises(ValueError, match="unsupported LLM_PROVIDER"):
        suggest_for_finding(_finding(), settings)


# ── agent is called with the right prompt ─────────────────────────────────────


def test_suggest_passes_finding_details_to_agent() -> None:
    expected = LLMSuggestion(confidence=0.8, rationale="r", should_auto_apply=False)
    agent_instance = _mock_agent_returning(expected)
    with patch("pydantic_ai.Agent", return_value=agent_instance):
        suggest_for_finding(_finding(), _settings())

    prompt = agent_instance.run_sync.call_args[0][0]
    assert "Fix the thing" in prompt
    assert "Estimate" in prompt
    assert "Priority" in prompt
