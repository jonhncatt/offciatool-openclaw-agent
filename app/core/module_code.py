from __future__ import annotations

from pathlib import Path
import re
from typing import Any

_VERSION_RE = re.compile(r'(^\s*version\s*=\s*")([^"]+)("\s*$)', re.MULTILINE)


def read_python_module_version(module_dir: Path) -> str:
    module_path = Path(module_dir) / "module.py"
    if not module_path.is_file():
        return ""
    try:
        text = module_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    match = _VERSION_RE.search(text)
    if not match:
        return ""
    return str(match.group(2) or "").strip()


def sync_python_module_version(module_dir: Path, target_version: str) -> dict[str, Any]:
    module_path = Path(module_dir) / "module.py"
    target = str(target_version or "").strip()
    if not target:
        return {"ok": False, "reason": "missing_target_version", "path": str(module_path)}
    if not module_path.is_file():
        return {"ok": False, "reason": "module_py_missing", "path": str(module_path)}
    try:
        text = module_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "reason": f"read_failed: {exc}", "path": str(module_path)}
    match = _VERSION_RE.search(text)
    if not match:
        return {"ok": False, "reason": "version_attr_missing", "path": str(module_path)}
    before = str(match.group(2) or "").strip()
    updated = _VERSION_RE.sub(lambda match: f"{match.group(1)}{target}{match.group(3)}", text, count=1)
    if updated != text:
        module_path.write_text(updated, encoding="utf-8")
    return {
        "ok": True,
        "path": str(module_path),
        "before": before,
        "after": target,
        "changed": before != target,
    }
