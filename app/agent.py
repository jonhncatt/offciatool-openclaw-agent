from __future__ import annotations

from dataclasses import asdict, dataclass, field as dc_field, fields as dataclass_fields, is_dataclass, replace
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from app.agents.reviewer_helpers import (
    normalize_reviewer_verdict as normalize_reviewer_verdict_helper,
    reviewer_readonly_tool_names as reviewer_readonly_tool_names_helper,
    summarize_reviewer_tool_result as summarize_reviewer_tool_result_helper,
)
from app.agents.role_contracts import (
    validate_role_result as validate_role_result_helper,
    validate_runtime_profile as validate_runtime_profile_helper,
)
from app.agents.role_catalog import ROLE_KINDS as _ROLE_KINDS, SPECIALIST_LABELS as _SPECIALIST_LABELS
from app.agents.role_helpers import (
    make_default_role_result as make_default_role_result_helper,
    make_role_context as make_role_context_helper,
    make_role_result as make_role_result_helper,
    make_role_spec as make_role_spec_helper,
    role_payload_dict as role_payload_dict_helper,
)
from app.agents.runtime_profiles import (
    PATCH_WORKER_PROFILE,
    build_runtime_profile_hint,
    default_runtime_profile_for_route,
    runtime_profile_spec,
)
from app.agents.specialist_role import (
    build_specialist_input_payload as build_specialist_input_payload_helper,
    format_specialist_system_hint as format_specialist_system_hint_helper,
    normalize_specialist_brief_payload as normalize_specialist_brief_payload_helper,
    run_specialist_with_context as run_specialist_with_context_helper,
    specialist_contract as specialist_contract_helper,
    specialist_fallback as specialist_fallback_helper,
)
from app.attachments import extract_document_text, image_to_data_url_with_meta, summarize_file_payload
from app.config import AppConfig
from app.codex_runner import CodexResponsesRunner, build_codex_input_payload
from app.core.bootstrap import KernelRuntime, build_kernel_runtime
from app.core.module_manifest import read_module_manifest, write_module_manifest
from app.execution_policy import execution_policy_spec, planner_enabled_for_policy
from app.local_tools import LocalToolExecutor
from app.models import AgentPanel, ChatSettings, ToolEvent
from app.openai_auth import OpenAIAuthManager, normalize_model_for_auth_mode
from app.pipeline_hooks import (
    PIPELINE_HOOK_HANDLERS,
    build_pipeline_hook_panel_payload,
    build_pipeline_hook_telemetry,
)
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
from app.role_runtime import (
    HookDebugEntry,
    HookPromptInjection,
    HookResult,
    RoleContext,
    RoleResult,
    RoleSpec,
    RunState,
)


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
_ATTACHMENT_INLINE_IMAGE_MAX_BYTES = 12 * 1024 * 1024
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
_FOLLOWUP_EXECUTION_ACK_HINTS = (
    "可以",
    "可以了",
    "可以的",
    "行",
    "行吧",
    "好",
    "好的",
    "允许",
    "允许吧",
    "允许调用工具",
    "允许用工具",
    "开始",
    "开始吧",
    "开始搜索",
    "开始查",
    "开始搜",
    "开始检索",
    "继续",
    "继续吧",
    "继续执行",
    "继续搜索",
    "继续查",
    "继续搜",
    "搜吧",
    "查吧",
    "写入",
    "写入吧",
    "开始写入",
    "继续写入",
    "执行写入",
    "应用",
    "应用吧",
    "应用修改",
    "套用",
    "替换",
    "替换吧",
    "更新",
    "更新吧",
    "保存",
    "保存吧",
    "落盘",
    "覆盖写入",
    "apply",
    "apply it",
    "apply changes",
    "write it",
    "save it",
    "update it",
)
_FOLLOWUP_REFERENCE_HINTS = (
    "这个",
    "这个内容",
    "这个结果",
    "这个表",
    "这张表",
    "该表",
    "表格",
    "这份",
    "这段",
    "该段",
    "这版",
    "上述",
    "上面",
    "前面",
    "刚才",
    "刚刚",
    "上一轮",
    "上轮",
    "上一条",
    "上一版",
    "上一段",
    "原文",
    "命中",
    "那个",
    "that",
    "this",
    "above",
    "previous",
)
_FOLLOWUP_TRANSFORM_HINTS = (
    "归纳",
    "整理",
    "重整",
    "重排",
    "重做",
    "排版",
    "表格化",
    "格式化",
    "优化格式",
    "改写",
    "重写",
    "润色",
    "翻译",
    "译成",
    "译为",
    "中文",
    "英文",
    "双语",
    "写进",
    "写到",
    "放到",
    "放进",
    "改成",
    "生成版本",
    "几个版本",
    "文档",
    "报告",
    "邮件",
    "redmine",
    "summarize",
    "rewrite",
    "polish",
    "translate",
    "translation",
)

_UNDERSTANDING_HINTS = (
    "总结",
    "总结下",
    "概括",
    "提炼",
    "整体思路",
    "整体框架",
    "整体结构",
    "整体逻辑",
    "总体思路",
    "总体框架",
    "总体结构",
    "总览",
    "整理",
    "重整",
    "重排",
    "表格化",
    "讲讲",
    "讲一下",
    "讲下",
    "解读",
    "解释",
    "说明",
    "分析",
    "梳理",
    "翻译",
    "转录",
    "抄录",
    "识别",
    "ocr",
    "原文",
    "可见文字",
    "图里",
    "图片里",
    "截图里",
    "看到了什么",
    "看到什么",
    "写了什么",
    "内容是什么",
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

_MEETING_HINTS = (
    "会议",
    "例会",
    "周会",
    "晨会",
    "复盘会",
    "评审会",
    "讨论会",
    "meeting",
    "standup",
    "retro",
    "sync",
    "kickoff",
    "1:1",
)

_MEETING_MINUTES_ACTION_HINTS = (
    "会议纪要",
    "会议记录",
    "会议摘要",
    "会议要点",
    "会后纪要",
    "整理纪要",
    "整理成纪要",
    "整理会议",
    "提炼会议",
    "action item",
    "action items",
    "meeting minutes",
    "meeting notes",
    "minutes",
    "记录要点",
    "待办项",
    "决议",
    "下一步",
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

_INITIAL_CONTENT_TRIAGE_HINTS = (
    "能理解吗",
    "能看懂吗",
    "看得懂吗",
    "能看明白吗",
    "帮我看下",
    "帮我看看",
    "看下下面",
    "看一下下面",
    "帮我读一下",
    "先看看",
    "先看下",
    "can you understand",
    "can you read",
    "can you make sense",
)

@dataclass
class ExecutionState:
    task_type: str = "standard"
    complexity: str = "medium"
    tool_mode: str = "auto"  # off | auto | on | forced
    tool_latch: bool = False
    attempts: int = 0
    max_attempts: int = 24
    status: str = "initialized"
    transitions: list[str] = dc_field(default_factory=list)


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
    start_line: int = Field(default=0, ge=0)
    max_lines: int = Field(default=0, ge=0, le=200000)


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
    def __init__(self, config: AppConfig, *, kernel_runtime: KernelRuntime | None = None) -> None:
        self.config = config
        self.tools = LocalToolExecutor(config)
        self._auth_manager = OpenAIAuthManager(config)
        self._kernel_runtime = kernel_runtime or build_kernel_runtime(config)

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
        tool_registry = getattr(self._kernel_runtime.registry, "tool_registry", None)
        tool_registry_ref = str((self._kernel_runtime.registry.selected_refs or {}).get("tool_registry") or "")
        fallback_tool_registry_ref = "tool_registry@1.0.0"
        if tool_registry is not None and hasattr(tool_registry, "build_langchain_tools"):
            try:
                self._lc_tools = list(tool_registry.build_langchain_tools(agent=self))
                self._record_module_success(
                    kind="tool_registry",
                    selected_ref=tool_registry_ref or fallback_tool_registry_ref,
                )
            except Exception as exc:
                self._record_module_failure(
                    kind="tool_registry",
                    requested_ref=tool_registry_ref or fallback_tool_registry_ref,
                    fallback_ref=fallback_tool_registry_ref,
                    error=str(exc),
                )
                self._lc_tools = self._build_langchain_tools()
        else:
            self._lc_tools = self._build_langchain_tools()
        self._lc_tool_map = {getattr(tool, "name", ""): tool for tool in self._lc_tools}
        self._model_failover_lock = threading.Lock()
        self._model_failover_state: dict[str, dict[str, int | float]] = {}

    def _debug_openai_auth_summary(self) -> dict[str, Any]:
        return self._auth_manager.auth_summary()

    def _debug_codex_input_payload(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        built_messages: list[Any] = []
        for item in messages:
            role = str(item.get("role") or "").strip().lower()
            content = item.get("content") or ""
            if role == "system":
                built_messages.append(self._SystemMessage(content=content))
                continue
            if role == "user":
                built_messages.append(self._HumanMessage(content=content))
                continue
            if role == "assistant":
                built_messages.append(
                    self._AIMessage(
                        content=content,
                        tool_calls=item.get("tool_calls") or [],
                    )
                )
                continue
            if role == "tool":
                built_messages.append(
                    self._ToolMessage(
                        content=content,
                        tool_call_id=str(item.get("tool_call_id") or "call_missing"),
                        name=str(item.get("name") or ""),
                    )
                )
        instructions, input_items = build_codex_input_payload(built_messages)
        return {"instructions": instructions, "input": input_items}

    def _debug_normalize_model_for_auth(self, model: str, auth_mode: str) -> dict[str, Any]:
        return {"normalized_model": normalize_model_for_auth_mode(model, auth_mode)}

    def _debug_kernel_module_snapshot(self) -> dict[str, Any]:
        snapshot = self._kernel_runtime.health_snapshot()
        return {
            "active_manifest": dict(snapshot.active_manifest),
            "selected_modules": dict(snapshot.selected_modules),
            "module_health": dict(snapshot.module_health),
            "runtime_files": dict(snapshot.runtime_files),
        }

    def _debug_tool_registry_snapshot(self) -> dict[str, Any]:
        registry = self._module_registry()
        module = getattr(registry, "tool_registry", None)
        selected_ref = str((registry.selected_refs or {}).get("tool_registry") or "")
        if module is None or not hasattr(module, "describe_tools"):
            return {
                "selected_ref": selected_ref,
                "tool_count": len(self._lc_tools),
                "tools": [
                    {"name": str(getattr(tool, "name", "") or ""), "description": str(getattr(tool, "description", "") or "")[:200]}
                    for tool in self._lc_tools
                ],
            }
        payload = module.describe_tools(agent=self)
        if isinstance(payload, dict):
            payload.setdefault("selected_ref", selected_ref)
            return payload
        return {"selected_ref": selected_ref, "tool_count": len(self._lc_tools)}

    def _debug_kernel_shadow_upgrade_flow(self, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            shadow = runtime.load_shadow_manifest()
            shadow.router = str(target_router_ref or shadow.router)
            runtime.write_shadow_manifest(shadow)
            validation = runtime.validate_shadow_manifest()
            promotion = runtime.promote_shadow_manifest()
            active_after = runtime.supervisor.load_active_manifest().to_dict()
            rollback = runtime.rollback_active_manifest()
            active_restored = runtime.supervisor.load_active_manifest().to_dict()
            return {
                "validation": validation,
                "promotion": promotion,
                "rollback": rollback,
                "active_after": active_after,
                "active_restored": active_restored,
            }

    def _debug_kernel_shadow_validation_rejects_broken_manifest(
        self,
        broken_router_ref: str = "router_rules@999.0.0",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-bad-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            initial_active = runtime.supervisor.load_active_manifest().to_dict()
            shadow = runtime.load_shadow_manifest()
            shadow.router = str(broken_router_ref or shadow.router)
            runtime.write_shadow_manifest(shadow)
            validation = runtime.validate_shadow_manifest()
            promotion = runtime.promote_shadow_manifest()
            active_after_attempt = runtime.supervisor.load_active_manifest().to_dict()
            return {
                "initial_active": initial_active,
                "validation": validation,
                "promotion": promotion,
                "active_after_attempt": active_after_attempt,
            }

    def _debug_kernel_shadow_stage_and_smoke(
        self,
        target_router_ref: str = "router_rules@2.0.0",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-smoke-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            stage = runtime.stage_shadow_manifest(overrides={"router": str(target_router_ref)})
            smoke = runtime.run_shadow_smoke(
                user_message="给我今天的新闻",
                validate_provider=False,
            )
            return {
                "stage": stage,
                "smoke": smoke,
            }

    def _debug_kernel_shadow_replay(self, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-replay-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            runtime.stage_shadow_manifest(overrides={"router": str(target_router_ref)})
            replay_record = {
                "run_id": "synthetic-replay",
                "session_id": "synthetic-session",
                "message": "把数据整理成表格",
                "settings": {"enable_tools": True, "response_style": "short"},
                "summary_before": "",
                "history_turns_before": [],
                "attachment_metas": [],
                "route_state_input": {},
            }
            replay = runtime.run_shadow_replay(replay_record=replay_record)
            return {"replay": replay}

    def _debug_kernel_shadow_contracts(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-contracts-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            contracts = runtime.run_shadow_contracts()
            return {"contracts": contracts}

    def _debug_kernel_shadow_pipeline(self, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-pipeline-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            pipeline = runtime.run_shadow_pipeline(
                overrides={"router": str(target_router_ref)},
                smoke_message="给我今天的新闻",
                validate_provider=False,
                replay_record={
                    "run_id": "synthetic-pipeline",
                    "session_id": "synthetic-session",
                    "message": "给我今天的新闻",
                    "settings": {"enable_tools": True, "response_style": "short"},
                    "summary_before": "",
                    "history_turns_before": [],
                    "attachment_metas": [],
                    "route_state_input": {},
                },
                promote_if_healthy=True,
            )
            rollback = runtime.rollback_active_manifest()
            last_upgrade_run = runtime.read_last_upgrade_run()
            upgrade_runs = runtime.list_upgrade_runs(limit=5)
            return {
                "pipeline": pipeline,
                "stage": dict(pipeline.get("stage") or {}),
                "validation": dict(pipeline.get("validation") or {}),
                "smoke": dict(pipeline.get("smoke") or {}),
                "replay": dict(pipeline.get("replay") or {}),
                "promotion": dict(pipeline.get("promotion") or {}),
                "rollback": rollback,
                "last_upgrade_run": last_upgrade_run,
                "upgrade_runs": upgrade_runs,
            }

    def _debug_kernel_shadow_pipeline_classifies_broken_manifest(
        self,
        broken_router_ref: str = "router_rules@999.0.0",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-pipeline-bad-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            pipeline = runtime.run_shadow_pipeline(
                overrides={"router": str(broken_router_ref)},
                smoke_message="给我今天的新闻",
                validate_provider=False,
                replay_record=None,
                promote_if_healthy=False,
            )
            return {
                "pipeline": pipeline,
                "last_upgrade_run": runtime.read_last_upgrade_run(),
                "upgrade_runs": runtime.list_upgrade_runs(limit=5),
            }

    def _debug_kernel_shadow_auto_repair_broken_manifest(
        self,
        broken_router_ref: str = "router_rules@999.0.0",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-repair-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            broken_pipeline = runtime.run_shadow_pipeline(
                overrides={"router": str(broken_router_ref)},
                smoke_message="给我今天的新闻",
                validate_provider=False,
                replay_record=None,
                promote_if_healthy=False,
            )
            repair = runtime.run_shadow_auto_repair(
                base_upgrade_run=broken_pipeline,
                replay_record=None,
                smoke_message="给我今天的新闻",
                validate_provider=False,
                promote_if_healthy=False,
                max_attempts=2,
            )
            return {
                "broken_pipeline": broken_pipeline,
                "repair": repair,
                "last_repair_run": runtime.read_last_repair_run(),
                "repair_runs": runtime.list_repair_runs(limit=5),
                "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
            }

    def _debug_kernel_shadow_promote_rejects_path_refs(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-promote-path-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            source_dir = cfg.modules_dir / "router_rules" / "v1"
            path_module_dir = runtime_dir / "shadow_router_path"
            shutil.copytree(source_dir, path_module_dir)
            stage = runtime.stage_shadow_manifest(overrides={"router": f"path:{path_module_dir}"})
            promote_check = runtime.shadow_promote_check()
            promotion = runtime.promote_shadow_manifest()
            return {
                "stage": stage,
                "promote_check": promote_check,
                "promotion": promotion,
                "shadow_manifest": runtime.load_shadow_manifest().to_dict(),
                "active_manifest": runtime.supervisor.load_active_manifest().to_dict(),
            }

    def _debug_kernel_shadow_patch_worker_persists_missing_tasks(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-patch-empty-") as tmp_dir:
            runtime_dir = Path(tmp_dir).resolve()
            cfg = replace(
                self.config,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            patch_worker = runtime.run_shadow_patch_worker(
                repair_run={"run_id": "synthetic-repair", "repair_tasks": []},
                replay_record=None,
                max_tasks=1,
                max_rounds=3,
                promote_if_healthy=False,
            )
            return {
                "patch_worker": patch_worker,
                "last_patch_worker_run": runtime.read_last_patch_worker_run(),
                "patch_worker_runs": runtime.list_patch_worker_runs(limit=5),
            }

    def _debug_kernel_shadow_package_path_router(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-package-") as tmp_dir:
            root = Path(tmp_dir).resolve()
            runtime_dir = root / "runtime"
            modules_dir = root / "modules"
            shutil.copytree(self.config.modules_dir, modules_dir)
            cfg = replace(
                self.config,
                modules_dir=modules_dir,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            source_router_dir = modules_dir / "router_rules" / "v1"
            stage = runtime.stage_shadow_manifest(overrides={"router": f"path:{source_router_dir}"})
            package_run = runtime.package_shadow_modules(
                labels=["router"],
                package_note="debug package path router",
                source_run_id="synthetic-upgrade",
                patch_worker_run_id="synthetic-patch",
                runtime_profile="patch_worker",
            )
            return {
                "stage": stage,
                "package_run": package_run,
                "last_package_run": runtime.read_last_package_run(),
                "package_runs": runtime.list_package_runs(limit=5),
                "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
            }

    def _debug_role_contract_matrix(self) -> dict[str, Any]:
        route = {
            "task_type": "attachment_tooling",
            "primary_intent": "understanding",
            "execution_policy": "attachment_tooling",
            "runtime_profile": default_runtime_profile_for_route(
                {
                    "task_type": "attachment_tooling",
                    "primary_intent": "understanding",
                    "execution_policy": "attachment_tooling",
                }
            ),
        }
        role_payloads = {
            "planner": {
                "objective": "整理文档重点",
                "constraints": ["不要输出思维链"],
                "plan": ["先看附件摘要", "再给解释"],
                "watchouts": ["不要误判成取证链"],
                "success_signals": ["回答清楚整体思路"],
            },
            "reviewer": {
                "verdict": "pass",
                "confidence": "medium",
                "summary": "结构完整。",
                "strengths": ["覆盖了主要问题"],
                "risks": [],
                "followups": [],
            },
            "revision": {
                "changed": True,
                "summary": "已按 reviewer 调整措辞。",
                "key_changes": ["补充了限制说明"],
                "final_answer": "这是修订后的答复。",
            },
            "conflict_detector": {
                "has_conflict": False,
                "confidence": "medium",
                "summary": "未发现明显冲突。",
                "concerns": [],
                "suggested_checks": [],
            },
        }
        specialist_payloads = {
            role: self._specialist_fallback(
                specialist=role,
                requested_model=self.config.default_model,
                attachment_metas=[],
                initial_triage_request=False,
            )
            for role in ("researcher", "file_reader", "summarizer", "fixer")
        }
        roles: list[dict[str, Any]] = []
        for role, payload in {**role_payloads, **specialist_payloads}.items():
            output_keys_map = {
                "planner": ["objective", "constraints", "plan", "watchouts", "success_signals"],
                "reviewer": ["verdict", "confidence", "summary", "strengths", "risks", "followups"],
                "revision": ["changed", "summary", "key_changes", "final_answer"],
                "conflict_detector": ["has_conflict", "confidence", "summary", "concerns", "suggested_checks"],
                "researcher": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
                "file_reader": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
                "summarizer": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
                "fixer": ["summary", "bullets", "worker_hint", "queries", "scope", "stop_rules"],
            }
            result = self._make_default_role_result(
                role,
                payload=payload,
                requested_model=self.config.default_model,
                user_message="解释一下文档整体思路",
                history_summary="上一轮已经读了附件摘要。",
                route=route,
                description=f"{role} contract test",
                output_keys=output_keys_map.get(role, []),
            )
            validation = validate_role_result_helper(result)
            roles.append(validation)
        profiles = [
            validate_runtime_profile_helper(runtime_profile_spec("explainer")),
            validate_runtime_profile_helper(runtime_profile_spec("evidence")),
            validate_runtime_profile_helper(PATCH_WORKER_PROFILE),
        ]
        return {
            "ok": all(bool(item.get("ok")) for item in roles) and all(bool(item.get("ok")) for item in profiles),
            "roles": roles,
            "profiles": profiles,
        }

    def _debug_kernel_shadow_promote_rejects_dependency_mismatch(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-dependency-") as tmp_dir:
            root = Path(tmp_dir).resolve()
            runtime_dir = root / "runtime"
            modules_dir = root / "modules"
            shutil.copytree(self.config.modules_dir, modules_dir)
            cfg = replace(
                self.config,
                modules_dir=modules_dir,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            source_router_dir = modules_dir / "router_rules" / "v1"
            runtime.stage_shadow_manifest(overrides={"router": f"path:{source_router_dir}"})
            package_run = runtime.package_shadow_modules(labels=["router"], runtime_profile="patch_worker")
            packaged_ref = str(((package_run.get("packaged_modules") or [{}])[0] or {}).get("packaged_ref") or "")
            packaged_manifest_path = modules_dir / "router_rules" / "v3" / "manifest.toml"
            packaged_manifest = read_module_manifest(packaged_manifest_path)
            broken_manifest = type(packaged_manifest)(
                id=packaged_manifest.id,
                version=packaged_manifest.version,
                api_version=packaged_manifest.api_version,
                kind=packaged_manifest.kind,
                entrypoint=packaged_manifest.entrypoint,
                capabilities=packaged_manifest.capabilities,
                depends_on=("policy=policy_resolver@999.0.0",),
                runtime_profile=packaged_manifest.runtime_profile,
                source_ref=packaged_manifest.source_ref,
                packaged_at=packaged_manifest.packaged_at,
                path=packaged_manifest.path,
            )
            write_module_manifest(packaged_manifest_path, broken_manifest)
            runtime.stage_shadow_manifest(overrides={"router": packaged_ref})
            promote_check = runtime.shadow_promote_check()
            promotion = runtime.promote_shadow_manifest()
            return {
                "package_run": package_run,
                "promote_check": promote_check,
                "promotion": promotion,
                "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
            }

    def _debug_kernel_shadow_self_upgrade_flow(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="officetool-kernel-self-upgrade-") as tmp_dir:
            root = Path(tmp_dir).resolve()
            runtime_dir = root / "runtime"
            modules_dir = root / "modules"
            shutil.copytree(self.config.modules_dir, modules_dir)
            cfg = replace(
                self.config,
                modules_dir=modules_dir,
                runtime_dir=runtime_dir,
                active_manifest_path=runtime_dir / "active_manifest.json",
                shadow_manifest_path=runtime_dir / "shadow_manifest.json",
                rollback_pointer_path=runtime_dir / "rollback_pointer.json",
                module_health_path=runtime_dir / "module_health.json",
            )
            runtime = build_kernel_runtime(cfg)
            source_router_dir = modules_dir / "router_rules" / "v1"
            base_pipeline = runtime.run_shadow_pipeline(
                overrides={"router": f"path:{source_router_dir}"},
                smoke_message="给我今天的新闻",
                validate_provider=False,
                replay_record=None,
                promote_if_healthy=True,
            )
            self_upgrade = runtime.run_shadow_self_upgrade(
                base_upgrade_run=base_pipeline,
                replay_record=None,
                smoke_message="给我今天的新闻",
                validate_provider=False,
                max_attempts=1,
                max_tasks=1,
                max_rounds=2,
                promote_if_healthy=True,
            )
            return {
                "base_pipeline": base_pipeline,
                "self_upgrade": self_upgrade,
                "active_manifest_after": runtime.supervisor.load_active_manifest().to_dict(),
                "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
                "last_package_run": runtime.read_last_package_run(),
                "last_patch_worker_run": runtime.read_last_patch_worker_run(),
            }

    def _module_registry(self):
        return self._kernel_runtime.registry

    def _record_module_failure(
        self,
        *,
        kind: str,
        requested_ref: str,
        fallback_ref: str = "",
        error: str,
        mode: str | None = None,
    ) -> None:
        self._kernel_runtime.record_module_failure(
            kind=kind,
            requested_ref=requested_ref,
            fallback_ref=fallback_ref,
            error=error,
            mode=mode,
        )

    def _record_module_success(self, *, kind: str, selected_ref: str, mode: str | None = None) -> None:
        self._kernel_runtime.record_module_success(kind=kind, selected_ref=selected_ref, mode=mode)

    def _ensure_openai_ca_env(self, ca_cert_path: str) -> None:
        os.environ.setdefault("SSL_CERT_FILE", ca_cert_path)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_cert_path)

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
        route_state: dict[str, Any] | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[
        str,
        list[ToolEvent],
        str,
        list[str],
        list[str],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
        str | None,
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, int],
        str,
        dict[str, Any],
    ]:
        requested_model = settings.model or self.config.default_model
        effective_model = requested_model
        style_hint = _STYLE_HINTS.get(settings.response_style, _STYLE_HINTS["normal"])
        execution_plan: list[str] = []
        execution_trace: list[str] = []
        pipeline_hook_telemetry: list[dict[str, Any]] = []
        debug_flow: list[dict[str, Any]] = []
        agent_panels: list[dict[str, Any]] = []
        active_roles: set[str] = set()
        current_role: str | None = None
        role_states: dict[str, dict[str, Any]] = {}
        worker_citation_candidates: list[dict[str, Any]] = []
        answer_bundle: dict[str, Any] = {"summary": "", "claims": [], "citations": [], "warnings": []}
        usage_total = self._empty_usage()
        run_state: RunState | None = None
        route: dict[str, Any] = {}
        role_node_seq: dict[str, int] = {}
        role_instance_seq: dict[str, int] = {}
        coordinator_node_id = ""
        coordinator_instance_id = ""
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
            emit_progress(
                "agent_state",
                panels=list(agent_panels),
                execution_plan=list(execution_plan),
                active_roles=sorted(active_roles),
                current_role=current_role,
                role_states=list(role_states.values()),
            )

        def _normalize_role_status(value: str) -> str:
            status = str(value or "").strip().lower()
            if status in {"idle", "seen", "active", "current"}:
                return status
            return "seen"

        def _upsert_role_state(
            role: str,
            *,
            status: str | None = None,
            phase: str | None = None,
            detail: str | None = None,
        ) -> None:
            role_key = str(role or "").strip().lower()
            if not role_key:
                return
            payload = dict(role_states.get(role_key) or {"role": role_key, "status": "seen", "phase": "", "detail": ""})
            if status is not None:
                payload["status"] = _normalize_role_status(status)
            if phase is not None:
                payload["phase"] = self._shorten(str(phase or "").strip(), 40)
            if detail is not None:
                payload["detail"] = self._shorten(str(detail or "").strip(), 90)
            role_states[role_key] = payload

        def set_role_activity(
            *roles: str,
            current: str | None = None,
            phase: str | None = None,
            detail: str | None = None,
        ) -> None:
            nonlocal current_role
            normalized = {str(role or "").strip().lower() for role in roles if str(role or "").strip()}
            previous_active = set(active_roles)
            active_roles.clear()
            active_roles.update(normalized)
            current_role = str(current or "").strip().lower() or (next(iter(sorted(active_roles))) if active_roles else None)
            for role_key in previous_active | set(role_states.keys()):
                if role_key not in active_roles:
                    _upsert_role_state(role_key, status="seen")
            for role_key in active_roles:
                is_current = role_key == current_role
                role_phase = phase if is_current else ("协调" if role_key == "coordinator" else "协同")
                role_detail = detail if is_current else ""
                _upsert_role_state(
                    role_key,
                    status="current" if is_current else "active",
                    phase=role_phase,
                    detail=role_detail,
                )
            emit_agent_state()

        def clear_role_activity(*, final_status: str | None = None, summary_text: str = "") -> None:
            nonlocal current_role
            for role_key in list(active_roles):
                _upsert_role_state(role_key, status="seen")
            active_roles.clear()
            current_role = None
            if run_state is not None and run_state.ended_at <= 0 and final_status:
                normalized = str(final_status or "").strip().lower()
                if normalized not in {"completed", "failed", "cancelled"}:
                    normalized = "completed"
                complete_role_instance(
                    coordinator_instance_id,
                    summary_text=summary_text or f"final_status={normalized}",
                )
                run_state.finish(status=normalized)  # type: ignore[arg-type]
                add_trace(
                    "Runtime: "
                    + self._shorten(
                        json.dumps(run_state.snapshot_compact(), ensure_ascii=False),
                        280,
                    )
                )
            emit_agent_state()

        def add_panel(role: str, title: str, summary_text: str, bullets: list[str] | None = None) -> None:
            role_key = str(role or "").strip().lower()
            panel = AgentPanel(
                role=role_key,
                title=title,
                kind=str(_ROLE_KINDS.get(role_key, "agent")),
                summary=self._shorten(summary_text, 500 if not debug_raw else 4000),
                bullets=self._normalize_string_list(bullets or [], limit=8, item_limit=220),
            )
            payload = panel.model_dump()
            _upsert_role_state(role_key, status=role_states.get(role_key, {}).get("status", "seen"))
            for idx, existing in enumerate(agent_panels):
                if str(existing.get("role") or "") == role_key:
                    agent_panels[idx] = payload
                    emit_agent_state()
                    return
            agent_panels.append(payload)
            emit_agent_state()

        def record_pipeline_hook(
            *,
            phase: str,
            hook_payload: dict[str, Any],
            route_before: dict[str, Any] | None = None,
            route_after: dict[str, Any] | None = None,
        ) -> None:
            item = build_pipeline_hook_telemetry(
                phase=phase,
                handler_name=str(PIPELINE_HOOK_HANDLERS.get(str(phase or "").strip()) or ""),
                hook_payload=hook_payload,
                route_before=route_before,
                route_after=route_after,
            )
            pipeline_hook_telemetry.append(item)
            add_run_event("pipeline_hook", **item)
            summary_text, bullets = build_pipeline_hook_panel_payload(pipeline_hook_telemetry)
            add_panel("pipeline_hooks", "Pipeline Hooks", summary_text, bullets)

        def begin_role_instance(
            role: str,
            *,
            parent_node_id: str | None = None,
            phase: str = "",
            tool_mode: str = "",
            meta: dict[str, Any] | None = None,
        ) -> tuple[str, str]:
            nonlocal run_state
            if run_state is None:
                return "", ""
            role_key = str(role or "").strip().lower()
            if not role_key:
                return "", ""
            role_node_seq[role_key] = role_node_seq.get(role_key, 0) + 1
            role_instance_seq[role_key] = role_instance_seq.get(role_key, 0) + 1
            node_id = f"{role_key}:{role_node_seq[role_key]}"
            parent_id = parent_node_id or run_state.root_node_id
            run_state.add_node(
                node_id=node_id,
                role=role_key,
                role_kind=str(_ROLE_KINDS.get(role_key, "agent")),  # type: ignore[arg-type]
                parent_node_id=parent_id,
                phase=phase,
                meta=meta or {},
            )
            instance_id = f"{role_key}#{role_instance_seq[role_key]}"
            run_state.start_instance(
                instance_id=instance_id,
                role=role_key,
                node_id=node_id,
                sequence=role_instance_seq[role_key],
                tool_mode=tool_mode,
                meta=meta or {},
            )
            return node_id, instance_id

        def complete_role_instance(instance_id: str, *, summary_text: str = "") -> None:
            nonlocal run_state
            if run_state is None or not instance_id:
                return
            run_state.complete_instance(instance_id, summary=summary_text)

        def fail_role_instance(instance_id: str, *, error_text: str = "") -> None:
            nonlocal run_state
            if run_state is None or not instance_id:
                return
            run_state.fail_instance(instance_id, error=error_text)

        def add_run_event(kind: str, **payload: Any) -> None:
            nonlocal run_state
            if run_state is None:
                return
            run_state.add_event(kind, **payload)

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
                    "如果用户要求找函数/找文件/搜代码但没有明确给路径，"
                    "默认先在当前工作区根目录 '.' 调用 search_codebase 或 list_directory，不要先索取具体路径；"
                    "只有在你至少完成一次默认搜索后仍无法缩小范围时，才向用户追问路径。\n"
                    "如果用户给的是相对目录名或允许根目录的别名（例如 workbench），"
                    "可以直接把该名字作为 list_directory(path=...) 或 search_codebase(root=...) 的参数尝试；"
                    "不要先要求绝对路径。\n"
                    "如果用户只给了文件关键词（例如 tcg_accl0030）且未带扩展名，"
                    "默认先按 basename 进行模糊搜索（文件名/内容都可），不要先追问完整文件名或扩展名。\n"
                    "当默认 root='.' 没有命中时，继续在其他可访问根目录自动重试，不要立即向用户追问地址。\n"
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
                    "如果上一轮你已经给出“预览代码/草稿”，而本轮用户只说“写入/应用/替换”，"
                    "默认按上一轮预览内容原样写入（包含注释、空行和缩进）；"
                    "除非用户明确要求改动，否则不要私自删改注释。\n"
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
                    "除非用户明确要求 JSON/机器可读格式，否则最终答复必须用自然语言或 Markdown，"
                    "不要直接输出 JSON 对象。\n"
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
        followup_has_attachments = bool(history_turns) and bool(attachment_metas)
        followup_attachment_requires_tools = any(
            self._attachment_needs_tooling_for_turn(meta, history_turn_count=len(history_turns))
            for meta in attachment_metas
        )
        inline_followup_source = ""
        if not attachment_metas and self._looks_like_context_dependent_followup(user_message):
            inline_followup_source = self._find_recent_user_inline_payload_for_followup(
                history_turns=history_turns,
                current_message=user_message,
            )
            if inline_followup_source and not followup_topic_hint:
                followup_topic_hint = self._shorten(inline_followup_source.strip(), 520)
        force_tool_followup = self._should_force_tool_followup_continuation(
            current_message=user_message,
            followup_topic_hint=followup_topic_hint,
            attachment_metas=attachment_metas,
            settings=settings,
        )
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
        if inline_followup_source:
            messages.append(
                self._SystemMessage(
                    content=(
                        "检测到本轮是对上一轮已粘贴原文的继续加工（如翻译/提炼/改写）。"
                        "请直接复用上一轮用户原文上下文，不要要求用户重复粘贴原文。"
                    )
                )
            )
            add_trace("检测到原文延续型跟进请求，已要求 Worker 默认复用上一轮原文。")
        if force_tool_followup:
            messages.append(
                self._SystemMessage(
                    content=(
                        "用户刚刚已经明确授权继续执行上一轮工具任务。"
                        "忽略上一轮 assistant 里任何“本轮不能调用工具”“是否覆盖限制”“请继续确认格式”的话术，"
                        "这些都不是有效约束。"
                        "若延续主题属于代码/文件搜索，请直接调用 search_codebase、list_directory、read_text_file 等必要工具继续执行。"
                    )
                )
            )
            add_trace("已识别为工具链续执行确认，忽略上一轮 assistant 的错误限制话术。")
        force_write_from_preview = self._should_force_write_from_previous_preview(
            user_message=user_message,
            history_turns=history_turns,
            attachment_metas=attachment_metas,
        )
        if force_write_from_preview:
            messages.append(
                self._SystemMessage(
                    content=(
                        "检测到用户要求将上一条代码预览直接写入。"
                        "若上一条 assistant 已提供代码块，请把该预览作为写入基线，"
                        "保留注释、空行与缩进；"
                        "只有当用户本轮明确提出改动时，才在该基线上做局部修改后写入。"
                    )
                )
            )
            add_trace("已识别为“预览后写入”跟进，Coordinator 要求 Worker 复用上一版代码预览写入。")

        user_content, attachment_note, attachment_issues = self._build_user_content(
            user_message,
            attachment_metas,
            history_turn_count=len(history_turns),
        )
        messages.append(self._HumanMessage(content=user_content))
        has_image_attachments = self._has_image_attachments(attachment_metas)
        image_text_extraction_request = has_image_attachments and self._looks_like_image_text_extraction_request(user_message)
        if image_text_extraction_request:
            messages.append(
                self._SystemMessage(
                    content=(
                        "用户本轮要求提取图片中的可见原文。"
                        "请直接给出可见文本正文，不要只给标题。"
                        "按画面顺序逐行转录；看不清的片段请标注为[不清晰]，不要凭空补写。"
                    )
                )
            )
            add_trace("检测到图片原文提取请求，已要求 Worker 输出完整可见文本，不只返回标题。")
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
            user_message=planner_user_message,
            summary=summary,
            attachment_metas=attachment_metas,
            settings=settings,
            route_state=route_state,
            inline_followup_context=bool(inline_followup_source),
        )
        route_before_hook = dict(route)
        route_finalize_hook = self._run_pipeline_hook(
            "before_route_finalize",
            route=route,
            router_raw=router_raw,
            planner_user_message=planner_user_message,
            attachment_issues=attachment_issues,
            followup_has_attachments=followup_has_attachments,
            followup_attachment_requires_tools=followup_attachment_requires_tools,
            attachment_metas=attachment_metas,
            settings=settings,
        )
        route = route_finalize_hook["route"]
        router_raw = str(route_finalize_hook["router_raw"] or "")
        record_pipeline_hook(
            phase="before_route_finalize",
            hook_payload=route_finalize_hook,
            route_before=route_before_hook,
            route_after=route,
        )
        self._apply_pipeline_hook_effects(
            hook_payload=route_finalize_hook,
            add_trace=add_trace,
            add_debug=add_debug,
        )
        execution_state = self._coordinator_init_state(
            route=route,
            settings=settings,
            force_tool_followup=force_tool_followup,
        )
        route_before_hook = dict(route)
        worker_prompt_hook = self._run_pipeline_hook(
            "before_worker_prompt",
            route=route,
            router_raw=router_raw,
            execution_state=execution_state,
            planner_user_message=planner_user_message,
            attachment_metas=attachment_metas,
            settings=settings,
            force_tool_followup=force_tool_followup,
        )
        route = worker_prompt_hook["route"]
        router_raw = str(worker_prompt_hook["router_raw"] or "")
        execution_state = worker_prompt_hook["execution_state"]
        spec_lookup_request = bool(worker_prompt_hook["spec_lookup_request"])
        evidence_required_mode = bool(worker_prompt_hook["evidence_required_mode"])
        record_pipeline_hook(
            phase="before_worker_prompt",
            hook_payload=worker_prompt_hook,
            route_before=route_before_hook,
            route_after=route,
        )
        self._apply_pipeline_hook_effects(
            hook_payload=worker_prompt_hook,
            messages=messages,
            add_trace=add_trace,
            add_debug=add_debug,
        )
        run_state = RunState.create(
            run_id=f"run_{int(time.time() * 1000)}_{os.getpid()}",
            session_id=str(session_id or ""),
            task_type=str(route.get("task_type") or "standard"),
            root_role="router",
            root_role_kind="hybrid",
            meta={
                "requested_model": requested_model,
                "execution_mode": requested_execution_mode,
            },
        )
        add_run_event(
            "route_selected",
            task_type=str(route.get("task_type") or "standard"),
            complexity=str(route.get("complexity") or "medium"),
            source=str(route.get("source") or "rules"),
            use_worker_tools=bool(route.get("use_worker_tools")),
        )
        router_node_id, router_instance_id = begin_role_instance(
            "router",
            parent_node_id=run_state.root_node_id,
            phase="route",
            meta={"source": str(route.get("source") or "rules")},
        )
        complete_role_instance(
            router_instance_id,
            summary_text=(
                f"task_type={str(route.get('task_type') or 'standard')}, "
                f"complexity={str(route.get('complexity') or 'medium')}"
            ),
        )
        coordinator_node_id, coordinator_instance_id = begin_role_instance(
            "coordinator",
            parent_node_id=router_node_id or run_state.root_node_id,
            phase="orchestrate",
            tool_mode=execution_state.tool_mode,
            meta={"tool_mode": execution_state.tool_mode},
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
            stage="llm_to_backend" if route.get("source") == "llm_router" else "backend_router",
            title="Router 判定 -> Coordinator" if route.get("source") == "llm_router" else "规则 Router 判定 -> Coordinator",
            detail=(
                f"route={json.dumps(route, ensure_ascii=False)}\n"
                f"raw={self._shorten(router_raw, 2400 if not debug_raw else 120000)}"
            ),
        )
        add_debug(
            stage="backend_coordinator",
            title="Coordinator 初始化",
            detail=(
                f"task_type={execution_state.task_type}\n"
                f"complexity={execution_state.complexity}\n"
                f"tool_mode={execution_state.tool_mode}\n"
                f"tool_latch={str(execution_state.tool_latch).lower()}\n"
                f"transitions={json.dumps(execution_state.transitions, ensure_ascii=False)}"
            ),
        )
        add_panel(
            "router",
            "Router",
            str(route.get("summary") or "已完成链路分诊。").strip() or "已完成链路分诊。",
            self._format_router_panel_bullets(route),
        )
        add_panel(
            "coordinator",
            "Coordinator",
            self._coordinator_summary(execution_state),
            self._coordinator_panel_bullets(execution_state),
        )
        set_role_activity(
            "router",
            "coordinator",
            current="router",
            phase="分诊",
            detail=f"task_type={route.get('task_type') or 'standard'}",
        )
        planner_result = self._make_default_role_result(
            "planner",
            requested_model=effective_model,
            user_message=planner_user_message,
            history_summary=summary,
            attachment_metas=attachment_metas,
            extra={
                "response_style": settings.response_style,
                "enable_tools": settings.enable_tools,
                "max_context_turns": settings.max_context_turns,
            },
            description="提炼目标、约束和执行计划。",
            output_keys=["objective", "constraints", "plan", "watchouts", "success_signals"],
            payload={
                "objective": self._shorten(planner_user_message.strip(), 220),
                "constraints": [],
                "plan": list(execution_plan),
                "watchouts": [],
                "success_signals": [],
                "usage": self._empty_usage(),
                "effective_model": effective_model,
                "notes": [],
            },
        )
        planner_raw = planner_result.raw_text
        if route.get("use_planner"):
            planner_node_id, planner_instance_id = begin_role_instance(
                "planner",
                parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
                phase="plan",
            )
            set_role_activity("coordinator", "planner", current="planner", phase="规划", detail="整理目标与执行计划")
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
                title="Coordinator -> Planner",
                detail=planner_request_detail,
            )
            planner_result = self._run_planner_role(context=planner_result.context, settings=settings)
            planner_raw = planner_result.raw_text
            planner_effective_model = str(planner_result.effective_model or "").strip()
            if planner_effective_model:
                effective_model = planner_effective_model
            usage_total = self._merge_usage(usage_total, planner_result.usage or self._empty_usage())
            for note in planner_result.notes:
                add_trace(note)
            add_trace("多 Role: Planner 已生成目标摘要与执行计划。")
            complete_role_instance(
                planner_instance_id,
                summary_text=str(planner_result.summary or "planner_ready"),
            )
            add_run_event(
                "planner_completed",
                node_id=planner_node_id,
                verdict="ok",
                model=planner_effective_model or requested_model,
            )
            add_debug(
                stage="llm_to_backend",
                title="Planner -> Coordinator",
                detail=(
                    f"effective_model={planner_effective_model or requested_model}\n"
                    f"{self._shorten(planner_raw, 4000 if debug_raw else 1200)}"
                ),
            )
            planner_plan = self._normalize_string_list(planner_result.payload.get("plan") or [], limit=8, item_limit=160)
            if planner_plan:
                execution_plan[:] = planner_plan
            planner_summary = planner_result.summary or "已生成目标摘要。"
            planner_bullets = (
                self._normalize_string_list(planner_result.payload.get("constraints") or [], limit=3, item_limit=180)
                + self._normalize_string_list(planner_result.payload.get("plan") or [], limit=4, item_limit=180)
            )
            add_panel("planner", "Planner", planner_summary, planner_bullets)
            planner_hook = self._run_pipeline_hook(
                "after_planner",
                planner_brief=planner_result,
            )
            record_pipeline_hook(
                phase="after_planner",
                hook_payload=planner_hook,
                route_before=route,
                route_after=route,
            )
            planner_execution_plan = planner_hook["execution_plan"]
            if planner_execution_plan:
                execution_plan[:] = planner_execution_plan
            self._apply_pipeline_hook_effects(
                hook_payload=planner_hook,
                messages=messages,
                add_trace=add_trace,
                add_debug=add_debug,
            )
        else:
            add_trace("Router 已跳过 Planner。")
            add_run_event("planner_skipped", reason="route_use_planner_false")

        specialist_prefetch_query = planner_user_message
        specialist_system_hints: list[str] = []
        for specialist in self._normalize_specialists(route.get("specialists") or []):
            specialist_label = _SPECIALIST_LABELS.get(specialist, specialist)
            specialist_node_id, specialist_instance_id = begin_role_instance(
                specialist,
                parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
                phase="specialist_brief",
            )
            set_role_activity(
                "coordinator",
                specialist,
                current=specialist,
                phase="专门分析",
                detail=f"{specialist_label} 正在生成简报",
            )
            add_debug(
                stage="backend_to_llm",
                title=f"Coordinator -> {specialist_label}",
                detail=(
                    f"model={self.config.summary_model or requested_model}\n"
                    f"task_type={route.get('task_type')}\n"
                    f"attachments={len(attachment_metas)}\n"
                    f"user_message_preview={self._shorten(planner_user_message, 400 if not debug_raw else 5000)}"
                ),
            )
            specialist_context = self._make_role_context(
                specialist,
                requested_model=requested_model,
                user_message=user_message,
                effective_user_message=planner_user_message,
                history_summary=summary,
                attachment_metas=attachment_metas,
                route=route,
                user_content=user_content,
            )
            specialist_result = self._run_specialist_with_context(context=specialist_context)
            specialist_brief = specialist_result.payload
            specialist_raw = specialist_result.raw_text
            specialist_model = str(specialist_result.effective_model or "").strip()
            usage_total = self._merge_usage(usage_total, specialist_result.usage or self._empty_usage())
            for note in specialist_result.notes:
                add_trace(note)
                add_trace(f"多 Role: {specialist_label} 已生成专门简报。")
            complete_role_instance(
                specialist_instance_id,
                summary_text=str(specialist_result.summary or f"{specialist_label} ready"),
            )
            add_run_event(
                "specialist_completed",
                role=specialist,
                node_id=specialist_node_id,
                model=specialist_model or self.config.summary_model or requested_model,
                bullet_count=len(specialist_result.payload.get("bullets") or []),
            )
            add_debug(
                stage="llm_to_backend",
                title=f"{specialist_label} -> Coordinator",
                detail=(
                    f"effective_model={specialist_model or self.config.summary_model or requested_model}\n"
                    f"{self._shorten(specialist_raw, 4000 if debug_raw else 1200)}"
                ),
            )
            add_panel(
                specialist,
                f"Specialist · {specialist_label}",
                specialist_result.summary or f"{specialist_label} 已生成简报。",
                self._normalize_string_list(specialist_result.payload.get("bullets") or [], limit=4, item_limit=180),
            )
            specialist_hint = self._format_specialist_system_hint(specialist, specialist_result)
            if specialist_hint:
                specialist_system_hints.append(specialist_hint)
            if specialist == "researcher":
                suggested_queries = self._normalize_string_list(specialist_result.payload.get("queries") or [], limit=3, item_limit=80)
                if suggested_queries:
                    specialist_prefetch_query = suggested_queries[0]
        for hint in reversed(specialist_system_hints):
            messages.insert(1, self._SystemMessage(content=hint))
        if specialist_system_hints:
            add_trace("多 Role: Coordinator 已将专门角色摘要注入 Worker 请求。")
            add_debug(
                stage="backend_coordinator",
                title="Coordinator 注入专门角色摘要",
                detail="\n\n".join(specialist_system_hints),
            )

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
                f"enable_tools={self._coordinator_tools_enabled(execution_state)}\n"
                f"tool_mode={execution_state.tool_mode}\n"
                f"attachments={len(attachment_metas)}\n"
                f"history_turns_used={min(len(history_turns), settings.max_context_turns)}"
            ),
        )
        add_debug(
            stage="backend_to_llm",
            title="Coordinator -> Worker（最终组包）",
            detail=(
                f"model={requested_model}, enable_tools={self._coordinator_tools_enabled(execution_state)}, max_output_tokens={settings.max_output_tokens}, "
                f"tool_mode={execution_state.tool_mode}, "
                f"debug_raw={debug_raw}, "
                f"history_turns_used={min(len(history_turns), settings.max_context_turns)}, "
                f"attachments={len(attachment_metas)}\n"
                f"message_roles={self._summarize_message_roles(messages)}\n"
                f"user_message_preview={self._shorten(user_message, 400 if not debug_raw else 20000)}\n"
                f"request_payload:\n{self._serialize_messages_for_debug(messages, raw_mode=debug_raw)}"
            ),
        )

        def invoke_worker_turn(
            *,
            title: str,
            model: str,
            current_runner: Any | None = None,
        ) -> tuple[Any, Any, str, list[str]]:
            if execution_state.attempts >= execution_state.max_attempts:
                execution_state.status = "attempt_limit_reached"
                execution_state.transitions.append("attempt_limit_reached")
                add_trace(f"已达到 Worker 最大尝试次数 {execution_state.max_attempts}，Coordinator 停止继续重试。")
                add_debug(
                    stage="backend_warning",
                    title="Coordinator 达到最大尝试次数",
                    detail=(
                        f"attempts={execution_state.attempts}\n"
                        f"max_attempts={execution_state.max_attempts}\n"
                        f"tool_mode={execution_state.tool_mode}"
                    ),
                )
                add_panel(
                    "coordinator",
                    "Coordinator",
                    self._coordinator_summary(execution_state),
                    self._coordinator_panel_bullets(execution_state),
                )
                raise RuntimeError(f"Worker exceeded max_attempts={execution_state.max_attempts}")
            execution_state.attempts += 1
            execution_state.status = "worker_running"
            execution_state.transitions.append(f"invoke:{execution_state.tool_mode}")
            worker_node_id, worker_instance_id = begin_role_instance(
                "worker",
                parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
                phase=f"attempt_{execution_state.attempts}",
                tool_mode=execution_state.tool_mode,
                meta={"attempt": execution_state.attempts},
            )
            set_role_activity(
                "coordinator",
                "worker",
                current="worker",
                phase="主执行",
                detail=f"attempt={execution_state.attempts}/{execution_state.max_attempts}",
            )
            add_panel(
                "coordinator",
                "Coordinator",
                self._coordinator_summary(execution_state),
                self._coordinator_panel_bullets(execution_state),
            )
            try:
                if current_runner is None:
                    next_msg, next_runner, next_model, failover_notes = self._invoke_chat_with_runner(
                        messages=messages,
                        model=model,
                        max_output_tokens=settings.max_output_tokens,
                        enable_tools=self._coordinator_tools_enabled(execution_state),
                    )
                else:
                    next_msg, next_runner, next_model, failover_notes = self._invoke_with_runner_recovery(
                        runner=current_runner,
                        messages=messages,
                        model=model,
                        max_output_tokens=settings.max_output_tokens,
                        enable_tools=self._coordinator_tools_enabled(execution_state),
                    )
            except Exception as exc:
                fail_role_instance(worker_instance_id, error_text=self._shorten(exc, 220))
                add_run_event(
                    "worker_attempt_failed",
                    node_id=worker_node_id,
                    attempt=execution_state.attempts,
                    error=self._shorten(exc, 220),
                )
                raise
            for note in failover_notes:
                add_trace(note)
            execution_state.status = "worker_returned"
            complete_role_instance(
                worker_instance_id,
                summary_text=f"model={next_model}, tool_mode={execution_state.tool_mode}",
            )
            add_run_event(
                "worker_attempt_completed",
                node_id=worker_node_id,
                attempt=execution_state.attempts,
                model=next_model,
                tool_mode=execution_state.tool_mode,
            )
            add_panel(
                "coordinator",
                "Coordinator",
                self._coordinator_summary(execution_state),
                self._coordinator_panel_bullets(execution_state),
            )
            add_debug(
                stage="llm_to_backend",
                title=title,
                detail=(
                    f"effective_model={next_model}\n"
                    f"{self._summarize_ai_response(next_msg, raw_mode=debug_raw)}"
                ),
            )
            return next_msg, next_runner, next_model, failover_notes

        def append_tool_result_message(
            *,
            name: str,
            arguments: dict[str, Any],
            result: dict[str, Any],
            call_id: str,
            synthetic: bool = False,
        ) -> None:
            nonlocal worker_citation_candidates
            result_json = json.dumps(result, ensure_ascii=False)
            if synthetic:
                set_role_activity(
                    "coordinator",
                    "worker",
                    current="coordinator",
                    phase="工具调度",
                    detail=f"自动补读 {name}",
                )
                add_trace(f"Coordinator 根据 {name} 上下文自动补充工具读取。")
                add_debug(
                    stage="backend_coordinator",
                    title="Coordinator 自动扩展工具链",
                    detail=(
                        f"tool={name}\n"
                        f"args={self._shorten(json.dumps(arguments, ensure_ascii=False), 1200 if not debug_raw else 50000)}"
                    ),
                )
                messages.append(
                    self._AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": arguments,
                                "id": call_id,
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            else:
                set_role_activity(
                    "coordinator",
                    "worker",
                    current="worker",
                    phase="工具请求",
                    detail=f"调用 {name}",
                )
                add_trace(f"执行工具: {name}")
                add_debug(
                    stage="llm_to_backend",
                    title=f"Worker -> Coordinator（请求工具 {name}）",
                    detail=f"args={self._shorten(json.dumps(arguments, ensure_ascii=False), 1200 if not debug_raw else 50000)}",
                )

            add_tool_event(
                ToolEvent(
                    name=name,
                    input=arguments,
                    output_preview=result_json[:1200],
                )
            )
            add_run_event(
                "tool_executed",
                tool=name,
                ok=bool(result.get("ok")) if isinstance(result, dict) else False,
                synthetic=bool(synthetic),
                call_id=call_id,
            )

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
                title=f"Coordinator 执行工具结果 {name}",
                detail=self._shorten(result_json, 1800 if not debug_raw else 120000),
            )
            set_role_activity(
                "coordinator",
                "worker",
                current="worker",
                phase="继续推理",
                detail=f"接收 {name} 结果",
            )
            add_debug(
                stage="backend_to_llm",
                title=f"Coordinator -> Worker（工具结果 {name}）",
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

        add_trace("开始模型推理。")

        try:
            ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                title="Worker -> Coordinator（首次响应）",
                model=requested_model,
            )
            usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
        except Exception as exc:
            add_trace(f"模型请求失败: {exc}")
            add_debug(stage="llm_error", title="Worker 模型调用失败", detail=str(exc))
            clear_role_activity(final_status="failed", summary_text="worker_initial_invoke_failed")
            return (
                f"请求模型失败: {exc}",
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                pipeline_hook_telemetry,
                debug_flow,
                agent_panels,
                sorted(active_roles),
                current_role,
                list(role_states.values()),
                answer_bundle,
                usage_total,
                effective_model,
                self._build_session_route_state(route) if route else {},
            )

        has_attachments = bool(attachment_metas)
        attachments_need_tooling = any(
            self._attachment_needs_tooling_for_turn(meta, history_turn_count=len(history_turns))
            for meta in attachment_metas
        )
        has_msg_attachment = any(str(meta.get("suffix", "") or "").lower() == ".msg" for meta in attachment_metas)
        request_requires_tools = self._coordinator_tools_enabled(execution_state) or attachments_need_tooling
        auto_nudge_budget = min(execution_state.max_attempts, 4 if has_attachments else 2)
        for _ in range(execution_state.max_attempts):
            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            ai_content_text = self._content_to_text(getattr(ai_msg, "content", ""))
            inferred_tool_call = None
            if not tool_calls:
                inferred_tool_call = self._infer_bare_tool_call_from_text(
                    ai_content_text,
                    task_type=str(route.get("task_type") or ""),
                )
            if (
                not self._coordinator_tools_enabled(execution_state)
                and settings.enable_tools
                and not tool_calls
                and inferred_tool_call
                and auto_nudge_budget > 0
            ):
                auto_nudge_budget -= 1
                route = self._coordinator_apply_tool_mode(
                    state=execution_state,
                    route=route,
                    settings=settings,
                    tool_mode="forced",
                    reason="worker_bare_json_tool_args_backend_escalated",
                    summary="Worker 输出了工具参数 JSON，Coordinator 已升级为工具链并直接执行。",
                )
                request_requires_tools = True
                execution_plan[:] = self._build_execution_plan(
                    attachment_metas=attachment_metas,
                    settings=settings,
                    route=route,
                )
                add_panel(
                    "router",
                    "Router",
                    str(route.get("summary") or "已完成链路分诊。").strip() or "已完成链路分诊。",
                    self._format_router_panel_bullets(route),
                )
                add_panel(
                    "coordinator",
                    "Coordinator",
                    self._coordinator_summary(execution_state),
                    self._coordinator_panel_bullets(execution_state),
                )
                tool_calls = [inferred_tool_call]
                add_trace(
                    f"检测到 Worker 输出了裸工具参数 JSON，Coordinator 已开启工具链并自动执行 {inferred_tool_call['name']}。"
                )
                add_debug(
                    stage="backend_warning",
                    title="自动纠偏：裸 JSON 参数触发工具链升级",
                    detail=(
                        "Worker 输出了工具参数样式 JSON，但当时未处于工具链模式。"
                        f"后端已升级并改写为 {inferred_tool_call['name']} 调用，args="
                        f"{self._shorten(json.dumps(inferred_tool_call.get('args') or {}, ensure_ascii=False), 1200 if not debug_raw else 50000)}"
                    ),
                )
            if self._coordinator_tools_enabled(execution_state) and not tool_calls:
                if inferred_tool_call:
                    tool_calls = [inferred_tool_call]
                    add_trace(
                        f"检测到 Worker 输出了裸工具参数 JSON，后端已自动转成 {inferred_tool_call['name']} 工具调用。"
                    )
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：裸 JSON 工具参数",
                        detail=(
                            "Worker 没有发出正式 tool call，而是直接输出了 JSON 参数。"
                            f"后端已自动改写为 {inferred_tool_call['name']} 调用，args="
                            f"{self._shorten(json.dumps(inferred_tool_call.get('args') or {}, ensure_ascii=False), 1200 if not debug_raw else 50000)}"
                        ),
                    )
                elif auto_nudge_budget > 0 and self._looks_like_bare_tool_arguments_text(ai_content_text):
                    auto_nudge_budget -= 1
                    add_trace("检测到 Worker 直接输出了工具参数 JSON（未形成正式 tool_call），Coordinator 已要求其改为有效工具调用后继续。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：裸 JSON 参数但未触发工具",
                        detail=(
                            "Worker 输出了工具参数样式 JSON（可能参数缺失，如 query 为空），"
                            "后端已追加系统指令，要求改为正式 tool_call 并继续执行。"
                        ),
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "你刚刚输出了工具参数 JSON，但没有发出正式 tool_call。"
                                "请不要把 JSON 直接回复给用户。"
                                "如果参数不完整（例如 query 为空），先补全参数；"
                                "随后必须发出有效 tool_call 并继续执行。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（纠正裸 JSON 参数后响应）",
                            model=effective_model,
                            current_runner=runner,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"纠正裸 JSON 参数后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 裸 JSON 参数纠偏失败", detail=str(exc))
                        break
            if not self._coordinator_tools_enabled(execution_state) or not tool_calls:
                if (
                    not self._coordinator_tools_enabled(execution_state)
                    and settings.enable_tools
                    and auto_nudge_budget > 0
                    and self._request_likely_requires_tools(planner_user_message, attachment_metas)
                    and self._looks_like_tool_escalation_needed(ai_content_text)
                ):
                    auto_nudge_budget -= 1
                    route = self._coordinator_apply_tool_mode(
                        state=execution_state,
                        route=route,
                        settings=settings,
                        tool_mode="forced",
                        reason="worker_requested_tools_backend_escalated",
                        summary="Worker 已暴露需要文件/代码工具，后端已强制升级到工具链。",
                    )
                    add_debug(
                        stage="backend_coordinator",
                        title="Coordinator 切换工具模式",
                        detail=(
                            "reason=worker_requested_tools_backend_escalated\n"
                            f"tool_mode={execution_state.tool_mode}\n"
                            f"tool_latch={str(execution_state.tool_latch).lower()}\n"
                            f"transitions={json.dumps(execution_state.transitions[-3:], ensure_ascii=False)}"
                        ),
                    )
                    request_requires_tools = True
                    execution_plan[:] = self._build_execution_plan(
                        attachment_metas=attachment_metas,
                        settings=settings,
                        route=route,
                    )
                    add_panel(
                        "router",
                        "Router",
                        str(route.get("summary") or "已完成链路分诊。").strip() or "已完成链路分诊。",
                        self._format_router_panel_bullets(route),
                    )
                    add_panel(
                        "coordinator",
                        "Coordinator",
                        self._coordinator_summary(execution_state),
                        self._coordinator_panel_bullets(execution_state),
                    )
                    add_trace("检测到 Worker 误判为无工具路径，后端已强制升级为工具链重跑。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：升级到工具链",
                        detail="Worker 表示需要代码/文件搜索或声称工具未启用，后端已强制开启工具链并重跑。",
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "后端已判定当前任务必须使用本地搜索工具。"
                                "不要再说“代码搜索工具未启用”、不要要求用户提供源文件、不要再询问是否确认。"
                                "请立即使用 search_codebase、list_directory、read_text_file 等必要工具继续完成任务。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（升级工具链后响应）",
                            model=effective_model,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"升级工具链后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 工具链升级失败", detail=str(exc))
                        break
                if (
                    self._coordinator_tools_enabled(execution_state)
                    and str(route.get("task_type") or "") == "code_lookup"
                    and auto_nudge_budget > 0
                    and self._tool_events_have_code_hits(tool_events)
                    and self._answer_incorrectly_denies_code_hits(
                        self._content_to_text(getattr(ai_msg, "content", ""))
                    )
                ):
                    auto_nudge_budget -= 1
                    add_trace("检测到 search_codebase 已命中，但 Worker 仍声称未找到代码，Coordinator 已要求继续基于命中上下文作答。")
                    add_debug(
                        stage="backend_coordinator",
                        title="Coordinator 纠正命中否认",
                        detail="search_codebase 已有 match_count>0，但 Worker 文本仍否认命中；已追加系统指令要求基于命中继续解释。",
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "你已经拿到了 search_codebase 的真实命中和后续代码上下文。"
                                "不要再说未找到、没有真实代码命中，也不要继续追问是否读取。"
                                "请直接基于已返回的命中路径、行号和代码片段解释目标函数。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（纠正代码命中否认后响应）",
                            model=effective_model,
                            current_runner=runner,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"纠正代码命中否认后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 代码命中纠偏失败", detail=str(exc))
                        break
                if (
                    self._coordinator_tools_enabled(execution_state)
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
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（补足证据后响应）",
                            model=effective_model,
                            current_runner=runner,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"证据补强后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 证据补强失败", detail=str(exc))
                        break
                if (
                    has_image_attachments
                    and auto_nudge_budget > 0
                    and self._looks_like_image_capability_denial(ai_content_text)
                ):
                    auto_nudge_budget -= 1
                    add_trace("检测到模型误报“无法看图/OCR”，Coordinator 已追加纠偏并重试。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：图片能力误报",
                        detail=(
                            "模型在已注入 image_url 的前提下仍声称无法读取图片；"
                            "后端已追加提示并要求基于图片可见内容重新作答。"
                        ),
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "你已经收到本轮图片输入（image_url）。"
                                "不要再说无法看图、无法 OCR、无法提取图片文字。"
                                "请直接基于图片可见内容作答；看不清的部分明确标注[不清晰]。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（纠正图片能力误报后响应）",
                            model=effective_model,
                            current_runner=runner,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"纠正图片能力误报后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 图片能力纠偏失败", detail=str(exc))
                        break
                if (
                    has_image_attachments
                    and image_text_extraction_request
                    and auto_nudge_budget > 0
                    and self._looks_like_stub_image_transcription(ai_content_text)
                ):
                    auto_nudge_budget -= 1
                    add_trace("检测到图片原文提取回复过短，Coordinator 已要求补全可见文本并重试。")
                    add_debug(
                        stage="backend_warning",
                        title="自动纠偏：图片转录内容过短",
                        detail=(
                            "当前回复像是“标题占位”而非完整转录；"
                            "后端已要求按画面顺序补全可见文本正文。"
                        ),
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "你当前回复只给了标题或极短占位，未给出实际转录内容。"
                                "请重新输出图片中可见原文，按画面顺序逐行列出。"
                                "不要只给“以下为原文”这类开头句。"
                            )
                        )
                    )
                    try:
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（补全图片转录后响应）",
                            model=effective_model,
                            current_runner=runner,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"补全图片转录后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 图片转录补全失败", detail=str(exc))
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
                    rerun_needs_rebind = False
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
                        if (
                            not self._coordinator_tools_enabled(execution_state)
                            and settings.enable_tools
                            and self._should_force_initial_tool_execution(planner_user_message, attachment_metas)
                        ):
                            route = self._coordinator_apply_tool_mode(
                                state=execution_state,
                                route=route,
                                settings=settings,
                                tool_mode="forced",
                                reason="permission_gate_forced_tool_continuation",
                                summary="检测到拖延式确认话术，Coordinator 已强制切换到工具执行模式。",
                                use_planner=True,
                            )
                            add_debug(
                                stage="backend_coordinator",
                                title="Coordinator 切换工具模式",
                                detail=(
                                    "reason=permission_gate_forced_tool_continuation\n"
                                    f"tool_mode={execution_state.tool_mode}\n"
                                    f"tool_latch={str(execution_state.tool_latch).lower()}\n"
                                    f"transitions={json.dumps(execution_state.transitions[-3:], ensure_ascii=False)}"
                                ),
                            )
                            request_requires_tools = True
                            execution_plan[:] = self._build_execution_plan(
                                attachment_metas=attachment_metas,
                                settings=settings,
                                route=route,
                            )
                            add_panel(
                                "router",
                                "Router",
                                str(route.get("summary") or "已完成链路分诊。").strip() or "已完成链路分诊。",
                                self._format_router_panel_bullets(route),
                            )
                            add_panel(
                                "coordinator",
                                "Coordinator",
                                self._coordinator_summary(execution_state),
                                self._coordinator_panel_bullets(execution_state),
                            )
                            rerun_needs_rebind = True
                            messages.append(
                                self._SystemMessage(
                                    content=(
                                        "不要再询问是否直接搜索、是否确认、是否需要绝对路径。"
                                        "当前任务已被 Coordinator 判定为本地搜索/代码定位任务。"
                                        "请直接在默认根目录或用户给出的相对目录下调用 search_codebase/list_directory/read_text_file 完成任务。"
                                    )
                                )
                            )
                        ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                            title="Worker -> Coordinator（自动纠偏后响应）",
                            model=effective_model,
                            current_runner=None if rerun_needs_rebind else runner,
                        )
                        usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                        continue
                    except Exception as exc:
                        add_trace(f"自动纠偏后推理失败: {exc}")
                        add_debug(stage="llm_error", title="Worker 自动纠偏后失败", detail=str(exc))
                        break
                break

            messages.append(ai_msg)
            batch_has_read_text_call = any(str(call.get("name") or "") == "read_text_file" for call in tool_calls)
            for call in tool_calls:
                name = call.get("name") or "unknown"
                arguments = call.get("args") or {}
                if not isinstance(arguments, dict):
                    arguments = {}

                result = self.tools.execute(name, arguments)
                call_id = call.get("id") or f"call_{len(tool_events)}"
                append_tool_result_message(
                    name=name,
                    arguments=arguments,
                    result=result,
                    call_id=call_id,
                )
                if (
                    str(route.get("task_type") or "") == "code_lookup"
                    and name == "search_codebase"
                    and not batch_has_read_text_call
                ):
                    auto_reads = self._coordinator_auto_read_code_search_matches(result)
                    for idx, synthetic_call in enumerate(auto_reads, start=1):
                        synthetic_name = str(synthetic_call.get("name") or "").strip()
                        synthetic_args = synthetic_call.get("args") if isinstance(synthetic_call.get("args"), dict) else {}
                        if not synthetic_name or not synthetic_args:
                            continue
                        synthetic_result = self.tools.execute(synthetic_name, synthetic_args)
                        append_tool_result_message(
                            name=synthetic_name,
                            arguments=synthetic_args,
                            result=synthetic_result,
                            call_id=f"{call_id}_auto_{idx}",
                            synthetic=True,
                        )

            try:
                pruned = self._prune_old_tool_messages(messages)
                if pruned > 0:
                    add_trace(f"已裁剪旧工具上下文 {pruned} 条，降低上下文膨胀。")
                ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                    title="Worker -> Coordinator（后续响应）",
                    model=effective_model,
                    current_runner=runner,
                )
                usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
            except Exception as exc:
                add_trace(f"工具后续推理失败: {exc}")
                add_debug(stage="llm_error", title="Worker 工具后续推理失败", detail=str(exc))
                clear_role_activity(final_status="failed", summary_text="worker_followup_invoke_failed")
                return (
                    f"工具执行后续推理失败: {exc}",
                    tool_events,
                    attachment_note,
                    execution_plan,
                    execution_trace,
                    pipeline_hook_telemetry,
                    debug_flow,
                    agent_panels,
                    sorted(active_roles),
                    current_role,
                    list(role_states.values()),
                    answer_bundle,
                    usage_total,
                    effective_model,
                    self._build_session_route_state(route) if route else {},
                )

        text = self._content_to_text(getattr(ai_msg, "content", ""))
        if not text.strip():
            text = "模型未返回可见文本。"
        add_trace("已生成最终答复。")
        worker_bullets = [
            f"执行环境: {requested_execution_mode}",
            f"工具调用次数: {len(tool_events)}",
            f"tool_mode: {execution_state.tool_mode}",
            f"附件数量: {len(attachment_metas)}",
            f"历史消息载入: {min(len(history_turns), settings.max_context_turns)}",
        ]
        if prefetch_payload:
            worker_bullets.append(f"自动预搜索: {prefetch_payload.get('count', 0)} 条")
        worker_summary = (
            "主执行 Role 已完成取证、工具调用与作答。"
            if self._coordinator_tools_enabled(execution_state)
            else "主执行 Role 已基于当前上下文直接完成作答。"
        )
        add_panel("worker", "Worker", worker_summary, worker_bullets)
        add_debug(
            stage="multi_agent_worker",
            title="Worker 执行完成（含最终草稿）",
            detail=(
                f"effective_model={effective_model}\n"
                f"tool_events={len(tool_events)}\n"
                f"text_chars={len(text)}\n"
                f"text_preview={self._shorten(text, 1200 if not debug_raw else 50000)}"
            ),
        )
        conflict_result = self._make_default_role_result(
            "conflict_detector",
            requested_model=effective_model,
            user_message=user_message,
            effective_user_message=planner_user_message,
            history_summary=summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_result,
            response_text=text,
            extra={
                "spec_lookup_request": spec_lookup_request,
                "evidence_required_mode": evidence_required_mode,
            },
            description="检查答案是否与通识或成熟工程知识明显冲突。",
            output_keys=["has_conflict", "confidence", "summary", "concerns", "suggested_checks"],
            payload={
                "has_conflict": False,
                "confidence": "medium",
                "summary": "Router 已跳过冲突检查。",
                "concerns": [],
                "suggested_checks": [],
                "usage": self._empty_usage(),
                "effective_model": effective_model,
                "notes": [],
            },
        )
        reviewer_result = self._make_default_role_result(
            "reviewer",
            requested_model=effective_model,
            user_message=user_message,
            effective_user_message=planner_user_message,
            history_summary=summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_result,
            conflict_brief=conflict_result,
            execution_trace=execution_trace,
            response_text=text,
            extra={
                "spec_lookup_request": spec_lookup_request,
                "evidence_required_mode": evidence_required_mode,
            },
            description="对最终答复做覆盖度、证据链和风险审阅。",
            tool_names=self._reviewer_readonly_tool_names(),
            output_keys=["verdict", "confidence", "summary", "strengths", "risks", "followups"],
            payload={
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
            },
        )
        review_hook = self._run_pipeline_hook(
            "before_reviewer",
            route=route,
            spec_lookup_request=spec_lookup_request,
            evidence_required_mode=evidence_required_mode,
        )
        record_pipeline_hook(
            phase="before_reviewer",
            hook_payload=review_hook,
            route_before=route,
            route_after=route,
        )
        self._apply_pipeline_hook_effects(
            hook_payload=review_hook,
            add_trace=add_trace,
            add_debug=add_debug,
        )

        if review_hook["use_reviewer"]:
            max_reviewer_reruns = 3
            reviewer_rerun_budget = max_reviewer_reruns if self._coordinator_tools_enabled(execution_state) else 0
            while True:
                if review_hook["use_conflict_detector"]:
                    conflict_node_id, conflict_instance_id = begin_role_instance(
                        "conflict_detector",
                        parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
                        phase="review_conflict_check",
                    )
                    set_role_activity(
                        "coordinator",
                        "conflict_detector",
                        current="conflict_detector",
                        phase="风险检查",
                        detail="检查常识冲突与过度确定性",
                    )
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
                        title="Coordinator -> Conflict Detector",
                        detail=conflict_request_detail,
                    )
                    conflict_context = self._make_role_context(
                        "conflict_detector",
                        requested_model=effective_model or requested_model,
                        user_message=user_message,
                        effective_user_message=planner_user_message,
                        history_summary=summary,
                        attachment_metas=attachment_metas,
                        tool_events=tool_events,
                        planner_brief=planner_result,
                        response_text=text,
                        extra={
                            "spec_lookup_request": spec_lookup_request,
                            "evidence_required_mode": evidence_required_mode,
                        },
                    )
                    conflict_result = self._run_answer_conflict_detector_role(context=conflict_context)
                    conflict_raw = conflict_result.raw_text
                    conflict_effective_model = str(conflict_result.effective_model or "").strip()
                    if conflict_effective_model:
                        effective_model = conflict_effective_model
                    usage_total = self._merge_usage(usage_total, conflict_result.usage or self._empty_usage())
                    complete_role_instance(
                        conflict_instance_id,
                        summary_text=str(conflict_result.summary or "conflict_checked"),
                    )
                    add_run_event(
                        "conflict_detector_completed",
                        node_id=conflict_node_id,
                        has_conflict=bool(conflict_result.payload.get("has_conflict")),
                        confidence=str(conflict_result.payload.get("confidence") or "medium"),
                    )
                    for note in conflict_result.notes:
                        add_trace(note)
                    add_debug(
                        stage="llm_to_backend",
                        title="Conflict Detector -> Coordinator",
                        detail=(
                            f"effective_model={conflict_effective_model or effective_model or requested_model}\n"
                            f"{self._shorten(conflict_raw, 4000 if debug_raw else 1200)}"
                        ),
                    )
                    conflict_summary = conflict_result.summary or "已完成通识冲突检查。"
                    conflict_bullets = self._normalize_string_list(conflict_result.payload.get("concerns") or [], limit=4, item_limit=180)
                    add_panel("conflict_detector", "Conflict Detector", conflict_summary, conflict_bullets)
                else:
                    add_trace("Router 已跳过 Conflict Detector。")

                set_role_activity(
                    "coordinator",
                    "reviewer",
                    current="reviewer",
                    phase="最终审阅",
                    detail="检查覆盖度、证据链与交付风险",
                )
                reviewer_node_id, reviewer_instance_id = begin_role_instance(
                    "reviewer",
                    parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
                    phase="final_review",
                )
                reviewer_request_detail = "\n".join(
                    [
                        f"requested_model={effective_model or requested_model}",
                        f"tool_events={len(tool_events)}",
                        f"execution_trace_items={len(execution_trace)}",
                        f"effective_user_request={self._shorten(planner_user_message, 280 if not debug_raw else 5000)}",
                        f"history_summary_chars={len(summary.strip())}",
                        f"draft_chars={len(text)}",
                        f"draft_preview={self._shorten(text, 400 if not debug_raw else 5000)}",
                        f"evidence_required_mode={evidence_required_mode}",
                    ]
                )
                add_debug(
                    stage="backend_to_llm",
                    title="Coordinator -> Reviewer",
                    detail=reviewer_request_detail,
                )
                reviewer_context = self._make_role_context(
                    "reviewer",
                    requested_model=effective_model or requested_model,
                    user_message=user_message,
                    effective_user_message=planner_user_message,
                    history_summary=summary,
                    attachment_metas=attachment_metas,
                    tool_events=tool_events,
                    planner_brief=planner_result,
                    conflict_brief=conflict_result,
                    execution_trace=execution_trace,
                    response_text=text,
                    extra={
                        "spec_lookup_request": spec_lookup_request,
                        "evidence_required_mode": evidence_required_mode,
                    },
                )
                reviewer_result = self._run_reviewer_role(
                    context=reviewer_context,
                    debug_cb=add_debug,
                    trace_cb=add_trace,
                )
                reviewer_raw = reviewer_result.raw_text
                reviewer_effective_model = str(reviewer_result.effective_model or "").strip()
                if reviewer_effective_model:
                    effective_model = reviewer_effective_model
                usage_total = self._merge_usage(usage_total, reviewer_result.usage or self._empty_usage())
                complete_role_instance(
                    reviewer_instance_id,
                    summary_text=str(reviewer_result.summary or "reviewed"),
                )
                add_run_event(
                    "reviewer_completed",
                    node_id=reviewer_node_id,
                    verdict=str(reviewer_result.payload.get("verdict") or "pass"),
                    confidence=str(reviewer_result.payload.get("confidence") or "medium"),
                )
                for note in reviewer_result.notes:
                    add_trace(note)
                reviewer_verdict = str(reviewer_result.payload.get("verdict") or "pass").strip().lower()
                reviewer_confidence = str(reviewer_result.payload.get("confidence") or "medium").strip().lower()
                if reviewer_verdict == "block":
                    add_trace(f"多 Role: Reviewer 判定阻断，需要大幅修订，confidence={reviewer_confidence}。")
                elif reviewer_verdict == "warn":
                    add_trace(f"多 Role: Reviewer 判定可保留但需补强，confidence={reviewer_confidence}。")
                else:
                    add_trace(f"多 Role: Reviewer 通过，confidence={reviewer_confidence}。")
                add_debug(
                    stage="llm_to_backend",
                    title="Reviewer -> Coordinator",
                    detail=(
                        f"effective_model={reviewer_effective_model or effective_model or requested_model}\n"
                        f"{self._shorten(reviewer_raw, 4000 if debug_raw else 1200)}"
                    ),
                )
                reviewer_summary = reviewer_result.summary or "已完成最终答复审阅。"
                reviewer_bullets = (
                    self._normalize_string_list(
                        [f"判定: {reviewer_verdict}"],
                        limit=1,
                        item_limit=80,
                    )
                    + self._normalize_string_list(
                        [f"使用工具: {item}" for item in reviewer_result.payload.get("readonly_checks") or []],
                        limit=4,
                        item_limit=180,
                    )
                    + self._normalize_string_list(
                        [f"复核证据: {item}" for item in reviewer_result.payload.get("readonly_evidence") or []],
                        limit=4,
                        item_limit=200,
                    )
                    + self._normalize_string_list(reviewer_result.payload.get("strengths") or [], limit=2, item_limit=180)
                    + self._normalize_string_list(reviewer_result.payload.get("risks") or [], limit=3, item_limit=180)
                    + self._normalize_string_list(reviewer_result.payload.get("followups") or [], limit=2, item_limit=180)
                )
                add_panel("reviewer", "Reviewer", reviewer_summary, reviewer_bullets)

                followup_reads = self._coordinator_collect_truncated_read_requests(tool_events, limit=2)
                reviewer_wants_more = self._reviewer_requests_more_evidence(reviewer_result)
                if (
                    reviewer_rerun_budget > 0
                    and followup_reads
                    and self._coordinator_should_rerun_worker_after_reviewer(
                        route=route,
                        reviewer_brief=reviewer_result,
                        tool_events=tool_events,
                    )
                ):
                    reviewer_rerun_budget -= 1
                    add_trace("Reviewer 指出当前证据仍是局部读取，Coordinator 已继续读取后续分块并回流给 Worker。")
                    add_debug(
                        stage="backend_coordinator",
                        title="Coordinator 根据 Reviewer 回流 Worker",
                        detail=(
                            f"reviewer_verdict={reviewer_verdict}\n"
                            f"followup_reads={json.dumps(followup_reads, ensure_ascii=False)}"
                        ),
                    )
                    messages.append(ai_msg)
                    for idx, synthetic_call in enumerate(followup_reads, start=1):
                        synthetic_name = str(synthetic_call.get("name") or "").strip()
                        synthetic_args = (
                            synthetic_call.get("args") if isinstance(synthetic_call.get("args"), dict) else {}
                        )
                        if not synthetic_name or not synthetic_args:
                            continue
                        synthetic_result = self.tools.execute(synthetic_name, synthetic_args)
                        append_tool_result_message(
                            name=synthetic_name,
                            arguments=synthetic_args,
                            result=synthetic_result,
                            call_id=f"reviewer_followup_{idx}",
                            synthetic=True,
                        )
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "Reviewer 已确认你已经命中了目标，但当前证据还是局部片段。"
                                "Coordinator 已继续读取后续代码上下文。"
                                "不要再说未命中、不要再要求用户确认是否继续读取。"
                                "请直接基于现有 search_codebase 命中与新增 read_text_file 内容，给出目标函数的解释。"
                            )
                        )
                    )
                    ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                        title="Worker -> Coordinator（根据 Reviewer 继续取证）",
                        model=effective_model,
                        current_runner=runner,
                    )
                    usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                    text = self._content_to_text(getattr(ai_msg, "content", ""))
                    if not text.strip():
                        text = "模型未返回可见文本。"
                    continue

                if (
                    reviewer_rerun_budget > 0
                    and reviewer_wants_more
                    and self._coordinator_tools_enabled(execution_state)
                    and (bool(attachment_metas) or bool(tool_events))
                ):
                    reviewer_rerun_budget -= 1
                    add_trace("Reviewer 指出当前证据或上下文不足，Coordinator 已要求 Worker 继续基于现有附件与工具结果完成任务。")
                    add_debug(
                        stage="backend_coordinator",
                        title="Coordinator 根据 Reviewer 继续推动 Worker",
                        detail=(
                            f"reviewer_verdict={reviewer_verdict}\n"
                            f"reviewer_summary={self._shorten(reviewer_summary, 280)}\n"
                            f"reviewer_risks={json.dumps(self._normalize_string_list(reviewer_result.payload.get('risks') or [], limit=4, item_limit=180), ensure_ascii=False)}"
                        ),
                    )
                    messages.append(ai_msg)
                    messages.append(
                        self._SystemMessage(
                            content=(
                                "Reviewer 已指出当前答案的证据或上下文仍不足。"
                                "请继续基于现有附件、路径和先前工具结果完成任务。"
                                "如果本轮有附件但尚未读取足够正文，优先调用 read_text_file、search_text_in_file、read_section_by_heading、table_extract、search_codebase 等只读工具继续补足。"
                                "不要再要求用户确认，不要停在“无法核对/需要文档”这类占位结论。"
                                "补足后直接给出更新后的完整答案。"
                            )
                        )
                    )
                    ai_msg, runner, effective_model, failover_notes = invoke_worker_turn(
                        title="Worker -> Coordinator（根据 Reviewer 继续完成）",
                        model=effective_model,
                        current_runner=runner,
                    )
                    usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
                    text = self._content_to_text(getattr(ai_msg, "content", ""))
                    if not text.strip():
                        text = "模型未返回可见文本。"
                    continue

                if review_hook["use_revision"]:
                    revision_node_id, revision_instance_id = begin_role_instance(
                        "revision",
                        parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
                        phase="final_revision",
                    )
                    set_role_activity(
                        "coordinator",
                        "revision",
                        current="revision",
                        phase="最终修订",
                        detail=f"根据 reviewer_verdict={reviewer_verdict} 调整答案",
                    )
                    revision_request_detail = "\n".join(
                        [
                            f"requested_model={effective_model or requested_model}",
                            f"effective_user_request={self._shorten(planner_user_message, 280 if not debug_raw else 5000)}",
                            f"reviewer_verdict={reviewer_verdict}",
                            f"reviewer_confidence={reviewer_confidence}",
                            f"reviewer_summary={self._shorten(reviewer_result.summary, 220 if not debug_raw else 5000)}",
                            f"reviewer_risks={json.dumps(self._normalize_string_list(reviewer_result.payload.get('risks') or [], limit=4, item_limit=180), ensure_ascii=False)}",
                            f"reviewer_followups={json.dumps(self._normalize_string_list(reviewer_result.payload.get('followups') or [], limit=3, item_limit=180), ensure_ascii=False)}",
                            f"current_text_chars={len(text)}",
                            f"current_text_preview={self._shorten(text, 400 if not debug_raw else 5000)}",
                        ]
                    )
                    add_debug(
                        stage="backend_to_llm",
                        title="Coordinator -> Revision",
                        detail=revision_request_detail,
                    )
                    revision_context = self._make_role_context(
                        "revision",
                        requested_model=effective_model or requested_model,
                        user_message=user_message,
                        effective_user_message=planner_user_message,
                        history_summary=summary,
                        attachment_metas=attachment_metas,
                        tool_events=tool_events,
                        planner_brief=planner_result,
                        reviewer_brief=reviewer_result,
                        conflict_brief=conflict_result,
                        response_text=text,
                        extra={"evidence_required_mode": evidence_required_mode},
                    )
                    revision_result = self._run_revision_role(context=revision_context)
                    revision_raw = revision_result.raw_text
                    revision_effective_model = str(revision_result.effective_model or "").strip()
                    if revision_effective_model:
                        effective_model = revision_effective_model
                    usage_total = self._merge_usage(usage_total, revision_result.usage or self._empty_usage())
                    complete_role_instance(
                        revision_instance_id,
                        summary_text=str(revision_result.summary or "revision_done"),
                    )
                    add_run_event(
                        "revision_completed",
                        node_id=revision_node_id,
                        changed=bool(revision_result.payload.get("changed")),
                    )
                    for note in revision_result.notes:
                        add_trace(note)
                    revised_text = str(revision_result.payload.get("final_answer") or "").strip()
                    revision_changed = bool(revision_result.payload.get("changed")) and bool(revised_text)
                    if revision_changed:
                        if self._should_preserve_worker_answer_after_revision(
                            current_text=text,
                            revised_text=revised_text,
                            tool_events=tool_events,
                            attachment_metas=attachment_metas,
                        ):
                            add_trace("检测到 Revision 抹掉了已成功写出的交付结果，Coordinator 已保留 Worker 原答复。")
                            add_debug(
                                stage="backend_warning",
                                title="Coordinator 拒绝覆盖 Worker 交付结果",
                                detail=(
                                    f"current_text_preview={self._shorten(text, 500 if not debug_raw else 5000)}\n"
                                    f"revised_text_preview={self._shorten(revised_text, 500 if not debug_raw else 5000)}"
                                ),
                            )
                        else:
                            text = revised_text
                            add_trace("多 Role: Revision 已应用到最终答复。")
                    else:
                        add_trace("多 Role: Revision 未修改最终答复。")
                    add_debug(
                        stage="llm_to_backend",
                        title="Revision -> Coordinator",
                        detail=(
                            f"effective_model={revision_effective_model or effective_model or requested_model}\n"
                            f"{self._shorten(revision_raw, 4000 if debug_raw else 1200)}"
                        ),
                    )
                    revision_summary = revision_result.summary or "已完成最终润色与修订判断。"
                    revision_bullets = self._normalize_string_list(
                        revision_result.payload.get("key_changes") or [], limit=4, item_limit=180
                    )
                    add_panel("revision", "Revision", revision_summary, revision_bullets)
                else:
                    add_trace("Hook(before_reviewer): 已跳过 Revision。")
                break
        else:
            add_trace("Hook(before_reviewer): 已跳过 Conflict Detector / Reviewer / Revision。")

        text = self._sanitize_final_answer_text(
            text,
            user_message=user_message,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            inline_followup_context=bool(inline_followup_source),
        )
        structurer_hook = self._run_pipeline_hook(
            "before_structurer",
            route=route,
            final_text=text,
            citations=self._finalize_citation_candidates(worker_citation_candidates),
            reviewer_brief=reviewer_result,
            conflict_brief=conflict_result,
            evidence_required_mode=evidence_required_mode,
            spec_lookup_request=spec_lookup_request,
        )
        record_pipeline_hook(
            phase="before_structurer",
            hook_payload=structurer_hook,
            route_before=route,
            route_after=route,
        )
        finalized_citations = structurer_hook["finalized_citations"]
        answer_bundle = structurer_hook["answer_bundle"]
        self._apply_pipeline_hook_effects(
            hook_payload=structurer_hook,
            add_trace=add_trace,
            add_debug=add_debug,
        )
        if not structurer_hook["use_structurer"]:
            clear_role_activity(final_status="completed", summary_text="completed_without_structurer")
            return (
                text,
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                pipeline_hook_telemetry,
                debug_flow,
                agent_panels,
                sorted(active_roles),
                current_role,
                list(role_states.values()),
                answer_bundle,
                usage_total,
                effective_model,
                self._build_session_route_state(route) if route else {},
            )
        if not structurer_hook["should_emit_answer_bundle"]:
            clear_role_activity(final_status="completed", summary_text="completed_without_answer_bundle")
            return (
                text,
                tool_events,
                attachment_note,
                execution_plan,
                execution_trace,
                pipeline_hook_telemetry,
                debug_flow,
                agent_panels,
                sorted(active_roles),
                current_role,
                list(role_states.values()),
                answer_bundle,
                usage_total,
                effective_model,
                self._build_session_route_state(route) if route else {},
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
            title="Coordinator -> Structurer",
            detail=structurer_request_detail,
        )
        set_role_activity(
            "coordinator",
            "structurer",
            current="structurer",
            phase="结构化整理",
            detail=f"整理 {len(finalized_citations)} 条 citations（证据来源）",
        )
        structurer_node_id, structurer_instance_id = begin_role_instance(
            "structurer",
            parent_node_id=coordinator_node_id or run_state.root_node_id if run_state else None,
            phase="bundle_answer",
        )
        structurer_context = self._make_role_context(
            "structurer",
            requested_model=effective_model or requested_model,
            reviewer_brief=reviewer_result,
            conflict_brief=conflict_result,
            response_text=text,
            extra={"citations": finalized_citations},
        )
        structurer_result = self._run_answer_structurer_role(context=structurer_context)
        answer_bundle = structurer_result.payload
        structurer_raw = structurer_result.raw_text
        complete_role_instance(
            structurer_instance_id,
            summary_text=str(structurer_result.summary or "structured"),
        )
        add_run_event(
            "structurer_completed",
            node_id=structurer_node_id,
            claim_count=len(answer_bundle.get("claims") or []),
            citation_count=len(answer_bundle.get("citations") or []),
        )
        add_debug(
            stage="llm_to_backend",
            title="Structurer -> Coordinator",
            detail=self._shorten(structurer_raw, 4000 if debug_raw else 1200),
        )
        add_panel(
            "structurer",
            "Structured Output",
            structurer_result.summary or str(answer_bundle.get("summary") or "").strip() or "已生成结构化答案与证据链。",
            self._normalize_string_list(
                [
                    f"assertions（关键结论）={len(answer_bundle.get('claims') or [])}",
                    f"citations（证据来源）={len(answer_bundle.get('citations') or [])}",
                ]
                + [f"warning（风险提示）: {item}" for item in (answer_bundle.get("warnings") or [])],
                limit=6,
                item_limit=180,
            ),
        )
        clear_role_activity(final_status="completed", summary_text="completed_with_structured_bundle")
        return (
            text,
            tool_events,
            attachment_note,
            execution_plan,
            execution_trace,
            pipeline_hook_telemetry,
            debug_flow,
            agent_panels,
            sorted(active_roles),
            current_role,
            list(role_states.values()),
            answer_bundle,
            usage_total,
            effective_model,
            self._build_session_route_state(route) if route else {},
        )

    def _make_role_spec(
        self,
        role: str,
        *,
        description: str = "",
        tool_names: list[str] | tuple[str, ...] | None = None,
        output_keys: list[str] | tuple[str, ...] | None = None,
    ) -> RoleSpec:
        return make_role_spec_helper(
            self,
            role,
            description=description,
            tool_names=tool_names,
            output_keys=output_keys,
        )

    def _make_role_context(
        self,
        role: str,
        *,
        requested_model: str = "",
        user_message: str = "",
        effective_user_message: str = "",
        history_summary: str = "",
        attachment_metas: list[dict[str, Any]] | None = None,
        tool_events: list[ToolEvent] | None = None,
        planner_brief: RoleResult | dict[str, Any] | None = None,
        reviewer_brief: RoleResult | dict[str, Any] | None = None,
        conflict_brief: RoleResult | dict[str, Any] | None = None,
        route: dict[str, Any] | None = None,
        execution_trace: list[str] | None = None,
        response_text: str = "",
        user_content: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> RoleContext:
        return make_role_context_helper(
            self,
            role,
            requested_model=requested_model,
            user_message=user_message,
            effective_user_message=effective_user_message,
            history_summary=history_summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_brief,
            reviewer_brief=reviewer_brief,
            conflict_brief=conflict_brief,
            route=route,
            execution_trace=execution_trace,
            response_text=response_text,
            user_content=user_content,
            extra=extra,
        )

    def _make_role_result(self, spec: RoleSpec, context: RoleContext, payload: dict[str, Any], raw_text: str) -> RoleResult:
        return make_role_result_helper(self, spec, context, payload, raw_text)

    def _make_default_role_result(
        self,
        role: str,
        *,
        payload: dict[str, Any],
        requested_model: str = "",
        user_message: str = "",
        effective_user_message: str = "",
        history_summary: str = "",
        attachment_metas: list[dict[str, Any]] | None = None,
        tool_events: list[ToolEvent] | None = None,
        planner_brief: RoleResult | dict[str, Any] | None = None,
        reviewer_brief: RoleResult | dict[str, Any] | None = None,
        conflict_brief: RoleResult | dict[str, Any] | None = None,
        route: dict[str, Any] | None = None,
        execution_trace: list[str] | None = None,
        response_text: str = "",
        user_content: Any = None,
        extra: dict[str, Any] | None = None,
        description: str = "",
        tool_names: list[str] | tuple[str, ...] | None = None,
        output_keys: list[str] | tuple[str, ...] | None = None,
        raw_text: str = "",
    ) -> RoleResult:
        return make_default_role_result_helper(
            self,
            role,
            payload=payload,
            requested_model=requested_model,
            user_message=user_message,
            effective_user_message=effective_user_message,
            history_summary=history_summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_brief,
            reviewer_brief=reviewer_brief,
            conflict_brief=conflict_brief,
            route=route,
            execution_trace=execution_trace,
            response_text=response_text,
            user_content=user_content,
            extra=extra,
            description=description,
            tool_names=tool_names,
            output_keys=output_keys,
            raw_text=raw_text,
        )

    def _role_payload_dict(self, value: RoleResult | dict[str, Any] | None) -> dict[str, Any]:
        return role_payload_dict_helper(value)

    def _run_planner(
        self,
        *,
        requested_model: str,
        user_message: str,
        summary: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
    ) -> tuple[dict[str, Any], str]:
        context = self._make_role_context(
            "planner",
            requested_model=requested_model,
            user_message=user_message,
            history_summary=summary,
            attachment_metas=attachment_metas,
            extra={
                "response_style": settings.response_style,
                "enable_tools": settings.enable_tools,
                "max_context_turns": settings.max_context_turns,
            },
        )
        result = self._run_planner_role(context=context, settings=settings)
        return result.payload, result.raw_text

    def _run_planner_role(self, *, context: RoleContext, settings: ChatSettings) -> RoleResult:
        spec = self._make_role_spec(
            "planner",
            description="提炼目标、约束和执行计划。",
            output_keys=["objective", "constraints", "plan", "watchouts", "success_signals"],
        )
        fallback = {
            "objective": self._shorten(context.user_message.strip(), 220),
            "constraints": [],
            "plan": self._build_execution_plan(attachment_metas=context.attachment_metas, settings=settings),
            "watchouts": [],
            "success_signals": [],
            "usage": self._empty_usage(),
            "effective_model": context.requested_model,
            "notes": [],
        }
        attachment_summary = self._summarize_attachment_metas_for_agents(context.attachment_metas)
        planner_input = "\n".join(
            [
                f"user_message:\n{context.user_message.strip() or '(empty)'}",
                f"history_summary:\n{context.history_summary.strip() or '(none)'}",
                f"attachments:\n{attachment_summary}",
                f"response_style={context.extra.get('response_style') or settings.response_style}",
                f"enable_tools={context.extra.get('enable_tools', settings.enable_tools)}",
                f"max_context_turns={context.extra.get('max_context_turns', settings.max_context_turns)}",
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
                model=context.requested_model,
                max_output_tokens=900,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Planner 未返回标准 JSON，已降级为默认执行计划。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return self._make_role_result(spec, context, fallback, raw_text)

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
            return self._make_role_result(spec, context, planner, raw_text)
        except Exception as exc:
            fallback["notes"] = [f"Planner 调用失败，已回退默认计划: {self._shorten(exc, 180)}"]
            raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return self._make_role_result(spec, context, fallback, raw_text)

    def _run_answer_conflict_detector(
        self,
        *,
        requested_model: str,
        user_message: str,
        effective_user_message: str,
        history_summary: str,
        attachment_metas: list[dict[str, Any]],
        final_text: str,
        planner_brief: RoleResult | dict[str, Any],
        tool_events: list[ToolEvent],
        spec_lookup_request: bool = False,
        evidence_required_mode: bool = False,
    ) -> tuple[dict[str, Any], str]:
        context = self._make_role_context(
            "conflict_detector",
            requested_model=requested_model,
            user_message=user_message,
            effective_user_message=effective_user_message,
            history_summary=history_summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_brief,
            response_text=final_text,
            extra={
                "spec_lookup_request": spec_lookup_request,
                "evidence_required_mode": evidence_required_mode,
            },
        )
        result = self._run_answer_conflict_detector_role(context=context)
        return result.payload, result.raw_text

    def _run_answer_conflict_detector_role(self, *, context: RoleContext) -> RoleResult:
        spec = self._make_role_spec(
            "conflict_detector",
            description="检查答案是否与通识或成熟工程知识明显冲突。",
            output_keys=["has_conflict", "confidence", "summary", "concerns", "suggested_checks"],
        )
        validation_context = self._summarize_validation_context(context.tool_events)
        attachment_summary = self._summarize_attachment_metas_for_agents(context.attachment_metas)
        tool_summaries = self._summarize_tool_events_for_review(context.tool_events, limit=10)
        detector_input = "\n".join(
            [
                f"effective_user_request:\n{context.primary_user_request or '(empty)'}",
                f"raw_user_message:\n{context.user_message.strip() or '(empty)'}",
                f"history_summary:\n{context.history_summary.strip() or '(none)'}",
                f"attachments:\n{attachment_summary}",
                f"planner_objective:\n{str(context.planner_brief.get('objective') or '').strip() or '(none)'}",
                f"spec_lookup_request={str(bool(context.extra.get('spec_lookup_request'))).lower()}",
                f"evidence_required_mode={str(bool(context.extra.get('evidence_required_mode'))).lower()}",
                f"web_tools_used={str(validation_context['web_tools_used']).lower()}",
                f"web_tools_success={str(validation_context['web_tools_success']).lower()}",
                "web_tool_notes:",
                *[f"- {item}" for item in validation_context["web_tool_notes"]],
                "web_tool_warnings:",
                *[f"- {item}" for item in validation_context["web_tool_warnings"]],
                "tool_events:",
                *(tool_summaries or ["(none)"]),
                f"answer:\n{context.response_text.strip() or '(empty)'}",
            ]
        )
        fallback = {
            "has_conflict": False,
            "confidence": "medium",
            "summary": "Conflict Detector 未发现明显常识冲突。",
            "concerns": [],
            "suggested_checks": [],
            "usage": self._empty_usage(),
            "effective_model": context.requested_model,
            "notes": [],
        }
        messages = [
            self._SystemMessage(
                content=(
                    "你是 Answer Conflict Detector。"
                    "基于通识、成熟工程知识和任务上下文，检查当前答案是否存在明显可疑点、过度确定、或与常见知识冲突。"
                    "不要输出思维链。"
                    "你的知识只能用于报警和建议复核，不能替代文件证据。"
                    "如果 attachments 或 tool_events 已显示本轮存在附件/本地文件且 Worker 已经读取过，"
                    "不要仅因为 raw_user_message 是短跟进、或你自己没有独立文件证据，就把答案判成“没有依据”。"
                    "只有当答案和通识或工程常识存在明确冲突时，才应标记 has_conflict=true。"
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
                model=context.requested_model,
                max_output_tokens=900,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Conflict Detector 未返回标准 JSON，已忽略冲突检查结果。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return self._make_role_result(spec, context, fallback, raw_text)

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
            return self._make_role_result(spec, context, detector, raw_text)
        except Exception as exc:
            fallback["notes"] = [f"Conflict Detector 调用失败，已跳过: {self._shorten(exc, 180)}"]
            raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return self._make_role_result(spec, context, fallback, raw_text)

    def _run_reviewer(
        self,
        *,
        requested_model: str,
        user_message: str,
        effective_user_message: str,
        history_summary: str,
        attachment_metas: list[dict[str, Any]],
        final_text: str,
        planner_brief: RoleResult | dict[str, Any],
        tool_events: list[ToolEvent],
        execution_trace: list[str],
        spec_lookup_request: bool = False,
        evidence_required_mode: bool = False,
        conflict_brief: RoleResult | dict[str, Any] | None = None,
        debug_cb: Callable[[str, str, str], None] | None = None,
        trace_cb: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, Any], str]:
        context = self._make_role_context(
            "reviewer",
            requested_model=requested_model,
            user_message=user_message,
            effective_user_message=effective_user_message,
            history_summary=history_summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_brief,
            conflict_brief=conflict_brief or {},
            execution_trace=execution_trace,
            response_text=final_text,
            extra={
                "spec_lookup_request": spec_lookup_request,
                "evidence_required_mode": evidence_required_mode,
            },
        )
        result = self._run_reviewer_role(context=context, debug_cb=debug_cb, trace_cb=trace_cb)
        return result.payload, result.raw_text

    def _run_reviewer_role(
        self,
        *,
        context: RoleContext,
        debug_cb: Callable[[str, str, str], None] | None = None,
        trace_cb: Callable[[str], None] | None = None,
    ) -> RoleResult:
        spec = self._make_role_spec(
            "reviewer",
            description="对最终答复做覆盖度、证据链和风险审阅。",
            tool_names=self._reviewer_readonly_tool_names(),
            output_keys=["verdict", "confidence", "summary", "strengths", "risks", "followups"],
        )
        tool_summaries = self._summarize_tool_events_for_review(context.tool_events, limit=12)
        write_actions = self._summarize_write_tool_events(context.tool_events, limit=6)
        attachment_summary = self._summarize_attachment_metas_for_agents(context.attachment_metas)
        validation_context = self._summarize_validation_context(context.tool_events)
        local_access_succeeded = self._has_successful_local_file_access(context.tool_events)
        conflict_lines = [
            f"conflict_has_conflict={str(bool(context.conflict_brief.get('has_conflict'))).lower()}",
            f"conflict_summary={str(context.conflict_brief.get('summary') or '').strip() or '(none)'}",
            "conflict_concerns:",
            *[f"- {item}" for item in self._normalize_string_list(context.conflict_brief.get("concerns") or [], limit=4)],
        ]
        reviewer_input = "\n".join(
            [
                f"effective_user_request:\n{context.primary_user_request or '(empty)'}",
                f"raw_user_message:\n{context.user_message.strip() or '(empty)'}",
                f"history_summary:\n{context.history_summary.strip() or '(none)'}",
                f"attachments:\n{attachment_summary}",
                f"planner_objective:\n{str(context.planner_brief.get('objective') or '').strip() or '(none)'}",
                "planner_plan:",
                *[f"- {item}" for item in self._normalize_string_list(context.planner_brief.get("plan") or [], limit=6)],
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
            "usage": self._empty_usage(),
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
            self._SystemMessage(content=reviewer_system_prompt),
            self._HumanMessage(content=reviewer_input),
        ]
        try:
            usage_total = self._empty_usage()
            notes: list[str] = []
            reviewer_tool_names: list[str] = []
            reviewer_evidence: list[str] = []
            ai_msg, runner, effective_model, invoke_notes = self._invoke_chat_with_runner(
                messages=messages,
                model=context.requested_model,
                max_output_tokens=1200,
                enable_tools=True,
                tool_names=readonly_tools,
            )
            notes.extend(invoke_notes)
            usage_total = self._merge_usage(usage_total, self._extract_usage_from_message(ai_msg))
            nudge_budget = 1 if (bool(context.extra.get("evidence_required_mode")) or bool(context.attachment_metas) or local_access_succeeded) else 0
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
                            f"Reviewer -> Coordinator（请求工具 {name}）",
                            "\n".join(
                                [
                                    f"tool={name}",
                                    f"args={self._shorten(json.dumps(args, ensure_ascii=False), 1200)}",
                                ]
                            ),
                        )
                    result = self.tools.execute(name, args)
                    result_json = json.dumps(result, ensure_ascii=False)
                    result_ok = bool(result.get("ok")) if isinstance(result, dict) else False
                    tool_payload, trim_note = self._prepare_tool_result_for_llm(
                        name=name,
                        arguments=args,
                        raw_result=result,
                        raw_json=result_json,
                    )
                    if result_ok:
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
                            f"Coordinator 执行 Reviewer 只读工具 {name}",
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
                            f"Coordinator -> Reviewer（工具结果 {name}）",
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
                return self._make_role_result(spec, context, fallback, raw_text)

            verdict = self._normalize_reviewer_verdict(
                parsed.get("verdict"),
                risks=parsed.get("risks") or [],
                followups=parsed.get("followups") or [],
                spec_lookup_request=bool(context.extra.get("spec_lookup_request")),
                evidence_required_mode=bool(context.extra.get("evidence_required_mode")),
                readonly_checks=reviewer_tool_names,
                conflict_has_conflict=bool(context.conflict_brief.get("has_conflict")),
                conflict_realtime_only=self._conflict_is_realtime_capability_warning(context.conflict_brief),
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
                "strengths": self._normalize_string_list(parsed.get("strengths") or [], limit=3, item_limit=180),
                "risks": self._normalize_string_list(parsed.get("risks") or [], limit=4, item_limit=180),
                "followups": self._normalize_string_list(parsed.get("followups") or [], limit=3, item_limit=180),
                "usage": usage_total,
                "effective_model": effective_model,
                "notes": notes,
                "readonly_checks": reviewer_tool_names,
                "readonly_evidence": reviewer_evidence,
            }
            return self._make_role_result(spec, context, reviewer, raw_text)
        except Exception as exc:
            fallback["notes"] = [f"Reviewer 调用失败，已跳过最终审阅: {self._shorten(exc, 180)}"]
            raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return self._make_role_result(spec, context, fallback, raw_text)

    def _run_revision(
        self,
        *,
        requested_model: str,
        user_message: str,
        effective_user_message: str,
        history_summary: str,
        attachment_metas: list[dict[str, Any]],
        current_text: str,
        planner_brief: RoleResult | dict[str, Any],
        reviewer_brief: RoleResult | dict[str, Any],
        tool_events: list[ToolEvent],
        conflict_brief: RoleResult | dict[str, Any] | None = None,
        evidence_required_mode: bool = False,
    ) -> tuple[dict[str, Any], str]:
        context = self._make_role_context(
            "revision",
            requested_model=requested_model,
            user_message=user_message,
            effective_user_message=effective_user_message,
            history_summary=history_summary,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
            planner_brief=planner_brief,
            reviewer_brief=reviewer_brief,
            conflict_brief=conflict_brief or {},
            response_text=current_text,
            extra={"evidence_required_mode": evidence_required_mode},
        )
        result = self._run_revision_role(context=context)
        return result.payload, result.raw_text

    def _run_revision_role(self, *, context: RoleContext) -> RoleResult:
        spec = self._make_role_spec(
            "revision",
            description="根据 reviewer 结论修订最终答复。",
            output_keys=["changed", "summary", "key_changes", "final_answer"],
        )
        local_access_succeeded = self._has_successful_local_file_access(context.tool_events)
        tool_summaries = self._summarize_tool_events_for_review(context.tool_events, limit=10)
        write_actions = self._summarize_write_tool_events(context.tool_events, limit=6)
        attachment_summary = self._summarize_attachment_metas_for_agents(context.attachment_metas)
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
                *[f"- {item}" for item in self._normalize_string_list(context.reviewer_brief.get("risks") or [], limit=5)],
                "reviewer_followups:",
                *[f"- {item}" for item in self._normalize_string_list(context.reviewer_brief.get("followups") or [], limit=4)],
                "reviewer_readonly_checks:",
                *[f"- {item}" for item in self._normalize_string_list(context.reviewer_brief.get("readonly_checks") or [], limit=8)],
                "reviewer_readonly_evidence:",
                *[
                    f"- {item}"
                    for item in self._normalize_string_list(context.reviewer_brief.get("readonly_evidence") or [], limit=8)
                ],
                f"conflict_summary={str(context.conflict_brief.get('summary') or '').strip() or '(none)'}",
                "conflict_concerns:",
                *[f"- {item}" for item in self._normalize_string_list(context.conflict_brief.get("concerns") or [], limit=4)],
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
            "usage": self._empty_usage(),
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
            self._SystemMessage(content=revision_system_prompt),
            self._HumanMessage(content=revision_input),
        ]
        try:
            ai_msg, _, effective_model, notes = self._invoke_chat_with_runner(
                messages=messages,
                model=context.requested_model,
                max_output_tokens=1800,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Revision 未返回标准 JSON，已保留原答复。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                return self._make_role_result(spec, context, fallback, raw_text)

            changed_raw = parsed.get("changed")
            if isinstance(changed_raw, bool):
                changed = changed_raw
            else:
                changed = str(changed_raw or "").strip().lower() in {"1", "true", "yes", "on"}
            final_answer = str(parsed.get("final_answer") or context.response_text).strip() or context.response_text
            revision = {
                "changed": changed and final_answer.strip() != context.response_text.strip(),
                "summary": str(parsed.get("summary") or fallback["summary"]).strip() or fallback["summary"],
                "key_changes": self._normalize_string_list(parsed.get("key_changes") or [], limit=4, item_limit=180),
                "final_answer": final_answer,
                "usage": self._extract_usage_from_message(ai_msg),
                "effective_model": effective_model,
                "notes": notes,
            }
            return self._make_role_result(spec, context, revision, raw_text)
        except Exception as exc:
            fallback["notes"] = [f"Revision 调用失败，已保留原答复: {self._shorten(exc, 180)}"]
            raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return self._make_role_result(spec, context, fallback, raw_text)

    def _sanitize_final_answer_text(
        self,
        text: str,
        *,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        tool_events: list[ToolEvent] | None = None,
        inline_followup_context: bool = False,
    ) -> str:
        registry = self._module_registry()
        module = getattr(registry, "finalizer", None)
        selected_ref = str((registry.selected_refs or {}).get("finalizer") or "")
        fallback_ref = "finalizer@1.0.0"
        if module is None or not hasattr(module, "sanitize"):
            return self._sanitize_final_answer_text_impl(
                text,
                user_message=user_message,
                attachment_metas=attachment_metas,
                tool_events=tool_events,
                inline_followup_context=inline_followup_context,
            )
        try:
            sanitized = module.sanitize(
                agent=self,
                text=text,
                user_message=user_message,
                attachment_metas=attachment_metas,
                tool_events=tool_events,
                inline_followup_context=inline_followup_context,
            )
            self._record_module_success(kind="finalizer", selected_ref=selected_ref or fallback_ref)
            return sanitized
        except Exception as exc:
            self._record_module_failure(
                kind="finalizer",
                requested_ref=selected_ref or fallback_ref,
                fallback_ref=fallback_ref,
                error=str(exc),
            )
            return self._sanitize_final_answer_text_impl(
                text,
                user_message=user_message,
                attachment_metas=attachment_metas,
                tool_events=tool_events,
                inline_followup_context=inline_followup_context,
            )

    def _sanitize_final_answer_text_impl(
        self,
        text: str,
        *,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        tool_events: list[ToolEvent] | None = None,
        inline_followup_context: bool = False,
    ) -> str:
        original = str(text or "").strip()
        if not original:
            return original

        cleaned = original
        local_access_succeeded = self._has_successful_local_file_access(tool_events or [])
        has_text_search_evidence = self._has_text_search_evidence(tool_events or [])
        had_unverified_fulltext_claim = self._looks_like_unverified_fulltext_search_claim(original) and not has_text_search_evidence
        had_internal_meta = bool(
            re.search(r"(?i)\b(?:reviewer_verdict|reviewer_confidence|evidence_required_mode|task_mode)\b", original)
        )
        had_path_denial = self._looks_like_local_path_denial(original)
        had_permission_gate = self._looks_like_permission_gate_text(
            original,
            has_attachments=bool(attachment_metas),
            request_requires_tools=local_access_succeeded,
        )
        had_inline_evidence_gate = bool(
            re.search(
                r"(?is)(?:证据优先任务模式|并不来自任何本地文件|无法进行复核取证|必须明确说明无法进行复核取证)",
                original,
            )
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
        if inline_followup_context and not attachment_metas:
            cleaned = re.sub(
                r"(?is)[^。；\n]*(?:请(?:先)?粘贴原文|请(?:先)?贴原文|请(?:先)?把原文贴|请(?:先)?提供原文(?:片段)?|paste the original|provide the original text)[^。；\n]*[。；]?",
                "",
                cleaned,
            )
        if (
            not attachment_metas
            and (
                inline_followup_context
                or self._looks_like_context_dependent_followup(user_message)
                or self._looks_like_table_reformat_request(user_message)
            )
        ):
            cleaned = re.sub(
                r"(?is)[^。；\n]*(?:证据优先任务模式|并不来自任何本地文件|无法进行复核取证|必须明确说明无法进行复核取证)[^。；\n]*[。；]?",
                "",
                cleaned,
            )

        if local_access_succeeded:
            cleaned = re.sub(r"(?is)[^。；\n]*(?:无法访问用户本地路径|无法访问本地路径|无法读取用户本地路径)[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*(?:没有提供可供工具读取|未提供可供工具读取|没有提供本地文件路径|未提供本地文件路径)[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*无法进行可复核的解析[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*请(?:提供|给出|补充).{0,50}路径[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*(?:回复.?同意继续|请回复.?同意继续|需要你同意继续|需要你回复同意继续|同意继续后)[^。；\n]*[。；]?", "", cleaned)

        if self._request_likely_requires_tools(user_message, attachment_metas):
            cleaned = re.sub(r"(?is)[^。；\n]*(?:是否可直接访问|是否可以直接访问|是否能直接访问|可直接访问的目录|可访问的目录)[^。；\n]*[。；]?", "", cleaned)
            cleaned = re.sub(r"(?is)[^。；\n]*workbench[^。；\n]*(?:可直接访问|直接访问|访问目录)[^。；\n]*[。；]?", "", cleaned)
        if had_unverified_fulltext_claim:
            cleaned = re.sub(
                r"(?is)[^。；\n]*(?:全文搜索|全文检索|对全文进行了搜索|全文件搜索|full[-\s]?text search|searched the entire|whole document search)[^。；\n]*[。；]?",
                "",
                cleaned,
            )

        inferred_bare_tool = self._infer_bare_tool_call_from_text(cleaned)
        bare_tool_like_json = self._looks_like_bare_tool_arguments_text(cleaned)
        explicit_json_requested = self._user_explicitly_requests_json_output(user_message)
        had_unexpected_json_answer = False
        if inferred_bare_tool or bare_tool_like_json:
            cleaned = ""
        elif not explicit_json_requested:
            standalone_json_answer = self._extract_standalone_json_answer(cleaned)
            if standalone_json_answer:
                had_unexpected_json_answer = True
                cleaned = self._render_json_answer_for_user(standalone_json_answer).strip()

        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if not cleaned:
            if not attachment_metas and self._looks_like_inline_document_payload(user_message) and had_internal_meta:
                return "已按你直接粘贴的原始文本内容理解，不需要额外提供本地文件路径。请继续指定你要我解释的结构、字段或结论。"
            if inline_followup_context and had_permission_gate:
                return "我会直接基于你上一轮已贴的原文继续处理，不需要你重复粘贴原文。请继续告诉我你要翻译/提炼的范围。"
            if had_inline_evidence_gate:
                return "我会直接基于你当前会话里已提供的文本继续整理，不需要本地路径或页码。请告诉我你想要的表格列和排序方式。"
            if local_access_succeeded and (had_path_denial or had_permission_gate or had_internal_meta):
                return "我已经能访问你授权的本地路径，不需要你重复提供路径或再次授权。请直接继续说明要看的函数、文件或上下文，我会继续读取并给出结果。"
            if inferred_bare_tool or bare_tool_like_json:
                if self._request_likely_requires_tools(user_message, attachment_metas):
                    return "我上一条误输出了工具参数而不是最终结果。请直接重试同一句，我会继续检索并返回结论。"
                return "我上一条误输出了结构化 JSON，而不是可读答复。请直接重试同一句，我会改成正常文本。"
            if had_unexpected_json_answer:
                return "我上一条误输出了结构化 JSON，而不是可读答复。请直接重试同一句，我会改成正常文本。"
            if had_unverified_fulltext_claim:
                return "这轮我还没有执行完整关键词检索，不能直接声称已做全文搜索。若你要定位出处，我会先检索并给出页码/片段。"
            return original
        return cleaned

    def _run_answer_structurer(
        self,
        *,
        requested_model: str,
        final_text: str,
        citations: list[dict[str, Any]],
        reviewer_brief: RoleResult | dict[str, Any],
        conflict_brief: RoleResult | dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        context = self._make_role_context(
            "structurer",
            requested_model=requested_model,
            reviewer_brief=self._role_payload_dict(reviewer_brief),
            conflict_brief=self._role_payload_dict(conflict_brief),
            response_text=final_text,
            extra={"citations": citations},
        )
        result = self._run_answer_structurer_role(context=context)
        return result.payload, result.raw_text

    def _run_answer_structurer_role(self, *, context: RoleContext) -> RoleResult:
        spec = self._make_role_spec(
            "structurer",
            description="把最终答复整理成结构化证据包。",
            output_keys=["summary", "claims", "warnings", "citations"],
        )
        citations = list(context.extra.get("citations") or [])
        reviewer_payload = context.reviewer_brief
        conflict_payload = context.conflict_brief
        fallback = self._fallback_answer_bundle(
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
                    f"excerpt={self._shorten(item.get('excerpt') or '', 260)}",
                    f"warning={item.get('warning') or '(none)'}",
                    "",
                ]
            )
        structurer_input = "\n".join(
            [
                f"final_answer:\n{context.response_text.strip() or '(empty)'}",
                f"reviewer_verdict={str(reviewer_payload.get('verdict') or 'pass').strip()}",
                "reviewer_risks:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_payload.get("risks") or [], limit=4)],
                "reviewer_followups:",
                *[f"- {item}" for item in self._normalize_string_list(reviewer_payload.get("followups") or [], limit=4)],
                f"conflict_summary={str(conflict_payload.get('summary') or '').strip() or '(none)'}",
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
                model=context.requested_model,
                max_output_tokens=1400,
                enable_tools=False,
            )
            raw_text = self._content_to_text(getattr(ai_msg, "content", "")).strip()
            parsed = self._parse_json_object(raw_text)
            if not parsed:
                fallback["notes"] = ["Structurer 未返回标准 JSON，已使用后端降级结构化结果。", *notes]
                fallback["usage"] = self._extract_usage_from_message(ai_msg)
                fallback["effective_model"] = effective_model
                bundle = self._strip_answer_bundle_meta(fallback)
                bundle["usage"] = fallback["usage"]
                bundle["effective_model"] = fallback["effective_model"]
                bundle["notes"] = fallback["notes"]
                return self._make_role_result(spec, context, bundle, raw_text)

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
            bundle = self._strip_answer_bundle_meta(bundle) | {
                "usage": bundle["usage"],
                "effective_model": bundle["effective_model"],
                "notes": bundle["notes"],
            }
            return self._make_role_result(spec, context, bundle, raw_text)
        except Exception as exc:
            fallback["notes"] = [f"Structurer 调用失败，已回退后端结构化结果: {self._shorten(exc, 180)}"]
            raw_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            bundle = self._strip_answer_bundle_meta(fallback) | {
                "usage": fallback["usage"],
                "effective_model": fallback["effective_model"],
                "notes": fallback["notes"],
            }
            return self._make_role_result(spec, context, bundle, raw_text)

    def _fallback_answer_bundle(
        self,
        *,
        final_text: str,
        citations: list[dict[str, Any]],
        reviewer_brief: RoleResult | dict[str, Any],
        conflict_brief: RoleResult | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reviewer_payload = self._role_payload_dict(reviewer_brief)
        conflict_payload = self._role_payload_dict(conflict_brief)
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
            list(reviewer_payload.get("risks") or [])
            + list(reviewer_payload.get("followups") or [])
            + list(conflict_payload.get("concerns") or []),
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

    def _is_evidence_verification_mode(
        self,
        *,
        route: dict[str, Any],
        evidence_required_mode: bool,
        spec_lookup_request: bool,
    ) -> bool:
        if evidence_required_mode or spec_lookup_request:
            return True
        task_type = str(route.get("task_type") or "").strip().lower()
        return task_type in {"evidence_lookup", "attachment_tooling", "web_research"}

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
        short_followup_search = self._looks_like_short_followup_search(current)
        short_execution_ack = self._looks_like_short_followup_execution_ack(current)
        context_dependent_followup = self._looks_like_context_dependent_followup(current)

        # Inline code/test payloads in follow-up turns often carry raw content only.
        # If prior user turn is clearly "modify/generate code", inherit that intent.
        current_is_inline_code_payload = self._looks_like_inline_code_payload(current)
        if current_is_inline_code_payload and not self._looks_like_code_generation_request(current, []):
            for turn in reversed(history_turns):
                role = str(turn.get("role") or "").strip().lower()
                text = str(turn.get("text") or "").strip()
                if role != "user" or not text or text == current:
                    continue
                compact_text = " ".join(text.split())
                if self._looks_like_code_generation_request(text, []) or self._looks_like_local_code_lookup_request(text, []):
                    return self._shorten(compact_text, 280)

        if not short_followup_search and not short_execution_ack and not context_dependent_followup:
            return ""

        if context_dependent_followup:
            recent_inline_payload = self._find_recent_user_inline_payload_for_followup(
                history_turns=history_turns,
                current_message=current,
            )
            if recent_inline_payload:
                return self._shorten(recent_inline_payload.strip(), 520)
            for turn in reversed(history_turns):
                role = str(turn.get("role") or "").strip().lower()
                text = str(turn.get("text") or "").strip()
                if not text or text == current:
                    continue
                compact_text = " ".join(text.split())
                if role == "assistant" and len(compact_text) >= 40:
                    return self._shorten(compact_text, 320)
                if role == "user" and len(compact_text) >= 20:
                    return self._shorten(compact_text, 320)

        for turn in reversed(history_turns):
            role = str(turn.get("role") or "").strip().lower()
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            if text == current:
                continue
            compact_text = " ".join(text.split())
            if role == "user":
                if short_execution_ack and not self._request_likely_requires_tools(text, []):
                    if len(compact_text) <= 80:
                        continue
                return self._shorten(compact_text, 280)
            if (
                role == "assistant"
                and short_execution_ack
                and self._request_likely_requires_tools(text, [])
                and not self._looks_like_permission_gate_text(text, has_attachments=False, request_requires_tools=True)
                and not self._looks_like_local_path_denial(text)
            ):
                return self._shorten(compact_text, 280)
        return ""

    def _find_recent_user_inline_payload_for_followup(
        self,
        *,
        history_turns: list[dict[str, Any]],
        current_message: str,
    ) -> str:
        current = str(current_message or "").strip()
        for turn in reversed(history_turns):
            role = str(turn.get("role") or "").strip().lower()
            if role != "user":
                continue
            text = str(turn.get("text") or "").strip()
            if not text or text == current:
                continue
            if self._looks_like_inline_document_payload(text):
                return text
            if re.search(r"(?m)^\s*\|.+\|\s*$", text) and text.count("\n") >= 1:
                return text
            if "\t" in text and text.count("\n") >= 1 and len(text) >= 40:
                return text
            if re.search(r"(?m)^.{2,}\s{2,}.{2,}$", text) and text.count("\n") >= 2 and len(text) >= 60:
                return text
            if len(text) >= 260 and text.count("\n") >= 4:
                return text
            if len(text) >= 420 and not self._message_has_explicit_local_path(text):
                return text
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

    def _looks_like_short_followup_execution_ack(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        if self._looks_like_explicit_tool_confirmation(lowered):
            return True
        compact = re.sub(r"[\s\"'`“”‘’.,!?，。！？、;；:：()\[\]{}<>《》【】/\\|-]+", "", lowered)
        if not compact or len(compact) > 16:
            return False
        return compact in _FOLLOWUP_EXECUTION_ACK_HINTS

    def _looks_like_write_or_edit_action(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        write_hints = (
            "写入",
            "落盘",
            "保存",
            "覆盖写入",
            "覆盖",
            "替换",
            "更新",
            "改成",
            "改为",
            "改一下",
            "修一下",
            "修复",
            "修改",
            "apply",
            "patch",
            "write back",
            "overwrite",
            "save",
            "update",
            "replace",
            "edit",
        )
        if any(hint in lowered for hint in write_hints):
            return True
        if re.search(r"(?:把|将).{0,40}(?:改成|改为|替换成|替换为|更新为)", lowered):
            return True
        if re.search(r"(?:replace|update|edit).{0,40}(?:with|to)", lowered):
            return True
        return False

    def _looks_like_explicit_tool_confirmation(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        compact = re.sub(r"[\s\"'`“”‘’.,!?，。！？、;；:：()\[\]{}<>《》【】/\\|-]+", "", lowered)
        if compact in _FOLLOWUP_EXECUTION_ACK_HINTS:
            return True
        if len(compact) > 220:
            return False
        if self._looks_like_write_or_edit_action(lowered) and len(compact) <= 48:
            return True

        confirm_hints = (
            "确认",
            "同意",
            "可以",
            "继续",
            "开始",
            "执行",
            "ok",
            "yes",
            "confirm",
            "proceed",
        )
        action_hints = (
            "读取",
            "读",
            "搜索",
            "检索",
            "查",
            "修改",
            "修复",
            "写入",
            "替换",
            "更新",
            "保存",
            "定位",
            "打开",
            "查看",
            "read",
            "search",
            "scan",
            "modify",
            "replace",
            "update",
            "write",
            "save",
            "edit",
            "open",
        )
        has_confirm = any(hint in lowered for hint in confirm_hints)
        has_action = any(hint in lowered for hint in action_hints)
        if has_confirm and has_action:
            return True
        if has_confirm and self._message_has_explicit_local_path(lowered):
            return True
        if re.search(r"(?:确认|同意).{0,20}(?:读取|搜索|执行|修改|定位|打开|查看)", lowered):
            return True
        if re.search(r"(?:confirm|proceed).{0,30}(?:read|search|execute|modify|open)", lowered):
            return True
        return False

    def _has_recent_assistant_code_preview(self, history_turns: list[dict[str, Any]], *, max_messages: int = 6) -> bool:
        seen = 0
        for turn in reversed(history_turns):
            role = str(turn.get("role") or "").strip().lower()
            if role != "assistant":
                continue
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            seen += 1
            if re.search(r"```[A-Za-z0-9_+.-]*\n[\s\S]{20,}?```", text):
                return True
            if seen >= max(1, int(max_messages)):
                break
        return False

    def _should_force_write_from_previous_preview(
        self,
        *,
        user_message: str,
        history_turns: list[dict[str, Any]],
        attachment_metas: list[dict[str, Any]],
    ) -> bool:
        if attachment_metas:
            return False
        current = str(user_message or "").strip()
        if not current:
            return False
        if self._looks_like_inline_code_payload(current):
            return False
        if not self._looks_like_write_or_edit_action(current):
            return False
        compact = re.sub(r"[\s\"'`“”‘’.,!?，。！？、;；:：()\[\]{}<>《》【】/\\|-]+", "", current.lower())
        if not compact:
            return False
        if len(compact) > 80:
            return False
        return self._has_recent_assistant_code_preview(history_turns)

    def _looks_like_context_dependent_followup(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        if self._looks_like_inline_document_payload(lowered):
            return False
        if self._looks_like_inline_code_payload(lowered):
            return False
        has_reference = any(hint in lowered for hint in _FOLLOWUP_REFERENCE_HINTS)
        has_transform = any(hint in lowered for hint in _FOLLOWUP_TRANSFORM_HINTS)
        if has_reference and has_transform:
            return True
        if (
            len(lowered) <= 100
            and has_transform
            and any(token in lowered for token in ("再", "继续", "接着", "重新", "再次", "again"))
            and any(token in lowered for token in ("表格", "table", "原文", "文本", "内容", "这版", "上一版", "这个"))
        ):
            return True
        if has_reference and len(lowered) <= 120 and ("文档" in lowered or "版本" in lowered or "总结" in lowered):
            return True
        return False

    def _message_has_explicit_local_path(self, text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        return bool(re.search(r"(?:^|[\s(])(?:/[^\s]+|[A-Za-z][:\\：][\\/][^\s]*)", raw))

    def _has_file_like_lookup_token(self, text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        tokens = re.findall(r"[a-z0-9][a-z0-9._-]{4,}", raw)
        tld_like_suffixes = (".com", ".cn", ".net", ".org", ".jp", ".io", ".dev")
        code_exts = {
            "c",
            "cc",
            "cpp",
            "cxx",
            "h",
            "hpp",
            "hh",
            "py",
            "js",
            "jsx",
            "ts",
            "tsx",
            "java",
            "go",
            "rs",
            "swift",
            "kt",
            "rb",
            "php",
            "sh",
            "ps1",
            "yaml",
            "yml",
            "json",
            "xml",
            "toml",
            "ini",
            "cfg",
            "md",
            "txt",
        }
        for token in tokens:
            if token.startswith(("http://", "https://", "www.")):
                continue
            if token.endswith(tld_like_suffixes):
                continue
            if "_" in token and len(token) >= 6:
                return True
            if "." in token:
                stem, _, suffix = token.rpartition(".")
                if stem and suffix in code_exts:
                    return True
        return False

    def _should_auto_search_default_roots(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        if attachment_metas:
            return False
        if self._looks_like_inline_document_payload(user_message):
            return False
        text = str(user_message or "").strip().lower()
        if not text:
            return False
        if self._message_has_explicit_local_path(user_message):
            return False
        if "http://" in text or "https://" in text or any(hint in text for hint in _NEWS_HINTS):
            return False

        search_verbs = (
            "找",
            "查",
            "搜",
            "搜索",
            "查找",
            "定位",
            "look for",
            "find",
            "search",
            "locate",
        )
        local_targets = (
            "函数",
            "方法",
            "代码",
            "源码",
            "测试",
            "用例",
            "文件",
            "目录",
            "文件夹",
            "项目",
            "仓库",
            "repo",
            "master",
            "source",
            "src",
            "test",
            "tests",
            "case",
            "实现",
            "定义",
            "声明",
            "调用点",
            "module",
            "function",
            "method",
            "file",
            "directory",
            "folder",
            "implementation",
        )
        has_lookup_verb = any(verb in text for verb in search_verbs)
        has_local_target = any(target in text for target in local_targets)
        return has_lookup_verb and (has_local_target or self._has_file_like_lookup_token(text))

    def _looks_like_local_code_lookup_request(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        if attachment_metas:
            return False
        if self._looks_like_inline_document_payload(user_message):
            return False
        text = str(user_message or "").strip().lower()
        if not text:
            return False
        if "http://" in text or "https://" in text or any(hint in text for hint in _NEWS_HINTS):
            return False

        local_scope_hints = (
            "路径",
            "目录",
            "文件夹",
            "目录下",
            "文件夹下",
            "路径下",
            "项目",
            "仓库",
            "repo",
            "workbench",
            "workspace",
            "master",
            "source",
            "src",
            "test",
            "tests",
            "folder",
            "directory",
            "repo",
            "project",
        )
        code_target_hints = (
            "函数",
            "方法",
            "实现",
            "定义",
            "声明",
            "调用点",
            "测试",
            "用例",
            "测试文件",
            "文件名",
            "module",
            "function",
            "method",
            "test",
            "case",
            "filename",
            "file name",
            "implementation",
            "call site",
            "definition",
        )
        lookup_hints = (
            "找",
            "查",
            "搜",
            "搜索",
            "查找",
            "定位",
            "解释",
            "分析",
            "说明",
            "梳理",
            "看看",
            "看下",
            "看一下",
            "look for",
            "find",
            "search",
            "locate",
            "explain",
            "analyze",
        )
        has_local_scope = self._message_has_explicit_local_path(user_message) or any(hint in text for hint in local_scope_hints)
        has_code_target = any(hint in text for hint in code_target_hints)
        has_lookup_intent = any(hint in text for hint in lookup_hints)
        has_file_like_token = self._has_file_like_lookup_token(text)
        return (
            has_lookup_intent
            and (has_code_target or has_file_like_token)
            and (has_local_scope or self._should_auto_search_default_roots(user_message, attachment_metas))
        )

    def _looks_like_code_generation_request(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        text = str(user_message or "").strip().lower()
        if not text:
            return False

        generation_hints = (
            "生成",
            "创建",
            "新建",
            "改",
            "修",
            "写",
            "编写",
            "实现",
            "开发",
            "补全",
            "重构",
            "改写",
            "修改",
            "修复",
            "替换",
            "更新",
            "写入",
            "保存",
            "generate",
            "create",
            "write",
            "implement",
            "build",
            "scaffold",
            "refactor",
            "rewrite",
            "modify",
            "fix",
            "replace",
            "update",
            "edit",
        )
        code_target_hints = (
            "代码",
            "函数",
            "类",
            "组件",
            "页面",
            "接口",
            "脚本",
            "测试",
            "单元测试",
            "模块",
            "变量",
            "参数",
            "字段",
            "头文件",
            "header",
            ".h",
            ".hpp",
            "plugin",
            "component",
            "page",
            "api",
            "endpoint",
            "script",
            "test",
            "class",
            "function",
            "module",
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".java",
            ".go",
            ".rs",
            ".cpp",
            ".c",
        )
        lookup_only_hints = (
            "找",
            "查",
            "搜",
            "搜索",
            "查找",
            "定位",
            "explain",
            "解释",
            "look for",
            "find",
            "search",
            "locate",
        )

        has_generation_intent = any(hint in text for hint in generation_hints)
        if not has_generation_intent:
            return False
        has_code_target = any(hint in text for hint in code_target_hints)
        if not has_code_target and not attachment_metas:
            return False
        if any(hint in text for hint in lookup_only_hints) and not any(
            hint in text
            for hint in (
                "生成",
                "创建",
                "新建",
                "改",
                "修",
                "写",
                "编写",
                "实现",
                "开发",
                "补全",
                "重构",
                "改写",
                "修改",
                "修复",
                "替换",
                "更新",
                "写入",
                "保存",
                "generate",
                "create",
                "write",
                "implement",
                "build",
                "scaffold",
                "refactor",
                "rewrite",
                "modify",
                "fix",
                "replace",
                "update",
                "edit",
            )
        ):
            return False
        return True

    def _should_force_initial_tool_execution(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        if attachment_metas:
            return False
        if self._looks_like_inline_document_payload(user_message):
            return False
        if self._looks_like_local_code_lookup_request(user_message, attachment_metas):
            return True
        if self._should_auto_search_default_roots(user_message, attachment_metas):
            return self._request_likely_requires_tools(user_message, attachment_metas)
        return False

    def _should_force_tool_followup_continuation(
        self,
        *,
        current_message: str,
        followup_topic_hint: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
    ) -> bool:
        if not settings.enable_tools:
            return False
        if attachment_metas:
            return False
        if not self._looks_like_explicit_tool_confirmation(current_message):
            return False
        topic = str(followup_topic_hint or "").strip()
        if topic and self._request_likely_requires_tools(topic, attachment_metas):
            return True
        return self._request_likely_requires_tools(current_message, attachment_metas)

    def _estimate_char_offset_for_line(
        self,
        path: str,
        line_number: int,
        *,
        context_lines_before: int = 40,
    ) -> int:
        real_path = Path(str(path or "")).expanduser()
        if not real_path.exists() or line_number <= 1:
            return 0
        try:
            lines = real_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        except Exception:
            return 0
        start_line = max(1, int(line_number) - max(0, int(context_lines_before)))
        return sum(len(chunk) for chunk in lines[: start_line - 1])

    def _coordinator_auto_read_code_search_matches(
        self,
        result: dict[str, Any],
        *,
        limit: int = 2,
        max_chars: int = 24000,
    ) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or not bool(result.get("ok")):
            return []
        matches = list(result.get("matches") or [])
        if not matches:
            return []
        out: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for match in matches:
            if not isinstance(match, dict):
                continue
            path = str(match.get("path") or "").strip()
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                line_no = int(match.get("line") or 0)
            except Exception:
                line_no = 0
            out.append(
                {
                    "name": "read_text_file",
                    "args": {
                        "path": path,
                        "start_char": self._estimate_char_offset_for_line(path, line_no, context_lines_before=50),
                        "max_chars": max(4000, min(int(max_chars), 50000)),
                    },
                    "source_line": line_no,
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    def _tool_events_have_code_hits(self, tool_events: list[ToolEvent]) -> bool:
        for event in tool_events:
            if str(getattr(event, "name", "") or "") != "search_codebase":
                continue
            preview = str(getattr(event, "output_preview", "") or "")
            parsed = self._parse_json_object(preview) or self._parse_loose_object_literal(preview)
            if not isinstance(parsed, dict):
                continue
            try:
                match_count = int(parsed.get("match_count") or len(parsed.get("matches") or []))
            except Exception:
                match_count = 0
            if bool(parsed.get("ok")) and match_count > 0:
                return True
        return False

    def _answer_incorrectly_denies_code_hits(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        patterns = (
            "没有真实的代码命中",
            "没有代码命中",
            "没有任何真实的代码命中",
            "没有找到代码",
            "未找到代码",
            "没有出现任何真实的代码命中内容",
            "no real code hit",
            "no code hit",
            "did not find code",
            "no matching code",
        )
        return any(pattern in lowered for pattern in patterns)

    def _reviewer_requests_more_evidence(self, reviewer_brief: RoleResult | dict[str, Any]) -> bool:
        reviewer_payload = self._role_payload_dict(reviewer_brief)
        lines = [
            str(reviewer_payload.get("summary") or "").strip(),
            *self._normalize_string_list(reviewer_payload.get("risks") or [], limit=6, item_limit=220),
            *self._normalize_string_list(reviewer_payload.get("followups") or [], limit=6, item_limit=220),
            *self._normalize_string_list(reviewer_payload.get("readonly_evidence") or [], limit=6, item_limit=220),
        ]
        lowered = " ".join(line.lower() for line in lines if line).strip()
        if not lowered:
            return False
        patterns = (
            "继续读取",
            "继续读",
            "继续取证",
            "需要更多上下文",
            "上下文不足",
            "未读完整",
            "还没读完",
            "只读了一部分",
            "仅读取了部分",
            "需要继续查看",
            "需要继续向后读取",
            "read more",
            "need more context",
            "partial read",
            "partial context",
            "insufficient context",
            "continue reading",
        )
        return any(pattern in lowered for pattern in patterns)

    def _coordinator_collect_truncated_read_requests(
        self,
        tool_events: list[ToolEvent],
        *,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in reversed(tool_events):
            if str(getattr(event, "name", "") or "") != "read_text_file":
                continue
            preview = str(getattr(event, "output_preview", "") or "")
            parsed = self._parse_json_object(preview) or self._parse_loose_object_literal(preview)
            if not isinstance(parsed, dict) or not bool(parsed.get("ok")):
                continue
            if not bool(parsed.get("has_more") or parsed.get("truncated")):
                continue
            args = getattr(event, "input", None) if isinstance(getattr(event, "input", None), dict) else {}
            path = str((args or {}).get("path") or parsed.get("path") or "").strip()
            if not path:
                continue
            try:
                next_start = int(parsed.get("end_char") or 0)
            except Exception:
                next_start = 0
            try:
                total_length = int(parsed.get("total_length") or 0)
            except Exception:
                total_length = 0
            try:
                next_max = int((args or {}).get("max_chars") or parsed.get("length") or 0)
            except Exception:
                next_max = 0
            next_max = max(4000, min(next_max or 24000, 50000))
            if total_length and next_start >= total_length:
                continue
            key = f"{path}:{next_start}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": "read_text_file",
                    "args": {
                        "path": path,
                        "start_char": max(0, next_start),
                        "max_chars": next_max,
                    },
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    def _coordinator_should_rerun_worker_after_reviewer(
        self,
        *,
        route: dict[str, Any],
        reviewer_brief: RoleResult | dict[str, Any],
        tool_events: list[ToolEvent],
    ) -> bool:
        reviewer_payload = self._role_payload_dict(reviewer_brief)
        verdict = str(reviewer_payload.get("verdict") or "pass").strip().lower()
        if verdict not in {"warn", "block"}:
            return False
        task_type = str(route.get("task_type") or "").strip()
        truncated_reads = self._coordinator_collect_truncated_read_requests(tool_events, limit=1)
        wants_more = self._reviewer_requests_more_evidence(reviewer_brief)
        if task_type == "code_lookup":
            return bool(truncated_reads) and (wants_more or self._tool_events_have_code_hits(tool_events))
        if task_type in {"evidence_lookup", "attachment_tooling"}:
            return bool(truncated_reads) and wants_more
        return False

    def _coordinator_init_state(
        self,
        *,
        route: dict[str, Any],
        settings: ChatSettings,
        force_tool_followup: bool = False,
    ) -> ExecutionState:
        task_type = str(route.get("task_type") or "standard").strip() or "standard"
        complexity = str(route.get("complexity") or "medium").strip().lower() or "medium"
        if not settings.enable_tools:
            tool_mode = "off"
        elif force_tool_followup:
            tool_mode = "forced"
        elif bool(route.get("use_worker_tools")):
            tool_mode = "on"
        else:
            tool_mode = "auto"
        state = ExecutionState(
            task_type=task_type,
            complexity=complexity,
            tool_mode=tool_mode,
            tool_latch=tool_mode in {"on", "forced"},
            status="ready",
        )
        state.transitions.append(f"init:{tool_mode}")
        return state

    def _coordinator_tools_enabled(self, state: ExecutionState) -> bool:
        return state.tool_mode in {"on", "forced"}

    def _coordinator_apply_tool_mode(
        self,
        *,
        state: ExecutionState,
        route: dict[str, Any],
        settings: ChatSettings,
        tool_mode: str,
        reason: str,
        summary: str,
        use_planner: bool | None = None,
    ) -> dict[str, Any]:
        normalized_mode = str(tool_mode or "auto").strip().lower()
        if normalized_mode not in {"off", "auto", "on", "forced"}:
            normalized_mode = "auto"
        if state.tool_latch and normalized_mode in {"off", "auto"}:
            normalized_mode = "on" if state.tool_mode in {"on", "forced"} else "on"
        state.tool_mode = normalized_mode
        if normalized_mode in {"on", "forced"}:
            state.tool_latch = True
        state.task_type = str(route.get("task_type") or state.task_type or "standard").strip() or "standard"
        state.complexity = str(route.get("complexity") or state.complexity or "medium").strip().lower() or "medium"
        state.transitions.append(reason)
        update: dict[str, Any] = {
            "task_type": state.task_type,
            "complexity": state.complexity,
            "use_worker_tools": self._coordinator_tools_enabled(state),
            "reason": reason,
            "summary": summary,
        }
        if use_planner is not None:
            update["use_planner"] = bool(use_planner)
        return self._normalize_route_decision(update, fallback=route, settings=settings)

    def _coordinator_summary(self, state: ExecutionState) -> str:
        tool_state = f"tool_mode={state.tool_mode}"
        if state.tool_latch:
            tool_state += ", latched"
        return f"Coordinator 正在管理运行时状态（{tool_state}，attempts={state.attempts}/{state.max_attempts}）。"

    def _coordinator_panel_bullets(self, state: ExecutionState) -> list[str]:
        base = [
            f"task_type: {state.task_type}",
            f"complexity: {state.complexity}",
            f"tool_mode: {state.tool_mode}",
            f"tool_latch: {str(state.tool_latch).lower()}",
            f"attempts: {state.attempts}/{state.max_attempts}",
        ]
        transitions = [f"transition: {item}" for item in state.transitions[-3:]]
        return self._normalize_string_list([*base, *transitions], limit=8, item_limit=220)

    def _run_pipeline_hook(self, phase: str, **kwargs: Any) -> dict[str, Any]:
        phase_key = str(phase or "").strip()
        handler_name = str(PIPELINE_HOOK_HANDLERS.get(phase_key) or "").strip()
        if not handler_name:
            raise ValueError(f"Unknown pipeline hook phase: {phase_key or '(empty)'}")
        handler = getattr(self, handler_name, None)
        if not callable(handler):
            raise ValueError(f"Pipeline hook handler missing: {handler_name}")
        raw_result = handler(**kwargs)
        if isinstance(raw_result, HookResult):
            raw_result = {
                field.name: getattr(raw_result, field.name)
                for field in dataclass_fields(HookResult)
            }
        elif is_dataclass(raw_result):
            raw_result = {
                field.name: getattr(raw_result, field.name)
                for field in dataclass_fields(raw_result)
            }
        if not isinstance(raw_result, dict):
            raise ValueError(f"Pipeline hook {phase_key} returned non-dict payload")

        normalized = dict(raw_result)
        normalized["phase"] = phase_key
        normalized["trace_notes"] = self._normalize_string_list(
            normalized.get("trace_notes") or [],
            limit=8,
            item_limit=220,
        )

        debug_entries: list[dict[str, str]] = []
        for item in list(normalized.get("debug_entries") or [])[:8]:
            if is_dataclass(item):
                item = asdict(item)
            if not isinstance(item, dict):
                continue
            debug_entries.append(
                {
                    "stage": str(item.get("stage") or "backend_hook").strip() or "backend_hook",
                    "title": self._shorten(str(item.get("title") or f"Hook({phase_key})").strip(), 120),
                    "detail": self._shorten(str(item.get("detail") or "").strip(), 4000),
                }
            )
        normalized["debug_entries"] = debug_entries

        prompt_injections: list[dict[str, str]] = []
        for item in list(normalized.get("prompt_injections") or [])[:8]:
            if is_dataclass(item):
                item = asdict(item)
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            position = str(item.get("position") or "append").strip().lower()
            if position not in {"front", "append"}:
                position = "append"
            prompt_injections.append(
                {
                    "position": position,
                    "title": self._shorten(str(item.get("title") or f"Hook({phase_key}) 注入提示").strip(), 120),
                    "content": content,
                    "trace_note": self._shorten(str(item.get("trace_note") or "").strip(), 220),
                }
            )
        normalized["prompt_injections"] = prompt_injections
        return normalized

    def _build_pipeline_hook_telemetry(
        self,
        *,
        phase: str,
        hook_payload: dict[str, Any],
        route_before: dict[str, Any] | None = None,
        route_after: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_pipeline_hook_telemetry(
            phase=phase,
            handler_name=str(PIPELINE_HOOK_HANDLERS.get(str(phase or "").strip()) or ""),
            hook_payload=hook_payload,
            route_before=route_before,
            route_after=route_after,
        )

    def _apply_pipeline_hook_effects(
        self,
        *,
        hook_payload: dict[str, Any],
        messages: list[Any] | None = None,
        add_trace: Callable[[str], None] | None = None,
        add_debug: Callable[[str, str, str], None] | None = None,
    ) -> None:
        for note in hook_payload.get("trace_notes") or []:
            if add_trace:
                add_trace(str(note))
        for item in hook_payload.get("debug_entries") or []:
            if add_debug and isinstance(item, dict):
                add_debug(
                    str(item.get("stage") or "backend_hook"),
                    str(item.get("title") or "Hook"),
                    str(item.get("detail") or ""),
                )
        if messages is None:
            return
        for injection in hook_payload.get("prompt_injections") or []:
            if not isinstance(injection, dict):
                continue
            message = self._SystemMessage(content=str(injection.get("content") or ""))
            if str(injection.get("position") or "append") == "front":
                messages.insert(1, message)
            else:
                messages.append(message)
            trace_note = str(injection.get("trace_note") or "").strip()
            if trace_note and add_trace:
                add_trace(trace_note)
            if add_debug:
                add_debug(
                    "backend_hook",
                    str(injection.get("title") or "Hook 注入提示"),
                    str(injection.get("content") or ""),
                )

    def _hook_before_route_finalize(
        self,
        *,
        route: dict[str, Any],
        router_raw: str,
        planner_user_message: str,
        attachment_issues: list[str],
        followup_has_attachments: bool,
        followup_attachment_requires_tools: bool,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
    ) -> HookResult:
        updated_route = dict(route or {})
        updated_raw = str(router_raw or "")
        trace_notes: list[str] = []
        debug_entries: list[HookDebugEntry] = []

        attachment_context_incomplete = any(
            ("未结构化解析" in str(issue)) or ("文档解析失败" in str(issue))
            for issue in (attachment_issues or [])
        )
        if (
            attachment_context_incomplete
            and settings.enable_tools
            and not updated_route.get("use_worker_tools")
            and self._looks_like_understanding_request(planner_user_message)
        ):
            updated_route = self._normalize_route_decision(
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
                    "reason": "backend_attachment_context_incomplete_requires_tooling",
                    "summary": "检测到附件仅注入了预览或解析失败，Coordinator 已切回 Worker 工具链先完成读取。",
                    "source": "backend_override",
                },
                fallback=updated_route,
                settings=settings,
            )
            updated_raw = json.dumps(
                {
                    "source": "backend_override",
                    "reason": "attachment_context_incomplete_requires_tooling",
                    "task_type": updated_route.get("task_type"),
                },
                ensure_ascii=False,
            )
            trace_notes.append("Hook(before_route_finalize): 附件仅有预览或解析失败，已改走 attachment_tooling。")
            debug_entries.append(
                HookDebugEntry(
                    stage="backend_hook",
                    title="Hook(before_route_finalize) 切换附件工具链",
                    detail="reason=attachment_context_incomplete_requires_tooling",
                )
            )

        if followup_has_attachments and settings.enable_tools and not updated_route.get("use_worker_tools"):
            updated_route = self._normalize_route_decision(
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
                    "reason": (
                        "backend_followup_attachment_requires_tooling"
                        if followup_attachment_requires_tools
                        else "backend_followup_attachment_prefers_worker_tooling"
                    ),
                    "summary": (
                        "跟进轮附件本轮仅提供路径，Coordinator 已强制启用 Worker 工具链继续读取。"
                        if followup_attachment_requires_tools
                        else "检测到跟进轮新增附件，Coordinator 已优先切回 Worker 工具链，避免只基于预览或内联片段误判。"
                    ),
                    "source": "backend_override",
                },
                fallback=updated_route,
                settings=settings,
            )
            updated_raw = json.dumps(
                {
                    "source": "backend_override",
                    "reason": (
                        "followup_attachment_requires_tooling"
                        if followup_attachment_requires_tools
                        else "followup_attachment_prefers_worker_tooling"
                    ),
                    "task_type": updated_route.get("task_type"),
                },
                ensure_ascii=False,
            )
            trace_notes.append("Hook(before_route_finalize): 跟进轮附件优先改走 Worker 工具链。")
            debug_entries.append(
                HookDebugEntry(
                    stage="backend_hook",
                    title="Hook(before_route_finalize) 跟进附件改走工具链",
                    detail=(
                        "reason=followup_attachment_requires_tooling"
                        if followup_attachment_requires_tools
                        else "reason=followup_attachment_prefers_worker_tooling"
                    ),
                )
            )
        return HookResult(
            route=updated_route,
            router_raw=updated_raw,
            trace_notes=trace_notes,
            debug_entries=debug_entries,
        )

    def _hook_before_worker_prompt(
        self,
        *,
        route: dict[str, Any],
        router_raw: str,
        execution_state: ExecutionState,
        planner_user_message: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        force_tool_followup: bool,
    ) -> HookResult:
        updated_route = dict(route or {})
        updated_raw = str(router_raw or "")
        trace_notes: list[str] = []
        debug_entries: list[HookDebugEntry] = []
        prompt_injections: list[HookPromptInjection] = []

        if force_tool_followup and not updated_route.get("use_worker_tools"):
            updated_route = self._coordinator_apply_tool_mode(
                state=execution_state,
                route=updated_route,
                settings=settings,
                tool_mode="forced",
                reason="followup_execution_ack_forces_tool_continuation",
                summary="检测到用户已明确授权继续执行上一轮工具任务，继续 Worker 工具链。",
                use_planner=True,
            )
            updated_raw = json.dumps(
                {
                    "source": "backend_override",
                    "reason": "followup_execution_ack_forces_tool_continuation",
                    "task_type": updated_route.get("task_type"),
                },
                ensure_ascii=False,
            )
            trace_notes.append("Hook(before_worker_prompt): 已识别为工具链续执行确认，继续 Worker 工具链。")
            debug_entries.append(
                HookDebugEntry(
                    stage="backend_hook",
                    title="Hook(before_worker_prompt) 切换工具模式",
                    detail=(
                        "reason=followup_execution_ack_forces_tool_continuation\n"
                        f"tool_mode={execution_state.tool_mode}\n"
                        f"tool_latch={str(execution_state.tool_latch).lower()}\n"
                        f"transitions={json.dumps(execution_state.transitions[-3:], ensure_ascii=False)}"
                    ),
                )
            )

        if settings.enable_tools and bool(updated_route.get("use_worker_tools")) and not self._coordinator_tools_enabled(execution_state):
            updated_route = self._coordinator_apply_tool_mode(
                state=execution_state,
                route=updated_route,
                settings=settings,
                tool_mode="forced" if force_tool_followup else "on",
                reason="backend_route_requires_worker_tools_sync",
                summary="Router/Coordinator 判定本轮需 Worker 工具链，已同步开启工具绑定。",
            )
            updated_raw = json.dumps(
                {
                    "source": "backend_override",
                    "reason": "route_requires_worker_tools_sync",
                    "task_type": updated_route.get("task_type"),
                },
                ensure_ascii=False,
            )
            trace_notes.append("Hook(before_worker_prompt): 已同步开启 Worker 工具绑定。")
            debug_entries.append(
                HookDebugEntry(
                    stage="backend_hook",
                    title="Hook(before_worker_prompt) 同步工具绑定",
                    detail=(
                        "reason=route_requires_worker_tools_sync\n"
                        f"tool_mode={execution_state.tool_mode}\n"
                        f"tool_latch={str(execution_state.tool_latch).lower()}\n"
                        f"transitions={json.dumps(execution_state.transitions[-3:], ensure_ascii=False)}"
                    ),
                )
            )

        router_system_hint = self._router_system_hint(updated_route)
        if router_system_hint:
            prompt_injections.append(
                HookPromptInjection(
                    position="front",
                    title="Hook(before_worker_prompt) 注入 Router 摘要",
                    content=router_system_hint,
                    trace_note="多 Role: Coordinator 已将 Router 摘要注入 Worker 请求。",
                )
            )

        runtime_profile_hint = build_runtime_profile_hint(updated_route)
        if runtime_profile_hint:
            prompt_injections.append(
                HookPromptInjection(
                    position="front",
                    title="Hook(before_worker_prompt) 注入 Runtime Profile",
                    content=runtime_profile_hint,
                    trace_note="多 Role: Coordinator 已将 runtime profile 注入 Worker 请求。",
                )
            )

        raw_spec_lookup_request = self._looks_like_spec_lookup_request(planner_user_message, attachment_metas)
        raw_evidence_required_mode = self._requires_evidence_mode(planner_user_message, attachment_metas)
        route_task_type = str(updated_route.get("task_type") or "").strip().lower()
        spec_lookup_request = raw_spec_lookup_request and route_task_type == "evidence_lookup"
        evidence_required_mode = raw_evidence_required_mode and route_task_type == "evidence_lookup"

        if spec_lookup_request:
            prompt_injections.append(
                HookPromptInjection(
                    position="append",
                    title="Hook(before_worker_prompt) 启用规范检索模式",
                    content=(
                        "本轮属于规范/规格书定位任务。"
                        "先用 search_text_in_file 对章节名、命令码、opcode 或寄存器名做命中定位，"
                        "必要时分别尝试章节关键词和 15h/15 h/0x15 这类十六进制变体；"
                        "再用 read_text_file 读取命中附近上下文；"
                        "最终回答必须附带命中证据。"
                        "若未命中，只能说当前提取文本未定位到，不得直接断言规范不存在。"
                    ),
                    trace_note="已启用规范文档检索模式。",
                )
            )
        if evidence_required_mode and updated_route.get("use_worker_tools"):
            prompt_injections.append(
                HookPromptInjection(
                    position="append",
                    title="Hook(before_worker_prompt) 启用证据优先模式",
                    content=(
                        "本轮已启用 evidence_required_mode。"
                        "对于文件、规范、代码库、章节定位类任务，必须给出证据来源（如路径、页码、章节、行号、命中片段）。"
                        "若证据不足，只能明确说明不足，不得给出无证据的确定性结论。"
                    ),
                    trace_note="已启用证据优先模式。",
                )
            )
        if updated_route.get("use_worker_tools") and self._should_auto_search_default_roots(planner_user_message, attachment_metas):
            prompt_injections.append(
                HookPromptInjection(
                    position="append",
                    title="Hook(before_worker_prompt) 启用默认根目录搜索",
                    content=(
                        "本轮属于本地搜索/代码定位任务，且用户未提供明确路径。"
                        "请先直接尝试默认搜索：优先在当前工作区根目录 '.' 使用 search_codebase；"
                        "若仍不够，再在允许访问根目录中选择最可能的项目目录继续搜索。"
                        "如果用户给了目录名（例如 workbench），可以直接把它当成 root/path 尝试。"
                        "不要先向用户索取路径，也不要要求用户提供工具调用格式。"
                    ),
                    trace_note="已启用默认根目录自动搜索策略。",
                )
            )
        return HookResult(
            route=updated_route,
            router_raw=updated_raw,
            execution_state=execution_state,
            spec_lookup_request=spec_lookup_request,
            evidence_required_mode=evidence_required_mode,
            prompt_injections=prompt_injections,
            trace_notes=trace_notes,
            debug_entries=debug_entries,
        )

    def _hook_before_reviewer(
        self,
        *,
        route: dict[str, Any],
        spec_lookup_request: bool,
        evidence_required_mode: bool,
    ) -> HookResult:
        execution_policy = str(route.get("execution_policy") or "").strip().lower()
        policy_spec = execution_policy_spec(execution_policy)
        reviewer_requested = bool(route.get("use_reviewer"))
        reviewer_enabled = reviewer_requested and policy_spec.reviewer
        conflict_detector_enabled = reviewer_enabled and policy_spec.conflict_detector and bool(route.get("use_conflict_detector"))
        revision_enabled = reviewer_enabled and policy_spec.revision and bool(route.get("use_revision"))
        trace_notes: list[str] = []
        debug_entries: list[dict[str, str]] = []

        if reviewer_requested and not reviewer_enabled:
            trace_notes.append("Hook(before_reviewer): 当前 execution_policy 不允许 Reviewer，已跳过审阅链。")
            debug_entries.append(
                HookDebugEntry(
                    stage="backend_hook",
                    title="Hook(before_reviewer) 跳过审阅链",
                    detail=(
                        f"execution_policy={execution_policy or '(empty)'}\n"
                        f"task_type={str(route.get('task_type') or 'standard')}"
                    ),
                )
            )
        return HookResult(
            use_reviewer=reviewer_enabled,
            use_conflict_detector=conflict_detector_enabled,
            use_revision=revision_enabled,
            trace_notes=trace_notes,
            debug_entries=debug_entries,
        )

    def _hook_after_planner(
        self,
        *,
        planner_brief: RoleResult | dict[str, Any],
    ) -> HookResult:
        planner_payload = self._role_payload_dict(planner_brief)
        planner_plan = self._normalize_string_list(planner_payload.get("plan") or [], limit=8, item_limit=160)
        planner_system_hint = self._format_planner_system_hint(planner_payload)
        prompt_injections: list[HookPromptInjection] = []
        if planner_system_hint:
            prompt_injections.append(
                HookPromptInjection(
                    position="front",
                    title="Hook(after_planner) 注入 Planner 摘要",
                    content=planner_system_hint,
                    trace_note="多 Role: Coordinator 已将 Planner 摘要注入 Worker 请求。",
                )
            )
        return HookResult(
            execution_plan=planner_plan,
            prompt_injections=prompt_injections,
        )

    def _hook_before_structurer(
        self,
        *,
        route: dict[str, Any],
        final_text: str,
        citations: list[dict[str, Any]],
        reviewer_brief: RoleResult | dict[str, Any] | None,
        conflict_brief: RoleResult | dict[str, Any] | None,
        evidence_required_mode: bool,
        spec_lookup_request: bool,
    ) -> HookResult:
        finalized_citations = list(citations or [])
        trace_notes: list[str] = []
        debug_entries: list[HookDebugEntry] = []

        if str(route.get("task_type") or "").strip().lower() == "meeting_minutes":
            finalized_citations = []
            trace_notes.append("Hook(before_structurer): 会议纪要模式已关闭 citations（证据来源）展示。")

        answer_bundle = self._fallback_answer_bundle(
            final_text=final_text,
            citations=finalized_citations,
            reviewer_brief=reviewer_brief,
            conflict_brief=conflict_brief,
        )
        if (
            not finalized_citations
            and not self._is_evidence_verification_mode(
                route=route,
                evidence_required_mode=evidence_required_mode,
                spec_lookup_request=spec_lookup_request,
            )
        ):
            answer_bundle["claims"] = []
            answer_bundle["warnings"] = []

        execution_policy = str(route.get("execution_policy") or "").strip().lower()
        policy_spec = execution_policy_spec(execution_policy)
        structurer_requested = bool(route.get("use_structurer"))
        structurer_enabled = structurer_requested and policy_spec.structurer
        if structurer_requested and not structurer_enabled:
            trace_notes.append("Hook(before_structurer): 当前 execution_policy 不允许 Structurer，已直接返回 fallback answer bundle。")
            debug_entries.append(
                HookDebugEntry(
                    stage="backend_hook",
                    title="Hook(before_structurer) 跳过结构化证据包",
                    detail=(
                        f"execution_policy={execution_policy or '(empty)'}\n"
                        f"task_type={str(route.get('task_type') or 'standard')}"
                    ),
                )
            )
        return HookResult(
            finalized_citations=finalized_citations,
            answer_bundle=answer_bundle,
            use_structurer=structurer_enabled,
            should_emit_answer_bundle=self._should_emit_answer_bundle(finalized_citations),
            trace_notes=trace_notes,
            debug_entries=debug_entries,
        )

    def _looks_like_tool_escalation_needed(self, text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        patterns = (
            "需要搜索代码",
            "需要先搜索代码",
            "需要代码搜索",
            "需要先定位代码",
            "需要在代码里找",
            "要在代码里找",
            "未启用代码搜索工具",
            "没有启用代码搜索工具",
            "当前环境中没有启用代码搜索工具",
            "当前环境没有启用代码搜索工具",
            "当前没有启用代码搜索工具",
            "必须让我把代码源文件给它",
            "必须提供代码源文件",
            "需要你把代码源文件给我",
            "需要你提供源文件",
            "需要提供源文件",
            "请把代码文件给我",
            "请提供代码文件",
            "请提供完整文件名",
            "请给出完整文件名",
            "请提供扩展名",
            "需要文件扩展名",
            "code search tool is not enabled",
            "code search is not enabled",
            "need to search the code",
            "need the source file",
            "please provide the source file",
            "need full filename",
            "need file extension",
            "没有可用的文件读取工具",
            "没有可用文件读取工具",
            "没有文件读取工具",
            "当前界面没有显示任何可用的文件读取工具",
            "当前没有可用的文件读取工具",
            "无法继续，因为当前界面没有显示任何可用的文件读取工具",
            "当前会话没有任何写入本地文件的工具",
            "没有任何写入本地文件的工具",
            "没有可用的写入工具",
            "当前没有写入工具",
            "没有文件写入工具",
            "写入工具不可用",
            "无法写入本地文件",
            "file reading tools are not available",
            "no file reading tool is available",
            "no file read tools available",
            "no file tools available",
            "no local file write tool is available",
            "no local file writing tools",
            "no write tools available",
            "write tool is not available",
            "cannot write local files",
        )
        return any(pattern in raw for pattern in patterns)

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

    def _format_planner_system_hint(self, planner_brief: RoleResult | dict[str, Any]) -> str:
        planner_payload = self._role_payload_dict(planner_brief)
        objective = str(planner_payload.get("objective") or "").strip()
        constraints = self._normalize_string_list(planner_payload.get("constraints") or [], limit=5, item_limit=180)
        plan = self._normalize_string_list(planner_payload.get("plan") or [], limit=6, item_limit=180)
        watchouts = self._normalize_string_list(planner_payload.get("watchouts") or [], limit=4, item_limit=180)
        success_signals = self._normalize_string_list(
            planner_payload.get("success_signals") or [], limit=4, item_limit=180
        )
        lines = ["多 Role 协调摘要（来自 Planner）："]
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
            f"primary_intent: {route.get('primary_intent')}",
            f"execution_policy: {route.get('execution_policy')}",
            f"runtime_profile: {route.get('runtime_profile')}",
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

    def _specialist_contract(self, specialist: str, *, initial_triage_request: bool = False) -> dict[str, Any]:
        return specialist_contract_helper(specialist, initial_triage_request=initial_triage_request)

    def _build_specialist_input_payload(
        self,
        *,
        specialist: str,
        context: RoleContext,
        route_summary: str,
        payload_preview: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        return build_specialist_input_payload_helper(
            self,
            specialist=specialist,
            context=context,
            route_summary=route_summary,
            payload_preview=payload_preview,
            contract=contract,
        )

    def _normalize_specialist_brief_payload(
        self,
        *,
        specialist: str,
        parsed: dict[str, Any],
        fallback: dict[str, Any],
        usage: dict[str, int],
        effective_model: str,
        notes: list[str],
        contract: dict[str, Any],
        initial_triage_request: bool = False,
    ) -> dict[str, Any]:
        return normalize_specialist_brief_payload_helper(
            self,
            specialist=specialist,
            parsed=parsed,
            fallback=fallback,
            usage=usage,
            effective_model=effective_model,
            notes=notes,
            contract=contract,
            initial_triage_request=initial_triage_request,
        )

    def _specialist_fallback(
        self,
        *,
        specialist: str,
        requested_model: str,
        attachment_metas: list[dict[str, Any]],
        initial_triage_request: bool = False,
    ) -> dict[str, Any]:
        return specialist_fallback_helper(
            self,
            specialist=specialist,
            requested_model=requested_model,
            attachment_metas=attachment_metas,
            initial_triage_request=initial_triage_request,
        )

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
        context = self._make_role_context(
            specialist,
            requested_model=requested_model,
            user_message=user_message,
            history_summary=summary,
            attachment_metas=attachment_metas,
            route=route,
            user_content=user_content,
        )
        result = self._run_specialist_with_context(context=context)
        return result.payload, result.raw_text

    def _run_specialist_with_context(self, *, context: RoleContext) -> RoleResult:
        return run_specialist_with_context_helper(self, context=context)

    def _format_specialist_system_hint(self, specialist: str, brief: RoleResult | dict[str, Any]) -> str:
        return format_specialist_system_hint_helper(self, specialist, brief)

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

    def _has_successful_local_file_access(self, tool_events: list[ToolEvent]) -> bool:
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
            if self._tool_event_ok(event) is True:
                return True
        return False

    def _has_text_search_evidence(self, tool_events: list[ToolEvent]) -> bool:
        search_tools = {"search_text_in_file", "multi_query_search", "search_codebase"}
        for event in tool_events:
            if str(event.name or "").strip() not in search_tools:
                continue
            if self._tool_event_ok(event) is True:
                return True
        return False

    def _looks_like_unverified_fulltext_search_claim(self, text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        markers = (
            "全文搜索",
            "全文检索",
            "对全文进行了搜索",
            "全文件搜索",
            "full text search",
            "full-text search",
            "searched the entire",
            "whole document search",
            "searched the whole document",
        )
        return any(marker in lowered for marker in markers)

    def _summarize_tool_events_for_review(self, tool_events: list[ToolEvent], limit: int = 12) -> list[str]:
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
        return [
            self._format_tool_event_for_review(idx=idx, event=tool_events[idx])
            for idx in kept_indexes
        ]

    def _format_tool_event_for_review(self, *, idx: int, event: ToolEvent) -> str:
        name = str(event.name or "unknown").strip() or "unknown"
        args = json.dumps(event.input or {}, ensure_ascii=False)
        parsed = self._parse_tool_event_preview(event) or {}
        status = self._tool_event_ok(event)
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
            details.append(f"query={self._shorten(query, 80)}")
        match_count = parsed.get("match_count")
        if isinstance(match_count, int):
            details.append(f"match_count={match_count}")
        replacements = parsed.get("replacements")
        if isinstance(replacements, int):
            details.append(f"replacements={replacements}")
        error = str(parsed.get("error") or "").strip()
        if error:
            details.append(f"error={self._shorten(error, 120)}")
        if not details:
            preview = self._shorten(str(event.output_preview or "").strip(), 120)
            if preview:
                details.append(preview)
        detail_text = "; ".join(details)
        return f"{idx + 1}. {name}({args}){f' -> {detail_text}' if detail_text else ''}"

    def _summarize_write_tool_events(self, tool_events: list[ToolEvent], limit: int = 6) -> list[str]:
        lines: list[str] = []
        for event in tool_events:
            name = str(event.name or "").strip()
            if name not in {"write_text_file", "append_text_file", "replace_in_file", "copy_file"}:
                continue
            parsed = self._parse_tool_event_preview(event) or {}
            ok = self._tool_event_ok(event)
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
                parts.append(f"error={self._shorten(error, 120)}")
            lines.append(" | ".join(parts))
        return lines[-max(1, limit) :]

    def _successful_write_targets(self, tool_events: list[ToolEvent]) -> list[str]:
        targets: list[str] = []
        for event in tool_events:
            name = str(event.name or "").strip()
            if name not in {"write_text_file", "append_text_file", "replace_in_file", "copy_file"}:
                continue
            if self._tool_event_ok(event) is not True:
                continue
            parsed = self._parse_tool_event_preview(event) or {}
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

    def _text_acknowledges_written_targets(self, text: str, targets: list[str]) -> bool:
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

    def _should_preserve_worker_answer_after_revision(
        self,
        *,
        current_text: str,
        revised_text: str,
        tool_events: list[ToolEvent],
        attachment_metas: list[dict[str, Any]],
    ) -> bool:
        targets = self._successful_write_targets(tool_events)
        revised_ack = self._text_acknowledges_written_targets(revised_text, targets) if targets else False
        if targets and revised_ack:
            return False
        current_ack = self._text_acknowledges_written_targets(current_text, targets) if targets else False
        revised_lower = str(revised_text or "").strip().lower()
        conservative_markers = (
            "证据不足",
            "需要继续核对",
            "需要继续取证",
            "需要进一步核对",
            "需要更多上下文",
            "无法确认",
            "无法确定",
            "请提供",
            "请重新提供",
            "建议继续核对",
            "保守",
            "need more evidence",
            "need more context",
            "cannot confirm",
            "unable to confirm",
        )
        attachment_denial = self._looks_like_attachment_absence_claim(
            revised_text,
            attachment_metas=attachment_metas,
            tool_events=tool_events,
        )
        looks_like_fallback = (
            self._looks_like_local_path_denial(revised_text)
            or self._looks_like_permission_gate_text(
                revised_text,
                has_attachments=False,
                request_requires_tools=True,
            )
            or attachment_denial
            or any(marker in revised_lower for marker in conservative_markers)
        )
        if not looks_like_fallback:
            return False
        if current_ack:
            return True
        if attachment_denial:
            return True
        if not targets:
            return False
        return len(revised_text.strip()) < max(80, len(current_text.strip()) // 2)

    def _looks_like_attachment_absence_claim(
        self,
        text: str,
        *,
        attachment_metas: list[dict[str, Any]],
        tool_events: list[ToolEvent],
    ) -> bool:
        if not attachment_metas:
            return False
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        absence_patterns = (
            "附件不存在",
            "附件未找到",
            "找不到附件",
            "没有附件",
            "未提供附件",
            "未上传附件",
            "附件不在当前上下文",
            "附件不在本轮",
            "no attachment",
            "attachments do not exist",
            "attachment not found",
            "attachments not found",
            "no attachments were provided",
        )
        if not any(pattern in lowered for pattern in absence_patterns):
            return False

        attachment_paths = {
            str(meta.get("path") or "").strip()
            for meta in attachment_metas
            if str(meta.get("path") or "").strip()
        }
        attachment_names = {
            str(meta.get("original_name") or "").strip().lower()
            for meta in attachment_metas
            if str(meta.get("original_name") or "").strip()
        }
        for event in tool_events:
            if self._tool_event_ok(event) is not True:
                continue
            parsed = self._parse_tool_event_preview(event) or {}
            for candidate in (
                str((event.input or {}).get("path") or "").strip(),
                str(parsed.get("path") or "").strip(),
            ):
                if not candidate:
                    continue
                if candidate in attachment_paths:
                    return True
                if Path(candidate).name.strip().lower() in attachment_names:
                    return True
        return True

    def _looks_like_local_path_denial(self, text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        patterns = (
            "无法访问用户本地路径",
            "无法访问本地路径",
            "无法读取用户本地路径",
            "没有提供可供工具读取",
            "未提供可供工具读取",
            "没有提供本地文件路径",
            "未提供本地文件路径",
            "没有给我实际路径",
            "没有给出实际路径",
            "未给出实际路径",
            "没有实际路径",
            "请提供路径",
            "请给出路径",
            "必须提供路径",
            "需要本地文件路径",
            "请提供完整文件名",
            "请给出完整文件名",
            "请提供完整的文件名",
            "请提供扩展名",
            "请给出扩展名",
            "需要扩展名",
            "需要文件扩展名",
            "无法进行可复核的解析",
        )
        return any(pattern in raw for pattern in patterns)

    def _conflict_is_realtime_capability_warning(self, conflict_brief: RoleResult | dict[str, Any] | None) -> bool:
        conflict_payload = self._role_payload_dict(conflict_brief)
        lines = [
            str(conflict_payload.get("summary") or "").strip(),
            *self._normalize_string_list(conflict_payload.get("concerns") or [], limit=4, item_limit=200),
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

    def _parse_loose_object_literal(self, raw_text: str) -> dict[str, Any] | None:
        text = str(raw_text or "").strip()
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if not (0 <= start < end):
            return None
        body = text[start + 1 : end]
        pair_pattern = re.compile(
            r'["\'](?P<key>[^"\']+)["\']\s*:\s*(?P<value>"[^"]*"|\'[^\']*\'|true|false|-?\d+(?:\.\d+)?)',
            re.IGNORECASE,
        )
        parsed: dict[str, Any] = {}
        for match in pair_pattern.finditer(body):
            key = str(match.group("key") or "").strip()
            raw_value = str(match.group("value") or "").strip()
            if not key or not raw_value:
                continue
            lowered = raw_value.lower()
            if lowered == "true":
                value: Any = True
            elif lowered == "false":
                value = False
            elif raw_value[:1] in {'"', "'"} and raw_value[-1:] == raw_value[:1]:
                value = raw_value[1:-1]
            else:
                try:
                    value = float(raw_value) if "." in raw_value else int(raw_value)
                except Exception:
                    value = raw_value
            parsed[key] = value
        return parsed or None

    def _extract_standalone_object_payload(self, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return ""
        if text.startswith("```"):
            match = re.match(r"^```(?:json|javascript|js)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
            if match:
                text = str(match.group(1) or "").strip()
        if text.lower().startswith("json"):
            text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE).strip()
        if text.startswith("{") and text.endswith("}"):
            return text
        return ""

    def _extract_standalone_json_answer(self, text: str) -> dict[str, Any] | None:
        payload = self._extract_standalone_object_payload(text)
        if not payload:
            return None
        parsed = self._parse_json_object(payload)
        if isinstance(parsed, dict) and parsed:
            return parsed
        return None

    def _user_explicitly_requests_json_output(self, user_message: str) -> bool:
        text = str(user_message or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if "```json" in lowered:
            return True
        if re.search(
            r"(?is)(?:请|请你|帮我|给我|用|以|按|返回|输出|改成|转成|格式化为|return|respond|format(?:\s+as)?|convert(?:\s+to)?).{0,16}json",
            text,
        ):
            return True
        if re.search(r"(?is)json.{0,16}(?:格式|输出|返回|对象|数组|schema|字段|键|object|array)", text):
            return True
        if re.search(r"(?i)\b(?:package|tsconfig|composer|manifest)\.json\b", text):
            return True
        return False

    def _render_json_answer_for_user(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict) or not payload:
            return ""
        email_payload = payload
        for nested_key in ("email", "mail", "draft"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict) and nested:
                email_payload = nested
                break
        email_text = self._render_email_json_answer(email_payload)
        if email_text:
            return email_text

        records = self._extract_json_records_for_table(payload)
        if records:
            table = self._render_records_markdown_table(records)
            if table:
                intro = str(payload.get("summary") or payload.get("title") or "").strip()
                if intro:
                    return f"{intro}\n\n{table}"
                return table

        lines: list[str] = []
        for key, value in list(payload.items())[:12]:
            label = str(key).strip() or "item"
            rendered = self._render_json_value_for_user(value)
            if not rendered:
                continue
            if "\n" in rendered:
                lines.append(f"{label}:")
                lines.append(rendered)
            else:
                lines.append(f"{label}: {rendered}")
        return "\n".join(lines).strip()

    def _render_email_json_answer(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict) or not payload:
            return ""
        pick = lambda *keys: next(
            (
                value
                for value in (
                    self._render_json_value_for_user(payload.get(key))
                    for key in keys
                )
                if value
            ),
            "",
        )
        subject = pick("subject", "email_subject", "title", "topic")
        to = pick("to", "recipient", "recipient_name")
        cc = pick("cc")
        greeting = pick("greeting", "salutation")
        body = pick("body", "content", "email_body", "draft_text", "message", "text")
        closing = pick("closing", "signature", "signoff")
        if not any((subject, to, body, greeting, closing, cc)):
            return ""
        if not body and not (subject or to or cc):
            return ""
        lines: list[str] = []
        if subject:
            lines.append(f"邮件主题：{subject}")
        if to:
            lines.append(f"收件人：{to}")
        if cc:
            lines.append(f"抄送：{cc}")
        if greeting:
            lines.append("")
            lines.append(greeting)
        if body:
            lines.append("")
            lines.append("邮件正文：")
            lines.append(body)
        if closing:
            lines.append("")
            lines.append(closing)
        return "\n".join(lines).strip()

    def _extract_json_records_for_table(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = (
            payload.get("table"),
            payload.get("rows"),
            payload.get("records"),
            payload.get("items"),
            payload.get("data"),
        )
        for candidate in candidates:
            if isinstance(candidate, list):
                rows = [item for item in candidate if isinstance(item, dict) and item]
                if rows:
                    return rows[:30]
        return []

    def _render_records_markdown_table(self, records: list[dict[str, Any]]) -> str:
        rows = [row for row in records if isinstance(row, dict) and row]
        if not rows:
            return ""
        columns: list[str] = []
        seen: set[str] = set()
        for row in rows[:30]:
            for raw_key in row.keys():
                key = str(raw_key).strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                columns.append(key)
                if len(columns) >= 10:
                    break
            if len(columns) >= 10:
                break
        if not columns:
            return ""

        def _cell(value: Any) -> str:
            if value is None:
                text = ""
            elif isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, (int, float)):
                text = str(value)
            elif isinstance(value, str):
                text = value.strip()
            else:
                text = self._shorten(json.dumps(value, ensure_ascii=False), 120)
            return text.replace("|", r"\|").replace("\n", "<br>")

        header = "| " + " | ".join(columns) + " |"
        divider = "| " + " | ".join("---" for _ in columns) + " |"
        body_lines: list[str] = []
        for row in rows[:20]:
            cells = [_cell(row.get(column)) for column in columns]
            body_lines.append("| " + " | ".join(cells) + " |")
        return "\n".join([header, divider, *body_lines]).strip()

    def _render_json_value_for_user(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            lines: list[str] = []
            for key, nested in list(value.items())[:8]:
                nested_text = self._render_json_value_for_user(nested)
                if not nested_text:
                    continue
                label = str(key).strip() or "item"
                if "\n" in nested_text:
                    lines.append(f"{label}:")
                    lines.append(nested_text)
                else:
                    lines.append(f"{label}: {nested_text}")
            return "\n".join(lines).strip()
        if isinstance(value, list):
            if not value:
                return ""
            if all(isinstance(item, str) for item in value[:8]):
                return "\n".join(f"- {str(item).strip()}" for item in value[:8] if str(item).strip()).strip()
            if all(isinstance(item, dict) for item in value[:8]):
                return self._render_records_markdown_table([item for item in value if isinstance(item, dict)])
            return self._shorten(json.dumps(value, ensure_ascii=False), 1000)
        return str(value).strip()

    def _infer_bare_tool_call_from_text(
        self,
        text: str,
        *,
        task_type: str = "",
    ) -> dict[str, Any] | None:
        payload = self._extract_standalone_object_payload(text)
        if not payload:
            return None
        parsed = self._parse_json_object(payload) or self._parse_loose_object_literal(payload)
        if not isinstance(parsed, dict) or not parsed:
            return None

        wrapper_tool = str(parsed.get("tool") or parsed.get("name") or "").strip()
        wrapper_args = parsed.get("args")
        if wrapper_tool and isinstance(wrapper_args, dict):
            return {
                "id": f"inferred_{wrapper_tool}",
                "name": wrapper_tool,
                "args": wrapper_args,
                "inferred": True,
            }

        keys = {str(key).strip() for key in parsed.keys()}
        if not keys:
            return None

        if "root_path" in parsed and "root" not in parsed:
            parsed["root"] = parsed.get("root_path")
        if "query_text" in parsed and "query" not in parsed:
            parsed["query"] = parsed.get("query_text")
        if "keyword" in parsed and "query" not in parsed:
            parsed["query"] = parsed.get("keyword")
        keys = {str(key).strip() for key in parsed.keys()}

        search_codebase_keys = {"query", "root", "max_matches", "file_glob", "use_regex", "case_sensitive"}
        list_directory_keys = {"path", "max_entries"}
        read_text_file_keys = {"path", "start_char", "max_chars", "start_line", "max_lines"}
        search_text_in_file_keys = {"path", "query", "max_matches", "context_chars"}

        if "query" in keys and ("root" in keys or keys.issubset(search_codebase_keys) or task_type == "code_lookup"):
            args: dict[str, Any] = {"query": str(parsed.get("query") or "").strip()}
            if not args["query"]:
                return None
            args["root"] = str(parsed.get("root") or ".").strip() or "."
            for optional in ("max_matches", "file_glob", "use_regex", "case_sensitive"):
                if optional in parsed:
                    args[optional] = parsed.get(optional)
            return {
                "id": "inferred_search_codebase",
                "name": "search_codebase",
                "args": args,
                "inferred": True,
            }

        if "path" in keys and keys.issubset(search_text_in_file_keys) and "query" in keys:
            path = str(parsed.get("path") or "").strip()
            query = str(parsed.get("query") or "").strip()
            if not path or not query:
                return None
            args = {"path": path, "query": query}
            for optional in ("max_matches", "context_chars"):
                if optional in parsed:
                    args[optional] = parsed.get(optional)
            return {
                "id": "inferred_search_text_in_file",
                "name": "search_text_in_file",
                "args": args,
                "inferred": True,
            }

        if "path" in keys and keys.issubset(read_text_file_keys):
            path = str(parsed.get("path") or "").strip()
            if not path:
                return None
            args = {"path": path}
            for optional in ("start_char", "max_chars", "start_line", "max_lines"):
                if optional in parsed:
                    args[optional] = parsed.get(optional)
            return {
                "id": "inferred_read_text_file",
                "name": "read_text_file",
                "args": args,
                "inferred": True,
            }

        if "path" in keys and keys.issubset(list_directory_keys):
            path = str(parsed.get("path") or "").strip() or "."
            args = {"path": path}
            if "max_entries" in parsed:
                args["max_entries"] = parsed.get("max_entries")
            return {
                "id": "inferred_list_directory",
                "name": "list_directory",
                "args": args,
                "inferred": True,
            }
        return None

    def _looks_like_bare_tool_arguments_text(self, text: str) -> bool:
        payload = self._extract_standalone_object_payload(text)
        if not payload:
            return False
        parsed = self._parse_json_object(payload) or self._parse_loose_object_literal(payload)
        if not isinstance(parsed, dict) or not parsed:
            return False
        keys = {str(key).strip().lower() for key in parsed.keys() if str(key).strip()}
        if not keys:
            return False
        known_keys = {
            "tool",
            "name",
            "args",
            "query",
            "query_text",
            "keyword",
            "root",
            "root_path",
            "path",
            "start_char",
            "max_chars",
            "start_line",
            "max_lines",
            "max_matches",
            "context_chars",
            "file_glob",
            "use_regex",
            "case_sensitive",
            "max_entries",
        }
        # Bare tool-args payloads are typically tiny dicts made of known arg keys.
        if not keys.issubset(known_keys):
            return False
        if "args" in keys and ("tool" in keys or "name" in keys):
            return True
        if {"query", "root"} & keys:
            return True
        if "path" in keys and ({"query", "start_char", "max_chars", "start_line", "max_lines", "max_entries"} & keys):
            return True
        return False

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
            lines.append(f"warning（风险提示）: {warning}")
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

    def _looks_like_holistic_document_explanation_request(self, user_message: str) -> bool:
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if self._looks_like_source_trace_request(user_message):
            return False
        if text_has_any(text, VERIFICATION_HINTS) or "页码" in text:
            return False
        has_overview = text_has_any(text, HOLISTIC_OVERVIEW_MARKERS)
        has_explain = text_has_any(text, HOLISTIC_EXPLAIN_MARKERS)
        if has_overview and has_explain:
            return True
        return text_has_any(text, HOLISTIC_DIRECT_PHRASES)

    def _looks_like_source_trace_request(self, user_message: str) -> bool:
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if text_has_any(text, SOURCE_TRACE_HINTS):
            return True
        return bool(
            re.search(r"(?:在哪|哪里|哪儿).{0,6}(?:看到|写到|提到)", text)
            or re.search(r"(?:where).{0,18}(?:see|mention|found)", text)
        )

    def _has_image_attachments(self, attachment_metas: list[dict[str, Any]]) -> bool:
        return any(str(meta.get("kind") or "").strip().lower() == "image" for meta in attachment_metas)

    def _looks_like_image_text_extraction_request(self, user_message: str) -> bool:
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

    def _looks_like_image_capability_denial(self, text: str) -> bool:
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

    def _looks_like_stub_image_transcription(self, text: str) -> bool:
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

    def _looks_like_meeting_minutes_request(self, user_message: str) -> bool:
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if self._requires_evidence_mode(user_message, []):
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

        has_meeting_context = any(hint in text for hint in _MEETING_HINTS)
        has_minutes_intent = any(hint in text for hint in _MEETING_MINUTES_ACTION_HINTS)
        return has_meeting_context and has_minutes_intent

    def _looks_like_inline_document_payload(self, user_message: str) -> bool:
        text = str(user_message or "").strip()
        if len(text) < 120:
            return False
        lowered = text.lower()
        if "<?xml" in lowered:
            return True
        if any(marker in lowered for marker in _INLINE_DOC_CODE_FENCE_HINTS):
            return True
        if self._looks_like_inline_code_payload(text):
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

    def _looks_like_inline_code_payload(self, text: str) -> bool:
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

    def _looks_like_initial_content_triage_request(self, user_message: str) -> bool:
        text = str(user_message or "").strip().lower()
        if not text:
            return False
        if any(hint in text for hint in _INITIAL_CONTENT_TRIAGE_HINTS):
            return True
        if ("下面" in text or "以下" in text or "below" in text) and (
            "理解" in text or "看懂" in text or "解释" in text or "understand" in text or "read" in text
        ):
            return True
        return False

    def _looks_like_internal_ticket_reference(self, user_message: str) -> bool:
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
        # Internal ticket messages often include both ticket semantics and private context hints.
        return has_ticket and (has_internal or has_url)

    def _attachment_is_inline_parseable(self, meta: dict[str, Any]) -> bool:
        suffix = str(meta.get("suffix") or "").strip().lower()
        kind = str(meta.get("kind") or "").strip().lower()
        try:
            size = int(meta.get("size") or 0)
        except Exception:
            size = 0
        if kind == "image":
            return size <= _ATTACHMENT_INLINE_IMAGE_MAX_BYTES
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
            ".pptx",
            ".pptm",
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

    def _task_type_to_primary_intent(self, task_type: str) -> str:
        normalized = str(task_type or "").strip().lower()
        mapping = {
            "simple_understanding": "understanding",
            "inline_document_understanding": "understanding",
            "attachment_tooling": "understanding",
            "mixed_attachment": "understanding",
            "evidence_lookup": "evidence",
            "web_news": "web",
            "web_research": "web",
            "code_lookup": "code_lookup",
            "grounded_code_generation": "generation",
            "code_generation": "generation",
            "meeting_minutes": "meeting_minutes",
            "simple_qa": "qa",
            "general_qa": "qa",
        }
        return mapping.get(normalized, "standard")

    def _task_type_to_execution_policy(self, task_type: str) -> str:
        normalized = str(task_type or "").strip().lower()
        mapping = {
            "simple_understanding": "understanding_direct",
            "inline_document_understanding": "inline_document_understanding_direct",
            "attachment_tooling": "understanding_with_tools",
            "mixed_attachment": "llm_router_attachment_ambiguity",
            "evidence_lookup": "evidence_full_pipeline",
            "web_news": "web_news_brief",
            "web_research": "web_research_full_pipeline",
            "code_lookup": "code_lookup_with_tools",
            "grounded_code_generation": "grounded_generation_with_tools",
            "code_generation": "generation_with_tools",
            "meeting_minutes": "meeting_minutes_output",
            "simple_qa": "qa_direct",
            "general_qa": "llm_router_general_ambiguity",
            "standard": "standard_full_pipeline",
        }
        return mapping.get(normalized, "standard_full_pipeline")

    def _default_execution_policy_for_intent(self, primary_intent: str) -> str:
        normalized = str(primary_intent or "").strip().lower()
        mapping = {
            "understanding": "understanding_direct",
            "evidence": "evidence_full_pipeline",
            "web": "web_research_full_pipeline",
            "code_lookup": "code_lookup_with_tools",
            "generation": "generation_with_tools",
            "meeting_minutes": "meeting_minutes_output",
            "qa": "qa_direct",
            "standard": "standard_full_pipeline",
        }
        return mapping.get(normalized, "standard_full_pipeline")

    def _normalize_primary_intent(self, value: str, *, task_type: str = "") -> str:
        normalized = str(value or "").strip().lower()
        allowed = {
            "understanding",
            "evidence",
            "web",
            "code_lookup",
            "generation",
            "meeting_minutes",
            "qa",
            "standard",
        }
        if normalized in allowed:
            return normalized
        if task_type:
            return self._task_type_to_primary_intent(task_type)
        return "standard"

    def _build_session_route_state(self, route: dict[str, Any]) -> dict[str, Any]:
        task_type = str(route.get("task_type") or "standard").strip().lower()
        primary_intent = self._normalize_primary_intent(
            str(route.get("primary_intent") or ""),
            task_type=task_type,
        )
        execution_policy = str(route.get("execution_policy") or "").strip() or self._task_type_to_execution_policy(task_type)
        runtime_profile = str(route.get("runtime_profile") or "").strip() or default_runtime_profile_for_route(route)
        return {
            "primary_intent": primary_intent,
            "execution_policy": execution_policy,
            "runtime_profile": runtime_profile,
            "task_type": task_type,
            "use_worker_tools": bool(route.get("use_worker_tools")),
            "evidence_mode": task_type == "evidence_lookup",
        }

    def _infer_followup_primary_intent_from_state(
        self,
        *,
        user_message: str,
        route_state: dict[str, Any] | None,
        signals: dict[str, Any],
    ) -> str:
        last_intent = self._normalize_primary_intent(
            str((route_state or {}).get("primary_intent") or ""),
            task_type=str((route_state or {}).get("task_type") or ""),
        )
        if last_intent == "standard":
            return ""
        if signals.get("holistic_document_explanation"):
            return ""
        if signals.get("source_trace_request") or signals.get("web_request"):
            return ""
        if signals.get("local_code_lookup_request") or signals.get("meeting_minutes_request"):
            return ""
        if self._looks_like_code_generation_request(user_message, signals.get("attachment_metas") or []):
            return ""
        text = str(signals.get("text") or "").strip()
        lowered = text.lower()
        if signals.get("explicit_tool_confirmation"):
            return last_intent
        if signals.get("context_dependent_followup"):
            return last_intent
        if len(text) <= 24 and any(token in lowered for token in ("继续", "接着", "然后", "再来", "continue", "next", "go on")):
            return last_intent
        return ""

    def _classify_primary_intent(
        self,
        *,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        route_state: dict[str, Any] | None,
        signals: dict[str, Any],
    ) -> str:
        if (
            signals.get("inline_followup_context")
            and signals.get("context_dependent_followup")
            and not signals.get("has_attachments")
            and not signals.get("web_request")
            and not signals.get("local_code_lookup_request")
            and not self._message_has_explicit_local_path(user_message)
            and not self._looks_like_write_or_edit_action(signals.get("text") or "")
        ):
            return "understanding"
        if self._looks_like_code_generation_request(user_message, attachment_metas):
            return "generation"
        if signals.get("has_attachments") and signals.get("source_trace_request"):
            return "evidence"
        if (
            signals.get("meeting_minutes_request")
            and not signals.get("spec_lookup_request")
            and not signals.get("evidence_required")
            and not signals.get("web_request")
        ):
            return "meeting_minutes"
        if signals.get("has_attachments") and signals.get("holistic_document_explanation") and not signals.get("web_request"):
            return "understanding"
        if (
            signals.get("has_attachments")
            and signals.get("understanding_request")
            and not signals.get("spec_lookup_request")
            and not signals.get("evidence_required")
            and not signals.get("web_request")
        ):
            return "understanding"
        if signals.get("web_request") and not signals.get("has_attachments"):
            return "web"
        if signals.get("spec_lookup_request") or signals.get("evidence_required"):
            return "evidence"
        if signals.get("local_code_lookup_request"):
            return "code_lookup"
        inherited = str(signals.get("inherited_primary_intent") or "").strip() or self._infer_followup_primary_intent_from_state(
            user_message=user_message,
            route_state=route_state,
            signals=signals,
        )
        if inherited:
            return inherited
        if (
            signals.get("has_attachments")
            and signals.get("inline_parseable_attachments")
            and signals.get("understanding_request")
            and not signals.get("attachment_needs_tooling")
        ):
            return "understanding"
        if (
            not signals.get("has_attachments")
            and signals.get("inline_document_payload")
            and not signals.get("request_requires_tools")
        ):
            return "understanding"
        if (
            not signals.get("has_attachments")
            and not signals.get("request_requires_tools")
            and not signals.get("understanding_request")
            and len(str(signals.get("text") or "")) <= 240
        ):
            return "qa"
        if signals.get("attachment_needs_tooling"):
            return "understanding"
        return "standard"

    def _resolve_execution_policy(
        self,
        *,
        primary_intent: str,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        signals: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        has_attachments = bool(signals.get("has_attachments"))
        attachment_needs_tooling = bool(signals.get("attachment_needs_tooling"))
        inline_parseable_attachments = bool(signals.get("inline_parseable_attachments"))
        understanding_request = bool(signals.get("understanding_request"))
        web_request = bool(signals.get("web_request"))
        spec_lookup_request = bool(signals.get("spec_lookup_request"))
        evidence_required = bool(signals.get("evidence_required"))
        request_requires_tools = bool(signals.get("request_requires_tools"))
        text = str(signals.get("text") or "")

        if primary_intent == "generation":
            if bool(signals.get("grounded_code_generation_context")):
                return self._normalize_route_decision(
                    {
                        "task_type": "grounded_code_generation",
                        "complexity": "high" if (has_attachments or evidence_required or spec_lookup_request) else "medium",
                        "use_planner": True,
                        "use_worker_tools": bool(settings.enable_tools),
                        "use_reviewer": False,
                        "use_revision": False,
                        "use_structurer": False,
                        "use_web_prefetch": False,
                        "use_conflict_detector": False,
                        "specialists": ["file_reader"] if has_attachments else [],
                        "execution_policy": "grounded_generation_with_tools",
                        "reason": "rules_grounded_code_generation_request",
                        "summary": "检测到基于附件/原文/现有代码的生成请求，保留阅读与实现链路，但不进入事实审阅链。",
                    },
                    fallback=fallback,
                    settings=settings,
                )
            return self._normalize_route_decision(
                {
                    "task_type": "code_generation",
                    "complexity": "high" if has_attachments else "medium",
                    "use_planner": True,
                    "use_worker_tools": bool(settings.enable_tools),
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["file_reader"] if has_attachments else [],
                    "execution_policy": "generation_with_tools",
                    "reason": "rules_code_generation_request",
                    "summary": "检测到代码生成/改写请求，直接交给 Worker 实现，不走事实审阅链。",
                },
                fallback=fallback,
                settings=settings,
            )

        if primary_intent == "evidence":
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
                    "execution_policy": "evidence_full_pipeline",
                    "reason": (
                        "rules_source_trace_request"
                        if bool(signals.get("source_trace_request")) and has_attachments
                        else "rules_evidence_or_spec_request"
                    ),
                    "summary": (
                        "检测到来源定位请求，强制走取证链路并返回可复核出处。"
                        if bool(signals.get("source_trace_request")) and has_attachments
                        else "检测到查证/定位类任务，保留完整取证链路。"
                    ),
                },
                fallback=fallback,
                settings=settings,
            )

        if primary_intent == "meeting_minutes":
            needs_attachment_tools = has_attachments and (attachment_needs_tooling or not inline_parseable_attachments)
            use_tools = bool(settings.enable_tools and needs_attachment_tools)
            return self._normalize_route_decision(
                {
                    "task_type": "meeting_minutes",
                    "complexity": "medium" if has_attachments else "low",
                    "use_planner": use_tools,
                    "use_worker_tools": use_tools,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["file_reader", "summarizer"] if has_attachments else ["summarizer"],
                    "execution_policy": "meeting_minutes_output",
                    "reason": "rules_meeting_minutes_request",
                    "summary": "检测到会议纪要整理任务，输出面向记录与执行项，不进入证据审计链。",
                },
                fallback=fallback,
                settings=settings,
            )

        if primary_intent == "understanding":
            if (
                signals.get("inline_followup_context")
                and signals.get("context_dependent_followup")
                and not has_attachments
                and not web_request
                and not signals.get("local_code_lookup_request")
                and not self._message_has_explicit_local_path(user_message)
                and not self._looks_like_write_or_edit_action(text)
            ):
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
                        "execution_policy": "inline_followup_understanding",
                        "reason": "rules_inline_followup_context_understanding",
                        "summary": "检测到引用上一轮原文的加工型跟进，直接沿用现有原文上下文回答。",
                    },
                    fallback=fallback,
                    settings=settings,
                )
            if has_attachments and bool(signals.get("holistic_document_explanation")) and not web_request:
                if attachment_needs_tooling or not inline_parseable_attachments:
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
                            "execution_policy": "attachment_holistic_understanding_with_tools",
                            "reason": "rules_attachment_holistic_understanding_requires_tooling",
                            "summary": "检测到附件整体理解任务，先读文档再做高层解释，不进入取证审阅链。",
                        },
                        fallback=fallback,
                        settings=settings,
                    )
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
                        "specialists": ["file_reader", "summarizer"],
                        "execution_policy": "attachment_holistic_understanding_direct",
                        "reason": "rules_attachment_holistic_understanding",
                        "summary": "检测到附件整体理解任务，直接围绕整体结构与主线做解释，不进入取证审阅链。",
                    },
                    fallback=fallback,
                    settings=settings,
                )
            if has_attachments and str(signals.get("inherited_primary_intent") or "") == "understanding":
                if attachment_needs_tooling or not inline_parseable_attachments:
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
                            "execution_policy": "attachment_followup_understanding_with_tools",
                            "reason": "rules_attachment_followup_inherited_understanding_requires_tooling",
                            "summary": "检测到沿用上一轮理解任务的附件跟进，继续读取文档并输出解释，不进入证据审阅链。",
                        },
                        fallback=fallback,
                        settings=settings,
                    )
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
                        "specialists": ["file_reader", "summarizer"],
                        "execution_policy": "attachment_followup_understanding_direct",
                        "reason": "rules_attachment_followup_inherited_understanding",
                        "summary": "检测到沿用上一轮理解任务的附件跟进，继续基于附件内容组织解释。",
                    },
                    fallback=fallback,
                    settings=settings,
                )
            if has_attachments and understanding_request and not spec_lookup_request and not evidence_required and not web_request:
                if attachment_needs_tooling or not inline_parseable_attachments:
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
                            "execution_policy": "attachment_understanding_with_tools",
                            "reason": "rules_attachment_understanding_requires_tooling",
                            "summary": "检测到附件理解任务，先由 FileReader + Worker 工具链完成读取，再输出结论。",
                        },
                        fallback=fallback,
                        settings=settings,
                    )
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
                        "specialists": ["file_reader", "summarizer"],
                        "execution_policy": "attachment_understanding_direct",
                        "reason": "rules_attachment_understanding",
                        "summary": "检测到附件理解任务，直接基于附件内容组织解释，不进入事实审阅链。",
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
                        "execution_policy": "small_parseable_attachment_understanding",
                        "reason": "rules_small_parseable_attachment_understanding",
                        "summary": "小型可解析附件的理解任务，直接由 Worker 作答。",
                    },
                    fallback=fallback,
                    settings=settings,
                )
            if not has_attachments and bool(signals.get("inline_document_payload")) and not request_requires_tools:
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
                        "execution_policy": "inline_document_understanding_direct",
                        "reason": "rules_inline_document_payload_understanding",
                        "summary": "检测到用户直接粘贴的原始长文本，按 inline 文档直接理解，不要求文件路径。",
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
                        "execution_policy": "attachment_tooling_generic",
                        "reason": "rules_attachment_requires_tooling",
                        "summary": "附件需要解包或分块读取，先走 Worker 工具链。",
                    },
                    fallback=fallback,
                    settings=settings,
                )

        if primary_intent == "web":
            if bool(signals.get("web_news_brief_request")):
                return self._normalize_route_decision(
                    {
                        "task_type": "web_news",
                        "complexity": "medium",
                        "use_planner": False,
                        "use_worker_tools": True,
                        "use_reviewer": False,
                        "use_revision": False,
                        "use_structurer": False,
                        "use_web_prefetch": True,
                        "use_conflict_detector": False,
                        "specialists": ["researcher"],
                        "execution_policy": "web_news_brief",
                        "reason": "rules_web_news_brief",
                        "summary": "检测到普通新闻/今日动态请求，启用轻量联网简报链路。",
                    },
                    fallback=fallback,
                    settings=settings,
                )
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
                    "execution_policy": "web_research_full_pipeline",
                    "reason": "rules_web_request",
                    "summary": "检测到联网/实时信息请求，启用联网取证链路。",
                },
                fallback=fallback,
                settings=settings,
            )

        if primary_intent == "code_lookup":
            return self._normalize_route_decision(
                {
                    "task_type": "code_lookup",
                    "complexity": "medium",
                    "use_planner": True,
                    "use_worker_tools": True,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["file_reader"],
                    "execution_policy": "code_lookup_with_tools",
                    "reason": "rules_local_code_lookup_request",
                    "summary": "检测到本地代码定位/函数解释请求，直接启用 Worker 工具链。",
                },
                fallback=fallback,
                settings=settings,
            )

        if primary_intent == "qa":
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
                    "execution_policy": "qa_direct",
                    "reason": "rules_simple_qa",
                    "summary": "简单问答，直接由 Worker 回答。",
                },
                fallback=fallback,
                settings=settings,
            )

        if bool(signals.get("explicit_tool_confirmation")) and settings.enable_tools:
            if request_requires_tools or self._message_has_explicit_local_path(user_message):
                return self._normalize_route_decision(
                    {
                        "task_type": "standard",
                        "complexity": "medium",
                        "use_planner": True,
                        "use_worker_tools": True,
                        "use_reviewer": False,
                        "use_revision": False,
                        "use_structurer": False,
                        "use_web_prefetch": False,
                        "use_conflict_detector": False,
                        "specialists": ["file_reader"] if has_attachments else [],
                        "execution_policy": "continue_tooling",
                        "reason": "rules_explicit_tool_confirmation",
                        "summary": "检测到用户明确要求继续读取/执行，延续 Worker 工具链。",
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
                    "execution_policy": "llm_router_attachment_ambiguity",
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
                    "execution_policy": "llm_router_general_ambiguity",
                    "reason": "rules_ambiguous_general_request",
                    "summary": "普通问答但复杂度不够明确，交给轻量 Router 补判。",
                },
                fallback=fallback,
                settings=settings,
            )

        if request_requires_tools:
            return self._normalize_route_decision(
                {
                    "task_type": "standard",
                    "complexity": "medium",
                    "needs_llm_router": True,
                    "execution_policy": "llm_router_tool_ambiguity",
                    "reason": "rules_ambiguous_tool_intent",
                    "summary": "检测到工具意图但规则分诊不够确定，交给轻量 Router 补判最小链路。",
                },
                fallback=fallback,
                settings=settings,
            )

        return self._normalize_route_decision(fallback, fallback=fallback, settings=settings)

    def _normalize_route_decision(
        self,
        route: dict[str, Any],
        *,
        fallback: dict[str, Any],
        settings: ChatSettings,
    ) -> dict[str, Any]:
        registry = self._module_registry()
        module = getattr(registry, "policy", None)
        selected_ref = str((registry.selected_refs or {}).get("policy") or "")
        fallback_ref = "policy_resolver@1.0.0"
        if module is None or not hasattr(module, "normalize_route"):
            return self._normalize_route_decision_impl(route=route, fallback=fallback, settings=settings)
        try:
            normalized = module.normalize_route(
                agent=self,
                route=route,
                fallback=fallback,
                settings=settings,
            )
            self._record_module_success(kind="policy", selected_ref=selected_ref or fallback_ref)
            return normalized
        except Exception as exc:
            self._record_module_failure(
                kind="policy",
                requested_ref=selected_ref or fallback_ref,
                fallback_ref=fallback_ref,
                error=str(exc),
            )
            return self._normalize_route_decision_impl(route=route, fallback=fallback, settings=settings)

    def _normalize_route_decision_impl(
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
        normalized["primary_intent"] = self._normalize_primary_intent(
            str(normalized.get("primary_intent") or fallback.get("primary_intent") or ""),
            task_type=normalized["task_type"],
        )
        normalized["execution_policy"] = (
            str(normalized.get("execution_policy") or fallback.get("execution_policy") or "").strip()
            or self._task_type_to_execution_policy(normalized["task_type"])
        )
        normalized["runtime_profile"] = (
            str(normalized.get("runtime_profile") or fallback.get("runtime_profile") or "").strip()
            or default_runtime_profile_for_route(normalized)
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

        policy_spec = execution_policy_spec(normalized["execution_policy"])
        normalized["use_planner"] = planner_enabled_for_policy(
            normalized["execution_policy"],
            use_worker_tools=bool(normalized["use_worker_tools"]),
        )
        normalized["use_reviewer"] = policy_spec.reviewer
        normalized["use_revision"] = policy_spec.revision
        normalized["use_structurer"] = policy_spec.structurer
        normalized["use_conflict_detector"] = policy_spec.conflict_detector

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
        route_state: dict[str, Any] | None = None,
        inline_followup_context: bool = False,
    ) -> dict[str, Any]:
        registry = self._module_registry()
        module = getattr(registry, "router", None)
        selected_ref = str((registry.selected_refs or {}).get("router") or "")
        fallback_ref = "router_rules@1.0.0"
        if module is None or not hasattr(module, "route"):
            return self._route_request_by_rules_impl(
                user_message=user_message,
                attachment_metas=attachment_metas,
                settings=settings,
                route_state=route_state,
                inline_followup_context=inline_followup_context,
            )
        try:
            routed = module.route(
                agent=self,
                user_message=user_message,
                attachment_metas=attachment_metas,
                settings=settings,
                route_state=route_state,
                inline_followup_context=inline_followup_context,
            )
            self._record_module_success(kind="router", selected_ref=selected_ref or fallback_ref)
            return routed
        except Exception as exc:
            self._record_module_failure(
                kind="router",
                requested_ref=selected_ref or fallback_ref,
                fallback_ref=fallback_ref,
                error=str(exc),
            )
            return self._route_request_by_rules_impl(
                user_message=user_message,
                attachment_metas=attachment_metas,
                settings=settings,
                route_state=route_state,
                inline_followup_context=inline_followup_context,
            )

    def _route_request_by_rules_impl(
        self,
        *,
        user_message: str,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        route_state: dict[str, Any] | None = None,
        inline_followup_context: bool = False,
    ) -> dict[str, Any]:
        text = (user_message or "").strip().lower()
        context_dependent_followup = self._looks_like_context_dependent_followup(user_message)
        has_attachments = bool(attachment_metas)
        spec_lookup_request = self._looks_like_spec_lookup_request(user_message, attachment_metas)
        evidence_required = self._requires_evidence_mode(user_message, attachment_metas)
        attachment_needs_tooling = any(self._attachment_needs_tooling(meta) for meta in attachment_metas)
        inline_parseable_attachments = has_attachments and all(
            self._attachment_is_inline_parseable(meta) for meta in attachment_metas
        )
        inline_document_payload = self._looks_like_inline_document_payload(user_message)
        understanding_request = self._looks_like_understanding_request(user_message)
        holistic_document_explanation = has_attachments and self._looks_like_holistic_document_explanation_request(user_message)
        source_trace_request = self._looks_like_source_trace_request(user_message)
        explicit_tool_confirmation = self._looks_like_explicit_tool_confirmation(user_message)
        meeting_minutes_request = self._looks_like_meeting_minutes_request(user_message)
        has_url = "http://" in text or "https://" in text
        short_query_like = len(text) <= 280 and "\n" not in text
        explicit_web_intent = any(hint in text for hint in ("上网", "网上", "联网", "web research", "web_research"))
        internal_ticket_reference = self._looks_like_internal_ticket_reference(user_message)
        news_request = (
            any(hint in text for hint in _NEWS_HINTS)
            and short_query_like
            and not has_attachments
            and not inline_document_payload
            and not internal_ticket_reference
        )
        heavy_web_research_markers = (
            "出处",
            "来源",
            "source",
            "链接",
            "link",
            "比较",
            "对比",
            "compare",
            "comparison",
            "核对",
            "核验",
            "verify",
            "verification",
            "fact check",
            "真假",
            "是否属实",
            "timeline",
            "时间线",
            "谣言",
        )
        explicit_news_brief_markers = (
            "news",
            "新闻",
            "ニュース",
            "headline",
            "头条",
            "热点",
            "热搜",
            "简报",
            "汇总",
        )
        web_news_brief_request = (
            news_request
            and any(marker in text for marker in explicit_news_brief_markers)
            and not any(marker in text for marker in heavy_web_research_markers)
        )
        web_request = (
            news_request
            or explicit_web_intent
            or (
                has_url
                and not internal_ticket_reference
                and not has_attachments
                and not inline_document_payload
            )
        )
        request_requires_tools = self._request_likely_requires_tools(user_message, attachment_metas)
        local_code_lookup_request = self._looks_like_local_code_lookup_request(user_message, attachment_metas)
        grounded_generation_hints = (
            "参考",
            "参照",
            "对照",
            "基于现有",
            "按现有",
            "沿用",
            "按这个目录",
            "在这个目录",
            "在该目录",
            "按这个文件",
            "参考目录",
            "reference",
            "based on",
            "according to",
            "existing code",
            "existing file",
        )
        grounded_code_generation_context = (
            has_attachments
            or inline_document_payload
            or spec_lookup_request
            or evidence_required
            or local_code_lookup_request
            or self._message_has_explicit_local_path(user_message)
            or self._has_file_like_lookup_token(text)
            or any(hint in text for hint in grounded_generation_hints)
        )
        signals = {
            "text": text,
            "attachment_metas": attachment_metas,
            "route_state": route_state or {},
            "inline_followup_context": bool(inline_followup_context),
            "context_dependent_followup": context_dependent_followup,
            "has_attachments": has_attachments,
            "spec_lookup_request": spec_lookup_request,
            "evidence_required": evidence_required,
            "attachment_needs_tooling": attachment_needs_tooling,
            "inline_parseable_attachments": inline_parseable_attachments,
            "inline_document_payload": inline_document_payload,
            "understanding_request": understanding_request,
            "holistic_document_explanation": holistic_document_explanation,
            "source_trace_request": source_trace_request,
            "explicit_tool_confirmation": explicit_tool_confirmation,
            "meeting_minutes_request": meeting_minutes_request,
            "web_news_brief_request": web_news_brief_request,
            "web_request": web_request,
            "request_requires_tools": request_requires_tools,
            "local_code_lookup_request": local_code_lookup_request,
            "grounded_code_generation_context": grounded_code_generation_context,
        }
        inherited_primary_intent = self._infer_followup_primary_intent_from_state(
            user_message=user_message,
            route_state=route_state,
            signals=signals,
        )
        if inherited_primary_intent:
            signals["inherited_primary_intent"] = inherited_primary_intent
        primary_intent = self._classify_primary_intent(
            user_message=user_message,
            attachment_metas=attachment_metas,
            route_state=route_state,
            signals=signals,
        )

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
            "primary_intent": primary_intent,
            "execution_policy": self._default_execution_policy_for_intent(primary_intent),
        }
        return self._resolve_execution_policy(
            primary_intent=primary_intent,
            user_message=user_message,
            attachment_metas=attachment_metas,
            settings=settings,
            signals=signals,
            fallback=fallback,
        )

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
        auth_summary = self._auth_manager.auth_summary()
        if not bool(auth_summary.get("available")):
            return fallback, json.dumps({"skipped": auth_summary.get("reason") or "openai_auth_missing"}, ensure_ascii=False)

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
                    "当用户在本地项目里找测试/文件/函数，且给了类似 tcg_accl0030 这类关键词时，"
                    "应优先 use_worker_tools=true；先检索，不要先追问完整文件名或扩展名。"
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
        route_state: dict[str, Any] | None = None,
        inline_followup_context: bool = False,
    ) -> tuple[dict[str, Any], str]:
        rules_route = self._route_request_by_rules(
            user_message=user_message,
            attachment_metas=attachment_metas,
            settings=settings,
            route_state=route_state,
            inline_followup_context=inline_followup_context,
        )
        if not rules_route.get("needs_llm_router"):
            return rules_route, json.dumps(
                {
                    "source": "rules",
                    "task_type": rules_route.get("task_type"),
                    "primary_intent": rules_route.get("primary_intent"),
                    "execution_policy": rules_route.get("execution_policy"),
                },
                ensure_ascii=False,
            )
        route, raw = self._run_router(
            requested_model=requested_model,
            user_message=user_message,
            summary=summary,
            attachment_metas=attachment_metas,
            settings=settings,
            rules_route=rules_route,
        )
        if (
            settings.enable_tools
            and not route.get("use_worker_tools")
            and self._should_force_initial_tool_execution(user_message, attachment_metas)
        ):
            forced_route = self._normalize_route_decision(
                {
                    "task_type": "code_lookup" if self._looks_like_local_code_lookup_request(user_message, attachment_metas) else route.get("task_type"),
                    "complexity": route.get("complexity") or "medium",
                    "use_planner": True,
                    "use_worker_tools": True,
                    "use_reviewer": False,
                    "use_revision": False,
                    "use_structurer": False,
                    "use_web_prefetch": False,
                    "use_conflict_detector": False,
                    "specialists": ["file_reader"] if self._looks_like_local_code_lookup_request(user_message, attachment_metas) else route.get("specialists") or [],
                    "reason": "backend_force_initial_tool_execution",
                    "summary": "后端判定本轮属于本地搜索/代码定位任务，直接升级为 Worker 工具链。",
                    "source": "backend_override",
                },
                fallback=route,
                settings=settings,
            )
            raw = json.dumps(
                {
                    "source": "backend_override",
                    "reason": "force_initial_tool_execution",
                    "previous_source": route.get("source"),
                    "task_type": forced_route.get("task_type"),
                },
                ensure_ascii=False,
            )
            return forced_route, raw
        return route, raw

    def _router_system_hint(self, route: dict[str, Any]) -> str:
        task_type = str(route.get("task_type") or "standard").strip()
        if task_type == "meeting_minutes":
            return (
                "本轮属于会议纪要整理任务。"
                "优先输出会议目标、关键讨论、结论、待办与负责人。"
                "不要改写成证据审计/查证报告格式，不要附加 claims 或 citations（证据来源）风格内容。"
            )
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
        if task_type == "web_news":
            return (
                "本轮属于新闻简报任务。"
                "优先抓取近期主要新闻，再直接给简明摘要。"
                "不要改写成研究报告、冲突审计或证据仲裁格式。"
            )
        if task_type == "web_research":
            return "本轮属于联网信息任务。优先用联网工具取证，再回答。"
        if task_type == "code_lookup":
            return (
                "本轮属于本地代码定位/函数解释任务。"
                "直接调用 search_codebase、list_directory、read_text_file 等工具搜索并读取上下文。"
                "不要向用户追问是否确认、是否继续，也不要要求绝对路径。"
                "当用户只给了不带扩展名的关键词时，先按 basename 模糊搜索，不要先追问扩展名。"
                "若 root='.' 首次未命中，自动在其余可访问根目录继续搜索。"
            )
        if task_type == "grounded_code_generation":
            return (
                "本轮属于基于附件、原文或现有代码的生成/改写任务。"
                "先读取相关附件或上下文，必要时先定位目标文件，再按约束实现并写入。"
                "不要把答案改写成事实审计或证据仲裁格式。"
            )
        if task_type == "code_generation":
            return (
                "本轮属于代码生成/改写任务。"
                "优先根据用户目标、现有代码与附件约束直接实现。"
                "不要把答案改写成事实审计或证据仲裁格式。"
            )
        if task_type == "evidence_lookup":
            return (
                "本轮属于查证/定位任务。优先完整取证，再给可复核答案。"
                "当用户询问“你在哪看到的/哪一页”时，先执行关键词检索并给出处位置。"
                "未实际执行检索前，不要声称“已全文搜索”。"
            )
        return ""

    def _build_execution_plan(
        self,
        attachment_metas: list[dict[str, Any]],
        settings: ChatSettings,
        route: dict[str, Any] | None = None,
    ) -> list[str]:
        route = route or {}
        specialists = self._normalize_specialists(route.get("specialists") or [])
        task_type = str(route.get("task_type") or "standard")
        primary_intent = str(route.get("primary_intent") or self._task_type_to_primary_intent(task_type))
        execution_policy = str(route.get("execution_policy") or self._task_type_to_execution_policy(task_type))
        plan = [
            (
                "Router 分诊主意图与执行链路"
                f"（task_type={task_type}, primary_intent={primary_intent}, "
                f"execution_policy={execution_policy}, complexity={str(route.get('complexity') or 'medium')}）。"
            )
        ]
        plan.append("Coordinator 持有运行时状态，决定 Worker 是否重绑工具并继续执行。")
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
        auth = self._auth_manager.require()
        provider_mode = str(auth.mode or "").strip().lower()
        registry = self._module_registry()
        provider = registry.provider_for_mode(provider_mode) if registry is not None else None
        selected_ref = str((registry.selected_refs or {}).get(f"provider:{provider_mode}") or "")
        fallback_ref = (
            "provider_codex_auth@1.0.0"
            if provider_mode == "codex_auth"
            else "provider_openai_api@1.0.0"
        )
        if provider is not None and hasattr(provider, "build_runner"):
            try:
                runner = provider.build_runner(
                    agent=self,
                    auth=auth,
                    model=model,
                    max_output_tokens=max_output_tokens,
                    use_responses_api=use_responses_api,
                )
                self._record_module_success(
                    kind="provider",
                    selected_ref=selected_ref or fallback_ref,
                    mode=provider_mode,
                )
                return runner
            except Exception as exc:
                self._record_module_failure(
                    kind="provider",
                    requested_ref=selected_ref or fallback_ref,
                    fallback_ref=fallback_ref,
                    error=str(exc),
                    mode=provider_mode,
                )
        return self._build_llm_direct_fallback(
            auth=auth,
            model=model,
            max_output_tokens=max_output_tokens,
            use_responses_api=use_responses_api,
        )

    def _build_llm_direct_fallback(
        self,
        *,
        auth: Any,
        model: str,
        max_output_tokens: int,
        use_responses_api: bool | None = None,
    ):
        if auth.mode == "codex_auth":
            return CodexResponsesRunner(
                auth_manager=self._auth_manager,
                model=model,
                max_output_tokens=max_output_tokens,
                temperature=self.config.openai_temperature,
                ai_message_cls=self._AIMessage,
            )

        selected_use_responses = self.config.openai_use_responses_api if use_responses_api is None else use_responses_api
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": auth.api_key,
            "max_tokens": max_output_tokens,
            "use_responses_api": selected_use_responses,
        }
        if self.config.openai_temperature is not None:
            kwargs["temperature"] = self.config.openai_temperature
        if self.config.openai_base_url:
            kwargs["base_url"] = self._normalize_base_url(self.config.openai_base_url)
        if self.config.openai_ca_cert_path:
            self._ensure_openai_ca_env(self.config.openai_ca_cert_path)
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
        auth = self._auth_manager.require(allow_refresh=False)
        llm = self._build_llm(model=model, max_output_tokens=max_output_tokens)
        runner = llm.bind_tools(self._select_langchain_tools(tool_names)) if enable_tools else llm
        try:
            return runner.invoke(messages), runner, notes
        except Exception as exc:
            if auth.mode == "codex_auth" or not self._is_405_error(exc):
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
            model = self._normalize_model_for_current_auth(str(raw or "").strip())
            if not model:
                continue
            key = model.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(model)
        return candidates

    def _normalize_model_for_current_auth(self, model: str) -> str:
        resolved = self._auth_manager.resolve()
        return normalize_model_for_auth_mode(model, resolved.mode)

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
                    "Supports chunked reads with start_char, and optional line-mode reads with start_line/max_lines. "
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
                description=(
                    "Search code/text files under a local root and return file, line, and excerpt matches. "
                    "If root is omitted, it defaults to '.' (the current workspace root)."
                ),
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
        return reviewer_readonly_tool_names_helper()

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
        attachment_context_available: bool = False,
    ) -> str:
        return normalize_reviewer_verdict_helper(
            self,
            raw_verdict,
            risks=risks,
            followups=followups,
            spec_lookup_request=spec_lookup_request,
            evidence_required_mode=evidence_required_mode,
            readonly_checks=readonly_checks,
            conflict_has_conflict=conflict_has_conflict,
            conflict_realtime_only=conflict_realtime_only,
            web_tools_success=web_tools_success,
            attachment_context_available=attachment_context_available,
        )

    def _summarize_reviewer_tool_result(self, *, name: str, result: dict[str, Any]) -> str:
        return summarize_reviewer_tool_result_helper(self, name=name, result=result)

    def _run_shell_tool(self, command: str, cwd: str = ".", timeout_sec: int = 15) -> str:
        result = self.tools.run_shell(command=command, cwd=cwd, timeout_sec=timeout_sec)
        return json.dumps(result, ensure_ascii=False)

    def _list_directory_tool(self, path: str = ".", max_entries: int = 200) -> str:
        result = self.tools.list_directory(path=path, max_entries=max_entries)
        return json.dumps(result, ensure_ascii=False)

    def _read_text_file_tool(
        self,
        path: str,
        start_char: int = 0,
        max_chars: int = 200000,
        start_line: int = 0,
        max_lines: int = 0,
    ) -> str:
        result = self.tools.read_text_file(
            path=path,
            start_char=start_char,
            max_chars=max_chars,
            start_line=start_line,
            max_lines=max_lines,
        )
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
        base_root = str(root or ".").strip() or "."
        try:
            base_match_count = int((result or {}).get("match_count") or len((result or {}).get("matches") or []))
        except Exception:
            base_match_count = 0
        base_ok = bool((result or {}).get("ok"))
        if (
            base_ok
            and base_match_count <= 0
            and base_root in {"", "."}
            and not bool(file_glob.strip())
            and bool(str(query or "").strip())
        ):
            searched_roots: list[str] = [str((result or {}).get("root") or base_root)]
            for candidate in self.config.allowed_roots:
                candidate_root = str(candidate)
                if candidate_root in searched_roots:
                    continue
                extra = self.tools.search_codebase(
                    query=query,
                    root=candidate_root,
                    max_matches=max_matches,
                    file_glob=file_glob,
                    use_regex=use_regex,
                    case_sensitive=case_sensitive,
                )
                searched_roots.append(str((extra or {}).get("root") or candidate_root))
                try:
                    extra_match_count = int((extra or {}).get("match_count") or len((extra or {}).get("matches") or []))
                except Exception:
                    extra_match_count = 0
                if bool((extra or {}).get("ok")) and extra_match_count > 0:
                    merged = dict(extra)
                    merged["auto_root_fallback"] = True
                    merged["initial_root"] = "."
                    merged["searched_roots"] = searched_roots
                    return json.dumps(merged, ensure_ascii=False)
            if isinstance(result, dict):
                result = dict(result)
                result["auto_root_fallback"] = True
                result["initial_root"] = "."
                result["searched_roots"] = searched_roots
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
        if self._looks_like_holistic_document_explanation_request(user_message):
            return False

        if re.search(r"(?i)\b(?:0x[0-9a-f]{1,4}|[0-9a-f]{1,4}h)\b", text):
            return True
        return text_has_any(text, SPEC_LOOKUP_HINTS)

    def _looks_like_table_reformat_request(self, text: str) -> bool:
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

    def _requires_evidence_mode(self, user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
        text = (user_message or "").strip().lower()
        if not text:
            return False
        if not attachment_metas and self._looks_like_inline_document_payload(user_message):
            return False
        if attachment_metas and self._looks_like_holistic_document_explanation_request(user_message):
            return False
        if not attachment_metas and self._looks_like_table_reformat_request(text):
            return False
        if (
            not attachment_metas
            and self._looks_like_context_dependent_followup(user_message)
            and any(
                hint in text
                for hint in (
                    "翻译",
                    "译成",
                    "译为",
                    "中文",
                    "英文",
                    "双语",
                    "总结",
                    "概括",
                    "提炼",
                    "整理",
                    "改写",
                    "润色",
                    "translate",
                )
            )
        ):
            return False
        if text_has_any(text, VERIFICATION_HINTS):
            return True
        if attachment_metas:
            return text_has_any(text, SPEC_SCOPE_HINTS)
        return False

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

    def _attachment_needs_tooling_for_turn(self, meta: dict[str, Any], history_turn_count: int = 0) -> bool:
        if self._attachment_needs_tooling(meta):
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
        return size > _FOLLOWUP_INLINE_MAX_BYTES

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
        if any(
            str(meta.get("kind") or "").strip().lower() == "document"
            and not self._attachment_is_inline_parseable(meta)
            for meta in attachment_metas
        ):
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
            "文件夹",
            "测试",
            "用例",
            "测试文件",
            "文件名",
            "扩展名",
            "函数",
            "方法",
            "代码",
            "源码",
            "代码库",
            "仓库",
            "repo",
            "项目",
            "实现",
            "调用点",
            "定义",
            "声明",
            "master",
            "source",
            "src",
            "test",
            "tests",
            "case",
            "在哪",
            "搜索",
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
            "写入",
            "替换",
            "更新",
            "改成",
            "改为",
            "保存",
            "落盘",
            "apply",
            "patch",
            "write back",
            "overwrite",
            "replace",
            "update",
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
        if self._looks_like_write_or_edit_action(text):
            return True
        if self._has_file_like_lookup_token(text):
            return True
        if re.search(r"(?:^|[\s(])(?:/[^\s]+|[A-Za-z][:\\：][\\/][^\s]*)", text):
            return True
        return any(hint in text for hint in _NEWS_HINTS)

    def _looks_like_permission_gate_text(
        self,
        text: str,
        *,
        has_attachments: bool = False,
        request_requires_tools: bool = False,
    ) -> bool:
        text = str(text or "").strip().lower()
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
            "是否直接搜索",
            "是否直接查",
            "直接搜索",
            "直接查",
            "先直接搜索",
            "先直接查",
            "能直接搜索吗",
            "可以直接搜索吗",
            "要不要直接搜索",
            "要不要我直接搜索",
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
            "同意继续",
            "回复同意继续",
            "回复“同意继续”",
            "回复'同意继续'",
            "授权继续",
            "是否可直接访问",
            "是否可以直接访问",
            "是否能直接访问",
            "可直接访问的目录",
            "可访问的目录",
            "是不是可访问",
            "请确认我可以读取",
            "请确认我能读取",
            "请确认可以读取",
            "请确认可读取",
            "请确认我可以访问",
            "请确认我可以查看",
            "请确认可访问",
            "请确认可以访问",
            "可否读取",
            "能否读取",
            "读取下面两个路径",
            "读取以下两个路径",
            "读取下列路径",
            "预览内容不完整",
            "预览不完整",
            "内容不完整（截断",
            "内容不完整(截断",
            "preview is incomplete",
            "preview was truncated",
            "content preview is truncated",
            "please confirm i can read",
            "can i read the following",
            "need to read the full file",
            "need to read the full document",
            "is workbench directly accessible",
            "is it directly accessible",
            "请提供完整文件名",
            "请给出完整文件名",
            "请提供完整的文件名",
            "请提供扩展名",
            "请给出扩展名",
            "需要扩展名",
            "需要文件扩展名",
            "需要完整文件名",
            "带扩展名",
            "完整文件名",
            "完整的文件名",
            "file extension",
            "with extension",
            "full filename",
            "exact filename",
            "请粘贴原文",
            "请贴原文",
            "请把原文贴",
            "请提供原文",
            "请先提供原文",
            "请先提供原文片段",
            "请先贴原文",
            "请先把原文贴",
            "请把代码贴出来",
            "请贴出完整代码",
            "请贴出原始代码",
            "paste the original",
            "paste the full code",
            "provide the original text",
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
            "绝对路径",
            "具体路径",
            "完整路径",
            "文件夹路径",
            "请告诉我",
            "你可以告诉我",
            "继续读取吗",
            "继续读吗",
            "继续读取其他部分",
            "继续查看其他部分",
            "需要继续读取",
            "需要继续读",
            "需要读取其他部分",
            "需要读其他部分",
            "怕太大",
            "太大",
            "文件太大",
            "内容太大",
            "最终确认",
            "确认句",
            "无需你回答",
            "不执行写入",
            "触发工具调用",
            "必须包含路径",
            "需要你同意",
            "需要你的同意",
            "需要你回复同意继续",
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
            "directly search",
            "search directly",
            "absolute path",
            "full path",
            "full filename",
            "exact filename",
            "file extension",
            "with extension",
        )
        if re.search(r"请确认.{0,24}(?:读取|访问|查看).{0,24}(?:路径|文件|附件)", text):
            return True
        if re.search(r"confirm.{0,30}(?:read|access|open).{0,30}(?:path|file|attachment)", text):
            return True
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
            "扩展名",
            "文件名",
            "解析",
            "搜索",
            "函数",
            "目录",
            "文件夹",
        )
        return any(h in text for h in file_hints)

    def _looks_like_permission_gate(
        self,
        ai_msg: Any,
        has_attachments: bool = False,
        request_requires_tools: bool = False,
    ) -> bool:
        text = self._content_to_text(getattr(ai_msg, "content", ""))
        return self._looks_like_permission_gate_text(
            text,
            has_attachments=has_attachments,
            request_requires_tools=request_requires_tools,
        )

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
