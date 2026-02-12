from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from app.attachments import extract_document_text, image_to_data_url
from app.config import AppConfig
from app.local_tools import LocalToolExecutor
from app.models import ChatSettings, ToolEvent


_STYLE_HINTS = {
    "short": "回答尽量简短，先给结论，再给最多3条关键点。",
    "normal": "回答清晰、可执行，避免冗长。",
    "long": "回答可适当详细，但要结构化并突出行动建议。",
}


class RunShellArgs(BaseModel):
    command: str = Field(description="Shell command, e.g. `ls -la` or `rg TODO .`")
    cwd: str = Field(default=".", description="Working directory relative to workspace")
    timeout_sec: int = Field(default=15, ge=1, le=30)


class ListDirectoryArgs(BaseModel):
    path: str = Field(default=".")
    max_entries: int = Field(default=200, ge=1, le=500)


class ReadTextFileArgs(BaseModel):
    path: str
    max_chars: int = Field(default=10000, ge=128, le=50000)


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

        keep = max(2, min(40, keep_last_turns))
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
    ) -> tuple[str, list[ToolEvent], str, list[str], list[str]]:
        model = settings.model or self.config.default_model
        style_hint = _STYLE_HINTS.get(settings.response_style, _STYLE_HINTS["normal"])
        execution_plan = self._build_execution_plan(attachment_metas=attachment_metas, settings=settings)
        execution_trace: list[str] = []

        messages: list[Any] = [
            self._SystemMessage(content=f"{self.config.system_prompt}\n\n输出风格: {style_hint}")
        ]

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

        user_content, attachment_note = self._build_user_content(user_message, attachment_metas)
        messages.append(self._HumanMessage(content=user_content))
        if attachment_metas:
            execution_trace.append(f"已处理 {len(attachment_metas)} 个附件输入。")

        tool_events: list[ToolEvent] = []
        execution_trace.append("开始模型推理。")

        try:
            ai_msg, runner = self._invoke_chat_with_runner(
                messages=messages,
                model=model,
                max_output_tokens=settings.max_output_tokens,
                enable_tools=settings.enable_tools,
            )
        except Exception as exc:
            execution_trace.append(f"模型请求失败: {exc}")
            return f"请求模型失败: {exc}", tool_events, attachment_note, execution_plan, execution_trace

        for _ in range(6):
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

            try:
                ai_msg = runner.invoke(messages)
            except Exception as exc:
                execution_trace.append(f"工具后续推理失败: {exc}")
                return f"工具执行后续推理失败: {exc}", tool_events, attachment_note, execution_plan, execution_trace

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if not text.strip():
            text = "模型未返回可见文本。"
        execution_trace.append("已生成最终答复。")
        return text, tool_events, attachment_note, execution_plan, execution_trace

    def _build_execution_plan(self, attachment_metas: list[dict[str, Any]], settings: ChatSettings) -> list[str]:
        plan = ["理解你的目标和约束。"]
        if attachment_metas:
            plan.append(f"解析附件内容（{len(attachment_metas)} 个）。")
        plan.append(f"结合最近 {settings.max_context_turns} 条历史消息组织上下文。")
        if settings.enable_tools:
            plan.append("如有必要调用本地工具（读文件/列目录/执行命令）获取事实。")
        plan.append("汇总结论并按你选择的回答长度输出。")
        return plan

    def _build_llm(self, model: str, max_output_tokens: int, use_responses_api: bool | None = None):
        selected_use_responses = self.config.openai_use_responses_api if use_responses_api is None else use_responses_api
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "max_tokens": max_output_tokens,
            "temperature": 0,
            "use_responses_api": selected_use_responses,
        }
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
                description="Read a UTF-8 text file in workspace.",
                args_schema=ReadTextFileArgs,
                func=self._read_text_file_tool,
            ),
        ]

    def _run_shell_tool(self, command: str, cwd: str = ".", timeout_sec: int = 15) -> str:
        result = self.tools.run_shell(command=command, cwd=cwd, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _list_directory_tool(self, path: str = ".", max_entries: int = 200) -> str:
        result = self.tools.list_directory(path=path, max_entries=max_entries)
        return json.dumps(result, ensure_ascii=False)

    def _read_text_file_tool(self, path: str, max_chars: int = 10000) -> str:
        result = self.tools.read_text_file(path=path, max_chars=max_chars)
        return json.dumps(result, ensure_ascii=False)

    def _build_user_content(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": user_message}]
        notes: list[str] = []

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
                else:
                    parts.append({"type": "text", "text": f"[附件文档: {name}] 该格式暂不支持解析文本。"})
                    notes.append(f"文档(未解析):{name}")
            elif kind == "image":
                try:
                    data_url = image_to_data_url(path, mime)
                    parts.append({"type": "text", "text": f"[附件图片: {name}]"})
                    parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    notes.append(f"图片:{name}")
                except Exception as exc:
                    parts.append({"type": "text", "text": f"[附件图片: {name}] 读取失败: {exc}"})
                    notes.append(f"图片(失败):{name}")
            else:
                parts.append({"type": "text", "text": f"[附件: {name}] 该类型按原样保留，建议转成 txt/pdf/docx 或图片。"})
                notes.append(f"其他:{name}")

        return parts, "；".join(notes)

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
