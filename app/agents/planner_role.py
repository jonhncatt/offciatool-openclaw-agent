from __future__ import annotations

import json
from typing import Any

from app.role_runtime import RoleContext, RoleResult


def run_planner_role(agent: Any, *, context: RoleContext, settings: Any) -> RoleResult:
    spec = agent._make_role_spec(
        "planner",
        description="提炼目标、约束和执行计划。",
        output_keys=["objective", "constraints", "plan", "watchouts", "success_signals"],
    )
    fallback = {
        "objective": agent._shorten(context.user_message.strip(), 220),
        "constraints": [],
        "plan": agent._build_execution_plan(attachment_metas=context.attachment_metas, settings=settings),
        "watchouts": [],
        "success_signals": [],
        "usage": agent._empty_usage(),
        "effective_model": context.requested_model,
        "notes": [],
    }
    attachment_summary = agent._summarize_attachment_metas_for_agents(context.attachment_metas)
    planner_input = "\n".join(
        [
            f"user_message:\n{context.user_message.strip() or '(empty)'}",
            f"history_summary:\n{context.history_summary.strip() or '(none)'}",
            f"attachments:\n{attachment_summary}",
            f"response_style={context.extra.get('response_style') or settings.response_style}",
            f"enable_tools={context.extra.get('enable_tools', settings.enable_tools)}",
            f"max_context_turns={context.extra.get('max_context_turns', settings.max_context_turns)}",
        ]
    )
    messages = [
        agent._SystemMessage(
            content=(
                "你是 Planner Agent。你的职责是为 Worker 生成可见的目标摘要和执行计划。"
                "不要输出思维链，不要写解释。"
                '只返回 JSON 对象，字段固定为 objective, constraints, plan, watchouts, success_signals。'
                "每个数组最多 5 条，每条一句话。"
            )
        ),
        agent._HumanMessage(content=planner_input),
    ]
    try:
        ai_msg, _, effective_model, notes = agent._invoke_chat_with_runner(
            messages=messages,
            model=context.requested_model,
            max_output_tokens=900,
            enable_tools=False,
        )
        raw_text = agent._content_to_text(getattr(ai_msg, "content", "")).strip()
        parsed = agent._parse_json_object(raw_text)
        if not parsed:
            fallback["notes"] = ["Planner 未返回标准 JSON，已降级为默认执行计划。", *notes]
            fallback["usage"] = agent._extract_usage_from_message(ai_msg)
            fallback["effective_model"] = effective_model
            return agent._make_role_result(spec, context, fallback, raw_text)

        planner = {
            "objective": str(parsed.get("objective") or fallback["objective"]).strip() or fallback["objective"],
            "constraints": agent._normalize_string_list(parsed.get("constraints") or [], limit=5, item_limit=180),
            "plan": agent._normalize_string_list(parsed.get("plan") or fallback["plan"], limit=6, item_limit=180),
            "watchouts": agent._normalize_string_list(parsed.get("watchouts") or [], limit=5, item_limit=180),
            "success_signals": agent._normalize_string_list(
                parsed.get("success_signals") or [], limit=4, item_limit=180
            ),
            "usage": agent._extract_usage_from_message(ai_msg),
            "effective_model": effective_model,
            "notes": notes,
        }
        return agent._make_role_result(spec, context, planner, raw_text)
    except Exception as exc:
        fallback["notes"] = [f"Planner 调用失败，已回退默认计划: {agent._shorten(exc, 180)}"]
        raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
        return agent._make_role_result(spec, context, fallback, raw_text)
