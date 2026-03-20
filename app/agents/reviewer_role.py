from __future__ import annotations

import json
from typing import Any, Callable

from app.role_runtime import RoleContext, RoleResult


def run_reviewer_role(
    agent: Any,
    *,
    context: RoleContext,
    debug_cb: Callable[[str, str, str], None] | None = None,
    trace_cb: Callable[[str], None] | None = None,
) -> RoleResult:
    spec = agent._make_role_spec(
        "reviewer",
        description="对最终答复做覆盖度、证据链和风险审阅。",
        tool_names=agent._reviewer_readonly_tool_names(),
        output_keys=["verdict", "confidence", "summary", "strengths", "risks", "followups"],
    )
    tool_summaries = agent._summarize_tool_events_for_review(context.tool_events, limit=12)
    write_actions = agent._summarize_write_tool_events(context.tool_events, limit=6)
    attachment_summary = agent._summarize_attachment_metas_for_agents(context.attachment_metas)
    validation_context = agent._summarize_validation_context(context.tool_events)
    local_access_succeeded = agent._has_successful_local_file_access(context.tool_events)
    conflict_lines = [
        f"conflict_has_conflict={str(bool(context.conflict_brief.get('has_conflict'))).lower()}",
        f"conflict_summary={str(context.conflict_brief.get('summary') or '').strip() or '(none)'}",
        "conflict_concerns:",
        *[f"- {item}" for item in agent._normalize_string_list(context.conflict_brief.get("concerns") or [], limit=4)],
    ]
    reviewer_input = "\n".join(
        [
            f"effective_user_request:\n{context.primary_user_request or '(empty)'}",
            f"raw_user_message:\n{context.user_message.strip() or '(empty)'}",
            f"history_summary:\n{context.history_summary.strip() or '(none)'}",
            f"attachments:\n{attachment_summary}",
            f"planner_objective:\n{str(context.planner_brief.get('objective') or '').strip() or '(none)'}",
            "planner_plan:",
            *[f"- {item}" for item in agent._normalize_string_list(context.planner_brief.get("plan") or [], limit=6)],
            f"task_mode={'spec_lookup' if context.extra.get('spec_lookup_request') else 'general'}",
            f"evidence_required_mode={str(bool(context.extra.get('evidence_required_mode'))).lower()}",
            f"web_tools_used={str(validation_context['web_tools_used']).lower()}",
            f"web_tools_success={str(validation_context['web_tools_success']).lower()}",
            "web_tool_notes:",
            *[f"- {item}" for item in validation_context["web_tool_notes"]],
            "web_tool_warnings:",
            *[f"- {item}" for item in validation_context["web_tool_warnings"]],
            *conflict_lines,
            "write_actions:",
            *(write_actions or ["(none)"]),
            "tool_events:",
            *(tool_summaries or ["(none)"]),
            "execution_trace_tail:",
            *[f"- {line}" for line in context.execution_trace[-8:]],
            f"final_text:\n{context.response_text.strip() or '(empty)'}",
        ]
    )
    fallback = {
        "verdict": "pass",
        "confidence": "medium",
        "summary": "Reviewer 未发现阻断性问题。",
        "strengths": ["已完成基础自检。"],
        "risks": [],
        "followups": [],
        "usage": agent._empty_usage(),
        "effective_model": context.requested_model,
        "notes": [],
    }
    readonly_tools = list(spec.tool_names)
    reviewer_system_prompt = (
        "你是 Reviewer Agent。检查最终答复是否覆盖用户目标、是否基于已有工具证据、是否存在明显遗漏。"
        "如果 raw_user_message 看起来只是短跟进/纠偏，而 effective_user_request 延续了上一轮完整目标，"
        "你必须以 effective_user_request 作为主要评审目标。"
        "如果 attachments 段列出了本轮附件，你必须把这些附件视为已存在且对当前任务有效，"
        "不能因为 raw_user_message 没有重复列出附件名，就误判为附件不存在、未提供或不在本轮范围内。"
        "你的 verdict 必须使用三级结论：pass、warn、block。"
        "pass = 结论和证据都足够，可以直接放行；"
        "warn = 核心方向基本正确，但证据表达、引用粒度或措辞需要补强，不应全盘否决；"
        "block = 证据链明显缺失、独立复核冲突明显、或当前结论高风险到不能直接交付。"
        "如果 tool_events 或 write_actions 已显示 Worker 成功新建/改写了交付文件，"
        "不得仅因为 raw_user_message 里出现“查看/查找旧文件”之类的短跟进措辞，就误判为偏离任务。"
        "如果任务是 spec_lookup，那么没有 search_text_in_file + read_text_file 的取证链时通常应为 block；"
        "如果已经有命中和相关信息，但缺少页码/章节/命中片段等可复核表达，通常应为 warn，而不是 block。"
        "如果 evidence_required_mode=true，那么你必须优先使用只读工具做独立复核。"
        "优先考虑 fact_check_file、read_section_by_heading、search_text_in_file、table_extract、search_codebase、search_web、fetch_web。"
        "你还要使用自己的通识和领域知识做冲突检测："
        "如果最终答复与广为人知的事实、常见协议知识或成熟工程常识明显冲突，"
        "即使文案表面自洽，也必须标记为 warn 或 block，并在 risks/followups 中明确要求重新取证。"
        "但你的知识只能用于报警和指出不一致，不能替代工具证据直接宣称文档事实。"
        "Conflict Detector 只是逻辑/通识报警器，不是最终裁决者。"
        "如果本轮已经成功使用联网工具获得实时来源，"
        "不得仅因为“模型原生不支持实时信息”就给出 warn 或 block；"
        "只有在来源不可靠、抓取 warning 明显、网页正文不足，或你独立复核仍不足时，才可降级。"
    )
    if local_access_succeeded:
        reviewer_system_prompt += (
            "如果 tool_events 已显示本地文件工具成功访问了目标路径，"
            "不得再以“无法访问用户本地路径/未提供路径”为理由给出 warn 或 block；"
            "此时应评估的是证据是否充分，而不是权限是否存在。"
        )
    reviewer_system_prompt += (
        "不要输出思维链。"
        '只返回 JSON 对象，字段固定为 verdict, confidence, summary, strengths, risks, followups。'
        "verdict 只能是 pass, warn, block；confidence 只能是 high, medium, low。"
    )
    messages = [
        agent._SystemMessage(content=reviewer_system_prompt),
        agent._HumanMessage(content=reviewer_input),
    ]
    try:
        usage_total = agent._empty_usage()
        notes: list[str] = []
        reviewer_tool_names: list[str] = []
        reviewer_evidence: list[str] = []
        ai_msg, runner, effective_model, invoke_notes = agent._invoke_chat_with_runner(
            messages=messages,
            model=context.requested_model,
            max_output_tokens=1200,
            enable_tools=True,
            tool_names=readonly_tools,
        )
        notes.extend(invoke_notes)
        usage_total = agent._merge_usage(usage_total, agent._extract_usage_from_message(ai_msg))
        nudge_budget = 1 if (bool(context.extra.get("evidence_required_mode")) or bool(context.attachment_metas) or local_access_succeeded) else 0
        for _ in range(12):
            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            if not tool_calls:
                if nudge_budget > 0 and not reviewer_tool_names:
                    nudge_budget -= 1
                    messages.append(ai_msg)
                    messages.append(
                        agent._SystemMessage(
                            content=(
                                "请先完成独立复核，再输出 JSON。"
                                "优先调用 fact_check_file；若需要精读文档章节，调用 read_section_by_heading 或 search_text_in_file。"
                                "如果是代码任务，优先调用 search_codebase。"
                                "如果涉及实时信息、新闻、网页来源或联网事实，优先调用 search_web 和 fetch_web。"
                            )
                        )
                    )
                    ai_msg, runner, effective_model, invoke_notes = agent._invoke_with_runner_recovery(
                        runner=runner,
                        messages=messages,
                        model=effective_model,
                        max_output_tokens=1200,
                        enable_tools=True,
                        tool_names=readonly_tools,
                    )
                    notes.extend(invoke_notes)
                    usage_total = agent._merge_usage(usage_total, agent._extract_usage_from_message(ai_msg))
                    continue
                break

            messages.append(ai_msg)
            for call in tool_calls:
                name = call.get("name") or "unknown"
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                if debug_cb is not None:
                    debug_cb(
                        "llm_to_backend",
                        f"Reviewer -> Coordinator（请求工具 {name}）",
                        "\n".join(
                            [
                                f"tool={name}",
                                f"args={agent._shorten(json.dumps(args, ensure_ascii=False), 1200)}",
                            ]
                        ),
                    )
                result = agent.tools.execute(name, args)
                result_json = json.dumps(result, ensure_ascii=False)
                result_ok = bool(result.get("ok")) if isinstance(result, dict) else False
                tool_payload, trim_note = agent._prepare_tool_result_for_llm(
                    name=name,
                    arguments=args,
                    raw_result=result,
                    raw_json=result_json,
                )
                if result_ok:
                    reviewer_tool_names.append(name)
                evidence_summary = agent._summarize_reviewer_tool_result(name=name, result=result)
                if evidence_summary:
                    reviewer_evidence.append(evidence_summary)
                    if trace_cb is not None:
                        trace_cb(f"Reviewer 复核: {evidence_summary}")
                if trim_note:
                    notes.append(trim_note)
                if debug_cb is not None:
                    debug_cb(
                        "backend_tool",
                        f"Coordinator 执行 Reviewer 只读工具 {name}",
                        "\n".join(
                            [
                                f"tool={name}",
                                f"summary={evidence_summary or '(none)'}",
                                f"result={agent._shorten(result_json, 1800)}",
                            ]
                        ),
                    )
                messages.append(
                    agent._ToolMessage(
                        content=tool_payload,
                        tool_call_id=call.get("id") or f"reviewer_{len(reviewer_tool_names)}",
                        name=name,
                    )
                )
                if debug_cb is not None:
                    debug_cb(
                        "backend_to_llm",
                        f"Coordinator -> Reviewer（工具结果 {name}）",
                        "\n".join(
                            [
                                f"tool={name}",
                                f"summary={evidence_summary or '(none)'}",
                                f"tool_payload={agent._shorten(tool_payload, 1800)}",
                            ]
                        ),
                    )
            ai_msg, runner, effective_model, invoke_notes = agent._invoke_with_runner_recovery(
                runner=runner,
                messages=messages,
                model=effective_model,
                max_output_tokens=1200,
                enable_tools=True,
                tool_names=readonly_tools,
            )
            notes.extend(invoke_notes)
            usage_total = agent._merge_usage(usage_total, agent._extract_usage_from_message(ai_msg))

        raw_text = agent._content_to_text(getattr(ai_msg, "content", "")).strip()
        parsed = agent._parse_json_object(raw_text)
        if not parsed:
            fallback["notes"] = ["Reviewer 未返回标准 JSON，已按保守策略记录。", *notes]
            fallback["usage"] = usage_total
            fallback["effective_model"] = effective_model
            return agent._make_role_result(spec, context, fallback, raw_text)

        verdict = agent._normalize_reviewer_verdict(
            parsed.get("verdict"),
            risks=parsed.get("risks") or [],
            followups=parsed.get("followups") or [],
            spec_lookup_request=bool(context.extra.get("spec_lookup_request")),
            evidence_required_mode=bool(context.extra.get("evidence_required_mode")),
            readonly_checks=reviewer_tool_names,
            conflict_has_conflict=bool(context.conflict_brief.get("has_conflict")),
            conflict_realtime_only=agent._conflict_is_realtime_capability_warning(context.conflict_brief),
            web_tools_success=bool(validation_context["web_tools_success"]),
            attachment_context_available=bool(context.attachment_metas) or local_access_succeeded,
        )
        confidence = str(parsed.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        reviewer = {
            "verdict": verdict,
            "confidence": confidence,
            "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
            "strengths": agent._normalize_string_list(parsed.get("strengths") or [], limit=3, item_limit=180),
            "risks": agent._normalize_string_list(parsed.get("risks") or [], limit=4, item_limit=180),
            "followups": agent._normalize_string_list(parsed.get("followups") or [], limit=3, item_limit=180),
            "usage": usage_total,
            "effective_model": effective_model,
            "notes": notes,
            "readonly_checks": reviewer_tool_names,
            "readonly_evidence": reviewer_evidence,
        }
        return agent._make_role_result(spec, context, reviewer, raw_text)
    except Exception as exc:
        fallback["notes"] = [f"Reviewer 调用失败，已跳过最终审阅: {agent._shorten(exc, 180)}"]
        raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
        return agent._make_role_result(spec, context, fallback, raw_text)
