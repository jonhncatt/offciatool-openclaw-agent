from __future__ import annotations

import json
from typing import Any

from app.role_runtime import RoleContext, RoleResult


def run_structurer_role(agent: Any, *, context: RoleContext) -> RoleResult:
    spec = agent._make_role_spec(
        "structurer",
        description="把最终答复整理成结构化证据包。",
        output_keys=["summary", "claims", "warnings", "citations"],
    )
    citations = list(context.extra.get("citations") or [])
    reviewer_payload = context.reviewer_brief
    conflict_payload = context.conflict_brief
    fallback = agent._fallback_answer_bundle(
        final_text=context.response_text,
        citations=citations,
        reviewer_brief=reviewer_payload,
        conflict_brief=conflict_payload,
    )
    citation_lines: list[str] = []
    for item in citations[:10]:
        citation_lines.extend(
            [
                f"id={item.get('id')}",
                f"kind={item.get('kind') or 'evidence'}",
                f"tool={item.get('tool') or '(unknown)'}",
                f"source_type={item.get('source_type') or 'other'}",
                f"label={item.get('label') or '(none)'}",
                f"url={item.get('url') or '(none)'}",
                f"path={item.get('path') or '(none)'}",
                f"locator={item.get('locator') or '(none)'}",
                f"excerpt={agent._shorten(item.get('excerpt') or '', 260)}",
                f"warning={item.get('warning') or '(none)'}",
                "",
            ]
        )
    structurer_input = "\n".join(
        [
            f"final_answer:\n{context.response_text.strip() or '(empty)'}",
            f"reviewer_verdict={str(reviewer_payload.get('verdict') or 'pass').strip()}",
            "reviewer_risks:",
            *[f"- {item}" for item in agent._normalize_string_list(reviewer_payload.get("risks") or [], limit=4)],
            "reviewer_followups:",
            *[f"- {item}" for item in agent._normalize_string_list(reviewer_payload.get("followups") or [], limit=4)],
            f"conflict_summary={str(conflict_payload.get('summary') or '').strip() or '(none)'}",
            "citations:",
            *(citation_lines or ["(none)"]),
        ]
    )
    messages = [
        agent._SystemMessage(
            content=(
                "你是 Answer Structurer。"
                "把最终答复整理为结构化证据包。"
                "不要改写事实本身，不要输出思维链。"
                "你只能使用输入里已经给出的 citation id，禁止捏造新来源。"
                "kind=evidence 才能视为已取证来源；kind=candidate 只是搜索候选，不足以单独支撑确定性结论。"
                "claims 最多 5 条，每条都必须简洁。"
                "如果某条结论没有足够证据，status 必须是 needs_review 或 partially_supported。"
                "warnings 应优先写来源 warning、证据不足、Reviewer 风险。"
                '只返回 JSON 对象，字段固定为 summary, claims, warnings。'
                "claims 中每项字段固定为 statement, citation_ids, confidence, status。"
                "confidence 只能是 high, medium, low；"
                "status 只能是 supported, partially_supported, needs_review。"
            )
        ),
        agent._HumanMessage(content=structurer_input),
    ]
    try:
        ai_msg, _, effective_model, notes = agent._invoke_chat_with_runner(
            messages=messages,
            model=context.requested_model,
            max_output_tokens=1400,
            enable_tools=False,
        )
        raw_text = agent._content_to_text(getattr(ai_msg, "content", "")).strip()
        parsed = agent._parse_json_object(raw_text)
        if not parsed:
            fallback["notes"] = ["Structurer 未返回标准 JSON，已使用后端降级结构化结果。", *notes]
            fallback["usage"] = agent._extract_usage_from_message(ai_msg)
            fallback["effective_model"] = effective_model
            bundle = agent._strip_answer_bundle_meta(fallback)
            bundle["usage"] = fallback["usage"]
            bundle["effective_model"] = fallback["effective_model"]
            bundle["notes"] = fallback["notes"]
            return agent._make_role_result(spec, context, bundle, raw_text)

        valid_ids = {str(item.get("id") or "").strip() for item in citations}
        citations_by_id = {
            str(item.get("id") or "").strip(): item for item in citations if str(item.get("id") or "").strip()
        }
        claims_out: list[dict[str, Any]] = []
        for item in parsed.get("claims") or []:
            if not isinstance(item, dict):
                continue
            statement = str(item.get("statement") or "").strip()
            if not statement:
                continue
            raw_ids = item.get("citation_ids") or []
            if not isinstance(raw_ids, list):
                raw_ids = [raw_ids]
            citation_ids = [str(cid).strip() for cid in raw_ids if str(cid).strip() in valid_ids][:4]
            confidence = str(item.get("confidence") or "medium").strip().lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            status = str(item.get("status") or "supported").strip().lower()
            if status not in {"supported", "partially_supported", "needs_review"}:
                status = "supported" if citation_ids else "needs_review"
            claims_out.append(
                agent._normalize_claim_record(
                    statement=statement,
                    citation_ids=citation_ids,
                    confidence=confidence,
                    status=status,
                    citations_by_id=citations_by_id,
                )
            )
            if len(claims_out) >= 5:
                break

        warnings = agent._normalize_string_list(parsed.get("warnings") or [], limit=5, item_limit=220)
        warnings = agent._augment_bundle_warnings(warnings=warnings, citations=citations)
        bundle = {
            "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
            "claims": claims_out or fallback["claims"],
            "citations": citations,
            "warnings": warnings or fallback["warnings"],
            "usage": agent._extract_usage_from_message(ai_msg),
            "effective_model": effective_model,
            "notes": notes,
        }
        bundle = agent._strip_answer_bundle_meta(bundle) | {
            "usage": bundle["usage"],
            "effective_model": bundle["effective_model"],
            "notes": bundle["notes"],
        }
        return agent._make_role_result(spec, context, bundle, raw_text)
    except Exception as exc:
        fallback["notes"] = [f"Structurer 调用失败，已回退后端结构化结果: {agent._shorten(exc, 180)}"]
        raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
        bundle = agent._strip_answer_bundle_meta(fallback) | {
            "usage": fallback["usage"],
            "effective_model": fallback["effective_model"],
            "notes": fallback["notes"],
        }
        return agent._make_role_result(spec, context, bundle, raw_text)
