from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from app.attachments import extract_document_text, image_to_data_url
from app.config import AppConfig
from app.local_tools import LocalToolExecutor, parse_json_arguments
from app.models import ChatSettings, ToolEvent


_STYLE_HINTS = {
    "short": "回答尽量简短，先给结论，再给最多3条关键点。",
    "normal": "回答清晰、可执行，避免冗长。",
    "long": "回答可适当详细，但要结构化并突出行动建议。",
}


class OfficeAgent:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=config.openai_base_url,
        )
        self.tools = LocalToolExecutor(config)

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
            resp = self.client.responses.create(
                model=self.config.summary_model,
                max_output_tokens=450,
                input=[
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "你是会话摘要器。请把历史对话压缩成可供后续继续工作的摘要，"
                                    "要保留目标、关键约束、已完成动作、未完成事项。"
                                ),
                            }
                        ],
                    },
                    {"role": "user", "content": [{"type": "input_text", "text": raw}]},
                ],
            )
            summarized = (resp.output_text or "").strip()
            if summarized:
                return summarized
        except Exception:
            pass

        # fallback when summarize call is unavailable
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
    ) -> tuple[str, list[ToolEvent], str]:
        model = settings.model or self.config.default_model
        style_hint = _STYLE_HINTS.get(settings.response_style, _STYLE_HINTS["normal"])

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"{self.config.system_prompt}\n\n输出风格: {style_hint}",
                    }
                ],
            }
        ]

        if summary.strip():
            messages.append(
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": f"历史摘要:\n{summary}"}],
                }
            )

        for turn in history_turns[-settings.max_context_turns :]:
            role = turn.get("role", "user")
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            messages.append(
                {
                    "role": role if role in {"user", "assistant"} else "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )

        user_parts, attachment_note = self._build_user_parts(user_message, attachment_metas)
        messages.append({"role": "user", "content": user_parts})

        tools = self.tools.tool_specs if settings.enable_tools else []
        tool_events: list[ToolEvent] = []

        try:
            response = self.client.responses.create(
                model=model,
                input=messages,
                tools=tools,
                max_output_tokens=settings.max_output_tokens,
            )
        except Exception as exc:
            return f"请求模型失败: {exc}", tool_events, attachment_note

        for _ in range(6):
            calls = self._extract_function_calls(response)
            if not calls:
                break

            tool_outputs = []
            for call in calls:
                name = call.get("name") or "unknown"
                arguments = parse_json_arguments(call.get("arguments"))
                result = self.tools.execute(name, arguments)
                preview = json.dumps(result, ensure_ascii=False)
                tool_events.append(
                    ToolEvent(
                        name=name,
                        input=arguments,
                        output_preview=preview[:1200],
                    )
                )

                call_id = call.get("call_id") or call.get("id")
                if call_id:
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    )

            if not tool_outputs:
                break

            try:
                response = self.client.responses.create(
                    model=model,
                    previous_response_id=response.id,
                    input=tool_outputs,
                    tools=tools,
                    max_output_tokens=settings.max_output_tokens,
                )
            except Exception as exc:
                return f"工具执行后续推理失败: {exc}", tool_events, attachment_note

        text = self._extract_text(response)
        if not text.strip():
            text = "模型未返回可见文本。"
        return text, tool_events, attachment_note

    def _build_user_parts(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        parts: list[dict[str, Any]] = [{"type": "input_text", "text": user_message}]
        notes: list[str] = []

        for meta in attachment_metas:
            name = meta.get("original_name", "file")
            path = meta.get("path", "")
            kind = meta.get("kind", "other")
            mime = meta.get("mime", "application/octet-stream")

            if kind == "document":
                extracted = extract_document_text(path, self.config.max_attachment_chars)
                if extracted:
                    parts.append(
                        {
                            "type": "input_text",
                            "text": f"\n[附件文档: {name}]\n{extracted}",
                        }
                    )
                    notes.append(f"文档:{name}")
                else:
                    parts.append(
                        {
                            "type": "input_text",
                            "text": f"[附件文档: {name}] 该格式暂不支持解析文本。",
                        }
                    )
                    notes.append(f"文档(未解析):{name}")
            elif kind == "image":
                try:
                    data_url = image_to_data_url(path, mime)
                    parts.append({"type": "input_text", "text": f"[附件图片: {name}]"})
                    parts.append({"type": "input_image", "image_url": data_url})
                    notes.append(f"图片:{name}")
                except Exception as exc:
                    parts.append(
                        {
                            "type": "input_text",
                            "text": f"[附件图片: {name}] 读取失败: {exc}",
                        }
                    )
                    notes.append(f"图片(失败):{name}")
            else:
                parts.append(
                    {
                        "type": "input_text",
                        "text": f"[附件: {name}] 该类型按原样保留，建议转成 txt/pdf/docx 或图片。",
                    }
                )
                notes.append(f"其他:{name}")

        note = "；".join(notes)
        return parts, note

    def _extract_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", "")
        if output_text:
            return str(output_text)

        dumped = self._safe_dump(response)
        out: list[str] = []
        for item in dumped.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    ctype = content.get("type")
                    if ctype in {"output_text", "text", "input_text"}:
                        text = content.get("text") or ""
                        if text:
                            out.append(text)
        return "\n".join(out).strip()

    def _extract_function_calls(self, response: Any) -> list[dict[str, Any]]:
        dumped = self._safe_dump(response)
        calls: list[dict[str, Any]] = []

        for item in dumped.get("output", []):
            item_type = item.get("type")
            if item_type == "function_call":
                calls.append(item)
            elif item_type == "tool_call" and isinstance(item.get("function"), dict):
                function = item["function"]
                calls.append(
                    {
                        "name": function.get("name"),
                        "arguments": function.get("arguments"),
                        "call_id": item.get("call_id") or item.get("id"),
                    }
                )
        return calls

    def _safe_dump(self, response: Any) -> dict[str, Any]:
        if hasattr(response, "model_dump"):
            try:
                return response.model_dump()
            except Exception:
                pass
        if isinstance(response, dict):
            return response
        return {}
