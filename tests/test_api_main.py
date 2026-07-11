# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable

import pytest
from fastapi.testclient import TestClient

from github_audit.api import main
from github_audit.api.service import ChatResult, ChatUnavailableError
from github_audit.github_client import GitHubError


class FakeService:
    def status(self) -> dict[str, object]:
        return {
            "ok": True,
            "configured": True,
            "llm_ready": True,
            "project_guard": "api",
        }

    def context_options(self) -> list[dict[str, str]]:
        return [{"value": "1|org/repo|issue|7", "label": "org/repo #7: Estimate"}]

    def reply(
        self,
        message: str,
        conversation_id: str | None = None,
        context: str | None = None,
    ) -> ChatResult:
        return ChatResult(conversation_id or "new-session", f"reply:{message}:{context or ''}")

    def stream(
        self,
        message: str,
        conversation_id: str | None = None,
        context: str | None = None,
    ) -> tuple[AsyncGenerator[str, None], Callable[[], ChatResult]] | None:
        if context:
            return None

        async def chunks() -> AsyncGenerator[str, None]:
            yield "hel"
            yield "lo"

        def finalise() -> ChatResult:
            return ChatResult(conversation_id or "new-session", f"hello:{message}")

        return chunks(), finalise


@pytest.fixture(autouse=True)
def fake_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "service", FakeService())


def test_status() -> None:
    response = TestClient(main.app).get("/status")

    assert response.status_code == 200
    assert response.json()["project_guard"] == "api"


def test_context_options() -> None:
    response = TestClient(main.app).get("/context")

    assert response.status_code == 200
    assert response.json() == {
        "options": [{"value": "1|org/repo|issue|7", "label": "org/repo #7: Estimate"}]
    }


def test_chat_json_path() -> None:
    response = TestClient(main.app).post(
        "/chat?stream=false",
        json={"prompt": "set estimate to 20", "conversationId": "c1", "context": "ctx"},
    )

    assert response.status_code == 200
    assert response.json()["conversationId"] == "c1"
    assert response.json()["answer"] == "reply:set estimate to 20:ctx"


def test_chat_sse_path() -> None:
    response = TestClient(main.app).post("/chat?stream=true", json={"prompt": "hello"})

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert 'data: {"delta": "hel"}' in response.text
    assert '"answer": "hello:hello"' in response.text
    assert "data: [DONE]" in response.text


def test_stream_events_close_propagates_to_token_stream() -> None:
    """Client disconnect closes _stream_events; the LLM token stream must close too."""
    closed = False

    async def tokens() -> AsyncGenerator[str, None]:
        nonlocal closed
        try:
            yield "a"
            yield "b"
        finally:
            closed = True

    async def scenario() -> None:
        events = main._stream_events((tokens(), lambda: ChatResult("s", "ab")))
        assert await anext(events) == {"data": '{"delta": "a"}'}
        await events.aclose()

    asyncio.run(scenario())
    assert closed is True


def test_chat_error_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenService(FakeService):
        def reply(
            self,
            message: str,
            conversation_id: str | None = None,
            context: str | None = None,
        ) -> ChatResult:
            raise ChatUnavailableError("LLM is not configured")

        def stream(
            self,
            message: str,
            conversation_id: str | None = None,
            context: str | None = None,
        ) -> tuple[AsyncGenerator[str, None], Callable[[], ChatResult]] | None:
            return None

    monkeypatch.setattr(main, "service", BrokenService())

    response = TestClient(main.app).post("/chat?stream=false", json={"prompt": "hello"})

    assert response.status_code == 503
    assert response.json()["detail"] == "LLM is not configured"


def test_chat_github_error_maps_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    class GitHubBrokenService(FakeService):
        def reply(
            self,
            message: str,
            conversation_id: str | None = None,
            context: str | None = None,
        ) -> ChatResult:
            raise GitHubError("GitHub GraphQL error: API rate limit already exceeded")

        def stream(
            self,
            message: str,
            conversation_id: str | None = None,
            context: str | None = None,
        ) -> tuple[AsyncGenerator[str, None], Callable[[], ChatResult]] | None:
            return None

        def context_options(self) -> list[dict[str, str]]:
            raise GitHubError("GitHub GraphQL error: API rate limit already exceeded")

    monkeypatch.setattr(main, "service", GitHubBrokenService())

    response = TestClient(main.app).post("/chat?stream=false", json={"prompt": "hello"})
    assert response.status_code == 502
    assert "rate limit" in response.json()["detail"]

    response = TestClient(main.app).get("/context")
    assert response.status_code == 502
    assert "rate limit" in response.json()["detail"]
