from __future__ import annotations

import time
from collections.abc import Mapping
from typing import cast

import httpx

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


class GitHubError(RuntimeError):
    """GitHub request failed."""


class GitHubClient:
    def __init__(self, token: str, *, timeout_seconds: float = 30.0) -> None:
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "github-audit/0.1.0",
            },
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def graphql(self, query: str, variables: Mapping[str, JsonValue] | None = None) -> JsonObject:
        payload: JsonObject = {"query": query, "variables": dict(variables or {})}
        response: httpx.Response | None = None
        for attempt in range(3):
            response = self._client.post(GITHUB_GRAPHQL_URL, json=payload)
            if response.status_code in {429, 502, 503, 504} and attempt < 2:
                time.sleep(2**attempt)
                continue
            break
        if response is None:
            msg = "GitHub GraphQL request was not sent"
            raise GitHubError(msg)
        if response.status_code >= 400:
            detail = response.text[:500]
            msg = f"GitHub GraphQL HTTP {response.status_code}: {detail}"
            raise GitHubError(msg)
        raw = response.json()
        if not isinstance(raw, dict):
            msg = "GitHub GraphQL returned non-object JSON"
            raise GitHubError(msg)
        data = cast(JsonObject, raw)
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            messages: list[str] = []
            for error in errors:
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str):
                        messages.append(message)
            msg = "GitHub GraphQL error: " + "; ".join(messages or ["unknown error"])
            raise GitHubError(msg)
        graph_data = data.get("data")
        if not isinstance(graph_data, dict):
            msg = "GitHub GraphQL response missing data"
            raise GitHubError(msg)
        return cast(JsonObject, graph_data)


def as_object(value: JsonValue, name: str) -> JsonObject:
    if isinstance(value, dict):
        return value
    msg = f"{name} is missing or not an object"
    raise GitHubError(msg)


def as_list(value: JsonValue, name: str) -> list[JsonValue]:
    if isinstance(value, list):
        return value
    msg = f"{name} is missing or not a list"
    raise GitHubError(msg)


def optional_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def required_str(value: JsonValue, name: str) -> str:
    if isinstance(value, str):
        return value
    msg = f"{name} is missing or not a string"
    raise GitHubError(msg)


def required_int(value: JsonValue, name: str) -> int:
    if isinstance(value, int):
        return value
    msg = f"{name} is missing or not an integer"
    raise GitHubError(msg)
