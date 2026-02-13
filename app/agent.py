from __future__ import annotations

import json
import os
from typing import Any
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


class RunShellArgs(BaseModel):
    command: str = Field(description="Shell command, e.g. `ls -la` or `rg TODO .`")
    cwd: str = Field(default=".", description="Working directory relative to workspace")
    timeout_sec: int = Field(default=15, ge=1, le=30)


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


class WriteTextFileArgs(BaseModel):
    path: str
    content: str
    overwrite: bool = True
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


class SearchWebArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)
    timeout_sec: int = Field(default=12, ge=3, le=30)


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
    ) -> tuple[str, list[ToolEvent], str, list[str], list[str], list[dict[str, Any]], dict[str, int]]:
        model = settings.model or self.config.default_model
        style_hint = _STYLE_HINTS.get(settings.response_style, _STYLE_HINTS["normal"])
        execution_plan = self._build_execution_plan(attachment_metas=attachment_metas, settings=settings)
        execution_trace: list[str] = []
        debug_flow: list[dict[str, Any]] = []
        usage_total = self._empty_usage()
        allowed_roots_text = ", ".join(str(p) for p in self.config.allowed_roots)

        debug_raw = bool(getattr(settings, "debug_raw", False))
        debug_limit = 120000 if debug_raw else 3200

        def add_debug(stage: str, title: str, detail: str) -> None:
            debug_flow.append(
                {
                    "step": len(debug_flow) + 1,
                    "stage": stage,
                    "title": title,
                    "detail": self._shorten(detail, debug_limit),
                }
            )

        messages: list[Any] = [
            self._SystemMessage(
                content=(
                    f"{self.config.system_prompt}\n\n"
                    f"输出风格: {style_hint}\n"
                    "处理本地文件请求时，先调用工具再下结论，不要凭空判断权限。\n"
                    f"可访问路径根目录: {allowed_roots_text}\n"
                    "读取文件优先使用 list_directory/read_text_file；"
                    "大文件优先用 read_text_file(start_char, max_chars) 分块读取；"
                    "复制文件优先使用 copy_file（不要用读写拼接，避免截断）；"
                    "改写或新建文件优先使用 replace_in_file/write_text_file，尽量使用绝对路径。\n"
                    "联网任务优先先用 search_web(query) 自动找候选链接，再用 fetch_web(url) 读正文；"
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
        execution_trace.append(f"工具开关: {'开启' if settings.enable_tools else '关闭'}。")
        execution_trace.append(f"可访问根目录: {allowed_roots_text}")

        if summary.strip():
            messages.append(self._SystemMessage(content=f"历史摘要:\n{summary}"))
            execution_trace.append("已加载历史摘要，减少上下文占用。")

        for turn in history_turns[-settings.max_context_turns :]:
            role = turn.get("role", "user")
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            if role == "assistant":
                messages.append(self._AIMessage(content=text))
            else:
                messages.append(self._HumanMessage(content=text))
        execution_trace.append(f"已载入最近 {min(len(history_turns), settings.max_context_turns)} 条历史消息。")

        user_content, attachment_note, attachment_issues = self._build_user_content(user_message, attachment_metas)
        messages.append(self._HumanMessage(content=user_content))
        tool_events: list[ToolEvent] = []
        if attachment_metas:
            execution_trace.append(f"已处理 {len(attachment_metas)} 个附件输入。")
        for issue in attachment_issues:
            execution_trace.append(f"附件提示: {issue}")

        prefetch_payload = self._auto_prefetch_web(user_message, settings.enable_tools)
        if prefetch_payload:
            messages.append(self._SystemMessage(content=prefetch_payload["context"]))
            execution_trace.append(
                f"已自动预搜索网络候选: {prefetch_payload.get('count', 0)} 条（query={prefetch_payload['query']}）。"
            )
            warning = prefetch_payload.get("warning")
            if warning:
                execution_trace.append(f"预搜索提示: {warning}")
            tool_events.append(
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
                f"model={model}, enable_tools={settings.enable_tools}, max_output_tokens={settings.max_output_tokens}, "
                f"debug_raw={debug_raw}, "
                f"history_turns_used={min(len(history_turns), settings.max_context_turns)}, "
                f"attachments={len(attachment_metas)}\n"
                f"message_roles={self._summarize_message_roles(messages)}\n"
                f"user_message_preview={self._shorten(user_message, 400 if not debug_raw else 20000)}\n"
                f"request_payload:\n{self._serialize_messages_for_debug(messages, raw_mode=debug_raw)}"
            ),
        )

        execution_trace.append("开始模型推理。")

        try:
            ai_msg, runner = self._invoke_chat_with_runner(
                messages=messages,
                model=model,
                max_output_tokens=settings.max_output_tokens,
                enable_tools=settings.enable_tools,
            )
            usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
            add_debug(
                stage="llm_to_backend",
                title="LLM -> 后端 首次响应",
                detail=self._summarize_ai_response(ai_msg, raw_mode=debug_raw),
            )
        except Exception as exc:
            execution_trace.append(f"模型请求失败: {exc}")
            add_debug(stage="llm_error", title="LLM 请求失败", detail=str(exc))
            return (
                f"请求模型失败: {exc}",
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                debug_flow,
                usage_total,
            )

        for _ in range(12):
            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            if not settings.enable_tools or not tool_calls:
                break

            messages.append(ai_msg)
            for call in tool_calls:
                name = call.get("name") or "unknown"
                arguments = call.get("args") or {}
                if not isinstance(arguments, dict):
                    arguments = {}

                result = self.tools.execute(name, arguments)
                result_json = json.dumps(result, ensure_ascii=False)
                execution_trace.append(f"执行工具: {name}")
                add_debug(
                    stage="llm_to_backend",
                    title=f"LLM -> 后端 工具调用 {name}",
                    detail=f"args={self._shorten(json.dumps(arguments, ensure_ascii=False), 1200 if not debug_raw else 50000)}",
                )

                tool_events.append(
                    ToolEvent(
                        name=name,
                        input=arguments,
                        output_preview=result_json[:1200],
                    )
                )

                call_id = call.get("id") or f"call_{len(tool_events)}"
                messages.append(
                    self._ToolMessage(
                        content=result_json,
                        tool_call_id=call_id,
                        name=name,
                    )
                )
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
                        content=result_json,
                        raw_mode=debug_raw,
                    ),
                )

            try:
                ai_msg = runner.invoke(messages)
                usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                add_debug(
                    stage="llm_to_backend",
                    title="LLM -> 后端 后续响应",
                    detail=self._summarize_ai_response(ai_msg, raw_mode=debug_raw),
                )
            except Exception as exc:
                execution_trace.append(f"工具后续推理失败: {exc}")
                add_debug(stage="llm_error", title="工具后续推理失败", detail=str(exc))
                return (
                    f"工具执行后续推理失败: {exc}",
                    tool_events,
                    attachment_note,
                    execution_plan,
                    execution_trace,
                    debug_flow,
                    usage_total,
                )

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if not text.strip():
            text = "模型未返回可见文本。"
        execution_trace.append("已生成最终答复。")
        add_debug(
            stage="llm_final",
            title="LLM 最终输出",
            detail=f"text_chars={len(text)}\npreview={self._shorten(text, 1200 if not debug_raw else 50000)}",
        )
        return text, tool_events, attachment_note, execution_plan, execution_trace, debug_flow, usage_total

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
            plan.append("如有必要调用工具（读文件/列目录/执行命令/联网搜索与抓取）获取事实。")
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
    ) -> tuple[Any, Any]:
        llm = self._build_llm(model=model, max_output_tokens=max_output_tokens)
        runner = llm.bind_tools(self._lc_tools) if enable_tools else llm
        try:
            return runner.invoke(messages), runner
        except Exception as exc:
            if not self._is_405_error(exc):
                raise

        fallback_use_responses = not self.config.openai_use_responses_api
        llm_fb = self._build_llm(
            model=model,
            max_output_tokens=max_output_tokens,
            use_responses_api=fallback_use_responses,
        )
        runner_fb = llm_fb.bind_tools(self._lc_tools) if enable_tools else llm_fb
        return runner_fb.invoke(messages), runner_fb

    def _invoke_with_405_fallback(
        self,
        messages: list[Any],
        model: str,
        max_output_tokens: int,
        enable_tools: bool,
    ) -> Any:
        response, _ = self._invoke_chat_with_runner(
            messages=messages,
            model=model,
            max_output_tokens=max_output_tokens,
            enable_tools=enable_tools,
        )
        return response

    def _build_langchain_tools(self) -> list[Any]:
        return [
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
                description="Read a UTF-8 text file in workspace. Supports chunked reads with start_char.",
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
                name="write_text_file",
                description="Create or overwrite a UTF-8 text file in workspace.",
                args_schema=WriteTextFileArgs,
                func=self._write_text_file_tool,
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
                name="search_web",
                description="Search web by query and return candidate URLs/snippets before fetch_web.",
                args_schema=SearchWebArgs,
                func=self._search_web_tool,
            ),
        ]

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

    def _search_web_tool(self, query: str, max_results: int = 5, timeout_sec: int = 12) -> str:
        result = self.tools.search_web(query=query, max_results=max_results, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _build_user_content(
        self, user_message: str, attachment_metas: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str, list[str]]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": user_message}]
        notes: list[str] = []
        issues: list[str] = []

        for meta in attachment_metas:
            name = meta.get("original_name", "file")
            path = meta.get("path", "")
            kind = meta.get("kind", "other")
            mime = meta.get("mime", "application/octet-stream")

            if kind == "document":
                extracted = extract_document_text(path, self.config.max_attachment_chars)
                if extracted:
                    parts.append({"type": "text", "text": f"\n[附件文档: {name}]\n{extracted}"})
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
                                    f"[附件文档: {name}] 未识别为结构化文本，已附带文件预览。\n{preview}"
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
                    parts.append({"type": "text", "text": f"[附件图片: {name}]"})
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
                            "text": f"[附件: {name}] 二进制/未知类型，已附带文件预览。\n{preview}",
                        }
                    )
                    notes.append(f"其他(预览):{name}")
                    issues.append(f"{name} 附件类型未知，已提供二进制预览。")
                except Exception as exc:
                    parts.append({"type": "text", "text": f"[附件: {name}] 读取失败: {exc}"})
                    notes.append(f"其他(失败):{name}")
                    issues.append(f"{name} 附件读取失败: {exc}")

        return parts, "；".join(notes), issues

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

    def _is_405_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "405" in text or "method not allowed" in text
