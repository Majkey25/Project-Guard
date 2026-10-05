from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from github_audit.api.service import ProjectGuardChatService
from github_audit.applier import build_apply_plan
from github_audit.cli import main
from github_audit.config import Settings
from github_audit.discovery import discover_all
from github_audit.github_client import GitHubClient, GitHubError, JsonObject
from github_audit.models import AuditResult, ProjectFieldDefinition, ProjectItem
from github_audit.scanner import content_from_project_item, scan_all


def _settings(org: str = "probe-org", numbers: str = "1") -> Settings:
    return Settings.model_validate(
        {
            "github_token": f"synthetic-{org}",
            "github_org": org,
            "github_project_numbers_raw": numbers,
            "github_repository_allowlist_raw": "fixture",
            "required_project_fields_raw": "Priority",
            "require_assignee": False,
            "require_target_assignee": False,
            "require_development_link": False,
            "llm_enabled": False,
        }
    )


def _project(number: int) -> tuple[JsonObject, list[ProjectFieldDefinition]]:
    return (
        {
            "id": f"P_{number}",
            "number": number,
            "title": f"Project {number}",
            "url": f"https://example.invalid/projects/{number}",
        },
        [],
    )


def _item(number: int, org: str = "probe-org") -> ProjectItem:
    return ProjectItem(
        id=f"PI_{number}",
        content_id=f"I_{number}",
        content_type="issue",
        repository=f"{org}/fixture",
        number=number,
        title=f"Issue {number}",
        url=f"https://example.invalid/issues/{number}",
        state="OPEN",
    )


@pytest.mark.parametrize("numbers", [(1,), (1, 2)])
@pytest.mark.parametrize("with_items", [False, True])
@pytest.mark.parametrize("require_project_item", [False, True])
def test_scan_reuses_all_discovered_items_without_changing_public_results(
    numbers: tuple[int, ...], with_items: bool, require_project_item: bool
) -> None:
    settings = _settings(numbers=",".join(str(number) for number in numbers))
    settings.require_project_item = require_project_item
    pages = {number: [_item(number)] if with_items else [] for number in numbers}
    handoff: dict[int, list[ProjectItem]] = {}
    client = MagicMock(spec=GitHubClient)
    with (
        patch("github_audit.discovery.probe_branch_links", return_value=(True, "ok")),
        patch(
            "github_audit.discovery.fetch_project_fields",
            side_effect=[_project(number) for number in numbers] * 2,
        ),
        patch(
            "github_audit.discovery.fetch_project_items",
            side_effect=list(pages.values()) * 2,
        ) as fetched,
        patch("github_audit.scanner.fetch_project_items") as refetched,
    ):
        original = discover_all(
            client, settings, repositories=["probe-org/fixture"], searched_items=[]
        )
        discoveries = discover_all(
            client,
            settings,
            repositories=["probe-org/fixture"],
            searched_items=[],
            project_items_by_number=handoff,
        )
        audits = scan_all(client, settings, discoveries, [], project_items_by_number=handoff)

    assert fetched.call_count == 2 * len(numbers)
    refetched.assert_not_called()
    assert handoff == pages
    assert [result.model_dump_json() for result in discoveries] == [
        result.model_dump_json() for result in original
    ]
    assert sum(len(audit.findings) for audit in audits) == len(numbers) * int(with_items)


def test_missing_handoff_project_still_fetches_and_unselected_items_do_not_count() -> None:
    settings = _settings(numbers="1,2")
    client = MagicMock(spec=GitHubClient)
    with (
        patch("github_audit.discovery.probe_branch_links", return_value=(True, "ok")),
        patch(
            "github_audit.discovery.fetch_project_fields", side_effect=[_project(1), _project(2)]
        ),
        patch("github_audit.discovery.fetch_project_items", return_value=[]),
    ):
        discoveries = discover_all(
            client, settings, repositories=["probe-org/fixture"], searched_items=[]
        )
    handoff = {1: [], 999: [_item(42)]}
    searched = content_from_project_item(_item(42), {"probe-org/fixture"})
    assert searched is not None
    with patch("github_audit.scanner.fetch_project_items", return_value=[]) as fetched:
        audits = scan_all(
            client, settings, discoveries, [searched], project_items_by_number=handoff
        )
    fetched.assert_called_once_with(client, "probe-org", 2)
    assert all([finding.number for finding in audit.findings] == [42] for audit in audits)
    assert set(handoff) == {1, 999}


def test_failed_discovery_does_not_publish_an_empty_item_snapshot() -> None:
    handoff: dict[int, list[ProjectItem]] = {}
    with (
        patch("github_audit.discovery.probe_branch_links", return_value=(True, "ok")),
        patch("github_audit.discovery.fetch_project_fields", return_value=_project(1)),
        patch("github_audit.discovery.fetch_project_items", side_effect=GitHubError("denied")),
        pytest.raises(GitHubError, match="denied"),
    ):
        discover_all(
            MagicMock(spec=GitHubClient),
            _settings(),
            repositories=["probe-org/fixture"],
            searched_items=[],
            project_items_by_number=handoff,
        )
    assert handoff == {}


def test_api_refresh_uses_new_scope_after_snapshot_expires() -> None:
    service = ProjectGuardChatService()
    started = datetime.now(UTC)
    with (
        patch(
            "github_audit.api.service.load_settings",
            side_effect=[_settings("first"), _settings("second")],
        ),
        patch("github_audit.api.service.datetime") as clock,
        patch("github_audit.api.service.GitHubClient"),
        patch(
            "github_audit.api.service.discover_repositories",
            side_effect=[["first/fixture"], ["second/fixture"]],
        ),
        patch("github_audit.api.service.search_items", return_value=[]),
        patch("github_audit.discovery.probe_branch_links", return_value=(True, "ok")),
        patch("github_audit.discovery.fetch_project_fields", return_value=_project(1)),
        patch(
            "github_audit.discovery.fetch_project_items",
            side_effect=[[_item(1, "first")], [_item(2, "second")]],
        ) as fetched,
        patch("github_audit.scanner.fetch_project_items") as refetched,
    ):
        clock.now.side_effect = [started, started + timedelta(seconds=61)]
        first = service.context_options()
        second = service.context_options()
    assert fetched.call_count == 2
    refetched.assert_not_called()
    assert len(first) == len(second) == 1
    assert "first/fixture" in first[0]["label"]
    assert "second/fixture" in second[0]["label"]


def test_cli_apply_keeps_its_fresh_read_before_write_planning() -> None:
    with (
        patch("github_audit.cli.load_settings", return_value=_settings()),
        patch("github_audit.cli.GitHubClient"),
        patch("github_audit.cli.discover_repositories", return_value=["probe-org/fixture"]),
        patch("github_audit.cli.search_items", return_value=[]),
        patch("github_audit.cli.add_suggestions"),
        patch("github_audit.cli.build_apply_plan", wraps=build_apply_plan) as build,
        patch("github_audit.discovery.probe_branch_links", return_value=(True, "ok")),
        patch("github_audit.discovery.fetch_project_fields", return_value=_project(1)),
        patch("github_audit.discovery.fetch_project_items", return_value=[]) as discovered,
        patch("github_audit.scanner.fetch_project_items", return_value=[_item(42)]) as fetched,
    ):
        exit_code = main(["apply", "--dry-run"])
    assert exit_code == 0
    discovered.assert_called_once()
    fetched.assert_called_once()
    audit = build.call_args.args[0]
    assert isinstance(audit, AuditResult)
    assert [finding.number for finding in audit.findings] == [42]
