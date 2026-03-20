from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any

from app.core.module_code import sync_python_module_version
from app.core.module_manifest import ModuleManifest, read_module_manifest, render_module_manifest_toml, version_dir_name
from app.core.module_types import ModuleReference, ModuleRuntimeContext


def _major_from_version(version: str) -> int:
    raw = str(version or "").strip()
    head = raw.split(".", 1)[0].strip()
    try:
        return max(1, int(head))
    except Exception:
        return 1


class ModulePackager:
    def __init__(self, context: ModuleRuntimeContext) -> None:
        self._context = context

    def next_version_for_module(self, module_id: str) -> str:
        module_root = self._context.modules_dir / str(module_id or "").strip()
        majors: list[int] = []
        for manifest_path in module_root.glob("v*/manifest.toml"):
            try:
                manifest = read_module_manifest(manifest_path)
            except Exception:
                continue
            majors.append(_major_from_version(manifest.version))
        next_major = (max(majors) + 1) if majors else 1
        return f"{next_major}.0.0"

    def package_reference(
        self,
        *,
        reference: ModuleReference,
        source_ref: str,
        depends_on: list[str] | None = None,
        runtime_profile: str = "",
        package_note: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_manifest = read_module_manifest(reference.path / "manifest.toml")
        new_version = self.next_version_for_module(reference.module_id)
        target_dir = self._context.modules_dir / reference.module_id / version_dir_name(new_version)
        if target_dir.exists():
            raise RuntimeError(f"Target module dir already exists: {target_dir}")

        ignore = shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc", ".pytest_cache")
        shutil.copytree(reference.path, target_dir, ignore=ignore)

        packaged_manifest = ModuleManifest(
            id=source_manifest.id,
            version=new_version,
            api_version=source_manifest.api_version,
            kind=source_manifest.kind,
            entrypoint=source_manifest.entrypoint,
            capabilities=tuple(source_manifest.capabilities),
            depends_on=tuple(str(item).strip() for item in (depends_on or []) if str(item).strip()),
            runtime_profile=str(runtime_profile or source_manifest.runtime_profile or "").strip(),
            source_ref=str(source_ref or reference.ref).strip(),
            packaged_at=datetime.now(timezone.utc).isoformat(),
            path=target_dir / "manifest.toml",
        )
        (target_dir / "manifest.toml").write_text(render_module_manifest_toml(packaged_manifest), encoding="utf-8")
        code_version_sync = sync_python_module_version(target_dir, new_version)

        package_meta = {
            "module_id": packaged_manifest.id,
            "kind": packaged_manifest.kind,
            "packaged_ref": f"{packaged_manifest.id}@{packaged_manifest.version}",
            "source_ref": str(source_ref or reference.ref).strip(),
            "source_path": str(reference.path),
            "target_dir": str(target_dir),
            "runtime_profile": packaged_manifest.runtime_profile,
            "depends_on": list(packaged_manifest.depends_on),
            "packaged_at": packaged_manifest.packaged_at,
            "package_note": str(package_note or "").strip(),
            "code_version_sync": code_version_sync,
            "metadata": dict(metadata or {}),
        }
        (target_dir / "package_meta.json").write_text(json.dumps(package_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return package_meta
