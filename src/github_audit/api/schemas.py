from __future__ import annotations

from typing import NotRequired, TypedDict

from pydantic import AliasChoices, BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(validation_alias=AliasChoices("message", "prompt"))
    conversation_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "conversation_id",
            "conversationId",
            "session_id",
            "sessionId",
        ),
    )
    context: str | None = None

    model_config = {"populate_by_name": True}


class ChatDetails(TypedDict, total=False):
    next_question: list[str]


class ChatPayload(TypedDict):
    conversation_id: str
    conversationId: str
    answer: str
    details: ChatDetails


class ContextOption(TypedDict):
    value: str
    label: str


class ContextOptionsPayload(TypedDict):
    options: list[ContextOption]


class StatusPayload(TypedDict):
    ok: bool
    configured: bool
    llm_ready: bool
    configuration_error: NotRequired[str]
    project_guard: str
