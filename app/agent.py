from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from app.attachments import extract_document_text, image_to_data_url_with_meta, summarize_file_payload
from app.config import AppConfig
from app.local_tools import LocalToolExecutor
from app.models import AgentPanel, ChatSettings, ToolEvent


_STYLE_HINTS = {
    "short": "回答尽量简短，先给结论，再给最多3条关键点。",
    "normal": "回答清晰、可执行，避免冗长。",
    "long": "回答可适当详细，但要结构化并突出行动建议。",
}

_NEWS_HINTS = (
    "news",
    "latest",
    "recent",
    "breaking",
    "headline",
    "today",
    "score",
    "scores",
    "最近",
    "近期",
    "近况",
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
_FOLLOWUP_SEARCH_HINTS = (
    "上网查",
    "网上查",
    "查一下",
    "搜一下",
    "再查",
    "继续查",
    "帮我查",
    "帮我搜",
    "再搜",
)

_UNDERSTANDING_HINTS = (
    "总结",
    "总结下",
    "概括",
    "提炼",
    "解读",
    "解释",
    "说明",
    "分析",
    "梳理",
    "翻译",
    "摘要",
    "看懂",
    "说说",
    "summarize",
    "summary",
    "explain",
    "interpret",
    "analyze",
    "analyse",
    "translate",
    "overview",
)

_INLINE_DOC_CODE_FENCE_HINTS = (
    "```xml",
    "```html",
    "```json",
    "```yaml",
    "```yml",
    "```rss",
    "```atom",
)

_SPECIALIST_LABELS = {
    "researcher": "Researcher",
    "file_reader": "FileReader",
    "summarizer": "Summarizer",
    "fixer": "Fixer",
}


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


class SearchTextInFileArgs(BaseModel):
    path: str
    query: str
    max_matches: int = Field(default=8, ge=1, le=20)
    context_chars: int = Field(default=280, ge=40, le=2000)


class MultiQuerySearchArgs(BaseModel):
    path: str
    queries: list[str]
    per_query_max_matches: int = Field(default=3, ge=1, le=10)
    context_chars: int = Field(default=280, ge=40, le=2000)


class DocIndexBuildArgs(BaseModel):
    path: str
    force_rebuild: bool = False
    max_headings: int = Field(default=400, ge=20, le=2000)


class ReadSectionByHeadingArgs(BaseModel):
    path: str
    heading: str
    max_chars: int = Field(default=12000, ge=512, le=50000)


class TableExtractArgs(BaseModel):
    path: str
    query: str = ""
    page_hint: int = Field(default=0, ge=0)
    max_tables: int = Field(default=5, ge=1, le=20)
    max_rows: int = Field(default=25, ge=1, le=200)


class FactCheckFileArgs(BaseModel):
    path: str
    claim: str
    queries: list[str] = Field(default_factory=list)
    max_evidence: int = Field(default=6, ge=1, le=12)


class SearchCodebaseArgs(BaseModel):
    query: str
    root: str = "."
    max_matches: int = Field(default=20, ge=1, le=100)
    file_glob: str = ""
    use_regex: bool = False
    case_sensitive: bool = False


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


class ExtractMsgAttachmentsArgs(BaseModel):
    msg_path: str
    dst_dir: str = Field(default="", description="Destination directory. Empty means <msg_stem>_attachments.")
    overwrite: bool = True
    create_dirs: bool = True
    max_attachments: int = Field(default=500, ge=1, le=5000)
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
        self._lc_tool_map = {getattr(tool, "name", ""): tool for tool in self._lc_tools}
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
    ) -> tuple[
        str,
        list[ToolEvent],
        str,
        list[str],
        list[str],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, int],
        str,
    ]:
        requested_model = settings.model or self.config.default_model
        effective_model = requested_model
        style_hint = _STYLE_HINTS.get(settings.response_style, _STYLE_HINTS["normal"])
        execution_plan: list[str] = []
        execution_trace: list[str] = []
        debug_flow: list[dict[str, Any]] = []
        agent_panels: list[dict[str, Any]] = []
        worker_citation_candidates: list[dict[str, Any]] = []
        answer_bundle: dict[str, Any] = {"summary": "", "claims": [], "citations": [], "warnings": []}
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

        def emit_agent_state() -> None:
            emit_progress("agent_state", panels=list(agent_panels), execution_plan=list(execution_plan))

        def add_panel(role: str, title: str, summary_text: str, bullets: list[str] | None = None) -> None:
            panel = AgentPanel(
                role=role,
                title=title,
                summary=self._shorten(summary_text, 500 if not debug_raw else 4000),
                bullets=self._normalize_string_list(bullets or [], limit=8, item_limit=220),
            )
            agent_panels.append(panel.model_dump())
            emit_agent_state()

        messages: list[Any] = [
            self._SystemMessage(
                content=(
                    f"{self.config.system_prompt}\n\n"
                    f"输出风格: {style_hint}\n"
                    "处理本地文件请求时，先调用工具再下结论，不要凭空判断权限。\n"
                    f"可访问路径根目录: {allowed_roots_text}\n"
                    "读取文件优先使用 list_directory/read_text_file；"
                    "read_text_file 对本地 PDF/DOCX/MSG/XLSX 会自动提取文本；"
                    "当用户在规范/协议/规格书中定位章节、命令码、opcode、寄存器或状态码时，优先使用 search_text_in_file；"
                    "search_text_in_file 会自动尝试 15h/15 h/0x15 这类十六进制变体；"
                    "当用户说“看某一章/某一节/某个 heading”时，优先用 read_section_by_heading；"
                    "当用户说“看表格/参数表/opcode 表”时，优先用 table_extract；"
                    "当用户要求核事实或复核结论时，可用 fact_check_file；"
                    "当用户要求搜代码、定位实现、找调用点时，优先用 search_codebase；"
                    "当需要对同一文件同时尝试多个关键词时，优先用 multi_query_search；"
                    "大 PDF 首次会建索引缓存，必要时可先调用 doc_index_build 查看 heading/缓存状态；"
                    "大文件优先用 read_text_file(start_char, max_chars) 分块读取；"
                    "当用户要求“读完/完整读取/全量分析”时，默认已授权你连续读取，"
                    "应先调用 read_text_file(path=..., start_char=0, max_chars=1000000)，"
                    "若 has_more=true 再自动续读后续分块，不要把“是否继续读取”抛回给用户；"
                    "对于规范/规格书问答，必须先给出命中证据（页码/章节/片段）再下结论；"
                    "若当前提取文本未命中，只能说“在当前提取文本中未定位到”，不得直接断言规范不存在该命令或条目；"
                    "复制文件优先使用 copy_file（不要用读写拼接，避免截断）；"
                    "解压 zip 文件优先使用 extract_zip；"
                    "当用户要求“打开/读取/解析 .msg 邮件里的附件”时，优先调用 extract_msg_attachments(msg_path=...)；"
                    "拿到附件落盘路径后继续调用 read_text_file 或处理图片，不要要求用户手工找目录。\n"
                    "当用户要求“解释邮件全部内容/完整解释邮件”时，默认范围=邮件正文+可解析附件内容；"
                    "不要用“用户未要求附件”作为理由跳过附件解析。\n"
                    "用户上传附件时会提供本地路径，处理附件文件请优先使用该路径，不要凭空猜路径。\n"
                    "改写或新建文件优先使用 replace_in_file/write_text_file（大内容可分块配合 append_text_file），尽量使用绝对路径。\n"
                    "当 execution_mode=docker 且调用 run_shell 时，/workspace 与 /allowed/* 是主机目录挂载；"
                    "必须基于工具返回的 host_cwd（以及 mount_mappings）向用户报告主机绝对路径。\n"
                    "禁止回复“文件只在沙箱里所以无法给路径”。\n"
                    "当用户要求查看/分析/改写文件时，默认已授权你直接读取相关文件并连续执行，不要逐步询问“要不要继续读下一步”。\n"
                    "分块读取大文件时，应在同一轮里自动继续调用 read_text_file(start_char, max_chars) 直到信息足够或达到安全上限，"
                    "仅在目标路径不明确、权限不足或文件不存在时再向用户提问。\n"
                    "如果用户直接在消息里粘贴了 XML/HTML/JSON/YAML 等原始长文本，"
                    "应把它当作当前上下文中的 inline 文档直接分析，"
                    "不要因为它不在本地文件系统就要求用户提供文件路径；"
                    "只有当用户明确要求对磁盘文件做可复核检索时，才需要本地路径和文件工具。\n"
                    "默认不要向用户逐步播报内部工具执行过程（例如“正在自动写入/继续分块写入/继续读取”）；"
                    "除非用户明确要求过程日志，否则直接给最终结果和必要说明。\n"
                    "对于纯知识问答（不需要读写文件或联网抓取），直接回答问题本身；"
                    "不要先输出“收到/会严格遵守/无需调用工具”等流程性话术。\n"
                    "不要给用户提供“方案A/方案B/二选一”来规避工具执行；"
                    "只要路径和目标明确，就直接调用工具完成并返回结果。\n"
                    "不要声称“工具未启用/工具未激活/系统无法触发工具”，"
                    "除非你刚刚实际调用工具并收到后端明确错误；否则应直接调用工具执行。\n"
                    f"{session_tools_hint}"
                    "联网任务优先先用 search_web(query) 自动找候选链接，再用 fetch_web(url) 读正文；"
                    "如果用户要求“下载/保存文件（PDF/ZIP/图片等）”，优先使用 download_web_file，不要说只能写 UTF-8。\n"
                    "fetch_web 遇到 PDF 会尝试抽取正文文本；若用户要求原文件落盘，必须用 download_web_file。\n"
                    "对于公众人物在公开新闻、公开活动、公开比赛、公开采访中的出现地点或行程，"
                    "如果问题明显是在问公开报道中的活动地点（例如是否在某国参加比赛/活动），可以联网搜索并基于公开来源总结；"
                    "只有当用户要求精确实时位置、非公开行踪、住所、酒店、私人行程或可用于跟踪个人的细粒度位置时，才按隐私高风险处理。\n"
                    "除非用户明确指定网址，不要反复要求用户先给 URL。\n"
                    "对新闻/实时信息类问题，若第一次搜索结果不足，先自动改写 query 并重试最多 2 次，"
                    "再决定是否向用户补充提问。\n"
                    "如果当前用户消息只是“上网查一下/再查一下/搜一下”这类短跟进，默认延续最近一轮用户主题，不要假装丢失上下文重新问用户想查什么。\n"
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

        followup_topic_hint = self._build_followup_topic_hint(user_message=user_message, history_turns=history_turns)
        planner_user_message = user_message
        if followup_topic_hint:
            planner_user_message = f"{user_message}\n\n[延续主题]\n{followup_topic_hint}"
            messages.append(
                self._SystemMessage(
                    content=(
                        "检测到本轮用户消息是短跟进请求。"
                        f"默认延续最近一次用户主题：{followup_topic_hint}"
                    )
                )
            )
            add_trace(f"已识别为跟进请求，默认延续主题：{self._shorten(followup_topic_hint, 120)}")

        user_content, attachment_note, attachment_issues = self._build_user_content(
            user_message,
            attachment_metas,
            history_turn_count=len(history_turns),
        )
        messages.append(self._HumanMessage(content=user_content))
        tool_events: list[ToolEvent] = []
        add_debug(
            stage="backend_ingress",
            title="后端接收并整理用户输入",
            detail=(
                f"user_message_chars={len(user_message)}\n"
                f"attachments={len(attachment_metas)}\n"
                f"history_turns_used={min(len(history_turns), settings.max_context_turns)}\n"
                f"user_message_preview={self._shorten(user_message, 400 if not debug_raw else 5000)}\n"
                f"normalized_user_payload:\n{self._serialize_content_for_debug(user_content, raw_mode=debug_raw)}"
            ),
        )
        if attachment_metas:
            add_trace(f"已处理 {len(attachment_metas)} 个附件输入。")
        for issue in attachment_issues:
            add_trace(f"附件提示: {issue}")

        route, router_raw = self._route_request(
            requested_model=requested_model,
            user_message=user_message,
            summary=summary,
            attachment_metas=attachment_metas,
            settings=settings,
        )
        execution_plan[:] = self._build_execution_plan(
            attachment_metas=attachment_metas,
            settings=settings,
            route=route,
        )
        add_trace(
            "Router 分诊完成: "
            f"task_type={route.get('task_type')}, complexity={route.get('complexity')}, source={route.get('source')}。"
        )
        add_debug(
            stage="backend_to_llm" if route.get("source") == "llm_router" else "backend_router",
            title="后端编排器 -> Router" if route.get("source") == "llm_router" else "后端规则 Router",
            detail=(
                f"route={json.dumps(route, ensure_ascii=False)}\n"
                f"raw={self._shorten(router_raw, 2400 if not debug_raw else 120000)}"
            ),
        )
        add_panel(
            "router",
            "Router",
            str(route.get("summary") or "已完成链路分诊。").strip() or "已完成链路分诊。",
            self._format_router_panel_bullets(route),
        )

        router_system_hint = self._router_system_hint(route)
        if router_system_hint:
            messages.insert(1, self._SystemMessage(content=router_system_hint))
            add_trace("多 Agent: Worker 已加载 Router 摘要。")

        spec_lookup_request = self._looks_like_spec_lookup_request(user_message, attachment_metas)
        evidence_required_mode = self._requires_evidence_mode(user_message, attachment_metas)
        if spec_lookup_request:
            messages.append(
                self._SystemMessage(
                    content=(
                        "本轮属于规范/规格书定位任务。"
                        "先用 search_text_in_file 对章节名、命令码、opcode 或寄存器名做命中定位，"
                        "必要时分别尝试章节关键词和 15h/15 h/0x15 这类十六进制变体；"
                        "再用 read_text_file 读取命中附近上下文；"
                        "最终回答必须附带命中证据。"
                        "若未命中，只能说当前提取文本未定位到，不得直接断言规范不存在。"
                    )
                )
            )
            add_trace("已启用规范文档检索模式。")
        if evidence_required_mode and route.get("use_worker_tools"):
            messages.append(
                self._SystemMessage(
                    content=(
                        "本轮已启用 evidence_required_mode。"
                        "对于文件、规范、代码库、章节定位类任务，必须给出证据来源（如路径、页码、章节、行号、命中片段）。"
                        "若证据不足，只能明确说明不足，不得给出无证据的确定性结论。"
                    )
                )
            )
            add_trace("已启用证据优先模式。")

        planner_brief = {
            "objective": self._shorten(planner_user_message.strip(), 220),
            "constraints": [],
            "plan": list(execution_plan),
            "watchouts": [],
            "success_signals": [],
            "usage": self._empty_usage(),
            "effective_model": effective_model,
            "notes": [],
        }
        planner_raw = ""
        if route.get("use_planner"):
            planner_request_detail = "\n".join(
                [
                    f"requested_model={requested_model}",
                    f"response_style={settings.response_style}",
                    f"attachments={len(attachment_metas)}",
                    f"history_summary_chars={len(summary.strip())}",
                    f"user_message_preview={self._shorten(user_message, 400 if not debug_raw else 5000)}",
                ]
            )
            add_debug(
                stage="backend_to_llm",
                title="后端编排器 -> Planner",
                detail=planner_request_detail,
            )
            planner_brief, planner_raw = self._run_planner(
                requested_model=requested_model,
                user_message=planner_user_message,
                summary=summary,
                attachment_metas=attachment_metas,
                settings=settings,
            )
            planner_effective_model = str(planner_brief.get("effective_model") or "").strip()
            if planner_effective_model:
                effective_model = planner_effective_model
            usage_total = self._merge_usage(usage_total, planner_brief.get("usage") or self._empty_usage())
            for note in self._normalize_string_list(planner_brief.get("notes") or [], limit=4, item_limit=200):
                add_trace(note)
            add_trace("多 Agent: Planner 已生成目标摘要与执行计划。")
            add_debug(
                stage="llm_to_backend",
                title="Planner -> 后端编排器",
                detail=(
                    f"effective_model={planner_effective_model or requested_model}\n"
                    f"{self._shorten(planner_raw, 4000 if debug_raw else 1200)}"
                ),
            )
            planner_plan = self._normalize_string_list(planner_brief.get("plan") or [], limit=8, item_limit=160)
            if planner_plan:
                execution_plan[:] = planner_plan
            planner_summary = str(planner_brief.get("objective") or "").strip() or "已生成目标摘要。"
            planner_bullets = (
                self._normalize_string_list(planner_brief.get("constraints") or [], limit=3, item_limit=180)
                + self._normalize_string_list(planner_brief.get("plan") or [], limit=4, item_limit=180)
            )
            add_panel("planner", "Planner", planner_summary, planner_bullets)
            planner_system_hint = self._format_planner_system_hint(planner_brief)
            if planner_system_hint:
                messages.insert(1, self._SystemMessage(content=planner_system_hint))
                add_trace("多 Agent: Worker 已加载 Planner 摘要。")
        else:
            add_trace("Router 已跳过 Planner。")

        specialist_prefetch_query = user_message
        specialist_system_hints: list[str] = []
        for specialist in self._normalize_specialists(route.get("specialists") or []):
            specialist_label = _SPECIALIST_LABELS.get(specialist, specialist)
            add_debug(
                stage="backend_to_llm",
                title=f"后端编排器 -> {specialist_label}",
                detail=(
                    f"model={self.config.summary_model or requested_model}\n"
                    f"task_type={route.get('task_type')}\n"
                    f"attachments={len(attachment_metas)}\n"
                    f"user_message_preview={self._shorten(user_message, 400 if not debug_raw else 5000)}"
                ),
            )
            specialist_brief, specialist_raw = self._run_specialist_role(
                specialist=specialist,
                requested_model=requested_model,
                user_message=user_message,
                summary=summary,
                user_content=user_content,
                attachment_metas=attachment_metas,
                route=route,
            )
            specialist_model = str(specialist_brief.get("effective_model") or "").strip()
            usage_total = self._merge_usage(usage_total, specialist_brief.get("usage") or self._empty_usage())
            for note in self._normalize_string_list(specialist_brief.get("notes") or [], limit=3, item_limit=200):
                add_trace(note)
            add_trace(f"多 Agent: {specialist_label} 已生成专门简报。")
            add_debug(
                stage="llm_to_backend",
                title=f"{specialist_label} -> 后端编排器",
                detail=(
                    f"effective_model={specialist_model or self.config.summary_model or requested_model}\n"
                    f"{self._shorten(specialist_raw, 4000 if debug_raw else 1200)}"
                ),
            )
            add_panel(
                specialist,
                specialist_label,
                str(specialist_brief.get("summary") or "").strip() or f"{specialist_label} 已生成简报。",
                self._normalize_string_list(specialist_brief.get("bullets") or [], limit=4, item_limit=180),
            )
            specialist_hint = self._format_specialist_system_hint(specialist, specialist_brief)
            if specialist_hint:
                specialist_system_hints.append(specialist_hint)
            if specialist == "researcher":
                suggested_queries = self._normalize_string_list(specialist_brief.get("queries") or [], limit=3, item_limit=80)
                if suggested_queries:
                    specialist_prefetch_query = suggested_queries[0]
        for hint in reversed(specialist_system_hints):
            messages.insert(1, self._SystemMessage(content=hint))
        if specialist_system_hints:
            add_trace("多 Agent: Worker 已加载专门角色摘要。")

        prefetch_payload = self._auto_prefetch_web(specialist_prefetch_query, bool(route.get("use_web_prefetch")))
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
                stage="backend_prefetch",
                title="后台预取 search_web（Worker 前置）",
                detail=self._shorten(
                    json.dumps(prefetch_payload.get("raw_result", {}), ensure_ascii=False),
                    3200 if not debug_raw else 120000,
                ),
            )
            worker_citation_candidates = self._merge_citation_candidates(
                worker_citation_candidates,
                self._extract_citations_from_tool_result(
                    name="search_web",
                    arguments={"query": prefetch_payload["query"], "max_results": prefetch_payload.get("count", 0)},
                    result=prefetch_payload.get("raw_result", {}),
                ),
            )

        add_debug(
            stage="multi_agent_worker",
            title="Worker 开始执行",
            detail=(
                f"requested_model={requested_model}\n"
                f"execution_mode={requested_execution_mode}\n"
                f"enable_tools={bool(route.get('use_worker_tools'))}\n"
                f"attachments={len(attachment_metas)}\n"
                f"history_turns_used={min(len(history_turns), settings.max_context_turns)}"
            ),
        )
        add_debug(
            stage="backend_to_llm",
            title="后端编排器 -> Worker",
            detail=(
                f"model={requested_model}, enable_tools={bool(route.get('use_worker_tools'))}, max_output_tokens={settings.max_output_tokens}, "
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
                enable_tools=bool(route.get("use_worker_tools")),
            )
            for note in failover_notes:
                add_trace(note)
            usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
            add_debug(
                stage="llm_to_backend",
                title="Worker -> 后端编排器（首次响应）",
                detail=(
                    f"effective_model={effective_model}\n"
                    f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                ),
            )
        except Exception as exc:
            add_trace(f"模型请求失败: {exc}")
            add_debug(stage="llm_error", title="Worker 模型调用失败", detail=str(exc))
            return (
                f"请求模型失败: {exc}",
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                debug_flow,
                agent_panels,
                answer_bundle,
                usage_total,
                effective_model,
            )

        has_attachments = bool(attachment_metas)
        attachments_need_tooling = any(self._attachment_needs_tooling(meta) for meta in attachment_metas)
        has_msg_attachment = any(str(meta.get("suffix", "") or "").lower() == ".msg" for meta in attachment_metas)
        request_requires_tools = bool(route.get("use_worker_tools")) or attachments_need_tooling
        auto_nudge_budget = 4 if has_attachments else 2
        for _ in range(24):
            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            if not route.get("use_worker_tools") or not tool_calls:
                if (
                    route.get("use_worker_tools")
                    and evidence_required_mode
                    and auto_nudge_budget > 0
                    and self._evidence_mode_needs_more_support(
                        ai_msg=ai_msg,
                        tool_events=tool_events,
                        spec_lookup_request=spec_lookup_request,
                    )
                ):
                    auto_nudge_budget -= 1
                    add_trace("检测到证据链不足，后端已要求 Worker 继续检索并补足取证。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：补足证据链",
                        detail="当前答案缺少足够的文件/代码/规范证据，已追加指令要求继续检索和精读。",
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "当前仍处于证据优先任务。"
                                "请不要直接下结论。"
                                "优先使用最合适的只读工具完成取证，例如 search_text_in_file、read_section_by_heading、table_extract、search_codebase、fact_check_file。"
                                "若已命中，请继续读取命中上下文再回答。"
                                "最终答案必须包含路径、页码、章节、表格、行号或命中片段中的至少一种证据。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = self._invoke_with_runner_recovery(
                            runner=runner,
                            messages=messages,
                            model=effective_model,
                            max_output_tokens=settings.max_output_tokens,
                            enable_tools=bool(route.get("use_worker_tools")),
                        )
                        for note in failover_notes:
                            add_trace(note)
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        add_debug(
                            stage="llm_to_backend",
                            title="Worker -> 后端编排器（补足证据后响应）",
                            detail=(
                                f"effective_model={effective_model}\n"
                                f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                            ),
                        )
                        continue
                    except Exception as exc:
                        add_trace(f"证据补强后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 证据补强失败", detail=str(exc))
                        break
                if (
                    auto_nudge_budget > 0
                    and self._looks_like_permission_gate(
                        ai_msg,
                        has_attachments=has_attachments,
                        request_requires_tools=request_requires_tools,
                    )
                ):
                    auto_nudge_budget -= 1
                    add_trace("检测到模型回复出现流程化确认/占位话术，后端已追加纠偏指令。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：避免逐步确认",
                        detail="模型出现流程化确认/占位话术，已追加系统指令要求直接产出结果。",
                    )
                    messages.append(ai_msg)
                    nudge_lines = [
                        "不要复述规则或承诺（例如“收到、会严格遵守、无需调用工具”）。",
                        "不要询问用户是否继续读取、是否继续写入、是否授权或是否确认。",
                        "不要让用户在方案A/方案B之间选择，也不要要求用户二次确认。",
                    ]
                    if request_requires_tools:
                        nudge_lines.extend(
                            [
                                "用户当前请求已授权你直接继续执行。",
                                "请立即调用必要工具完成任务（例如 read_text_file/write_text_file/append_text_file/replace_in_file），",
                                "并直接返回最终结果。",
                            ]
                        )
                    else:
                        nudge_lines.extend(
                            [
                                "当前问题可直接回答，不需要工具时请直接给结论和关键要点。",
                                "不要解释内部流程，也不要让用户再给下一步指令。",
                            ]
                        )
                    if attachments_need_tooling:
                        nudge_lines.extend(
                            [
                                "本轮存在附件输入，禁止回复“已完成解析/无需调用工具/后续再解析”这类占位话术。",
                                "必须先调用工具读取或解析附件内容，再给出结论。",
                            ]
                        )
                    if has_msg_attachment:
                        nudge_lines.append(
                            "检测到 .msg 邮件附件时，先调用 extract_msg_attachments(msg_path=...)，"
                            "再读取提取出的附件文件。"
                        )
                    messages.append(
                        self._SystemMessage(
                            content="\n".join(nudge_lines)
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = self._invoke_with_runner_recovery(
                            runner=runner,
                            messages=messages,
                            model=effective_model,
                            max_output_tokens=settings.max_output_tokens,
                            enable_tools=bool(route.get("use_worker_tools")),
                        )
                        for note in failover_notes:
                            add_trace(note)
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        add_debug(
                            stage="llm_to_backend",
                            title="Worker -> 后端编排器（自动纠偏后响应）",
                            detail=(
                                f"effective_model={effective_model}\n"
                                f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                            ),
                        )
                        continue
                    except Exception as exc:
                        add_trace(f"自动纠偏后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 自动纠偏后失败", detail=str(exc))
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
                    title=f"Worker -> 后端编排器（请求工具 {name}）",
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
                    title=f"Worker 工具执行结果 {name}",
                    detail=self._shorten(result_json, 1800 if not debug_raw else 120000),
                )
                add_debug(
                    stage="backend_to_llm",
                    title=f"后端编排器 -> Worker（工具结果 {name}）",
                    detail=self._serialize_tool_message_for_debug(
                        name=name,
                        tool_call_id=call_id,
                        content=tool_message_payload,
                        raw_mode=debug_raw,
                    ),
                )
                worker_citation_candidates = self._merge_citation_candidates(
                    worker_citation_candidates,
                    self._extract_citations_from_tool_result(name=name, arguments=arguments, result=result),
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
                    enable_tools=bool(route.get("use_worker_tools")),
                )
                for note in failover_notes:
                    add_trace(note)
                usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                add_debug(
                    stage="llm_to_backend",
                    title="Worker -> 后端编排器（后续响应）",
                    detail=(
                        f"effective_model={effective_model}\n"
                        f"{self._summarize_ai_response(ai_msg, raw_mode=debug_raw)}"
                    ),
                )
            except Exception as exc:
                add_trace(f"工具后续推理失败: {exc}")
                add_debug(stage="llm_error", title="Worker 工具后续推理失败", detail=str(exc))
                return (
                    f"工具执行后续推理失败: {exc}",
                    tool_events,
                    attachment_note,
                    execution_plan,
                    execution_trace,
                    debug_flow,
                    agent_panels,
                    answer_bundle,
                    usage_total,
                    effective_model,
                )

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if not text.strip():
            text = "模型未返回可见文本。"
        add_trace("已生成最终答复。")
        worker_bullets = [
            f"执行环境: {requested_execution_mode}",
            f"工具调用次数: {len(tool_events)}",
            f"附件数量: {len(attachment_metas)}",
            f"历史消息载入: {min(len(history_turns), settings.max_context_turns)}",
        ]
        if prefetch_payload:
            worker_bullets.append(f"自动预搜索: {prefetch_payload.get('count', 0)} 条")
        worker_summary = (
            "主执行 Agent 已完成取证、工具调用与作答。"
            if route.get("use_worker_tools")
            else "主执行 Agent 已基于当前上下文直接完成作答。"
        )
        add_panel("worker", "Worker", worker_summary, worker_bullets)
        add_debug(
            stage="multi_agent_worker",
            title="Worker 执行完成",
            detail=(
                f"effective_model={effective_model}\n"
                f"tool_events={len(tool_events)}\n"
                f"text_chars={len(text)}\n"
                f"text_preview={self._shorten(text, 1200 if not debug_raw else 50000)}"
            ),
        )
        add_debug(
            stage="llm_final",
            title="Worker 最终草稿",
            detail=(
                f"effective_model={effective_model}\n"
                f"text_chars={len(text)}\npreview={self._shorten(text, 1200 if not debug_raw else 50000)}"
            ),
        )
        conflict_brief = {
            "has_conflict": False,
            "confidence": "medium",
            "summary": "Router 已跳过冲突检查。",
            "concerns": [],
            "suggested_checks": [],
            "usage": self._empty_usage(),
            "effective_model": effective_model,
            "notes": [],
        }
        reviewer_brief = {
            "verdict": "pass",
            "confidence": "medium",
            "summary": "Router 已跳过最终审阅。",
            "strengths": [],
            "risks": [],
            "followups": [],
            "usage": self._empty_usage(),
            "effective_model": effective_model,
            "notes": [],
            "readonly_checks": [],
            "readonly_evidence": [],
        }
        if route.get("use_reviewer"):
            if route.get("use_conflict_detector"):
                conflict_request_detail = "\n".join(
                    [
                        f"requested_model={effective_model or requested_model}",
                        f"evidence_required_mode={evidence_required_mode}",
                        f"web_tools_used={str(self._summarize_validation_context(tool_events)['web_tools_used']).lower()}",
                        f"web_tools_success={str(self._summarize_validation_context(tool_events)['web_tools_success']).lower()}",
                        f"draft_chars={len(text)}",
                        f"draft_preview={self._shorten(text, 400 if not debug_raw else 5000)}",
                    ]
                )
                add_debug(
                    stage="backend_to_llm",
                    title="后端编排器 -> Conflict Detector",
                    detail=conflict_request_detail,
                )
                conflict_brief, conflict_raw = self._run_answer_conflict_detector(
                    requested_model=effective_model or requested_model,
                    user_message=user_message,
                    final_text=text,
                    planner_brief=planner_brief,
                    tool_events=tool_events,
                    spec_lookup_request=spec_lookup_request,
                    evidence_required_mode=evidence_required_mode,
                )
                conflict_effective_model = str(conflict_brief.get("effective_model") or "").strip()
                if conflict_effective_model:
                    effective_model = conflict_effective_model
                usage_total = self._merge_usage(usage_total, conflict_brief.get("usage") or self._empty_usage())
                for note in self._normalize_string_list(conflict_brief.get("notes") or [], limit=3, item_limit=200):
                    add_trace(note)
                add_debug(
                    stage="llm_to_backend",
                    title="Conflict Detector -> 后端编排器",
                    detail=(
                        f"effective_model={conflict_effective_model or effective_model or requested_model}\n"
                        f"{self._shorten(conflict_raw, 4000 if debug_raw else 1200)}"
                    ),
                )
                conflict_summary = str(conflict_brief.get("summary") or "").strip() or "已完成通识冲突检查。"
                conflict_bullets = self._normalize_string_list(conflict_brief.get("concerns") or [], limit=4, item_limit=180)
                add_panel("conflict_detector", "Conflict Detector", conflict_summary, conflict_bullets)
            else:
                add_trace("Router 已跳过 Conflict Detector。")

            reviewer_request_detail = "\n".join(
                [
                    f"requested_model={effective_model or requested_model}",
                    f"tool_events={len(tool_events)}",
                    f"execution_trace_items={len(execution_trace)}",
                    f"draft_chars={len(text)}",
                    f"draft_preview={self._shorten(text, 400 if not debug_raw else 5000)}",
                    f"evidence_required_mode={evidence_required_mode}",
                ]
            )
            add_debug(
                stage="backend_to_llm",
                title="后端编排器 -> Reviewer",
                detail=reviewer_request_detail,
            )
            reviewer_brief, reviewer_raw = self._run_reviewer(
                requested_model=effective_model or requested_model,
                user_message=user_message,
                final_text=text,
                planner_brief=planner_brief,
                tool_events=tool_events,
                execution_trace=execution_trace,
                spec_lookup_request=spec_lookup_request,
                evidence_required_mode=evidence_required_mode,
                conflict_brief=conflict_brief,
                debug_cb=add_debug,
                trace_cb=add_trace,
            )
            reviewer_effective_model = str(reviewer_brief.get("effective_model") or "").strip()
            if reviewer_effective_model:
                effective_model = reviewer_effective_model
            usage_total = self._merge_usage(usage_total, reviewer_brief.get("usage") or self._empty_usage())
            for note in self._normalize_string_list(reviewer_brief.get("notes") or [], limit=4, item_limit=200):
                add_trace(note)
            reviewer_verdict = str(reviewer_brief.get("verdict") or "pass").strip().lower()
            reviewer_confidence = str(reviewer_brief.get("confidence") or "medium").strip().lower()
            if reviewer_verdict == "block":
                add_trace(f"多 Agent: Reviewer 判定阻断，需要大幅修订，confidence={reviewer_confidence}。")
            elif reviewer_verdict == "warn":
                add_trace(f"多 Agent: Reviewer 判定可保留但需补强，confidence={reviewer_confidence}。")
            else:
                add_trace(f"多 Agent: Reviewer 通过，confidence={reviewer_confidence}。")
            add_debug(
                stage="llm_to_backend",
                title="Reviewer -> 后端编排器",
                detail=(
                    f"effective_model={reviewer_effective_model or effective_model or requested_model}\n"
                    f"{self._shorten(reviewer_raw, 4000 if debug_raw else 1200)}"
                ),
            )
            reviewer_summary = str(reviewer_brief.get("summary") or "").strip() or "已完成最终答复审阅。"
            reviewer_bullets = (
                self._normalize_string_list(
                    [f"判定: {reviewer_verdict}"],
                    limit=1,
                    item_limit=80,
                )
                + self._normalize_string_list(
                    [f"使用工具: {item}" for item in reviewer_brief.get("readonly_checks") or []],
                    limit=4,
                    item_limit=180,
                )
                + self._normalize_string_list(
                    [f"复核证据: {item}" for item in reviewer_brief.get("readonly_evidence") or []],
                    limit=4,
                    item_limit=200,
                )
                + self._normalize_string_list(reviewer_brief.get("strengths") or [], limit=2, item_limit=180)
                + self._normalize_string_list(reviewer_brief.get("risks") or [], limit=3, item_limit=180)
                + self._normalize_string_list(reviewer_brief.get("followups") or [], limit=2, item_limit=180)
            )
            add_panel("reviewer", "Reviewer", reviewer_summary, reviewer_bullets)
            if route.get("use_revision"):
                revision_request_detail = "\n".join(
                    [
                        f"requested_model={effective_model or requested_model}",
                        f"reviewer_verdict={reviewer_verdict}",
                        f"reviewer_confidence={reviewer_confidence}",
                        f"current_text_chars={len(text)}",
                        f"current_text_preview={self._shorten(text, 400 if not debug_raw else 5000)}",
                    ]
                )
                add_debug(
                    stage="backend_to_llm",
                    title="后端编排器 -> Revision",
                    detail=revision_request_detail,
                )
                revision_brief, revision_raw = self._run_revision(
                    requested_model=effective_model or requested_model,
                    user_message=user_message,
                    current_text=text,
                    planner_brief=planner_brief,
                    reviewer_brief=reviewer_brief,
                    tool_events=tool_events,
                    conflict_brief=conflict_brief,
                    evidence_required_mode=evidence_required_mode,
                )
                revision_effective_model = str(revision_brief.get("effective_model") or "").strip()
                if revision_effective_model:
                    effective_model = revision_effective_model
                usage_total = self._merge_usage(usage_total, revision_brief.get("usage") or self._empty_usage())
                for note in self._normalize_string_list(revision_brief.get("notes") or [], limit=4, item_limit=200):
                    add_trace(note)
                revised_text = str(revision_brief.get("final_answer") or "").strip()
                revision_changed = bool(revision_brief.get("changed")) and bool(revised_text)
                if revision_changed:
                    text = revised_text
                    add_trace("多 Agent: Revision 已应用到最终答复。")
                else:
                    add_trace("多 Agent: Revision 未修改最终答复。")
                add_debug(
                    stage="llm_to_backend",
                    title="Revision -> 后端编排器",
                    detail=(
                        f"effective_model={revision_effective_model or effective_model or requested_model}\n"
                        f"{self._shorten(revision_raw, 4000 if debug_raw else 1200)}"
                    ),
                )
                revision_summary = str(revision_brief.get("summary") or "").strip() or "已完成最终润色与修订判断。"
                revision_bullets = self._normalize_string_list(revision_brief.get("key_changes") or [], limit=4, item_limit=180)
                add_panel("revision", "Revision", revision_summary, revision_bullets)
            else:
                add_trace("Router 已跳过 Revision。")
        else:
            add_trace("Router 已跳过 Conflict Detector / Reviewer / Revision。")

        text = self._sanitize_final_answer_text(
            text,
            user_message=user_message,
            attachment_metas=attachment_metas,
        )
        finalized_citations = self._finalize_citation_candidates(worker_citation_candidates)
        if not route.get("use_structurer"):
            return (
                text,
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                debug_flow,
                agent_panels,
                answer_bundle,
                usage_total,
                effective_model,
            )
        if not self._should_emit_answer_bundle(finalized_citations):
            return (
                text,
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                debug_flow,
                agent_panels,
                answer_bundle,
                usage_total,
                effective_model,
            )
        structurer_request_detail = "\n".join(
            [
                f"requested_model={effective_model or requested_model}",
                f"citation_count={len(finalized_citations)}",
                f"final_text_chars={len(text)}",
                f"final_text_preview={self._shorten(text, 400 if not debug_raw else 5000)}",
            ]
        )
        add_debug(
            stage="backend_to_llm",
            title="后端编排器 -> Structurer",
            detail=structurer_request_detail,
        )
        answer_bundle, structurer_raw = self._run_answer_structurer(
            requested_model=effective_model or requested_model,
            final_text=text,
            citations=finalized_citations,
            reviewer_brief=reviewer_brief,
            conflict_brief=conflict_brief,
        )
        add_debug(
            stage="llm_to_backend",
            title="Structurer -> 后端编排器",
            detail=self._shorten(structurer_raw, 4000 if debug_raw else 1200),
        )
        add_panel(
            "structurer",
            "Structured Output",
            str(answer_bundle.get("summary") or "").strip() or "已生成结构化答案与证据链。",
            self._normalize_string_list(
                [f"claims={len(answer_bundle.get('claims') or [])}", f"citations={len(answer_bundle.get('citations') or [])}"]
                + [f"warning: {item}" for item in (answer_bundle.get("warnings") or [])],
                limit=6,
                item_limit=180,
            ),
        )
        return (
            text,
            tool_events,
            attachment_note,
            execution_plan,
            execution_trace,
            debug_flow,
            agent_panels,
            answer_bundle,
            usage_total,
            effective_model,
        )

    def _run_planner(
        self,
        *,
        requested_model: str,
        user_message: str,
        summary: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
    ) -> tuple[dict[str, Any], str]:
        fallback = {
            "objective": self._shorten(user_message.strip(), 220),
            "constraints": [],
            "plan": self._build_execution_plan(attachment_metas=attachment_metas, settings=settings),
            "watchouts": [],
            "success_signals": [],
            "usage": self._empty_usage(),
            "effective_model": requested_model,
            "notes": [],
        }
        attachment_summary = self._summarize_attachment_metas_for_agents(attachment_metas)
        planner_input = "\n".join(
            [
                f"user_message:\n{user_message.strip() or '(empty)'}",
                f"history_summary:\n{summary.strip() or '(none)'}",
                f"attachments:\n{attachment_summary}",
                f"response_style={settings.response_style}",
                f"enable_tools={settings.enable_tools}",
                f"max_context_turns={settings.max_context_turns}",
            ]
        )
        messages = [
            self._SystemMessage(
                content=(
                    "你是 Planner Agent。你的职责是为 Worker 生成可见的目标摘要和执行计划。"
                    "不要输出思维链，不要写解释。"
                    '只返回 JSON 对象，字段固定为 objective, constraints, plan, watchouts, success_signals。'
                    "每个数组最多 5 条，每条一句话。"
                )
            ),
            self._HumanMessage(content=planner_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=requested_model,
                max_output_tokens=900,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Planner 未返回标准 JSON，已降级为默认执行计划。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return fallback, raw_text

            planner = {
                "objective": str(parsed.get("objective") or fallback["objective"]).strip() or fallback["objective"],
                "constraints": self._normalize_string_list(parsed.get("constraints") or [], limit=5, item_limit=180),
                "plan": self._normalize_string_list(parsed.get("plan") or fallback["plan"], limit=6, item_limit=180),
                "watchouts": self._normalize_string_list(parsed.get("watchouts") or [], limit=5, item_limit=180),
                "success_signals": self._normalize_string_list(
                    parsed.get("success_signals") or [], limit=4, item_limit=180
                ),
                "usage": self._extract_usage_from_message(ai_msg),
                "effective_model": effective_model,
                "notes": notes,
            }
            return planner, raw_text
        except Exception as exc:
            fallback["notes"] = [f"Planner 调用失败，已回退默认计划: {self._shorten(exc, 180)}"]
            return fallback, json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _run_answer_conflict_detector(
        self,
        *,
        requested_model: str,
        user_message: str,
        final_text: str,
        planner_brief: dict[str, Any],
        tool_events: list[ToolEvent],
        spec_lookup_request: bool = False,
        evidence_required_mode: bool = False,
    ) -> tuple[dict[str, Any], str]:
        validation_context = self._summarize_validation_context(tool_events)
        detector_input = "\n".join(
            [
                f"user_message:\n{user_message.strip() or '(empty)'}",
                f"planner_objective:\n{str(planner_brief.get('objective') or '').strip() or '(none)'}",
                f"spec_lookup_request={str(spec_lookup_request).lower()}",
                f"evidence_required_mode={str(evidence_required_mode).lower()}",
                f"web_tools_used={str(validation_context['web_tools_used']).lower()}",
                f"web_tools_success={str(validation_context['web_tools_success']).lower()}",
                "web_tool_notes:",
                *[f"- {item}" for item in validation_context["web_tool_notes"]],
                "web_tool_warnings:",
                *[f"- {item}" for item in validation_context["web_tool_warnings"]],
                f"answer:\n{final_text.strip() or '(empty)'}",
            ]
        )
        fallback = {
            "has_conflict": False,
            "confidence": "medium",
            "summary": "Conflict Detector 未发现明显常识冲突。",
            "concerns": [],
            "suggested_checks": [],
            "usage": self._empty_usage(),
            "effective_model": requested_model,
            "notes": [],
        }
        messages = [
            self._SystemMessage(
                content=(
                    "你是 Answer Conflict Detector。"
                    "基于通识、成熟工程知识和任务上下文，检查当前答案是否存在明显可疑点、过度确定、或与常见知识冲突。"
                    "不要输出思维链。"
                    "你的知识只能用于报警和建议复核，不能替代文件证据。"
                    "必须区分底层模型限制与工具增强后的系统能力。"
                    "如果本轮已经成功使用 search_web、fetch_web 或 download_web_file 获得实时来源，"
                    "不能仅因为“模型原生不支持实时信息”就判定答案冲突；"
                    "这类情况最多只能提醒来源质量、时效性或复核范围。"
                    '只返回 JSON 对象，字段固定为 has_conflict, confidence, summary, concerns, suggested_checks。'
                    "has_conflict 必须是 true 或 false；confidence 只能是 high, medium, low。"
                )
            ),
            self._HumanMessage(content=detector_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=requested_model,
                max_output_tokens=900,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Conflict Detector 未返回标准 JSON，已忽略冲突检查结果。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return fallback, raw_text

            has_conflict_raw = parsed.get("has_conflict")
            if isinstance(has_conflict_raw, bool):
                has_conflict = has_conflict_raw
            else:
                has_conflict = str(has_conflict_raw or "").strip().lower() in {"1", "true", "yes", "on"}
            confidence = str(parsed.get("confidence") or "medium").strip().lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            detector = {
                "has_conflict": has_conflict,
                "confidence": confidence,
                "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
                "concerns": self._normalize_string_list(parsed.get("concerns") or [], limit=4, item_limit=180),
                "suggested_checks": self._normalize_string_list(
                    parsed.get("suggested_checks") or [], limit=4, item_limit=180
                ),
                "usage": self._extract_usage_from_message(ai_msg),
                "effective_model": effective_model,
                "notes": notes,
            }
            return detector, raw_text
        except Exception as exc:
            fallback["notes"] = [f"Conflict Detector 调用失败，已跳过: {self._shorten(exc, 180)}"]
            return fallback, json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _run_reviewer(
        self,
        *,
        requested_model: str,
        user_message: str,
        final_text: str,
        planner_brief: dict[str, Any],
        tool_events: list[ToolEvent],
        execution_trace: list[str],
        spec_lookup_request: bool = False,
        evidence_required_mode: bool = False,
        conflict_brief: dict[str, Any] | None = None,
        debug_cb: Callable[[str, str, str], None] | None = None,
        trace_cb: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], str]:
        tool_summaries = [
            f"{idx + 1}. {tool.name}({json.dumps(tool.input or {}, ensure_ascii=False)})"
            for idx, tool in enumerate(tool_events[:10])
        ]
        validation_context = self._summarize_validation_context(tool_events)
        conflict_lines = [
            f"conflict_has_conflict={str(bool((conflict_brief or {}).get('has_conflict'))).lower()}",
            f"conflict_summary={str((conflict_brief or {}).get('summary') or '').strip() or '(none)'}",
            "conflict_concerns:",
            *[f"- {item}" for item in self._normalize_string_list((conflict_brief or {}).get("concerns") or [], limit=4)],
        ]
        reviewer_input = "\n".join(
            [
                f"user_message:\n{user_message.strip() or '(empty)'}",
                f"planner_objective:\n{str(planner_brief.get('objective') or '').strip() or '(none)'}",
                "planner_plan:",
                *[f"- {item}" for item in self._normalize_string_list(planner_brief.get("plan") or [], limit=6)],
                f"task_mode={'spec_lookup' if spec_lookup_request else 'general'}",
                f"evidence_required_mode={str(evidence_required_mode).lower()}",
                f"web_tools_used={str(validation_context['web_tools_used']).lower()}",
                f"web_tools_success={str(validation_context['web_tools_success']).lower()}",
                "web_tool_notes:",
                *[f"- {item}" for item in validation_context["web_tool_notes"]],
                "web_tool_warnings:",
                *[f"- {item}" for item in validation_context["web_tool_warnings"]],
                *conflict_lines,
                "tool_events:",
                *(tool_summaries or ["(none)"]),
                "execution_trace_tail:",
                *[f"- {line}" for line in execution_trace[-8:]],
                f"final_text:\n{final_text.strip() or '(empty)'}",
            ]
        )
        fallback = {
            "verdict": "pass",
            "confidence": "medium",
            "summary": "Reviewer 未发现阻断性问题。",
            "strengths": ["已完成基础自检。"],
            "risks": [],
            "followups": [],
            "usage": self._empty_usage(),
            "effective_model": requested_model,
            "notes": [],
        }
        readonly_tools = self._reviewer_readonly_tool_names()
        messages = [
            self._SystemMessage(
                content=(
                    "你是 Reviewer Agent。检查最终答复是否覆盖用户目标、是否基于已有工具证据、是否存在明显遗漏。"
                    "你的 verdict 必须使用三级结论：pass、warn、block。"
                    "pass = 结论和证据都足够，可以直接放行；"
                    "warn = 核心方向基本正确，但证据表达、引用粒度或措辞需要补强，不应全盘否决；"
                    "block = 证据链明显缺失、独立复核冲突明显、或当前结论高风险到不能直接交付。"
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
                    "不要输出思维链。"
                    '只返回 JSON 对象，字段固定为 verdict, confidence, summary, strengths, risks, followups。'
                    "verdict 只能是 pass, warn, block；confidence 只能是 high, medium, low。"
                )
            ),
            self._HumanMessage(content=reviewer_input),
        ]
        try:
            usage_total = self._empty_usage()
            notes: list[str] = []
            reviewer_tool_names: list[str] = []
            reviewer_evidence: list[str] = []
            ai_msg, runner, effective_model, invoke_notes = self._invoke_chat_with_runner(
                messages=messages,
                model=requested_model,
                max_output_tokens=1200,
                enable_tools=True,
                tool_names=readonly_tools,
            )
            notes.extend(invoke_notes)
            usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
            nudge_budget = 1 if evidence_required_mode else 0
            for _ in range(12):
                tool_calls = getattr(ai_msg, "tool_calls", None) or []
                if not tool_calls:
                    if nudge_budget > 0 and not reviewer_tool_names:
                        nudge_budget -= 1
                        messages.append(ai_msg)
                        messages.append(
                            self._SystemMessage(
                                content=(
                                    "请先完成独立复核，再输出 JSON。"
                                    "优先调用 fact_check_file；若需要精读文档章节，调用 read_section_by_heading 或 search_text_in_file。"
                                    "如果是代码任务，优先调用 search_codebase。"
                                    "如果涉及实时信息、新闻、网页来源或联网事实，优先调用 search_web 和 fetch_web。"
                                )
                            )
                        )
                        ai_msg, runner, effective_model, invoke_notes = self._invoke_with_runner_recovery(
                            runner=runner,
                            messages=messages,
                            model=effective_model,
                            max_output_tokens=1200,
                            enable_tools=True,
                            tool_names=readonly_tools,
                        )
                        notes.extend(invoke_notes)
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
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
                            f"Reviewer -> 后端编排器（请求工具 {name}）",
                            "\n".join(
                                [
                                    f"tool={name}",
                                    f"args={self._shorten(json.dumps(args, ensure_ascii=False), 1200)}",
                                ]
                            ),
                        )
                    result = self.tools.execute(name, args)
                    result_json = json.dumps(result, ensure_ascii=False)
                    tool_payload, trim_note = self._prepare_tool_result_for_llm(
                        name=name,
                        arguments=args,
                        raw_result=result,
                        raw_json=result_json,
                    )
                    reviewer_tool_names.append(name)
                    evidence_summary = self._summarize_reviewer_tool_result(name=name, result=result)
                    if evidence_summary:
                        reviewer_evidence.append(evidence_summary)
                        if trace_cb is not None:
                            trace_cb(f"Reviewer 复核: {evidence_summary}")
                    if trim_note:
                        notes.append(trim_note)
                    if debug_cb is not None:
                        debug_cb(
                            "backend_tool",
                            f"后端编排器执行 Reviewer 只读工具 {name}",
                            "\n".join(
                                [
                                    f"tool={name}",
                                    f"summary={evidence_summary or '(none)'}",
                                    f"result={self._shorten(result_json, 1800)}",
                                ]
                            ),
                        )
                    messages.append(
                        self._ToolMessage(
                            content=tool_payload,
                            tool_call_id=call.get("id") or f"reviewer_{len(reviewer_tool_names)}",
                            name=name,
                        )
                    )
                    if debug_cb is not None:
                        debug_cb(
                            "backend_to_llm",
                            f"后端编排器 -> Reviewer（工具结果 {name}）",
                            "\n".join(
                                [
                                    f"tool={name}",
                                    f"summary={evidence_summary or '(none)'}",
                                    f"tool_payload={self._shorten(tool_payload, 1800)}",
                                ]
                            ),
                        )
                ai_msg, runner, effective_model, invoke_notes = self._invoke_with_runner_recovery(
                    runner=runner,
                    messages=messages,
                    model=effective_model,
                    max_output_tokens=1200,
                    enable_tools=True,
                    tool_names=readonly_tools,
                )
                notes.extend(invoke_notes)
                usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))

            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Reviewer 未返回标准 JSON，已按保守策略记录。", *notes]
                fallback["usage"] = usage_total
                fallback["effective_model"] = effective_model
                return fallback, raw_text

            verdict = self._normalize_reviewer_verdict(
                parsed.get("verdict"),
                risks=parsed.get("risks") or [],
                followups=parsed.get("followups") or [],
                spec_lookup_request=spec_lookup_request,
                evidence_required_mode=evidence_required_mode,
                readonly_checks=reviewer_tool_names,
                conflict_has_conflict=bool((conflict_brief or {}).get("has_conflict")),
                conflict_realtime_only=self._conflict_is_realtime_capability_warning(conflict_brief),
                web_tools_success=bool(validation_context["web_tools_success"]),
            )
            confidence = str(parsed.get("confidence") or "medium").strip().lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            reviewer = {
                "verdict": verdict,
                "confidence": confidence,
                "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
                "strengths": self._normalize_string_list(parsed.get("strengths") or [], limit=3, item_limit=180),
                "risks": self._normalize_string_list(parsed.get("risks") or [], limit=4, item_limit=180),
                "followups": self._normalize_string_list(parsed.get("followups") or [], limit=3, item_limit=180),
                "usage": usage_total,
                "effective_model": effective_model,
                "notes": notes,
                "readonly_checks": reviewer_tool_names,
                "readonly_evidence": reviewer_evidence,
            }
            return reviewer, raw_text
        except Exception as exc:
            fallback["notes"] = [f"Reviewer 调用失败，已跳过最终审阅: {self._shorten(exc, 180)}"]
            return fallback, json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _run_revision(
        self,
        *,
        requested_model: str,
        user_message: str,
        current_text: str,
        planner_brief: dict[str, Any],
        reviewer_brief: dict[str, Any],
        tool_events: list[ToolEvent],
        conflict_brief: dict[str, Any] | None = None,
        evidence_required_mode: bool = False,
    ) -> tuple[dict[str, Any], str]:
        tool_summaries = [
            f"{idx + 1}. {tool.name}({json.dumps(tool.input or {}, ensure_ascii=False)})"
            for idx, tool in enumerate(tool_events[:8])
        ]
        revision_input = "\n".join(
            [
                f"user_message:\n{user_message.strip() or '(empty)'}",
                f"planner_objective:\n{str(planner_brief.get('objective') or '').strip() or '(none)'}",
                f"reviewer_verdict={str(reviewer_brief.get('verdict') or 'pass').strip()}",
                f"reviewer_confidence={str(reviewer_brief.get('confidence') or 'medium').strip()}",
                f"evidence_required_mode={str(evidence_required_mode).lower()}",
                "reviewer_risks:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_brief.get("risks") or [], limit=5)],
                "reviewer_followups:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_brief.get("followups") or [], limit=4)],
                "reviewer_readonly_checks:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_brief.get("readonly_checks") or [], limit=8)],
                "reviewer_readonly_evidence:",
                *[
                    f"- {item}"
                    for item in self._normalize_string_list(reviewer_brief.get("readonly_evidence") or [], limit=8)
                ],
                f"conflict_summary={str((conflict_brief or {}).get('summary') or '').strip() or '(none)'}",
                "conflict_concerns:",
                *[f"- {item}" for item in self._normalize_string_list((conflict_brief or {}).get("concerns") or [], limit=4)],
                "tool_events:",
                *(tool_summaries or ["(none)"]),
                f"current_answer:\n{current_text.strip() or '(empty)'}",
            ]
        )
        fallback = {
            "changed": False,
            "summary": "Revision 未修改最终答复。",
            "key_changes": [],
            "final_answer": current_text,
            "usage": self._empty_usage(),
            "effective_model": requested_model,
            "notes": [],
        }
        messages = [
            self._SystemMessage(
                content=(
                    "你是 Revision Agent。你负责根据 Reviewer 结论对最终答复做最后一次修订。"
                    "如果 reviewer_verdict=pass，通常保持原文或只做极小润色。"
                    "如果 reviewer_verdict=warn，优先保留 Worker 已经找到的核心信息，补上页码/章节/命中片段/限定语，"
                    "不要因为证据表达不完整就整段推翻。"
                    "如果 reviewer_verdict=block，才应把最终答复改成更保守或更明确要求继续取证的版本。"
                    "如果当前答复已经足够好，可以保持原文不变。"
                    "禁止引入新的未经工具或上下文支持的事实。"
                    "最终输出绝不能暴露内部控制变量或流程字段，"
                    "例如 reviewer_verdict、reviewer_confidence、evidence_required_mode、task_mode。"
                    "如果 Reviewer 指出与通识或领域知识存在明显冲突，而工具证据又不足，"
                    "你应把最终答复改成更保守的表述，例如说明当前证据不足、需要继续核对原文，"
                    "而不是继续维持一个可疑的确定性结论。"
                    "如果 evidence_required_mode=true，而当前答案缺少路径、页码、章节、行号、表格或命中片段证据，"
                    "你应优先把最终答复改成证据优先的保守版本。"
                    '只返回 JSON 对象，字段固定为 changed, summary, key_changes, final_answer。'
                    "changed 必须是 true 或 false；key_changes 最多 4 条。"
                )
            ),
            self._HumanMessage(content=revision_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=requested_model,
                max_output_tokens=1800,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Revision 未返回标准 JSON，已保留原答复。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return fallback, raw_text

            changed_raw = parsed.get("changed")
            if isinstance(changed_raw, bool):
                changed = changed_raw
            else:
                changed = str(changed_raw or "").strip().lower() in {"1", "true", "yes", "on"}
            final_answer = str(parsed.get("final_answer") or current_text).strip() or current_text
            revision = {
                "changed": changed and final_answer.strip() != current_text.strip(),
                "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
                "key_changes": self._normalize_string_list(parsed.get("key_changes") or [], limit=4, item_limit=180),
                "final_answer": final_answer,
                "usage": self._extract_usage_from_message(ai_msg),
                "effective_model": effective_model,
                "notes": notes,
            }
            return revision, raw_text
        except Exception as exc:
            fallback["notes"] = [f"Revision 调用失败，已保留原答复: {self._shorten(exc, 180)}"]
            return fallback, json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _sanitize_final_answer_text(
        self,
        text: str,
        *,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
    ) -> str:
        original = str(text or "").strip()
        if not original:
            return original

        cleaned = original
        had_internal_meta = bool(
            re.search(r"(?i)\b(?:reviewer_verdict|reviewer_confidence|evidence_required_mode|task_mode)\b", original)
        )
        cleaned = re.sub(r"(?im)^.*\b(?:reviewer_verdict|reviewer_confidence|evidence_required_mode|task_mode)\b.*$", "", cleaned)
        cleaned = re.sub(
            r"(?is)(?:^|[。；\n])[^。；\n]*(?:reviewer_verdict|reviewer_confidence|evidence_required_mode|task_mode)[^。；\n]*[。；]?",
            "",
            cleaned,
        )

        if not attachment_metas and self._looks_like_inline_document_payload(user_message):
            cleaned = re.sub(r"(?is)[^。；\n]*(?:本地文件路径|文件路径|磁盘文件)[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*无法进行可复核的解析[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*请(?:提供|给出|上传).{0,40}路径[^。；\n]*[。；]?", "", cleaned)

        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if not cleaned:
            if not attachment_metas and self._looks_like_inline_document_payload(user_message) and had_internal_meta:
                return "已按你直接粘贴的原始文本内容理解，不需要额外提供本地文件路径。请继续指定你要我解释的结构、字段或结论。"
            return original
        return cleaned

    def _run_answer_structurer(
        self,
        *,
        requested_model: str,
        final_text: str,
        citations: list[dict[str, Any]],
        reviewer_brief: dict[str, Any],
        conflict_brief: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        fallback = self._fallback_answer_bundle(
            final_text=final_text,
            citations=citations,
            reviewer_brief=reviewer_brief,
            conflict_brief=conflict_brief,
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
                    f"excerpt={self._shorten(item.get('excerpt') or '', 260)}",
                    f"warning={item.get('warning') or '(none)'}",
                    "",
                ]
            )
        structurer_input = "\n".join(
            [
                f"final_answer:\n{final_text.strip() or '(empty)'}",
                f"reviewer_verdict={str(reviewer_brief.get('verdict') or 'pass').strip()}",
                "reviewer_risks:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_brief.get("risks") or [], limit=4)],
                "reviewer_followups:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_brief.get("followups") or [], limit=4)],
                f"conflict_summary={str((conflict_brief or {}).get('summary') or '').strip() or '(none)'}",
                "citations:",
                *(citation_lines or ["(none)"]),
            ]
        )
        messages = [
            self._SystemMessage(
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
            self._HumanMessage(content=structurer_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=requested_model,
                max_output_tokens=1400,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Structurer 未返回标准 JSON，已使用后端降级结构化结果。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return self._strip_answer_bundle_meta(fallback), raw_text

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
                    self._normalize_claim_record(
                        statement=statement,
                        citation_ids=citation_ids,
                        confidence=confidence,
                        status=status,
                        citations_by_id=citations_by_id,
                    )
                )
                if len(claims_out) >= 5:
                    break

            warnings = self._normalize_string_list(parsed.get("warnings") or [], limit=5, item_limit=220)
            warnings = self._augment_bundle_warnings(warnings=warnings, citations=citations)
            bundle = {
                "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
                "claims": claims_out or fallback["claims"],
                "citations": citations,
                "warnings": warnings or fallback["warnings"],
                "usage": self._extract_usage_from_message(ai_msg),
                "effective_model": effective_model,
                "notes": notes,
            }
            return self._strip_answer_bundle_meta(bundle), raw_text
        except Exception as exc:
            fallback["notes"] = [f"Structurer 调用失败，已回退后端结构化结果: {self._shorten(exc, 180)}"]
            return self._strip_answer_bundle_meta(fallback), json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _fallback_answer_bundle(
        self,
        *,
        final_text: str,
        citations: list[dict[str, Any]],
        reviewer_brief: dict[str, Any],
        conflict_brief: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary = self._extract_answer_summary(final_text)
        citations_by_id = {
            str(item.get("id") or "").strip(): item for item in citations if str(item.get("id") or "").strip()
        }
        evidence_ids = [
            cid
            for cid, item in citations_by_id.items()
            if self._citation_kind(item) == "evidence"
        ]
        candidate_ids = [
            cid
            for cid, item in citations_by_id.items()
            if self._citation_kind(item) == "candidate"
        ]
        claims: list[dict[str, Any]] = []
        for statement in self._split_claim_candidates(final_text)[:5]:
            linked_ids = evidence_ids[: min(2, len(evidence_ids))] or candidate_ids[: min(2, len(candidate_ids))]
            claims.append(
                self._normalize_claim_record(
                    statement=statement,
                    citation_ids=linked_ids,
                    confidence="medium" if evidence_ids else "low",
                    status="supported" if evidence_ids else "needs_review",
                    citations_by_id=citations_by_id,
                )
            )
        warnings = self._normalize_string_list(
            list(reviewer_brief.get("risks") or [])
            + list(reviewer_brief.get("followups") or [])
            + list((conflict_brief or {}).get("concerns") or []),
            limit=5,
            item_limit=220,
        )
        warnings = self._augment_bundle_warnings(warnings=warnings, citations=citations)
        return {
            "summary": summary,
            "claims": claims,
            "citations": citations,
            "warnings": warnings,
            "usage": self._empty_usage(),
            "effective_model": "",
            "notes": [],
        }

    def _strip_answer_bundle_meta(self, bundle: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": str(bundle.get("summary") or "").strip(),
            "claims": list(bundle.get("claims") or []),
            "citations": list(bundle.get("citations") or []),
            "warnings": list(bundle.get("warnings") or []),
        }

    def _should_emit_answer_bundle(self, citations: list[dict[str, Any]]) -> bool:
        return bool(citations)

    def _citation_kind(self, citation: dict[str, Any]) -> str:
        kind = str(citation.get("kind") or "").strip().lower()
        if kind in {"evidence", "candidate"}:
            return kind
        return "candidate" if str(citation.get("tool") or "").strip() == "search_web" else "evidence"

    def _citation_strength(self, citation: dict[str, Any]) -> int:
        if self._citation_kind(citation) != "evidence":
            return 0
        confidence = str(citation.get("confidence") or "medium").strip().lower()
        return {"high": 3, "medium": 2, "low": 1}.get(confidence, 2)

    def _normalize_claim_record(
        self,
        *,
        statement: str,
        citation_ids: list[str],
        confidence: str,
        status: str,
        citations_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        linked_ids = [cid for cid in citation_ids if cid in citations_by_id][:4]
        linked_citations = [citations_by_id[cid] for cid in linked_ids]
        evidence_strength = max((self._citation_strength(item) for item in linked_citations), default=0)
        has_evidence = any(self._citation_kind(item) == "evidence" for item in linked_citations)

        normalized_confidence = confidence if confidence in {"high", "medium", "low"} else "medium"
        normalized_status = status if status in {"supported", "partially_supported", "needs_review"} else "needs_review"

        if not linked_ids:
            normalized_confidence = "low"
            normalized_status = "needs_review"
        elif not has_evidence:
            normalized_confidence = "low"
            normalized_status = "needs_review"
        elif evidence_strength <= 1:
            if normalized_confidence == "high":
                normalized_confidence = "medium"
            if normalized_status == "supported":
                normalized_status = "partially_supported"

        return {
            "statement": self._shorten(statement, 220),
            "citation_ids": linked_ids,
            "confidence": normalized_confidence,
            "status": normalized_status,
        }

    def _augment_bundle_warnings(self, *, warnings: list[str], citations: list[dict[str, Any]]) -> list[str]:
        normalized = self._normalize_string_list(warnings, limit=5, item_limit=220)
        if not citations:
            return normalized
        if all(self._citation_kind(item) == "candidate" for item in citations if isinstance(item, dict)):
            normalized = self._normalize_string_list(
                ["当前来源仅为搜索候选链接，尚未抓取正文，结论需复核。", *normalized],
                limit=5,
                item_limit=220,
            )
        return normalized

    def _extract_answer_summary(self, final_text: str) -> str:
        cleaned = " ".join(str(final_text or "").strip().split())
        if not cleaned:
            return ""
        sentence = re.split(r"(?<=[。.!?！？])\s+", cleaned, maxsplit=1)[0]
        return self._shorten(sentence or cleaned, 220)

    def _build_followup_topic_hint(self, *, user_message: str, history_turns: list[dict[str, Any]]) -> str:
        current = str(user_message or "").strip()
        if not self._looks_like_short_followup_search(current):
            return ""
        for turn in reversed(history_turns):
            if str(turn.get("role") or "") != "user":
                continue
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            if text == current:
                continue
            return self._shorten(" ".join(text.split()), 280)
        return ""

    def _looks_like_short_followup_search(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        if "http://" in lowered or "https://" in lowered:
            return False
        compact = lowered.replace(" ", "")
        if len(compact) > 24:
            return False
        if not any(hint in compact for hint in _FOLLOWUP_SEARCH_HINTS):
            return False
        concrete_markers = (
            ".com",
            ".jp",
            ".cn",
            ".org",
            "http",
            "谁",
            "什么",
            "哪家",
            "哪个",
            "大谷",
            "openai",
            "nvme",
        )
        if any(marker in compact for marker in concrete_markers):
            return False
        return True

    def _split_claim_candidates(self, final_text: str) -> list[str]:
        raw = str(final_text or "").strip()
        if not raw:
            return []
        normalized = raw.replace("\r\n", "\n")
        candidates: list[str] = []
        for line in normalized.splitlines():
            line = line.strip().lstrip("-*•").strip()
            if not line:
                continue
            parts = [item.strip() for item in re.split(r"(?<=[。.!?！？])\s+", line) if item.strip()]
            candidates.extend(parts or [line])
            if len(candidates) >= 8:
                break
        out: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            compact = " ".join(item.split())
            if len(compact) < 8:
                continue
            key = compact.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(self._shorten(compact, 220))
            if len(out) >= 5:
                break
        return out

    def _format_planner_system_hint(self, planner_brief: dict[str, Any]) -> str:
        objective = str(planner_brief.get("objective") or "").strip()
        constraints = self._normalize_string_list(planner_brief.get("constraints") or [], limit=5, item_limit=180)
        plan = self._normalize_string_list(planner_brief.get("plan") or [], limit=6, item_limit=180)
        watchouts = self._normalize_string_list(planner_brief.get("watchouts") or [], limit=4, item_limit=180)
        success_signals = self._normalize_string_list(
            planner_brief.get("success_signals") or [], limit=4, item_limit=180
        )
        lines = ["多 Agent 协调摘要（来自 Planner）："]
        if objective:
            lines.append(f"目标: {objective}")
        if constraints:
            lines.append("关键约束:")
            lines.extend(f"- {item}" for item in constraints)
        if plan:
            lines.append("执行计划:")
            lines.extend(f"- {item}" for item in plan)
        if watchouts:
            lines.append("注意风险:")
            lines.extend(f"- {item}" for item in watchouts)
        if success_signals:
            lines.append("完成信号:")
            lines.extend(f"- {item}" for item in success_signals)
        return "\n".join(lines) if len(lines) > 1 else ""

    def _format_router_panel_bullets(self, route: dict[str, Any]) -> list[str]:
        worker_uses_tools = bool(route.get("use_worker_tools"))
        return [
            f"task_type: {route.get('task_type')}",
            f"complexity: {route.get('complexity')}",
            f"source: {route.get('source')}",
            f"specialists: {', '.join(route.get('specialists') or []) or '(none)'}",
            f"planner_enabled: {str(bool(route.get('use_planner'))).lower()}",
            "worker_enabled: true",
            f"worker_mode: {'uses_tools' if worker_uses_tools else 'direct_answer'}",
            f"reviewer_enabled: {str(bool(route.get('use_reviewer'))).lower()}",
            f"revision_enabled: {str(bool(route.get('use_revision'))).lower()}",
            f"structurer_enabled: {str(bool(route.get('use_structurer'))).lower()}",
        ]

    def _normalize_specialists(self, value: Any, *, limit: int = 3) -> list[str]:
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            specialist = str(item or "").strip().lower()
            if specialist not in _SPECIALIST_LABELS:
                continue
            if specialist in seen:
                continue
            seen.add(specialist)
            out.append(specialist)
            if len(out) >= max(1, limit):
                break
        return out

    def _specialist_plan_line(self, specialist: str) -> str:
        if specialist == "researcher":
            return "Researcher 生成联网取证简报。"
        if specialist == "file_reader":
            return "FileReader 生成文件阅读与定位简报。"
        if specialist == "summarizer":
            return "Summarizer 生成内容提炼简报。"
        if specialist == "fixer":
            return "Fixer 生成问题修复简报。"
        return f"{_SPECIALIST_LABELS.get(specialist, specialist)} 生成专门简报。"

    def _specialist_fallback(
        self,
        *,
        specialist: str,
        requested_model: str,
        attachment_metas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        attachment_summary = self._summarize_attachment_metas_for_agents(attachment_metas)
        if specialist == "researcher":
            return {
                "role": specialist,
                "summary": "优先聚焦公开来源、近期时间线与权威报道。",
                "bullets": [
                    "优先用 search_web 找候选，再用 fetch_web 读正文。",
                    "优先查看权威媒体、官方赛事和可核实新闻来源。",
                ],
                "worker_hint": "先围绕时间、地点、事件三件事取证，再给结论，避免只基于搜索摘要。",
                "queries": [],
                "usage": self._empty_usage(),
                "effective_model": requested_model,
                "notes": [],
            }
        if specialist == "file_reader":
            return {
                "role": specialist,
                "summary": "先缩小目标范围，再进入命中上下文或相关附件。",
                "bullets": [
                    f"附件概览: {self._shorten(attachment_summary, 120)}",
                    "优先定位关键词、章节、表格或命中片段，再读取上下文。",
                ],
                "worker_hint": "文件任务先做定位，再精读命中附近内容，不要泛读整份文档。",
                "queries": [],
                "usage": self._empty_usage(),
                "effective_model": requested_model,
                "notes": [],
            }
        if specialist == "summarizer":
            return {
                "role": specialist,
                "summary": "直接围绕用户问题提炼当前内联内容的核心信息。",
                "bullets": [
                    "先给结论，再补 2-4 条关键点。",
                    "避免流程化话术，不要改写成取证报告。",
                ],
                "worker_hint": "直接总结当前消息和附件内容，不要解释内部流程，也不要假装缺少工具。",
                "queries": [],
                "usage": self._empty_usage(),
                "effective_model": requested_model,
                "notes": [],
            }
        return {
            "role": specialist,
            "summary": f"{_SPECIALIST_LABELS.get(specialist, specialist)} 已回退到默认简报。",
            "bullets": [],
            "worker_hint": "",
            "queries": [],
            "usage": self._empty_usage(),
            "effective_model": requested_model,
            "notes": [],
        }

    def _run_specialist_role(
        self,
        *,
        specialist: str,
        requested_model: str,
        user_message: str,
        summary: str,
        user_content: Any,
        attachment_metas: list[dict[str, Any]],
        route: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        specialist = str(specialist or "").strip().lower()
        fallback = self._specialist_fallback(
            specialist=specialist,
            requested_model=requested_model,
            attachment_metas=attachment_metas,
        )
        if specialist not in _SPECIALIST_LABELS:
            fallback["notes"] = [f"未知专门角色: {specialist}"]
            return fallback, json.dumps({"error": "unknown specialist"}, ensure_ascii=False)

        payload_preview = self._shorten(self._content_to_text(user_content), 16000)
        route_summary = json.dumps(
            {
                "task_type": route.get("task_type"),
                "complexity": route.get("complexity"),
                "use_worker_tools": bool(route.get("use_worker_tools")),
                "use_reviewer": bool(route.get("use_reviewer")),
            },
            ensure_ascii=False,
        )
        specialist_input = "\n".join(
            [
                f"user_message:\n{user_message.strip() or '(empty)'}",
                f"history_summary:\n{summary.strip() or '(none)'}",
                f"route:\n{route_summary}",
                f"attachments:\n{self._summarize_attachment_metas_for_agents(attachment_metas)}",
                f"context_preview:\n{payload_preview or '(empty)'}",
            ]
        )

        if specialist == "researcher":
            system_prompt = (
                "你是 Researcher 专门角色。"
                "你的职责是为后续 Worker 生成联网取证简报，而不是直接回答用户。"
                "聚焦：搜索角度、来源优先级、需要核对的时间/地点/人物关系。"
                '只返回 JSON，对象字段固定为 summary, bullets, worker_hint, queries。'
                "bullets 最多 4 条，queries 最多 3 条。"
            )
        elif specialist == "file_reader":
            system_prompt = (
                "你是 FileReader 专门角色。"
                "你的职责是为文档/附件任务生成阅读与定位简报，而不是直接回答用户。"
                "聚焦：应优先看的文件、章节、关键词、命中策略。"
                '只返回 JSON，对象字段固定为 summary, bullets, worker_hint, queries。'
                "bullets 最多 4 条，queries 最多 4 条。"
            )
        elif specialist == "summarizer":
            system_prompt = (
                "你是 Summarizer 专门角色。"
                "你的职责是为简单理解任务生成内容提炼简报，而不是输出最终答复。"
                "聚焦：用户真正要的结论、重点信息、回答组织方式。"
                '只返回 JSON，对象字段固定为 summary, bullets, worker_hint, queries。'
                "bullets 最多 4 条；如果不需要 queries，就返回空数组。"
            )
        else:
            system_prompt = (
                "你是专门角色。"
                '只返回 JSON，对象字段固定为 summary, bullets, worker_hint, queries。'
            )

        messages = [
            self._SystemMessage(content=system_prompt),
            self._HumanMessage(content=specialist_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=self.config.summary_model or requested_model,
                max_output_tokens=900,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            usage = self._extract_usage_from_message(ai_msg)
            if not parsed:
                fallback["notes"] = [f"{_SPECIALIST_LABELS[specialist]} 未返回标准 JSON，已回退默认简报。", *notes]
                fallback["usage"] = usage
                fallback["effective_model"] = effective_model
                return fallback, raw_text
            brief = {
                "role": specialist,
                "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
                "bullets": self._normalize_string_list(parsed.get("bullets") or fallback["bullets"], limit=4, item_limit=180),
                "worker_hint": str(parsed.get("worker_hint") or fallback["worker_hint"]).strip() or fallback["worker_hint"],
                "queries": self._normalize_string_list(parsed.get("queries") or [], limit=4, item_limit=80),
                "usage": usage,
                "effective_model": effective_model,
                "notes": notes,
            }
            return brief, raw_text
        except Exception as exc:
            fallback["notes"] = [f"{_SPECIALIST_LABELS[specialist]} 调用失败，已回退默认简报: {self._shorten(exc, 180)}"]
            return fallback, json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _format_specialist_system_hint(self, specialist: str, brief: dict[str, Any]) -> str:
        label = _SPECIALIST_LABELS.get(specialist, specialist)
        lines = [f"专门角色摘要（来自 {label}）："]
        summary = str(brief.get("summary") or "").strip()
        bullets = self._normalize_string_list(brief.get("bullets") or [], limit=4, item_limit=180)
        worker_hint = str(brief.get("worker_hint") or "").strip()
        queries = self._normalize_string_list(brief.get("queries") or [], limit=4, item_limit=80)
        if summary:
            lines.append(f"摘要: {summary}")
        if bullets:
            lines.append("要点:")
            lines.extend(f"- {item}" for item in bullets)
        if queries:
            lines.append("建议关键词/查询:")
            lines.extend(f"- {item}" for item in queries)
        if worker_hint:
            lines.append(f"执行提示: {worker_hint}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _summarize_attachment_metas_for_agents(self, attachment_metas: list[dict[str, Any]]) -> str:
        if not attachment_metas:
            return "(none)"
        lines: list[str] = []
        for idx, meta in enumerate(attachment_metas[:8], start=1):
            name = str(meta.get("original_name") or meta.get("name") or f"file_{idx}")
            kind = str(meta.get("kind") or "other")
            size = self._format_bytes(meta.get("size"))
            suffix = str(meta.get("suffix") or "")
            lines.append(f"{idx}. {name} kind={kind} size={size} suffix={suffix or '-'}")
        if len(attachment_metas) > 8:
            lines.append(f"... and {len(attachment_metas) - 8} more")
        return "\n".join(lines)

    def _parse_tool_event_preview(self, event: ToolEvent) -> dict[str, Any] | None:
        raw = str(event.output_preview or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _tool_event_ok(self, event: ToolEvent) -> bool | None:
        parsed = self._parse_tool_event_preview(event)
        if isinstance(parsed, dict) and "ok" in parsed:
            return bool(parsed.get("ok"))
        preview = str(event.output_preview or "").lower()
        if '"ok": true' in preview:
            return True
        if '"ok": false' in preview:
            return False
        return None

    def _summarize_validation_context(self, tool_events: list[ToolEvent]) -> dict[str, Any]:
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
            ok_flag = self._tool_event_ok(event)
            parsed = self._parse_tool_event_preview(event) or {}
            if ok_flag is True:
                success = True

            detail = base_name
            if base_name.startswith("search_web"):
                query = str((event.input or {}).get("query") or parsed.get("query") or "").strip()
                count = int(parsed.get("count") or 0)
                engine = str(parsed.get("engine") or "").strip()
                parts = [detail]
                if query:
                    parts.append(f"query={self._shorten(query, 60)}")
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
                    parts.append(f"url={self._shorten(url, 80)}")
                if source_format:
                    parts.append(f"format={source_format}")
                detail = ", ".join(parts)
            elif base_name == "download_web_file":
                url = str((event.input or {}).get("url") or parsed.get("url") or "").strip()
                path = str(parsed.get("path") or "").strip()
                parts = [detail]
                if url:
                    parts.append(f"url={self._shorten(url, 80)}")
                if path:
                    parts.append(f"path={self._shorten(path, 80)}")
                detail = ", ".join(parts)

            if ok_flag is True:
                detail += " [ok]"
            elif ok_flag is False:
                detail += " [failed]"
            notes.append(detail)

            warning = str(parsed.get("warning") or "").strip()
            if warning:
                warnings.append(f"{base_name}: {self._shorten(warning, 160)}")

        return {
            "web_tools_used": used,
            "web_tools_success": success,
            "web_tool_notes": self._normalize_string_list(notes, limit=6, item_limit=180),
            "web_tool_warnings": self._normalize_string_list(warnings, limit=4, item_limit=180),
        }

    def _conflict_is_realtime_capability_warning(self, conflict_brief: dict[str, Any] | None) -> bool:
        lines = [
            str((conflict_brief or {}).get("summary") or "").strip(),
            *self._normalize_string_list((conflict_brief or {}).get("concerns") or [], limit=4, item_limit=200),
        ]
        text = " ".join(item.lower() for item in lines if item).strip()
        if not text:
            return False
        realtime_markers = ("实时", "实时信息", "latest", "real-time", "realtime", "up-to-date", "最新")
        model_limit_markers = (
            "模型",
            "model",
            "原生",
            "natively",
            "本身不支持",
            "does not support",
            "cannot access",
            "无法访问",
        )
        return any(marker in text for marker in realtime_markers) and any(
            marker in text for marker in model_limit_markers
        )

    def _extract_citations_from_tool_result(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or not bool(result.get("ok")):
            return []

        path = str(result.get("path") or arguments.get("path") or "").strip() or None
        out: list[dict[str, Any]] = []

        if str(name).startswith("search_web"):
            query = str(result.get("query") or arguments.get("query") or "").strip()
            for row in list(result.get("results") or [])[:4]:
                if not isinstance(row, dict):
                    continue
                url = str(row.get("url") or "").strip()
                title = str(row.get("title") or "").strip()
                snippet = str(row.get("snippet") or "").strip()
                domain = str(row.get("domain") or self._domain_from_url(url) or "").strip() or None
                if not url and not title:
                    continue
                out.append(
                    {
                        "source_type": "web",
                        "kind": "candidate",
                        "tool": "search_web",
                        "label": title or domain or url or "web result",
                        "url": url or None,
                        "title": title or None,
                        "domain": domain,
                        "locator": f"query={query}" if query else None,
                        "excerpt": self._shorten(snippet, 280),
                        "published_at": str(row.get("published_at") or "").strip() or None,
                        "warning": None,
                        "confidence": "low",
                    }
                )
            return out

        if name == "fetch_web":
            url = str(result.get("url") or arguments.get("url") or "").strip()
            excerpt = str(result.get("content") or "").strip()
            out.append(
                {
                    "source_type": "web",
                    "kind": "evidence",
                    "tool": "fetch_web",
                    "label": str(result.get("title") or self._domain_from_url(url) or url or "web page").strip(),
                    "url": url or None,
                    "title": str(result.get("title") or "").strip() or None,
                    "domain": str(result.get("domain") or self._domain_from_url(url) or "").strip() or None,
                    "locator": str(result.get("canonical_url") or "").strip() or None,
                    "excerpt": self._shorten(excerpt, 320),
                    "published_at": str(result.get("published_at") or "").strip() or None,
                    "warning": str(result.get("warning") or "").strip() or None,
                    "confidence": "high" if excerpt else "medium",
                }
            )
            return out

        if name == "search_text_in_file":
            query = str(result.get("query") or arguments.get("query") or "").strip()
            for match in list(result.get("matches") or [])[:4]:
                if not isinstance(match, dict):
                    continue
                page_hint = int(match.get("page_hint") or 0)
                locator = f"page {page_hint}" if page_hint > 0 else None
                out.append(
                    {
                        "source_type": "document",
                        "kind": "evidence",
                        "tool": "search_text_in_file",
                        "label": Path(path or "document").name,
                        "path": path,
                        "locator": f"{locator}, query={query}" if locator and query else (locator or f"query={query}" if query else None),
                        "excerpt": self._shorten(match.get("context") or "", 320),
                        "warning": None,
                        "confidence": "high",
                    }
                )
            return out

        if name == "read_section_by_heading":
            matched_heading = str(result.get("matched_heading") or result.get("matched_section") or "").strip()
            page_start = int(result.get("page_start") or 0)
            page_end = int(result.get("page_end") or 0)
            locator = matched_heading or None
            if page_start > 0:
                locator = f"{locator or 'section'} | pages {page_start}-{page_end or page_start}"
            out.append(
                {
                    "source_type": "document",
                    "kind": "evidence",
                    "tool": "read_section_by_heading",
                    "label": Path(path or "document").name,
                    "path": path,
                    "locator": locator,
                    "excerpt": self._shorten(result.get("content") or "", 320),
                    "warning": None,
                    "confidence": "high",
                }
            )
            return out

        if name == "table_extract":
            for table in list(result.get("tables") or [])[:3]:
                if not isinstance(table, dict):
                    continue
                page = int(table.get("page") or 0)
                sheet = str(table.get("sheet") or "").strip()
                locator = f"page {page}" if page > 0 else (sheet or None)
                rows = [str(row).strip() for row in list(table.get("rows") or [])[:3] if str(row).strip()]
                out.append(
                    {
                        "source_type": "table",
                        "kind": "evidence",
                        "tool": "table_extract",
                        "label": Path(path or "table").name,
                        "path": path,
                        "locator": locator,
                        "excerpt": self._shorten("\n".join(rows), 320),
                        "warning": None,
                        "confidence": "high",
                    }
                )
            return out

        if name == "search_codebase":
            for match in list(result.get("matches") or [])[:4]:
                if not isinstance(match, dict):
                    continue
                match_path = str(match.get("path") or "").strip()
                line = int(match.get("line") or 0)
                out.append(
                    {
                        "source_type": "codebase",
                        "kind": "evidence",
                        "tool": "search_codebase",
                        "label": Path(match_path or "code").name,
                        "path": match_path or None,
                        "locator": f"line {line}" if line > 0 else None,
                        "excerpt": self._shorten(match.get("text") or "", 320),
                        "warning": None,
                        "confidence": "high",
                    }
                )
            return out

        if name == "fact_check_file":
            for match in list(result.get("evidence") or [])[:3]:
                if not isinstance(match, dict):
                    continue
                page_hint = int(match.get("page_hint") or 0)
                out.append(
                    {
                        "source_type": "document",
                        "kind": "evidence",
                        "tool": "fact_check_file",
                        "label": Path(path or "document").name,
                        "path": path,
                        "locator": f"page {page_hint}" if page_hint > 0 else None,
                        "excerpt": self._shorten(match.get("context") or "", 320),
                        "warning": str(result.get("verdict") or "").strip() or None,
                        "confidence": "medium",
                    }
                )
            return out

        return out

    def _merge_citation_candidates(
        self,
        existing: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged = list(existing)
        seen = {
            (
                str(item.get("tool") or "").strip().lower(),
                str(item.get("url") or "").strip().lower(),
                str(item.get("path") or "").strip().lower(),
                str(item.get("locator") or "").strip().lower(),
                str(item.get("excerpt") or "").strip().lower(),
            )
            for item in merged
            if isinstance(item, dict)
        }
        for item in incoming:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("tool") or "").strip().lower(),
                str(item.get("url") or "").strip().lower(),
                str(item.get("path") or "").strip().lower(),
                str(item.get("locator") or "").strip().lower(),
                str(item.get("excerpt") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _finalize_citation_candidates(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = [item for item in citations if isinstance(item, dict)]
        if any(self._citation_kind(item) == "evidence" for item in prepared):
            prepared = [item for item in prepared if self._citation_kind(item) == "evidence"]

        def sort_key(item: dict[str, Any]) -> tuple[int, int, int, int, int]:
            tool = str(item.get("tool") or "").strip()
            source_type = str(item.get("source_type") or "").strip()
            confidence = str(item.get("confidence") or "medium").strip().lower()

            tool_priority = {
                "fetch_web": 6,
                "fact_check_file": 5,
                "search_text_in_file": 5,
                "read_section_by_heading": 5,
                "table_extract": 5,
                "search_codebase": 4,
                "search_web": 1,
            }.get(tool, 3)
            if source_type in {"document", "table", "codebase"} and tool_priority < 5:
                tool_priority = 5

            confidence_priority = {"high": 3, "medium": 2, "low": 1}.get(confidence, 2)
            excerpt_priority = 1 if str(item.get("excerpt") or "").strip() else 0
            published_priority = 1 if str(item.get("published_at") or "").strip() else 0
            warning_penalty = 0 if not str(item.get("warning") or "").strip() else -1
            return (
                tool_priority,
                confidence_priority,
                excerpt_priority,
                published_priority,
                warning_penalty,
            )

        out: list[dict[str, Any]] = []
        for idx, item in enumerate(sorted(prepared, key=sort_key, reverse=True)[:12], start=1):
            out.append(
                {
                    "id": f"c{idx}",
                    "source_type": str(item.get("source_type") or "other").strip() or "other",
                    "kind": self._citation_kind(item),
                    "tool": str(item.get("tool") or "").strip(),
                    "label": str(item.get("label") or "").strip() or f"source_{idx}",
                    "path": str(item.get("path") or "").strip() or None,
                    "url": str(item.get("url") or "").strip() or None,
                    "title": str(item.get("title") or "").strip() or None,
                    "domain": str(item.get("domain") or "").strip() or None,
                    "locator": str(item.get("locator") or "").strip() or None,
                    "excerpt": self._shorten(str(item.get("excerpt") or "").strip(), 360),
                    "published_at": str(item.get("published_at") or "").strip() or None,
                    "warning": str(item.get("warning") or "").strip() or None,
                    "confidence": str(item.get("confidence") or "medium").strip().lower()
                    if str(item.get("confidence") or "medium").strip().lower() in {"high", "medium", "low"}
                    else "medium",
                }
            )
        return out

    def _domain_from_url(self, raw_url: str) -> str | None:
        try:
            host = (urlparse(str(raw_url or "")).hostname or "").strip().lower()
        except Exception:
            host = ""
        return host or None

    def _normalize_string_list(
        self,
        value: Any,
        *,
        limit: int = 5,
        item_limit: int = 180,
    ) -> list[str]:
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []
        out: list[str] = []
        for item in raw_items:
            text = str(item or "").strip()
            if not text:
                continue
            if text.startswith("- "):
                text = text[2:].strip()
            text = " ".join(text.split())
            if not text:
                continue
            out.append(self._shorten(text, item_limit))
            if len(out) >= max(1, limit):
                break
        return out

    def _parse_json_object(self, raw_text: str) -> dict[str, Any] | None:
        text = str(raw_text or "").strip()
        if not text:
            return None
        candidates = [text]
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None

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

    def _looks_like_understanding_request(self, user_message: str) -> bool:
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if any(hint in text for hint in _NEWS_HINTS):
            return False
        if self._looks_like_inline_document_payload(user_message):
            return True
        if self._requires_evidence_mode(user_message, []):
            return False
        tool_markers = (
            "read_text_file",
            "search_text_in_file",
            "table_extract",
            "fact_check_file",
            "search_codebase",
            "search_web",
            "fetch_web",
            "download_web_file",
        )
        if any(marker in text for marker in tool_markers):
            return False
        return any(hint in text for hint in _UNDERSTANDING_HINTS)

    def _looks_like_inline_document_payload(self, user_message: str) -> bool:
        text = str(user_message or "").strip()
        if len(text) < 120:
            return False
        lowered = text.lower()
        if "<?xml" in lowered:
            return True
        if any(marker in lowered for marker in _INLINE_DOC_CODE_FENCE_HINTS):
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

    def _attachment_is_inline_parseable(self, meta: dict[str, Any]) -> bool:
        suffix = str(meta.get("suffix") or "").strip().lower()
        kind = str(meta.get("kind") or "").strip().lower()
        try:
            size = int(meta.get("size") or 0)
        except Exception:
            size = 0
        if kind != "document":
            return False
        if self._attachment_needs_tooling(meta):
            return False
        parseable_suffixes = {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".pdf",
            ".docx",
            ".xlsx",
            ".xlsm",
            ".xltx",
            ".xltm",
            ".xls",
            ".html",
            ".xml",
            ".atom",
            ".rss",
            ".yaml",
            ".yml",
            ".log",
            ".py",
            ".js",
            ".ts",
            ".tsx",
        }
        return suffix in parseable_suffixes and size <= _ATTACHMENT_INLINE_MAX_BYTES

    def _normalize_route_decision(
        self,
        route: dict[str, Any],
        *,
        fallback: dict[str, Any],
        settings: ChatSettings,
    ) -> dict[str, Any]:
        normalized = dict(fallback)
        normalized.update(route or {})

        normalized["task_type"] = str(normalized.get("task_type") or fallback.get("task_type") or "standard").strip()
        complexity = str(normalized.get("complexity") or fallback.get("complexity") or "medium").strip().lower()
        if complexity not in {"low", "medium", "high"}:
            complexity = "medium"
        normalized["complexity"] = complexity
        normalized["specialists"] = self._normalize_specialists(
            normalized.get("specialists") or fallback.get("specialists") or []
        )

        for key in (
            "use_planner",
            "use_worker_tools",
            "use_reviewer",
            "use_revision",
            "use_structurer",
            "use_web_prefetch",
            "use_conflict_detector",
            "needs_llm_router",
        ):
            normalized[key] = bool(normalized.get(key))

        if not settings.enable_tools:
            normalized["use_worker_tools"] = False
            normalized["use_web_prefetch"] = False

        if not normalized["use_reviewer"]:
            normalized["use_revision"] = False
            normalized["use_conflict_detector"] = False

        if not normalized["use_worker_tools"]:
            normalized["use_web_prefetch"] = False

        normalized["reason"] = str(normalized.get("reason") or fallback.get("reason") or "").strip()
        normalized["source"] = str(normalized.get("source") or fallback.get("source") or "rules").strip() or "rules"
        normalized["summary"] = (
            str(normalized.get("summary") or "").strip()
            or f"task_type={normalized['task_type']}, complexity={normalized['complexity']}"
        )
        normalized["router_model"] = str(normalized.get("router_model") or "").strip()
        return normalized

    def _route_request_by_rules(
        self,
        *,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
    ) -> dict[str, Any]:
        text = (user_message or "").strip().lower()
        has_attachments = bool(attachment_metas)
        spec_lookup_request = self._looks_like_spec_lookup_request(user_message, attachment_metas)
        evidence_required = self._requires_evidence_mode(user_message, attachment_metas)
        attachment_needs_tooling = any(self._attachment_needs_tooling(meta) for meta in attachment_metas)
        inline_parseable_attachments = has_attachments and all(
            self._attachment_is_inline_parseable(meta) for meta in attachment_metas
        )
        inline_document_payload = self._looks_like_inline_document_payload(user_message)
        understanding_request = self._looks_like_understanding_request(user_message)
        web_request = (
            any(hint in text for hint in _NEWS_HINTS)
            or "http://" in text
            or "https://" in text
            or any(hint in text for hint in ("上网", "网上", "联网", "search_web", "fetch_web", "download_web_file"))
        )
        request_requires_tools = self._request_likely_requires_tools(user_message, attachment_metas)

        fallback = {
            "task_type": "standard",
            "complexity": "medium",
            "use_planner": True,
            "use_worker_tools": bool(settings.enable_tools and request_requires_tools),
            "use_reviewer": True,
            "use_revision": True,
            "use_structurer": True,
            "use_web_prefetch": bool(settings.enable_tools and web_request),
            "use_conflict_detector": True,
            "specialists": [],
            "needs_llm_router": False,
            "reason": "rules_default_full_pipeline",
            "source": "rules",
            "summary": "默认走完整流水线。",
            "router_model": "",
        }

        if spec_lookup_request or evidence_required:
            return self._normalize_route_decision(
                {
                    "task_type": "evidence_lookup",
                    "complexity": "high" if has_attachments else "medium",
                    "use_planner": True,
                    "use_worker_tools": True,
                    "use_reviewer": True,
                    "use_revision": True,
                    "use_structurer": True,
                    "use_web_prefetch": False,
                    "use_conflict_detector": True,
                    "specialists": ["file_reader"] if has_attachments else [],
                    "reason": "rules_evidence_or_spec_request",
                    "summary": "检测到查证/定位类任务，保留完整取证链路。",
                },
                fallback=fallback,
                settings=settings,
            )

        if web_request:
            return self._normalize_route_decision(
                {
                    "task_type": "web_research",
                    "complexity": "medium",
                    "use_planner": True,
                    "use_worker_tools": True,
                    "use_reviewer": True,
                    "use_revision": True,
                    "use_structurer": True,
                    "use_web_prefetch": True,
                    "use_conflict_detector": True,
                    "specialists": ["researcher"],
                    "reason": "rules_web_request",
                    "summary": "检测到联网/实时信息请求，启用联网取证链路。",
                },
                fallback=fallback,
                settings=settings,
            )

        if has_attachments and inline_parseable_attachments and understanding_request and not attachment_needs_tooling:
            return self._normalize_route_decision(
                {
                    "task_type": "simple_understanding",
                    "complexity": "low",
                    "use_planner": False,
                    "use_worker_tools": False,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["summarizer"],
                    "reason": "rules_small_parseable_attachment_understanding",
                    "summary": "小型可解析附件的理解任务，直接由 Worker 作答。",
                },
                fallback=fallback,
                settings=settings,
            )

        if not has_attachments and inline_document_payload and not request_requires_tools:
            return self._normalize_route_decision(
                {
                    "task_type": "inline_document_understanding",
                    "complexity": "low",
                    "use_planner": False,
                    "use_worker_tools": False,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["summarizer"],
                    "reason": "rules_inline_document_payload_understanding",
                    "summary": "检测到用户直接粘贴的原始长文本，按 inline 文档直接理解，不要求文件路径。",
                },
                fallback=fallback,
                settings=settings,
            )

        if not has_attachments and not request_requires_tools and not understanding_request and len(text) <= 240:
            return self._normalize_route_decision(
                {
                    "task_type": "simple_qa",
                    "complexity": "low",
                    "use_planner": False,
                    "use_worker_tools": False,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "reason": "rules_simple_qa",
                    "summary": "简单问答，直接由 Worker 回答。",
                },
                fallback=fallback,
                settings=settings,
            )

        if attachment_needs_tooling:
            return self._normalize_route_decision(
                {
                    "task_type": "attachment_tooling",
                    "complexity": "medium",
                    "use_planner": True,
                    "use_worker_tools": True,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["file_reader"],
                    "reason": "rules_attachment_requires_tooling",
                    "summary": "附件需要解包或分块读取，先走 Worker 工具链。",
                },
                fallback=fallback,
                settings=settings,
            )

        if has_attachments and inline_parseable_attachments and not request_requires_tools:
            return self._normalize_route_decision(
                {
                    "task_type": "mixed_attachment",
                    "complexity": "medium",
                    "needs_llm_router": True,
                    "reason": "rules_ambiguous_small_attachment_request",
                    "summary": "附件可直接理解，但用户意图不够明确，交给轻量 Router 补判。",
                },
                fallback=fallback,
                settings=settings,
            )

        if not has_attachments and not request_requires_tools and len(text) <= 800:
            return self._normalize_route_decision(
                {
                    "task_type": "general_qa",
                    "complexity": "medium",
                    "needs_llm_router": True,
                    "reason": "rules_ambiguous_general_request",
                    "summary": "普通问答但复杂度不够明确，交给轻量 Router 补判。",
                },
                fallback=fallback,
                settings=settings,
            )

        return self._normalize_route_decision(fallback, fallback=fallback, settings=settings)

    def _run_router(
        self,
        *,
        requested_model: str,
        user_message: str,
        summary: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        rules_route: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        fallback = self._normalize_route_decision(rules_route, fallback=rules_route, settings=settings)
        if not str(os.environ.get("OPENAI_API_KEY") or "").strip():
            return fallback, json.dumps({"skipped": "OPENAI_API_KEY missing"}, ensure_ascii=False)

        router_input = "\n".join(
            [
                f"user_message:\n{user_message.strip() or '(empty)'}",
                f"history_summary:\n{summary.strip() or '(none)'}",
                f"attachments:\n{self._summarize_attachment_metas_for_agents(attachment_metas)}",
                f"enable_tools={str(settings.enable_tools).lower()}",
                f"rules_task_type={fallback['task_type']}",
                f"rules_complexity={fallback['complexity']}",
                f"rules_reason={fallback['reason']}",
                f"rules_summary={fallback['summary']}",
            ]
        )
        messages = [
            self._SystemMessage(
                content=(
                    "你是轻量 Router。"
                    "你的职责是为当前请求选择最小可行链路，避免所有请求都跑完整流水线。"
                    "优先最小化角色数和工具数，但不能牺牲明显必要的取证。"
                    "只返回 JSON 对象，字段固定为 "
                    "task_type, complexity, use_planner, use_worker_tools, use_reviewer, use_revision, "
                    "use_structurer, use_web_prefetch, use_conflict_detector, specialists, reason, summary。"
                    "specialists 只能从 researcher, file_reader, summarizer, fixer 中选择。"
                    "complexity 只能是 low, medium, high。"
                    "典型规则："
                    "简单文本理解/小附件摘要 => specialists 可选 summarizer；"
                    "规范定位/证据请求 => specialists 可选 file_reader；"
                    "联网/实时问题 => specialists 可选 researcher；"
                    "不要输出思维链。"
                )
            ),
            self._HumanMessage(content=router_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=self.config.summary_model or requested_model,
                max_output_tokens=500,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["router_model"] = effective_model
                fallback["source"] = "rules_fallback"
                fallback["reason"] = f"{fallback['reason']}; router_invalid_json"
                fallback["summary"] = f"{fallback['summary']} Router 未返回标准 JSON，回退规则分诊。"
                return fallback, raw_text
            normalized = self._normalize_route_decision(
                {
                    **parsed,
                        "source": "llm_router",
                        "router_model": effective_model,
                        "needs_llm_router": False,
                    },
                fallback=fallback,
                settings=settings,
            )
            if notes:
                normalized["reason"] = "; ".join(
                    [normalized["reason"], *self._normalize_string_list(notes, limit=2, item_limit=120)]
                ).strip("; ")
            return normalized, raw_text
        except Exception as exc:
            fallback["source"] = "rules_fallback"
            fallback["reason"] = f"{fallback['reason']}; router_failed"
            fallback["summary"] = f"{fallback['summary']} Router 调用失败，回退规则分诊。"
            return fallback, json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _route_request(
        self,
        *,
        requested_model: str,
        user_message: str,
        summary: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
    ) -> tuple[dict[str, Any], str]:
        rules_route = self._route_request_by_rules(
            user_message=user_message,
            attachment_metas=attachment_metas,
            settings=settings,
        )
        if not rules_route.get("needs_llm_router"):
            return rules_route, json.dumps({"source": "rules", "task_type": rules_route.get("task_type")}, ensure_ascii=False)
        return self._run_router(
            requested_model=requested_model,
            user_message=user_message,
            summary=summary,
            attachment_metas=attachment_metas,
            settings=settings,
            rules_route=rules_route,
        )

    def _router_system_hint(self, route: dict[str, Any]) -> str:
        task_type = str(route.get("task_type") or "standard").strip()
        if task_type == "simple_understanding":
            return (
                "本轮属于简单理解任务。"
                "直接基于当前消息与已内联附件内容回答。"
                "不要调用工具，不要输出流程说明，不要把回答改写成证据审计格式。"
            )
        if task_type == "inline_document_understanding":
            return (
                "本轮属于 inline 文档理解任务。"
                "用户已经直接提供了原始文本内容，应直接基于该文本分析。"
                "不要要求本地文件路径，不要引用内部审阅变量，不要改写成证据审计格式。"
            )
        if task_type == "simple_qa":
            return "本轮属于简单问答。直接回答，不要调用工具，不要追加多余审阅话术。"
        if task_type == "attachment_tooling":
            return "本轮附件需要工具预处理。先用必要工具完成读取/解包，再给结论。"
        if task_type == "web_research":
            return "本轮属于联网信息任务。优先用联网工具取证，再回答。"
        if task_type == "evidence_lookup":
            return "本轮属于查证/定位任务。优先完整取证，再给可复核答案。"
        return ""

    def _build_execution_plan(
        self,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        route: dict[str, Any] | None = None,
    ) -> list[str]:
        route = route or {}
        specialists = self._normalize_specialists(route.get("specialists") or [])
        plan = [
            f"Router 分诊任务类型与链路（task_type={str(route.get('task_type') or 'standard')}, complexity={str(route.get('complexity') or 'medium')}）。"
        ]
        for specialist in specialists:
            plan.append(self._specialist_plan_line(specialist))
        if route.get("use_planner"):
            plan.append("Planner 提炼目标、约束与执行计划。")
        plan.append("Worker 根据当前链路执行与作答。")
        if attachment_metas:
            plan.append(f"解析附件内容（{len(attachment_metas)} 个）。")
        plan.append(f"结合最近 {settings.max_context_turns} 条历史消息组织上下文。")
        if settings.enable_tools and route.get("use_worker_tools"):
            plan.append("如有必要自动连续调用工具（读文件/列目录/执行命令/联网搜索与抓取）获取事实，不逐步征询。")
            if self.config.enable_session_tools:
                plan.append("涉及历史对话时，自动调用会话工具检索旧 session。")
        if route.get("use_reviewer"):
            plan.append("Reviewer 做最终自检。")
        if route.get("use_revision"):
            plan.append("Revision 按审阅结果做最后修订。")
        if route.get("use_structurer"):
            plan.append("Structurer 在有来源时生成结构化证据包。")
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
        tool_names: list[str] | None = None,
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
                    tool_names=tool_names,
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
                tool_names=tool_names,
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
        tool_names: list[str] | None = None,
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
                tool_names=tool_names,
            )
            prefix = f"模型 {model} 在持续推理阶段失败（{self._shorten(exc, 200)}），已自动恢复重试。"
            return recovered_msg, recovered_runner, recovered_model, [prefix, *notes]

    def _invoke_single_model(
        self,
        messages: list[Any],
        model: str,
        max_output_tokens: int,
        enable_tools: bool,
        tool_names: list[str] | None = None,
    ) -> tuple[Any, Any, list[str]]:
        notes: list[str] = []
        llm = self._build_llm(model=model, max_output_tokens=max_output_tokens)
        runner = llm.bind_tools(self._select_langchain_tools(tool_names)) if enable_tools else llm
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
        runner_fb = llm_fb.bind_tools(self._select_langchain_tools(tool_names)) if enable_tools else llm_fb
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
                    "Read a local text/document file. Auto extracts text from PDF/DOCX/MSG/XLSX. "
                    "Supports chunked reads with start_char. "
                    "For complete reading use max_chars up to 1000000 and continue while has_more=true."
                ),
                args_schema=ReadTextFileArgs,
                func=self._read_text_file_tool,
            ),
            self._StructuredTool.from_function(
                name="search_text_in_file",
                description=(
                    "Search within a local text/document file and return matching evidence snippets. "
                    "Use this first for specs/protocols/command codes; it expands hex variants like 15h/15 h/0x15."
                ),
                args_schema=SearchTextInFileArgs,
                func=self._search_text_in_file_tool,
            ),
            self._StructuredTool.from_function(
                name="multi_query_search",
                description="Run multiple file-search queries against one file and merge the matching evidence snippets.",
                args_schema=MultiQuerySearchArgs,
                func=self._multi_query_search_tool,
            ),
            self._StructuredTool.from_function(
                name="doc_index_build",
                description="Build or inspect a cached PDF document index, including headings and cache status.",
                args_schema=DocIndexBuildArgs,
                func=self._doc_index_build_tool,
            ),
            self._StructuredTool.from_function(
                name="read_section_by_heading",
                description="Read a document section by matching a heading or section number.",
                args_schema=ReadSectionByHeadingArgs,
                func=self._read_section_by_heading_tool,
            ),
            self._StructuredTool.from_function(
                name="table_extract",
                description="Extract tables from a PDF/XLSX file, optionally narrowed by query or page hint.",
                args_schema=TableExtractArgs,
                func=self._table_extract_tool,
            ),
            self._StructuredTool.from_function(
                name="fact_check_file",
                description="Check whether a file contains evidence that supports or conflicts with a claim.",
                args_schema=FactCheckFileArgs,
                func=self._fact_check_file_tool,
            ),
            self._StructuredTool.from_function(
                name="search_codebase",
                description="Search code/text files under a local root and return file, line, and excerpt matches.",
                args_schema=SearchCodebaseArgs,
                func=self._search_codebase_tool,
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
                name="extract_msg_attachments",
                description=(
                    "Extract attachments from a local .msg email into a target directory, "
                    "then continue reading those files."
                ),
                args_schema=ExtractMsgAttachmentsArgs,
                func=self._extract_msg_attachments_tool,
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

    def _select_langchain_tools(self, tool_names: list[str] | None = None) -> list[Any]:
        if not tool_names:
            return self._lc_tools
        selected: list[Any] = []
        seen: set[str] = set()
        for name in tool_names:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            tool = self._lc_tool_map.get(key)
            if tool is None:
                continue
            seen.add(key)
            selected.append(tool)
        return selected

    def _reviewer_readonly_tool_names(self) -> list[str]:
        return [
            "list_directory",
            "read_text_file",
            "search_text_in_file",
            "multi_query_search",
            "doc_index_build",
            "read_section_by_heading",
            "table_extract",
            "fact_check_file",
            "search_codebase",
            "search_web",
            "fetch_web",
        ]

    def _normalize_reviewer_verdict(
        self,
        raw_verdict: Any,
        *,
        risks: Any,
        followups: Any,
        spec_lookup_request: bool,
        evidence_required_mode: bool,
        readonly_checks: list[str],
        conflict_has_conflict: bool,
        conflict_realtime_only: bool,
        web_tools_success: bool,
    ) -> str:
        verdict = str(raw_verdict or "pass").strip().lower()
        if verdict == "needs_attention":
            if (conflict_has_conflict and not (conflict_realtime_only and web_tools_success)) or (
                spec_lookup_request and "search_text_in_file" not in set(readonly_checks)
            ):
                return "block"
            return "warn"
        if verdict in {"pass", "warn", "block"}:
            if verdict == "block" and conflict_realtime_only and web_tools_success:
                return "warn"
            return verdict

        has_risks = bool(self._normalize_string_list(risks or [], limit=4, item_limit=180))
        has_followups = bool(self._normalize_string_list(followups or [], limit=4, item_limit=180))
        readonly_set = set(readonly_checks)
        if conflict_has_conflict and not (conflict_realtime_only and web_tools_success):
            return "block"
        if spec_lookup_request and "search_text_in_file" not in readonly_set:
            return "block"
        if evidence_required_mode and not readonly_set:
            return "block"
        if has_risks or has_followups:
            return "warn"
        return "pass"

    def _summarize_reviewer_tool_result(self, *, name: str, result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return f"{name} 返回了非结构化结果。"

        if not bool(result.get("ok")):
            return f"{name} 失败: {self._shorten(result.get('error') or 'unknown error', 120)}"

        if name == "fact_check_file":
            verdict = str(result.get("verdict") or "unknown").strip() or "unknown"
            evidence_count = int(result.get("evidence_count") or 0)
            queries = self._normalize_string_list(result.get("queries_used") or [], limit=3, item_limit=40)
            query_text = ", ".join(queries) if queries else "(none)"
            return f"fact_check_file verdict={verdict}, evidence={evidence_count}, queries={query_text}"

        if name == "search_text_in_file":
            query = str(result.get("query") or "").strip() or "(empty)"
            matches = list(result.get("matches") or [])
            match_count = int(result.get("match_count") or len(matches))
            first = matches[0] if matches else {}
            page_hint = int(first.get("page_hint") or 0)
            matched_text = self._shorten(first.get("matched_text") or "", 60) if first else ""
            if page_hint > 0:
                return f"search_text_in_file query={query}, matches={match_count}, first_page={page_hint}, first_hit={matched_text or '(none)'}"
            return f"search_text_in_file query={query}, matches={match_count}, first_hit={matched_text or '(none)'}"

        if name == "multi_query_search":
            queries = self._normalize_string_list(result.get("queries") or [], limit=4, item_limit=40)
            matches = list(result.get("matches") or [])
            match_count = int(result.get("match_count") or len(matches))
            first = matches[0] if matches else {}
            page_hint = int(first.get("page_hint") or 0)
            return f"multi_query_search queries={', '.join(queries) or '(none)'}, matches={match_count}, first_page={page_hint or 'n/a'}"

        if name == "doc_index_build":
            page_count = int(result.get("page_count") or 0)
            heading_count = int(result.get("heading_count") or 0)
            cached = bool(result.get("cached"))
            return f"doc_index_build cached={str(cached).lower()}, pages={page_count}, headings={heading_count}"

        if name == "read_section_by_heading":
            heading = str(result.get("matched_heading") or result.get("matched_section") or "").strip() or "(not found)"
            page_start = int(result.get("page_start") or 0)
            page_end = int(result.get("page_end") or 0)
            if page_start > 0:
                return f"read_section_by_heading matched={heading}, pages={page_start}-{page_end or page_start}"
            return f"read_section_by_heading matched={heading}"

        if name == "table_extract":
            tables = list(result.get("tables") or [])
            table_count = int(result.get("table_count") or len(tables))
            first = tables[0] if tables else {}
            page = int(first.get("page") or 0)
            rows = len(first.get("rows") or []) if isinstance(first, dict) else 0
            if page > 0:
                return f"table_extract tables={table_count}, first_page={page}, first_rows={rows}"
            return f"table_extract tables={table_count}, first_rows={rows}"

        if name == "search_codebase":
            matches = list(result.get("matches") or [])
            match_count = int(result.get("match_count") or len(matches))
            first = matches[0] if matches else {}
            path = str(first.get("path") or "").strip()
            line = int(first.get("line") or 0)
            if path:
                return f"search_codebase matches={match_count}, first={path}:{line or '?'}"
            return f"search_codebase matches={match_count}"

        if name == "search_web":
            query = str(result.get("query") or "").strip() or "(empty)"
            count = int(result.get("count") or 0)
            engine = str(result.get("engine") or "unknown").strip() or "unknown"
            rows = list(result.get("results") or [])
            first = rows[0] if rows else {}
            first_title = self._shorten(first.get("title") or "", 60) if isinstance(first, dict) else ""
            return f"search_web query={query}, count={count}, engine={engine}, first={first_title or '(none)'}"

        if name == "fetch_web":
            url = str(result.get("url") or "").strip() or "(empty)"
            source_format = str(result.get("source_format") or result.get("content_type") or "unknown").strip()
            length = int(result.get("length") or 0)
            warning = self._shorten(result.get("warning") or "", 80)
            if warning:
                return (
                    f"fetch_web url={self._shorten(url, 80)}, format={source_format or 'unknown'}, "
                    f"length={length}, warning={warning}"
                )
            return f"fetch_web url={self._shorten(url, 80)}, format={source_format or 'unknown'}, length={length}"

        if name == "read_text_file":
            path = str(result.get("path") or "").strip()
            length = int(result.get("length") or 0)
            start_char = int(result.get("start_char") or 0)
            end_char = int(result.get("end_char") or 0)
            truncated = bool(result.get("truncated"))
            return (
                f"read_text_file path={self._shorten(path, 60)}, chars={length}, "
                f"range={start_char}-{end_char}, truncated={str(truncated).lower()}"
            )

        if name == "list_directory":
            path = str(result.get("path") or "").strip() or "."
            entries = result.get("entries") or []
            count = len(entries) if isinstance(entries, list) else 0
            return f"list_directory path={self._shorten(path, 60)}, entries={count}"

        return f"{name} 已完成复核。"

    def _run_shell_tool(self, command: str, cwd: str = ".", timeout_sec: int = 15) -> str:
        result = self.tools.run_shell(command=command, cwd=cwd, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _list_directory_tool(self, path: str = ".", max_entries: int = 200) -> str:
        result = self.tools.list_directory(path=path, max_entries=max_entries)
        return json.dumps(result, ensure_ascii=False)

    def _read_text_file_tool(self, path: str, start_char: int = 0, max_chars: int = 200000) -> str:
        result = self.tools.read_text_file(path=path, start_char=start_char, max_chars=max_chars)
        return json.dumps(result, ensure_ascii=False)

    def _search_text_in_file_tool(
        self, path: str, query: str, max_matches: int = 8, context_chars: int = 280
    ) -> str:
        result = self.tools.search_text_in_file(
            path=path,
            query=query,
            max_matches=max_matches,
            context_chars=context_chars,
        )
        return json.dumps(result, ensure_ascii=False)

    def _multi_query_search_tool(
        self, path: str, queries: list[str], per_query_max_matches: int = 3, context_chars: int = 280
    ) -> str:
        result = self.tools.multi_query_search(
            path=path,
            queries=queries,
            per_query_max_matches=per_query_max_matches,
            context_chars=context_chars,
        )
        return json.dumps(result, ensure_ascii=False)

    def _doc_index_build_tool(self, path: str, force_rebuild: bool = False, max_headings: int = 400) -> str:
        result = self.tools.doc_index_build(path=path, force_rebuild=force_rebuild, max_headings=max_headings)
        return json.dumps(result, ensure_ascii=False)

    def _read_section_by_heading_tool(self, path: str, heading: str, max_chars: int = 12000) -> str:
        result = self.tools.read_section_by_heading(path=path, heading=heading, max_chars=max_chars)
        return json.dumps(result, ensure_ascii=False)

    def _table_extract_tool(
        self, path: str, query: str = "", page_hint: int = 0, max_tables: int = 5, max_rows: int = 25
    ) -> str:
        result = self.tools.table_extract(
            path=path,
            query=query,
            page_hint=page_hint,
            max_tables=max_tables,
            max_rows=max_rows,
        )
        return json.dumps(result, ensure_ascii=False)

    def _fact_check_file_tool(
        self, path: str, claim: str, queries: list[str] | None = None, max_evidence: int = 6
    ) -> str:
        result = self.tools.fact_check_file(
            path=path,
            claim=claim,
            queries=queries or [],
            max_evidence=max_evidence,
        )
        return json.dumps(result, ensure_ascii=False)

    def _search_codebase_tool(
        self,
        query: str,
        root: str = ".",
        max_matches: int = 20,
        file_glob: str = "",
        use_regex: bool = False,
        case_sensitive: bool = False,
    ) -> str:
        result = self.tools.search_codebase(
            query=query,
            root=root,
            max_matches=max_matches,
            file_glob=file_glob,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
        )
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

    def _extract_msg_attachments_tool(
        self,
        msg_path: str,
        dst_dir: str = "",
        overwrite: bool = True,
        create_dirs: bool = True,
        max_attachments: int = 500,
        max_total_bytes: int = 524288000,
    ) -> str:
        result = self.tools.extract_msg_attachments(
            msg_path=msg_path,
            dst_dir=dst_dir,
            overwrite=overwrite,
            create_dirs=create_dirs,
            max_attachments=max_attachments,
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
            msg_hint_line = (
                "该文件是 MSG 邮件；若需读取其中附件（如 xlsx/png），先调用 "
                "extract_msg_attachments(msg_path=该路径, dst_dir=目标目录)。"
                "当用户说“完整/全部解释邮件”时，必须执行该步骤，不要跳过。\n"
                if suffix == ".msg"
                else ""
            )

            if kind == "document":
                if history_turn_count > 0 and file_size > _FOLLOWUP_INLINE_MAX_BYTES:
                    parts.append(
                        {
                            "type": "text",
                            "text": (
                                f"[附件文档: {name}] 当前为跟进轮次，为避免重复消耗 token，本轮默认仅提供路径。\n"
                                f"{local_path_line}{file_size_line}{zip_hint_line}{msg_hint_line}"
                                "若任务是在规范/协议中定位章节或命令码，先调用 search_text_in_file(path=该路径, query=目标关键词)；"
                                "若用户已给出章节/heading，优先调用 read_section_by_heading(path=该路径, heading=...)；"
                                "若用户提到表格/opcode 表，优先调用 table_extract(path=该路径, query=...)；"
                                "随后再用 read_text_file(path=该路径, start_char=..., max_chars=...) 读取命中上下文，不要先询问用户。"
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
                                f"{local_path_line}{file_size_line}{zip_hint_line}{msg_hint_line}"
                                "若任务是在规范/协议中定位章节或命令码，先调用 search_text_in_file(path=该路径, query=目标关键词)；"
                                "若用户已给出章节/heading，优先调用 read_section_by_heading(path=该路径, heading=...)；"
                                "若用户提到表格/opcode 表，优先调用 table_extract(path=该路径, query=...)；"
                                "随后再用 read_text_file(path=该路径, start_char=..., max_chars=...) 读取命中上下文后再分析，不要先询问用户。"
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
                            "text": f"\n[附件文档: {name}]\n{local_path_line}{zip_hint_line}{msg_hint_line}{extracted}",
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
                                    f"{local_path_line}{zip_hint_line}{msg_hint_line}{preview}"
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

    def _looks_like_spec_lookup_request(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        if not attachment_metas:
            return False

        text = (user_message or "").strip().lower()
        if not text:
            return False

        if re.search(r"(?i)\b(?:0x[0-9a-f]{1,4}|[0-9a-f]{1,4}h)\b", text):
            return True

        hints = (
            "spec",
            "specification",
            "protocol",
            "opcode",
            "command",
            "register",
            "section",
            "chapter",
            "status code",
            "feature id",
            "feature identifier",
            "nvme",
            "规范",
            "协议",
            "规格",
            "规格书",
            "命令",
            "寄存器",
            "章节",
            "条目",
            "状态码",
        )
        return any(hint in text for hint in hints)

    def _requires_evidence_mode(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if not attachment_metas and self._looks_like_inline_document_payload(user_message):
            return False
        hints = (
            "spec",
            "specification",
            "protocol",
            "opcode",
            "register",
            "section",
            "chapter",
            "heading",
            "table",
            "pdf",
            "docx",
            "xlsx",
            "codebase",
            "repo",
            "source code",
            "line ",
            "规范",
            "协议",
            "规格",
            "章节",
            "表格",
            "源码",
            "代码库",
            "行号",
            "路径",
            "页码",
            "证据",
            "出处",
            "引用",
            "定位",
            "命中",
            "查证",
            "核对",
            "根据原文",
            "according to",
            "citation",
        )
        return any(hint in text for hint in hints)

    def _attachment_needs_tooling(self, meta: dict[str, Any]) -> bool:
        suffix = str(meta.get("suffix") or "").strip().lower()
        kind = str(meta.get("kind") or "").strip().lower()
        try:
            size = int(meta.get("size") or 0)
        except Exception:
            size = 0

        if suffix in {".zip", ".msg"}:
            return True
        if kind == "document" and size > _ATTACHMENT_INLINE_MAX_BYTES:
            return True
        return False

    def _evidence_mode_needs_more_support(
        self,
        ai_msg: Any,
        tool_events: list[ToolEvent],
        spec_lookup_request: bool = False,
    ) -> bool:
        content = self._content_to_text(getattr(ai_msg, "content", "")).strip().lower()
        if not content:
            return True

        tool_names = {tool.name for tool in tool_events}
        evidence_tool_hits = tool_names & {
            "search_text_in_file",
            "multi_query_search",
            "read_text_file",
            "read_section_by_heading",
            "table_extract",
            "fact_check_file",
            "search_codebase",
        }
        if spec_lookup_request and "search_text_in_file" not in tool_names:
            return True
        if not evidence_tool_hits:
            return True
        if spec_lookup_request and not (
            {"read_text_file", "read_section_by_heading", "table_extract", "fact_check_file"} & tool_names
        ):
            return True

        evidence_markers = (
            "page",
            "页",
            "section",
            "chapter",
            "章节",
            "命中",
            "片段",
            "line ",
            "行 ",
            "路径",
            "according to",
            "在当前提取文本中",
        )
        return not any(marker in content for marker in evidence_markers)

    def _request_likely_requires_tools(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        if any(self._attachment_needs_tooling(meta) for meta in attachment_metas):
            return True
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if not attachment_metas and self._looks_like_inline_document_payload(user_message):
            return False
        if "http://" in text or "https://" in text:
            return True

        direct_hints = (
            "路径",
            "目录",
            "上网",
            "网上",
            "查一下",
            "搜一下",
            "read_text_file",
            "search_text_in_file",
            "multi_query_search",
            "read_section_by_heading",
            "table_extract",
            "fact_check_file",
            "search_codebase",
            "write_text_file",
            "append_text_file",
            "replace_in_file",
            "run_shell",
            "search_web",
            "fetch_web",
            "download_web_file",
            ".pdf",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".xlsx",
            ".csv",
            ".zip",
            ".msg",
            "页码",
            "定位",
            "命中",
            "查证",
            "核对",
            "according to",
            "citation",
        )
        if any(hint in text for hint in direct_hints):
            return True
        return any(hint in text for hint in _NEWS_HINTS)

    def _looks_like_permission_gate(
        self,
        ai_msg: Any,
        has_attachments: bool = False,
        request_requires_tools: bool = False,
    ) -> bool:
        text = self._content_to_text(getattr(ai_msg, "content", "")).strip().lower()
        if not text:
            return False
        if len(text) > 5000:
            text = text[:5000]
        attachment_deferral_patterns = (
            "已完成解析",
            "已经完成解析",
            "已经完成了解析",
            "已解析完成",
            "已经解析完成",
            "无需调用工具",
            "无需再调用工具",
            "无需再次调用工具",
            "不需要调用工具",
            "不必调用工具",
            "already parsed",
            "already finished parsing",
            "no need to call tool",
            "no need to use tool",
            "no tools needed",
        )
        if has_attachments and any(p in text for p in attachment_deferral_patterns):
            return True

        general_gate_patterns = (
            "要不要",
            "是否继续",
            "是否要我",
            "你选",
            "请选择",
            "选一个",
            "选一种",
            "二选一",
            "do you want me to continue",
            "should i continue",
            "if you agree",
            "if agreed",
            "如你同意",
            "如果同意",
            "若你同意",
            "如果你同意",
            "如您同意",
            "若您同意",
            "同意的话",
        )
        if not request_requires_tools and not has_attachments:
            return any(p in text for p in general_gate_patterns)

        patterns = (
            *general_gate_patterns,
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
            "你当前的指示中没有新增对读取附件内容的要求",
            "没有新增对读取附件内容的要求",
            "若后续需要解析",
            "后续需要解析",
            "无需调用工具",
            "无需再调用工具",
            "无需再次调用工具",
            "不需要调用工具",
            "已完成解析",
            "已经完成了解析",
            "已解析完成",
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
            "邮件",
            "文档",
            "path",
            "解析",
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
            lines.append(f"msg {idx} | {role}")
            rendered = self._shorten(self._serialize_content_for_debug(content, raw_mode=raw_mode), max_content)
            lines.append(self._indent_block(rendered, prefix="  "))
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
                lines.append(f"part {idx} | {item}")
                continue
            if not isinstance(item, dict):
                lines.append(f"part {idx} | {str(item)}")
                continue

            item_type = item.get("type")
            if item_type == "image_url":
                image_url = item.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else ""
                if isinstance(url, str) and url.startswith("data:"):
                    if raw_mode:
                        preview = self._shorten(url, 1200)
                        lines.append(f"part {idx} | image_url(data_url_len={len(url)}) preview={preview}")
                    else:
                        lines.append(f"part {idx} | image_url(data_url_len={len(url)}) [omitted]")
                else:
                    lines.append(f"part {idx} | {json.dumps(item, ensure_ascii=False, default=str)}")
                continue

            lines.append(f"part {idx} | {json.dumps(item, ensure_ascii=False, default=str)}")
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
                    f"call {idx} | {name}(args={self._shorten(json.dumps(args, ensure_ascii=False), 600)})"
                )

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if text.strip():
            lines.append(f"text_preview={self._shorten(text, 1200)}")

        if not lines:
            lines.append("empty response content")
        return "\n".join(lines)

    def _indent_block(self, text: Any, prefix: str = "  ") -> str:
        raw = str(text or "")
        if not raw:
            return ""
        return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in raw.splitlines())

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
