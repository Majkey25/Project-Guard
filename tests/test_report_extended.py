from __future__ import annotations

import csv
from pathlib import Path

from github_audit.models import (
    ApplyChange,
    ApplyResult,
    AuditFinding,
    AuditResult,
    BrowserProjectFinding,
    BrowserScanResult,
    DiscoveryResult,
    ProjectFieldDefinition,
)
from github_audit.report import (
    apply_text,
    browser_scan_text,
    discovery_text,
    format_finding,
    write_csv,
    write_markdown,
)


def _discovery() -> DiscoveryResult:
    return DiscoveryResult(
        organization="OKsystem",
        project_id="PVT_1",
        project_number=42,
        project_title="My Project",
        project_url="https://github.com/orgs/OKsystem/projects/42",
        repositories=["OKsystem/repo-a"],
        fields=[
            ProjectFieldDefinition(id="f1", name="Estimate", data_type="NUMBER", kind="field"),
        ],
        required_fields_missing=["Priority"],
        issue_sample_count=5,
        pull_request_sample_count=2,
        project_item_sample_count=10,
        content_types=["issue", "pull_request"],
        development_strategy="closing references",
        development_limitations=["branch links not probed"],
    )


def _finding(number: int = 1, item_type: str = "issue") -> AuditFinding:
    return AuditFinding(
        repository="OKsystem/repo",
        item_type=item_type,  # type: ignore[arg-type]
        number=number,
        title=f"Issue {number}",
        url=f"https://github.com/OKsystem/repo/issues/{number}",
        assignees=["alice"],
        missing_fields=["Estimate"],
        development_status="linked_pull_requests=0",
        project_number=42,
    )


def _audit() -> AuditResult:
    return AuditResult(
        organization="OKsystem",
        project_number=42,
        project_title="My Project",
        repositories=["OKsystem/repo"],
        findings=[_finding(1), _finding(2)],
        scanned_issue_count=2,
        scanned_pull_request_count=0,
    )


# ── discovery_text ───────────────────────────────────────────────────────────


def test_discovery_text_contains_org() -> None:
    text = discovery_text(_discovery())
    assert "OKsystem" in text


def test_discovery_text_shows_project_number_and_title() -> None:
    text = discovery_text(_discovery())
    assert "#42" in text
    assert "My Project" in text


def test_discovery_text_shows_missing_required_fields() -> None:
    text = discovery_text(_discovery())
    assert "Priority" in text


def test_discovery_text_shows_limitations() -> None:
    text = discovery_text(_discovery())
    assert "branch links not probed" in text


def test_discovery_text_shows_counts() -> None:
    text = discovery_text(_discovery())
    assert "5" in text  # issue sample count
    assert "2" in text  # PR sample count


# ── browser_scan_text ────────────────────────────────────────────────────────


def test_browser_scan_text_signed_in() -> None:
    result = BrowserScanResult(
        url="https://github.com/orgs/OKsystem/projects/1",
        title="Project",
        signed_in=True,
        headers=["Title", "Estimate"],
        findings=[
            BrowserProjectFinding(
                row_label="Issue 1",
                missing_fields=["Estimate"],
                cells=["Issue 1", ""],
            )
        ],
        missing_headers=[],
        limitations=["only visible rows"],
    )
    text = browser_scan_text(result)
    assert "Findings: 1" in text
    assert "Issue 1" in text
    assert "Estimate" in text
    assert "only visible rows" in text


def test_browser_scan_text_not_signed_in() -> None:
    result = BrowserScanResult(
        url="https://github.com",
        title="GitHub",
        signed_in=False,
        headers=[],
        findings=[],
        missing_headers=["Estimate"],
        limitations=["not signed in"],
    )
    text = browser_scan_text(result)
    assert "False" in text


# ── apply_text ───────────────────────────────────────────────────────────────


def _change() -> ApplyChange:
    return ApplyChange(
        repository="org/repo",
        item_type="issue",
        number=1,
        project_item_id="item-1",
        field_name="Estimate",
        value=5,
    )


def test_apply_text_dry_run() -> None:
    result = ApplyResult(dry_run=True, applied=[], skipped=["dry-run: org/repo#1 set Estimate=5"])
    text = apply_text(result)
    assert "Dry run: True" in text
    assert "Applied: 0" in text
    assert "dry-run" in text


def test_apply_text_applied_changes() -> None:
    change = _change()
    result = ApplyResult(dry_run=False, applied=[change])
    text = apply_text(result)
    assert "Dry run: False" in text
    assert "Applied: 1" in text
    assert "org/repo#1 Estimate" in text


# ── format_finding ───────────────────────────────────────────────────────────


def test_format_finding_with_project_number() -> None:
    text = format_finding(_finding(1))
    assert "Project #42" in text
    assert "OKsystem/repo" in text
    assert "#1" in text
    assert "Estimate" in text


def test_format_finding_without_project_number() -> None:
    finding = _finding(2)
    finding.project_number = None
    text = format_finding(finding)
    assert "Project" not in text


# ── write_markdown ────────────────────────────────────────────────────────────


def test_write_markdown_creates_file(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    write_markdown(out, _audit())
    content = out.read_text(encoding="utf-8")
    assert "# GitHub Audit" in content
    assert "OKsystem/repo" in content
    assert "Issue 1" in content
    assert "Issue 2" in content


def test_write_markdown_groups_by_repo(tmp_path: Path) -> None:
    audit = AuditResult(
        organization="OKsystem",
        repositories=["OKsystem/repo-a", "OKsystem/repo-b"],
        findings=[
            AuditFinding(
                repository="OKsystem/repo-a",
                item_type="issue",
                number=1,
                title="A1",
                url="https://github.com/OKsystem/repo-a/issues/1",
                assignees=[],
                missing_fields=["Estimate"],
                development_status="linked_pull_requests=0",
            ),
            AuditFinding(
                repository="OKsystem/repo-b",
                item_type="issue",
                number=2,
                title="B2",
                url="https://github.com/OKsystem/repo-b/issues/2",
                assignees=[],
                missing_fields=["Priority"],
                development_status="linked_pull_requests=0",
            ),
        ],
        scanned_issue_count=2,
        scanned_pull_request_count=0,
    )
    out = tmp_path / "report.md"
    write_markdown(out, audit)
    content = out.read_text(encoding="utf-8")
    assert "## OKsystem/repo-a" in content
    assert "## OKsystem/repo-b" in content


# ── write_csv ─────────────────────────────────────────────────────────────────


def test_write_csv_creates_valid_csv(tmp_path: Path) -> None:
    out = tmp_path / "report.csv"
    write_csv(out, _audit())
    with out.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["repository"] == "OKsystem/repo"
    assert rows[0]["item_type"] == "issue"
    assert rows[0]["number"] == "1"
    assert "Estimate" in rows[0]["missing_fields"]


def test_write_csv_assignees_joined(tmp_path: Path) -> None:
    audit = AuditResult(
        organization="OKsystem",
        repositories=["OKsystem/repo"],
        findings=[
            AuditFinding(
                repository="OKsystem/repo",
                item_type="issue",
                number=1,
                title="T",
                url="https://github.com/OKsystem/repo/issues/1",
                assignees=["alice", "bob"],
                missing_fields=["Estimate"],
                development_status="linked_pull_requests=0",
            )
        ],
        scanned_issue_count=1,
        scanned_pull_request_count=0,
    )
    out = tmp_path / "report.csv"
    write_csv(out, audit)
    with out.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        row = next(reader)
    assert row["assignees"] == "alice,bob"
