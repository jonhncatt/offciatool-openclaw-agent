from __future__ import annotations

import json
import os
import queue
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
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


class AgentRunQueue:
    """
    OpenClaw-style lane queue:
    - one active run per session
    - bounded global concurrency across sessions
    """

    def __init__(self, max_concurrent_runs: int) -> None:
        self._global_sem = threading.BoundedSemaphore(max(1, int(max_concurrent_runs)))
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        sid = str(session_id or "").strip() or "__anon__"
        with self._locks_guard:
            lock = self._session_locks.get(sid)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[sid] = lock
            return lock

    def run_slot(self, session_id: str):
        sid = str(session_id or "").strip() or "__anon__"
        started = time.monotonic()
        session_lock = self._get_session_lock(sid)
        session_lock.acquire()
        self._global_sem.acquire()
        wait_ms = int((time.monotonic() - started) * 1000)
        return _AgentRunQueueTicket(self._global_sem, session_lock, wait_ms)


class _AgentRunQueueTicket:
    def __init__(
        self,
        global_sem: threading.BoundedSemaphore,
        session_lock: threading.Lock,
        wait_ms: int,
    ) -> None:
        self._global_sem = global_sem
        self._session_lock = session_lock
        self.wait_ms = max(0, int(wait_ms))
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._global_sem.release()
        finally:
            self._session_lock.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


run_queue = AgentRunQueue(config.max_concurrent_runs)


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
    agent = get_agent()
    docker_ok, docker_msg = agent.tools.docker_status()
    return HealthResponse(
        ok=True,
        model_default=config.default_model,
        execution_mode_default=config.execution_mode,
        docker_available=docker_ok,
        docker_message=docker_msg,
        web_allow_all_domains=config.web_allow_all_domains,
        web_allowed_domains=config.web_allowed_domains,
    )


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
    return _process_chat_request(req)


def _emit_progress(progress_cb: Callable[[dict[str, Any]], None] | None, event: str, **payload: Any) -> None:
    if not progress_cb:
        return
    try:
        progress_cb({"event": event, **payload})
    except Exception:
        pass


def _process_chat_request(
    req: ChatRequest, progress_cb: Callable[[dict[str, Any]], None] | None = None
) -> ChatResponse:
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is required")
    run_id = str(uuid.uuid4())
    _emit_progress(
        progress_cb,
        "stage",
        code="backend_start",
        detail=f"后端已接收请求，开始处理。run_id={run_id}",
        run_id=run_id,
    )

    seed_session = session_store.load_or_create(req.session_id)
    session_id = str(seed_session.get("id") or "")
    if not session_id:
        raise HTTPException(status_code=500, detail="Session create failed")

    queue_wait_ms = 0
    with run_queue.run_slot(session_id) as ticket:
        queue_wait_ms = int(ticket.wait_ms)
        if queue_wait_ms >= config.run_queue_wait_notice_ms:
            _emit_progress(
                progress_cb,
                "trace",
                message=f"当前会话存在并发请求，已排队等待 {queue_wait_ms} ms。",
                run_id=run_id,
            )

        session = session_store.load_or_create(session_id)
        _emit_progress(
            progress_cb,
            "stage",
            code="session_ready",
            detail=f"会话已就绪: {session.get('id')}",
            run_id=run_id,
            queue_wait_ms=queue_wait_ms,
        )
        agent = get_agent()
        summarized = agent.maybe_compact_session(session, req.settings.max_context_turns)
        if summarized:
            _emit_progress(progress_cb, "trace", message="历史上下文已自动压缩摘要。", run_id=run_id)

        attachments = upload_store.get_many(req.attachment_ids)
        _emit_progress(
            progress_cb,
            "stage",
            code="attachments_ready",
            detail=f"附件检查完成: 请求 {len(req.attachment_ids)} 个，命中 {len(attachments)} 个。",
            run_id=run_id,
        )
        found_attachment_ids = {str(item.get("id")) for item in attachments if item.get("id")}
        missing_attachment_ids = [file_id for file_id in req.attachment_ids if file_id not in found_attachment_ids]

        _emit_progress(progress_cb, "stage", code="agent_run_start", detail="开始模型推理与工具调度。", run_id=run_id)
        (
            text,
            tool_events,
            attachment_note,
            execution_plan,
            execution_trace,
            debug_flow,
            token_usage,
            effective_model,
        ) = agent.run_chat(
            history_turns=session.get("turns", []),
            summary=session.get("summary", ""),
            user_message=req.message,
            attachment_metas=attachments,
            settings=req.settings,
            session_id=session_id,
            progress_cb=progress_cb,
        )
        _emit_progress(progress_cb, "stage", code="agent_run_done", detail="模型推理结束，开始写入会话与统计。", run_id=run_id)
        if missing_attachment_ids:
            warning_msg = f"警告: {len(missing_attachment_ids)} 个附件未找到，可能已被清理或会话刷新，请重新上传。"
            execution_trace.append(warning_msg)
            _emit_progress(progress_cb, "trace", message=warning_msg, index=len(execution_trace), run_id=run_id)

            debug_item = {
                "step": len(debug_flow) + 1,
                "stage": "backend_warning",
                "title": "附件检查",
                "detail": f"检测到 {len(missing_attachment_ids)} 个附件 ID 丢失，已提示前端重新上传。",
            }
            debug_flow.append(debug_item)
            _emit_progress(progress_cb, "debug", item=debug_item, run_id=run_id)

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
        _emit_progress(progress_cb, "stage", code="session_saved", detail="会话已写入本地存储。", run_id=run_id)

        selected_model = effective_model or req.settings.model or config.default_model
        pricing_meta = estimate_usage_cost(
            model=selected_model,
            input_tokens=token_usage.get("input_tokens", 0),
            output_tokens=token_usage.get("output_tokens", 0),
        )
        token_usage = {**token_usage, **pricing_meta}
        if pricing_meta.get("pricing_known"):
            pricing_trace = (
                "费用估算: "
                f"input ${pricing_meta.get('input_price_per_1m')}/1M, "
                f"output ${pricing_meta.get('output_price_per_1m')}/1M."
            )
            execution_trace.append(pricing_trace)
            _emit_progress(progress_cb, "trace", message=pricing_trace, index=len(execution_trace), run_id=run_id)

            pricing_debug = {
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
            debug_flow.append(pricing_debug)
            _emit_progress(progress_cb, "debug", item=pricing_debug, run_id=run_id)
        else:
            pricing_trace = f"费用估算未启用: 当前模型 {selected_model} 未匹配价格表。"
            execution_trace.append(pricing_trace)
            _emit_progress(progress_cb, "trace", message=pricing_trace, index=len(execution_trace), run_id=run_id)

            pricing_debug = {
                "step": len(debug_flow) + 1,
                "stage": "backend_pricing",
                "title": "费用估算",
                "detail": f"模型 {selected_model} 未匹配内置价格表，仅统计 token。",
            }
            debug_flow.append(pricing_debug)
            _emit_progress(progress_cb, "debug", item=pricing_debug, run_id=run_id)

        stats_snapshot = token_stats_store.add_usage(
            session_id=session["id"],
            usage=token_usage,
            model=selected_model,
        )
        _emit_progress(progress_cb, "stage", code="stats_saved", detail="Token 统计已更新。", run_id=run_id)
        session_totals_raw = stats_snapshot.get("sessions", {}).get(session["id"], {})
        global_totals_raw = stats_snapshot.get("totals", {})
        response = ChatResponse(
            session_id=session["id"],
            run_id=run_id,
            effective_model=selected_model,
            queue_wait_ms=queue_wait_ms,
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
        _emit_progress(progress_cb, "stage", code="ready", detail="本轮结果已准备完成。", run_id=run_id)
        return response


def _sse_pack(event: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {raw}\n\n"


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    def event_stream():
        events: queue.Queue[dict[str, Any]] = queue.Queue()
        done_event = threading.Event()

        def emit(payload: dict[str, Any]) -> None:
            event_name = str(payload.get("event") or "message")
            data = {k: v for k, v in payload.items() if k != "event"}
            events.put({"event": event_name, "payload": data})

        def worker() -> None:
            try:
                response = _process_chat_request(req, progress_cb=emit)
                events.put({"event": "final", "payload": {"response": response.model_dump()}})
            except HTTPException as exc:
                events.put(
                    {
                        "event": "error",
                        "payload": {"status_code": exc.status_code, "detail": str(exc.detail or "HTTP error")},
                    }
                )
            except Exception as exc:
                events.put({"event": "error", "payload": {"status_code": 500, "detail": str(exc)}})
            finally:
                done_event.set()
                events.put({"event": "done", "payload": {"ok": True}})

        threading.Thread(target=worker, daemon=True).start()

        while True:
            try:
                item = events.get(timeout=10.0)
            except queue.Empty:
                yield _sse_pack("heartbeat", {"ts": int(time.time())})
                if done_event.is_set():
                    break
                continue
            event_name = str(item.get("event") or "message")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            yield _sse_pack(event_name, payload)
            if event_name == "done":
                break

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
