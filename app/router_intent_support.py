from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from app.router_rules import (
    HOLISTIC_DIRECT_PHRASES,
    HOLISTIC_EXPLAIN_MARKERS,
    HOLISTIC_OVERVIEW_MARKERS,
    SOURCE_TRACE_HINTS,
    SPEC_LOOKUP_HINTS,
    SPEC_SCOPE_HINTS,
    TABLE_REFERENCE_HINTS,
    TABLE_REFORMAT_HINTS,
    VERIFICATION_HINTS,
    text_has_any,
)


def looks_like_source_trace_request(user_message: str) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if text_has_any(text, SOURCE_TRACE_HINTS):
        return True
    return bool(
        re.search(r"(?:在哪|哪里|哪儿).{0,6}(?:看到|写到|提到)", text)
        or re.search(r"(?:where).{0,18}(?:see|mention|found)", text)
    )


def has_image_attachments(attachment_metas: list[dict[str, Any]]) -> bool:
    return any(str(meta.get("kind") or "").strip().lower() == "image" for meta in attachment_metas)


def looks_like_image_text_extraction_request(user_message: str) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    hints = (
        "原文",
        "可见文字",
        "完整转录",
        "转录",
        "抄录",
        "逐字",
        "逐行",
        "ocr",
        "图片中可见",
        "截图中可见",
        "text in image",
        "transcribe",
        "verbatim",
    )
    if any(hint in text for hint in hints):
        return True
    return bool(re.search(r"(?:图片|截图|图里|图片里|截图里).{0,8}(?:写了什么|写的什么|写了啥|是什么)", text))


def looks_like_image_capability_denial(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    patterns = (
        "无法直接对图像执行ocr",
        "无法对图像执行ocr",
        "无法执行ocr",
        "目前无法直接对图像",
        "无法读取图片",
        "不能读取图片",
        "无法识别图片",
        "无法查看图片",
        "我目前无法直接",
        "can't directly perform ocr",
        "cannot directly perform ocr",
        "cannot perform ocr",
        "can't perform ocr",
        "cannot read the image",
        "can't read the image",
        "cannot view images",
        "can't view images",
        "unable to process image",
    )
    return any(pattern in lowered for pattern in patterns)


def looks_like_stub_image_transcription(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if len(raw) > 260:
        return False
    lowered = raw.lower()
    intro_markers = (
        "以下为图片中可见",
        "以下是图片中可见",
        "以下为截图中可见",
        "以下是截图中可见",
        "按画面顺序",
        "完整转录",
        "无推测",
        "transcription",
        "verbatim",
    )
    if not any(marker in lowered for marker in intro_markers):
        return False
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) <= 1:
        return True
    body = " ".join(lines[1:]).strip()
    if not body:
        return True
    if re.search(r"[：:]\s*$", lines[0]) and len(body) < 20:
        return True
    return len(body) < 12


def looks_like_inline_code_payload(text: str) -> bool:
    raw = str(text or "").strip()
    if len(raw) < 120:
        return False
    fenced_blocks = re.findall(r"```[A-Za-z0-9_+.-]*\n([\s\S]{80,}?)```", raw)
    code_markers = (
        "def ",
        "class ",
        "return ",
        "import ",
        "from ",
        "const ",
        "let ",
        "function ",
        "public ",
        "private ",
        "if (",
        "=>",
        "</",
        "{",
        "};",
    )
    if any(any(marker in block for marker in code_markers) for block in fenced_blocks[:3]):
        return True
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 6:
        return False
    marker_hits = sum(1 for line in lines[:40] if any(marker in line for marker in code_markers))
    punctuation_hits = sum(1 for line in lines[:40] if line.count("{") + line.count("}") + line.count(";") >= 1)
    return marker_hits >= 4 or (marker_hits >= 2 and punctuation_hits >= 4)


def looks_like_inline_document_payload(agent: Any, user_message: str, code_fence_hints: tuple[str, ...]) -> bool:
    text = str(user_message or "").strip()
    if len(text) < 120:
        return False
    lowered = text.lower()
    if "<?xml" in lowered:
        return True
    if any(marker in lowered for marker in code_fence_hints):
        return True
    if agent._looks_like_inline_code_payload(text):
        return True

    xml_tag_matches = re.findall(r"</?[a-zA-Z_][\w:.-]*(?:\s[^<>]{0,200})?>", text)
    if len(xml_tag_matches) >= 6 and ("\n" in text or len(text) >= 240):
        return True

    json_key_count = len(re.findall(r'"[^"\n]{1,80}"\s*:', text))
    if json_key_count >= 4 and len(text) >= 180:
        return True

    yaml_key_count = len(re.findall(r"(?m)^[A-Za-z0-9_.-]{1,60}:\s+\S", text))
    if yaml_key_count >= 5 and len(text) >= 180:
        return True

    return False


def looks_like_initial_content_triage_request(user_message: str, triage_hints: tuple[str, ...]) -> bool:
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    if any(hint in text for hint in triage_hints):
        return True
    if ("下面" in text or "以下" in text or "below" in text) and (
        "理解" in text or "看懂" in text or "解释" in text or "understand" in text or "read" in text
    ):
        return True
    return False


def looks_like_internal_ticket_reference(user_message: str) -> bool:
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    ticket_markers = (
        "redmine",
        "jira",
        "ticket",
        "issue",
        "工单",
        "票",
        "任务单",
        "缺陷",
        "bug单",
        "需求单",
    )
    internal_markers = (
        "internal",
        "intranet",
        "corp",
        "private",
        "内部",
        "内网",
        "公司",
        "企业",
        "私有",
    )
    has_ticket = any(marker in text for marker in ticket_markers)
    has_internal = any(marker in text for marker in internal_markers)
    has_url = "http://" in text or "https://" in text
    return has_ticket and (has_internal or has_url)


def attachment_needs_tooling(meta: dict[str, Any], attachment_inline_max_bytes: int) -> bool:
    suffix = str(meta.get("suffix") or "").strip().lower()
    kind = str(meta.get("kind") or "").strip().lower()
    try:
        size = int(meta.get("size") or 0)
    except Exception:
        size = 0

    if suffix in {".zip", ".msg"}:
        return True
    if kind == "document" and size > attachment_inline_max_bytes:
        return True
    return False


def attachment_is_inline_parseable(
    agent: Any,
    meta: dict[str, Any],
    *,
    attachment_inline_image_max_bytes: int,
    attachment_inline_max_bytes: int,
) -> bool:
    suffix = str(meta.get("suffix") or "").strip().lower()
    kind = str(meta.get("kind") or "").strip().lower()
    try:
        size = int(meta.get("size") or 0)
    except Exception:
        size = 0
    if kind == "image":
        return size <= attachment_inline_image_max_bytes
    if kind != "document":
        return False
    if agent._attachment_needs_tooling(meta):
        return False
    parseable_suffixes = {
        ".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".pptx", ".pptm",
        ".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".html", ".xml", ".atom",
        ".rss", ".yaml", ".yml", ".log", ".py", ".js", ".ts", ".tsx",
    }
    return suffix in parseable_suffixes and size <= attachment_inline_max_bytes


def looks_like_holistic_document_explanation_request(agent: Any, user_message: str) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if agent._looks_like_source_trace_request(user_message):
        return False
    if text_has_any(text, VERIFICATION_HINTS) or "页码" in text:
        return False
    has_overview = text_has_any(text, HOLISTIC_OVERVIEW_MARKERS)
    has_explain = text_has_any(text, HOLISTIC_EXPLAIN_MARKERS)
    if has_overview and has_explain:
        return True
    return text_has_any(text, HOLISTIC_DIRECT_PHRASES)


def looks_like_spec_lookup_request(agent: Any, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
    if not attachment_metas:
        return False
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if agent._looks_like_holistic_document_explanation_request(user_message):
        return False
    if re.search(r"(?i)\b(?:0x[0-9a-f]{1,4}|[0-9a-f]{1,4}h)\b", text):
        return True
    return text_has_any(text, SPEC_LOOKUP_HINTS)


def looks_like_table_reformat_request(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if "table_extract" in lowered or "read_text_file" in lowered:
        return False
    has_table_ref = text_has_any(lowered, TABLE_REFERENCE_HINTS)
    if not has_table_ref:
        return False
    if text_has_any(lowered, VERIFICATION_HINTS) or any(marker in lowered for marker in ("页码", "路径", "行号")):
        return False
    return text_has_any(lowered, TABLE_REFORMAT_HINTS)


def requires_evidence_mode(agent: Any, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if not attachment_metas and agent._looks_like_inline_document_payload(user_message):
        return False
    if attachment_metas and agent._looks_like_holistic_document_explanation_request(user_message):
        return False
    if not attachment_metas and agent._looks_like_table_reformat_request(text):
        return False
    if (
        not attachment_metas
        and agent._looks_like_context_dependent_followup(user_message)
        and any(
            hint in text
            for hint in (
                "翻译", "译成", "译为", "中文", "英文", "双语", "总结", "概括", "提炼", "整理",
                "改写", "润色", "translate",
            )
        )
    ):
        return False
    if text_has_any(text, VERIFICATION_HINTS):
        return True
    if attachment_metas:
        return text_has_any(text, SPEC_SCOPE_HINTS)
    return False


def looks_like_understanding_request(
    agent: Any,
    user_message: str,
    *,
    understanding_hints: tuple[str, ...],
    news_hints: tuple[str, ...],
) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if any(hint in text for hint in news_hints):
        return False
    if agent._looks_like_inline_document_payload(user_message):
        return True
    if agent._requires_evidence_mode(user_message, []):
        return False
    tool_markers = (
        "read_text_file", "search_text_in_file", "table_extract", "fact_check_file", "search_codebase",
        "search_web", "fetch_web", "download_web_file",
    )
    if any(marker in text for marker in tool_markers):
        return False
    return any(hint in text for hint in understanding_hints)


def looks_like_meeting_minutes_request(
    agent: Any,
    user_message: str,
    *,
    meeting_hints: tuple[str, ...],
    meeting_minutes_action_hints: tuple[str, ...],
) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if agent._requires_evidence_mode(user_message, []):
        return False

    direct_phrases = (
        "会议纪要",
        "会议记录",
        "meeting minutes",
        "meeting notes",
        "minutes of meeting",
    )
    if any(phrase in text for phrase in direct_phrases):
        return True

    has_meeting_context = any(hint in text for hint in meeting_hints)
    has_minutes_intent = any(hint in text for hint in meeting_minutes_action_hints)
    return has_meeting_context and has_minutes_intent


def attachment_needs_tooling_for_turn(
    agent: Any,
    meta: dict[str, Any],
    *,
    history_turn_count: int,
    followup_inline_max_bytes: int,
) -> bool:
    if agent._attachment_needs_tooling(meta):
        return True
    if history_turn_count <= 0:
        return False
    kind = str(meta.get("kind") or "").strip().lower()
    if kind != "document":
        return False
    try:
        size = int(meta.get("size") or 0)
    except Exception:
        size = 0
    path = str(meta.get("path") or "").strip()
    if (not size) and path:
        try:
            size = Path(path).stat().st_size
        except Exception:
            size = 0
    return size > followup_inline_max_bytes


def evidence_mode_needs_more_support(agent: Any, ai_msg: Any, tool_events: list[Any], spec_lookup_request: bool = False) -> bool:
    content = agent._content_to_text(getattr(ai_msg, "content", "")).strip().lower()
    if not content:
        return True

    tool_names = {tool.name for tool in tool_events}
    evidence_tool_hits = tool_names & {
        "search_text_in_file", "multi_query_search", "read_text_file", "read_section_by_heading",
        "table_extract", "fact_check_file", "search_codebase",
    }
    if spec_lookup_request and "search_text_in_file" not in tool_names:
        return True
    if not evidence_tool_hits:
        return True
    if spec_lookup_request and not ({"read_text_file", "read_section_by_heading", "table_extract", "fact_check_file"} & tool_names):
        return True

    evidence_markers = (
        "page", "页", "section", "chapter", "章节", "命中", "片段", "line ", "行 ", "路径", "according to", "在当前提取文本中",
    )
    return not any(marker in content for marker in evidence_markers)


def request_likely_requires_tools(agent: Any, user_message: str, attachment_metas: list[dict[str, Any]], *, news_hints: tuple[str, ...]) -> bool:
    if any(agent._attachment_needs_tooling(meta) for meta in attachment_metas):
        return True
    if any(
        str(meta.get("kind") or "").strip().lower() == "document"
        and not agent._attachment_is_inline_parseable(meta)
        for meta in attachment_metas
    ):
        return True
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if not attachment_metas and agent._looks_like_inline_document_payload(user_message):
        return False
    if "http://" in text or "https://" in text:
        return True

    direct_hints = (
        "路径", "目录", "文件夹", "测试", "用例", "测试文件", "文件名", "扩展名", "函数", "方法", "代码", "源码",
        "代码库", "仓库", "repo", "项目", "实现", "调用点", "定义", "声明", "master", "source", "src", "test",
        "tests", "case", "在哪", "搜索", "上网", "网上", "查一下", "搜一下", "read_text_file", "search_text_in_file",
        "multi_query_search", "read_section_by_heading", "table_extract", "fact_check_file", "search_codebase", "write_text_file",
        "append_text_file", "replace_in_file", "写入", "替换", "更新", "改成", "改为", "保存", "落盘", "apply", "patch",
        "write back", "overwrite", "replace", "update", "run_shell", "search_web", "fetch_web", "download_web_file", ".pdf",
        ".doc", ".docx", ".ppt", ".pptx", ".xlsx", ".csv", ".zip", ".msg", "页码", "定位", "命中", "查证", "核对",
        "according to", "citation",
    )
    if any(hint in text for hint in direct_hints):
        return True
    if agent._looks_like_write_or_edit_action(text):
        return True
    if agent._has_file_like_lookup_token(text):
        return True
    if re.search(r"(?:^|[\s(])(?:/[^\s]+|[A-Za-z][:\\：][\\/][^\s]*)", text):
        return True
    return any(hint in text for hint in news_hints)
