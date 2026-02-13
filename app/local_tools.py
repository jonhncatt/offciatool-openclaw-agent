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
import xml.etree.ElementTree as ET
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


def _extract_search_query(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return None

    host = (parsed.hostname or "").lower()
    if not host:
        return None

    q = urllib.parse.parse_qs(parsed.query or "")
    key = None
    if "google." in host or "bing." in host:
        key = "q"
    elif "yahoo." in host:
        key = "p"
    elif "baidu." in host:
        key = "wd"

    if not key:
        return None
    vals = q.get(key) or []
    if not vals:
        return None
    out = (vals[0] or "").strip()
    return out or None


def _clean_html_fragment(raw_html: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_ddg_redirect(raw_url: str) -> str:
    if not raw_url:
        return raw_url
    url = unescape(raw_url).strip()
    absolute = urllib.parse.urljoin("https://duckduckgo.com", url)
    try:
        parsed = urllib.parse.urlsplit(absolute)
    except Exception:
        return absolute

    host = (parsed.hostname or "").lower()
    if host.endswith("duckduckgo.com") and parsed.path == "/l/":
        q = urllib.parse.parse_qs(parsed.query or "")
        target = (q.get("uddg") or [""])[0].strip()
        if target:
            return urllib.parse.unquote(target)
    return absolute


def _extract_ddg_results(raw_html: str, max_results: int) -> list[dict[str, str]]:
    html = raw_html or ""
    limit = max(1, min(20, int(max_results)))
    patterns = [
        re.compile(
            r'(?is)<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        ),
        re.compile(
            r"(?is)<a[^>]*class=['\"][^'\"]*result-link[^'\"]*['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>"
        ),
    ]

    seen: set[str] = set()
    out: list[dict[str, str]] = []

    for pattern in patterns:
        for match in pattern.finditer(html):
            href = _decode_ddg_redirect(match.group(1) or "")
            title = _clean_html_fragment(match.group(2) or "")
            if not href or not title:
                continue
            try:
                parsed = urllib.parse.urlsplit(href)
            except Exception:
                continue
            if parsed.scheme not in {"http", "https"}:
                continue
            host = (parsed.hostname or "").lower()
            if host.endswith("duckduckgo.com") and parsed.path == "/y.js":
                continue

            key = f"{href}|{title}".lower()
            if key in seen:
                continue
            seen.add(key)

            snippet = ""
            window = html[match.end() : match.end() + 2400]
            snippet_match = re.search(
                r'(?is)<(?:a|div|span)[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|span)>',
                window,
            )
            if not snippet_match:
                snippet_match = re.search(
                    r"(?is)<td[^>]*class=['\"][^'\"]*result-snippet[^'\"]*['\"][^>]*>(.*?)</td>",
                    window,
                )
            if snippet_match:
                snippet = _clean_html_fragment(snippet_match.group(1) or "")

            out.append({"title": title, "url": href, "snippet": snippet})
            if len(out) >= limit:
                return out

    return out


def _looks_news_like_query(query: str) -> bool:
    text = (query or "").strip().lower()
    if not text:
        return False
    keywords = [
        "news",
        "latest",
        "breaking",
        "headline",
        "headlines",
        "today",
        "score",
        "scores",
        "新闻",
        "消息",
        "今日",
        "今天",
        "速报",
        "戰報",
        "战报",
        "比分",
        "ニュース",
    ]
    return any(k in text for k in keywords)


def _looks_baseball_query(query: str) -> bool:
    text = (query or "").strip().lower()
    if not text:
        return False
    keywords = [
        "baseball",
        "mlb",
        "npb",
        "kbo",
        "棒球",
        "野球",
        "甲子園",
        "甲子园",
        "大谷",
    ]
    return any(k in text for k in keywords)


def _build_rss_candidates(query: str) -> list[tuple[str, str]]:
    q = (query or "").strip()
    out: list[tuple[str, str]] = []
    is_baseball = _looks_baseball_query(q)

    if is_baseball:
        q_en = urllib.parse.quote_plus(f"{q} baseball")
        q_ja = urllib.parse.quote_plus(f"{q} 野球")
        out.extend(
            [
                ("mlb_official_rss", "https://www.mlb.com/feeds/news/rss.xml"),
                ("espn_mlb_rss", "https://www.espn.com/espn/rss/mlb/news"),
                ("yahoo_mlb_rss", "https://sports.yahoo.com/mlb/rss/"),
                (
                    "google_news_baseball_en",
                    f"https://news.google.com/rss/search?q={q_en}&hl=en-US&gl=US&ceid=US:en",
                ),
                (
                    "google_news_baseball_ja",
                    f"https://news.google.com/rss/search?q={q_ja}&hl=ja&gl=JP&ceid=JP:ja",
                ),
                ("nhk_sports_rss", "https://www3.nhk.or.jp/rss/news/cat7.xml"),
            ]
        )
    else:
        quoted = urllib.parse.quote_plus(q)
        out.append(
            (
                "google_news_query_zh",
                f"https://news.google.com/rss/search?q={quoted}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
            )
        )
        out.append(
            (
                "google_news_query_en",
                f"https://news.google.com/rss/search?q={quoted}&hl=en-US&gl=US&ceid=US:en",
            )
        )

    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for name, url in out:
        if url in seen:
            continue
        seen.add(url)
        deduped.append((name, url))
    return deduped


def _extract_google_news_rss_results(raw_xml: str, max_results: int) -> list[dict[str, str]]:
    limit = max(1, min(20, int(max_results)))
    xml_text = (raw_xml or "").strip()
    if not xml_text:
        return []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []

    items = root.findall(".//item")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not title or not link:
            continue

        title = _clean_html_fragment(title)
        link = _clean_html_fragment(link)
        snippet = _clean_html_fragment(desc)
        key = f"{title}|{link}".lower()
        if key in seen:
            continue
        seen.add(key)
        published_at = (item.findtext("pubDate") or "").strip()
        entry = {"title": title, "url": link, "snippet": snippet}
        if published_at:
            entry["published_at"] = published_at
        out.append(entry)
        if len(out) >= limit:
            break
    return out


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
                "description": "Read a UTF-8 text file in workspace. Supports chunked reads with start_char.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_char": {"type": "integer", "minimum": 0, "default": 0},
                        "max_chars": {"type": "integer", "minimum": 128, "maximum": 1000000, "default": 200000},
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
                        "max_chars": {"type": "integer", "minimum": 512, "maximum": 500000, "default": 120000},
                        "timeout_sec": {"type": "integer", "minimum": 3, "maximum": 30, "default": 12},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "search_web",
                "description": "Search the web by query and return candidate URLs/snippets.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query keywords"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                        "timeout_sec": {"type": "integer", "minimum": 3, "maximum": 30, "default": 12},
                    },
                    "required": ["query"],
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
        if name == "search_web":
            return self.search_web(**arguments)
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

    def read_text_file(self, path: str, start_char: int = 0, max_chars: int = 200000) -> dict[str, Any]:
        try:
            real_path = _resolve_workspace_path(self.config, path)
            if not real_path.exists():
                return {"ok": False, "error": f"Path not found: {path}"}
            if not real_path.is_file():
                return {"ok": False, "error": f"Not a file: {path}"}

            full_text = real_path.read_text(encoding="utf-8", errors="ignore")
            total_length = len(full_text)
            limit = max(128, min(1_000_000, int(max_chars)))
            start = max(0, int(start_char))
            if start > total_length:
                start = total_length
            end = min(total_length, start + limit)
            text = full_text[start:end]
            truncated = end < total_length
            return {
                "ok": True,
                "path": str(real_path),
                "content": text,
                "length": len(text),
                "start_char": start,
                "end_char": end,
                "total_length": total_length,
                "truncated": truncated,
                "has_more": truncated,
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

    def search_web(self, query: str, max_results: int = 5, timeout_sec: int = 12) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "query cannot be empty"}

        timeout_val = max(3, min(30, timeout_sec))
        limit = max(1, min(20, int(max_results)))
        read_limit = min(500000, max(20000, self.config.web_fetch_max_chars))
        ddg_allowed = self._domain_allowed("duckduckgo.com")
        prefer_news = _looks_news_like_query(q)
        prefer_baseball = _looks_baseball_query(q)
        rss_candidates = _build_rss_candidates(q)
        rss_allowed_candidates: list[tuple[str, str]] = []
        for name, url in rss_candidates:
            host = (urllib.parse.urlsplit(url).hostname or "").strip().lower()
            if host and self._domain_allowed(host):
                rss_allowed_candidates.append((name, url))

        if not ddg_allowed and not rss_allowed_candidates:
            return {
                "ok": False,
                "error": (
                    "Domain not allowed for search engines and RSS sources. "
                    f"Allowed: {', '.join(self.config.web_allowed_domains)}"
                ),
            }

        search_url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote_plus(q)
        lite_url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(q)

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

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        tls_warning: str | None = None
        active_context = ssl_context

        def _open(current_context: ssl.SSLContext | None, target_url: str):
            req = urllib.request.Request(
                url=target_url,
                headers=headers,
                method="GET",
            )
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=current_context))
            return opener.open(req, timeout=timeout_val)

        def _fetch_page(target_url: str, current_context: ssl.SSLContext | None) -> tuple[int, str, str, bool]:
            with _open(current_context, target_url) as resp:
                status = getattr(resp, "status", None) or 200
                content_type = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read(read_limit + 1)
                truncated = len(raw) > read_limit
                raw = raw[:read_limit]
                text = raw.decode("utf-8", errors="ignore")
                return status, content_type, text, truncated

        def _fetch_page_with_retry(target_url: str) -> tuple[int, str, str, bool]:
            nonlocal active_context, tls_warning
            try:
                return _fetch_page(target_url, active_context)
            except Exception as first_exc:
                if not self.config.web_skip_tls_verify and _is_cert_verify_error(first_exc):
                    tls_warning = "TLS verify failed; search_web auto-retried with verify disabled."
                    active_context = ssl._create_unverified_context()
                    return _fetch_page(target_url, active_context)
                raise

        try:
            results: list[dict[str, str]] = []
            source = "unknown"
            status = 200
            content_type = "text/html"
            truncated = False
            warning_parts: list[str] = []
            seen_result_keys: set[str] = set()

            def _append_results(items: list[dict[str, str]], source_name: str) -> int:
                added = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title", "")).strip()
                    url = str(item.get("url", "")).strip()
                    if not title and not url:
                        continue
                    key = f"{title}|{url}".lower()
                    if key in seen_result_keys:
                        continue
                    seen_result_keys.add(key)

                    row = dict(item)
                    row.setdefault("source", source_name)
                    results.append(row)
                    added += 1
                    if len(results) >= limit:
                        break
                return added

            if prefer_news and rss_allowed_candidates:
                for rss_name, rss_url in rss_allowed_candidates:
                    if len(results) >= limit:
                        break
                    try:
                        status, content_type, text, truncated = _fetch_page_with_retry(rss_url)
                        rss_results = _extract_google_news_rss_results(text, max_results=limit)
                        if _append_results(rss_results, rss_name) > 0 and source == "unknown":
                            source = f"rss:{rss_name}"
                    except Exception as exc:
                        warning_parts.append(f"{rss_name} 获取失败: {exc}")

            ddg_error: str | None = None
            if ddg_allowed and not results:
                try:
                    status, content_type, text, truncated = _fetch_page_with_retry(search_url)
                    ddg_results = _extract_ddg_results(text, max_results=limit)
                    if _append_results(ddg_results, "duckduckgo_html") > 0:
                        source = "duckduckgo_html"
                    if not results:
                        status, content_type, text, truncated = _fetch_page_with_retry(lite_url)
                        ddg_results = _extract_ddg_results(text, max_results=limit)
                        if _append_results(ddg_results, "duckduckgo_lite") > 0:
                            source = "duckduckgo_lite"
                except Exception as exc:
                    ddg_error = str(exc)

            if ddg_error:
                warning_parts.append(f"DuckDuckGo 搜索失败: {ddg_error}")

            if not results and rss_allowed_candidates and not prefer_news:
                for rss_name, rss_url in rss_allowed_candidates:
                    if len(results) >= limit:
                        break
                    try:
                        status, content_type, text, truncated = _fetch_page_with_retry(rss_url)
                        rss_results = _extract_google_news_rss_results(text, max_results=limit)
                        if _append_results(rss_results, rss_name) > 0 and source == "unknown":
                            source = f"rss:{rss_name}"
                    except Exception as exc:
                        warning_parts.append(f"{rss_name} 回退失败: {exc}")

            if not results and prefer_baseball:
                curated = [
                    {
                        "title": "MLB News (Official)",
                        "url": "https://www.mlb.com/news",
                        "snippet": "Fallback source when search engines are blocked.",
                        "source": "fallback_static",
                    },
                    {
                        "title": "ESPN MLB",
                        "url": "https://www.espn.com/mlb/",
                        "snippet": "Fallback source when search engines are blocked.",
                        "source": "fallback_static",
                    },
                    {
                        "title": "Yahoo Sports MLB",
                        "url": "https://sports.yahoo.com/mlb/",
                        "snippet": "Fallback source when search engines are blocked.",
                        "source": "fallback_static",
                    },
                    {
                        "title": "NPB Official",
                        "url": "https://npb.jp/",
                        "snippet": "Fallback source when search engines are blocked.",
                        "source": "fallback_static",
                    },
                    {
                        "title": "Yahoo Japan NPB",
                        "url": "https://baseball.yahoo.co.jp/npb/",
                        "snippet": "Fallback source when search engines are blocked.",
                        "source": "fallback_static",
                    },
                ]
                for item in curated:
                    host = (urllib.parse.urlsplit(item["url"]).hostname or "").strip().lower()
                    if host and self._domain_allowed(host):
                        results.append(item)
                    if len(results) >= limit:
                        break
                if results:
                    source = "fallback:baseball_static_links"
                    warning_parts.append("实时新闻抓取受限，已回退到可访问的棒球新闻入口链接。")

            if not results:
                warning_parts.append("搜索结果页解析为空，可能被网关改写或反爬。")

            if tls_warning:
                warning_parts.insert(0, tls_warning)

            warning = " ".join(part.strip() for part in warning_parts if part and part.strip()) or None
            if source == "unknown":
                source = "none"

            return {
                "ok": True,
                "query": q,
                "engine": source,
                "status": status,
                "content_type": content_type,
                "count": len(results),
                "results": results,
                "truncated": truncated,
                "warning": warning,
            }
        except urllib.error.HTTPError as exc:
            body = exc.read(4000).decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}", "body_preview": body}
        except Exception as exc:
            return {"ok": False, "error": f"search_web failed: {exc}"}

    def fetch_web(self, url: str, max_chars: int = 120000, timeout_sec: int = 12) -> dict[str, Any]:
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
        limit = max(512, min(500000, max_chars, self.config.web_fetch_max_chars))
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

        default_headers = {
            # Use a browser-like UA to reduce bot-block false positives.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/json,text/plain,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        tls_warning: str | None = None

        def _open(current_context: ssl.SSLContext | None, target_url: str):
            req = urllib.request.Request(
                url=target_url,
                headers=default_headers,
                method="GET",
            )
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=current_context))
            return opener.open(req, timeout=timeout_val)

        try:
            try:
                resp_cm = _open(ssl_context, request_url)
            except Exception as first_exc:
                # Pragmatic fallback for enterprise TLS chains:
                # if verification fails and user did not explicitly disable it,
                # retry once with verification off for fetch_web only.
                if not self.config.web_skip_tls_verify and _is_cert_verify_error(first_exc):
                    tls_warning = "TLS verify failed; fetch_web auto-retried with verify disabled."
                    resp_cm = _open(ssl._create_unverified_context(), request_url)
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

                        # Search-engine anti-bot fallback: try a text-friendly results page.
                        search_query = _extract_search_query(url)
                        if search_query and self._domain_allowed("duckduckgo.com"):
                            fallback_url = (
                                "https://duckduckgo.com/html/?q="
                                + urllib.parse.quote_plus(search_query)
                            )
                            try:
                                with _open(ssl_context, fallback_url) as fb_resp:
                                    fb_status = getattr(fb_resp, "status", None) or 200
                                    fb_ct = (fb_resp.headers.get("Content-Type") or "").lower()
                                    fb_raw = fb_resp.read(limit + 1)
                                    fb_truncated = len(fb_raw) > limit
                                    fb_raw = fb_raw[:limit]
                                    fb_text = fb_raw.decode("utf-8", errors="ignore")
                                    fb_extracted = _extract_html_text(fb_text, max_chars=limit)

                                if fb_extracted.strip() and not _looks_like_script_payload(fb_extracted):
                                    if tls_warning:
                                        warning = f"{tls_warning} {warning}" if warning else tls_warning
                                    fallback_warning = (
                                        f"{warning} 已自动回退到 DuckDuckGo HTML 结果页（query={search_query}）。"
                                        if warning
                                        else f"已自动回退到 DuckDuckGo HTML 结果页（query={search_query}）。"
                                    )
                                    return {
                                        "ok": True,
                                        "url": url,
                                        "status": fb_status,
                                        "content_type": fb_ct,
                                        "binary": False,
                                        "truncated": fb_truncated,
                                        "content": fb_extracted,
                                        "length": len(fb_extracted),
                                        "source_format": "search_fallback_duckduckgo_html",
                                        "warning": fallback_warning,
                                    }
                            except Exception as fb_exc:
                                warning = (
                                    f"{warning} DuckDuckGo 回退失败: {fb_exc}"
                                    if warning
                                    else f"DuckDuckGo 回退失败: {fb_exc}"
                                )

                        # Avoid passing noisy script payload to the model.
                        extracted = (
                            "[抓取到脚本/反爬页面，正文不可用。"
                            "请改用目标站点公开 API、可直读正文 URL，或非搜索结果页链接。]"
                        )
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
