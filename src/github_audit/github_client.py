from __future__ import annotations

import time
from collections.abc import Mapping
from typing import cast

import httpx

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# 403 is included because GitHub's secondary/abuse rate limit responds with it, but a bare 403
# is usually a genuine auth/permission failure - _looks_rate_limited() disambiguates before retry.
_RETRYABLE_STATUSES = {403, 429, 502, 503, 504}
_MAX_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 120.0


class GitHubError(RuntimeError):
    """GitHub request failed."""


def _header_str(headers: httpx.Headers, name: str) -> str | None:
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _looks_rate_limited(response: httpx.Response) -> bool:
    if _header_str(response.headers, "retry-after") is not None:
        return True
    return _header_str(response.headers, "x-ratelimit-remaining") == "0"


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = _header_str(response.headers, "retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    reset_at = _header_str(response.headers, "x-ratelimit-reset")
    if reset_at is not None:
        try:
            return max(0.0, float(reset_at) - time.time())
        except ValueError:
            pass
    return float(2**attempt)


def _is_rate_limited_graphql_error(errors: list[JsonValue]) -> bool:
    return any(isinstance(error, dict) and error.get("type") == "RATE_LIMITED" for error in errors)


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
        is_mutation = query.lstrip().startswith("mutation")
        for attempt in range(_MAX_ATTEMPTS):
            is_last = attempt == _MAX_ATTEMPTS - 1
            try:
                response = self._client.post(GITHUB_GRAPHQL_URL, json=payload)
            except httpx.HTTPError as exc:
                # Transport failures must never escape as raw httpx errors - callers
                # rely on GitHubError. Connect-phase failures never reached GitHub and
                # are always safe to retry; a later failure (read timeout, reset) may
                # mean a mutation already executed, where a blind retry double-writes.
                request_sent = not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
                if not is_last and not (request_sent and is_mutation):
                    time.sleep(min(float(2**attempt), _MAX_BACKOFF_SECONDS))
                    continue
                msg = f"GitHub request failed: {exc}"
                raise GitHubError(msg) from exc
            # 5xx can arrive after the mutation already executed server-side, so a
            # retry double-writes (e.g. duplicate addComment). 429 and rate-limited
            # 403 are rejected before execution and stay safe to retry.
            retryable = (
                response.status_code in _RETRYABLE_STATUSES
                and not is_last
                and (not is_mutation or response.status_code in {403, 429})
            )
            if retryable and (response.status_code != 403 or _looks_rate_limited(response)):
                time.sleep(min(_retry_delay(response, attempt), _MAX_BACKOFF_SECONDS))
                continue
            if response.status_code >= 400:
                msg = f"GitHub API returned HTTP {response.status_code}"
                raise GitHubError(msg)
            try:
                raw = response.json()
            except ValueError as exc:
                msg = "GitHub returned invalid JSON"
                raise GitHubError(msg) from exc
            if not isinstance(raw, dict):
                msg = "GitHub GraphQL returned non-object JSON"
                raise GitHubError(msg)
            data = cast(JsonObject, raw)
            errors = data.get("errors")
            if isinstance(errors, list) and errors:
                if _is_rate_limited_graphql_error(errors) and not is_last:
                    time.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
                    continue
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
        msg = "GitHub GraphQL request was not sent"
        raise GitHubError(msg)


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
