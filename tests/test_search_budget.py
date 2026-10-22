from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Barrier, Lock
from unittest.mock import patch

import pytest

from github_audit.cli import main
from github_audit.config import Settings
from github_audit.github_client import GitHubClient, GitHubError, JsonObject, JsonValue
from github_audit.project_fields import search_items


class PagingClient(GitHubClient):
    def __init__(self, response: Callable[[Mapping[str, JsonValue]], JsonObject]) -> None:
        self.response = response

    def graphql(self, query: str, variables: Mapping[str, JsonValue] | None = None) -> JsonObject:
        assert "query SearchItems" in query
        assert variables is not None
        return self.response(variables)

    def close(self) -> None:
        pass


def page(*, count: int = 1, after: str | None = None) -> JsonObject:
    item: JsonObject = {
        "__typename": "Issue",
        "id": "I_1",
        "number": 1,
        "title": "Fixture",
        "url": "https://github.com/org/repo/issues/1",
        "state": "OPEN",
        "repository": {"nameWithOwner": "org/repo"},
        "assignees": {"nodes": []},
        "labels": {"nodes": []},
        "closedByPullRequestsReferences": {"totalCount": 0},
    }
    nodes: list[JsonValue] = [item] * count
    return {
        "search": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": after is not None, "endCursor": after},
        }
    }


def test_excess_query_scope_fails_before_sending_requests() -> None:
    def unexpected(_variables: Mapping[str, JsonValue]) -> JsonObject:
        pytest.fail("Scope must be rejected before any GitHub request")

    with pytest.raises(GitHubError, match="needs more than 2 queries"):
        search_items(
            PagingClient(unexpected),
            (f"org/repo-{number}" for number in range(100_000)),
            ["alice"],
            include_issues=True,
            include_pull_requests=False,
            include_closed_issues=False,
            max_pages=2,
        )


def test_page_reservations_are_shared_across_concurrent_searches() -> None:
    barrier = Barrier(2)
    calls: list[JsonValue] = []
    lock = Lock()

    def response(variables: Mapping[str, JsonValue]) -> JsonObject:
        with lock:
            calls.append(variables["after"])
        barrier.wait(timeout=5)
        return page(after="next")

    with pytest.raises(GitHubError, match="GITHUB_SEARCH_MAX_PAGES"):
        search_items(
            PagingClient(response),
            ["org/first", "org/second"],
            ["alice"],
            include_issues=True,
            include_pull_requests=False,
            include_closed_issues=False,
            max_pages=2,
        )
    assert calls == [None, None]


@pytest.mark.parametrize("workers,queries,nodes", [(2, 2, 1), (8, 10, 2)])
def test_raw_node_limit_counts_duplicates_and_stops_queued_requests(
    workers: int, queries: int, nodes: int
) -> None:
    barrier = Barrier(workers)
    calls: list[JsonValue] = []
    lock = Lock()

    def response(variables: Mapping[str, JsonValue]) -> JsonObject:
        with lock:
            calls.append(variables["query"])
        barrier.wait(timeout=5)
        return page(count=nodes)

    with pytest.raises(GitHubError, match="GITHUB_SEARCH_MAX_ITEMS"):
        search_items(
            PagingClient(response),
            [f"org/repo-{number}" for number in range(queries)],
            ["alice"],
            include_issues=True,
            include_pull_requests=False,
            include_closed_issues=False,
            max_items=1,
        )
    assert len(calls) == len(set(calls)) == workers


def test_complete_search_at_exact_budget_keeps_deduplication() -> None:
    calls: list[JsonValue] = []

    def response(variables: Mapping[str, JsonValue]) -> JsonObject:
        calls.append(variables["after"])
        return page(after="next" if variables["after"] is None else None)

    results = search_items(
        PagingClient(response),
        ["org/repo"],
        ["alice"],
        include_issues=True,
        include_pull_requests=False,
        include_closed_issues=False,
        max_pages=2,
        max_items=2,
    )
    assert calls == [None, "next"]
    assert [item.id for item in results] == ["I_1"]


@pytest.mark.parametrize("cursor", [None, "same"])
def test_search_rejects_nonadvancing_cursor(cursor: str | None) -> None:
    calls = 0

    def response(_variables: Mapping[str, JsonValue]) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"search": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": cursor}}}

    with pytest.raises(GitHubError, match="pagination did not advance"):
        search_items(
            PagingClient(response),
            ["org/repo"],
            ["alice"],
            include_issues=True,
            include_pull_requests=False,
            include_closed_issues=False,
        )
    assert calls == (1 if cursor is None else 2)


def test_apply_does_not_reach_suggestions_or_writes_after_budget_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings.model_validate(
        {
            "github_token": "synthetic",
            "github_project_number": 1,
            "github_repository_allowlist_raw": "repo",
            "target_assignees_raw": "alice",
            "include_pull_requests": False,
            "auto_apply": True,
            "github_search_max_items": 1,
        }
    )
    with (
        patch("github_audit.cli.load_settings", return_value=settings),
        patch("github_audit.cli.GitHubClient", return_value=PagingClient(lambda _: page(count=2))),
        patch("github_audit.cli.discover_repositories", return_value=["org/repo"]),
        patch("github_audit.cli.add_suggestions") as suggest,
        patch("github_audit.cli.apply_plan") as apply,
    ):
        assert main(["apply", "--yes"]) == 2
    assert "GITHUB_SEARCH_MAX_ITEMS" in capsys.readouterr().err
    suggest.assert_not_called()
    apply.assert_not_called()
