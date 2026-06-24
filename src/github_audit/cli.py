from __future__ import annotations

import argparse
import sys
from pathlib import Path

from github_audit.applier import apply_plan, build_apply_plan
from github_audit.browser_scan import BrowserSettings, run_browser_scan
from github_audit.config import Settings, load_settings
from github_audit.discovery import discover_all
from github_audit.github_client import GitHubClient, GitHubError
from github_audit.llm_evaluator import suggest_for_finding
from github_audit.logging import configure_logging
from github_audit.models import AuditResult
from github_audit.report import (
    apply_text,
    audit_text,
    browser_scan_text,
    discovery_text,
    to_json,
    write_csv,
    write_markdown,
)
from github_audit.scanner import scan_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="github-audit")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--json", type=Path)
    discover_parser.add_argument("--markdown", type=Path)

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--json", action="store_true")
    scan_parser.add_argument("--markdown", type=Path)
    scan_parser.add_argument("--csv", type=Path)

    suggest_parser = subparsers.add_parser("suggest")
    suggest_parser.add_argument("--json", action="store_true")

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--yes", action="store_true")

    browser_parser = subparsers.add_parser("browser-scan")
    browser_parser.add_argument("--project-url")
    browser_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(bool(args.verbose))
    try:
        if args.command == "browser-scan":
            result = run_browser_scan(
                BrowserSettings(),
                project_url=args.project_url,
            )
            print(to_json(result) if args.json else browser_scan_text(result))
            return 0
        settings = load_settings()
        with GitHubClient(settings.github_token) as client:
            if args.command == "discover":
                results = discover_all(client, settings)
                if args.json:
                    args.json.write_text(to_json(results), encoding="utf-8")
                if args.markdown:
                    args.markdown.write_text(
                        "\n\n".join(
                            f"# GitHub Audit Discovery\n\n```text\n{discovery_text(result)}\n```"
                            for result in results
                        ),
                        encoding="utf-8",
                    )
                print("\n\n".join(discovery_text(result) for result in results))
                return 0
            discoveries = discover_all(client, settings)
            if args.command == "apply" and len(discoveries) != 1:
                msg = "apply supports one project; set GITHUB_PROJECT_NUMBER to one project number"
                raise ValueError(msg)
            audits = scan_all(client, settings, discoveries)
            audit = merge_audits(audits)
            if args.command == "scan":
                if args.markdown:
                    write_markdown(args.markdown, audit)
                if args.csv:
                    write_csv(args.csv, audit)
                print(to_json(audit) if args.json else audit_text(audit))
                return 0
            if args.command == "suggest":
                add_suggestions(audit, settings)
                print(to_json(audit) if args.json else audit_text(audit))
                return 0
            if args.command == "apply":
                suggestion_error: str | None = None
                try:
                    add_suggestions(audit, settings)
                except ValueError as exc:
                    if args.yes:
                        raise
                    suggestion_error = str(exc)
                discovery = discoveries[0]
                plan = build_apply_plan(audit, discovery.fields, settings)
                if suggestion_error:
                    plan.skipped.append(f"suggestions unavailable: {suggestion_error}")
                result = apply_plan(
                    client,
                    plan,
                    discovery.project_id,
                    discovery.fields,
                    dry_run=bool(args.dry_run) or not bool(args.yes),
                    allow_write=settings.auto_apply and bool(args.yes),
                )
                print(apply_text(result))
                return 0
    except (GitHubError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def add_suggestions(audit: AuditResult, settings: Settings) -> None:
    for finding in audit.findings:
        finding.llm_suggestion = suggest_for_finding(finding, settings)


def merge_audits(audits: list[AuditResult]) -> AuditResult:
    if len(audits) == 1:
        return audits[0]
    findings = [finding for audit in audits for finding in audit.findings]
    findings.sort(
        key=lambda item: (
            item.project_number or 0,
            item.repository,
            item.item_type,
            item.number,
        )
    )
    return AuditResult(
        organization=audits[0].organization,
        repositories=sorted({repository for audit in audits for repository in audit.repositories}),
        findings=findings,
        scanned_issue_count=sum(audit.scanned_issue_count for audit in audits),
        scanned_pull_request_count=sum(audit.scanned_pull_request_count for audit in audits),
        limitations=sorted({limitation for audit in audits for limitation in audit.limitations}),
    )


if __name__ == "__main__":
    raise SystemExit(main())
