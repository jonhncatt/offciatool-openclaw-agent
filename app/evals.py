from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from app.agent import ExecutionState, OfficeAgent
from app.config import load_config
from app.models import ChatSettings, ToolEvent
from app.storage import now_iso

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "evals" / "cases.json"


def _resolve_value(value: Any) -> Any:
    if isinstance(value, str):
        expanded = os.path.expandvars(value)
        if expanded.startswith("./") or expanded.startswith("../") or expanded.startswith("evals/") or expanded.startswith("app/"):
            return str((ROOT / expanded).resolve())
        return expanded
    if isinstance(value, list):
        return [_resolve_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item) for key, item in value.items()}
    return value


def _get_path(obj: Any, path: str) -> Any:
    current = obj
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        if isinstance(current, list):
            current = current[int(part)]
            continue
        if isinstance(current, dict):
            current = current[part]
            continue
        raise KeyError(f"Cannot traverse {part} in non-container {type(current).__name__}")
    return current


def _ensure_generated_fixtures() -> None:
    generated_dir = ROOT / "evals" / "fixtures" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = generated_dir / "opcode_table.xlsx"
    if xlsx_path.exists():
        return

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "OpcodeTable"
    ws.append(["Value", "Description"])
    ws.append(["0Ah", "Invalid Format"])
    ws.append(["0Ch", "Command Sequence Error"])
    ws.append(["15h", "Operation Denied"])
    ws.append(["20h", "Namespace is Write Protected"])
    wb.save(xlsx_path)
    wb.close()


def _ensure_atom_fixture() -> None:
    generated_dir = ROOT / "evals" / "fixtures" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    atom_path = generated_dir / "sample.atom"
    if atom_path.exists():
        return
    atom_path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Feed</title>
  <subtitle>A subtitle.</subtitle>
  <link href="http://example.org/"/>
  <updated>2003-12-13T18:30:02Z</updated>
  <author><name>John Doe</name></author>
  <id>urn:uuid:60a76c80-d399-11d9-b93C-0003939e0af6</id>
  <entry>
    <title>Atom-Powered Robots Run Amok</title>
    <link href="http://example.org/2003/12/13/atom03"/>
    <id>urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a</id>
    <updated>2003-12-13T18:30:02Z</updated>
    <summary>Some text about robots.</summary>
  </entry>
</feed>
""",
        encoding="utf-8",
    )


def _prepare_case(case: dict[str, Any]) -> None:
    fixture = str((case.get("prepare") or {}).get("fixture") or "").strip()
    if fixture == "opcode_xlsx":
        _ensure_generated_fixtures()
    elif fixture == "sample_atom":
        _ensure_atom_fixture()


def _skip_reason(case: dict[str, Any]) -> str | None:
    env_keys = [str(item).strip() for item in case.get("skip_if_missing_env") or [] if str(item).strip()]
    for key in env_keys:
        if not str(os.environ.get(key) or "").strip():
            return f"missing env {key}"
    return None


def _assertions(payload: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    payload_error = str(payload.get("error") or "").strip() if isinstance(payload, dict) else ""
    missing = object()

    def get_value(path: str) -> Any:
        try:
            return _get_path(payload, path)
        except Exception as exc:
            detail = f"{path}: missing in payload"
            if payload_error:
                detail += f" (payload error: {payload_error})"
            else:
                detail += f" ({exc})"
            errors.append(detail)
            return missing

    for path, expected in (spec.get("equals") or {}).items():
        actual = get_value(path)
        if actual is missing:
            continue
        if actual != expected:
            errors.append(f"{path}: expected {expected!r}, got {actual!r}")

    for path, expected in (spec.get("min_value") or {}).items():
        actual = get_value(path)
        if actual is missing:
            continue
        try:
            if float(actual) < float(expected):
                errors.append(f"{path}: expected >= {expected!r}, got {actual!r}")
        except Exception:
            errors.append(f"{path}: expected numeric >= {expected!r}, got {actual!r}")

    for path, expected in (spec.get("max_value") or {}).items():
        actual = get_value(path)
        if actual is missing:
            continue
        try:
            if float(actual) > float(expected):
                errors.append(f"{path}: expected <= {expected!r}, got {actual!r}")
        except Exception:
            errors.append(f"{path}: expected numeric <= {expected!r}, got {actual!r}")

    for path, snippets in (spec.get("contains") or {}).items():
        actual = get_value(path)
        if actual is missing:
            continue
        actual = str(actual)
        for snippet in snippets:
            if str(snippet) not in actual:
                errors.append(f"{path}: missing snippet {snippet!r}")

    for path, snippets in (spec.get("contains_any") or {}).items():
        actual = get_value(path)
        if actual is missing:
            continue
        actual = str(actual)
        if not any(str(snippet) in actual for snippet in snippets):
            errors.append(f"{path}: missing any of {snippets!r}")

    for path, snippets in (spec.get("not_contains") or {}).items():
        actual = get_value(path)
        if actual is missing:
            continue
        actual = str(actual)
        for snippet in snippets:
            if str(snippet) in actual:
                errors.append(f"{path}: unexpectedly contains {snippet!r}")

    return errors


def _attachment_meta(entry: dict[str, Any]) -> dict[str, Any]:
    path = Path(_resolve_value(entry.get("path")))
    return {
        "id": f"eval-{path.name}",
        "original_name": str(entry.get("original_name") or path.name),
        "safe_name": path.name,
        "mime": str(entry.get("mime") or "application/octet-stream"),
        "suffix": path.suffix.lower(),
        "kind": str(entry.get("kind") or "document"),
        "size": path.stat().st_size,
        "path": str(path.resolve()),
        "created_at": now_iso(),
    }


def _helper_arg(value: Any) -> Any:
    if isinstance(value, dict):
        marker = str(value.get("__type") or "").strip()
        if marker == "ToolEvent":
            return ToolEvent(
                name=str(value.get("name") or ""),
                input=value.get("input") if isinstance(value.get("input"), dict) else None,
                output_preview=str(value.get("output_preview") or ""),
            )
        if marker == "ChatSettings":
            payload = {key: item for key, item in value.items() if key != "__type"}
            return ChatSettings(**payload)
        if marker == "ExecutionState":
            payload = {key: item for key, item in value.items() if key != "__type"}
            return ExecutionState(**payload)
        return {key: _helper_arg(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_helper_arg(item) for item in value]
    return value


def _run_tool_case(case: dict[str, Any], executor: Any) -> dict[str, Any]:
    tool_name = str(case["tool"])
    args = _resolve_value(case.get("args") or {})
    fn = getattr(executor, tool_name)
    started = time.perf_counter()
    result = fn(**args)
    elapsed_sec = time.perf_counter() - started
    payload = result if isinstance(result, dict) else {"result": result}
    payload["elapsed_sec"] = round(elapsed_sec, 3)
    return payload


def _run_helper_case(case: dict[str, Any], agent: OfficeAgent) -> dict[str, Any]:
    helper_name = str(case["helper"])
    args = _helper_arg(_resolve_value(case.get("args") or {}))
    fn = getattr(agent, helper_name)
    started = time.perf_counter()
    result = fn(**args)
    elapsed_sec = time.perf_counter() - started
    payload = result if isinstance(result, dict) else {"result": result}
    payload["elapsed_sec"] = round(elapsed_sec, 3)
    return payload


def _run_agent_case(case: dict[str, Any], agent: OfficeAgent) -> dict[str, Any]:
    message = str(case.get("message") or "")
    attachments = [_attachment_meta(item) for item in case.get("attachments") or []]
    settings = ChatSettings(**(case.get("settings") or {}))
    started = time.perf_counter()
    (
        text,
        tool_events,
        attachment_note,
        execution_plan,
        execution_trace,
        debug_flow,
        agent_panels,
        active_roles,
        current_role,
        role_states,
        answer_bundle,
        token_usage,
        effective_model,
        route_state,
    ) = agent.run_chat(
        history_turns=[],
        summary="",
        user_message=message,
        attachment_metas=attachments,
        settings=settings,
        session_id="eval-harness",
        route_state=case.get("route_state"),
    )
    elapsed_sec = time.perf_counter() - started
    return {
        "text": text,
        "attachment_note": attachment_note,
        "execution_plan": execution_plan,
        "execution_trace": execution_trace,
        "debug_flow_count": len(debug_flow),
        "agent_panels": agent_panels,
        "active_roles": active_roles,
        "current_role": current_role,
        "role_states": role_states,
        "answer_bundle": answer_bundle,
        "tool_events": [item.model_dump() for item in tool_events],
        "tool_events_count": len(tool_events),
        "token_usage": token_usage,
        "effective_model": effective_model,
        "route_state": route_state,
        "elapsed_sec": round(elapsed_sec, 3),
    }


def run_regression_evals(
    *,
    cases_path: str | Path | None = None,
    name_filter: str = "",
    include_optional: bool = False,
) -> dict[str, Any]:
    resolved_cases_path = Path(cases_path or DEFAULT_CASES_PATH).resolve()
    cases = json.loads(resolved_cases_path.read_text(encoding="utf-8"))

    cfg = load_config()
    agent = OfficeAgent(cfg)
    tools = agent.tools

    results: list[dict[str, Any]] = []
    passes = 0
    failures = 0
    skips = 0
    started = time.perf_counter()

    for case in cases:
        name = str(case.get("name") or "")
        if name_filter and name_filter not in name:
            continue
        if bool(case.get("optional")) and not include_optional:
            results.append({"name": name, "status": "skipped", "reason": "optional"})
            skips += 1
            continue

        reason = _skip_reason(case)
        if reason:
            results.append({"name": name, "status": "skipped", "reason": reason})
            skips += 1
            continue

        _prepare_case(case)
        kind = str(case.get("kind") or "tool")
        try:
            if kind == "tool":
                payload = _run_tool_case(case, tools)
            elif kind == "helper":
                payload = _run_helper_case(case, agent)
            else:
                payload = _run_agent_case(case, agent)
            errors = _assertions(payload, case.get("assert") or {})
            if errors:
                results.append({"name": name, "kind": kind, "status": "failed", "errors": errors, "payload": payload})
                failures += 1
            else:
                results.append({"name": name, "kind": kind, "status": "passed", "payload": payload})
                passes += 1
        except Exception as exc:
            results.append({"name": name, "kind": kind, "status": "failed", "errors": [str(exc)]})
            failures += 1

    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    return {
        "ok": failures == 0,
        "include_optional": bool(include_optional),
        "name_filter": str(name_filter or ""),
        "cases_path": str(resolved_cases_path),
        "passed": passes,
        "failed": failures,
        "skipped": skips,
        "total": passes + failures + skips,
        "duration_ms": duration_ms,
        "results": results,
    }
