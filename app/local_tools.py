from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from app.config import AppConfig


def _resolve_workspace_path(workspace_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = workspace_root / path
    path = path.resolve()
    if path != workspace_root and workspace_root not in path.parents:
        raise ValueError(f"Path out of workspace: {raw_path}")
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
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "run_shell":
            return self.run_shell(**arguments)
        if name == "list_directory":
            return self.list_directory(**arguments)
        if name == "read_text_file":
            return self.read_text_file(**arguments)
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
            real_cwd = _resolve_workspace_path(self.config.workspace_root, cwd)
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
            real_path = _resolve_workspace_path(self.config.workspace_root, path)
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
            real_path = _resolve_workspace_path(self.config.workspace_root, path)
            if not real_path.exists():
                return {"ok": False, "error": f"Path not found: {path}"}
            if not real_path.is_file():
                return {"ok": False, "error": f"Not a file: {path}"}

            text = real_path.read_text(encoding="utf-8", errors="ignore")
            text = text[:max_chars]
            return {"ok": True, "path": str(real_path), "content": text, "length": len(text)}
        except Exception as exc:
            return {"ok": False, "error": f"read_text_file failed: {exc}"}


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
