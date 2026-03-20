from __future__ import annotations

import copy
import json
import os
import queue
from pathlib import Path
import subprocess
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
from app.core.bootstrap import build_kernel_runtime
from app.core.healthcheck import build_kernel_health_payload
from app.evals import run_regression_evals
from app.models import (
    ChatRequest,
    ChatResponse,
    ClearStatsResponse,
    DeleteSessionResponse,
    EvalCaseResult,
    EvalRunRequest,
    EvalRunResponse,
    HealthResponse,
    KernelManifestUpdateRequest,
    KernelShadowPipelineRequest,
    KernelShadowAutoRepairRequest,
    KernelShadowPackageRequest,
    KernelShadowPatchWorkerRequest,
    KernelShadowReplayRequest,
    KernelShadowSelfUpgradeRequest,
    KernelRuntimeResponse,
    KernelShadowSmokeRequest,
    NewSessionResponse,
    SessionDetailResponse,
    SessionListItem,
    SessionListResponse,
    SessionTurn,
    UpdateSessionTitleRequest,
    UpdateSessionTitleResponse,
    SandboxDrillRequest,
    SandboxDrillResponse,
    SandboxDrillStep,
    TokenStatsResponse,
    TokenTotals,
    TokenUsage,
    UploadResponse,
)
from app.openai_auth import OpenAIAuthManager
from app.pricing import estimate_usage_cost
from app import session_context as session_context_impl
from app.session_context import normalize_attachment_ids
from app.storage import SessionStore, ShadowLogStore, TokenStatsStore, UploadStore

config = load_config()
session_store = SessionStore(config.sessions_dir)
upload_store = UploadStore(config.uploads_dir)
token_stats_store = TokenStatsStore(config.token_stats_path)
shadow_log_store = ShadowLogStore(config.shadow_logs_dir)
kernel_runtime = build_kernel_runtime(config)
_agent: OfficeAgent | None = None
APP_VERSION = "0.3.5"


def _resolve_build_version() -> str:
    override = str(os.environ.get("OFFICETOOL_BUILD_VERSION") or "").strip()
    if override:
        return override

    repo_root = Path(__file__).resolve().parent.parent
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            ).stdout.strip()
        )
    except Exception:
        commit = ""
    try:
        branch = (
            subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            ).stdout.strip()
        )
    except Exception:
        branch = ""

    parts = [f"v{APP_VERSION}"]
    if branch and commit:
        parts.append(f"{branch}@{commit}")
    elif commit:
        parts.append(commit)
    return " · ".join(parts)


BUILD_VERSION = _resolve_build_version()


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


def get_kernel_runtime():
    return kernel_runtime


def get_agent() -> OfficeAgent:
    global _agent
    if _agent is None:
        _agent = OfficeAgent(config, kernel_runtime=get_kernel_runtime())
    return _agent

app = FastAPI(title="Officetool", version=APP_VERSION)

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
    auth_summary = OpenAIAuthManager(config).auth_summary()
    kernel_health = build_kernel_health_payload(get_kernel_runtime())
    tool_registry = agent._debug_tool_registry_snapshot()
    return HealthResponse(
        ok=True,
        app_version=APP_VERSION,
        build_version=BUILD_VERSION,
        model_default=config.default_model,
        auth_mode=str(auth_summary.get("mode") or ""),
        execution_mode_default=config.execution_mode,
        docker_available=docker_ok,
        docker_message=docker_msg,
        platform_name=config.platform_name,
        workspace_root=str(config.workspace_root),
        allow_any_path=config.allow_any_path,
        allowed_roots=[str(path) for path in config.allowed_roots],
        default_extra_allowed_roots=[str(path) for path in config.default_extra_allowed_roots],
        extra_allowed_roots_source=config.extra_allowed_roots_source,
        web_allow_all_domains=config.web_allow_all_domains,
        web_allowed_domains=config.web_allowed_domains,
        kernel_active_manifest=dict(kernel_health.get("active_manifest") or {}),
        kernel_shadow_manifest=dict(kernel_health.get("shadow_manifest") or {}),
        kernel_shadow_validation=dict(kernel_health.get("shadow_validation") or {}),
        kernel_shadow_promote_check=dict(kernel_health.get("shadow_promote_check") or {}),
        kernel_rollback_pointer=dict(kernel_health.get("rollback_pointer") or {}),
        kernel_last_shadow_run=dict(kernel_health.get("last_shadow_run") or {}),
        kernel_last_upgrade_run=dict(kernel_health.get("last_upgrade_run") or {}),
        kernel_last_repair_run=dict(kernel_health.get("last_repair_run") or {}),
        kernel_last_patch_worker_run=dict(kernel_health.get("last_patch_worker_run") or {}),
        kernel_last_package_run=dict(kernel_health.get("last_package_run") or {}),
        kernel_selected_modules=dict(kernel_health.get("selected_modules") or {}),
        kernel_module_health=dict(kernel_health.get("module_health") or {}),
        kernel_runtime_files=dict(kernel_health.get("runtime_files") or {}),
        kernel_tool_registry=dict(tool_registry or {}),
    )


def _kernel_runtime_response(
    *,
    ok: bool,
    detail: str = "",
    validation: dict[str, object] | None = None,
    contracts: dict[str, object] | None = None,
    smoke: dict[str, object] | None = None,
    replay: dict[str, object] | None = None,
    pipeline: dict[str, object] | None = None,
    repair: dict[str, object] | None = None,
    patch_worker: dict[str, object] | None = None,
) -> KernelRuntimeResponse:
    kernel_health = build_kernel_health_payload(get_kernel_runtime())
    tool_registry = get_agent()._debug_tool_registry_snapshot()
    return KernelRuntimeResponse(
        ok=ok,
        detail=detail,
        validation=dict(validation or {}),
        contracts=dict(contracts or {}),
        smoke=dict(smoke or {}),
        replay=dict(replay or {}),
        pipeline=dict(pipeline or {}),
        repair=dict(repair or {}),
        patch_worker=dict(patch_worker or {}),
        kernel_active_manifest=dict(kernel_health.get("active_manifest") or {}),
        kernel_shadow_manifest=dict(kernel_health.get("shadow_manifest") or {}),
        kernel_shadow_validation=dict(kernel_health.get("shadow_validation") or {}),
        kernel_shadow_promote_check=dict(kernel_health.get("shadow_promote_check") or {}),
        kernel_rollback_pointer=dict(kernel_health.get("rollback_pointer") or {}),
        kernel_last_shadow_run=dict(kernel_health.get("last_shadow_run") or {}),
        kernel_last_upgrade_run=dict(kernel_health.get("last_upgrade_run") or {}),
        kernel_last_repair_run=dict(kernel_health.get("last_repair_run") or {}),
        kernel_last_patch_worker_run=dict(kernel_health.get("last_patch_worker_run") or {}),
        kernel_last_package_run=dict(kernel_health.get("last_package_run") or {}),
        kernel_selected_modules=dict(kernel_health.get("selected_modules") or {}),
        kernel_module_health=dict(kernel_health.get("module_health") or {}),
        kernel_runtime_files=dict(kernel_health.get("runtime_files") or {}),
        kernel_tool_registry=dict(tool_registry or {}),
    )


def _find_shadow_replay_record(run_id: str | None = None) -> dict[str, Any] | None:
    run_id_text = str(run_id or "").strip()
    if run_id_text:
        return shadow_log_store.find_run(run_id_text)
    recent = shadow_log_store.list_recent(limit=1)
    return recent[0] if recent else None


def _find_upgrade_run(run_id: str | None = None) -> dict[str, Any] | None:
    runtime = get_kernel_runtime()
    payload = runtime.find_upgrade_run(run_id)
    return payload if isinstance(payload, dict) and payload else None


def _find_repair_run(run_id: str | None = None) -> dict[str, Any] | None:
    runtime = get_kernel_runtime()
    payload = runtime.find_repair_run(run_id)
    return payload if isinstance(payload, dict) and payload else None


@app.get("/api/kernel/runtime", response_model=KernelRuntimeResponse)
def kernel_runtime_state() -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    return _kernel_runtime_response(
        ok=True,
        detail="内核运行时状态。",
        validation=runtime.validate_shadow_manifest(),
    )


@app.get("/api/kernel/repairs", response_model=KernelRuntimeResponse)
def kernel_repair_history(limit: int = 10) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    runs = runtime.list_repair_runs(limit=limit)
    summary = [
        {
            "run_id": str(item.get("run_id") or ""),
            "ok": bool(item.get("ok")),
            "base_upgrade_run_id": str(item.get("base_upgrade_run_id") or ""),
            "strategy": str(item.get("strategy") or ""),
            "attempt_count": len(item.get("attempts") or []) if isinstance(item.get("attempts"), list) else 0,
            "finished_at": str(item.get("finished_at") or ""),
        }
        for item in runs
    ]
    return _kernel_runtime_response(
        ok=True,
        detail="最近 repair attempts。",
        repair={"repair_runs": summary},
    )


@app.get("/api/kernel/patch-workers", response_model=KernelRuntimeResponse)
def kernel_patch_worker_history(limit: int = 10) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    runs = runtime.list_patch_worker_runs(limit=limit)
    summary = [
        {
            "run_id": str(item.get("run_id") or ""),
            "ok": bool(item.get("ok")),
            "base_repair_run_id": str(item.get("base_repair_run_id") or ""),
            "task_count": len(item.get("executed_tasks") or []) if isinstance(item.get("executed_tasks"), list) else 0,
            "round_count": int(item.get("round_count") or 0),
            "stop_reason": str(item.get("stop_reason") or ""),
            "finished_at": str(item.get("finished_at") or ""),
        }
        for item in runs
    ]
    return _kernel_runtime_response(
        ok=True,
        detail="最近 patch worker runs。",
        patch_worker={"patch_worker_runs": summary},
    )


@app.get("/api/kernel/packages", response_model=KernelRuntimeResponse)
def kernel_package_history(limit: int = 10) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    runs = runtime.list_package_runs(limit=limit)
    summary = [
        {
            "run_id": str(item.get("run_id") or ""),
            "ok": bool(item.get("ok")),
            "packaged_count": len(item.get("packaged_modules") or []) if isinstance(item.get("packaged_modules"), list) else 0,
            "packaged_labels": list(item.get("packaged_labels") or []) if isinstance(item.get("packaged_labels"), list) else [],
            "finished_at": str(item.get("finished_at") or ""),
        }
        for item in runs
    ]
    return _kernel_runtime_response(
        ok=True,
        detail="最近 package runs。",
        pipeline={"package_runs": summary},
    )


@app.get("/api/kernel/upgrades", response_model=KernelRuntimeResponse)
def kernel_upgrade_history(limit: int = 10) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    runs = runtime.list_upgrade_runs(limit=limit)
    summary = [
        {
            "run_id": str(item.get("run_id") or ""),
            "ok": bool(item.get("ok")),
            "started_at": str(item.get("started_at") or ""),
            "finished_at": str(item.get("finished_at") or ""),
            "failed_stage": str(((item.get("failure_classification") or {}) if isinstance(item.get("failure_classification"), dict) else {}).get("failed_stage") or ""),
            "category": str(((item.get("failure_classification") or {}) if isinstance(item.get("failure_classification"), dict) else {}).get("category") or ""),
        }
        for item in runs
    ]
    return _kernel_runtime_response(
        ok=True,
        detail="最近 upgrade attempts。",
        pipeline={"upgrade_runs": summary},
    )


@app.post("/api/kernel/shadow/stage", response_model=KernelRuntimeResponse)
def kernel_shadow_stage(req: KernelManifestUpdateRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    result = runtime.stage_shadow_manifest(overrides=req.model_dump(exclude_none=True))
    return _kernel_runtime_response(
        ok=bool(result.get("ok")),
        detail="shadow manifest 已更新。",
        validation=result.get("validation") if isinstance(result.get("validation"), dict) else {},
    )


@app.post("/api/kernel/shadow/validate", response_model=KernelRuntimeResponse)
def kernel_shadow_validate() -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    validation = runtime.validate_shadow_manifest()
    return _kernel_runtime_response(
        ok=bool(validation.get("ok")),
        detail="shadow manifest 校验完成。",
        validation=validation,
    )


@app.get("/api/kernel/shadow/promote-check", response_model=KernelRuntimeResponse)
def kernel_shadow_promote_check() -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    promote_check = runtime.shadow_promote_check()
    return _kernel_runtime_response(
        ok=bool(promote_check.get("ok")),
        detail="shadow promote 检查完成。",
        validation=runtime.validate_shadow_manifest(),
        pipeline={"promote_check": promote_check},
    )


@app.post("/api/kernel/shadow/contracts", response_model=KernelRuntimeResponse)
def kernel_shadow_contracts() -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    contracts = runtime.run_shadow_contracts()
    validation = runtime.validate_shadow_manifest()
    return _kernel_runtime_response(
        ok=bool(contracts.get("ok")),
        detail="shadow contracts 已执行。",
        validation=validation,
        contracts=contracts,
    )


@app.post("/api/kernel/shadow/smoke", response_model=KernelRuntimeResponse)
def kernel_shadow_smoke(req: KernelShadowSmokeRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    smoke = runtime.run_shadow_smoke(
        user_message=req.message,
        validate_provider=bool(req.validate_provider),
    )
    return _kernel_runtime_response(
        ok=bool(smoke.get("ok")),
        detail="shadow smoke 已执行。",
        validation=runtime.validate_shadow_manifest(),
        smoke=smoke,
    )


@app.get("/api/kernel/shadow/logs", response_model=KernelRuntimeResponse)
def kernel_shadow_logs(limit: int = 10) -> KernelRuntimeResponse:
    records = shadow_log_store.list_recent(limit=limit)
    summary = [
        {
            "run_id": str(item.get("run_id") or ""),
            "logged_at": str(item.get("logged_at") or ""),
            "session_id": str(item.get("session_id") or ""),
            "message_preview": str(item.get("message_preview") or ""),
            "effective_model": str(item.get("effective_model") or ""),
        }
        for item in records
    ]
    return _kernel_runtime_response(
        ok=True,
        detail="最近 shadow log 列表。",
        pipeline={"recent_runs": summary},
    )


@app.post("/api/kernel/shadow/replay", response_model=KernelRuntimeResponse)
def kernel_shadow_replay(req: KernelShadowReplayRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    record = _find_shadow_replay_record(req.run_id)
    if not isinstance(record, dict):
        return _kernel_runtime_response(
            ok=False,
            detail="未找到可回放的 shadow log 记录。",
        )
    replay = runtime.run_shadow_replay(replay_record=record)
    return _kernel_runtime_response(
        ok=bool(replay.get("ok")),
        detail="shadow replay 已执行。",
        validation=runtime.validate_shadow_manifest(),
        replay=replay,
    )


@app.post("/api/kernel/shadow/promote", response_model=KernelRuntimeResponse)
def kernel_shadow_promote() -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    result = runtime.promote_shadow_manifest()
    return _kernel_runtime_response(
        ok=bool(result.get("ok")),
        detail="shadow manifest promote 完成。" if result.get("ok") else "shadow manifest promote 失败。",
        validation=result.get("validation") if isinstance(result.get("validation"), dict) else {},
    )


@app.post("/api/kernel/rollback", response_model=KernelRuntimeResponse)
def kernel_runtime_rollback() -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    result = runtime.rollback_active_manifest()
    return _kernel_runtime_response(
        ok=bool(result.get("ok")),
        detail="active manifest 回滚完成。" if result.get("ok") else "active manifest 回滚失败。",
        validation=result.get("validation") if isinstance(result.get("validation"), dict) else {},
    )


@app.post("/api/kernel/shadow/pipeline", response_model=KernelRuntimeResponse)
def kernel_shadow_pipeline(req: KernelShadowPipelineRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    overrides = req.model_dump(
        exclude_none=True,
        include={"router", "policy", "attachment_context", "finalizer", "tool_registry", "providers"},
    )
    replay_record = _find_shadow_replay_record(req.replay_run_id) if (req.replay_run_id or shadow_log_store.list_recent(limit=1)) else None
    pipeline = runtime.run_shadow_pipeline(
        overrides=overrides,
        smoke_message=req.smoke_message,
        validate_provider=bool(req.validate_provider),
        replay_record=replay_record if isinstance(replay_record, dict) else None,
        promote_if_healthy=bool(req.promote_if_healthy),
    )
    validation = pipeline.get("validation") if isinstance(pipeline.get("validation"), dict) else {}
    contracts = pipeline.get("contracts") if isinstance(pipeline.get("contracts"), dict) else {}
    smoke = pipeline.get("smoke") if isinstance(pipeline.get("smoke"), dict) else {}
    replay = pipeline.get("replay") if isinstance(pipeline.get("replay"), dict) else {}

    return _kernel_runtime_response(
        ok=bool(pipeline.get("ok")),
        detail="shadow pipeline 已执行。",
        validation=validation,
        contracts=contracts,
        smoke=smoke,
        replay=replay,
        pipeline=pipeline,
    )


@app.post("/api/kernel/shadow/auto-repair", response_model=KernelRuntimeResponse)
def kernel_shadow_auto_repair(req: KernelShadowAutoRepairRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    base_upgrade_run = _find_upgrade_run(req.upgrade_run_id)
    if not isinstance(base_upgrade_run, dict):
        return _kernel_runtime_response(
            ok=False,
            detail="未找到可修复的 upgrade attempt。",
        )
    replay_source_run_id = req.replay_run_id or str(base_upgrade_run.get("replay_source_run_id") or "").strip() or None
    replay_record = _find_shadow_replay_record(replay_source_run_id)
    repair = runtime.run_shadow_auto_repair(
        base_upgrade_run=base_upgrade_run,
        replay_record=replay_record if isinstance(replay_record, dict) else None,
        smoke_message=req.smoke_message,
        validate_provider=req.validate_provider,
        promote_if_healthy=req.promote_if_healthy,
        max_attempts=req.max_attempts,
    )
    repaired_pipeline = repair.get("repaired_pipeline") if isinstance(repair.get("repaired_pipeline"), dict) else {}
    validation = repaired_pipeline.get("validation") if isinstance(repaired_pipeline.get("validation"), dict) else runtime.validate_shadow_manifest()
    contracts = repaired_pipeline.get("contracts") if isinstance(repaired_pipeline.get("contracts"), dict) else {}
    smoke = repaired_pipeline.get("smoke") if isinstance(repaired_pipeline.get("smoke"), dict) else {}
    replay = repaired_pipeline.get("replay") if isinstance(repaired_pipeline.get("replay"), dict) else {}
    return _kernel_runtime_response(
        ok=bool(repair.get("ok")),
        detail="shadow auto-repair 已执行。",
        validation=validation,
        contracts=contracts,
        smoke=smoke,
        replay=replay,
        repair=repair,
    )


@app.post("/api/kernel/shadow/patch-worker", response_model=KernelRuntimeResponse)
def kernel_shadow_patch_worker(req: KernelShadowPatchWorkerRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    repair_run = _find_repair_run(req.repair_run_id)
    if not isinstance(repair_run, dict):
        return _kernel_runtime_response(
            ok=False,
            detail="未找到可执行 patch worker 的 repair run。",
        )
    replay_source_run_id = req.replay_run_id or str((repair_run.get("repaired_pipeline") or {}).get("replay_source_run_id") or "").strip() or None
    replay_record = _find_shadow_replay_record(replay_source_run_id)
    patch_worker = runtime.run_shadow_patch_worker(
        repair_run=repair_run,
        replay_record=replay_record if isinstance(replay_record, dict) else None,
        max_tasks=req.max_tasks,
        max_rounds=req.max_rounds,
        auto_package_on_success=bool(req.auto_package_on_success),
        promote_if_healthy=req.promote_if_healthy,
    )
    pipeline = patch_worker.get("pipeline") if isinstance(patch_worker.get("pipeline"), dict) else {}
    validation = pipeline.get("validation") if isinstance(pipeline.get("validation"), dict) else runtime.validate_shadow_manifest()
    contracts = pipeline.get("contracts") if isinstance(pipeline.get("contracts"), dict) else {}
    smoke = pipeline.get("smoke") if isinstance(pipeline.get("smoke"), dict) else {}
    replay = pipeline.get("replay") if isinstance(pipeline.get("replay"), dict) else {}
    return _kernel_runtime_response(
        ok=bool(patch_worker.get("ok")),
        detail="shadow patch worker 已执行。",
        validation=validation,
        contracts=contracts,
        smoke=smoke,
        replay=replay,
        pipeline=pipeline,
        patch_worker=patch_worker,
    )


@app.post("/api/kernel/shadow/package", response_model=KernelRuntimeResponse)
def kernel_shadow_package(req: KernelShadowPackageRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    package_run = runtime.package_shadow_modules(
        labels=req.labels,
        package_note=req.package_note,
        source_run_id=str(req.source_run_id or ""),
        repair_run_id=str(req.repair_run_id or ""),
        patch_worker_run_id=str(req.patch_worker_run_id or ""),
        runtime_profile=req.runtime_profile,
    )
    validation = package_run.get("validation") if isinstance(package_run.get("validation"), dict) else runtime.validate_shadow_manifest()
    return _kernel_runtime_response(
        ok=bool(package_run.get("ok")),
        detail="shadow modules 已打包为正式版本。" if package_run.get("ok") else "shadow modules 打包失败。",
        validation=validation,
        pipeline={"package_run": package_run},
    )


@app.post("/api/kernel/shadow/self-upgrade", response_model=KernelRuntimeResponse)
def kernel_shadow_self_upgrade(req: KernelShadowSelfUpgradeRequest) -> KernelRuntimeResponse:
    runtime = get_kernel_runtime()
    base_upgrade_run = _find_upgrade_run(req.upgrade_run_id)
    if not isinstance(base_upgrade_run, dict):
        return _kernel_runtime_response(
            ok=False,
            detail="未找到可执行 self-upgrade 的 upgrade run。",
        )
    replay_source_run_id = req.replay_run_id or str(base_upgrade_run.get("replay_source_run_id") or "").strip() or None
    replay_record = _find_shadow_replay_record(replay_source_run_id)
    self_upgrade = runtime.run_shadow_self_upgrade(
        base_upgrade_run=base_upgrade_run,
        replay_record=replay_record if isinstance(replay_record, dict) else None,
        smoke_message=req.smoke_message,
        validate_provider=req.validate_provider,
        max_attempts=req.max_attempts,
        max_tasks=req.max_tasks,
        max_rounds=req.max_rounds,
        promote_if_healthy=bool(req.promote_if_healthy),
    )
    final_pipeline = self_upgrade.get("final_pipeline") if isinstance(self_upgrade.get("final_pipeline"), dict) else {}
    validation = final_pipeline.get("validation") if isinstance(final_pipeline.get("validation"), dict) else runtime.validate_shadow_manifest()
    contracts = final_pipeline.get("contracts") if isinstance(final_pipeline.get("contracts"), dict) else {}
    smoke = final_pipeline.get("smoke") if isinstance(final_pipeline.get("smoke"), dict) else {}
    replay = final_pipeline.get("replay") if isinstance(final_pipeline.get("replay"), dict) else {}
    return _kernel_runtime_response(
        ok=bool(self_upgrade.get("ok")),
        detail="shadow self-upgrade 已执行。",
        validation=validation,
        contracts=contracts,
        smoke=smoke,
        replay=replay,
        pipeline={"self_upgrade": self_upgrade},
        repair=self_upgrade.get("repair") if isinstance(self_upgrade.get("repair"), dict) else {},
        patch_worker=self_upgrade.get("patch_worker") if isinstance(self_upgrade.get("patch_worker"), dict) else {},
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


@app.patch("/api/session/{session_id}/title", response_model=UpdateSessionTitleResponse)
def update_session_title(session_id: str, req: UpdateSessionTitleRequest) -> UpdateSessionTitleResponse:
    loaded = session_store.load(session_id)
    if not loaded:
        raise HTTPException(status_code=404, detail="Session not found")

    title = str(req.title or "").strip()[:120]
    loaded["title"] = title
    session_store.save(loaded)
    return UpdateSessionTitleResponse(ok=True, session_id=session_id, title=title)


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
                answer_bundle=item.get("answer_bundle") or {},
                created_at=str(item.get("created_at")) if item.get("created_at") else None,
            )
        )

    return SessionDetailResponse(
        session_id=session_id,
        title=str(loaded.get("title") or ""),
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


def _resolve_execution_mode(requested_mode: str | None) -> str:
    mode = str(requested_mode or "").strip().lower()
    if mode in {"host", "docker"}:
        return mode
    return config.execution_mode


def _append_drill_step(
    steps: list[SandboxDrillStep],
    *,
    name: str,
    ok: bool,
    detail: str,
    started_at: float,
) -> None:
    steps.append(
        SandboxDrillStep(
            name=name,
            ok=bool(ok),
            detail=str(detail),
            duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        )
    )


@app.post("/api/sandbox/drill", response_model=SandboxDrillResponse)
def sandbox_drill(req: SandboxDrillRequest) -> SandboxDrillResponse:
    run_id = str(uuid.uuid4())
    execution_mode = _resolve_execution_mode(req.execution_mode)
    agent = get_agent()
    docker_ok, docker_msg = agent.tools.docker_status()
    steps: list[SandboxDrillStep] = []
    failed = 0
    drill_session_id = f"__drill__{run_id}"
    pwd_result: dict[str, Any] | None = None

    started = time.perf_counter()
    _append_drill_step(
        steps,
        name="runtime_context",
        ok=True,
        detail=f"run_id={run_id}, execution_mode={execution_mode}, session_id={drill_session_id}",
        started_at=started,
    )

    if execution_mode == "docker":
        started = time.perf_counter()
        docker_step_ok = bool(docker_ok)
        _append_drill_step(
            steps,
            name="docker_ready",
            ok=docker_step_ok,
            detail=docker_msg or ("Docker server ready." if docker_step_ok else "Docker unavailable."),
            started_at=started,
        )
        if not docker_step_ok:
            failed += 1

    agent.tools.set_runtime_context(execution_mode=execution_mode, session_id=drill_session_id)
    try:
        started = time.perf_counter()
        list_result = agent.tools.list_directory(path=".", max_entries=20)
        list_ok = bool(list_result.get("ok"))
        list_detail = (
            f"path={list_result.get('path', '')}, entries={len(list_result.get('entries') or [])}"
            if list_ok
            else str(list_result.get("error") or "list_directory failed")
        )
        _append_drill_step(
            steps,
            name="list_directory",
            ok=list_ok,
            detail=list_detail,
            started_at=started,
        )
        if not list_ok:
            failed += 1

        started = time.perf_counter()
        pwd_result = agent.tools.run_shell(command="pwd", cwd=".", timeout_sec=12)
        pwd_ok = bool(pwd_result.get("ok"))
        pwd_detail = (
            f"mode={pwd_result.get('execution_mode')}, host_cwd={pwd_result.get('host_cwd')}, "
            f"sandbox_cwd={pwd_result.get('sandbox_cwd') or '-'}"
            if pwd_ok
            else str(pwd_result.get("error") or "run_shell pwd failed")
        )
        _append_drill_step(
            steps,
            name="run_shell_pwd",
            ok=pwd_ok,
            detail=pwd_detail,
            started_at=started,
        )
        if not pwd_ok:
            failed += 1

        started = time.perf_counter()
        if "python3" in config.allowed_commands:
            py_result = agent.tools.run_shell(command="python3 --version", cwd=".", timeout_sec=12)
            py_ok = bool(py_result.get("ok"))
            py_out = str(py_result.get("stdout") or py_result.get("stderr") or "").strip().splitlines()
            py_detail = py_out[0] if py_out else (
                str(py_result.get("error") or "python3 --version failed") if not py_ok else "python3 ok"
            )
            _append_drill_step(
                steps,
                name="run_shell_python3_version",
                ok=py_ok,
                detail=py_detail,
                started_at=started,
            )
            if not py_ok:
                failed += 1
        else:
            _append_drill_step(
                steps,
                name="run_shell_python3_version",
                ok=True,
                detail="skipped: python3 is not in OFFICETOOL_ALLOWED_COMMANDS",
                started_at=started,
            )

        if execution_mode == "docker":
            started = time.perf_counter()
            mapping_ok = False
            mapping_detail = "missing docker pwd result"
            if isinstance(pwd_result, dict) and pwd_result.get("ok"):
                mode = str(pwd_result.get("execution_mode") or "").strip().lower()
                host_cwd = str(pwd_result.get("host_cwd") or "").strip()
                sandbox_cwd = str(pwd_result.get("sandbox_cwd") or "").strip()
                mounts = pwd_result.get("mount_mappings") if isinstance(pwd_result.get("mount_mappings"), list) else []
                mapping_ok = mode == "docker" and bool(host_cwd) and bool(sandbox_cwd) and bool(mounts)
                mapping_detail = (
                    f"mode={mode}, host_cwd={host_cwd}, sandbox_cwd={sandbox_cwd}, mount_count={len(mounts)}"
                )
            _append_drill_step(
                steps,
                name="docker_path_mapping",
                ok=mapping_ok,
                detail=mapping_detail,
                started_at=started,
            )
            if not mapping_ok:
                failed += 1
    finally:
        agent.tools.clear_runtime_context()

    if failed == 0:
        summary = f"沙盒演练通过（{len(steps)} 步）。"
    else:
        summary = f"沙盒演练发现 {failed} 个失败步骤（共 {len(steps)} 步）。"

    return SandboxDrillResponse(
        ok=failed == 0,
        run_id=run_id,
        execution_mode=execution_mode,
        docker_available=docker_ok,
        docker_message=docker_msg,
        summary=summary,
        steps=steps,
    )


@app.post("/api/evals/run", response_model=EvalRunResponse)
def run_evals(req: EvalRunRequest) -> EvalRunResponse:
    run_id = str(uuid.uuid4())
    summary = run_regression_evals(
        include_optional=bool(req.include_optional),
        name_filter=str(req.name_filter or "").strip(),
    )
    passed = int(summary.get("passed") or 0)
    failed = int(summary.get("failed") or 0)
    skipped = int(summary.get("skipped") or 0)
    total = int(summary.get("total") or 0)
    duration_ms = int(summary.get("duration_ms") or 0)
    summary_text = (
        f"回归测试通过：passed={passed}, failed={failed}, skipped={skipped}, total={total}"
        if failed == 0
        else f"回归测试失败：passed={passed}, failed={failed}, skipped={skipped}, total={total}"
    )
    return EvalRunResponse(
        ok=bool(summary.get("ok")),
        run_id=run_id,
        include_optional=bool(summary.get("include_optional")),
        name_filter=str(summary.get("name_filter") or ""),
        cases_path=str(summary.get("cases_path") or ""),
        passed=passed,
        failed=failed,
        skipped=skipped,
        total=total,
        duration_ms=duration_ms,
        summary=summary_text,
        results=[EvalCaseResult(**item) for item in summary.get("results") or [] if isinstance(item, dict)],
    )


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
    auth_summary = OpenAIAuthManager(config).auth_summary()
    if not bool(auth_summary.get("available")):
        raise HTTPException(status_code=500, detail=str(auth_summary.get("reason") or "OpenAI credentials are required"))
    run_id = str(uuid.uuid4())
    _emit_progress(
        progress_cb,
        "stage",
        code="backend_start",
        detail=f"后端已接收请求，开始处理。run_id={run_id}, auth_mode={auth_summary.get('mode')}",
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
        history_turns_before = copy.deepcopy(session.get("turns", []))
        summary_before = str(session.get("summary", "") or "")
        summarized = agent.maybe_compact_session(session, req.settings.max_context_turns)
        if summarized:
            _emit_progress(progress_cb, "trace", message="历史上下文已自动压缩摘要。", run_id=run_id)

        runtime = get_kernel_runtime()
        attachment_registry = runtime.registry
        attachment_module = attachment_registry.attachment_context
        attachment_selected_ref = str((attachment_registry.selected_refs or {}).get("attachment_context") or "")
        attachment_fallback_ref = "attachment_context@1.0.0"
        try:
            attachment_context = attachment_module.resolve_attachment_context(
                session=session,
                message=req.message,
                requested_attachment_ids=req.attachment_ids,
            )
            runtime.record_module_success(
                kind="attachment_context",
                selected_ref=attachment_selected_ref or attachment_fallback_ref,
            )
        except Exception as exc:
            runtime.record_module_failure(
                kind="attachment_context",
                requested_ref=attachment_selected_ref or attachment_fallback_ref,
                fallback_ref=attachment_fallback_ref,
                error=str(exc),
            )
            attachment_context = session_context_impl.resolve_attachment_context(
                session,
                message=req.message,
                requested_attachment_ids=req.attachment_ids,
            )
        requested_attachment_ids = attachment_context["requested_attachment_ids"]
        clear_attachment_context = bool(attachment_context["clear_attachment_context"])
        attachment_context_mode = str(attachment_context["attachment_context_mode"] or "none")
        auto_linked_attachment_ids = list(attachment_context["auto_linked_attachment_ids"] or [])
        effective_attachment_ids = list(attachment_context["effective_attachment_ids"] or [])
        attachment_context_key = str(attachment_context["attachment_context_key"] or "")

        attachments = upload_store.get_many(effective_attachment_ids)
        _emit_progress(
            progress_cb,
            "stage",
            code="attachments_ready",
            detail=(
                f"附件检查完成: mode={attachment_context_mode}, "
                f"请求 {len(effective_attachment_ids)} 个，命中 {len(attachments)} 个。"
            ),
            run_id=run_id,
        )
        found_attachment_ids = {str(item.get("id")) for item in attachments if item.get("id")}
        missing_attachment_ids = [file_id for file_id in effective_attachment_ids if file_id not in found_attachment_ids]
        resolved_attachment_ids = [file_id for file_id in effective_attachment_ids if file_id in found_attachment_ids]
        try:
            attachment_module.apply_attachment_context_result(
                session=session,
                resolved_attachment_ids=resolved_attachment_ids,
                attachment_context_mode=attachment_context_mode,
                clear_attachment_context=clear_attachment_context,
                requested_attachment_ids=requested_attachment_ids,
            )
            runtime.record_module_success(
                kind="attachment_context",
                selected_ref=attachment_selected_ref or attachment_fallback_ref,
            )
        except Exception as exc:
            runtime.record_module_failure(
                kind="attachment_context",
                requested_ref=attachment_selected_ref or attachment_fallback_ref,
                fallback_ref=attachment_fallback_ref,
                error=str(exc),
            )
            session_context_impl.apply_attachment_context_result(
                session,
                resolved_attachment_ids=resolved_attachment_ids,
                attachment_context_mode=attachment_context_mode,
                clear_attachment_context=clear_attachment_context,
                requested_attachment_ids=requested_attachment_ids,
            )
        resolved_attachment_context_key = attachment_context_key or ""
        if resolved_attachment_ids:
            resolved_attachment_context_key = "|".join(normalize_attachment_ids(resolved_attachment_ids))
        try:
            route_state_input, route_state_scope = attachment_module.resolve_scoped_route_state(
                session=session,
                attachment_ids=resolved_attachment_ids,
            )
            runtime.record_module_success(
                kind="attachment_context",
                selected_ref=attachment_selected_ref or attachment_fallback_ref,
            )
        except Exception as exc:
            runtime.record_module_failure(
                kind="attachment_context",
                requested_ref=attachment_selected_ref or attachment_fallback_ref,
                fallback_ref=attachment_fallback_ref,
                error=str(exc),
            )
            route_state_input, route_state_scope = session_context_impl.resolve_scoped_route_state(
                session,
                attachment_ids=resolved_attachment_ids,
            )

        _emit_progress(progress_cb, "stage", code="agent_run_start", detail="开始模型推理与工具调度。", run_id=run_id)
        (
            text,
            tool_events,
            attachment_note,
            execution_plan,
            execution_trace,
            pipeline_hooks,
            debug_flow,
            agent_panels,
            active_roles,
            current_role,
            role_states,
            answer_bundle,
            token_usage,
            effective_model,
            route_state,
        ) = agent.run_chat(
            history_turns=session.get("turns", []),
            summary=session.get("summary", ""),
            user_message=req.message,
            attachment_metas=attachments,
            settings=req.settings,
            session_id=session_id,
            route_state=route_state_input,
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

        auto_linked_attachment_names = [
            str(item.get("original_name") or "")
            for item in attachments
            if str(item.get("id") or "") in set(auto_linked_attachment_ids)
        ]
        if auto_linked_attachment_names:
            auto_link_msg = f"已自动关联历史附件: {', '.join(auto_linked_attachment_names[:6])}"
            execution_trace.append(auto_link_msg)
            _emit_progress(progress_cb, "trace", message=auto_link_msg, index=len(execution_trace), run_id=run_id)
        elif attachment_context_mode == "cleared" and not requested_attachment_ids:
            cleared_msg = "已按用户指令清空历史附件关联。"
            execution_trace.append(cleared_msg)
            _emit_progress(progress_cb, "trace", message=cleared_msg, index=len(execution_trace), run_id=run_id)

        user_text = req.message.strip()
        if attachment_note:
            user_text = f"{user_text}\n\n[附件] {attachment_note}"

        session_store.append_turn(
            session,
            role="user",
            text=user_text,
            attachments=[{"id": item.get("id"), "name": item.get("original_name")} for item in attachments],
        )
        session_store.append_turn(session, role="assistant", text=text, answer_bundle=answer_bundle)
        try:
            attachment_module.store_scoped_route_state(
                session=session,
                attachment_ids=resolved_attachment_ids,
                route_state=route_state,
            )
            runtime.record_module_success(
                kind="attachment_context",
                selected_ref=attachment_selected_ref or attachment_fallback_ref,
            )
        except Exception as exc:
            runtime.record_module_failure(
                kind="attachment_context",
                requested_ref=attachment_selected_ref or attachment_fallback_ref,
                fallback_ref=attachment_fallback_ref,
                error=str(exc),
            )
            session_context_impl.store_scoped_route_state(
                session,
                attachment_ids=resolved_attachment_ids,
                route_state=route_state,
            )
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
        if config.enable_shadow_logging:
            kernel_health = build_kernel_health_payload(get_kernel_runtime())
            shadow_path = shadow_log_store.append(
                {
                    "run_id": run_id,
                    "session_id": session["id"],
                    "effective_model": selected_model,
                    "attachment_context_mode": attachment_context_mode,
                    "attachment_context_key": resolved_attachment_context_key,
                    "effective_attachment_ids": resolved_attachment_ids,
                    "auto_linked_attachment_ids": [item for item in auto_linked_attachment_ids if item in found_attachment_ids],
                    "missing_attachment_ids": missing_attachment_ids,
                    "route_state_scope": route_state_scope,
                    "route_state_input": route_state_input or {},
                    "route_state": route_state or {},
                    "pipeline_hooks": pipeline_hooks,
                    "tool_events_count": len(tool_events),
                    "active_roles": active_roles,
                    "current_role": current_role,
                    "token_usage": token_usage,
                    "message": req.message,
                    "settings": req.settings.model_dump(),
                    "summary_before": summary_before,
                    "history_turns_before": history_turns_before,
                    "attachment_metas": attachments,
                    "kernel_selected_modules": kernel_health.get("selected_modules") or {},
                    "kernel_module_health": kernel_health.get("module_health") or {},
                    "message_preview": req.message[:500],
                    "response_preview": text[:500],
                }
            )
            _emit_progress(
                progress_cb,
                "trace",
                message=f"shadow log 已写入: {shadow_path.name}",
                run_id=run_id,
            )
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
            pipeline_hooks=pipeline_hooks,
            debug_flow=debug_flow,
            agent_panels=agent_panels,
            active_roles=active_roles,
            current_role=current_role,
            role_states=role_states,
            answer_bundle=answer_bundle,
            attachment_context_mode=attachment_context_mode,
            effective_attachment_ids=resolved_attachment_ids,
            auto_linked_attachment_ids=[item for item in auto_linked_attachment_ids if item in found_attachment_ids],
            auto_linked_attachment_names=auto_linked_attachment_names,
            missing_attachment_ids=missing_attachment_ids,
            route_state_scope=route_state_scope,
            attachment_context_key=resolved_attachment_context_key,
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
