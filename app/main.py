from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent import OfficeAgent
from app.config import load_config
from app.models import ChatRequest, ChatResponse, HealthResponse, NewSessionResponse, UploadResponse
from app.storage import SessionStore, UploadStore

config = load_config()
session_store = SessionStore(config.sessions_dir)
upload_store = UploadStore(config.uploads_dir)
_agent: OfficeAgent | None = None


def get_agent() -> OfficeAgent:
    global _agent
    if _agent is None:
        _agent = OfficeAgent(config)
    return _agent

app = FastAPI(title="Offciatool", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = (Path(__file__).resolve().parent / "static").resolve()
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


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

    text, tool_events, attachment_note, execution_plan, execution_trace = agent.run_chat(
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

    return ChatResponse(
        session_id=session["id"],
        text=text,
        tool_events=tool_events,
        execution_plan=execution_plan,
        execution_trace=execution_trace,
        missing_attachment_ids=missing_attachment_ids,
        turn_count=len(session.get("turns", [])),
        summarized=summarized,
    )
