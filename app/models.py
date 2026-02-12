from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatSettings(BaseModel):
    model: str | None = None
    max_output_tokens: int = Field(default=700, ge=120, le=4000)
    max_context_turns: int = Field(default=10, ge=2, le=40)
    enable_tools: bool = True
    response_style: Literal["short", "normal", "long"] = "normal"


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1)
    attachment_ids: list[str] = Field(default_factory=list)
    settings: ChatSettings = Field(default_factory=ChatSettings)


class ToolEvent(BaseModel):
    name: str
    input: dict | None = None
    output_preview: str


class ChatResponse(BaseModel):
    session_id: str
    text: str
    tool_events: list[ToolEvent] = Field(default_factory=list)
    turn_count: int
    summarized: bool = False


class UploadResponse(BaseModel):
    id: str
    name: str
    mime: str
    size: int
    kind: Literal["image", "document", "other"]


class NewSessionResponse(BaseModel):
    session_id: str


class HealthResponse(BaseModel):
    ok: bool
    model_default: str
