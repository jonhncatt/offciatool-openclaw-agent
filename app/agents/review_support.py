from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import ToolEvent


def summarize_validation_context(agent: Any, tool_events: list[ToolEvent]) -> dict[str, Any]:
    web_tool_prefixes = ("search_web", "fetch_web", "download_web_file")
    notes: list[str] = []
    warnings: list[str] = []
    used = False
    success = False

    for event in tool_events:
        base_name = str(event.name or "").strip()
        if not base_name.startswith(web_tool_prefixes):
            continue
        used = True
        ok_flag = agent._tool_event_ok(event)
        parsed = agent._parse_tool_event_preview(event) or {}
        if ok_flag is True:
            success = True

        detail = base_name
        if base_name.startswith("search_web"):
            query = str((event.input or {}).get("query") or parsed.get("query") or "").strip()
            count = int(parsed.get("count") or 0)
            engine = str(parsed.get("engine") or "").strip()
            parts = [detail]
            if query:
                parts.append(f"query={agent._shorten(query, 60)}")
            if count:
                parts.append(f"count={count}")
            if engine:
                parts.append(f"engine={engine}")
            detail = ", ".join(parts)
        elif base_name == "fetch_web":
            url = str((event.input or {}).get("url") or parsed.get("url") or "").strip()
            source_format = str(parsed.get("source_format") or parsed.get("content_type") or "").strip()
            parts = [detail]
            if url:
                parts.append(f"url={agent._shorten(url, 80)}")
            if source_format:
                parts.append(f"format={source_format}")
            detail = ", ".join(parts)
        elif base_name == "download_web_file":
            url = str((event.input or {}).get("url") or parsed.get("url") or "").strip()
            path = str(parsed.get("path") or "").strip()
            parts = [detail]
            if url:
                parts.append(f"url={agent._shorten(url, 80)}")
            if path:
                parts.append(f"path={agent._shorten(path, 80)}")
            detail = ", ".join(parts)

        if ok_flag is True:
            detail += " [ok]"
        elif ok_flag is False:
            detail += " [failed]"
        notes.append(detail)

        warning = str(parsed.get("warning") or "").strip()
        if warning:
            warnings.append(f"{base_name}: {agent._shorten(warning, 160)}")

    return {
        "web_tools_used": used,
        "web_tools_success": success,
        "web_tool_notes": agent._normalize_string_list(notes, limit=6, item_limit=180),
        "web_tool_warnings": agent._normalize_string_list(warnings, limit=4, item_limit=180),
    }


def has_successful_local_file_access(agent: Any, tool_events: list[ToolEvent]) -> bool:
    local_file_tools = {
        "list_directory",
        "read_text_file",
        "search_text_in_file",
        "multi_query_search",
        "doc_index_build",
        "read_section_by_heading",
        "table_extract",
        "fact_check_file",
        "search_codebase",
        "extract_zip",
        "extract_msg_attachments",
    }
    for event in tool_events:
        name = str(event.name or "").strip()
        if name not in local_file_tools:
            continue
        if agent._tool_event_ok(event) is True:
            return True
    return False


def format_tool_event_for_review(agent: Any, *, idx: int, event: ToolEvent) -> str:
    name = str(event.name or "unknown").strip() or "unknown"
    args = json.dumps(event.input or {}, ensure_ascii=False)
    parsed = agent._parse_tool_event_preview(event) or {}
    status = agent._tool_event_ok(event)
    details: list[str] = []
    if status is True:
        details.append("ok")
    elif status is False:
        details.append("failed")
    path = str(parsed.get("path") or "").strip()
    if path:
        details.append(f"path={path}")
    action = str(parsed.get("action") or "").strip()
    if action:
        details.append(f"action={action}")
    query = str((event.input or {}).get("query") or parsed.get("query") or "").strip()
    if query:
        details.append(f"query={agent._shorten(query, 80)}")
    match_count = parsed.get("match_count")
    if isinstance(match_count, int):
        details.append(f"match_count={match_count}")
    replacements = parsed.get("replacements")
    if isinstance(replacements, int):
        details.append(f"replacements={replacements}")
    error = str(parsed.get("error") or "").strip()
    if error:
        details.append(f"error={agent._shorten(error, 120)}")
    if not details:
        preview = agent._shorten(str(event.output_preview or "").strip(), 120)
        if preview:
            details.append(preview)
    detail_text = "; ".join(details)
    return f"{idx + 1}. {name}({args}){f' -> {detail_text}' if detail_text else ''}"


def summarize_tool_events_for_review(agent: Any, tool_events: list[ToolEvent], limit: int = 12) -> list[str]:
    if not tool_events:
        return []

    keep_names = {
        "write_text_file",
        "append_text_file",
        "replace_in_file",
        "copy_file",
        "extract_zip",
        "extract_msg_attachments",
    }
    kept_indexes: list[int] = []
    seen_indexes: set[int] = set()

    for idx, event in enumerate(tool_events):
        name = str(event.name or "").strip()
        if name in keep_names:
            kept_indexes.append(idx)
            seen_indexes.add(idx)

    tail_keep = max(0, limit - len(kept_indexes))
    for idx in range(max(0, len(tool_events) - tail_keep), len(tool_events)):
        if idx in seen_indexes:
            continue
        kept_indexes.append(idx)
        seen_indexes.add(idx)

    if not kept_indexes:
        kept_indexes = list(range(max(0, len(tool_events) - limit), len(tool_events)))
    kept_indexes = sorted(kept_indexes)[-max(1, limit) :]
    return [format_tool_event_for_review(agent, idx=idx, event=tool_events[idx]) for idx in kept_indexes]


def summarize_write_tool_events(agent: Any, tool_events: list[ToolEvent], limit: int = 6) -> list[str]:
    lines: list[str] = []
    for event in tool_events:
        name = str(event.name or "").strip()
        if name not in {"write_text_file", "append_text_file", "replace_in_file", "copy_file"}:
            continue
        parsed = agent._parse_tool_event_preview(event) or {}
        ok = agent._tool_event_ok(event)
        path = str(parsed.get("path") or "").strip() or str((event.input or {}).get("path") or "").strip()
        action = str(parsed.get("action") or "").strip()
        if name == "copy_file" and not path:
            path = str(parsed.get("dst_path") or (event.input or {}).get("dst_path") or "").strip()
        parts = [name]
        if ok is True:
            parts.append("ok")
        elif ok is False:
            parts.append("failed")
        if action:
            parts.append(f"action={action}")
        if path:
            parts.append(f"path={path}")
        replacements = parsed.get("replacements")
        if isinstance(replacements, int):
            parts.append(f"replacements={replacements}")
        error = str(parsed.get("error") or "").strip()
        if error:
            parts.append(f"error={agent._shorten(error, 120)}")
        lines.append(" | ".join(parts))
    return lines[-max(1, limit) :]


def successful_write_targets(agent: Any, tool_events: list[ToolEvent]) -> list[str]:
    targets: list[str] = []
    for event in tool_events:
        name = str(event.name or "").strip()
        if name not in {"write_text_file", "append_text_file", "replace_in_file", "copy_file"}:
            continue
        if agent._tool_event_ok(event) is not True:
            continue
        parsed = agent._parse_tool_event_preview(event) or {}
        path = str(parsed.get("path") or "").strip()
        if not path:
            path = str((event.input or {}).get("path") or "").strip()
        if name == "copy_file" and not path:
            path = str(parsed.get("dst_path") or (event.input or {}).get("dst_path") or "").strip()
        if path:
            targets.append(path)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in targets:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def text_acknowledges_written_targets(text: str, targets: list[str]) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    tokens: list[str] = []
    for path in targets:
        normalized = str(path or "").strip()
        if not normalized:
            continue
        tokens.append(normalized.lower())
        basename = Path(normalized).name.strip().lower()
        if basename:
            tokens.append(basename)
    if any(token and token in lowered for token in tokens):
        return True
    ack_markers = (
        "已创建",
        "已生成",
        "已写入",
        "已保存",
        "创建了",
        "生成了",
        "写入了",
        "保存到",
        "created",
        "generated",
        "written",
        "saved",
    )
    artifact_markers = ("文件", "文档", "markdown", ".md", ".txt", ".docx", "file", "document")
    return any(marker in lowered for marker in ack_markers) and any(marker in lowered for marker in artifact_markers)


def prepare_tool_result_for_llm(
    agent: Any,
    *,
    name: str,
    arguments: dict[str, Any],
    raw_result: Any,
    raw_json: str,
) -> tuple[str, str | None]:
    text = str(raw_json or "")
    length = len(text)
    soft = max(2000, int(agent.config.tool_result_soft_trim_chars))
    hard = max(soft + 1, int(agent.config.tool_result_hard_clear_chars))
    head = max(200, min(int(agent.config.tool_result_head_chars), max(200, soft // 2)))
    tail = max(200, min(int(agent.config.tool_result_tail_chars), max(200, soft // 2)))

    if length <= soft:
        return text, None

    if length >= hard:
        compact_payload = {
            "ok": raw_result.get("ok") if isinstance(raw_result, dict) else None,
            "tool": name,
            "arguments": arguments,
            "trimmed": "hard",
            "original_chars": length,
            "content_preview_head": text[:head],
            "content_preview_tail": text[-tail:] if tail > 0 else "",
            "note": "Tool result was too large and hard-pruned for context safety.",
        }
        return (
            json.dumps(compact_payload, ensure_ascii=False),
            f"工具结果过大({length} chars)，已做硬裁剪后再喂给模型。",
        )

    trimmed = f"{text[:head]}\n...[tool_result_trimmed {length} chars]...\n{text[-tail:]}"
    return trimmed, f"工具结果较大({length} chars)，已做软裁剪后继续推理。"
