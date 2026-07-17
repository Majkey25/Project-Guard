from __future__ import annotations

from github_audit.branches import parse_branch
from github_audit.github_client import JsonObject, JsonValue
from github_audit.models import AuditFinding, BranchInfo, BranchPullRequest


def _raw_branch(name: str, prs: list[JsonValue]) -> JsonObject:
    return {
        "name": name,
        "target": {
            "__typename": "Commit",
            "committedDate": "2026-06-01T10:00:00Z",
            "messageHeadline": "fix: something",
            "author": {"user": {"login": "majkey"}, "name": "Michal"},
        },
        "associatedPullRequests": {"totalCount": len(prs), "nodes": prs},
    }


def test_parse_branch_full() -> None:
    raw = _raw_branch(
        "feat/x",
        [
            {
                "number": 7,
                "state": "MERGED",
                "isDraft": False,
                "title": "Feat x",
                "url": "https://github.com/o/r/pull/7",
            }
        ],
    )
    branch = parse_branch(raw, "o/r", "main")
    assert branch.name == "feat/x"
    assert branch.is_default is False
    assert branch.last_commit_date == "2026-06-01T10:00:00Z"
    assert branch.last_committer == "majkey"
    assert branch.url == "https://github.com/o/r/tree/feat/x"
    assert branch.pull_requests[0].state == "MERGED"


def test_parse_branch_default_and_url_quoting() -> None:
    raw = _raw_branch("fix/#12 test", [])
    raw["name"] = "fix/#12 test"
    branch = parse_branch(raw, "o/r", "fix/#12 test")
    assert branch.is_default is True
    assert branch.url == "https://github.com/o/r/tree/fix/%2312%20test"


def test_parse_branch_missing_commit_author_falls_back_to_name() -> None:
    raw = _raw_branch("b", [])
    target = raw["target"]
    assert isinstance(target, dict)
    target["author"] = {"user": None, "name": "Ext Committer"}
    branch = parse_branch(raw, "o/r", "main")
    assert branch.last_committer == "Ext Committer"


def test_pr_state_priority() -> None:
    def info(states: list[str]) -> BranchInfo:
        return BranchInfo(
            repository="o/r",
            name="b",
            url="https://github.com/o/r/tree/b",
            pull_requests=[
                BranchPullRequest(number=i, state=s, title="t", url="u")
                for i, s in enumerate(states, start=1)
            ],
        )

    assert info([]).pr_state == "none"
    assert info(["CLOSED"]).pr_state == "closed"
    assert info(["CLOSED", "MERGED"]).pr_state == "merged"
    assert info(["MERGED", "OPEN"]).pr_state == "open"


def test_display_state_labels() -> None:
    def finding(state: str, *, is_draft: bool = False) -> AuditFinding:
        return AuditFinding(
            repository="o/r",
            item_type="pull_request",
            number=1,
            title="t",
            url="u",
            state=state,
            is_draft=is_draft,
            assignees=[],
            missing_fields=["assignee"],
            development_status="closing_issues=0",
        )

    assert finding("OPEN").display_state == "Open"
    assert finding("OPEN", is_draft=True).display_state == "Draft"
    assert finding("MERGED").display_state == "Merged"
    assert finding("CLOSED", is_draft=True).display_state == "Closed"
    assert finding("").display_state == ""
