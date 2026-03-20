from __future__ import annotations

import json
from typing import Any

from app.role_runtime import RoleContext, RoleResult


def run_revision_role(agent: Any, *, context: RoleContext) -> RoleResult:
    spec = agent._make_role_spec(
        "revision",
        description="根据 reviewer 结论修订最终答复。",
        output_keys=["changed", "summary", "key_changes", "final_answer"],
    )
    local_access_succeeded = agent._has_successful_local_file_access(context.tool_events)
    tool_summaries = agent._summarize_tool_events_for_review(context.tool_events, limit=10)
    write_actions = agent._summarize_write_tool_events(context.tool_events, limit=6)
    attachment_summary = agent._summarize_attachment_metas_for_agents(context.attachment_metas)
    revision_input = "\n".join(
        [
            f"effective_user_request:\n{context.primary_user_request or '(empty)'}",
            f"raw_user_message:\n{context.user_message.strip() or '(empty)'}",
            f"history_summary:\n{context.history_summary.strip() or '(none)'}",
            f"attachments:\n{attachment_summary}",
            f"planner_objective:\n{str(context.planner_brief.get('objective') or '').strip() or '(none)'}",
            f"reviewer_verdict={str(context.reviewer_brief.get('verdict') or 'pass').strip()}",
            f"reviewer_confidence={str(context.reviewer_brief.get('confidence') or 'medium').strip()}",
            f"evidence_required_mode={str(bool(context.extra.get('evidence_required_mode'))).lower()}",
            "reviewer_risks:",
            *[f"- {item}" for item in agent._normalize_string_list(context.reviewer_brief.get("risks") or [], limit=5)],
            "reviewer_followups:",
            *[f"- {item}" for item in agent._normalize_string_list(context.reviewer_brief.get("followups") or [], limit=4)],
            "reviewer_readonly_checks:",
            *[f"- {item}" for item in agent._normalize_string_list(context.reviewer_brief.get("readonly_checks") or [], limit=8)],
            "reviewer_readonly_evidence:",
            *[
                f"- {item}"
                for item in agent._normalize_string_list(context.reviewer_brief.get("readonly_evidence") or [], limit=8)
            ],
            f"conflict_summary={str(context.conflict_brief.get('summary') or '').strip() or '(none)'}",
            "conflict_concerns:",
            *[f"- {item}" for item in agent._normalize_string_list(context.conflict_brief.get("concerns") or [], limit=4)],
            "write_actions:",
            *(write_actions or ["(none)"]),
            "tool_events:",
            *(tool_summaries or ["(none)"]),
            f"current_answer:\n{context.response_text.strip() or '(empty)'}",
        ]
    )
    fallback = {
        "changed": False,
        "summary": "Revision 未修改最终答复。",
        "key_changes": [],
        "final_answer": context.response_text,
        "usage": agent._empty_usage(),
        "effective_model": context.requested_model,
        "notes": [],
    }
    revision_system_prompt = (
        "你是 Revision Agent。你负责根据 Reviewer 结论对最终答复做最后一次修订。"
        "如果 raw_user_message 看起来只是短跟进/纠偏，而 effective_user_request 延续了上一轮完整目标，"
        "你必须以 effective_user_request 作为主要修订目标。"
        "如果 attachments 段列出了本轮附件，你必须把这些附件视为已存在且对当前任务有效，"
        "不能仅因为 raw_user_message 没有重复列出附件名，就把答案改写成“附件不存在/未提供附件”的版本。"
        "如果 reviewer_verdict=pass，通常保持原文或只做极小润色。"
        "如果 reviewer_verdict=warn，优先保留 Worker 已经找到的核心信息，补上页码/章节/命中片段/限定语，"
        "不要因为证据表达不完整就整段推翻。"
        "如果 reviewer_verdict=block，才应把最终答复改成更保守或更明确要求继续取证的版本。"
        "如果当前答复已经足够好，可以保持原文不变。"
        "如果 write_actions 已显示 Worker 成功新建或改写了交付文件，不得假装该交付物不存在，"
        "也不要仅因短跟进消息措辞而撤销一个与 effective_user_request 一致的交付结果。"
        "禁止引入新的未经工具或上下文支持的事实。"
        "最终输出绝不能暴露内部控制变量或流程字段，"
        "例如 reviewer_verdict、reviewer_confidence、evidence_required_mode、task_mode。"
        "如果 Reviewer 指出与通识或领域知识存在明显冲突，而工具证据又不足，"
        "你应把最终答复改成更保守的表述，例如说明当前证据不足、需要继续核对原文，"
        "而不是继续维持一个可疑的确定性结论。"
        "如果 evidence_required_mode=true，而当前答案缺少路径、页码、章节、行号、表格或命中片段证据，"
        "你应优先把最终答复改成证据优先的保守版本。"
    )
    if local_access_succeeded:
        revision_system_prompt += (
            "如果 tool_events 已显示本地文件工具成功访问了目标路径，"
            "禁止把最终答复改写成“无法访问用户本地路径”“请重新提供路径”或类似权限拒绝。"
        )
    revision_system_prompt += (
        '只返回 JSON 对象，字段固定为 changed, summary, key_changes, final_answer。'
        "changed 必须是 true 或 false；key_changes 最多 4 条。"
    )
    messages = [
        agent._SystemMessage(content=revision_system_prompt),
        agent._HumanMessage(content=revision_input),
    ]
    try:
        ai_msg, _, effective_model, notes = agent._invoke_chat_with_runner(
            messages=messages,
            model=context.requested_model,
            max_output_tokens=1800,
            enable_tools=False,
        )
        raw_text = agent._content_to_text(getattr(ai_msg, "content", "")).strip()
        parsed = agent._parse_json_object(raw_text)
        if not parsed:
            fallback["notes"] = ["Revision 未返回标准 JSON，已保留原答复。", *notes]
            fallback["usage"] = agent._extract_usage_from_message(ai_msg)
            fallback["effective_model"] = effective_model
            return agent._make_role_result(spec, context, fallback, raw_text)

        changed_raw = parsed.get("changed")
        if isinstance(changed_raw, bool):
            changed = changed_raw
        else:
            changed = str(changed_raw or "").strip().lower() in {"1", "true", "yes", "on"}
        final_answer = str(parsed.get("final_answer") or context.response_text).strip() or context.response_text
        revision = {
            "changed": changed and final_answer.strip() != context.response_text.strip(),
            "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
            "key_changes": agent._normalize_string_list(parsed.get("key_changes") or [], limit=4, item_limit=180),
            "final_answer": final_answer,
            "usage": agent._extract_usage_from_message(ai_msg),
            "effective_model": effective_model,
            "notes": notes,
        }
        return agent._make_role_result(spec, context, revision, raw_text)
    except Exception as exc:
        fallback["notes"] = [f"Revision 调用失败，已保留原答复: {agent._shorten(exc, 180)}"]
        raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
        return agent._make_role_result(spec, context, fallback, raw_text)
