from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from github_audit.github_client import (
    GitHubClient,
    JsonObject,
    as_list,
    as_object,
    optional_str,
    required_int,
    required_str,
)
from github_audit.models import BranchInfo, BranchPullRequest

BRANCHES_QUERY = """
query RepoBranches($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      nodes {
        name
        target {
          __typename
          ... on Commit {
            committedDate
            messageHeadline
            author {
              user { login }
              name
            }
          }
        }
        associatedPullRequests(first: 5, orderBy: {field: UPDATED_AT, direction: DESC}) {
          totalCount
          nodes {
            number
            state
            isDraft
            title
            url
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_BRANCH_MAX_WORKERS = 8


def fetch_branches(client: GitHubClient, repositories: list[str]) -> list[BranchInfo]:
    """Fetch all branches for the given "owner/name" repositories, concurrently."""
    if not repositories:
        return []
    with ThreadPoolExecutor(max_workers=min(_BRANCH_MAX_WORKERS, len(repositories))) as pool:
        futures = [pool.submit(fetch_repo_branches, client, repo) for repo in repositories]
        return [branch for future in futures for branch in future.result()]


def fetch_repo_branches(client: GitHubClient, repository: str) -> list[BranchInfo]:
    owner, _, name = repository.partition("/")
    branches: list[BranchInfo] = []
    after: str | None = None
    while True:
        data = client.graphql(BRANCHES_QUERY, {"owner": owner, "name": name, "after": after})
        repo = as_object(data.get("repository"), "repository")
        default_ref = repo.get("defaultBranchRef")
        default_name = (
            optional_str(as_object(default_ref, "defaultBranchRef").get("name"))
            if default_ref is not None
            else None
        )
        connection = as_object(repo.get("refs"), "repository.refs")
        for node in as_list(connection.get("nodes"), "repository.refs.nodes"):
            branches.append(parse_branch(as_object(node, "branch"), repository, default_name))
        page_info = as_object(connection.get("pageInfo"), "repository.refs.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return branches


def parse_branch(raw: JsonObject, repository: str, default_name: str | None) -> BranchInfo:
    name = required_str(raw.get("name"), "branch name")
    target = raw.get("target")
    commit = as_object(target, "branch target") if isinstance(target, dict) else {}
    committer: str | None = None
    author_raw = commit.get("author")
    if isinstance(author_raw, dict):
        user_raw = author_raw.get("user")
        committer = (
            optional_str(as_object(user_raw, "commit author user").get("login"))
            if isinstance(user_raw, dict)
            else None
        ) or optional_str(author_raw.get("name"))
    prs_raw = as_object(raw.get("associatedPullRequests"), "associatedPullRequests")
    pull_requests = [
        BranchPullRequest(
            number=required_int(pr.get("number"), "pr number"),
            state=required_str(pr.get("state"), "pr state"),
            is_draft=pr.get("isDraft") is True,
            title=required_str(pr.get("title"), "pr title"),
            url=required_str(pr.get("url"), "pr url"),
        )
        for node in as_list(prs_raw.get("nodes"), "associatedPullRequests.nodes")
        for pr in (as_object(node, "associated pull request"),)
    ]
    return BranchInfo(
        repository=repository,
        name=name,
        url=f"https://github.com/{repository}/tree/{quote(name, safe='/')}",
        is_default=name == default_name,
        last_commit_date=optional_str(commit.get("committedDate")),
        last_committer=committer,
        last_commit_message=optional_str(commit.get("messageHeadline")) or "",
        pull_requests=pull_requests,
        pull_requests_total=required_int(
            prs_raw.get("totalCount"), "associatedPullRequests.totalCount"
        ),
    )
