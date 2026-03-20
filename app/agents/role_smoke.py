from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.agents.planner_role import run_planner_role
from app.agents.reviewer_role import run_reviewer_role
from app.agents.revision_role import run_revision_role
from app.agents.role_contracts import validate_role_result, validate_runtime_profile
from app.agents.runtime_profiles import PATCH_WORKER_PROFILE, runtime_profile_spec
from app.agents.structurer_role import run_structurer_role
from app.models import ChatSettings


class _FakeMessage:
    def __init__(self, content: str, *, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self.content = content
        self.tool_calls = list(tool_calls or [])


def run_role_execution_smoke(agent: Any) -> dict[str, Any]:
    original_invoke = agent._invoke_chat_with_runner
    original_recover = agent._invoke_with_runner_recovery
    original_extract_usage = agent._extract_usage_from_message

    def _fake_invoke_chat_with_runner(*, messages: list[Any], model: str, max_output_tokens: int, enable_tools: bool, tool_names: list[str] | None = None):
        system_prompt = str(getattr(messages[0], "content", "")) if messages else ""
        if "Planner Agent" in system_prompt:
            payload = {
                "objective": "解释整体思路",
                "constraints": ["不要输出思维链"],
                "plan": ["先看上下文", "再整理主线"],
                "watchouts": ["避免误入取证模式"],
                "success_signals": ["用户能看懂整体结构"],
            }
        elif "Reviewer Agent" in system_prompt:
            payload = {
                "verdict": "pass",
                "confidence": "medium",
                "summary": "已覆盖主要目标。",
                "strengths": ["目标明确"],
                "risks": ["可再补充一点证据表达"],
                "followups": ["如有需要再补页码"],
            }
        elif "Revision Agent" in system_prompt:
            payload = {
                "changed": True,
                "summary": "已按 Reviewer 修订。",
                "key_changes": ["补充了保守限定语"],
                "final_answer": "这是修订后的最终答复。",
            }
        elif "Answer Structurer" in system_prompt:
            payload = {
                "summary": "已整理结构化证据包。",
                "claims": [
                    {
                        "statement": "系统已经支持 shadow 自升级闭环。",
                        "citation_ids": ["cite-1"],
                        "confidence": "high",
                        "status": "supported",
                    }
                ],
                "warnings": ["仍建议保留 rollback。"],
            }
        else:
            raise RuntimeError(f"unexpected smoke role prompt: {system_prompt[:80]}")
        return _FakeMessage(json.dumps(payload, ensure_ascii=False)), None, model, []

    def _fake_invoke_with_runner_recovery(*, runner: Any, messages: list[Any], model: str, max_output_tokens: int, enable_tools: bool, tool_names: list[str] | None = None):
        return _fake_invoke_chat_with_runner(
            messages=messages,
            model=model,
            max_output_tokens=max_output_tokens,
            enable_tools=enable_tools,
            tool_names=tool_names,
        )

    def _fake_extract_usage_from_message(_message: Any) -> dict[str, int]:
        return {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "llm_calls": 1}

    agent._invoke_chat_with_runner = _fake_invoke_chat_with_runner
    agent._invoke_with_runner_recovery = _fake_invoke_with_runner_recovery
    agent._extract_usage_from_message = _fake_extract_usage_from_message
    try:
        settings = ChatSettings(enable_tools=True, response_style="short", max_output_tokens=1200)
        route = {
            "task_type": "attachment_tooling",
            "primary_intent": "understanding",
            "execution_policy": "attachment_tooling",
            "runtime_profile": "explainer",
        }
        planner_context = agent._make_role_context(
            "planner",
            requested_model=agent.config.default_model,
            user_message="解释一下这份设计文档的整体思路",
            history_summary="上一轮已经整理了附件摘要。",
            attachment_metas=[{"name": "spec.pdf", "kind": "pdf"}],
            route=route,
            extra={"response_style": settings.response_style, "enable_tools": settings.enable_tools, "max_context_turns": settings.max_context_turns},
        )
        planner_result = run_planner_role(agent, context=planner_context, settings=settings)

        reviewer_context = agent._make_role_context(
            "reviewer",
            requested_model=agent.config.default_model,
            user_message="继续解释整体思路",
            effective_user_message="继续解释整体思路",
            history_summary="已经给出第一版高层解释。",
            attachment_metas=[{"name": "spec.pdf", "kind": "pdf"}],
            planner_brief=planner_result,
            response_text="这是当前答复。",
            route=route,
            extra={"evidence_required_mode": False, "spec_lookup_request": False},
        )
        reviewer_result = run_reviewer_role(agent, context=reviewer_context)

        revision_context = replace(
            reviewer_context,
            role="revision",
            reviewer_brief=reviewer_result.payload,
            conflict_brief={"has_conflict": False, "summary": "未发现冲突。", "concerns": []},
            response_text="这是当前答复。",
            extra={"evidence_required_mode": False},
        )
        revision_result = run_revision_role(agent, context=revision_context)

        structurer_context = agent._make_role_context(
            "structurer",
            requested_model=agent.config.default_model,
            reviewer_brief=reviewer_result,
            conflict_brief={"has_conflict": False, "summary": "未发现冲突。", "concerns": []},
            response_text=revision_result.payload.get("final_answer") or "这是当前答复。",
            extra={
                "citations": [
                    {
                        "id": "cite-1",
                        "kind": "evidence",
                        "tool": "search_codebase",
                        "path": "/tmp/spec.txt",
                        "locator": "L10",
                        "excerpt": "shadow self-upgrade loop",
                    }
                ]
            },
        )
        structurer_result = run_structurer_role(agent, context=structurer_context)

        roles = {
            "planner": validate_role_result(planner_result),
            "reviewer": validate_role_result(reviewer_result),
            "revision": validate_role_result(revision_result),
            "structurer": validate_role_result(structurer_result),
        }
        profiles = {
            "explainer": validate_runtime_profile(runtime_profile_spec("explainer")),
            "evidence": validate_runtime_profile(runtime_profile_spec("evidence")),
            "patch_worker": validate_runtime_profile(PATCH_WORKER_PROFILE),
        }
        return {
            "ok": all(item.get("ok") for item in roles.values()) and all(item.get("ok") for item in profiles.values()),
            "roles": roles,
            "profiles": profiles,
            "payloads": {
                "planner": planner_result.payload,
                "reviewer": reviewer_result.payload,
                "revision": revision_result.payload,
                "structurer": structurer_result.payload,
            },
        }
    finally:
        agent._invoke_chat_with_runner = original_invoke
        agent._invoke_with_runner_recovery = original_recover
        agent._extract_usage_from_message = original_extract_usage
