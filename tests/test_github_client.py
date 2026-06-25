from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from github_audit.github_client import (
    GitHubClient,
    GitHubError,
    as_list,
    as_object,
    optional_str,
    required_int,
    required_str,
)


def _resp(*, status: int = 200, body: object = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.text = text
    return r


@pytest.fixture
def http() -> Generator[MagicMock, None, None]:
    """Yield a mock httpx.Client instance, active for the duration of the test."""
    instance = MagicMock()
    with patch("github_audit.github_client.httpx.Client", return_value=instance):
        yield instance


# ── graphql success ───────────────────────────────────────────────────────────


def test_graphql_returns_data_object(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"data": {"org": {"id": "O_1"}}})
    result = GitHubClient("tok").graphql("query {}")
    assert result == {"org": {"id": "O_1"}}


def test_graphql_passes_variables(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"data": {}})
    GitHubClient("tok").graphql("query($x: String!) {}", {"x": "hello"})
    sent = http.post.call_args[1]["json"]
    assert sent["variables"] == {"x": "hello"}


def test_graphql_defaults_variables_to_empty_dict(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"data": {}})
    GitHubClient("tok").graphql("query {}")
    sent = http.post.call_args[1]["json"]
    assert sent["variables"] == {}


# ── HTTP errors ───────────────────────────────────────────────────────────────


def test_graphql_raises_on_401(http: MagicMock) -> None:
    http.post.return_value = _resp(status=401, text="Unauthorized")
    with pytest.raises(GitHubError, match="401"):
        GitHubClient("tok").graphql("query {}")


def test_graphql_raises_on_403(http: MagicMock) -> None:
    http.post.return_value = _resp(status=403, text="Forbidden")
    with pytest.raises(GitHubError, match="403"):
        GitHubClient("tok").graphql("query {}")


def test_graphql_raises_on_500(http: MagicMock) -> None:
    http.post.return_value = _resp(status=500, text="Internal Server Error")
    with pytest.raises(GitHubError, match="500"):
        GitHubClient("tok").graphql("query {}")


# ── GraphQL-layer errors ──────────────────────────────────────────────────────


def test_graphql_raises_on_errors_field(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"data": {}, "errors": [{"message": "Field not found"}]})
    with pytest.raises(GitHubError, match="Field not found"):
        GitHubClient("tok").graphql("query {}")


def test_graphql_raises_on_errors_without_message(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"data": {}, "errors": [{"no_msg": True}]})
    with pytest.raises(GitHubError, match="unknown error"):
        GitHubClient("tok").graphql("query {}")


def test_graphql_raises_when_data_missing(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"errors": []})
    with pytest.raises(GitHubError, match="missing data"):
        GitHubClient("tok").graphql("query {}")


def test_graphql_raises_when_data_not_dict(http: MagicMock) -> None:
    http.post.return_value = _resp(body={"data": "oops"})
    with pytest.raises(GitHubError, match="missing data"):
        GitHubClient("tok").graphql("query {}")


def test_graphql_raises_on_array_response(http: MagicMock) -> None:
    http.post.return_value = _resp(body=["not", "a", "dict"])
    with pytest.raises(GitHubError, match="non-object"):
        GitHubClient("tok").graphql("query {}")


# ── retry logic ───────────────────────────────────────────────────────────────


def test_graphql_retries_429_and_succeeds(http: MagicMock) -> None:
    http.post.side_effect = [
        _resp(status=429, text="rate limited"),
        _resp(body={"data": {"ok": True}}),
    ]
    with patch("time.sleep"):
        result = GitHubClient("tok").graphql("query {}")
    assert result == {"ok": True}
    assert http.post.call_count == 2


def test_graphql_retries_502(http: MagicMock) -> None:
    http.post.side_effect = [
        _resp(status=502, text="bad gateway"),
        _resp(status=502, text="bad gateway"),
        _resp(body={"data": {}}),
    ]
    with patch("time.sleep"):
        result = GitHubClient("tok").graphql("query {}")
    assert result == {}
    assert http.post.call_count == 3


def test_graphql_503_exhausts_retries_and_raises(http: MagicMock) -> None:
    http.post.return_value = _resp(status=503, text="unavailable")
    with patch("time.sleep"), pytest.raises(GitHubError, match="503"):
        GitHubClient("tok").graphql("query {}")


# ── context manager ───────────────────────────────────────────────────────────


def test_context_manager_calls_close(http: MagicMock) -> None:
    with GitHubClient("tok"):
        pass
    http.close.assert_called_once()


def test_context_manager_returns_self() -> None:
    with patch("github_audit.github_client.httpx.Client"):
        client = GitHubClient("tok")
    with client as ctx:
        assert ctx is client


# ── helper functions ──────────────────────────────────────────────────────────


def test_as_object_with_dict() -> None:
    assert as_object({"a": 1}, "field") == {"a": 1}


def test_as_object_raises_for_list() -> None:
    with pytest.raises(GitHubError, match="field"):
        as_object([1, 2], "field")


def test_as_object_raises_for_none() -> None:
    with pytest.raises(GitHubError, match="field"):
        as_object(None, "field")


def test_as_list_with_list() -> None:
    assert as_list([1, 2], "items") == [1, 2]


def test_as_list_raises_for_dict() -> None:
    with pytest.raises(GitHubError, match="items"):
        as_list({}, "items")


def test_optional_str_returns_string() -> None:
    assert optional_str("hello") == "hello"


def test_optional_str_returns_none_for_int() -> None:
    assert optional_str(99) is None


def test_optional_str_returns_none_for_none() -> None:
    assert optional_str(None) is None


def test_required_str_returns_value() -> None:
    assert required_str("val", "f") == "val"


def test_required_str_raises_for_int() -> None:
    with pytest.raises(GitHubError, match="f"):
        required_str(42, "f")


def test_required_str_raises_for_none() -> None:
    with pytest.raises(GitHubError, match="f"):
        required_str(None, "f")


def test_required_int_returns_value() -> None:
    assert required_int(7, "n") == 7


def test_required_int_raises_for_string() -> None:
    with pytest.raises(GitHubError, match="n"):
        required_int("seven", "n")
