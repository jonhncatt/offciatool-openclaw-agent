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


def _env(*keys: str, default: str | None = None) -> str | None:
    for key in keys:
        if key in os.environ:
            return os.environ.get(key)
    return default


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        return value[1:-1]
    return value


def _should_dotenv_override(key: str) -> bool:
    normalized = key.strip().upper()
    if normalized.startswith("OFFICETOOL_") or normalized.startswith("OFFCIATOOL_"):
        return True
    return normalized in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}


def _load_dotenv_if_present() -> None:
    candidates = [
        (Path.cwd() / ".env").resolve(),
        (Path(__file__).resolve().parent.parent / ".env").resolve(),
    ]

    seen: set[str] = set()
    for dotenv_path in candidates:
        key = str(dotenv_path)
        if key in seen or not dotenv_path.is_file():
            continue
        seen.add(key)

        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue

            env_key, env_value = line.split("=", 1)
            env_key = env_key.strip()
            env_value = env_value.strip()
            if not env_key:
                continue

            env_value = _strip_optional_quotes(env_value)
            if " #" in env_value:
                env_value = env_value.split(" #", 1)[0].rstrip()

            if _should_dotenv_override(env_key):
                os.environ[env_key] = env_value
            else:
                os.environ.setdefault(env_key, env_value)


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
    web_skip_tls_verify: bool
    web_ca_cert_path: str | None
    openai_base_url: str | None
    openai_ca_cert_path: str | None
    openai_temperature: float | None
    openai_use_responses_api: bool
    default_model: str
    model_fallbacks: list[str]
    model_cooldown_base_sec: int
    model_cooldown_max_sec: int
    summary_model: str
    system_prompt: str
    summary_trigger_turns: int
    max_context_turns: int
    max_attachment_chars: int
    max_upload_mb: int
    tool_result_soft_trim_chars: int
    tool_result_hard_clear_chars: int
    tool_result_head_chars: int
    tool_result_tail_chars: int
    tool_context_prune_keep_last: int
    max_concurrent_runs: int
    run_queue_wait_notice_ms: int
    execution_mode: str
    docker_image: str
    docker_network: str
    docker_memory: str
    docker_cpus: str
    docker_pids_limit: int
    docker_container_prefix: str
    enable_session_tools: bool
    allowed_commands: list[str]


DEFAULT_SYSTEM_PROMPT = (
    "你是一个办公室效率助手。优先给可执行结论和下一步动作，输出简洁。"
    "如果用户提供图片或文档，先提炼关键信息再回答。"
    "当需要读取本地信息时可调用工具；调用前先判断是否必要。"
)


def load_config() -> AppConfig:
    _load_dotenv_if_present()

    workspace_root = Path(_env("OFFICETOOL_WORKSPACE_ROOT", "OFFCIATOOL_WORKSPACE_ROOT", default=os.getcwd()) or os.getcwd()).resolve()
    sessions_dir = Path(
        _env(
            "OFFICETOOL_SESSIONS_DIR",
            "OFFCIATOOL_SESSIONS_DIR",
            default=str(workspace_root / "app" / "data" / "sessions"),
        )
        or str(workspace_root / "app" / "data" / "sessions")
    ).resolve()
    uploads_dir = Path(
        _env(
            "OFFICETOOL_UPLOADS_DIR",
            "OFFCIATOOL_UPLOADS_DIR",
            default=str(workspace_root / "app" / "data" / "uploads"),
        )
        or str(workspace_root / "app" / "data" / "uploads")
    ).resolve()
    token_stats_path = Path(
        _env(
            "OFFICETOOL_TOKEN_STATS_PATH",
            "OFFCIATOOL_TOKEN_STATS_PATH",
            default=str(workspace_root / "app" / "data" / "token_stats.json"),
        )
        or str(workspace_root / "app" / "data" / "token_stats.json")
    ).resolve()

    sessions_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    token_stats_path.parent.mkdir(parents=True, exist_ok=True)

    allowed_commands_raw = _env(
        "OFFICETOOL_ALLOWED_COMMANDS",
        "OFFCIATOOL_ALLOWED_COMMANDS",
        default="pwd,ls,cat,rg,head,tail,wc,find,echo,date,python3,git,npm,node,pytest,sed,awk,mkdir,touch,cp,mv",
    ) or "pwd,ls,cat,rg,head,tail,wc,find,echo,date,python3,git,npm,node,pytest,sed,awk,mkdir,touch,cp,mv"

    openai_base_url = (
        _env("OFFICETOOL_OPENAI_BASE_URL", "OFFCIATOOL_OPENAI_BASE_URL", "OPENAI_BASE_URL", default="") or ""
    ).strip() or None
    openai_ca_cert_path = (
        _env("OFFICETOOL_CA_CERT_PATH", "OFFCIATOOL_CA_CERT_PATH", "SSL_CERT_FILE", default="") or ""
    ).strip() or None
    openai_temperature_raw = (
        _env("OFFICETOOL_TEMPERATURE", "OFFCIATOOL_TEMPERATURE", default="") or ""
    ).strip()
    openai_temperature: float | None = None
    if openai_temperature_raw:
        try:
            openai_temperature = float(openai_temperature_raw)
        except Exception:
            openai_temperature = None

    use_responses_raw = (
        _env("OFFICETOOL_USE_RESPONSES_API", "OFFCIATOOL_USE_RESPONSES_API", default="false") or "false"
    ).strip().lower()
    openai_use_responses_api = use_responses_raw in {"1", "true", "yes", "on"}

    model_fallbacks = _split_csv(
        _env("OFFICETOOL_MODEL_FALLBACKS", "OFFCIATOOL_MODEL_FALLBACKS", default="") or ""
    )
    model_cooldown_base_sec = int(
        (
            _env(
                "OFFICETOOL_MODEL_COOLDOWN_BASE_SEC",
                "OFFCIATOOL_MODEL_COOLDOWN_BASE_SEC",
                default="60",
            )
            or "60"
        ).strip()
    )
    model_cooldown_max_sec = int(
        (
            _env(
                "OFFICETOOL_MODEL_COOLDOWN_MAX_SEC",
                "OFFCIATOOL_MODEL_COOLDOWN_MAX_SEC",
                default="3600",
            )
            or "3600"
        ).strip()
    )

    allow_any_raw = (_env("OFFICETOOL_ALLOW_ANY_PATH", "OFFCIATOOL_ALLOW_ANY_PATH", default="false") or "false").strip().lower()
    allow_any_path = allow_any_raw in {"1", "true", "yes", "on"}

    default_workbench_root = str((Path.home() / "Desktop" / "workbench").resolve())
    extra_allowed_roots_raw = (
        _env(
            "OFFICETOOL_EXTRA_ALLOWED_ROOTS",
            "OFFCIATOOL_EXTRA_ALLOWED_ROOTS",
            default=default_workbench_root,
        )
        or ""
    ).strip()
    extra_allowed_roots = [Path(item).resolve() for item in _split_paths(extra_allowed_roots_raw)]

    web_domains_raw = (_env("OFFICETOOL_WEB_ALLOWED_DOMAINS", "OFFCIATOOL_WEB_ALLOWED_DOMAINS", default="") or "").strip()
    web_allowed_domains = _split_csv(web_domains_raw)
    web_allow_all_domains = len(web_allowed_domains) == 0

    web_fetch_timeout_sec = int(
        (_env("OFFICETOOL_WEB_FETCH_TIMEOUT_SEC", "OFFCIATOOL_WEB_FETCH_TIMEOUT_SEC", default="12") or "12").strip()
    )
    web_fetch_max_chars = int(
        (_env("OFFICETOOL_WEB_FETCH_MAX_CHARS", "OFFCIATOOL_WEB_FETCH_MAX_CHARS", default="120000") or "120000").strip()
    )
    web_skip_tls_verify_raw = (
        _env("OFFICETOOL_WEB_SKIP_TLS_VERIFY", "OFFCIATOOL_WEB_SKIP_TLS_VERIFY", default="false") or "false"
    ).strip().lower()
    web_skip_tls_verify = web_skip_tls_verify_raw in {"1", "true", "yes", "on"}
    web_ca_cert_path = (
        _env(
            "OFFICETOOL_WEB_CA_CERT_PATH",
            "OFFCIATOOL_WEB_CA_CERT_PATH",
            default=(openai_ca_cert_path or ""),
        )
        or ""
    ).strip() or None

    allowed_roots: list[Path] = []
    seen: set[str] = set()
    for root in [workspace_root, *extra_allowed_roots]:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        allowed_roots.append(root)

    tool_result_soft_trim_chars = int(
        (
            _env(
                "OFFICETOOL_TOOL_RESULT_SOFT_TRIM_CHARS",
                "OFFCIATOOL_TOOL_RESULT_SOFT_TRIM_CHARS",
                default="40000",
            )
            or "40000"
        ).strip()
    )
    tool_result_hard_clear_chars = int(
        (
            _env(
                "OFFICETOOL_TOOL_RESULT_HARD_CLEAR_CHARS",
                "OFFCIATOOL_TOOL_RESULT_HARD_CLEAR_CHARS",
                default="180000",
            )
            or "180000"
        ).strip()
    )
    tool_result_head_chars = int(
        (
            _env(
                "OFFICETOOL_TOOL_RESULT_HEAD_CHARS",
                "OFFCIATOOL_TOOL_RESULT_HEAD_CHARS",
                default="8000",
            )
            or "8000"
        ).strip()
    )
    tool_result_tail_chars = int(
        (
            _env(
                "OFFICETOOL_TOOL_RESULT_TAIL_CHARS",
                "OFFCIATOOL_TOOL_RESULT_TAIL_CHARS",
                default="4000",
            )
            or "4000"
        ).strip()
    )
    tool_context_prune_keep_last = int(
        (
            _env(
                "OFFICETOOL_TOOL_CONTEXT_PRUNE_KEEP_LAST",
                "OFFCIATOOL_TOOL_CONTEXT_PRUNE_KEEP_LAST",
                default="3",
            )
            or "3"
        ).strip()
    )
    max_concurrent_runs = int(
        (
            _env("OFFICETOOL_MAX_CONCURRENT_RUNS", "OFFCIATOOL_MAX_CONCURRENT_RUNS", default="2")
            or "2"
        ).strip()
    )
    run_queue_wait_notice_ms = int(
        (
            _env(
                "OFFICETOOL_RUN_QUEUE_WAIT_NOTICE_MS",
                "OFFCIATOOL_RUN_QUEUE_WAIT_NOTICE_MS",
                default="1500",
            )
            or "1500"
        ).strip()
    )
    execution_mode = (
        _env("OFFICETOOL_EXECUTION_MODE", "OFFCIATOOL_EXECUTION_MODE", default="host") or "host"
    ).strip().lower()
    if execution_mode not in {"host", "docker"}:
        execution_mode = "host"
    docker_image = (
        _env("OFFICETOOL_DOCKER_IMAGE", "OFFCIATOOL_DOCKER_IMAGE", default="python:3.11-slim")
        or "python:3.11-slim"
    ).strip()
    docker_network = (
        _env("OFFICETOOL_DOCKER_NETWORK", "OFFCIATOOL_DOCKER_NETWORK", default="none") or "none"
    ).strip()
    docker_memory = (
        _env("OFFICETOOL_DOCKER_MEMORY", "OFFCIATOOL_DOCKER_MEMORY", default="2g") or "2g"
    ).strip()
    docker_cpus = (
        _env("OFFICETOOL_DOCKER_CPUS", "OFFCIATOOL_DOCKER_CPUS", default="1.0") or "1.0"
    ).strip()
    docker_pids_limit = int(
        (_env("OFFICETOOL_DOCKER_PIDS_LIMIT", "OFFCIATOOL_DOCKER_PIDS_LIMIT", default="256") or "256").strip()
    )
    docker_container_prefix = (
        _env("OFFICETOOL_DOCKER_CONTAINER_PREFIX", "OFFCIATOOL_DOCKER_CONTAINER_PREFIX", default="officetool-sbx")
        or "officetool-sbx"
    ).strip()
    enable_session_tools_raw = (
        _env("OFFICETOOL_ENABLE_SESSION_TOOLS", "OFFCIATOOL_ENABLE_SESSION_TOOLS", default="true") or "true"
    ).strip().lower()
    enable_session_tools = enable_session_tools_raw in {"1", "true", "yes", "on"}

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
        web_fetch_max_chars=max(2000, min(500000, web_fetch_max_chars)),
        web_skip_tls_verify=web_skip_tls_verify,
        web_ca_cert_path=web_ca_cert_path,
        openai_base_url=openai_base_url,
        openai_ca_cert_path=openai_ca_cert_path,
        openai_temperature=openai_temperature,
        openai_use_responses_api=openai_use_responses_api,
        default_model=(
            _env("OFFICETOOL_DEFAULT_MODEL", "OFFCIATOOL_DEFAULT_MODEL", default="gpt-5.1-chat") or "gpt-5.1-chat"
        ),
        model_fallbacks=model_fallbacks,
        model_cooldown_base_sec=max(10, min(3600, model_cooldown_base_sec)),
        model_cooldown_max_sec=max(60, min(86400, model_cooldown_max_sec)),
        summary_model=(
            _env(
                "OFFICETOOL_SUMMARY_MODEL",
                "OFFICETOOL_SUMMARY_MODE",
                "OFFCIATOOL_SUMMARY_MODEL",
                "OFFCIATOOL_SUMMARY_MODE",
                default="gpt-5.1-chat",
            )
            or "gpt-5.1-chat"
        ),
        system_prompt=_env("OFFICETOOL_SYSTEM_PROMPT", "OFFCIATOOL_SYSTEM_PROMPT", default=DEFAULT_SYSTEM_PROMPT)
        or DEFAULT_SYSTEM_PROMPT,
        summary_trigger_turns=max(
            6,
            min(
                10000,
                int(
                    _env("OFFICETOOL_SUMMARY_TRIGGER_TURNS", "OFFCIATOOL_SUMMARY_TRIGGER_TURNS", default="2000")
                    or "2000"
                ),
            ),
        ),
        max_context_turns=max(
            2,
            min(
                2000,
                int(_env("OFFICETOOL_MAX_CONTEXT_TURNS", "OFFCIATOOL_MAX_CONTEXT_TURNS", default="2000") or "2000"),
            ),
        ),
        max_attachment_chars=max(
            2000,
            min(
                1000000,
                int(
                    _env("OFFICETOOL_MAX_ATTACHMENT_CHARS", "OFFCIATOOL_MAX_ATTACHMENT_CHARS", default="1000000")
                    or "1000000"
                ),
            ),
        ),
        max_upload_mb=max(
            1,
            min(2048, int(_env("OFFICETOOL_MAX_UPLOAD_MB", "OFFCIATOOL_MAX_UPLOAD_MB", default="200") or "200")),
        ),
        tool_result_soft_trim_chars=max(2000, min(1_000_000, tool_result_soft_trim_chars)),
        tool_result_hard_clear_chars=max(4000, min(2_000_000, tool_result_hard_clear_chars)),
        tool_result_head_chars=max(500, min(200_000, tool_result_head_chars)),
        tool_result_tail_chars=max(500, min(200_000, tool_result_tail_chars)),
        tool_context_prune_keep_last=max(0, min(20, tool_context_prune_keep_last)),
        max_concurrent_runs=max(1, min(32, max_concurrent_runs)),
        run_queue_wait_notice_ms=max(0, min(120_000, run_queue_wait_notice_ms)),
        execution_mode=execution_mode,
        docker_image=docker_image or "python:3.11-slim",
        docker_network=docker_network or "none",
        docker_memory=docker_memory or "2g",
        docker_cpus=docker_cpus or "1.0",
        docker_pids_limit=max(16, min(4096, docker_pids_limit)),
        docker_container_prefix=docker_container_prefix or "officetool-sbx",
        enable_session_tools=enable_session_tools,
        allowed_commands=_split_csv(allowed_commands_raw),
    )
