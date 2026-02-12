from __future__ import annotations

import json
import re
import shlex
import shutil
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Any

from app.config import AppConfig


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _build_path_candidates(config: AppConfig, raw_path: str) -> list[Path]:
    raw = (raw_path or ".").strip() or "."
    path = Path(raw).expanduser()
    seen: set[str] = set()
    candidates: list[Path] = []

    def add(p: Path) -> None:
        resolved = p.resolve()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        candidates.append(resolved)

    if path.is_absolute():
        add(path)
        return candidates

    normalized = raw.replace("\\", "/").strip("/").lower()
    normalized_slash = raw.replace("\\", "/").strip("/")
    if normalized:
        # High-priority alias mapping, e.g. "workbench/a.txt" -> "<allowed_root_named_workbench>/a.txt"
        for root in config.allowed_roots:
            root_norm = str(root).replace("\\", "/").rstrip("/").lower()
            if normalized == root_norm or normalized == root.name.lower():
                add(root)
                continue
            prefix = f"{root.name.lower()}/"
            if normalized.startswith(prefix):
                suffix = normalized_slash[len(prefix) :]
                add(root / suffix)

    # Default mapping keeps backward compatibility.
    add(config.workspace_root / path)
    for root in config.allowed_roots:
        if root == config.workspace_root:
            continue
        add(root / path)

    return candidates


def _resolve_workspace_path(config: AppConfig, raw_path: str) -> Path:
    if config.allow_any_path:
        path = Path((raw_path or ".").strip() or ".").expanduser()
        if not path.is_absolute():
            path = config.workspace_root / path
        path = path.resolve()
        return path

    candidates = _build_path_candidates(config, raw_path)

    # Prefer existing paths in allowed roots for better UX with relative inputs.
    for path in candidates:
        for root in config.allowed_roots:
            if _is_within(path, root) and path.exists():
                return path

    # Fall back to first allowed candidate even if it does not exist,
    # prefer a candidate whose parent directory exists.
    for path in candidates:
        for root in config.allowed_roots:
            if _is_within(path, root) and path.parent.exists():
                return path

    # Last resort: return first allowed candidate even if parent does not exist,
    # so upper layers can return a clear "not found" error.
    for root in config.allowed_roots:
        for path in candidates:
            if _is_within(path, root):
                return path

    allowed = ", ".join(str(p) for p in config.allowed_roots)
    raise ValueError(f"Path out of allowed roots: {raw_path}. Allowed roots: {allowed}")


def _truncate_output(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[output truncated: {len(text)} chars]"


def _looks_like_html(content_type: str, text: str) -> bool:
    lower_ct = (content_type or "").lower()
    if "text/html" in lower_ct or "application/xhtml+xml" in lower_ct:
        return True
    head = text[:400].lower()
    return "<html" in head or "<!doctype html" in head


def _extract_html_text(raw_html: str, max_chars: int) -> str:
    html = re.sub(r"(?is)<!--.*?-->", " ", raw_html)
    html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h1|h2|h3|h4|h5|h6|section|article)>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = unescape(html)

    lines: list[str] = []
    for line in html.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            lines.append(normalized)

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


def _looks_like_script_payload(text: str) -> bool:
    sample = (text or "")[:6000].lower()
    if not sample:
        return False

    if "sourcemappingurl=" in sample:
        return True

    markers = [
        "function(",
        "var ",
        "const ",
        "let ",
        "window.",
        "document.",
        "=>",
    ]
    hits = sum(1 for m in markers if m in sample)
    longest_line = max((len(line) for line in sample.splitlines()), default=0)
    punct = sum(ch in "{}[]();=<>/\\*" for ch in sample)
    alpha = sum(ch.isalpha() for ch in sample) or 1
    punct_ratio = punct / alpha

    return (hits >= 3 and longest_line >= 220) or punct_ratio >= 0.45


def _normalize_url_for_request(raw_url: str) -> str:
    """
    Make URL safe for urllib by encoding non-ASCII host/path/query.
    """
    url = (raw_url or "").strip()
    parsed = urllib.parse.urlsplit(url)

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported")
    if not parsed.netloc:
        raise ValueError("Invalid URL")

    host = parsed.hostname or ""
    if not host:
        raise ValueError("Invalid URL")
    host_ascii = host.encode("idna").decode("ascii")

    auth = ""
    if parsed.username is not None:
        auth = urllib.parse.quote(parsed.username, safe="")
        if parsed.password is not None:
            auth += ":" + urllib.parse.quote(parsed.password, safe="")
        auth += "@"

    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{auth}{host_ascii}{port}"

    path = urllib.parse.quote(urllib.parse.unquote(parsed.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(urllib.parse.unquote(parsed.query or ""), safe="=&%:@!$'()*+,;/-._~")

    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _is_cert_verify_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "certificate_verify_failed" in text or "certificate verify failed" in text


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
                "name": "copy_file",
                "description": "Copy a file (binary-safe) from src_path to dst_path in allowed roots.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "src_path": {"type": "string"},
                        "dst_path": {"type": "string"},
                        "overwrite": {"type": "boolean", "default": True},
                        "create_dirs": {"type": "boolean", "default": True},
                    },
                    "required": ["src_path", "dst_path"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "write_text_file",
                "description": "Create or overwrite a UTF-8 text file in workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "overwrite": {"type": "boolean", "default": True},
                        "create_dirs": {"type": "boolean", "default": True},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "replace_in_file",
                "description": "Replace target text in a UTF-8 text file in workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                        "max_replacements": {"type": "integer", "minimum": 1, "maximum": 200, "default": 1},
                    },
                    "required": ["path", "old_text", "new_text"],
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
        if name == "copy_file":
            return self.copy_file(**arguments)
        if name == "write_text_file":
            return self.write_text_file(**arguments)
        if name == "replace_in_file":
            return self.replace_in_file(**arguments)
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

            full_text = real_path.read_text(encoding="utf-8", errors="ignore")
            total_length = len(full_text)
            text = full_text[:max_chars]
            truncated = total_length > len(text)
            return {
                "ok": True,
                "path": str(real_path),
                "content": text,
                "length": len(text),
                "total_length": total_length,
                "truncated": truncated,
            }
        except Exception as exc:
            return {"ok": False, "error": f"read_text_file failed: {exc}"}

    def copy_file(
        self, src_path: str, dst_path: str, overwrite: bool = True, create_dirs: bool = True
    ) -> dict[str, Any]:
        try:
            src_real = _resolve_workspace_path(self.config, src_path)
            dst_real = _resolve_workspace_path(self.config, dst_path)

            if not src_real.exists():
                return {"ok": False, "error": f"Source path not found: {src_path}"}
            if not src_real.is_file():
                return {"ok": False, "error": f"Source is not a file: {src_path}"}
            if src_real == dst_real:
                return {"ok": False, "error": "Source and destination are the same file"}

            if dst_real.exists() and dst_real.is_dir():
                return {"ok": False, "error": f"Destination is a directory: {dst_path}"}
            if dst_real.exists() and not overwrite:
                return {"ok": False, "error": f"Destination exists and overwrite=false: {dst_path}"}

            if not dst_real.parent.exists():
                if not create_dirs:
                    return {"ok": False, "error": f"Destination parent not found: {dst_real.parent}"}
                dst_real.parent.mkdir(parents=True, exist_ok=True)

            action = "overwrite" if dst_real.exists() else "create"
            shutil.copy2(src_real, dst_real)
            return {
                "ok": True,
                "src_path": str(src_real),
                "dst_path": str(dst_real),
                "action": action,
                "bytes": dst_real.stat().st_size,
            }
        except Exception as exc:
            return {"ok": False, "error": f"copy_file failed: {exc}"}

    def write_text_file(
        self, path: str, content: str, overwrite: bool = True, create_dirs: bool = True
    ) -> dict[str, Any]:
        try:
            real_path = _resolve_workspace_path(self.config, path)
            if real_path.exists() and real_path.is_dir():
                return {"ok": False, "error": f"Path is a directory, not a file: {path}"}

            if real_path.exists() and not overwrite:
                return {"ok": False, "error": f"File already exists and overwrite=false: {path}"}

            if not real_path.parent.exists():
                if not create_dirs:
                    return {"ok": False, "error": f"Parent directory not found: {real_path.parent}"}
                real_path.parent.mkdir(parents=True, exist_ok=True)

            action = "overwrite" if real_path.exists() else "create"
            real_path.write_text(content, encoding="utf-8")
            return {
                "ok": True,
                "path": str(real_path),
                "action": action,
                "chars": len(content),
                "bytes_utf8": len(content.encode("utf-8")),
            }
        except Exception as exc:
            return {"ok": False, "error": f"write_text_file failed: {exc}"}

    def replace_in_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        max_replacements: int = 1,
    ) -> dict[str, Any]:
        if not old_text:
            return {"ok": False, "error": "old_text cannot be empty"}
        if max_replacements < 1:
            return {"ok": False, "error": "max_replacements must be >= 1"}

        try:
            real_path = _resolve_workspace_path(self.config, path)
            if not real_path.exists():
                return {"ok": False, "error": f"Path not found: {path}"}
            if not real_path.is_file():
                return {"ok": False, "error": f"Not a file: {path}"}

            source = real_path.read_text(encoding="utf-8", errors="ignore")
            found = source.count(old_text)
            if found <= 0:
                return {"ok": False, "error": "Target text not found", "path": str(real_path)}

            limit = found if replace_all else min(found, max(1, min(200, max_replacements)))
            updated = source.replace(old_text, new_text, limit)
            real_path.write_text(updated, encoding="utf-8")
            return {
                "ok": True,
                "path": str(real_path),
                "replacements": limit,
                "remaining_matches": max(0, found - limit),
                "chars": len(updated),
                "bytes_utf8": len(updated.encode("utf-8")),
            }
        except Exception as exc:
            return {"ok": False, "error": f"replace_in_file failed: {exc}"}

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

        try:
            request_url = _normalize_url_for_request(url)
        except Exception as exc:
            return {"ok": False, "error": f"Invalid URL: {exc}"}

        host = parsed.hostname or ""
        if not self._domain_allowed(host):
            return {
                "ok": False,
                "error": f"Domain not allowed: {host}. Allowed: {', '.join(self.config.web_allowed_domains)}",
            }

        timeout_val = max(3, min(30, timeout_sec))
        limit = max(512, min(120000, max_chars, self.config.web_fetch_max_chars))
        ssl_context: ssl.SSLContext | None = None
        if parsed.scheme == "https":
            if self.config.web_skip_tls_verify:
                ssl_context = ssl._create_unverified_context()
            elif self.config.web_ca_cert_path:
                try:
                    ssl_context = ssl.create_default_context(cafile=self.config.web_ca_cert_path)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"Invalid web CA cert path: {self.config.web_ca_cert_path} ({exc})",
                    }
            else:
                ssl_context = ssl.create_default_context()

        req = urllib.request.Request(
            url=request_url,
            headers={
                "User-Agent": "OfficetoolAgent/1.0",
                "Accept": "text/html,application/json,text/plain,application/xml;q=0.9,*/*;q=0.5",
            },
            method="GET",
        )

        tls_warning: str | None = None

        def _open(current_context: ssl.SSLContext | None):
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=current_context))
            return opener.open(req, timeout=timeout_val)

        try:
            try:
                resp_cm = _open(ssl_context)
            except Exception as first_exc:
                # Pragmatic fallback for enterprise TLS chains:
                # if verification fails and user did not explicitly disable it,
                # retry once with verification off for fetch_web only.
                if not self.config.web_skip_tls_verify and _is_cert_verify_error(first_exc):
                    tls_warning = "TLS verify failed; fetch_web auto-retried with verify disabled."
                    resp_cm = _open(ssl._create_unverified_context())
                else:
                    raise

            with resp_cm as resp:
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
                        "warning": tls_warning,
                    }

                text = raw.decode("utf-8", errors="ignore")
                if _looks_like_html(content_type, text):
                    extracted = _extract_html_text(text, max_chars=limit)
                    warning = None
                    if len(extracted.strip()) < 80:
                        warning = (
                            "页面正文较少，可能是 JS 动态渲染或反爬页面。"
                            "建议改用该站点公开 API，或换一个可直读正文的页面。"
                        )
                    if _looks_like_script_payload(extracted):
                        script_warning = (
                            "抓取内容疑似脚本/反爬响应，而非正文页面。"
                            "请不要据此下结论，建议改用官方 API 或可直读页面。"
                        )
                        warning = f"{script_warning} {warning}" if warning else script_warning
                    if tls_warning:
                        warning = f"{tls_warning} {warning}" if warning else tls_warning
                    return {
                        "ok": True,
                        "url": url,
                        "status": status,
                        "content_type": content_type,
                        "binary": False,
                        "truncated": truncated,
                        "content": extracted,
                        "length": len(extracted),
                        "source_format": "html_text_extracted",
                        "warning": warning,
                    }

                return {
                    "ok": True,
                    "url": url,
                    "status": status,
                    "content_type": content_type,
                    "binary": False,
                    "truncated": truncated,
                    "content": text,
                    "length": len(text),
                    "warning": tls_warning,
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
