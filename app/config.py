from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _split_paths(raw: str) -> list[str]:
    if not raw:
        return []
    merged = raw.replace(",", os.pathsep)
    return [item.strip() for item in merged.split(os.pathsep) if item.strip()]


@dataclass(slots=True)
class AppConfig:
    workspace_root: Path
    sessions_dir: Path
    uploads_dir: Path
    token_stats_path: Path
    allowed_roots: list[Path]
    allow_any_path: bool
    web_allowed_domains: list[str]
    web_allow_all_domains: bool
    web_fetch_timeout_sec: int
    web_fetch_max_chars: int
    openai_base_url: str | None
    openai_ca_cert_path: str | None
    openai_use_responses_api: bool
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
    token_stats_path = Path(
        os.environ.get("OFFCIATOOL_TOKEN_STATS_PATH", workspace_root / "app" / "data" / "token_stats.json")
    ).resolve()

    sessions_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    token_stats_path.parent.mkdir(parents=True, exist_ok=True)

    allowed_commands_raw = os.environ.get(
        "OFFCIATOOL_ALLOWED_COMMANDS",
        "pwd,ls,cat,rg,head,tail,wc,find,echo,date,python3,git,npm,node,pytest,sed,awk,mkdir,touch,cp,mv",
    )

    openai_base_url = (os.environ.get("OFFCIATOOL_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip() or None
    openai_ca_cert_path = (
        os.environ.get("OFFCIATOOL_CA_CERT_PATH")
        or os.environ.get("SSL_CERT_FILE")
        or ""
    ).strip() or None
    use_responses_raw = (os.environ.get("OFFCIATOOL_USE_RESPONSES_API") or "false").strip().lower()
    openai_use_responses_api = use_responses_raw in {"1", "true", "yes", "on"}
    allow_any_raw = (os.environ.get("OFFCIATOOL_ALLOW_ANY_PATH") or "false").strip().lower()
    allow_any_path = allow_any_raw in {"1", "true", "yes", "on"}
    extra_allowed_roots_raw = os.environ.get("OFFCIATOOL_EXTRA_ALLOWED_ROOTS", "").strip()
    extra_allowed_roots = [Path(item).resolve() for item in _split_paths(extra_allowed_roots_raw)]
    web_domains_raw = os.environ.get("OFFCIATOOL_WEB_ALLOWED_DOMAINS", "").strip()
    web_allowed_domains = _split_csv(web_domains_raw)
    web_allow_all_domains = len(web_allowed_domains) == 0
    web_fetch_timeout_sec = int(os.environ.get("OFFCIATOOL_WEB_FETCH_TIMEOUT_SEC", "12"))
    web_fetch_max_chars = int(os.environ.get("OFFCIATOOL_WEB_FETCH_MAX_CHARS", "24000"))

    allowed_roots: list[Path] = []
    seen: set[str] = set()
    for root in [workspace_root, *extra_allowed_roots]:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        allowed_roots.append(root)

    return AppConfig(
        workspace_root=workspace_root,
        sessions_dir=sessions_dir,
        uploads_dir=uploads_dir,
        token_stats_path=token_stats_path,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
        web_allowed_domains=web_allowed_domains,
        web_allow_all_domains=web_allow_all_domains,
        web_fetch_timeout_sec=max(3, min(30, web_fetch_timeout_sec)),
        web_fetch_max_chars=max(2000, min(120000, web_fetch_max_chars)),
        openai_base_url=openai_base_url,
        openai_ca_cert_path=openai_ca_cert_path,
        openai_use_responses_api=openai_use_responses_api,
        default_model=os.environ.get("OFFCIATOOL_DEFAULT_MODEL", "gpt-4.1"),
        summary_model=os.environ.get("OFFCIATOOL_SUMMARY_MODEL", "gpt-4.1-mini"),
        system_prompt=os.environ.get("OFFCIATOOL_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        summary_trigger_turns=int(os.environ.get("OFFCIATOOL_SUMMARY_TRIGGER_TURNS", "24")),
        max_context_turns=int(os.environ.get("OFFCIATOOL_MAX_CONTEXT_TURNS", "12")),
        max_attachment_chars=int(os.environ.get("OFFCIATOOL_MAX_ATTACHMENT_CHARS", "24000")),
        max_upload_mb=int(os.environ.get("OFFCIATOOL_MAX_UPLOAD_MB", "20")),
        allowed_commands=_split_csv(allowed_commands_raw),
    )
