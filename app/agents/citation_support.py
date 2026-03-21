from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def domain_from_url(raw_url: str) -> str | None:
    try:
        host = (urlparse(str(raw_url or "")).hostname or "").strip().lower()
    except Exception:
        host = ""
    return host or None


def extract_citations_from_tool_result(
    agent: Any,
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
            domain = str(row.get("domain") or domain_from_url(url) or "").strip() or None
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
                    "excerpt": agent._shorten(snippet, 280),
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
                "label": str(result.get("title") or domain_from_url(url) or url or "web page").strip(),
                "url": url or None,
                "title": str(result.get("title") or "").strip() or None,
                "domain": str(result.get("domain") or domain_from_url(url) or "").strip() or None,
                "locator": str(result.get("canonical_url") or "").strip() or None,
                "excerpt": agent._shorten(excerpt, 320),
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
                    "excerpt": agent._shorten(match.get("context") or "", 320),
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
                "excerpt": agent._shorten(result.get("content") or "", 320),
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
                    "excerpt": agent._shorten("\n".join(rows), 320),
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
                    "excerpt": agent._shorten(match.get("text") or "", 320),
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
                    "excerpt": agent._shorten(match.get("context") or "", 320),
                    "warning": str(result.get("verdict") or "").strip() or None,
                    "confidence": "medium",
                }
            )
        return out

    return out


def merge_citation_candidates(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def finalize_citation_candidates(agent: Any, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = [item for item in citations if isinstance(item, dict)]
    if any(agent._citation_kind(item) == "evidence" for item in prepared):
        prepared = [item for item in prepared if agent._citation_kind(item) == "evidence"]

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
                "kind": agent._citation_kind(item),
                "tool": str(item.get("tool") or "").strip(),
                "label": str(item.get("label") or "").strip() or f"source_{idx}",
                "path": str(item.get("path") or "").strip() or None,
                "url": str(item.get("url") or "").strip() or None,
                "title": str(item.get("title") or "").strip() or None,
                "domain": str(item.get("domain") or "").strip() or None,
                "locator": str(item.get("locator") or "").strip() or None,
                "excerpt": agent._shorten(str(item.get("excerpt") or "").strip(), 360),
                "published_at": str(item.get("published_at") or "").strip() or None,
                "warning": str(item.get("warning") or "").strip() or None,
                "confidence": str(item.get("confidence") or "medium").strip().lower()
                if str(item.get("confidence") or "medium").strip().lower() in {"high", "medium", "low"}
                else "medium",
            }
        )
    return out
