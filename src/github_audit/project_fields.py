from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from github_audit.github_client import (
    GitHubClient,
    GitHubError,
    JsonObject,
    JsonValue,
    as_list,
    as_object,
    optional_str,
    required_int,
    required_str,
)
from github_audit.models import (
    GitHubComment,
    GitHubContent,
    GitHubIssue,
    GitHubPullRequest,
    ProjectContentType,
    ProjectFieldDefinition,
    ProjectFieldValue,
    ProjectItem,
)

FIELD_FRAGMENT = """
fragment FieldParts on ProjectV2FieldConfiguration {
  __typename
  ... on ProjectV2Field {
    id
    name
    dataType
  }
  ... on ProjectV2SingleSelectField {
    id
    name
    dataType
    options {
      id
      name
    }
  }
  ... on ProjectV2IterationField {
    id
    name
    dataType
    configuration {
      iterations {
        id
        title
      }
      completedIterations {
        id
        title
      }
    }
  }
}
"""

FIELD_VALUE_FRAGMENT = """
fragment FieldValueParts on ProjectV2ItemFieldValue {
  __typename
  ... on ProjectV2ItemFieldTextValue {
    text
    field { ...FieldParts }
  }
  ... on ProjectV2ItemFieldNumberValue {
    number
    field { ...FieldParts }
  }
  ... on ProjectV2ItemFieldDateValue {
    date
    field { ...FieldParts }
  }
  ... on ProjectV2ItemFieldSingleSelectValue {
    name
    optionId
    field { ...FieldParts }
  }
  ... on ProjectV2ItemFieldIterationValue {
    title
    iterationId
    field { ...FieldParts }
  }
}
"""

CONTENT_FRAGMENT = """
fragment ContentParts on ProjectV2ItemContent {
  __typename
  ... on Issue {
    id
    number
    title
    url
    state
    updatedAt
    body
    repository { nameWithOwner }
    assignees(first: 20) { nodes { login } }
    labels(first: 20) { nodes { name } }
    comments(last: 20) {
      totalCount
      nodes {
        author { login }
        body
        url
        updatedAt
      }
    }
    milestone { title }
    closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
      totalCount
    }
  }
  ... on PullRequest {
    id
    number
    title
    url
    state
    updatedAt
    body
    repository { nameWithOwner }
    assignees(first: 20) { nodes { login } }
    labels(first: 20) { nodes { name } }
    comments(last: 20) {
      totalCount
      nodes {
        author { login }
        body
        url
        updatedAt
      }
    }
    milestone { title }
    closingIssuesReferences(first: 10) {
      totalCount
    }
  }
  ... on DraftIssue {
    id
    title
  }
}
"""

PROJECT_QUERY = (
    FIELD_FRAGMENT
    + """
query ProjectFields($org: String!, $number: Int!, $after: String) {
  organization(login: $org) {
    login
    projectV2(number: $number) {
      id
      number
      title
      url
      fields(first: 100, after: $after) {
        nodes { ...FieldParts }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
)

PROJECT_ITEMS_QUERY = (
    FIELD_FRAGMENT
    + FIELD_VALUE_FRAGMENT
    + CONTENT_FRAGMENT
    + """
query ProjectItems($org: String!, $number: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      items(first: 50, after: $after) {
        nodes {
          id
          type
          content { ...ContentParts }
          fieldValues(first: 100) {
            nodes { ...FieldValueParts }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
)

ITEM_FIELD_VALUES_QUERY = (
    FIELD_FRAGMENT
    + FIELD_VALUE_FRAGMENT
    + """
query ItemFieldValues($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2Item {
      fieldValues(first: 100, after: $after) {
        nodes { ...FieldValueParts }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
)

REPOSITORY_QUERY = """
query Repository($org: String!, $name: String!) {
  organization(login: $org) {
    repository(name: $name) {
      id
      nameWithOwner
      isArchived
    }
  }
}
"""

REPOSITORIES_QUERY = """
query Repositories($org: String!, $after: String) {
  organization(login: $org) {
    repositories(first: 100, after: $after, orderBy: {field: NAME, direction: ASC}) {
      nodes {
        nameWithOwner
        isArchived
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECT_NUMBERS_QUERY = """
query ProjectNumbers($org: String!, $after: String) {
  organization(login: $org) {
    projectsV2(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        closed
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

SEARCH_QUERY = """
query SearchItems($query: String!, $after: String) {
  search(query: $query, type: ISSUE, first: 100, after: $after) {
    nodes {
      __typename
      ... on Issue {
        id
        number
        title
        url
        state
        updatedAt
        body
        repository { nameWithOwner }
        assignees(first: 20) { nodes { login } }
        labels(first: 20) { nodes { name } }
        comments(last: 20) {
          totalCount
          nodes {
            author { login }
            body
            url
            updatedAt
          }
        }
        milestone { title }
        closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
          totalCount
        }
      }
      ... on PullRequest {
        id
        number
        title
        url
        state
        updatedAt
        body
        repository { nameWithOwner }
        assignees(first: 20) { nodes { login } }
        labels(first: 20) { nodes { name } }
        comments(last: 20) {
          totalCount
          nodes {
            author { login }
            body
            url
            updatedAt
          }
        }
        milestone { title }
        closingIssuesReferences(first: 10) {
          totalCount
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

BRANCH_LINK_PROBE_QUERY = """
query BranchLinkProbe($query: String!) {
  search(query: $query, type: ISSUE, first: 1) {
    nodes {
      ... on Issue {
        linkedBranches(first: 1) {
          totalCount
        }
      }
    }
  }
}
"""

REPO_LABELS_QUERY = """
query RepoLabels($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    labels(first: 100, after: $after) {
      nodes { id name }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

REPO_MILESTONES_QUERY = """
query RepoMilestones($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    milestones(first: 100, after: $after, states: [OPEN, CLOSED]) {
      nodes { id title }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

REPO_ASSIGNABLE_USERS_QUERY = """
query RepoAssignableUsers($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    assignableUsers(first: 100, after: $after) {
      nodes { id login }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def split_repository(repository: str) -> tuple[str, str]:
    owner, _, name = repository.partition("/")
    return owner, name


def _fetch_repo_named_ids(
    client: GitHubClient,
    repository: str,
    query: str,
    connection_name: str,
    *,
    label_key: str = "name",
) -> dict[str, str]:
    owner, name = split_repository(repository)
    results: dict[str, str] = {}
    after: str | None = None
    while True:
        data = client.graphql(query, {"owner": owner, "name": name, "after": after})
        repo = as_object(data.get("repository"), "repository")
        connection = as_object(repo.get(connection_name), f"repository.{connection_name}")
        results.update(parse_named_ids(connection.get("nodes"), label_key))
        page_info = as_object(connection.get("pageInfo"), f"repository.{connection_name}.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return results


def fetch_repo_labels(client: GitHubClient, repository: str) -> dict[str, str]:
    """Return {label name: label node id} for the given "owner/name" repository."""
    return _fetch_repo_named_ids(client, repository, REPO_LABELS_QUERY, "labels")


def fetch_repo_milestones(client: GitHubClient, repository: str) -> dict[str, str]:
    """Return {milestone title: milestone node id}, open and closed."""
    return _fetch_repo_named_ids(
        client, repository, REPO_MILESTONES_QUERY, "milestones", label_key="title"
    )


def fetch_assignable_users(client: GitHubClient, repository: str) -> dict[str, str]:
    """Return {login: user node id} for users assignable in the given repository."""
    return _fetch_repo_named_ids(
        client, repository, REPO_ASSIGNABLE_USERS_QUERY, "assignableUsers", label_key="login"
    )


def fetch_project_fields(
    client: GitHubClient, org: str, project_number: int
) -> tuple[JsonObject, list[ProjectFieldDefinition]]:
    project: JsonObject = {}
    fields: list[ProjectFieldDefinition] = []
    after: str | None = None
    while True:
        data = client.graphql(PROJECT_QUERY, {"org": org, "number": project_number, "after": after})
        organization = as_object(data.get("organization"), "organization")
        raw_project = as_object(organization.get("projectV2"), "projectV2")
        project = raw_project
        connection = as_object(raw_project.get("fields"), "projectV2.fields")
        fields.extend(parse_field_nodes(as_list(connection.get("nodes"), "projectV2.fields.nodes")))
        page_info = as_object(connection.get("pageInfo"), "projectV2.fields.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return project, fields


def fetch_repositories(
    client: GitHubClient, org: str, allowlist: Iterable[str], include_all: bool
) -> list[str]:
    if include_all:
        return fetch_all_repositories(client, org)
    repositories: list[str] = []
    for name in allowlist:
        data = client.graphql(REPOSITORY_QUERY, {"org": org, "name": name})
        organization = as_object(data.get("organization"), "organization")
        repository = as_object(organization.get("repository"), f"repository {name}")
        if repository.get("isArchived") is True:
            continue
        repositories.append(
            required_str(repository.get("nameWithOwner"), "repository.nameWithOwner")
        )
    return repositories


def fetch_all_repositories(client: GitHubClient, org: str) -> list[str]:
    repositories: list[str] = []
    after: str | None = None
    while True:
        data = client.graphql(REPOSITORIES_QUERY, {"org": org, "after": after})
        organization = as_object(data.get("organization"), "organization")
        connection = as_object(organization.get("repositories"), "organization.repositories")
        for node in as_list(connection.get("nodes"), "organization.repositories.nodes"):
            repository = as_object(node, "repository")
            if repository.get("isArchived") is not True:
                repositories.append(
                    required_str(repository.get("nameWithOwner"), "repository.nameWithOwner")
                )
        page_info = as_object(connection.get("pageInfo"), "organization.repositories.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return repositories


def fetch_project_numbers(client: GitHubClient, org: str, *, include_closed: bool) -> list[int]:
    project_numbers: list[int] = []
    after: str | None = None
    while True:
        data = client.graphql(PROJECT_NUMBERS_QUERY, {"org": org, "after": after})
        organization = as_object(data.get("organization"), "organization")
        connection = as_object(organization.get("projectsV2"), "organization.projectsV2")
        for node in as_list(connection.get("nodes"), "organization.projectsV2.nodes"):
            project = as_object(node, "project")
            if include_closed or project.get("closed") is not True:
                project_numbers.append(required_int(project.get("number"), "project.number"))
        page_info = as_object(connection.get("pageInfo"), "organization.projectsV2.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return project_numbers


def fetch_project_items(client: GitHubClient, org: str, project_number: int) -> list[ProjectItem]:
    items: list[ProjectItem] = []
    after: str | None = None
    while True:
        data = client.graphql(
            PROJECT_ITEMS_QUERY, {"org": org, "number": project_number, "after": after}
        )
        organization = as_object(data.get("organization"), "organization")
        project = as_object(organization.get("projectV2"), "projectV2")
        connection = as_object(project.get("items"), "projectV2.items")
        for node in as_list(connection.get("nodes"), "projectV2.items.nodes"):
            item_node = as_object(node, "project item")
            item = parse_project_item(item_node)
            raw_values = as_object(item_node.get("fieldValues"), "project item fieldValues")
            page_info = as_object(raw_values.get("pageInfo"), "project item fieldValues.pageInfo")
            if page_info.get("hasNextPage") is True:
                item.field_values.update(
                    fetch_remaining_field_values(
                        client,
                        item.id,
                        optional_str(page_info.get("endCursor")),
                    )
                )
            items.append(item)
        page_info = as_object(connection.get("pageInfo"), "projectV2.items.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return items


def fetch_remaining_field_values(
    client: GitHubClient, item_id: str, after: str | None
) -> dict[str, ProjectFieldValue]:
    values: dict[str, ProjectFieldValue] = {}
    while after:
        data = client.graphql(ITEM_FIELD_VALUES_QUERY, {"id": item_id, "after": after})
        node = as_object(data.get("node"), "node")
        connection = as_object(node.get("fieldValues"), "node.fieldValues")
        values.update(
            parse_field_value_nodes(as_list(connection.get("nodes"), "node.fieldValues.nodes"))
        )
        page_info = as_object(connection.get("pageInfo"), "node.fieldValues.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return values


_SEARCH_MAX_WORKERS = 8


def search_items(
    client: GitHubClient,
    repositories: Iterable[str],
    assignees: Iterable[str],
    *,
    include_issues: bool,
    include_pull_requests: bool,
    include_closed_issues: bool,
    include_closed_pull_requests: bool = False,
    include_unassigned: bool = False,
) -> list[GitHubContent]:
    issue_states = "" if include_closed_issues else " is:open"
    pr_states = "" if include_closed_pull_requests else " is:open"
    queries: list[str] = []
    for repository in repositories:
        for assignee in assignees:
            if include_issues:
                queries.append(f"repo:{repository} is:issue assignee:{assignee}{issue_states}")
            if include_pull_requests:
                queries.append(f"repo:{repository} is:pr assignee:{assignee}{pr_states}")
        if include_unassigned:
            if include_issues:
                queries.append(f"repo:{repository} is:issue no:assignee{issue_states}")
            if include_pull_requests:
                queries.append(f"repo:{repository} is:pr no:assignee{pr_states}")
    if not queries:
        return []
    items_by_id: dict[str, GitHubContent] = {}
    with ThreadPoolExecutor(max_workers=min(_SEARCH_MAX_WORKERS, len(queries))) as pool:
        # Submit-order iteration (not as_completed) keeps merge order deterministic
        # even though the searches themselves run concurrently.
        futures = [pool.submit(run_search, client, query) for query in queries]
        for future in futures:
            for item in future.result():
                items_by_id[item.id] = item
    return list(items_by_id.values())


def run_search(client: GitHubClient, query: str) -> list[GitHubContent]:
    items: list[GitHubContent] = []
    after: str | None = None
    while True:
        data = client.graphql(SEARCH_QUERY, {"query": query, "after": after})
        connection = as_object(data.get("search"), "search")
        for node in as_list(connection.get("nodes"), "search.nodes"):
            item = parse_content(as_object(node, "search node"))
            if item is not None:
                items.append(item)
        page_info = as_object(connection.get("pageInfo"), "search.pageInfo")
        if page_info.get("hasNextPage") is not True:
            break
        after = optional_str(page_info.get("endCursor"))
    return items


def probe_branch_links(client: GitHubClient, repositories: list[str]) -> tuple[bool, str]:
    if not repositories:
        return False, "no repository available for branch-link probe"
    try:
        client.graphql(BRANCH_LINK_PROBE_QUERY, {"query": f"repo:{repositories[0]} is:issue"})
    except GitHubError as exc:
        return False, str(exc)
    return True, "Issue.linkedBranches is exposed by GraphQL for this token"


def parse_field_nodes(nodes: list[JsonValue]) -> list[ProjectFieldDefinition]:
    fields: list[ProjectFieldDefinition] = []
    for node in nodes:
        field = parse_field(as_object(node, "field"))
        if field is not None:
            fields.append(field)
    return fields


def parse_field(raw: JsonObject) -> ProjectFieldDefinition | None:
    typename = optional_str(raw.get("__typename")) or ""
    name = optional_str(raw.get("name"))
    field_id = optional_str(raw.get("id"))
    data_type = optional_str(raw.get("dataType"))
    if not name or not field_id or not data_type:
        return None
    if typename == "ProjectV2SingleSelectField":
        return ProjectFieldDefinition(
            id=field_id,
            name=name,
            data_type=data_type,
            kind="single_select",
            options=parse_named_ids(raw.get("options"), "name"),
        )
    if typename == "ProjectV2IterationField":
        configuration = as_object(raw.get("configuration"), "iteration configuration")
        iterations = parse_named_ids(configuration.get("iterations"), "title")
        iterations.update(parse_named_ids(configuration.get("completedIterations"), "title"))
        return ProjectFieldDefinition(
            id=field_id,
            name=name,
            data_type=data_type,
            kind="iteration",
            iterations=iterations,
        )
    return ProjectFieldDefinition(id=field_id, name=name, data_type=data_type, kind="field")


def parse_named_ids(value: JsonValue, label_key: str) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    results: dict[str, str] = {}
    for item in value:
        raw = as_object(item, "named id")
        label = optional_str(raw.get(label_key))
        item_id = optional_str(raw.get("id"))
        if label and item_id:
            results[label] = item_id
    return results


def parse_project_item(raw: JsonObject) -> ProjectItem:
    content = raw.get("content")
    content_object = as_object(content, "project item content") if content else {}
    parsed_content = parse_content(content_object) if content else None
    raw_values = as_object(raw.get("fieldValues"), "project item fieldValues")
    field_values = parse_field_value_nodes(
        as_list(raw_values.get("nodes"), "project item fieldValues.nodes")
    )
    if parsed_content is None:
        content_type = parse_content_type(optional_str(content_object.get("__typename")) or "")
        return ProjectItem(
            id=required_str(raw.get("id"), "project item id"),
            content_id=optional_str(content_object.get("id")) if content else None,
            content_type=content_type,
            repository=None,
            number=None,
            title=optional_str(content_object.get("title")) or "",
            url=None,
            field_values=field_values,
        )
    return ProjectItem(
        id=required_str(raw.get("id"), "project item id"),
        content_id=parsed_content.id,
        content_type="issue" if isinstance(parsed_content, GitHubIssue) else "pull_request",
        repository=parsed_content.repository,
        number=parsed_content.number,
        title=parsed_content.title,
        body=parsed_content.body,
        url=parsed_content.url,
        assignees=parsed_content.assignees,
        labels=parsed_content.labels,
        comments=parsed_content.comments,
        comments_total_count=parsed_content.comments_total_count,
        milestone=parsed_content.milestone,
        updated_at=parsed_content.updated_at,
        field_values=field_values,
        linked_pull_requests_count=(
            parsed_content.linked_pull_requests_count
            if isinstance(parsed_content, GitHubIssue)
            else 0
        ),
        closing_issues_count=(
            parsed_content.closing_issues_count
            if isinstance(parsed_content, GitHubPullRequest)
            else 0
        ),
    )


def parse_content(raw: JsonObject) -> GitHubContent | None:
    typename = optional_str(raw.get("__typename"))
    if typename == "Issue":
        return GitHubIssue(
            id=required_str(raw.get("id"), "issue id"),
            repository=parse_repository_name(raw),
            number=required_int(raw.get("number"), "issue number"),
            title=required_str(raw.get("title"), "issue title"),
            url=required_str(raw.get("url"), "issue url"),
            state=required_str(raw.get("state"), "issue state"),
            updated_at=optional_str(raw.get("updatedAt")),
            body=optional_str(raw.get("body")) or "",
            assignees=parse_named_nodes(raw, "assignees", "login"),
            labels=parse_named_nodes(raw, "labels", "name"),
            comments=parse_comments(raw),
            comments_total_count=parse_comments_total_count(raw),
            milestone=parse_milestone(raw),
            linked_pull_requests_count=parse_total_count(raw, "closedByPullRequestsReferences"),
        )
    if typename == "PullRequest":
        return GitHubPullRequest(
            id=required_str(raw.get("id"), "pull request id"),
            repository=parse_repository_name(raw),
            number=required_int(raw.get("number"), "pull request number"),
            title=required_str(raw.get("title"), "pull request title"),
            url=required_str(raw.get("url"), "pull request url"),
            state=required_str(raw.get("state"), "pull request state"),
            updated_at=optional_str(raw.get("updatedAt")),
            body=optional_str(raw.get("body")) or "",
            assignees=parse_named_nodes(raw, "assignees", "login"),
            labels=parse_named_nodes(raw, "labels", "name"),
            comments=parse_comments(raw),
            comments_total_count=parse_comments_total_count(raw),
            milestone=parse_milestone(raw),
            closing_issues_count=parse_total_count(raw, "closingIssuesReferences"),
        )
    return None


def parse_field_value_nodes(nodes: list[JsonValue]) -> dict[str, ProjectFieldValue]:
    values: dict[str, ProjectFieldValue] = {}
    for node in nodes:
        raw = as_object(node, "field value")
        raw_field = raw.get("field")
        if not isinstance(raw_field, dict):
            continue
        field = parse_field(raw_field)
        if field is None:
            continue
        value = parse_field_value(raw)
        if value is None:
            continue
        values[field.name] = ProjectFieldValue(
            field_id=field.id,
            field_name=field.name,
            value=value,
            option_id=optional_str(raw.get("optionId")),
            iteration_id=optional_str(raw.get("iterationId")),
        )
    return values


def parse_field_value(raw: JsonObject) -> str | int | float | bool | None:
    for key in ("text", "number", "date", "name", "title"):
        value = raw.get(key)
        if isinstance(value, str | int | float | bool):
            return value
    return None


def parse_repository_name(raw: JsonObject) -> str:
    repository = as_object(raw.get("repository"), "repository")
    return required_str(repository.get("nameWithOwner"), "repository.nameWithOwner")


def parse_named_nodes(raw: JsonObject, connection_name: str, key: str) -> list[str]:
    connection = as_object(raw.get(connection_name), connection_name)
    names: list[str] = []
    for node in as_list(connection.get("nodes"), f"{connection_name}.nodes"):
        item = as_object(node, connection_name)
        name = optional_str(item.get(key))
        if name:
            names.append(name)
    return names


def parse_comments(raw: JsonObject) -> list[GitHubComment]:
    connection = raw.get("comments")
    if connection is None:
        return []
    comments: list[GitHubComment] = []
    for node in as_list(as_object(connection, "comments").get("nodes"), "comments.nodes"):
        item = as_object(node, "comment")
        author_raw = item.get("author")
        author = (
            optional_str(as_object(author_raw, "comment.author").get("login"))
            if author_raw is not None
            else None
        )
        body = optional_str(item.get("body"))
        if body:
            comments.append(
                GitHubComment(
                    author=author,
                    body=body,
                    url=optional_str(item.get("url")),
                    updated_at=optional_str(item.get("updatedAt")),
                )
            )
    return comments


def parse_comments_total_count(raw: JsonObject) -> int:
    connection = raw.get("comments")
    if connection is None:
        return 0
    return required_int(as_object(connection, "comments").get("totalCount"), "comments.totalCount")


def parse_milestone(raw: JsonObject) -> str | None:
    milestone = raw.get("milestone")
    if milestone is None:
        return None
    return optional_str(as_object(milestone, "milestone").get("title"))


def parse_total_count(raw: JsonObject, connection_name: str) -> int:
    connection = as_object(raw.get(connection_name), connection_name)
    return required_int(connection.get("totalCount"), f"{connection_name}.totalCount")


def parse_content_type(typename: str) -> ProjectContentType:
    if typename == "DraftIssue":
        return "draft_issue"
    if typename == "Redacted":
        return "redacted"
    return "unknown"
