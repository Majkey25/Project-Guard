from __future__ import annotations

from threading import Lock
from typing import Any

from github_audit.github_client import GitHubClient, GitHubError
from github_audit.project_fields import (
    fetch_all_repositories,
    fetch_assignable_users,
    fetch_project_fields,
    fetch_project_items,
    fetch_project_numbers,
    fetch_repo_labels,
    fetch_repo_milestones,
    fetch_repositories,
    parse_content,
    probe_branch_links,
    search_items,
    split_repository,
)


class FakeClient(GitHubClient):
    """Stub that replays queued graphql() return values."""

    def __init__(self, *responses: Any) -> None:
        self._queue: list[Any] = list(responses)
        self._lock = Lock()

    def graphql(self, query: str, variables: Any = None) -> Any:
        _ = query, variables
        with self._lock:
            item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _page(nodes: list[Any], *, has_next: bool = False, cursor: str | None = None) -> dict[str, Any]:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}


def _issue_content(number: int = 1) -> dict[str, Any]:
    return {
        "__typename": "Issue",
        "id": f"I_{number}",
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://github.com/org/repo/issues/{number}",
        "state": "OPEN",
        "updatedAt": "2026-06-10T12:00:00Z",
        "body": "",
        "repository": {"nameWithOwner": "org/repo"},
        "assignees": {"nodes": [{"login": "alice"}]},
        "labels": {"nodes": [{"name": "bug"}]},
        "comments": {
            "totalCount": 1,
            "nodes": [
                {
                    "author": {"login": "bob"},
                    "body": "Please size this.",
                    "url": "https://github.com/org/repo/issues/1#issuecomment-1",
                    "updatedAt": "2026-07-01T08:00:00Z",
                }
            ],
        },
        "milestone": None,
        "closedByPullRequestsReferences": {"totalCount": 0},
    }


def _pr_content(number: int = 2) -> dict[str, Any]:
    return {
        "__typename": "PullRequest",
        "id": f"PR_{number}",
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/org/repo/pull/{number}",
        "state": "OPEN",
        "updatedAt": "2026-06-11T12:00:00Z",
        "body": "",
        "repository": {"nameWithOwner": "org/repo"},
        "assignees": {"nodes": []},
        "labels": {"nodes": []},
        "comments": {"totalCount": 0, "nodes": []},
        "milestone": None,
        "closingIssuesReferences": {"totalCount": 1},
    }


def _number_field_value(name: str = "Estimate", value: int = 3) -> dict[str, Any]:
    return {
        "__typename": "ProjectV2ItemFieldNumberValue",
        "number": value,
        "field": {
            "__typename": "ProjectV2Field",
            "id": f"f-{name.lower()}",
            "name": name,
            "dataType": "NUMBER",
        },
    }


def _single_select_field_def() -> dict[str, Any]:
    return {
        "__typename": "ProjectV2SingleSelectField",
        "id": "f-priority",
        "name": "Priority",
        "dataType": "SINGLE_SELECT",
        "options": [{"id": "opt-p1", "name": "P1"}, {"id": "opt-p2", "name": "P2"}],
    }


def _iteration_field_def() -> dict[str, Any]:
    return {
        "__typename": "ProjectV2IterationField",
        "id": "f-iteration",
        "name": "Iteration (sprint)",
        "dataType": "ITERATION",
        "configuration": {
            "iterations": [{"id": "iter-1", "title": "Sprint 1"}],
            "completedIterations": [{"id": "iter-0", "title": "Sprint 0"}],
        },
    }


# ── fetch_project_fields ──────────────────────────────────────────────────────


def test_parse_content_includes_recent_comments() -> None:
    content = parse_content(_issue_content())
    assert content is not None
    assert content.comments_total_count == 1
    assert content.comments[0].author == "bob"
    assert content.comments[0].body == "Please size this."


def test_fetch_project_fields_basic() -> None:
    response = {
        "organization": {
            "projectV2": {
                "id": "PVT_1",
                "number": 42,
                "title": "My Project",
                "url": "https://github.com/orgs/org/projects/42",
                "fields": _page([_single_select_field_def(), _iteration_field_def()]),
            }
        }
    }
    client = FakeClient(response)
    project, fields = fetch_project_fields(client, "org", 42)  # type: ignore[arg-type]
    assert project["id"] == "PVT_1"
    assert project["title"] == "My Project"
    assert len(fields) == 2
    priority = next(f for f in fields if f.name == "Priority")
    assert priority.kind == "single_select"
    assert priority.options == {"P1": "opt-p1", "P2": "opt-p2"}
    iteration = next(f for f in fields if f.name == "Iteration (sprint)")
    assert iteration.kind == "iteration"
    assert "Sprint 1" in iteration.iterations
    assert "Sprint 0" in iteration.iterations


def test_fetch_project_fields_pagination() -> None:
    page1 = {
        "organization": {
            "projectV2": {
                "id": "PVT_1",
                "number": 1,
                "title": "P",
                "url": "u",
                "fields": _page([_single_select_field_def()], has_next=True, cursor="c1"),
            }
        }
    }
    page2 = {
        "organization": {
            "projectV2": {
                "id": "PVT_1",
                "number": 1,
                "title": "P",
                "url": "u",
                "fields": _page([_iteration_field_def()]),
            }
        }
    }
    client = FakeClient(page1, page2)
    _, fields = fetch_project_fields(client, "org", 1)  # type: ignore[arg-type]
    assert len(fields) == 2


def test_fetch_project_fields_skips_unknown_type() -> None:
    unknown = {"__typename": "ProjectV2Field", "id": None, "name": None, "dataType": None}
    response = {
        "organization": {
            "projectV2": {
                "id": "PVT_1",
                "number": 1,
                "title": "P",
                "url": "u",
                "fields": _page([unknown]),
            }
        }
    }
    client = FakeClient(response)
    _, fields = fetch_project_fields(client, "org", 1)  # type: ignore[arg-type]
    assert fields == []


# ── fetch_project_items ───────────────────────────────────────────────────────


def _items_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"organization": {"projectV2": {"items": _page(items)}}}


def _project_item_node(
    content: dict[str, Any] | None,
    field_values: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "PVTI_1",
        "type": "ISSUE",
        "content": content,
        "fieldValues": _page(field_values or []),
    }


def test_fetch_project_items_issue_with_field_value() -> None:
    response = _items_response(
        [_project_item_node(_issue_content(), [_number_field_value("Estimate", 5)])]
    )
    client = FakeClient(response)
    items = fetch_project_items(client, "org", 1)  # type: ignore[arg-type]
    assert len(items) == 1
    assert items[0].repository == "org/repo"
    assert items[0].content_type == "issue"
    assert items[0].number == 1
    assert items[0].updated_at == "2026-06-10T12:00:00Z"
    assert "Estimate" in items[0].field_values
    assert items[0].field_values["Estimate"].value == 5


def test_fetch_project_items_pull_request() -> None:
    response = _items_response([_project_item_node(_pr_content())])
    client = FakeClient(response)
    items = fetch_project_items(client, "org", 1)  # type: ignore[arg-type]
    assert items[0].content_type == "pull_request"
    assert items[0].closing_issues_count == 1


def test_fetch_project_items_draft_issue() -> None:
    draft = {"__typename": "DraftIssue", "id": "DI_1", "title": "Draft"}
    response = _items_response([_project_item_node(draft)])
    client = FakeClient(response)
    items = fetch_project_items(client, "org", 1)  # type: ignore[arg-type]
    assert items[0].content_type == "draft_issue"
    assert items[0].number is None


def test_fetch_project_items_null_content() -> None:
    response = _items_response([_project_item_node(None)])
    client = FakeClient(response)
    items = fetch_project_items(client, "org", 1)  # type: ignore[arg-type]
    assert items[0].content_type == "unknown"


def test_fetch_project_items_pagination() -> None:
    page1: dict[str, Any] = {
        "organization": {
            "projectV2": {
                "items": {
                    "nodes": [_project_item_node(_issue_content(1))],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                }
            }
        }
    }
    page2: dict[str, Any] = {
        "organization": {"projectV2": {"items": _page([_project_item_node(_issue_content(2))])}}
    }
    client = FakeClient(page1, page2)
    items = fetch_project_items(client, "org", 1)  # type: ignore[arg-type]
    assert len(items) == 2


def test_fetch_project_items_milestone_and_labels() -> None:
    content = _issue_content()
    content["milestone"] = {"title": "v1.0"}
    content["labels"] = {"nodes": [{"name": "bug"}, {"name": "priority"}]}
    response = _items_response([_project_item_node(content)])
    client = FakeClient(response)
    items = fetch_project_items(client, "org", 1)  # type: ignore[arg-type]
    assert items[0].milestone == "v1.0"
    assert "bug" in items[0].labels


# ── fetch_repositories ────────────────────────────────────────────────────────


def test_fetch_repositories_from_allowlist() -> None:
    response = {
        "organization": {
            "repository": {
                "id": "R_1",
                "nameWithOwner": "org/repo-a",
                "isArchived": False,
            }
        }
    }
    client = FakeClient(response)
    repos = fetch_repositories(client, "org", ["repo-a"], include_all=False)  # type: ignore[arg-type]
    assert repos == ["org/repo-a"]


def test_fetch_repositories_skips_archived() -> None:
    response = {
        "organization": {
            "repository": {
                "id": "R_1",
                "nameWithOwner": "org/repo-archived",
                "isArchived": True,
            }
        }
    }
    client = FakeClient(response)
    repos = fetch_repositories(client, "org", ["repo-archived"], include_all=False)  # type: ignore[arg-type]
    assert repos == []


def test_fetch_all_repositories() -> None:
    response: dict[str, Any] = {
        "organization": {
            "repositories": _page(
                [
                    {"nameWithOwner": "org/repo-a", "isArchived": False},
                    {"nameWithOwner": "org/repo-b", "isArchived": True},
                ]
            )
        }
    }
    client = FakeClient(response)
    repos = fetch_all_repositories(client, "org")  # type: ignore[arg-type]
    assert repos == ["org/repo-a"]


def test_fetch_all_repositories_pagination() -> None:
    page1: dict[str, Any] = {
        "organization": {
            "repositories": {
                "nodes": [{"nameWithOwner": "org/a", "isArchived": False}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        }
    }
    page2: dict[str, Any] = {
        "organization": {"repositories": _page([{"nameWithOwner": "org/b", "isArchived": False}])}
    }
    client = FakeClient(page1, page2)
    repos = fetch_all_repositories(client, "org")  # type: ignore[arg-type]
    assert repos == ["org/a", "org/b"]


# ── fetch_project_numbers ────────────────────────────────────────────────────


def test_fetch_project_numbers_skips_closed_by_default() -> None:
    response: dict[str, Any] = {
        "organization": {
            "projectsV2": _page(
                [
                    {"number": 1, "closed": False},
                    {"number": 2, "closed": True},
                ]
            )
        }
    }
    client = FakeClient(response)
    numbers = fetch_project_numbers(client, "org", include_closed=False)  # type: ignore[arg-type]
    assert numbers == [1]


def test_fetch_project_numbers_can_include_closed() -> None:
    response: dict[str, Any] = {
        "organization": {
            "projectsV2": _page(
                [
                    {"number": 1, "closed": False},
                    {"number": 2, "closed": True},
                ]
            )
        }
    }
    client = FakeClient(response)
    numbers = fetch_project_numbers(client, "org", include_closed=True)  # type: ignore[arg-type]
    assert numbers == [1, 2]


# ── search_items ──────────────────────────────────────────────────────────────


def _search_response(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"search": _page(nodes)}


def test_search_items_returns_issues_and_prs() -> None:
    fc = FakeClient(_search_response([_issue_content(1)]), _search_response([_pr_content(2)]))
    results = search_items(
        fc,
        ["org/repo"],
        ["alice"],  # type: ignore[arg-type]
        include_issues=True,
        include_pull_requests=True,
        include_closed_issues=False,
    )
    # Issue and PR searches run concurrently, so list order isn't guaranteed - look up by id.
    by_id = {item.id: item for item in results}
    assert len(results) == 2
    assert by_id["I_1"].updated_at == "2026-06-10T12:00:00Z"
    assert by_id["PR_2"].updated_at == "2026-06-11T12:00:00Z"


def test_search_items_deduplicates_by_id() -> None:
    issue = _issue_content(1)
    fc = FakeClient(_search_response([issue]), _search_response([issue]))
    results = search_items(
        fc,
        ["org/repo"],
        ["alice"],  # type: ignore[arg-type]
        include_issues=True,
        include_pull_requests=True,
        include_closed_issues=False,
    )
    assert len(results) == 1


def test_search_items_skips_issues_when_disabled() -> None:
    fc = FakeClient(_search_response([_pr_content(2)]))
    results = search_items(
        fc,
        ["org/repo"],
        ["alice"],  # type: ignore[arg-type]
        include_issues=False,
        include_pull_requests=True,
        include_closed_issues=False,
    )
    assert len(results) == 1


def test_search_items_skips_prs_when_disabled() -> None:
    fc = FakeClient(_search_response([_issue_content(1)]))
    results = search_items(
        fc,
        ["org/repo"],
        ["alice"],  # type: ignore[arg-type]
        include_issues=True,
        include_pull_requests=False,
        include_closed_issues=False,
    )
    assert len(results) == 1


# ── probe_branch_links ────────────────────────────────────────────────────────


def test_probe_branch_links_success() -> None:
    client = FakeClient({"search": {"nodes": []}})
    available, detail = probe_branch_links(client, ["org/repo"])  # type: ignore[arg-type]
    assert available is True
    assert "exposed" in detail


def test_probe_branch_links_failure() -> None:
    client = FakeClient(GitHubError("linkedBranches not available"))
    available, detail = probe_branch_links(client, ["org/repo"])  # type: ignore[arg-type]
    assert available is False
    assert "linkedBranches" in detail


def test_probe_branch_links_no_repos() -> None:
    available, detail = probe_branch_links(FakeClient(), [])  # type: ignore[arg-type]
    assert available is False
    assert "no repository" in detail


# ── repo lookup queries ───────────────────────────────────────────────────────


def test_split_repository() -> None:
    assert split_repository("org/repo") == ("org", "repo")


def test_fetch_repo_labels_returns_name_to_id_map() -> None:
    fc = FakeClient(
        {"repository": {"labels": _page([{"id": "L_1", "name": "bug"}])}},
    )
    result = fetch_repo_labels(fc, "org/repo")  # type: ignore[arg-type]
    assert result == {"bug": "L_1"}


def test_fetch_repo_labels_paginates() -> None:
    fc = FakeClient(
        {
            "repository": {
                "labels": _page([{"id": "L_1", "name": "bug"}], has_next=True, cursor="c1")
            }
        },
        {"repository": {"labels": _page([{"id": "L_2", "name": "docs"}])}},
    )
    result = fetch_repo_labels(fc, "org/repo")  # type: ignore[arg-type]
    assert result == {"bug": "L_1", "docs": "L_2"}


def test_fetch_repo_milestones_returns_title_to_id_map() -> None:
    fc = FakeClient(
        {"repository": {"milestones": _page([{"id": "M_1", "title": "v1"}])}},
    )
    result = fetch_repo_milestones(fc, "org/repo")  # type: ignore[arg-type]
    assert result == {"v1": "M_1"}


def test_fetch_assignable_users_returns_login_to_id_map() -> None:
    fc = FakeClient(
        {"repository": {"assignableUsers": _page([{"id": "U_1", "login": "alice"}])}},
    )
    result = fetch_assignable_users(fc, "org/repo")  # type: ignore[arg-type]
    assert result == {"alice": "U_1"}
