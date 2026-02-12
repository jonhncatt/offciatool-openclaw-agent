from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class AppConfig:
    workspace_root: Path
    sessions_dir: Path
    uploads_dir: Path
    openai_base_url: str | None
    default_model: str
    summary_model: str
    system_prompt: str
    summary_trigger_turns: int
    max_context_turns: int
    max_attachment_chars: int
    max_upload_mb: int
    allowed_commands: list[str]


DEFAULT_SYSTEM_PROMPT = (
    "你是一个办公室效率助手。优先给可执行结论和下一步动作，输出简洁。"
    "如果用户提供图片或文档，先提炼关键信息再回答。"
    "当需要读取本地信息时可调用工具；调用前先判断是否必要。"
)


def load_config() -> AppConfig:
    workspace_root = Path(os.environ.get("OFFCIATOOL_WORKSPACE_ROOT", os.getcwd())).resolve()
    sessions_dir = Path(os.environ.get("OFFCIATOOL_SESSIONS_DIR", workspace_root / "app" / "data" / "sessions")).resolve()
    uploads_dir = Path(os.environ.get("OFFCIATOOL_UPLOADS_DIR", workspace_root / "app" / "data" / "uploads")).resolve()

    sessions_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    allowed_commands_raw = os.environ.get(
        "OFFCIATOOL_ALLOWED_COMMANDS",
        "pwd,ls,cat,rg,head,tail,wc,find,echo,date,python3,git,npm,node,pytest,sed,awk,mkdir,touch,cp,mv",
    )

    openai_base_url = (os.environ.get("OFFCIATOOL_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip() or None

    return AppConfig(
        workspace_root=workspace_root,
        sessions_dir=sessions_dir,
        uploads_dir=uploads_dir,
        openai_base_url=openai_base_url,
        default_model=os.environ.get("OFFCIATOOL_DEFAULT_MODEL", "gpt-4.1"),
        summary_model=os.environ.get("OFFCIATOOL_SUMMARY_MODEL", "gpt-4.1-mini"),
        system_prompt=os.environ.get("OFFCIATOOL_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        summary_trigger_turns=int(os.environ.get("OFFCIATOOL_SUMMARY_TRIGGER_TURNS", "24")),
        max_context_turns=int(os.environ.get("OFFCIATOOL_MAX_CONTEXT_TURNS", "12")),
        max_attachment_chars=int(os.environ.get("OFFCIATOOL_MAX_ATTACHMENT_CHARS", "24000")),
        max_upload_mb=int(os.environ.get("OFFCIATOOL_MAX_UPLOAD_MB", "20")),
        allowed_commands=_split_csv(allowed_commands_raw),
    )
