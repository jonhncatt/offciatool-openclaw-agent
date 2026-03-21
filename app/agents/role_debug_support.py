from __future__ import annotations

from typing import Any

from app.agents.role_contracts import validate_role_result, validate_runtime_profile
from app.agents.role_smoke import run_role_execution_smoke
from app.agents.runtime_profiles import PATCH_WORKER_PROFILE, default_runtime_profile_for_route, runtime_profile_spec


def debug_role_contract_matrix(agent: Any) -> dict[str, Any]:
    route = {
        "task_type": "attachment_tooling",
        "primary_intent": "understanding",
        "execution_policy": "attachment_tooling",
        "runtime_profile": default_runtime_profile_for_route(
            {
                "task_type": "attachment_tooling",
                "primary_intent": "understanding",
                "execution_policy": "attachment_tooling",
            }
        ),
    }
    role_payloads = {
        "planner": {
            "objective": "整理文档重点",
            "constraints": ["不要输出思维链"],
            "plan": ["先看附件摘要", "再给解释"],
            "watchouts": ["不要误判成取证链"],
            "success_signals": ["回答清楚整体思路"],
        },
        "reviewer": {
            "verdict": "pass",
            "confidence": "medium",
            "summary": "结构完整。",
            "strengths": ["覆盖了主要问题"],
            "risks": [],
            "followups": [],
        },
        "revision": {
            "changed": True,
            "summary": "已按 reviewer 调整措辞。",
            "key_changes": ["补充了限制说明"],
            "final_answer": "这是修订后的答复。",
        },
        "conflict_detector": {
            "has_conflict": False,
            "confidence": "medium",
            "summary": "未发现明显冲突。",
            "concerns": [],
            "suggested_checks": [],
        },
    }
    specialist_payloads = {
        role: agent._specialist_fallback(
            specialist=role,
            requested_model=agent.config.default_model,
            attachment_metas=[],
            initial_triage_request=False,
        )
        for role in ("researcher", "file_reader", "summarizer", "fixer")
    }
    roles: list[dict[str, Any]] = []
    for role, payload in {**role_payloads, **specialist_payloads}.items():
        output_keys_map = {
            "planner": ["objective", "constraints", "plan", "watchouts", "success_signals"],
            "reviewer": ["verdict", "confidence", "summary", "strengths", "risks", "followups"],
            "revision": ["changed", "summary", "key_changes", "final_answer"],
            "conflict_detector": ["has_conflict", "confidence", "summary", "concerns", "suggested_checks"],
            "researcher": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
            "file_reader": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
            "summarizer": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
            "fixer": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
        }
        result = agent._make_default_role_result(
            role,
            payload=payload,
            requested_model=agent.config.default_model,
            user_message="解释一下文档整体思路",
            history_summary="上一轮已经读了附件摘要。",
            route=route,
            description=f"{role} contract test",
            output_keys=output_keys_map.get(role, []),
        )
        roles.append(validate_role_result(result))
    profiles = [
        validate_runtime_profile(runtime_profile_spec("explainer")),
        validate_runtime_profile(runtime_profile_spec("evidence")),
        validate_runtime_profile(PATCH_WORKER_PROFILE),
    ]
    return {
        "ok": all(bool(item.get("ok")) for item in roles) and all(bool(item.get("ok")) for item in profiles),
        "roles": roles,
        "profiles": profiles,
    }


def debug_role_execution_smoke_matrix(agent: Any) -> dict[str, Any]:
    return run_role_execution_smoke(agent)
