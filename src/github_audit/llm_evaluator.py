from __future__ import annotations

from collections import Counter

from github_audit.config import Settings
from github_audit.models import (
    AuditFinding,
    BatchTriageResult,
    LLMSuggestion,
    NLFilterResult,
    RuleExplanation,
    SeverityScore,
    SeverityScoreList,
)

_SUGGEST_INSTRUCTIONS = """
Suggest missing GitHub workflow metadata. Do not claim a field is present or missing.
Use only the supplied issue or pull request data. Return conservative values.
"""

_TRIAGE_INSTRUCTIONS = """
You are a GitHub project health analyst. Analyze audit findings and identify systemic patterns.
Focus on root causes, not individual items. Be concise and actionable.
"""

_SEVERITY_INSTRUCTIONS = """
Score the urgency of each audit finding. HIGH = blocks delivery or critically violates process.
MEDIUM = needs attention soon. LOW = nice to fix but not urgent.
Base scoring on item age, type, and which fields are missing.
Return exactly one score per finding in the same order as given.
"""

_EXPLAIN_INSTRUCTIONS = """
Explain why a specific audit finding matters for this particular item.
Be concrete and specific — reference the item title, assignees, and missing fields.
Keep each field to 1-2 sentences.
"""

_NL_FILTER_INSTRUCTIONS = """
Parse a natural language search query into structured filter criteria for a GitHub audit tool.
Only use values from the available options provided. Return empty lists if nothing matches.
"""


def _make_agent(settings: Settings, output_type, instructions: str):
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
    return Agent(model, output_type=output_type, instructions=instructions)


def suggest_for_finding(finding: AuditFinding, settings: Settings) -> LLMSuggestion:
    agent = _make_agent(settings, LLMSuggestion, _SUGGEST_INSTRUCTIONS)
    return agent.run_sync(build_prompt(finding)).output


def batch_triage(findings: list[AuditFinding], settings: Settings) -> BatchTriageResult:
    agent = _make_agent(settings, BatchTriageResult, _TRIAGE_INSTRUCTIONS)
    return agent.run_sync(build_triage_prompt(findings)).output


def score_severities(findings: list[AuditFinding], settings: Settings) -> list[SeverityScore]:
    agent = _make_agent(settings, SeverityScoreList, _SEVERITY_INSTRUCTIONS)
    return agent.run_sync(build_severity_prompt(findings)).output.scores


def explain_finding(finding: AuditFinding, rule: str, settings: Settings) -> RuleExplanation:
    agent = _make_agent(settings, RuleExplanation, _EXPLAIN_INSTRUCTIONS)
    return agent.run_sync(build_explain_prompt(finding, rule)).output


def nl_to_filters(
    query: str,
    available_repos: list[str],
    available_assignees: list[str],
    available_fields: list[str],
    settings: Settings,
) -> NLFilterResult:
    agent = _make_agent(settings, NLFilterResult, _NL_FILTER_INSTRUCTIONS)
    return agent.run_sync(
        build_nl_prompt(query, available_repos, available_assignees, available_fields)
    ).output


# ── prompt builders ───────────────────────────────────────────────────────────

def build_prompt(finding: AuditFinding) -> str:
    return "\n".join([
        f"Repository: {finding.repository}",
        f"Item: {finding.item_type} #{finding.number}",
        f"Title: <title>{finding.title}</title>",
        f"URL: {finding.url}",
        f"Assignees: {', '.join(finding.assignees) or 'none'}",
        f"Missing fields: {', '.join(finding.missing_fields)}",
        f"Current project fields: {finding.current_project_fields}",
        f"Development status: {finding.development_status}",
    ])


def build_triage_prompt(findings: list[AuditFinding]) -> str:
    missing_counts: Counter[str] = Counter()
    repo_counts: Counter[str] = Counter()
    for f in findings:
        repo_counts[f.repository.split("/")[-1]] += 1
        for field in f.missing_fields:
            missing_counts[field] += 1

    lines = [
        f"Total findings: {len(findings)}",
        "",
        "Missing field frequency:",
        *[f"  {field}: {count}" for field, count in missing_counts.most_common(10)],
        "",
        "Top repositories by finding count:",
        *[f"  {repo}: {count}" for repo, count in repo_counts.most_common(5)],
        "",
        "Sample findings (first 15):",
        *[
            f"  [{f.item_type} #{f.number}] {f.title[:60]} — missing: {', '.join(f.missing_fields)}"
            for f in findings[:15]
        ],
    ]
    return "\n".join(lines)


def build_severity_prompt(findings: list[AuditFinding]) -> str:
    lines = [
        f"Score severity (HIGH/MEDIUM/LOW) for each of these {len(findings)} findings.",
        "Return one score per finding in the same order.",
        "",
    ]
    for i, f in enumerate(findings, 1):
        updated = f.updated_at[:10] if f.updated_at else "unknown"
        lines.append(
            f"{i}. [{f.item_type} #{f.number}] {f.title[:60]}"
            f" (repo: {f.repository.split('/')[-1]}, updated: {updated},"
            f" missing: {', '.join(f.missing_fields)})"
        )
    return "\n".join(lines)


def build_explain_prompt(finding: AuditFinding, rule: str) -> str:
    return "\n".join([
        f"Rule triggered: {rule}",
        f"Item: {finding.item_type} #{finding.number} — <title>{finding.title}</title>",
        f"Repository: {finding.repository}",
        f"Assignees: {', '.join(finding.assignees) or 'none'}",
        f"All missing fields: {', '.join(finding.missing_fields)}",
        f"Updated: {finding.updated_at or 'unknown'}",
    ])


def build_nl_prompt(
    query: str,
    available_repos: list[str],
    available_assignees: list[str],
    available_fields: list[str],
) -> str:
    return "\n".join([
        f"User query: <query>{query[:500]}</query>",
        "",
        f"Available repositories: {', '.join(available_repos) or 'none'}",
        f"Available assignees: {', '.join(available_assignees) or 'none'}",
        f"Available missing fields: {', '.join(available_fields) or 'none'}",
        "Available item types: Issue, PR",
        "",
        "Map the query to filter criteria using only the available values above.",
    ])
