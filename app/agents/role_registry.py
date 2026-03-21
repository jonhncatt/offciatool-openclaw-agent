from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.conflict_detector_role import run_conflict_detector_role
from app.agents.planner_role import run_planner_role
from app.agents.reviewer_role import run_reviewer_role
from app.agents.revision_role import run_revision_role
from app.agents.role_catalog import ROLE_KINDS, SPECIALIST_LABELS
from app.agents.specialist_role import run_specialist_with_context
from app.agents.structurer_role import run_structurer_role


RoleHandler = Callable[..., Any]


@dataclass(slots=True)
class RegisteredRole:
    role: str
    title: str
    kind: str = "agent"
    description: str = ""
    handler: RoleHandler | None = None
    executable: bool = True
    controller_backed: bool = True
    multi_instance_ready: bool = False
    supports_parent_child: bool = False
    runtime_profiles: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


class RoleRegistry:
    def __init__(self) -> None:
        self._roles: dict[str, RegisteredRole] = {}

    def register(self, role: RegisteredRole) -> RegisteredRole:
        key = str(role.role or "").strip().lower()
        if not key:
            raise ValueError("role must not be empty")
        role.role = key
        self._roles[key] = role
        return role

    def get(self, role: str) -> RegisteredRole | None:
        return self._roles.get(str(role or "").strip().lower())

    def require(self, role: str) -> RegisteredRole:
        item = self.get(role)
        if item is None:
            raise KeyError(f"unregistered role: {role}")
        return item

    def roles(self) -> list[RegisteredRole]:
        return [self._roles[key] for key in sorted(self._roles)]

    def snapshot(self) -> dict[str, Any]:
        roles = self.roles()
        kind_counts = {"agent": 0, "processor": 0, "hybrid": 0}
        executable_roles: list[str] = []
        controller_backed_roles: list[str] = []
        multi_instance_roles: list[str] = []
        parent_child_roles: list[str] = []
        controller_gaps: list[str] = []
        entries: list[dict[str, Any]] = []
        for item in roles:
            kind = str(item.kind or "agent").strip().lower()
            if kind not in kind_counts:
                kind = "agent"
            kind_counts[kind] += 1
            if item.executable and item.handler is not None:
                executable_roles.append(item.role)
            if item.controller_backed:
                controller_backed_roles.append(item.role)
            else:
                controller_gaps.append(item.role)
            if item.multi_instance_ready:
                multi_instance_roles.append(item.role)
            if item.supports_parent_child:
                parent_child_roles.append(item.role)
            entries.append(
                {
                    "role": item.role,
                    "title": item.title,
                    "kind": kind,
                    "description": item.description,
                    "executable": item.executable and item.handler is not None,
                    "controller_backed": item.controller_backed,
                    "multi_instance_ready": item.multi_instance_ready,
                    "supports_parent_child": item.supports_parent_child,
                    "runtime_profiles": list(item.runtime_profiles),
                }
            )
        return {
            "registered_roles": len(roles),
            "kind_counts": kind_counts,
            "executable_roles": executable_roles,
            "controller_backed_roles": controller_backed_roles,
            "multi_instance_ready_roles": multi_instance_roles,
            "parent_child_ready_roles": parent_child_roles,
            "controller_gaps": controller_gaps,
            "roles": entries,
        }


def build_default_role_registry() -> RoleRegistry:
    registry = RoleRegistry()

    def _register(
        role: str,
        *,
        title: str,
        description: str,
        handler: RoleHandler | None,
        executable: bool,
        controller_backed: bool,
        multi_instance_ready: bool,
        supports_parent_child: bool,
        runtime_profiles: tuple[str, ...] = (),
    ) -> None:
        registry.register(
            RegisteredRole(
                role=role,
                title=title,
                kind=str(ROLE_KINDS.get(role, "agent")),
                description=description,
                handler=handler,
                executable=executable,
                controller_backed=controller_backed,
                multi_instance_ready=multi_instance_ready,
                supports_parent_child=supports_parent_child,
                runtime_profiles=runtime_profiles,
            )
        )

    _register(
        "router",
        title="Router",
        description="规则与可选 LLM 路由入口。",
        handler=None,
        executable=False,
        controller_backed=True,
        multi_instance_ready=False,
        supports_parent_child=False,
    )
    _register(
        "coordinator",
        title="Coordinator",
        description="运行时状态机与调度处理器。",
        handler=None,
        executable=False,
        controller_backed=True,
        multi_instance_ready=False,
        supports_parent_child=True,
    )
    _register(
        "worker",
        title="Worker",
        description="主任务执行与工具循环。",
        handler=None,
        executable=False,
        controller_backed=True,
        multi_instance_ready=True,
        supports_parent_child=True,
        runtime_profiles=("explainer", "evidence", "patch_worker"),
    )
    _register(
        "planner",
        title="Planner",
        description="提炼目标、限制与执行计划。",
        handler=run_planner_role,
        executable=True,
        controller_backed=True,
        multi_instance_ready=True,
        supports_parent_child=True,
        runtime_profiles=("explainer", "evidence", "patch_worker"),
    )
    for specialist, title in SPECIALIST_LABELS.items():
        _register(
            specialist,
            title=title,
            description=f"{title} 专门简报角色。",
            handler=run_specialist_with_context,
            executable=True,
            controller_backed=True,
            multi_instance_ready=True,
            supports_parent_child=True,
            runtime_profiles=("explainer", "evidence", "patch_worker"),
        )
    _register(
        "conflict_detector",
        title="Conflict Detector",
        description="通识与工程知识冲突报警。",
        handler=run_conflict_detector_role,
        executable=True,
        controller_backed=True,
        multi_instance_ready=True,
        supports_parent_child=True,
        runtime_profiles=("evidence",),
    )
    _register(
        "reviewer",
        title="Reviewer",
        description="覆盖度、证据链和交付风险审阅。",
        handler=run_reviewer_role,
        executable=True,
        controller_backed=True,
        multi_instance_ready=True,
        supports_parent_child=True,
        runtime_profiles=("evidence",),
    )
    _register(
        "revision",
        title="Revision",
        description="按审阅结论修订答复。",
        handler=run_revision_role,
        executable=True,
        controller_backed=True,
        multi_instance_ready=True,
        supports_parent_child=True,
        runtime_profiles=("explainer", "evidence"),
    )
    _register(
        "structurer",
        title="Structurer",
        description="整理结构化证据包与 assertions。",
        handler=run_structurer_role,
        executable=True,
        controller_backed=True,
        multi_instance_ready=True,
        supports_parent_child=True,
        runtime_profiles=("evidence",),
    )
    return registry
