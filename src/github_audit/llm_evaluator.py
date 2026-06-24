from __future__ import annotations

from github_audit.config import Settings
from github_audit.models import AuditFinding, LLMSuggestion

INSTRUCTIONS = """
Suggest missing GitHub workflow metadata. Do not claim a field is present or missing.
Use only the supplied issue or pull request data. Return conservative values.
"""


def suggest_for_finding(finding: AuditFinding, settings: Settings) -> LLMSuggestion:
    settings.validate_llm()
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.azure import AzureProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    provider_name = settings.llm_provider_name
    if provider_name == "azure":
        provider = AzureProvider(
            azure_endpoint=settings.llm_base_url,
            api_key=settings.llm_api_key,
            api_version=settings.llm_api_version or None,
        )
    elif provider_name in {"openai", "openai-compatible"}:
        provider = OpenAIProvider(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
        )
    else:
        msg = (
            f"unsupported LLM_PROVIDER={settings.llm_provider!r}; "
            "supported: azure, openai, openai-compatible"
        )
        raise ValueError(msg)
    model = OpenAIChatModel(settings.llm_model_name, provider=provider)
    agent = Agent(model, output_type=LLMSuggestion, instructions=INSTRUCTIONS)
    result = agent.run_sync(build_prompt(finding))
    return result.output


def build_prompt(finding: AuditFinding) -> str:
    return "\n".join(
        [
            f"Repository: {finding.repository}",
            f"Item: {finding.item_type} #{finding.number}",
            f"Title: {finding.title}",
            f"URL: {finding.url}",
            f"Assignees: {', '.join(finding.assignees) or 'none'}",
            f"Missing fields: {', '.join(finding.missing_fields)}",
            f"Current project fields: {finding.current_project_fields}",
            f"Development status: {finding.development_status}",
        ]
    )
