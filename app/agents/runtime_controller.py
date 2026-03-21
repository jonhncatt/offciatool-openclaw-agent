from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from typing import Any

from app.role_runtime import RoleContext, RoleResult, RunState

from app.agents.role_registry import RegisteredRole, RoleRegistry


@dataclass(slots=True)
class RoleExecution:
    role: str
    node_id: str = ""
    instance_id: str = ""
    result: RoleResult | None = None
    error: str = ""


class RoleRuntimeController:
    def __init__(self, registry: RoleRegistry) -> None:
        self.registry = registry
        self._last_run_snapshot: dict[str, Any] = {}
        self._state_lock = threading.Lock()

    def _next_node_id(self, run_state: RunState, role: str) -> str:
        seq = sum(1 for node in run_state.nodes.values() if node.role == role and node.node_id != run_state.root_node_id) + 1
        return f"{role}:{seq}"

    def _next_instance_id(self, run_state: RunState, role: str) -> tuple[str, int]:
        seq = sum(1 for inst in run_state.instances.values() if inst.role == role) + 1
        return f"{role}#{seq}", seq

    def _next_branch_node_id(self, run_state: RunState, role: str, phase: str) -> str:
        phase_key = str(phase or "branch").strip().lower().replace(" ", "_")
        seq = sum(
            1
            for node in run_state.nodes.values()
            if node.role == role and node.node_type in {"branch", "join"} and node.phase == phase
        ) + 1
        return f"{role}:{phase_key}:{seq}"

    def execute(
        self,
        *,
        agent: Any,
        role: str,
        context: RoleContext,
        run_state: RunState | None = None,
        parent_node_id: str | None = None,
        phase: str = "",
        tool_mode: str = "",
        meta: dict[str, Any] | None = None,
        handler_kwargs: dict[str, Any] | None = None,
    ) -> RoleExecution:
        registered = self.registry.require(role)
        if not registered.executable or registered.handler is None:
            raise RuntimeError(f"role {role} is not controller-backed")

        node_id = ""
        instance_id = ""
        merged_meta = {
            "role_title": registered.title,
            "runtime_profiles": list(registered.runtime_profiles),
            **dict(meta or {}),
        }
        if run_state is not None:
            node_id = self._next_node_id(run_state, role)
            parent_id = parent_node_id or run_state.root_node_id
            with self._state_lock:
                run_state.add_node(
                    node_id=node_id,
                    role=role,
                    role_kind=str(registered.kind or "agent"),  # type: ignore[arg-type]
                    parent_node_id=parent_id,
                    phase=phase,
                    meta=merged_meta,
                )
                instance_id, sequence = self._next_instance_id(run_state, role)
                run_state.start_instance(
                    instance_id=instance_id,
                    role=role,
                    node_id=node_id,
                    sequence=sequence,
                    tool_mode=tool_mode,
                    meta=merged_meta,
                )
                run_state.add_event(
                    "role_dispatch",
                    role=role,
                    node_id=node_id,
                    instance_id=instance_id,
                    phase=phase,
                    parent_node_id=parent_id,
                )

        try:
            result = registered.handler(agent, context=context, **dict(handler_kwargs or {}))
            summary = str(
                result.summary
                or result.payload.get("summary")
                or result.payload.get("objective")
                or f"{role}_completed"
            ).strip()
            if run_state is not None and instance_id:
                with self._state_lock:
                    run_state.complete_instance(instance_id, summary=summary)
                    run_state.add_event(
                        "role_completed",
                        role=role,
                        node_id=node_id,
                        instance_id=instance_id,
                        summary=summary,
                        output_keys=list(result.payload.keys())[:10],
                    )
                    self.capture_run_state(run_state)
            return RoleExecution(role=role, node_id=node_id, instance_id=instance_id, result=result)
        except Exception as exc:
            if run_state is not None and instance_id:
                with self._state_lock:
                    run_state.fail_instance(instance_id, error=str(exc))
                    run_state.add_event(
                        "role_failed",
                        role=role,
                        node_id=node_id,
                        instance_id=instance_id,
                        error=str(exc),
                    )
                    self.capture_run_state(run_state)
            raise

    def begin_managed(
        self,
        *,
        role: str,
        run_state: RunState,
        parent_node_id: str | None = None,
        phase: str = "",
        tool_mode: str = "",
        meta: dict[str, Any] | None = None,
    ) -> RoleExecution:
        registered = self.registry.require(role)
        if not registered.controller_backed:
            raise RuntimeError(f"role {role} is not controller-backed")
        node_id = self._next_node_id(run_state, role)
        parent_id = parent_node_id or run_state.root_node_id
        merged_meta = {
            "role_title": registered.title,
            "runtime_profiles": list(registered.runtime_profiles),
            "execution_mode": "managed",
            **dict(meta or {}),
        }
        with self._state_lock:
            run_state.add_node(
                node_id=node_id,
                role=role,
                role_kind=str(registered.kind or "agent"),  # type: ignore[arg-type]
                parent_node_id=parent_id,
                phase=phase,
                meta=merged_meta,
            )
            instance_id, sequence = self._next_instance_id(run_state, role)
            run_state.start_instance(
                instance_id=instance_id,
                role=role,
                node_id=node_id,
                sequence=sequence,
                tool_mode=tool_mode,
                meta=merged_meta,
            )
            run_state.add_event(
                "role_dispatch",
                role=role,
                node_id=node_id,
                instance_id=instance_id,
                phase=phase,
                parent_node_id=parent_id,
                execution_mode="managed",
            )
            self.capture_run_state(run_state)
        return RoleExecution(role=role, node_id=node_id, instance_id=instance_id)

    def complete_managed(
        self,
        execution: RoleExecution,
        *,
        run_state: RunState,
        summary: str = "",
        payload_meta: dict[str, Any] | None = None,
    ) -> None:
        if not execution.instance_id:
            return
        with self._state_lock:
            run_state.complete_instance(execution.instance_id, summary=summary)
            run_state.add_event(
                "role_completed",
                role=execution.role,
                node_id=execution.node_id,
                instance_id=execution.instance_id,
                summary=summary,
                **dict(payload_meta or {}),
            )
            self.capture_run_state(run_state)

    def begin_task_node(
        self,
        *,
        run_state: RunState,
        role: str,
        parent_node_id: str | None = None,
        phase: str = "",
        role_kind: str = "processor",
        node_type: str = "branch",
        meta: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> str:
        resolved_node_id = str(node_id or "").strip() or self._next_branch_node_id(run_state, role, phase)
        parent_id = parent_node_id or run_state.root_node_id
        with self._state_lock:
            run_state.begin_node(
                node_id=resolved_node_id,
                role=role,
                role_kind=str(role_kind or "processor"),  # type: ignore[arg-type]
                node_type=str(node_type or "branch"),  # type: ignore[arg-type]
                parent_node_id=parent_id,
                phase=phase,
                meta=meta,
            )
            self.capture_run_state(run_state)
        return resolved_node_id

    def complete_task_node(
        self,
        node_id: str,
        *,
        run_state: RunState,
        summary: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not str(node_id or "").strip():
            return
        with self._state_lock:
            run_state.complete_node(node_id, summary=summary, meta=meta)
            self.capture_run_state(run_state)

    def fail_task_node(
        self,
        node_id: str,
        *,
        run_state: RunState,
        error: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not str(node_id or "").strip():
            return
        with self._state_lock:
            run_state.fail_node(node_id, error=error, meta=meta)
            self.capture_run_state(run_state)

    def fail_managed(
        self,
        execution: RoleExecution,
        *,
        run_state: RunState,
        error: str,
    ) -> None:
        if not execution.instance_id:
            return
        with self._state_lock:
            run_state.fail_instance(execution.instance_id, error=error)
            run_state.add_event(
                "role_failed",
                role=execution.role,
                node_id=execution.node_id,
                instance_id=execution.instance_id,
                error=error,
            )
            self.capture_run_state(run_state)

    def execute_batch(
        self,
        *,
        agent: Any,
        role: str,
        contexts: list[RoleContext],
        run_state: RunState,
        parent_node_id: str | None = None,
        phase: str = "",
        tool_mode: str = "",
        metas: list[dict[str, Any]] | None = None,
        max_workers: int = 4,
        handler_kwargs_list: list[dict[str, Any] | None] | None = None,
    ) -> list[RoleExecution]:
        if not contexts:
            return []
        jobs: list[tuple[int, RoleContext, dict[str, Any] | None, dict[str, Any] | None]] = []
        for idx, context in enumerate(contexts):
            job_meta = (metas[idx] if metas and idx < len(metas) else None) or {}
            job_kwargs = (handler_kwargs_list[idx] if handler_kwargs_list and idx < len(handler_kwargs_list) else None) or {}
            jobs.append((idx, context, job_meta, job_kwargs))

        results: list[RoleExecution] = [RoleExecution(role=role) for _ in jobs]

        def _run(job: tuple[int, RoleContext, dict[str, Any] | None, dict[str, Any] | None]) -> tuple[int, RoleExecution]:
            idx, context, job_meta, job_kwargs = job
            execution = self.execute(
                agent=agent,
                role=role,
                context=context,
                run_state=run_state,
                parent_node_id=parent_node_id,
                phase=phase,
                tool_mode=tool_mode,
                meta=job_meta,
                handler_kwargs=job_kwargs,
            )
            return idx, execution

        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
            futures = [pool.submit(_run, job) for job in jobs]
            for future in as_completed(futures):
                idx, execution = future.result()
                results[idx] = execution
        self.capture_run_state(run_state)
        return results

    def capture_run_state(
        self,
        run_state: RunState,
        *,
        active_roles: list[str] | None = None,
        current_role: str | None = None,
    ) -> dict[str, Any]:
        nodes = sorted(run_state.nodes.values(), key=lambda item: (item.created_at, item.node_id))
        instances = sorted(run_state.instances.values(), key=lambda item: (item.started_at, item.instance_id))
        snapshot = {
            "run": run_state.snapshot_compact(),
            "current_role": str(current_role or "").strip(),
            "active_roles": [str(item).strip() for item in (active_roles or []) if str(item).strip()],
            "nodes": [
                {
                    "node_id": item.node_id,
                    "role": item.role,
                    "node_type": item.node_type,
                    "parent_node_id": item.parent_node_id,
                    "phase": item.phase,
                    "status": item.status,
                    "attempts": item.attempts,
                    "summary": item.summary,
                    "error": item.error,
                    "meta": dict(item.meta or {}),
                }
                for item in nodes[-32:]
            ],
            "instances": [
                {
                    "instance_id": item.instance_id,
                    "role": item.role,
                    "node_id": item.node_id,
                    "sequence": item.sequence,
                    "status": item.status,
                    "tool_mode": item.tool_mode,
                    "summary": item.summary,
                    "error": item.error,
                    "duration_ms": item.duration_ms,
                }
                for item in instances[-18:]
            ],
            "events": list(run_state.events[-48:]),
        }
        self._last_run_snapshot = snapshot
        return snapshot

    def stage4_readiness(self) -> dict[str, Any]:
        registry_snapshot = self.registry.snapshot()
        executable_roles = list(registry_snapshot.get("executable_roles") or [])
        controller_backed_roles = list(registry_snapshot.get("controller_backed_roles") or [])
        multi_instance_roles = list(registry_snapshot.get("multi_instance_ready_roles") or [])
        parent_child_roles = list(registry_snapshot.get("parent_child_ready_roles") or [])
        controller_gaps = list(registry_snapshot.get("controller_gaps") or [])
        ready_for_trial = bool({"planner", "researcher", "file_reader"} & set(executable_roles)) and bool(multi_instance_roles)
        full_controller_coverage = not bool(controller_gaps)
        return {
            "ready_for_stage4_trial": ready_for_trial,
            "full_controller_coverage": full_controller_coverage,
            "executable_role_count": len(executable_roles),
            "controller_backed_role_count": len(controller_backed_roles),
            "multi_instance_role_count": len(multi_instance_roles),
            "parent_child_role_count": len(parent_child_roles),
            "controller_gaps": controller_gaps,
            "next_focus": "worker runtime abstraction" if "worker" in controller_gaps else "pilot multi-instance batch",
        }

    def runtime_snapshot(self) -> dict[str, Any]:
        return {
            "registry": self.registry.snapshot(),
            "stage4_readiness": self.stage4_readiness(),
            "last_run": dict(self._last_run_snapshot),
        }
