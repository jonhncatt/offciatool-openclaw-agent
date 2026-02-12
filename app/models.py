from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatSettings(BaseModel):
    model: str | None = None
    max_output_tokens: int = Field(default=32000, ge=120, le=64000)
    max_context_turns: int = Field(default=100, ge=2, le=200)
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


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0


class TokenTotals(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    session_id: str
    text: str
    tool_events: list[ToolEvent] = Field(default_factory=list)
    execution_plan: list[str] = Field(default_factory=list)
    execution_trace: list[str] = Field(default_factory=list)
    missing_attachment_ids: list[str] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    session_token_totals: TokenTotals = Field(default_factory=TokenTotals)
    global_token_totals: TokenTotals = Field(default_factory=TokenTotals)
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


class TokenStatsResponse(BaseModel):
    totals: TokenTotals = Field(default_factory=TokenTotals)
    sessions: dict[str, TokenTotals] = Field(default_factory=dict)
    records: list[dict] = Field(default_factory=list)


class ClearStatsResponse(BaseModel):
    ok: bool
