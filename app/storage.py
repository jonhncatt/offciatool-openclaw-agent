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

    def load_or_create(self, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return self.create()
        path = self._path(session_id)
        if not path.exists():
            return self.create()
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

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
        elif suffix in {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".pdf",
            ".docx",
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
