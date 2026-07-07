from __future__ import annotations

from github_audit.models import AuditFinding, AuditResult, LLMSuggestion
from github_audit.report import audit_text, to_json


def test_report_text_groups_core_counts() -> None:
    audit = AuditResult(
        organization="OKsystem",
        repositories=["OKsystem/repo"],
        findings=[
            AuditFinding(
                repository="OKsystem/repo",
                item_type="issue",
                number=1,
                title="Title",
                url="https://github.com/OKsystem/repo/issues/1",
                assignees=["alice"],
                missing_fields=["Estimate"],
                development_status="linked_pull_requests=0",
            )
        ],
        scanned_issue_count=1,
        scanned_pull_request_count=0,
    )
    assert "Findings: 1" in audit_text(audit)
    assert '"missing_fields"' in to_json(audit)


def test_report_text_renders_llm_suggestion() -> None:
    audit = AuditResult(
        organization="OKsystem",
        repositories=["OKsystem/repo"],
        findings=[
            AuditFinding(
                repository="OKsystem/repo",
                item_type="issue",
                number=1,
                title="Title",
                url="https://github.com/OKsystem/repo/issues/1",
                assignees=["alice"],
                missing_fields=["Estimate", "Priority"],
                development_status="linked_pull_requests=0",
                llm_suggestion=LLMSuggestion(
                    estimated_points=3,
                    priority="P1",
                    confidence=0.8,
                    rationale="Small well-scoped task.",
                    should_auto_apply=False,
                ),
            )
        ],
        scanned_issue_count=1,
        scanned_pull_request_count=0,
    )
    text = audit_text(audit)
    assert "Suggest: Estimate=3, Priority=P1" in text
    assert "confidence 0.80" in text
    assert "Small well-scoped task." in text
