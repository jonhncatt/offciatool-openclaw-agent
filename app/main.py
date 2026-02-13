from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent import OfficeAgent
from app.config import load_config
from app.models import (
    ChatRequest,
    ChatResponse,
    ClearStatsResponse,
    DeleteSessionResponse,
    HealthResponse,
    NewSessionResponse,
    SessionDetailResponse,
    SessionListItem,
    SessionListResponse,
    SessionTurn,
    TokenStatsResponse,
    TokenTotals,
    TokenUsage,
    UploadResponse,
)
from app.pricing import estimate_usage_cost
from app.storage import SessionStore, TokenStatsStore, UploadStore

config = load_config()
session_store = SessionStore(config.sessions_dir)
upload_store = UploadStore(config.uploads_dir)
token_stats_store = TokenStatsStore(config.token_stats_path)
_agent: OfficeAgent | None = None


def get_agent() -> OfficeAgent:
    global _agent
    if _agent is None:
        _agent = OfficeAgent(config)
    return _agent

app = FastAPI(title="Officetool", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = (Path(__file__).resolve().parent / "static").resolve()
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.middleware("http")
async def disable_static_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(static_dir / "index.html"))


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True, model_default=config.default_model)


@app.post("/api/session/new", response_model=NewSessionResponse)
def create_session() -> NewSessionResponse:
    session = session_store.create()
    return NewSessionResponse(session_id=session["id"])


@app.delete("/api/session/{session_id}", response_model=DeleteSessionResponse)
def delete_session(session_id: str) -> DeleteSessionResponse:
    deleted = session_store.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return DeleteSessionResponse(ok=True, session_id=session_id)


@app.get("/api/session/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, max_turns: int = 200) -> SessionDetailResponse:
    loaded = session_store.load(session_id)
    if not loaded:
        raise HTTPException(status_code=404, detail="Session not found")

    turns_raw = loaded.get("turns", [])
    if not isinstance(turns_raw, list):
        turns_raw = []
    limited_turns = turns_raw[-max(1, min(2000, max_turns)) :]
    turns: list[SessionTurn] = []
    for item in limited_turns:
        if not isinstance(item, dict):
            continue
        turns.append(
            SessionTurn(
                role=str(item.get("role") or "user"),
                text=str(item.get("text") or ""),
                created_at=str(item.get("created_at")) if item.get("created_at") else None,
            )
        )

    return SessionDetailResponse(
        session_id=session_id,
        summary=str(loaded.get("summary") or ""),
        turn_count=len(turns_raw),
        turns=turns,
    )


@app.get("/api/sessions", response_model=SessionListResponse)
def list_sessions(limit: int = 50) -> SessionListResponse:
    rows = session_store.list_sessions(limit=limit)
    return SessionListResponse(sessions=[SessionListItem(**row) for row in rows])


@app.post("/api/upload", response_model=UploadResponse)
async def upload(file: UploadFile = File(...)) -> UploadResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    meta = await upload_store.save_upload(file)
    max_bytes = config.max_upload_mb * 1024 * 1024
    if meta["size"] > max_bytes:
        upload_store.delete(meta["id"])
        raise HTTPException(status_code=413, detail=f"File too large (>{config.max_upload_mb}MB)")

    return UploadResponse(
        id=meta["id"],
        name=meta["original_name"],
        mime=meta["mime"],
        size=meta["size"],
        kind=meta["kind"],
    )


@app.get("/api/stats", response_model=TokenStatsResponse)
def get_stats() -> TokenStatsResponse:
    raw = token_stats_store.get_stats(max_records=500)
    sessions: dict[str, TokenTotals] = {}
    for session_id, totals in raw.get("sessions", {}).items():
        sessions[session_id] = TokenTotals(**totals)
    return TokenStatsResponse(
        totals=TokenTotals(**raw.get("totals", {})),
        sessions=sessions,
        records=raw.get("records", []),
    )


@app.post("/api/stats/clear", response_model=ClearStatsResponse)
def clear_stats() -> ClearStatsResponse:
    token_stats_store.clear()
    return ClearStatsResponse(ok=True)


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is required")

    session = session_store.load_or_create(req.session_id)
    agent = get_agent()
    summarized = agent.maybe_compact_session(session, req.settings.max_context_turns)

    attachments = upload_store.get_many(req.attachment_ids)
    found_attachment_ids = {str(item.get("id")) for item in attachments if item.get("id")}
    missing_attachment_ids = [file_id for file_id in req.attachment_ids if file_id not in found_attachment_ids]

    text, tool_events, attachment_note, execution_plan, execution_trace, debug_flow, token_usage = agent.run_chat(
        history_turns=session.get("turns", []),
        summary=session.get("summary", ""),
        user_message=req.message,
        attachment_metas=attachments,
        settings=req.settings,
    )
    if missing_attachment_ids:
        execution_trace.append(
            f"警告: {len(missing_attachment_ids)} 个附件未找到，可能已被清理或会话刷新，请重新上传。"
        )
        debug_flow.append(
            {
                "step": len(debug_flow) + 1,
                "stage": "backend_warning",
                "title": "附件检查",
                "detail": f"检测到 {len(missing_attachment_ids)} 个附件 ID 丢失，已提示前端重新上传。",
            }
        )

    user_text = req.message.strip()
    if attachment_note:
        user_text = f"{user_text}\n\n[附件] {attachment_note}"

    session_store.append_turn(
        session,
        role="user",
        text=user_text,
        attachments=[{"id": item.get("id"), "name": item.get("original_name")} for item in attachments],
    )
    session_store.append_turn(session, role="assistant", text=text)
    session_store.save(session)

    selected_model = req.settings.model or config.default_model
    pricing_meta = estimate_usage_cost(
        model=selected_model,
        input_tokens=token_usage.get("input_tokens", 0),
        output_tokens=token_usage.get("output_tokens", 0),
    )
    token_usage = {**token_usage, **pricing_meta}
    if pricing_meta.get("pricing_known"):
        execution_trace.append(
            "费用估算: "
            f"input ${pricing_meta.get('input_price_per_1m')}/1M, "
            f"output ${pricing_meta.get('output_price_per_1m')}/1M."
        )
        debug_flow.append(
            {
                "step": len(debug_flow) + 1,
                "stage": "backend_pricing",
                "title": "费用估算",
                "detail": (
                    f"按 {pricing_meta.get('pricing_model')} 计价："
                    f"in ${pricing_meta.get('input_price_per_1m')}/1M, "
                    f"out ${pricing_meta.get('output_price_per_1m')}/1M, "
                    f"本轮约 ${pricing_meta.get('estimated_cost_usd')}."
                ),
            }
        )
    else:
        execution_trace.append(f"费用估算未启用: 当前模型 {selected_model} 未匹配价格表。")
        debug_flow.append(
            {
                "step": len(debug_flow) + 1,
                "stage": "backend_pricing",
                "title": "费用估算",
                "detail": f"模型 {selected_model} 未匹配内置价格表，仅统计 token。",
            }
        )

    stats_snapshot = token_stats_store.add_usage(
        session_id=session["id"],
        usage=token_usage,
        model=selected_model,
    )
    session_totals_raw = stats_snapshot.get("sessions", {}).get(session["id"], {})
    global_totals_raw = stats_snapshot.get("totals", {})

    return ChatResponse(
        session_id=session["id"],
        text=text,
        tool_events=tool_events,
        execution_plan=execution_plan,
        execution_trace=execution_trace,
        debug_flow=debug_flow,
        missing_attachment_ids=missing_attachment_ids,
        token_usage=TokenUsage(**token_usage),
        session_token_totals=TokenTotals(**session_totals_raw),
        global_token_totals=TokenTotals(**global_totals_raw),
        turn_count=len(session.get("turns", [])),
        summarized=summarized,
    )
