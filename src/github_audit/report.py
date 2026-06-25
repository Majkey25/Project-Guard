from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from github_audit.github_client import JsonObject
from github_audit.models import (
    ApplyResult,
    AuditFinding,
    AuditResult,
    BrowserScanResult,
    DiscoveryResult,
)


def to_json(model: BaseModel | Sequence[BaseModel]) -> str:
    payload = (
        cast(JsonObject, model.model_dump(mode="json"))
        if isinstance(model, BaseModel)
        else [cast(JsonObject, item.model_dump(mode="json")) for item in model]
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def discovery_text(discovery: DiscoveryResult) -> str:
    lines = [
        f"Organization: {discovery.organization}",
        f"Project: #{discovery.project_number} {discovery.project_title}",
        f"Repositories: {', '.join(discovery.repositories) or 'none'}",
        f"Fields: {', '.join(field.name for field in discovery.fields) or 'none'}",
        f"Missing required fields: {', '.join(discovery.required_fields_missing) or 'none'}",
        f"Issue sample count: {discovery.issue_sample_count}",
        f"PR sample count: {discovery.pull_request_sample_count}",
        f"Project item sample count: {discovery.project_item_sample_count}",
        f"Content types: {', '.join(discovery.content_types) or 'none'}",
        f"Development strategy: {discovery.development_strategy}",
    ]
    lines.extend(f"Limitation: {limitation}" for limitation in discovery.development_limitations)
    return "\n".join(lines)


def audit_text(audit: AuditResult) -> str:
    lines = [
        f"Organization: {audit.organization}",
        (
            f"Project: #{audit.project_number} {audit.project_title}"
            if audit.project_number is not None
            else "Projects: multiple"
        ),
        f"Repositories: {', '.join(audit.repositories) or 'none'}",
        f"Scanned issues: {audit.scanned_issue_count}",
        f"Scanned PRs: {audit.scanned_pull_request_count}",
        f"Findings: {len(audit.findings)}",
    ]
    for finding in audit.findings:
        lines.append(format_finding(finding))
    lines.extend(f"Limitation: {limitation}" for limitation in audit.limitations)
    return "\n".join(lines)


def browser_scan_text(result: BrowserScanResult) -> str:
    lines = [
        f"URL: {result.url}",
        f"Title: {result.title}",
        f"Signed in: {result.signed_in}",
        f"Visible headers: {', '.join(result.headers) or 'none'}",
        f"Findings: {len(result.findings)}",
    ]
    for finding in result.findings:
        lines.append(f"{finding.row_label}: {', '.join(finding.missing_fields)}")
    lines.extend(f"Limitation: {limitation}" for limitation in result.limitations)
    return "\n".join(lines)


def apply_text(result: ApplyResult) -> str:
    lines = [f"Dry run: {result.dry_run}", f"Applied: {len(result.applied)}"]
    lines.extend(
        f"Applied: {change.repository}#{change.number} {change.field_name}"
        for change in result.applied
    )
    lines.extend(f"Skipped: {item}" for item in result.skipped)
    return "\n".join(lines)


def write_markdown(path: Path, audit: AuditResult) -> None:
    lines = [
        f"# GitHub Audit: {audit.organization}",
        "",
        (
            f"- Project: #{audit.project_number} {audit.project_title}"
            if audit.project_number is not None
            else "- Projects: multiple"
        ),
        f"- Repositories: {', '.join(audit.repositories) or 'none'}",
        f"- Scanned issues: {audit.scanned_issue_count}",
        f"- Scanned PRs: {audit.scanned_pull_request_count}",
        f"- Findings: {len(audit.findings)}",
        "",
    ]
    current_repo = ""
    for finding in audit.findings:
        if finding.repository != current_repo:
            current_repo = finding.repository
            lines.extend([f"## {current_repo}", ""])
        lines.extend(
            [
                f"### {finding.item_type} #{finding.number}: {finding.title}",
                "",
                f"- URL: {finding.url}",
                f"- Updated: {finding.updated_at or 'unknown'}",
                f"- Assignees: {', '.join(finding.assignees) or 'none'}",
                f"- Missing: {', '.join(finding.missing_fields)}",
                f"- Development: {finding.development_status}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_discovery_markdown(path: Path, discovery: DiscoveryResult) -> None:
    path.write_text(
        f"# GitHub Audit Discovery\n\n```text\n{discovery_text(discovery)}\n```\n",
        encoding="utf-8",
    )


def write_csv(path: Path, audit: AuditResult) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "repository",
                "item_type",
                "number",
                "title",
                "url",
                "updated_at",
                "assignees",
                "missing_fields",
                "development_status",
                "apply_status",
            ],
        )
        writer.writeheader()
        for finding in audit.findings:
            writer.writerow(
                {
                    "repository": finding.repository,
                    "item_type": finding.item_type,
                    "number": finding.number,
                    "title": finding.title,
                    "url": str(finding.url),
                    "updated_at": finding.updated_at or "",
                    "assignees": ",".join(finding.assignees),
                    "missing_fields": ",".join(finding.missing_fields),
                    "development_status": finding.development_status,
                    "apply_status": finding.apply_status,
                }
            )


def format_finding(finding: AuditFinding) -> str:
    project = f"Project #{finding.project_number}: " if finding.project_number is not None else ""
    return (
        f"{project}{finding.repository} {finding.item_type} #{finding.number}: "
        f"{', '.join(finding.missing_fields)}"
    )
