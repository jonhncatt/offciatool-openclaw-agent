from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Literal


RoleKind = Literal["agent", "processor", "hybrid"]
TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
InstanceStatus = Literal["idle", "running", "completed", "failed", "cancelled"]
NodeType = Literal["role", "branch", "join"]


@dataclass(slots=True)
class RoleSpec:
    role: str
    kind: RoleKind = "agent"
    llm_driven: bool = True
    description: str = ""
    tool_names: tuple[str, ...] = ()
    output_keys: tuple[str, ...] = ()


@dataclass(slots=True)
class RoleContext:
    role: str
    requested_model: str = ""
    user_message: str = ""
    effective_user_message: str = ""
    history_summary: str = ""
    attachment_metas: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[Any] = field(default_factory=list)
    planner_brief: dict[str, Any] = field(default_factory=dict)
    reviewer_brief: dict[str, Any] = field(default_factory=dict)
    conflict_brief: dict[str, Any] = field(default_factory=dict)
    route: dict[str, Any] = field(default_factory=dict)
    execution_trace: list[str] = field(default_factory=list)
    response_text: str = ""
    user_content: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_user_request(self) -> str:
        return self.effective_user_message.strip() or self.user_message.strip()


@dataclass(slots=True)
class RoleResult:
    spec: RoleSpec
    context: RoleContext
    payload: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    summary: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    effective_model: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HookDebugEntry:
    stage: str = "backend_hook"
    title: str = ""
    detail: str = ""


@dataclass(slots=True)
class HookPromptInjection:
    position: Literal["front", "append"] = "append"
    title: str = ""
    content: str = ""
    trace_note: str = ""


@dataclass(slots=True)
class HookResult:
    route: dict[str, Any] | None = None
    router_raw: str = ""
    execution_state: Any = None
    spec_lookup_request: bool = False
    evidence_required_mode: bool = False
    finalized_citations: list[dict[str, Any]] = field(default_factory=list)
    answer_bundle: dict[str, Any] | None = None
    use_reviewer: bool = False
    use_conflict_detector: bool = False
    use_revision: bool = False
    use_structurer: bool = False
    should_emit_answer_bundle: bool = False
    execution_plan: list[str] = field(default_factory=list)
    prompt_injections: list[HookPromptInjection] = field(default_factory=list)
    trace_notes: list[str] = field(default_factory=list)
    debug_entries: list[HookDebugEntry] = field(default_factory=list)


@dataclass(slots=True)
class RoleInstance:
    instance_id: str
    role: str
    node_id: str
    sequence: int = 1
    status: InstanceStatus = "idle"
    started_at: float = 0.0
    ended_at: float = 0.0
    tool_mode: str = ""
    summary: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if self.started_at <= 0:
            return 0
        end = self.ended_at if self.ended_at > 0 else time.time()
        return max(0, int((end - self.started_at) * 1000))


@dataclass(slots=True)
class TaskNode:
    node_id: str
    role: str
    role_kind: RoleKind = "agent"
    node_type: NodeType = "role"
    parent_node_id: str | None = None
    status: TaskStatus = "pending"
    phase: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    attempts: int = 0
    summary: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if self.started_at <= 0:
            return 0
        end = self.ended_at if self.ended_at > 0 else time.time()
        return max(0, int((end - self.started_at) * 1000))


@dataclass(slots=True)
class RunState:
    run_id: str
    session_id: str = ""
    task_type: str = "standard"
    created_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: TaskStatus = "running"
    root_node_id: str | None = None
    nodes: dict[str, TaskNode] = field(default_factory=dict)
    instances: dict[str, RoleInstance] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        session_id: str = "",
        task_type: str = "standard",
        root_role: str = "router",
        root_role_kind: RoleKind = "hybrid",
        meta: dict[str, Any] | None = None,
    ) -> "RunState":
        state = cls(
            run_id=run_id,
            session_id=session_id,
            task_type=task_type or "standard",
            meta=dict(meta or {}),
        )
        root_id = f"{root_role}:root"
        state.root_node_id = root_id
        state.nodes[root_id] = TaskNode(
            node_id=root_id,
            role=root_role,
            role_kind=root_role_kind,
            parent_node_id=None,
            status="running",
            phase="entry",
            started_at=state.started_at,
            attempts=1,
            summary=f"entry task_type={state.task_type}",
        )
        state.events.append(
            {
                "ts": state.started_at,
                "kind": "run_started",
                "run_id": run_id,
                "task_type": state.task_type,
                "session_id": session_id,
            }
        )
        return state

    def add_node(
        self,
        *,
        node_id: str,
        role: str,
        role_kind: RoleKind = "agent",
        node_type: NodeType = "role",
        parent_node_id: str | None = None,
        phase: str = "",
        meta: dict[str, Any] | None = None,
    ) -> TaskNode:
        existing = self.nodes.get(node_id)
        if existing is not None:
            return existing
        parent = parent_node_id if parent_node_id is not None else self.root_node_id
        node = TaskNode(
            node_id=node_id,
            role=role,
            role_kind=role_kind,
            node_type=node_type,
            parent_node_id=parent,
            phase=phase,
            meta=dict(meta or {}),
        )
        self.nodes[node_id] = node
        self.events.append(
            {
                "ts": time.time(),
                "kind": "node_added",
                "node_id": node_id,
                "role": role,
                "node_type": node_type,
                "parent_node_id": parent,
                "phase": phase,
            }
        )
        return node

    def begin_node(
        self,
        *,
        node_id: str,
        role: str,
        role_kind: RoleKind = "processor",
        node_type: NodeType = "branch",
        parent_node_id: str | None = None,
        phase: str = "",
        meta: dict[str, Any] | None = None,
    ) -> TaskNode:
        now = time.time()
        node = self.nodes.get(node_id)
        if node is None:
            node = self.add_node(
                node_id=node_id,
                role=role,
                role_kind=role_kind,
                node_type=node_type,
                parent_node_id=parent_node_id,
                phase=phase,
                meta=meta,
            )
        else:
            node.role = role
            node.role_kind = role_kind
            node.node_type = node_type
            if parent_node_id is not None:
                node.parent_node_id = parent_node_id
            if phase:
                node.phase = phase
            if meta:
                node.meta.update(dict(meta))
        node.status = "running"
        node.started_at = node.started_at or now
        node.ended_at = 0.0
        node.error = ""
        node.attempts = max(1, int(node.attempts) + 1 if node.attempts else 1)
        self.events.append(
            {
                "ts": now,
                "kind": "node_started",
                "node_id": node.node_id,
                "role": node.role,
                "node_type": node.node_type,
                "parent_node_id": node.parent_node_id,
                "phase": node.phase,
                "attempts": node.attempts,
            }
        )
        return node

    def complete_node(self, node_id: str, *, summary: str = "", meta: dict[str, Any] | None = None) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        now = time.time()
        node.status = "completed"
        node.ended_at = now
        if summary:
            node.summary = str(summary).strip()
        if meta:
            node.meta.update(dict(meta))
        self.events.append(
            {
                "ts": now,
                "kind": "node_completed",
                "node_id": node.node_id,
                "role": node.role,
                "node_type": node.node_type,
                "summary": node.summary,
                "attempts": node.attempts,
            }
        )

    def fail_node(self, node_id: str, *, error: str = "", meta: dict[str, Any] | None = None) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        now = time.time()
        err = str(error or "").strip()
        node.status = "failed"
        node.ended_at = now
        node.error = err
        if meta:
            node.meta.update(dict(meta))
        self.events.append(
            {
                "ts": now,
                "kind": "node_failed",
                "node_id": node.node_id,
                "role": node.role,
                "node_type": node.node_type,
                "error": err,
                "attempts": node.attempts,
            }
        )

    def start_instance(
        self,
        *,
        instance_id: str,
        role: str,
        node_id: str,
        sequence: int = 1,
        tool_mode: str = "",
        meta: dict[str, Any] | None = None,
    ) -> RoleInstance:
        now = time.time()
        node = self.nodes.get(node_id)
        if node is None:
            node = self.add_node(node_id=node_id, role=role)
        node.status = "running"
        node.started_at = node.started_at or now
        node.attempts = max(1, int(node.attempts) + 1 if node.attempts else 1)
        inst = RoleInstance(
            instance_id=instance_id,
            role=role,
            node_id=node_id,
            sequence=max(1, int(sequence)),
            status="running",
            started_at=now,
            tool_mode=str(tool_mode or ""),
            meta=dict(meta or {}),
        )
        self.instances[instance_id] = inst
        self.events.append(
            {
                "ts": now,
                "kind": "instance_started",
                "instance_id": instance_id,
                "role": role,
                "node_id": node_id,
                "tool_mode": inst.tool_mode,
            }
        )
        return inst

    def complete_instance(self, instance_id: str, *, summary: str = "") -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            return
        now = time.time()
        inst.status = "completed"
        inst.ended_at = now
        inst.summary = str(summary or "").strip()
        node = self.nodes.get(inst.node_id)
        if node is not None:
            node.status = "completed"
            node.ended_at = now
            if inst.summary:
                node.summary = inst.summary
        self.events.append(
            {
                "ts": now,
                "kind": "instance_completed",
                "instance_id": instance_id,
                "node_id": inst.node_id,
                "summary": inst.summary,
            }
        )

    def fail_instance(self, instance_id: str, *, error: str = "") -> None:
        inst = self.instances.get(instance_id)
        if inst is None:
            return
        now = time.time()
        err = str(error or "").strip()
        inst.status = "failed"
        inst.ended_at = now
        inst.error = err
        node = self.nodes.get(inst.node_id)
        if node is not None:
            node.status = "failed"
            node.ended_at = now
            node.error = err
        self.events.append(
            {
                "ts": now,
                "kind": "instance_failed",
                "instance_id": instance_id,
                "node_id": inst.node_id,
                "error": err,
            }
        )

    def add_event(self, kind: str, **payload: Any) -> None:
        event = {"ts": time.time(), "kind": str(kind or "").strip() or "event"}
        if payload:
            event.update(payload)
        self.events.append(event)

    def finish(self, *, status: TaskStatus = "completed") -> None:
        now = time.time()
        normalized = str(status or "completed").strip().lower()
        if normalized not in {"pending", "running", "completed", "failed", "cancelled"}:
            normalized = "completed"
        self.status = normalized  # type: ignore[assignment]
        self.ended_at = now
        if self.root_node_id:
            root = self.nodes.get(self.root_node_id)
            if root is not None:
                if root.status in {"pending", "running"}:
                    root.status = normalized  # type: ignore[assignment]
                root.ended_at = root.ended_at or now
        self.events.append(
            {
                "ts": now,
                "kind": "run_finished",
                "run_id": self.run_id,
                "status": self.status,
                "duration_ms": self.duration_ms,
            }
        )

    @property
    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at > 0 else time.time()
        return max(0, int((end - self.started_at) * 1000))

    def snapshot_compact(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.status] = counts.get(node.status, 0) + 1
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "task_type": self.task_type,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "node_count": len(self.nodes),
            "instance_count": len(self.instances),
            "status_counts": counts,
            "event_count": len(self.events),
        }
