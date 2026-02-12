from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.config import AppConfig


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_workspace_path(config: AppConfig, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = config.workspace_root / path
    path = path.resolve()

    if config.allow_any_path:
        return path

    for root in config.allowed_roots:
        if _is_within(path, root):
            return path

    allowed = ", ".join(str(p) for p in config.allowed_roots)
    raise ValueError(f"Path out of allowed roots: {raw_path}. Allowed roots: {allowed}")
    return path


def _truncate_output(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[output truncated: {len(text)} chars]"


class LocalToolExecutor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "run_shell",
                "description": "Run a safe shell command in workspace. Supports simple commands without pipes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command, e.g. `ls -la` or `rg TODO .`"},
                        "cwd": {"type": "string", "description": "Working directory relative to workspace", "default": "."},
                        "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 30, "default": 15},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "list_directory",
                "description": "List files in a workspace directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "read_text_file",
                "description": "Read a UTF-8 text file in workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 128, "maximum": 50000, "default": 10000},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "fetch_web",
                "description": "Fetch web content from a URL for information lookup.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "http/https URL"},
                        "max_chars": {"type": "integer", "minimum": 512, "maximum": 120000, "default": 24000},
                        "timeout_sec": {"type": "integer", "minimum": 3, "maximum": 30, "default": 12},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "run_shell":
            return self.run_shell(**arguments)
        if name == "list_directory":
            return self.list_directory(**arguments)
        if name == "read_text_file":
            return self.read_text_file(**arguments)
        if name == "fetch_web":
            return self.fetch_web(**arguments)
        return {"ok": False, "error": f"Unknown tool: {name}"}

    def run_shell(self, command: str, cwd: str = ".", timeout_sec: int = 15) -> dict[str, Any]:
        try:
            argv = shlex.split(command)
        except Exception as exc:
            return {"ok": False, "error": f"Command parse failed: {exc}"}

        if not argv:
            return {"ok": False, "error": "Empty command"}

        if any(token in command for token in ["|", "&&", "||", ";", "$(", "`"]):
            return {
                "ok": False,
                "error": "Complex shell operators are blocked for safety. Use a single command only.",
            }

        base_cmd = argv[0]
        if base_cmd not in self.config.allowed_commands:
            return {
                "ok": False,
                "error": f"Command not allowed: {base_cmd}. Allowed: {', '.join(self.config.allowed_commands)}",
            }

        try:
            real_cwd = _resolve_workspace_path(self.config, cwd)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        try:
            proc = subprocess.run(
                argv,
                cwd=str(real_cwd),
                capture_output=True,
                text=True,
                timeout=max(1, min(30, timeout_sec)),
                check=False,
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": _truncate_output(proc.stdout),
                "stderr": _truncate_output(proc.stderr),
                "cwd": str(real_cwd),
                "command": command,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Command timed out after {timeout_sec}s"}
        except Exception as exc:
            return {"ok": False, "error": f"run_shell failed: {exc}"}

    def list_directory(self, path: str = ".", max_entries: int = 200) -> dict[str, Any]:
        try:
            real_path = _resolve_workspace_path(self.config, path)
            if not real_path.exists():
                return {"ok": False, "error": f"Path not found: {path}"}
            if not real_path.is_dir():
                return {"ok": False, "error": f"Not a directory: {path}"}

            entries = []
            for child in sorted(real_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                entries.append(
                    {
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": child.stat().st_size if child.is_file() else None,
                    }
                )
                if len(entries) >= max_entries:
                    break
            return {"ok": True, "path": str(real_path), "entries": entries}
        except Exception as exc:
            return {"ok": False, "error": f"list_directory failed: {exc}"}

    def read_text_file(self, path: str, max_chars: int = 10000) -> dict[str, Any]:
        try:
            real_path = _resolve_workspace_path(self.config, path)
            if not real_path.exists():
                return {"ok": False, "error": f"Path not found: {path}"}
            if not real_path.is_file():
                return {"ok": False, "error": f"Not a file: {path}"}

            text = real_path.read_text(encoding="utf-8", errors="ignore")
            text = text[:max_chars]
            return {"ok": True, "path": str(real_path), "content": text, "length": len(text)}
        except Exception as exc:
            return {"ok": False, "error": f"read_text_file failed: {exc}"}

    def _domain_allowed(self, host: str) -> bool:
        if self.config.web_allow_all_domains:
            return True

        host = host.lower().strip(".")
        for allowed in self.config.web_allowed_domains:
            d = allowed.lower().strip(".")
            if host == d or host.endswith("." + d):
                return True
        return False

    def fetch_web(self, url: str, max_chars: int = 24000, timeout_sec: int = 12) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return {"ok": False, "error": "Only http/https URLs are supported"}
        if not parsed.netloc:
            return {"ok": False, "error": "Invalid URL"}

        host = parsed.hostname or ""
        if not self._domain_allowed(host):
            return {
                "ok": False,
                "error": f"Domain not allowed: {host}. Allowed: {', '.join(self.config.web_allowed_domains)}",
            }

        timeout_val = max(3, min(30, timeout_sec))
        limit = max(512, min(120000, max_chars, self.config.web_fetch_max_chars))

        req = urllib.request.Request(
            url=url,
            headers={
                "User-Agent": "OffciatoolAgent/1.0 (+https://github.com/jonhncatt/offciatool)",
                "Accept": "text/html,application/json,text/plain,application/xml;q=0.9,*/*;q=0.5",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_val) as resp:
                status = getattr(resp, "status", None) or 200
                content_type = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read(limit + 1)
                truncated = len(raw) > limit
                raw = raw[:limit]

                if any(x in content_type for x in ["application/octet-stream", "image/", "audio/", "video/"]):
                    return {
                        "ok": True,
                        "url": url,
                        "status": status,
                        "content_type": content_type,
                        "binary": True,
                        "size_preview_bytes": len(raw),
                        "truncated": truncated,
                    }

                text = raw.decode("utf-8", errors="ignore")
                return {
                    "ok": True,
                    "url": url,
                    "status": status,
                    "content_type": content_type,
                    "binary": False,
                    "truncated": truncated,
                    "content": text,
                    "length": len(text),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read(4000).decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}", "body_preview": body}
        except Exception as exc:
            return {"ok": False, "error": f"fetch_web failed: {exc}"}


def parse_json_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}
