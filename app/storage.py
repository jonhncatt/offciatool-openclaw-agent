from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile


_SAFE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(name: str) -> str:
    return _SAFE_NAME_PATTERN.sub("_", name).strip("._") or "file"


class SessionStore:
    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def create(self) -> dict[str, Any]:
        session = {
            "id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "summary": "",
            "turns": [],
        }
        self.save(session)
        return session

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def load_or_create(self, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return self.create()
        loaded = self.load(session_id)
        if not loaded:
            return self.create()
        return loaded

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = now_iso()
        path = self._path(session["id"])
        with self._lock:
            path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_turn(self, session: dict[str, Any], role: str, text: str, attachments: list[dict[str, Any]] | None = None) -> None:
        session.setdefault("turns", []).append(
            {
                "role": role,
                "text": text,
                "attachments": attachments or [],
                "created_at": now_iso(),
            }
        )

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        max_items = max(1, min(500, int(limit)))
        files = sorted(
            self.sessions_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        out: list[dict[str, Any]] = []
        for path in files:
            if len(out) >= max_items:
                break
            try:
                with self._lock:
                    session = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue

            sid = str(session.get("id") or path.stem)
            turns = session.get("turns", [])
            if not isinstance(turns, list):
                turns = []

            title = "新会话"
            for turn in turns:
                if isinstance(turn, dict) and str(turn.get("role") or "") == "user":
                    text = str(turn.get("text") or "").strip()
                    if text:
                        title = text.replace("\n", " ")[:48]
                    break

            preview = ""
            if turns:
                last = turns[-1]
                if isinstance(last, dict):
                    preview = str(last.get("text") or "").replace("\n", " ").strip()[:80]

            out.append(
                {
                    "session_id": sid,
                    "title": title,
                    "preview": preview,
                    "turn_count": len(turns),
                    "updated_at": str(session.get("updated_at") or ""),
                    "created_at": str(session.get("created_at") or ""),
                }
            )

        return out

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        try:
            with self._lock:
                path.unlink(missing_ok=False)
            return True
        except Exception:
            return False


class UploadStore:
    def __init__(self, uploads_dir: Path) -> None:
        self.uploads_dir = uploads_dir
        self.index_path = self.uploads_dir / "index.json"
        self._lock = threading.Lock()

        if not self.index_path.exists():
            self.index_path.write_text("{}", encoding="utf-8")

    def _load_index(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, index: dict[str, Any]) -> None:
        with self._lock:
            self.index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    async def save_upload(self, upload: UploadFile) -> dict[str, Any]:
        file_id = str(uuid.uuid4())
        original_name = upload.filename or "upload.bin"
        safe_name = _safe_name(original_name)
        stored_name = f"{file_id}__{safe_name}"
        target_path = (self.uploads_dir / stored_name).resolve()

        content = await upload.read()
        target_path.write_bytes(content)

        mime = upload.content_type or "application/octet-stream"
        suffix = Path(original_name).suffix.lower()
        kind = "other"
        if mime.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif"}:
            kind = "image"
        elif mime.lower() in {"application/vnd.ms-outlook", "application/x-msg"}:
            kind = "document"
        elif suffix in {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".pdf",
            ".docx",
            ".msg",
            ".doc",
            ".xlsx",
            ".xls",
            ".pptx",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".yaml",
            ".yml",
            ".log",
        }:
            kind = "document"

        meta = {
            "id": file_id,
            "original_name": original_name,
            "safe_name": safe_name,
            "mime": mime,
            "suffix": suffix,
            "kind": kind,
            "size": len(content),
            "path": str(target_path),
            "created_at": now_iso(),
        }

        index = self._load_index()
        index[file_id] = meta
        self._save_index(index)
        return meta

    def get_many(self, file_ids: list[str]) -> list[dict[str, Any]]:
        index = self._load_index()
        out: list[dict[str, Any]] = []
        for file_id in file_ids:
            meta = index.get(file_id)
            if meta:
                out.append(meta)
        return out

    def delete(self, file_id: str) -> None:
        index = self._load_index()
        meta = index.pop(file_id, None)
        if meta and meta.get("path"):
            try:
                Path(meta["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        self._save_index(index)


def _empty_totals() -> dict[str, int | float]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }


class TokenStatsStore:
    def __init__(self, stats_path: Path) -> None:
        self.stats_path = stats_path
        self._lock = threading.Lock()
        if not self.stats_path.exists():
            self._write(self._new_state())

    def _new_state(self) -> dict[str, Any]:
        return {
            "totals": _empty_totals(),
            "sessions": {},
            "records": [],
            "updated_at": now_iso(),
        }

    def _read(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(self.stats_path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        with self._lock:
            data["updated_at"] = now_iso()
            self.stats_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self._write(self._new_state())

    def _normalize_usage(self, usage: dict[str, Any]) -> dict[str, float]:
        return {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "estimated_cost_usd": float(usage.get("estimated_cost_usd", 0.0) or 0.0),
        }

    def add_usage(self, session_id: str, usage: dict[str, Any], model: str | None = None) -> dict[str, Any]:
        data = self._read()
        norm = self._normalize_usage(usage)

        totals = data.setdefault("totals", _empty_totals())
        totals["requests"] = int(totals.get("requests", 0) or 0) + 1
        totals["input_tokens"] = int(totals.get("input_tokens", 0) or 0) + norm["input_tokens"]
        totals["output_tokens"] = int(totals.get("output_tokens", 0) or 0) + norm["output_tokens"]
        totals["total_tokens"] = int(totals.get("total_tokens", 0) or 0) + norm["total_tokens"]
        totals["estimated_cost_usd"] = float(totals.get("estimated_cost_usd", 0.0) or 0.0) + norm["estimated_cost_usd"]

        sessions = data.setdefault("sessions", {})
        sess = sessions.setdefault(session_id, _empty_totals())
        sess["requests"] = int(sess.get("requests", 0) or 0) + 1
        sess["input_tokens"] = int(sess.get("input_tokens", 0) or 0) + norm["input_tokens"]
        sess["output_tokens"] = int(sess.get("output_tokens", 0) or 0) + norm["output_tokens"]
        sess["total_tokens"] = int(sess.get("total_tokens", 0) or 0) + norm["total_tokens"]
        sess["estimated_cost_usd"] = float(sess.get("estimated_cost_usd", 0.0) or 0.0) + norm["estimated_cost_usd"]

        records = data.setdefault("records", [])
        records.append(
            {
                "ts": now_iso(),
                "session_id": session_id,
                "model": model,
                "input_tokens": norm["input_tokens"],
                "output_tokens": norm["output_tokens"],
                "total_tokens": norm["total_tokens"],
                "llm_calls": int(usage.get("llm_calls", 0) or 0),
                "estimated_cost_usd": norm["estimated_cost_usd"],
                "pricing_known": bool(usage.get("pricing_known", False)),
                "pricing_model": usage.get("pricing_model"),
                "input_price_per_1m": usage.get("input_price_per_1m"),
                "output_price_per_1m": usage.get("output_price_per_1m"),
            }
        )

        self._write(data)
        return data

    def get_stats(self, max_records: int = 300) -> dict[str, Any]:
        data = self._read()
        records = data.get("records", [])
        if max_records > 0 and len(records) > max_records:
            data["records"] = records[-max_records:]
        return data
