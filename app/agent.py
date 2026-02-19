from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from app.attachments import extract_document_text, image_to_data_url_with_meta, summarize_file_payload
from app.config import AppConfig
from app.local_tools import LocalToolExecutor
from app.models import ChatSettings, ToolEvent


_STYLE_HINTS = {
    "short": "回答尽量简短，先给结论，再给最多3条关键点。",
    "normal": "回答清晰、可执行，避免冗长。",
    "long": "回答可适当详细，但要结构化并突出行动建议。",
}

_NEWS_HINTS = (
    "news",
    "latest",
    "breaking",
    "headline",
    "today",
    "score",
    "scores",
    "新闻",
    "消息",
    "今日",
    "今天",
    "战报",
    "比分",
    "ニュース",
)

_ATTACHMENT_INLINE_MAX_BYTES = 1 * 1024 * 1024
_ATTACHMENT_INLINE_MAX_CHARS_SOFT = 80000
_FOLLOWUP_INLINE_MAX_BYTES = 256 * 1024


class RunShellArgs(BaseModel):
    command: str = Field(description="Shell command, e.g. `ls -la` or `rg TODO .`")
    cwd: str = Field(default=".", description="Working directory relative to workspace")
    timeout_sec: int = Field(default=15, ge=1, le=120)


class ListDirectoryArgs(BaseModel):
    path: str = Field(default=".")
    max_entries: int = Field(default=200, ge=1, le=500)


class ReadTextFileArgs(BaseModel):
    path: str
    start_char: int = Field(default=0, ge=0)
    max_chars: int = Field(default=200000, ge=128, le=1000000)


class CopyFileArgs(BaseModel):
    src_path: str
    dst_path: str
    overwrite: bool = True
    create_dirs: bool = True


class ExtractZipArgs(BaseModel):
    zip_path: str
    dst_dir: str = Field(default="", description="Destination directory. Empty means sibling folder next to zip file.")
    overwrite: bool = True
    create_dirs: bool = True
    max_entries: int = Field(default=20000, ge=1, le=100000)
    max_total_bytes: int = Field(default=524288000, ge=1024, le=2147483648)


class WriteTextFileArgs(BaseModel):
    path: str
    content: str
    overwrite: bool = True
    create_dirs: bool = True


class AppendTextFileArgs(BaseModel):
    path: str
    content: str
    create_if_missing: bool = True
    create_dirs: bool = True


class ReplaceInFileArgs(BaseModel):
    path: str
    old_text: str
    new_text: str
    replace_all: bool = False
    max_replacements: int = Field(default=1, ge=1, le=200)


class FetchWebArgs(BaseModel):
    url: str
    max_chars: int = Field(default=120000, ge=512, le=500000)
    timeout_sec: int = Field(default=12, ge=3, le=30)


class DownloadWebFileArgs(BaseModel):
    url: str
    dst_path: str = Field(default="", description="Destination path. Empty means auto-save to downloads/<filename>.")
    overwrite: bool = True
    create_dirs: bool = True
    timeout_sec: int = Field(default=20, ge=3, le=120)
    max_bytes: int = Field(default=52428800, ge=1024, le=209715200)


class SearchWebArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)
    timeout_sec: int = Field(default=12, ge=3, le=30)


class ListSessionsArgs(BaseModel):
    max_sessions: int = Field(default=20, ge=1, le=200)


class ReadSessionHistoryArgs(BaseModel):
    session_id: str
    max_turns: int = Field(default=80, ge=1, le=800)


class OfficeAgent:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.tools = LocalToolExecutor(config)

        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
            from langchain_core.tools import StructuredTool
            from langchain_openai import ChatOpenAI
        except Exception as exc:
            raise RuntimeError(
                "Missing dependency: langchain_openai. Install with `pip install langchain-openai`."
            ) from exc

        self._AIMessage = AIMessage
        self._HumanMessage = HumanMessage
        self._SystemMessage = SystemMessage
        self._ToolMessage = ToolMessage
        self._StructuredTool = StructuredTool
        self._ChatOpenAI = ChatOpenAI
        self._lc_tools = self._build_langchain_tools()
        self._model_failover_lock = threading.Lock()
        self._model_failover_state: dict[str, dict[str, int | float]] = {}

    def maybe_compact_session(self, session: dict[str, Any], keep_last_turns: int) -> bool:
        turns = session.get("turns", [])
        if len(turns) <= self.config.summary_trigger_turns:
            return False

        keep = max(2, min(2000, keep_last_turns))
        older = turns[:-keep]
        recent = turns[-keep:]
        if not older:
            return False

        existing_summary = session.get("summary", "")
        session["summary"] = self._summarize_turns(existing_summary, older)
        session["turns"] = recent
        return True

    def _summarize_turns(self, existing_summary: str, older_turns: list[dict[str, Any]]) -> str:
        transcript = []
        if existing_summary:
            transcript.append(f"已有摘要:\n{existing_summary}\n")

        for turn in older_turns:
            role = turn.get("role", "user")
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            transcript.append(f"[{role}] {text}")

        raw = "\n".join(transcript)
        if not raw.strip():
            return existing_summary

        try:
            prompt_messages = [
                self._SystemMessage(
                    content=(
                        "你是会话摘要器。请把历史对话压缩成可供后续继续工作的摘要，"
                        "要保留目标、关键约束、已完成动作、未完成事项。"
                    )
                ),
                self._HumanMessage(content=raw),
            ]
            response = self._invoke_with_405_fallback(
                messages=prompt_messages,
                model=self.config.summary_model,
                max_output_tokens=450,
                enable_tools=False,
            )
            summarized = self._content_to_text(response.content).strip()
            if summarized:
                return summarized
        except Exception:
            pass

        lines: list[str] = []
        if existing_summary:
            lines.append(existing_summary)
        for turn in older_turns[-20:]:
            role = turn.get("role", "user")
            text = (turn.get("text") or "").replace("\n", " ").strip()
            if text:
                lines.append(f"[{role}] {text[:220]}")
        return "\n".join(lines)[:5000]

    def run_chat(
        self,
        history_turns: list[dict[str, Any]],
        summary: str,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        session_id: str | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[str, list[ToolEvent], str, list[str], list[str], list[dict[str, Any]], dict[str, int], str]:
        requested_model = settings.model or self.config.default_model
        effective_model = requested_model
        style_hint = _STYLE_HINTS.get(settings.response_style, _STYLE_HINTS["normal"])
        execution_plan = self._build_execution_plan(attachment_metas=attachment_metas, settings=settings)
        execution_trace: list[str] = []
        debug_flow: list[dict[str, Any]] = []
        usage_total = self._empty_usage()
        allowed_roots_text = ", ".join(str(p) for p in self.config.allowed_roots)
        session_tools_hint = (
            "当用户提到“之前/上次会话里说过什么”时，可调用 list_sessions 和 read_session_history 主动检索历史，不要先让用户手工找 session_id。\n"
            if self.config.enable_session_tools
            else "当前未启用跨会话工具，不要调用 list_sessions/read_session_history。\n"
        )

        debug_raw = bool(getattr(settings, "debug_raw", False))
        debug_limit = 120000 if debug_raw else 3200
        requested_execution_mode = str(getattr(settings, "execution_mode", "") or self.config.execution_mode).strip().lower()
        if requested_execution_mode not in {"host", "docker"}:
            requested_execution_mode = self.config.execution_mode
        self.tools.set_runtime_context(execution_mode=requested_execution_mode, session_id=session_id)

        def emit_progress(event: str, **payload: Any) -> None:
            if not progress_cb:
                return
            try:
                progress_cb({"event": event, **payload})
            except Exception:
                pass

        def add_trace(message: str) -> None:
            execution_trace.append(message)
            emit_progress("trace", message=message, index=len(execution_trace))

        def add_debug(stage: str, title: str, detail: str) -> None:
            item = {
                "step": len(debug_flow) + 1,
                "stage": stage,
                "title": title,
                "detail": self._shorten(detail, debug_limit),
            }
            debug_flow.append(item)
            emit_progress("debug", item=item)

        def add_tool_event(event: ToolEvent) -> None:
            tool_events.append(event)
            emit_progress("tool_event", item=event.model_dump())

        messages: list[Any] = [
            self._SystemMessage(
                content=(
                    f"{self.config.system_prompt}\n\n"
                    f"输出风格: {style_hint}\n"
                    "处理本地文件请求时，先调用工具再下结论，不要凭空判断权限。\n"
                    f"可访问路径根目录: {allowed_roots_text}\n"
                    "读取文件优先使用 list_directory/read_text_file；"
                    "read_text_file 对本地 PDF/DOCX/MSG 会自动提取文本；"
                    "大文件优先用 read_text_file(start_char, max_chars) 分块读取；"
                    "当用户要求“读完/完整读取/全量分析”时，默认已授权你连续读取，"
                    "应先调用 read_text_file(path=..., start_char=0, max_chars=1000000)，"
                    "若 has_more=true 再自动续读后续分块，不要把“是否继续读取”抛回给用户；"
                    "复制文件优先使用 copy_file（不要用读写拼接，避免截断）；"
                    "解压 zip 文件优先使用 extract_zip；"
                    "用户上传附件时会提供本地路径，处理附件文件请优先使用该路径，不要凭空猜路径。\n"
                    "改写或新建文件优先使用 replace_in_file/write_text_file（大内容可分块配合 append_text_file），尽量使用绝对路径。\n"
                    "当 execution_mode=docker 且调用 run_shell 时，/workspace 与 /allowed/* 是主机目录挂载；"
                    "必须基于工具返回的 host_cwd（以及 mount_mappings）向用户报告主机绝对路径。\n"
                    "禁止回复“文件只在沙箱里所以无法给路径”。\n"
                    "当用户要求查看/分析/改写文件时，默认已授权你直接读取相关文件并连续执行，不要逐步询问“要不要继续读下一步”。\n"
                    "分块读取大文件时，应在同一轮里自动继续调用 read_text_file(start_char, max_chars) 直到信息足够或达到安全上限，"
                    "仅在目标路径不明确、权限不足或文件不存在时再向用户提问。\n"
                    "默认不要向用户逐步播报内部工具执行过程（例如“正在自动写入/继续分块写入/继续读取”）；"
                    "除非用户明确要求过程日志，否则直接给最终结果和必要说明。\n"
                    "不要给用户提供“方案A/方案B/二选一”来规避工具执行；"
                    "只要路径和目标明确，就直接调用工具完成并返回结果。\n"
                    "不要声称“工具未启用/工具未激活/系统无法触发工具”，"
                    "除非你刚刚实际调用工具并收到后端明确错误；否则应直接调用工具执行。\n"
                    f"{session_tools_hint}"
                    "联网任务优先先用 search_web(query) 自动找候选链接，再用 fetch_web(url) 读正文；"
                    "如果用户要求“下载/保存文件（PDF/ZIP/图片等）”，优先使用 download_web_file，不要说只能写 UTF-8。\n"
                    "fetch_web 遇到 PDF 会尝试抽取正文文本；若用户要求原文件落盘，必须用 download_web_file。\n"
                    "除非用户明确指定网址，不要反复要求用户先给 URL。\n"
                    "对新闻/实时信息类问题，若第一次搜索结果不足，先自动改写 query 并重试最多 2 次，"
                    "再决定是否向用户补充提问。\n"
                    "如果参数可合理推断（如标题、默认文件名、默认目录），请直接执行并在回复里说明假设；"
                    "不要因为参数不完整而连续多轮追问。\n"
                    "联网信息不足时，先自动换来源继续抓取；即使正文不完整，也先基于可访问到的标题/摘要给临时结论，"
                    "不要先要求用户补网址。\n"
                    "当用户提“今日棒球新闻/今天棒球新闻”这类泛化请求时，默认范围=MLB+NPB，"
                    "直接给出你抓到的要点和来源，不要先追问范围。\n"
                    "当联网抓取返回 warning（如脚本/反爬页面）时，不要给确定性结论，"
                    "必须明确说明信息不足并建议改查权威来源。"
                )
            )
        ]
        add_trace(f"工具开关: {'开启' if settings.enable_tools else '关闭'}。")
        add_trace(f"执行环境: {requested_execution_mode}。")
        add_trace(f"可访问根目录: {allowed_roots_text}")

        if summary.strip():
            messages.append(self._SystemMessage(content=f"历史摘要:\n{summary}"))
            add_trace("已加载历史摘要，减少上下文占用。")

        for turn in history_turns[-settings.max_context_turns :]:
            role = turn.get("role", "user")
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            if role == "assistant":
                messages.append(self._AIMessage(content=text))
            else:
                messages.append(self._HumanMessage(content=text))
        add_trace(f"已载入最近 {min(len(history_turns), settings.max_context_turns)} 条历史消息。")

        user_content, attachment_note, attachment_issues = self._build_user_content(
            user_message,
            attachment_metas,
            history_turn_count=len(history_turns),
        )
        messages.append(self._HumanMessage(content=user_content))
        tool_events: list[ToolEvent] = []
        if attachment_metas:
            add_trace(f"已处理 {len(attachment_metas)} 个附件输入。")
        for issue in attachment_issues:
            add_trace(f"附件提示: {issue}")

        prefetch_payload = self._auto_prefetch_web(user_message, settings.enable_tools)
        if prefetch_payload:
            messages.append(self._SystemMessage(content=prefetch_payload["context"]))
            add_trace(
                f"已自动预搜索网络候选: {prefetch_payload.get('count', 0)} 条（query={prefetch_payload['query']}）。"
            )
            warning = prefetch_payload.get("warning")
            if warning:
                add_trace(f"预搜索提示: {warning}")
            add_tool_event(
                ToolEvent(
                    name="search_web(auto_prefetch)",
                    input={"query": prefetch_payload["query"], "max_results": prefetch_payload.get("count", 0)},
                    output_preview=self._shorten(
                        json.dumps(prefetch_payload.get("raw_result", {}), ensure_ascii=False),
                        1200,
                    ),
                )
            )
            add_debug(
                stage="backend_tool",
                title="后端自动预搜索 search_web",
                detail=self._shorten(
                    json.dumps(prefetch_payload.get("raw_result", {}), ensure_ascii=False),
                    3200 if not debug_raw else 120000,
                ),
            )

        add_debug(
            stage="backend_to_llm",
            title="后端 -> LLM 请求",
            detail=(
                f"model={requested_model}, enable_tools={settings.enable_tools}, max_output_tokens={settings.max_output_tokens}, "
                f"debug_raw={debug_raw}, "
                f"history_turns_used={min(len(history_turns), settings.max_context_turns)}, "
                f"attachments={len(attachment_metas)}\n"
                f"message_roles={self._summarize_message_roles(messages)}\n"
                f"user_message_preview={self._shorten(user_message, 400 if not debug_raw else 20000)}\n"
                f"request_payload:\n{self._serialize_messages_for_debug(messages, raw_mode=debug_raw)}"
            ),
        )

        add_trace("开始模型推理。")

        try:
            ai_msg, runner, effective_model, failover_notes = self._invoke_chat_with_runner(
                messages=messages,
                model=requested_model,
                max_output_tokens=settings.max_output_tokens,
                enable_tools=settings.enable_tools,
            )
            for note in failover_notes:
                add_trace(note)
            usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
            add_debug(
                stage="llm_to_backend",
                title="LLM -> 后端 首次响应",
                detail=(
                    f"effective_model={effective_model}\n"
                    f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                ),
            )
        except Exception as exc:
            add_trace(f"模型请求失败: {exc}")
            add_debug(stage="llm_error", title="LLM 请求失败", detail=str(exc))
            return (
                f"请求模型失败: {exc}",
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                debug_flow,
                usage_total,
                effective_model,
            )

        auto_nudge_budget = 2
        for _ in range(24):
            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            if not settings.enable_tools or not tool_calls:
                if (
                    settings.enable_tools
                    and auto_nudge_budget > 0
                    and self._looks_like_permission_gate(ai_msg)
                ):
                    auto_nudge_budget -= 1
                    add_trace("检测到模型在等待用户确认，后端已自动要求其直接执行工具。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：避免逐步确认",
                        detail="模型出现“是否继续读取”倾向，已追加系统指令要求直接执行。",
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "不要询问用户是否继续读取、是否继续写入、是否授权或是否确认。"
                                "不要让用户在方案A/方案B之间选择，也不要要求用户二次确认。"
                                "用户当前请求已授权你直接继续执行。"
                                "请立即调用必要工具完成任务（例如 read_text_file/write_text_file/append_text_file/replace_in_file），"
                                "并直接返回最终结果。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = self._invoke_with_runner_recovery(
                            runner=runner,
                            messages=messages,
                            model=effective_model,
                            max_output_tokens=settings.max_output_tokens,
                            enable_tools=settings.enable_tools,
                        )
                        for note in failover_notes:
                            add_trace(note)
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        add_debug(
                            stage="llm_to_backend",
                            title="LLM -> 后端 自动纠偏后响应",
                            detail=(
                                f"effective_model={effective_model}\n"
                                f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                            ),
                        )
                        continue
                    except Exception as exc:
                        add_trace(f"自动纠偏后推理失败: {exc}")
                        add_debug(stage="llm_error", title="自动纠偏后推理失败", detail=str(exc))
                        break
                break

            messages.append(ai_msg)
            for call in tool_calls:
                name = call.get("name") or "unknown"
                arguments = call.get("args") or {}
                if not isinstance(arguments, dict):
                    arguments = {}

                result = self.tools.execute(name, arguments)
                result_json = json.dumps(result, ensure_ascii=False)
                add_trace(f"执行工具: {name}")
                add_debug(
                    stage="llm_to_backend",
                    title=f"LLM -> 后端 工具调用 {name}",
                    detail=f"args={self._shorten(json.dumps(arguments, ensure_ascii=False), 1200 if not debug_raw else 50000)}",
                )

                add_tool_event(
                    ToolEvent(
                        name=name,
                        input=arguments,
                        output_preview=result_json[:1200],
                    )
                )

                call_id = call.get("id") or f"call_{len(tool_events)}"
                tool_message_payload, trim_note = self._prepare_tool_result_for_llm(
                    name=name,
                    arguments=arguments,
                    raw_result=result,
                    raw_json=result_json,
                )
                messages.append(
                    self._ToolMessage(
                        content=tool_message_payload,
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                if trim_note:
                    add_trace(trim_note)
                add_debug(
                    stage="backend_tool",
                    title=f"后端工具执行结果 {name}",
                    detail=self._shorten(result_json, 1800 if not debug_raw else 120000),
                )
                add_debug(
                    stage="backend_to_llm",
                    title=f"后端 -> LLM 工具结果 {name}",
                    detail=self._serialize_tool_message_for_debug(
                        name=name,
                        tool_call_id=call_id,
                        content=tool_message_payload,
                        raw_mode=debug_raw,
                    ),
                )

            try:
                pruned = self._prune_old_tool_messages(messages)
                if pruned > 0:
                    add_trace(f"已裁剪旧工具上下文 {pruned} 条，降低上下文膨胀。")
                ai_msg, runner, effective_model, failover_notes = self._invoke_with_runner_recovery(
                    runner=runner,
                    messages=messages,
                    model=effective_model,
                    max_output_tokens=settings.max_output_tokens,
                    enable_tools=settings.enable_tools,
                )
                for note in failover_notes:
                    add_trace(note)
                usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                add_debug(
                    stage="llm_to_backend",
                    title="LLM -> 后端 后续响应",
                    detail=(
                        f"effective_model={effective_model}\n"
                        f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                    ),
                )
            except Exception as exc:
                add_trace(f"工具后续推理失败: {exc}")
                add_debug(stage="llm_error", title="工具后续推理失败", detail=str(exc))
                return (
                    f"工具执行后续推理失败: {exc}",
                    tool_events,
                    attachment_note,
                    execution_plan,
                    execution_trace,
                    debug_flow,
                    usage_total,
                    effective_model,
                )

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if not text.strip():
            text = "模型未返回可见文本。"
        add_trace("已生成最终答复。")
        add_debug(
            stage="llm_final",
            title="LLM 最终输出",
            detail=(
                f"effective_model={effective_model}\n"
                f"text_chars={len(text)}\npreview={self._shorten(text, 1200 if not debug_raw else 50000)}"
            ),
        )
        return text, tool_events, attachment_note, execution_plan, execution_trace, debug_flow, usage_total, effective_model

    def _auto_prefetch_web(self, user_message: str, enable_tools: bool) -> dict[str, Any] | None:
        if not enable_tools:
            return None
        query = (user_message or "").strip()
        if not query:
            return None
        lowered = query.lower()
        if "http://" in lowered or "https://" in lowered:
            return None
        if not any(hint in lowered for hint in _NEWS_HINTS):
            return None

        variants = [query]
        if "news" not in lowered and "新闻" not in query and "ニュース" not in query:
            variants.append(f"{query} news")
        if "today" not in lowered and "今天" not in query and "今日" not in query:
            variants.append(f"{query} today")

        seen: set[str] = set()
        for candidate in variants:
            q = candidate.strip()
            if not q or q.lower() in seen:
                continue
            seen.add(q.lower())
            result = self.tools.search_web(query=q, max_results=6, timeout_sec=self.config.web_fetch_timeout_sec)
            if not result.get("ok"):
                continue
            count = int(result.get("count") or 0)
            if count <= 0:
                continue
            context = self._format_prefetch_search_context(query=q, result=result)
            return {
                "query": q,
                "count": count,
                "warning": result.get("warning"),
                "context": context,
                "raw_result": result,
            }
        return None

    def _format_prefetch_search_context(self, query: str, result: dict[str, Any]) -> str:
        rows = result.get("results") or []
        if not isinstance(rows, list):
            rows = []
        lines = [
            "自动预搜索结果（后端预取，供本轮直接使用）:",
            f"query={query}",
            f"engine={result.get('engine', 'unknown')}",
        ]
        for idx, item in enumerate(rows[:6], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            published_at = str(item.get("published_at", "")).strip()
            if not title and not url:
                continue
            lines.append(f"{idx}. {title}")
            if published_at:
                lines.append(f"   published_at: {published_at}")
            if url:
                lines.append(f"   url: {url}")
            if snippet:
                lines.append(f"   snippet: {self._shorten(snippet, 220)}")
        warning = str(result.get("warning") or "").strip()
        if warning:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)[:6000]

    def _build_execution_plan(self, attachment_metas: list[dict[str, Any]], settings: ChatSettings) -> list[str]:
        plan = ["理解你的目标和约束。"]
        if attachment_metas:
            plan.append(f"解析附件内容（{len(attachment_metas)} 个）。")
        plan.append(f"结合最近 {settings.max_context_turns} 条历史消息组织上下文。")
        if settings.enable_tools:
            plan.append("如有必要自动连续调用工具（读文件/列目录/执行命令/联网搜索与抓取）获取事实，不逐步征询。")
            if self.config.enable_session_tools:
                plan.append("涉及历史对话时，自动调用会话工具检索旧 session。")
        plan.append("汇总结论并按你选择的回答长度输出。")
        return plan

    def _build_llm(self, model: str, max_output_tokens: int, use_responses_api: bool | None = None):
        selected_use_responses = self.config.openai_use_responses_api if use_responses_api is None else use_responses_api
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "max_tokens": max_output_tokens,
            "use_responses_api": selected_use_responses,
        }
        if self.config.openai_temperature is not None:
            kwargs["temperature"] = self.config.openai_temperature
        if self.config.openai_base_url:
            kwargs["base_url"] = self._normalize_base_url(self.config.openai_base_url)
        if self.config.openai_ca_cert_path:
            # Keep TLS behavior close to curl --cacert for corporate gateways.
            os.environ.setdefault("SSL_CERT_FILE", self.config.openai_ca_cert_path)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", self.config.openai_ca_cert_path)
        return self._ChatOpenAI(**kwargs)

    def _invoke_chat_with_runner(
        self,
        messages: list[Any],
        model: str,
        max_output_tokens: int,
        enable_tools: bool,
    ) -> tuple[Any, Any, str, list[str]]:
        candidates = self._build_model_candidates(model)
        notes: list[str] = []
        last_exc: Exception | None = None
        attempted_any = False

        for candidate in candidates:
            cooldown_left = self._model_cooldown_left(candidate)
            if cooldown_left > 0:
                notes.append(f"模型 {candidate} 仍在冷却中（剩余约 {cooldown_left}s），跳过。")
                continue

            attempted_any = True
            try:
                response, runner, invoke_notes = self._invoke_single_model(
                    messages=messages,
                    model=candidate,
                    max_output_tokens=max_output_tokens,
                    enable_tools=enable_tools,
                )
                self._mark_model_success(candidate)
                if candidate != model:
                    notes.append(f"模型故障转移: {model} -> {candidate}")
                notes.extend(invoke_notes)
                return response, runner, candidate, notes
            except Exception as exc:
                last_exc = exc
                if not self._is_failover_error(exc):
                    raise

                cooldown_sec = self._mark_model_failure(candidate)
                notes.append(
                    f"模型 {candidate} 调用失败（{self._shorten(exc, 220)}），"
                    f"进入冷却 {cooldown_sec}s，尝试下一个候选模型。"
                )
                continue

        # If every candidate is cooling down, force-try the primary model once.
        if not attempted_any and candidates:
            primary = candidates[0]
            notes.append("所有候选模型均处于冷却状态，强制重试主模型一次。")
            response, runner, invoke_notes = self._invoke_single_model(
                messages=messages,
                model=primary,
                max_output_tokens=max_output_tokens,
                enable_tools=enable_tools,
            )
            notes.extend(invoke_notes)
            return response, runner, primary, notes

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No model candidates available")

    def _invoke_with_runner_recovery(
        self,
        runner: Any,
        messages: list[Any],
        model: str,
        max_output_tokens: int,
        enable_tools: bool,
    ) -> tuple[Any, Any, str, list[str]]:
        try:
            return runner.invoke(messages), runner, model, []
        except Exception as exc:
            if not (self._is_failover_error(exc) or self._is_405_error(exc)):
                raise
            recovered_msg, recovered_runner, recovered_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=model,
                max_output_tokens=max_output_tokens,
                enable_tools=enable_tools,
            )
            prefix = f"模型 {model} 在持续推理阶段失败（{self._shorten(exc, 200)}），已自动恢复重试。"
            return recovered_msg, recovered_runner, recovered_model, [prefix, *notes]

    def _invoke_single_model(
        self,
        messages: list[Any],
        model: str,
        max_output_tokens: int,
        enable_tools: bool,
    ) -> tuple[Any, Any, list[str]]:
        notes: list[str] = []
        llm = self._build_llm(model=model, max_output_tokens=max_output_tokens)
        runner = llm.bind_tools(self._lc_tools) if enable_tools else llm
        try:
            return runner.invoke(messages), runner, notes
        except Exception as exc:
            if not self._is_405_error(exc):
                raise

        fallback_use_responses = not self.config.openai_use_responses_api
        notes.append(
            f"模型 {model} 返回 405，自动切换 use_responses_api={str(fallback_use_responses).lower()} 重试。"
        )
        llm_fb = self._build_llm(
            model=model,
            max_output_tokens=max_output_tokens,
            use_responses_api=fallback_use_responses,
        )
        runner_fb = llm_fb.bind_tools(self._lc_tools) if enable_tools else llm_fb
        return runner_fb.invoke(messages), runner_fb, notes

    def _invoke_with_405_fallback(
        self,
        messages: list[Any],
        model: str,
        max_output_tokens: int,
        enable_tools: bool,
    ) -> Any:
        response, _, _, _ = self._invoke_chat_with_runner(
            messages=messages,
            model=model,
            max_output_tokens=max_output_tokens,
            enable_tools=enable_tools,
        )
        return response

    def _build_model_candidates(self, primary_model: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for raw in [primary_model, *self.config.model_fallbacks]:
            model = str(raw or "").strip()
            if not model:
                continue
            key = model.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(model)
        return candidates

    def _mark_model_success(self, model: str) -> None:
        key = model.strip().lower()
        if not key:
            return
        now = time.time()
        with self._model_failover_lock:
            state = self._model_failover_state.setdefault(key, {})
            state["failures"] = 0
            state["cooldown_until"] = 0.0
            state["last_used_at"] = now

    def _mark_model_failure(self, model: str) -> int:
        key = model.strip().lower()
        if not key:
            return self.config.model_cooldown_base_sec
        now = time.time()
        with self._model_failover_lock:
            state = self._model_failover_state.setdefault(key, {})
            failures = int(state.get("failures") or 0) + 1
            state["failures"] = failures
            cooldown = min(
                self.config.model_cooldown_max_sec,
                self.config.model_cooldown_base_sec * (5 ** max(0, failures - 1)),
            )
            state["cooldown_until"] = now + cooldown
            state["last_failed_at"] = now
            return int(cooldown)

    def _model_cooldown_left(self, model: str) -> int:
        key = model.strip().lower()
        if not key:
            return 0
        now = time.time()
        with self._model_failover_lock:
            state = self._model_failover_state.get(key) or {}
            until = float(state.get("cooldown_until") or 0.0)
        if until <= now:
            return 0
        return int(until - now)

    def _build_langchain_tools(self) -> list[Any]:
        tools = [
            self._StructuredTool.from_function(
                name="run_shell",
                description="Run a safe shell command in workspace. Supports simple commands without pipes.",
                args_schema=RunShellArgs,
                func=self._run_shell_tool,
            ),
            self._StructuredTool.from_function(
                name="list_directory",
                description="List files in a workspace directory.",
                args_schema=ListDirectoryArgs,
                func=self._list_directory_tool,
            ),
            self._StructuredTool.from_function(
                name="read_text_file",
                description=(
                    "Read a local text/document file. Auto extracts text from PDF/DOCX/MSG. "
                    "Supports chunked reads with start_char. "
                    "For complete reading use max_chars up to 1000000 and continue while has_more=true."
                ),
                args_schema=ReadTextFileArgs,
                func=self._read_text_file_tool,
            ),
            self._StructuredTool.from_function(
                name="copy_file",
                description="Copy a file (binary-safe) from src_path to dst_path in allowed roots.",
                args_schema=CopyFileArgs,
                func=self._copy_file_tool,
            ),
            self._StructuredTool.from_function(
                name="extract_zip",
                description="Extract a local .zip archive into a target directory (safe, with limits).",
                args_schema=ExtractZipArgs,
                func=self._extract_zip_tool,
            ),
            self._StructuredTool.from_function(
                name="write_text_file",
                description="Create or overwrite a UTF-8 text file in workspace.",
                args_schema=WriteTextFileArgs,
                func=self._write_text_file_tool,
            ),
            self._StructuredTool.from_function(
                name="append_text_file",
                description="Append UTF-8 text to a file (or create if missing) in workspace.",
                args_schema=AppendTextFileArgs,
                func=self._append_text_file_tool,
            ),
            self._StructuredTool.from_function(
                name="replace_in_file",
                description="Replace target text in a UTF-8 text file in workspace.",
                args_schema=ReplaceInFileArgs,
                func=self._replace_in_file_tool,
            ),
            self._StructuredTool.from_function(
                name="fetch_web",
                description="Fetch web content from a URL for information lookup.",
                args_schema=FetchWebArgs,
                func=self._fetch_web_tool,
            ),
            self._StructuredTool.from_function(
                name="download_web_file",
                description="Download and save a web file (binary-safe), e.g. PDF/ZIP/images.",
                args_schema=DownloadWebFileArgs,
                func=self._download_web_file_tool,
            ),
            self._StructuredTool.from_function(
                name="search_web",
                description="Search web by query and return candidate URLs/snippets before fetch_web.",
                args_schema=SearchWebArgs,
                func=self._search_web_tool,
            ),
        ]
        if self.config.enable_session_tools:
            tools.append(
                self._StructuredTool.from_function(
                    name="list_sessions",
                    description="List recent local chat sessions for cross-session context lookup.",
                    args_schema=ListSessionsArgs,
                    func=self._list_sessions_tool,
                )
            )
            tools.append(
                self._StructuredTool.from_function(
                    name="read_session_history",
                    description="Read one local chat session history by session_id.",
                    args_schema=ReadSessionHistoryArgs,
                    func=self._read_session_history_tool,
                )
            )
        return tools

    def _run_shell_tool(self, command: str, cwd: str = ".", timeout_sec: int = 15) -> str:
        result = self.tools.run_shell(command=command, cwd=cwd, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _list_directory_tool(self, path: str = ".", max_entries: int = 200) -> str:
        result = self.tools.list_directory(path=path, max_entries=max_entries)
        return json.dumps(result, ensure_ascii=False)

    def _read_text_file_tool(self, path: str, start_char: int = 0, max_chars: int = 200000) -> str:
        result = self.tools.read_text_file(path=path, start_char=start_char, max_chars=max_chars)
        return json.dumps(result, ensure_ascii=False)

    def _copy_file_tool(
        self, src_path: str, dst_path: str, overwrite: bool = True, create_dirs: bool = True
    ) -> str:
        result = self.tools.copy_file(
            src_path=src_path,
            dst_path=dst_path,
            overwrite=overwrite,
            create_dirs=create_dirs,
        )
        return json.dumps(result, ensure_ascii=False)

    def _extract_zip_tool(
        self,
        zip_path: str,
        dst_dir: str = "",
        overwrite: bool = True,
        create_dirs: bool = True,
        max_entries: int = 20000,
        max_total_bytes: int = 524288000,
    ) -> str:
        result = self.tools.extract_zip(
            zip_path=zip_path,
            dst_dir=dst_dir,
            overwrite=overwrite,
            create_dirs=create_dirs,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        )
        return json.dumps(result, ensure_ascii=False)

    def _write_text_file_tool(
        self, path: str, content: str, overwrite: bool = True, create_dirs: bool = True
    ) -> str:
        result = self.tools.write_text_file(
            path=path,
            content=content,
            overwrite=overwrite,
            create_dirs=create_dirs,
        )
        return json.dumps(result, ensure_ascii=False)

    def _append_text_file_tool(
        self,
        path: str,
        content: str,
        create_if_missing: bool = True,
        create_dirs: bool = True,
    ) -> str:
        result = self.tools.append_text_file(
            path=path,
            content=content,
            create_if_missing=create_if_missing,
            create_dirs=create_dirs,
        )
        return json.dumps(result, ensure_ascii=False)

    def _replace_in_file_tool(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        max_replacements: int = 1,
    ) -> str:
        result = self.tools.replace_in_file(
            path=path,
            old_text=old_text,
            new_text=new_text,
            replace_all=replace_all,
            max_replacements=max_replacements,
        )
        return json.dumps(result, ensure_ascii=False)

    def _fetch_web_tool(self, url: str, max_chars: int = 120000, timeout_sec: int = 12) -> str:
        result = self.tools.fetch_web(url=url, max_chars=max_chars, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _download_web_file_tool(
        self,
        url: str,
        dst_path: str = "",
        overwrite: bool = True,
        create_dirs: bool = True,
        timeout_sec: int = 20,
        max_bytes: int = 52428800,
    ) -> str:
        result = self.tools.download_web_file(
            url=url,
            dst_path=dst_path,
            overwrite=overwrite,
            create_dirs=create_dirs,
            timeout_sec=timeout_sec,
            max_bytes=max_bytes,
        )
        return json.dumps(result, ensure_ascii=False)

    def _search_web_tool(self, query: str, max_results: int = 5, timeout_sec: int = 12) -> str:
        result = self.tools.search_web(query=query, max_results=max_results, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _list_sessions_tool(self, max_sessions: int = 20) -> str:
        result = self.tools.list_sessions(max_sessions=max_sessions)
        return json.dumps(result, ensure_ascii=False)

    def _read_session_history_tool(self, session_id: str, max_turns: int = 80) -> str:
        result = self.tools.read_session_history(session_id=session_id, max_turns=max_turns)
        return json.dumps(result, ensure_ascii=False)

    def _build_user_content(
        self, user_message: str, attachment_metas: list[dict[str, Any]], history_turn_count: int = 0
    ) -> tuple[list[dict[str, Any]], str, list[str]]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": user_message}]
        notes: list[str] = []
        issues: list[str] = []
        inline_max_chars = max(2000, min(self.config.max_attachment_chars, _ATTACHMENT_INLINE_MAX_CHARS_SOFT))

        for meta in attachment_metas:
            name = meta.get("original_name", "file")
            path = meta.get("path", "")
            kind = meta.get("kind", "other")
            mime = meta.get("mime", "application/octet-stream")
            suffix = str(meta.get("suffix", "") or "").lower()
            try:
                file_size = int(meta.get("size") or 0)
            except Exception:
                file_size = 0
            if (not file_size) and path:
                try:
                    file_size = Path(path).stat().st_size
                except Exception:
                    file_size = 0
            local_path_line = f"本地路径: {path}\n" if path else ""
            file_size_line = f"文件大小: {self._format_bytes(file_size)}\n" if file_size > 0 else ""
            zip_hint_line = (
                "该文件是 ZIP，若需要解压可调用 extract_zip(zip_path=该路径, dst_dir=目标目录)。\n"
                if suffix == ".zip"
                else ""
            )

            if kind == "document":
                if history_turn_count > 0 and file_size > _FOLLOWUP_INLINE_MAX_BYTES:
                    parts.append(
                        {
                            "type": "text",
                            "text": (
                                f"[附件文档: {name}] 当前为跟进轮次，为避免重复消耗 token，本轮默认仅提供路径。\n"
                                f"{local_path_line}{file_size_line}{zip_hint_line}"
                                "你应直接调用 read_text_file(path=该路径, start_char=0, max_chars=200000) 分块读取，不要先询问用户。"
                            ),
                        }
                    )
                    notes.append(f"文档(跟进-路径):{name}")
                    issues.append(
                        f"{name} 在跟进轮次仅提供路径（{self._format_bytes(file_size)}），可按需再读，避免重复注入大文本。"
                    )
                    continue

                if file_size > _ATTACHMENT_INLINE_MAX_BYTES:
                    parts.append(
                        {
                            "type": "text",
                            "text": (
                                f"[附件文档: {name}] 文件较大，为避免首轮请求长时间无响应，本轮不自动注入全文。\n"
                                f"{local_path_line}{file_size_line}{zip_hint_line}"
                                "你应直接调用 read_text_file(path=该路径, start_char=0, max_chars=200000) 分块读取后再分析，不要先询问用户。"
                            ),
                        }
                    )
                    notes.append(f"文档(大文件-路径):{name}")
                    issues.append(
                        f"{name} 体积较大({self._format_bytes(file_size)})，未自动注入全文；请用 read_text_file 分块读取。"
                    )
                    continue

                extracted = extract_document_text(path, inline_max_chars)
                if extracted:
                    parts.append(
                        {
                            "type": "text",
                            "text": f"\n[附件文档: {name}]\n{local_path_line}{zip_hint_line}{extracted}",
                        }
                    )
                    notes.append(f"文档:{name}")
                    if extracted.startswith("[文档解析失败:"):
                        issues.append(f"{name} 文档解析失败，模型只收到错误信息。")
                else:
                    try:
                        preview = summarize_file_payload(path, max_bytes=768, max_text_chars=1200)
                        parts.append(
                            {
                                "type": "text",
                                "text": (
                                    f"[附件文档: {name}] 未识别为结构化文本，已附带文件预览。\n"
                                    f"{local_path_line}{zip_hint_line}{preview}"
                                ),
                            }
                        )
                        notes.append(f"文档(预览):{name}")
                        issues.append(f"{name} 未结构化解析，已提供文件预览。")
                    except Exception as exc:
                        parts.append({"type": "text", "text": f"[附件文档: {name}] 读取失败: {exc}"})
                        notes.append(f"文档(失败):{name}")
                        issues.append(f"{name} 文档读取失败: {exc}")
            elif kind == "image":
                try:
                    data_url, warn = image_to_data_url_with_meta(path, mime)
                    parts.append({"type": "text", "text": f"[附件图片: {name}]\n{local_path_line}"})
                    parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    notes.append(f"图片:{name}")
                    if warn:
                        issues.append(f"{name} {warn}")
                except Exception as exc:
                    parts.append({"type": "text", "text": f"[附件图片: {name}] 读取失败: {exc}"})
                    notes.append(f"图片(失败):{name}")
                    issues.append(f"{name} 图片读取失败: {exc}")
            else:
                try:
                    preview = summarize_file_payload(path, max_bytes=768, max_text_chars=1200)
                    parts.append(
                        {
                            "type": "text",
                            "text": (
                                f"[附件: {name}] 二进制/未知类型，已附带文件预览。\n"
                                f"{local_path_line}{zip_hint_line}{preview}"
                            ),
                        }
                    )
                    notes.append(f"其他(预览):{name}")
                    issues.append(f"{name} 附件类型未知，已提供二进制预览。")
                except Exception as exc:
                    parts.append({"type": "text", "text": f"[附件: {name}] 读取失败: {exc}"})
                    notes.append(f"其他(失败):{name}")
                    issues.append(f"{name} 附件读取失败: {exc}")

        return parts, "；".join(notes), issues

    def _prepare_tool_result_for_llm(
        self,
        name: str,
        arguments: dict[str, Any],
        raw_result: Any,
        raw_json: str,
    ) -> tuple[str, str | None]:
        text = str(raw_json or "")
        length = len(text)
        soft = max(2000, int(self.config.tool_result_soft_trim_chars))
        hard = max(soft + 1, int(self.config.tool_result_hard_clear_chars))
        head = max(200, min(int(self.config.tool_result_head_chars), max(200, soft // 2)))
        tail = max(200, min(int(self.config.tool_result_tail_chars), max(200, soft // 2)))

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

    def _prune_old_tool_messages(self, messages: list[Any]) -> int:
        keep_last = max(0, int(self.config.tool_context_prune_keep_last))
        tool_indexes: list[int] = []
        total_chars = 0
        for idx, msg in enumerate(messages):
            if type(msg).__name__ == "ToolMessage":
                tool_indexes.append(idx)
                total_chars += len(self._content_to_text(getattr(msg, "content", "")))
        if total_chars <= int(self.config.tool_result_hard_clear_chars):
            return 0
        if len(tool_indexes) <= keep_last:
            return 0

        pruned = 0
        candidates = tool_indexes[:-keep_last] if keep_last > 0 else tool_indexes
        for idx in candidates:
            msg = messages[idx]
            content = self._content_to_text(getattr(msg, "content", ""))
            if "[tool_result_pruned]" in content:
                continue
            tool_call_id = str(getattr(msg, "tool_call_id", "") or f"pruned_{idx}")
            name = str(getattr(msg, "name", "") or "tool")
            placeholder = {
                "tool": name,
                "trimmed": "history_pruned",
                "note": "Older tool result pruned to control context growth.",
            }
            messages[idx] = self._ToolMessage(
                content=json.dumps(placeholder, ensure_ascii=False),
                tool_call_id=tool_call_id,
                name=name,
            )
            pruned += 1
        return pruned

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")

        out: list[str] = []
        for item in content:
            if isinstance(item, str):
                out.append(item)
                continue
            if not isinstance(item, dict):
                out.append(str(item))
                continue

            item_type = item.get("type")
            if item_type in {"text", "output_text", "input_text"}:
                text = item.get("text")
                if isinstance(text, str) and text:
                    out.append(text)
        return "\n".join(out).strip()

    def _shorten(self, text: Any, limit: int = 800) -> str:
        raw = str(text or "")
        if len(raw) <= limit:
            return raw
        return f"{raw[:limit]}\n...[truncated {len(raw) - limit} chars]"

    def _format_bytes(self, value: int | float | None) -> str:
        try:
            size = float(value or 0)
        except Exception:
            return "0 B"
        if size <= 0:
            return "0 B"
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        idx = 0
        while size >= 1024 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(size)} {units[idx]}"
        return f"{size:.2f} {units[idx]}"

    def _looks_like_permission_gate(self, ai_msg: Any) -> bool:
        text = self._content_to_text(getattr(ai_msg, "content", "")).strip().lower()
        if not text:
            return False
        if len(text) > 5000:
            text = text[:5000]
        patterns = (
            "要不要",
            "是否继续",
            "是否要我",
            "你选",
            "请选择",
            "选一个",
            "选一种",
            "二选一",
            "两种方案",
            "可行方案",
            "方案a",
            "方案b",
            "工具未启用",
            "还没有被激活",
            "工具接口",
            "无法触发",
            "系统不执行写入",
            "请告诉我",
            "你可以告诉我",
            "继续读取吗",
            "继续读吗",
            "怕太大",
            "太大",
            "最终确认",
            "确认句",
            "无需你回答",
            "不执行写入",
            "触发工具调用",
            "必须包含路径",
            "need your confirmation",
            "do you want me to continue",
            "should i continue",
            "please provide instructions",
            "write_text_file",
            "append_text_file",
        )
        if not any(p in text for p in patterns):
            return False
        # Heuristic: avoid over-triggering on normal questions by requiring mention of files/reading.
        file_hints = (
            "文件",
            "读取",
            "写入",
            "生成",
            "保存",
            "read_text_file",
            "write_text_file",
            "append_text_file",
            "chunk",
            "附件",
            "文档",
            "path",
        )
        return any(h in text for h in file_hints)

    def _summarize_message_roles(self, messages: list[Any]) -> str:
        counts: dict[str, int] = {}
        for msg in messages:
            role = type(msg).__name__
            counts[role] = counts.get(role, 0) + 1
        parts = [f"{k}={v}" for k, v in sorted(counts.items())]
        return ", ".join(parts) if parts else "(empty)"

    def _serialize_messages_for_debug(self, messages: list[Any], raw_mode: bool = False) -> str:
        lines: list[str] = []
        max_content = 120000 if raw_mode else 1600
        for idx, msg in enumerate(messages, start=1):
            role = type(msg).__name__
            content = getattr(msg, "content", "")
            lines.append(f"[{idx}] {role}")
            lines.append(self._shorten(self._serialize_content_for_debug(content, raw_mode=raw_mode), max_content))
            lines.append("")
        return "\n".join(lines).strip()

    def _serialize_content_for_debug(self, content: Any, raw_mode: bool = False) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")

        lines: list[str] = []
        for idx, item in enumerate(content, start=1):
            if isinstance(item, str):
                lines.append(f"{idx}. {item}")
                continue
            if not isinstance(item, dict):
                lines.append(f"{idx}. {str(item)}")
                continue

            item_type = item.get("type")
            if item_type == "image_url":
                image_url = item.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else ""
                if isinstance(url, str) and url.startswith("data:"):
                    if raw_mode:
                        preview = self._shorten(url, 1200)
                        lines.append(f"{idx}. image_url(data_url_len={len(url)}) preview={preview}")
                    else:
                        lines.append(f"{idx}. image_url(data_url_len={len(url)}) [omitted]")
                else:
                    lines.append(f"{idx}. {json.dumps(item, ensure_ascii=False, default=str)}")
                continue

            lines.append(f"{idx}. {json.dumps(item, ensure_ascii=False, default=str)}")
        return "\n".join(lines)

    def _summarize_ai_response(self, ai_msg: Any, raw_mode: bool = False) -> str:
        if raw_mode:
            payload = {
                "tool_calls": getattr(ai_msg, "tool_calls", None),
                "content": getattr(ai_msg, "content", None),
                "additional_kwargs": getattr(ai_msg, "additional_kwargs", None),
            }
            return self._shorten(json.dumps(payload, ensure_ascii=False, default=str), 120000)

        lines: list[str] = []
        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if tool_calls:
            lines.append(f"tool_calls={len(tool_calls)}")
            for idx, call in enumerate(tool_calls, start=1):
                name = call.get("name") or "unknown"
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                lines.append(
                    f"{idx}. {name}(args={self._shorten(json.dumps(args, ensure_ascii=False), 600)})"
                )

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if text.strip():
            lines.append(f"text_preview={self._shorten(text, 1200)}")

        if not lines:
            lines.append("empty response content")
        return "\n".join(lines)

    def _serialize_tool_message_for_debug(
        self,
        name: str,
        tool_call_id: str,
        content: str,
        raw_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "name": name,
            "tool_call_id": tool_call_id,
            "payload_chars": len(content),
            "content": content,
        }
        limit = 120000 if raw_mode else 2400
        return self._shorten(json.dumps(payload, ensure_ascii=False), limit)

    def _empty_usage(self) -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "llm_calls": 0,
        }

    def _merge_usage(self, base: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
        merged = dict(base)
        merged["input_tokens"] = int(merged.get("input_tokens", 0)) + int(extra.get("input_tokens", 0))
        merged["output_tokens"] = int(merged.get("output_tokens", 0)) + int(extra.get("output_tokens", 0))
        merged["total_tokens"] = int(merged.get("total_tokens", 0)) + int(extra.get("total_tokens", 0))
        merged["llm_calls"] = int(merged.get("llm_calls", 0)) + int(extra.get("llm_calls", 0))
        return merged

    def _extract_usage_from_message(self, message: Any) -> dict[str, int]:
        usage = self._empty_usage()

        usage_metadata = getattr(message, "usage_metadata", None)
        if isinstance(usage_metadata, dict):
            usage["input_tokens"] = int(usage_metadata.get("input_tokens") or usage_metadata.get("prompt_tokens") or 0)
            usage["output_tokens"] = int(
                usage_metadata.get("output_tokens") or usage_metadata.get("completion_tokens") or 0
            )
            usage["total_tokens"] = int(usage_metadata.get("total_tokens") or 0)

        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, dict):
            token_usage = response_metadata.get("token_usage")
            if isinstance(token_usage, dict):
                if usage["input_tokens"] <= 0:
                    usage["input_tokens"] = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
                if usage["output_tokens"] <= 0:
                    usage["output_tokens"] = int(
                        token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
                    )
                if usage["total_tokens"] <= 0:
                    usage["total_tokens"] = int(token_usage.get("total_tokens") or 0)

        if usage["total_tokens"] <= 0:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

        usage["llm_calls"] = 1 if (usage["input_tokens"] > 0 or usage["output_tokens"] > 0 or usage["total_tokens"] > 0) else 0
        return usage

    def _normalize_base_url(self, raw_url: str) -> str:
        """
        Accept either base URL (..../v1) or full endpoint URL (..../v1/chat/completions).
        """
        url = raw_url.strip().strip("\"'").rstrip("/")
        parsed = urlparse(url)
        path = parsed.path or ""
        suffixes = ["/chat/completions", "/responses", "/v1/chat/completions", "/v1/responses"]
        lowered = path.lower()
        for suffix in suffixes:
            if lowered.endswith(suffix):
                path = path[: -len(suffix)] + ("/v1" if suffix.startswith("/v1/") else "")
                break
        normalized = urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), parsed.params, parsed.query, parsed.fragment))
        return normalized

    def _is_failover_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        hints = (
            "429",
            "rate limit",
            "rate_limit",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "service unavailable",
            "overloaded",
            "connection reset",
            "connection aborted",
            "connection error",
            "502",
            "503",
            "504",
            "quota",
            "insufficient",
            "authentication",
            "unauthorized",
            "forbidden",
            "401",
            "403",
        )
        return any(item in text for item in hints)

    def _is_405_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "405" in text or "method not allowed" in text
