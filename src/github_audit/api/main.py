from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterator
from typing import Annotated, cast

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from github_audit.api.schemas import (
    ChatDetails,
    ChatPayload,
    ChatRequest,
    ContextOption,
    ContextOptionsPayload,
    StatusPayload,
)
from github_audit.api.service import ChatResult, ChatServiceError, ProjectGuardChatService
from github_audit.github_client import GitHubError

app = FastAPI(title="Project Guard API", docs_url=None, redoc_url=None)
service = ProjectGuardChatService()


@app.get("/status")
def status() -> StatusPayload:
    return cast(StatusPayload, service.status())


@app.get("/context")
def context_options() -> ContextOptionsPayload:
    try:
        return {"options": cast(list[ContextOption], service.context_options())}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ChatServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/chat", response_model=None)
def chat(
    request: Request,
    payload: Annotated[ChatRequest, Body()],
) -> JSONResponse | EventSourceResponse:
    stream_param = request.query_params.get("stream", "true").lower()
    wants_stream = stream_param in {"1", "true", "yes"}
    try:
        if wants_stream:
            stream_bundle = service.stream(
                payload.message,
                payload.conversation_id,
                payload.context,
            )
            if stream_bundle is not None:
                return EventSourceResponse(
                    _stream_events(stream_bundle),
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
        result = service.reply(payload.message, payload.conversation_id, payload.context)
        return JSONResponse(_payload(result))
    except ChatServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _stream_events(
    stream_bundle: tuple[Iterator[str], Callable[[], ChatResult]],
) -> Iterator[dict[str, str]]:
    tokens, finalise = stream_bundle
    try:
        for token in tokens:
            if token:
                yield {"data": json.dumps({"delta": token}, ensure_ascii=False)}
        yield {"data": json.dumps(_payload(finalise()), ensure_ascii=False)}
        yield {"data": "[DONE]"}
    except Exception:
        yield {"data": json.dumps({"error": "Chat failed"}, ensure_ascii=False)}
        yield {"data": "[DONE]"}


def _payload(result: ChatResult) -> ChatPayload:
    details: ChatDetails = {"next_question": result.next_questions} if result.next_questions else {}
    return {
        "conversation_id": result.conversation_id,
        "conversationId": result.conversation_id,
        "answer": result.answer,
        "details": details,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Project Guard API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
