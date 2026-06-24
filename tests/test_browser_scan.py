from __future__ import annotations

from github_audit.browser_scan import (
    BrowserSettings,
    browser_start_url,
    extract_browser_findings,
)


def test_extract_browser_findings_detects_missing_estimate() -> None:
    result = extract_browser_findings(
        url="https://github.com/orgs/OKsystem/projects/1/views/1",
        title="Project",
        signed_in=True,
        headers=["Title", "Estimate", "Priority"],
        rows=[["Issue 1", "", "P1"], ["Issue 2", "3", ""]],
        required_fields=["Estimate", "Priority"],
    )
    assert [finding.missing_fields for finding in result.findings] == [
        ["Estimate"],
        ["Priority"],
    ]


def test_browser_start_url_uses_project_number() -> None:
    settings = BrowserSettings.model_validate(
        {"github_org": "OKsystem", "github_project_number": 12}
    )
    assert browser_start_url(settings, None).endswith("/orgs/OKsystem/projects/12/views/1")
