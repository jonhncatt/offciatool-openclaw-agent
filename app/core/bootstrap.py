from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
from uuid import uuid4

from app.agents.runtime_profiles import PATCH_WORKER_PROFILE
from app.config import AppConfig
from app.core.module_code import sync_python_module_version
from app.core.module_loader import ModuleLoader
from app.core.module_capability_smoke import run_module_capability_smoke
from app.core.module_packager import ModulePackager
from app.core.module_manifest import ActiveModuleManifest, read_module_manifest, write_active_manifest, write_module_manifest
from app.core.module_registry import KernelModuleRegistry
from app.core.module_types import ModuleHealthSnapshot, ModuleRuntimeContext
from app.core.supervisor import KernelSupervisor


@dataclass(slots=True)
class KernelRuntime:
    context: ModuleRuntimeContext
    loader: ModuleLoader
    supervisor: KernelSupervisor
    registry: KernelModuleRegistry

    def reload_registry(self) -> KernelModuleRegistry:
        self.registry = self.supervisor.load_registry()
        return self.registry

    def health_snapshot(self) -> ModuleHealthSnapshot:
        return self.supervisor.health_snapshot(self.registry)

    def record_module_failure(self, *, kind: str, requested_ref: str, fallback_ref: str = "", error: str, mode: str | None = None) -> None:
        self.supervisor.record_runtime_failure(
            kind=kind,
            requested_ref=requested_ref,
            fallback_ref=fallback_ref,
            error=error,
            mode=mode,
        )
        self.reload_registry()

    def record_module_success(self, *, kind: str, selected_ref: str, mode: str | None = None) -> None:
        self.supervisor.record_runtime_success(kind=kind, selected_ref=selected_ref, mode=mode)

    def load_shadow_manifest(self) -> ActiveModuleManifest:
        return self.supervisor.load_shadow_manifest()

    def write_shadow_manifest(self, manifest: ActiveModuleManifest) -> None:
        self.supervisor.write_shadow_manifest(manifest)

    def validate_shadow_manifest(self) -> dict[str, object]:
        return self.supervisor.validate_shadow_manifest()

    def validate_active_manifest(self) -> dict[str, object]:
        return self.supervisor.validate_active_manifest()

    def shadow_promote_check(self) -> dict[str, object]:
        return self.supervisor.shadow_promote_check()

    def promote_shadow_manifest(self) -> dict[str, object]:
        result = self.supervisor.promote_shadow_manifest()
        if result.get("ok"):
            self.reload_registry()
        return result

    def rollback_active_manifest(self) -> dict[str, object]:
        result = self.supervisor.rollback_active_manifest()
        if result.get("ok"):
            self.reload_registry()
        return result

    def stage_shadow_manifest(self, *, overrides: dict[str, object] | None = None) -> dict[str, object]:
        shadow = self.load_shadow_manifest()
        payload = dict(overrides or {})
        for key in ("router", "policy", "attachment_context", "finalizer", "tool_registry"):
            value = str(payload.get(key) or "").strip()
            if value:
                setattr(shadow, key, value)
        providers = dict(shadow.providers)
        raw_providers = payload.get("providers")
        if isinstance(raw_providers, dict):
            for mode, ref in raw_providers.items():
                mode_text = str(mode or "").strip()
                ref_text = str(ref or "").strip()
                if mode_text and ref_text:
                    providers[mode_text] = ref_text
        shadow.providers = providers
        self.write_shadow_manifest(shadow)
        validation = self.validate_shadow_manifest()
        promote_check = self.shadow_promote_check()
        return {
            "ok": bool(validation.get("ok")),
            "shadow_manifest": shadow.to_dict(),
            "validation": validation,
            "promote_check": promote_check,
        }

    def _last_shadow_run_path(self) -> Path:
        return self.context.runtime_dir / "last_shadow_run.json"

    def _upgrade_runs_dir(self) -> Path:
        return self.context.runtime_dir / "upgrade_runs"

    def _last_upgrade_run_path(self) -> Path:
        return self.context.runtime_dir / "last_upgrade_run.json"

    def _repair_runs_dir(self) -> Path:
        return self.context.runtime_dir / "repair_runs"

    def _last_repair_run_path(self) -> Path:
        return self.context.runtime_dir / "last_repair_run.json"

    def _repair_workspaces_dir(self) -> Path:
        return self.context.runtime_dir / "repair_workspaces"

    def _patch_worker_runs_dir(self) -> Path:
        return self.context.runtime_dir / "patch_worker_runs"

    def _last_patch_worker_run_path(self) -> Path:
        return self.context.runtime_dir / "last_patch_worker_run.json"

    def _package_runs_dir(self) -> Path:
        return self.context.runtime_dir / "package_runs"

    def _last_package_run_path(self) -> Path:
        return self.context.runtime_dir / "last_package_run.json"

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _read_json_dict(self, path: Path) -> dict[str, object]:
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def read_last_shadow_run(self) -> dict[str, object]:
        return self._read_json_dict(self._last_shadow_run_path())

    def read_last_upgrade_run(self) -> dict[str, object]:
        return self._read_json_dict(self._last_upgrade_run_path())

    def list_upgrade_runs(self, *, limit: int = 20) -> list[dict[str, object]]:
        max_items = max(1, min(200, int(limit)))
        files = sorted(self._upgrade_runs_dir().glob("*.json"), key=lambda path: path.name, reverse=True)
        out: list[dict[str, object]] = []
        for path in files:
            payload = self._read_json_dict(path)
            if payload:
                out.append(payload)
            if len(out) >= max_items:
                break
        return out

    def find_upgrade_run(self, run_id: str | None) -> dict[str, object]:
        wanted = str(run_id or "").strip()
        if not wanted:
            return self.read_last_upgrade_run()
        path = self._upgrade_runs_dir() / f"{wanted}.json"
        if path.is_file():
            return self._read_json_dict(path)
        for item in self.list_upgrade_runs(limit=200):
            if str(item.get("run_id") or "").strip() == wanted:
                return item
        return {}

    def read_last_repair_run(self) -> dict[str, object]:
        return self._read_json_dict(self._last_repair_run_path())

    def list_repair_runs(self, *, limit: int = 20) -> list[dict[str, object]]:
        max_items = max(1, min(200, int(limit)))
        files = sorted(self._repair_runs_dir().glob("*.json"), key=lambda path: path.name, reverse=True)
        out: list[dict[str, object]] = []
        for path in files:
            payload = self._read_json_dict(path)
            if payload:
                out.append(payload)
            if len(out) >= max_items:
                break
        return out

    def find_repair_run(self, run_id: str | None) -> dict[str, object]:
        wanted = str(run_id or "").strip()
        if not wanted:
            return self.read_last_repair_run()
        path = self._repair_runs_dir() / f"{wanted}.json"
        if path.is_file():
            return self._read_json_dict(path)
        for item in self.list_repair_runs(limit=200):
            if str(item.get("run_id") or "").strip() == wanted:
                return item
        return {}

    def read_last_patch_worker_run(self) -> dict[str, object]:
        return self._read_json_dict(self._last_patch_worker_run_path())

    def list_patch_worker_runs(self, *, limit: int = 20) -> list[dict[str, object]]:
        max_items = max(1, min(200, int(limit)))
        files = sorted(self._patch_worker_runs_dir().glob("*.json"), key=lambda path: path.name, reverse=True)
        out: list[dict[str, object]] = []
        for path in files:
            payload = self._read_json_dict(path)
            if payload:
                out.append(payload)
            if len(out) >= max_items:
                break
        return out

    def read_last_package_run(self) -> dict[str, object]:
        return self._read_json_dict(self._last_package_run_path())

    def list_package_runs(self, *, limit: int = 20) -> list[dict[str, object]]:
        max_items = max(1, min(200, int(limit)))
        files = sorted(self._package_runs_dir().glob("*.json"), key=lambda path: path.name, reverse=True)
        out: list[dict[str, object]] = []
        for path in files:
            payload = self._read_json_dict(path)
            if payload:
                out.append(payload)
            if len(out) >= max_items:
                break
        return out

    def _pipeline_run_id(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]

    def _pipeline_failure_text(self, payload: dict[str, object] | None) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("error", "reason"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        errors = payload.get("errors")
        if isinstance(errors, list):
            parts = [str(item).strip() for item in errors if str(item).strip()]
            if parts:
                return "; ".join(parts[:3])
        validation = payload.get("validation")
        if isinstance(validation, dict):
            errors = validation.get("errors")
            if isinstance(errors, list):
                parts = [str(item).strip() for item in errors if str(item).strip()]
                if parts:
                    return "; ".join(parts[:3])
        return ""

    def _pipeline_manifest_labels(self, validation: dict[str, object] | None) -> list[str]:
        if not isinstance(validation, dict):
            return []
        errors = validation.get("errors")
        if not isinstance(errors, list):
            return []
        labels: list[str] = []
        for item in errors:
            text = str(item or "").strip()
            if not text:
                continue
            label = text.split(":", 1)[0].strip()
            if label and label not in labels:
                labels.append(label)
        return labels

    def _safe_active_override_for_label(self, label: str, active_manifest: ActiveModuleManifest) -> tuple[str, str] | None:
        raw = str(label or "").strip()
        if not raw:
            return None
        if raw.startswith("provider:"):
            mode = raw.split(":", 1)[1].strip()
            if mode:
                ref = str(active_manifest.providers.get(mode) or "").strip()
                if ref:
                    return (f"providers.{mode}", ref)
            return None
        if raw in {"router", "policy", "attachment_context", "finalizer", "tool_registry"}:
            ref = str(getattr(active_manifest, raw, "") or "").strip()
            if ref:
                return (raw, ref)
        return None

    def _all_manifest_labels(self, manifest: ActiveModuleManifest) -> list[str]:
        return [
            "router",
            "policy",
            "attachment_context",
            "finalizer",
            "tool_registry",
            *[f"provider:{mode}" for mode in sorted(manifest.providers)],
        ]

    def _set_manifest_ref_for_label(self, manifest: ActiveModuleManifest, *, label: str, ref: str) -> None:
        raw = str(label or "").strip()
        ref_text = str(ref or "").strip()
        if not raw or not ref_text:
            return
        if raw.startswith("provider:"):
            mode = raw.split(":", 1)[1].strip()
            if mode:
                manifest.providers[mode] = ref_text
            return
        if raw in {"router", "policy", "attachment_context", "finalizer", "tool_registry"}:
            setattr(manifest, raw, ref_text)

    def _module_kind_for_label(self, label: str) -> str:
        raw = str(label or "").strip()
        if raw.startswith("provider:"):
            return "provider"
        if raw in {"router", "policy", "attachment_context", "finalizer", "tool_registry"}:
            return raw if raw != "policy" else "policy"
        return ""

    def _manifest_ref_for_label(self, label: str, manifest_dict: dict[str, object]) -> str:
        raw = str(label or "").strip()
        if not raw:
            return ""
        if raw.startswith("provider:"):
            mode = raw.split(":", 1)[1].strip()
            providers = manifest_dict.get("providers")
            if isinstance(providers, dict):
                return str(providers.get(mode) or "").strip()
            return ""
        return str(manifest_dict.get(raw) or "").strip()

    def _prepare_repair_tasks(
        self,
        *,
        repair_run_id: str,
        classification: dict[str, object],
        base_upgrade_run: dict[str, object],
    ) -> dict[str, object]:
        workspace_root = self._repair_workspaces_dir() / repair_run_id
        workspace_root.mkdir(parents=True, exist_ok=True)
        stage_payload = base_upgrade_run.get("stage")
        stage_payload = dict(stage_payload) if isinstance(stage_payload, dict) else {}
        shadow_manifest_dict = stage_payload.get("shadow_manifest")
        shadow_manifest_dict = dict(shadow_manifest_dict) if isinstance(shadow_manifest_dict, dict) else self.load_shadow_manifest().to_dict()
        active_manifest = self.supervisor.load_active_manifest()
        labels = [str(item).strip() for item in classification.get("blocking_modules") or [] if str(item).strip()]
        tasks: list[dict[str, object]] = []

        for label in labels:
            kind = self._module_kind_for_label(label)
            requested_ref = self._manifest_ref_for_label(label, shadow_manifest_dict)
            seed_ref = requested_ref
            seed_source = "shadow"
            reference = None
            if kind and requested_ref:
                try:
                    reference = self.loader.resolve_ref(requested_ref, expected_kind=kind)
                except Exception:
                    reference = None
            if reference is None:
                fallback = self._safe_active_override_for_label(label, active_manifest)
                if fallback is not None:
                    _, active_ref = fallback
                    seed_ref = active_ref
                    seed_source = "active"
                    if kind and seed_ref:
                        try:
                            reference = self.loader.resolve_ref(seed_ref, expected_kind=kind)
                        except Exception:
                            reference = None

            task_dir = workspace_root / label.replace(":", "__")
            task_dir.mkdir(parents=True, exist_ok=True)
            seed_path = ""
            if reference is not None:
                seed_path = str(reference.path)
                module_copy_dir = task_dir / "module"
                if module_copy_dir.exists():
                    shutil.rmtree(module_copy_dir)
                shutil.copytree(reference.path, module_copy_dir)
            task_payload: dict[str, object] = {
                "label": label,
                "kind": kind,
                "requested_ref": requested_ref,
                "seed_ref": seed_ref,
                "seed_source": seed_source,
                "seed_path": seed_path,
                "workspace_dir": str(task_dir),
                "failure_category": str(classification.get("category") or ""),
                "failure_reason": str(classification.get("reason") or ""),
                "target_dependencies": [
                    f"{dep_label}={self._manifest_ref_for_label(dep_label, shadow_manifest_dict)}"
                    for dep_label in self._all_manifest_labels(active_manifest)
                    if dep_label != label and self._manifest_ref_for_label(dep_label, shadow_manifest_dict)
                ],
                "target_api_version": "1",
                "target_kind": kind,
                "target_capabilities": list(self.supervisor._expected_capabilities_for_kind(kind)),
                "target_runtime_profile": "",
                "remediation_hints": list(base_upgrade_run.get("remediation_hints") or []),
            }
            self._write_json(task_dir / "repair_task.json", task_payload)
            tasks.append(task_payload)

        return {
            "workspace_root": str(workspace_root),
            "tasks": tasks,
        }

    def _apply_patch_worker_recipes(
        self,
        *,
        task_payload: dict[str, object],
        repair_context: dict[str, object],
        module_dir: Path,
    ) -> list[str]:
        actions: list[str] = []
        manifest_path = module_dir / "manifest.toml"
        if not manifest_path.is_file():
            return actions

        try:
            manifest = read_module_manifest(manifest_path)
        except Exception:
            return actions

        updated_manifest = manifest
        failure_reason = str(repair_context.get("last_pipeline_failure", {}).get("reason") or repair_context.get("failure_reason") or "").strip()
        failure_category = str(repair_context.get("last_pipeline_failure", {}).get("category") or repair_context.get("failure_category") or "").strip()
        target_dependencies = tuple(
            str(item).strip()
            for item in (task_payload.get("target_dependencies") or [])
            if str(item).strip()
        )
        target_api_version = str(task_payload.get("target_api_version") or "1").strip() or "1"
        target_kind = str(task_payload.get("target_kind") or updated_manifest.kind or "").strip()
        target_capabilities = tuple(
            str(item).strip()
            for item in (task_payload.get("target_capabilities") or [])
            if str(item).strip()
        )
        target_runtime_profile = str(task_payload.get("target_runtime_profile") or "").strip()

        if target_kind and updated_manifest.kind != target_kind:
            updated_manifest = replace(updated_manifest, kind=target_kind)
            actions.append(f"set kind={target_kind}")

        if updated_manifest.api_version != target_api_version:
            updated_manifest = replace(updated_manifest, api_version=target_api_version)
            actions.append(f"set api_version={target_api_version}")

        dependency_reasons = {"dependency_mismatch", "invalid_dependency_entry"}
        if target_dependencies and (
            updated_manifest.depends_on != target_dependencies
            or failure_reason in dependency_reasons
            or failure_category in {"manifest_validation", "promotion_failed"}
        ):
            updated_manifest = replace(updated_manifest, depends_on=target_dependencies)
            actions.append("aligned depends_on with shadow manifest")

        if failure_reason == "runtime_profile_invalid" and updated_manifest.runtime_profile != target_runtime_profile:
            updated_manifest = replace(updated_manifest, runtime_profile=target_runtime_profile)
            actions.append(f"set runtime_profile={target_runtime_profile or '(empty)'}")

        if target_capabilities:
            existing_capabilities = tuple(str(item).strip() for item in updated_manifest.capabilities if str(item).strip())
            merged_capabilities = tuple(dict.fromkeys([*existing_capabilities, *target_capabilities]))
            if (
                merged_capabilities != updated_manifest.capabilities
                or failure_reason == "capability_missing"
            ):
                updated_manifest = replace(updated_manifest, capabilities=merged_capabilities)
                actions.append("aligned capabilities with kernel contract")

        if updated_manifest != manifest:
            write_module_manifest(manifest_path, updated_manifest)

        version_sync = sync_python_module_version(module_dir, updated_manifest.version)
        if bool(version_sync.get("ok")) and bool(version_sync.get("changed")):
            actions.append(f"sync module.py version -> {updated_manifest.version}")

        pycache_dir = module_dir / "__pycache__"
        if pycache_dir.exists():
            shutil.rmtree(pycache_dir)
            actions.append("removed __pycache__")

        return actions

    def _hash_tree(self, root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not root.exists():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            try:
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return out

    def _changed_files(self, before: dict[str, str], after: dict[str, str]) -> list[str]:
        keys = sorted(set(before) | set(after))
        out: list[str] = []
        for key in keys:
            if before.get(key) != after.get(key):
                out.append(key)
        return out

    def _shadow_override_from_task(self, task: dict[str, object]) -> dict[str, object]:
        label = str(task.get("label") or "").strip()
        module_dir = Path(str(task.get("workspace_dir") or "")).resolve() / "module"
        path_ref = f"path:{module_dir}"
        if label.startswith("provider:"):
            mode = label.split(":", 1)[1].strip()
            return {"providers": {mode: path_ref}}
        if label in {"router", "policy", "attachment_context", "finalizer", "tool_registry"}:
            return {label: path_ref}
        return {}

    def package_shadow_modules(
        self,
        *,
        labels: list[str] | None = None,
        package_note: str = "",
        source_run_id: str = "",
        repair_run_id: str = "",
        patch_worker_run_id: str = "",
        runtime_profile: str = "",
    ) -> dict[str, object]:
        run_id = self._pipeline_run_id()
        started_at = datetime.now(timezone.utc).isoformat()
        shadow_manifest = self.load_shadow_manifest()
        requested_labels = [str(item).strip() for item in (labels or []) if str(item).strip()]
        if not requested_labels:
            requested_labels = self._all_manifest_labels(shadow_manifest)

        path_labels: list[str] = []
        for label in requested_labels:
            ref = self._manifest_ref_for_label(label, shadow_manifest.to_dict())
            if ref.startswith("path:"):
                path_labels.append(label)

        if not path_labels:
            payload = {
                "ok": False,
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "no_path_modules_to_package",
                "requested_labels": requested_labels,
                "shadow_manifest": shadow_manifest.to_dict(),
            }
            self._write_json(self._package_runs_dir() / f"{run_id}.json", payload)
            self._write_json(self._last_package_run_path(), payload)
            return payload

        packager = ModulePackager(self.context)
        packaged_modules: list[dict[str, object]] = []
        updated_manifest = ActiveModuleManifest(
            router=shadow_manifest.router,
            policy=shadow_manifest.policy,
            attachment_context=shadow_manifest.attachment_context,
            finalizer=shadow_manifest.finalizer,
            tool_registry=shadow_manifest.tool_registry,
            providers=dict(shadow_manifest.providers),
        )
        manifest_snapshot = shadow_manifest.to_dict()

        for label in path_labels:
            ref = self._manifest_ref_for_label(label, manifest_snapshot)
            kind = self._module_kind_for_label(label)
            if not kind:
                continue
            reference = self.loader.resolve_ref(ref, expected_kind=kind)
            dependency_refs = [
                f"{other_label}={self._manifest_ref_for_label(other_label, manifest_snapshot)}"
                for other_label in self._all_manifest_labels(shadow_manifest)
                if other_label != label
            ]
            package_meta = packager.package_reference(
                reference=reference,
                source_ref=ref,
                depends_on=dependency_refs,
                runtime_profile=runtime_profile,
                package_note=package_note,
                metadata={
                    "label": label,
                    "source_run_id": source_run_id,
                    "repair_run_id": repair_run_id,
                    "patch_worker_run_id": patch_worker_run_id,
                    "shadow_manifest": manifest_snapshot,
                },
            )
            packaged_ref = str(package_meta.get("packaged_ref") or "").strip()
            self._set_manifest_ref_for_label(updated_manifest, label=label, ref=packaged_ref)
            package_meta["label"] = label
            packaged_modules.append(package_meta)

        self.write_shadow_manifest(updated_manifest)
        validation = self.validate_shadow_manifest()
        promote_check = self.shadow_promote_check()
        payload = {
            "ok": bool(packaged_modules) and bool(validation.get("ok")),
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "requested_labels": requested_labels,
            "packaged_labels": path_labels,
            "packaged_modules": packaged_modules,
            "shadow_manifest_before": manifest_snapshot,
            "shadow_manifest_after": updated_manifest.to_dict(),
            "validation": validation,
            "promote_check": promote_check,
        }
        self._write_json(self._package_runs_dir() / f"{run_id}.json", payload)
        self._write_json(self._last_package_run_path(), payload)
        return payload

    def _build_contract_check(self, *, label: str, ok: bool, kind: str, detail: str = "", **extra: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "label": label,
            "kind": kind,
            "ok": bool(ok),
            "detail": str(detail or ""),
        }
        payload.update(extra)
        return payload

    def _classify_pipeline_failure(
        self,
        *,
        stage: dict[str, object],
        validation: dict[str, object],
        smoke: dict[str, object],
        replay: dict[str, object],
        promotion: dict[str, object],
    ) -> dict[str, object]:
        if not bool(validation.get("ok")):
            return {
                "ok": False,
                "category": "manifest_validation",
                "failed_stage": "validation",
                "reason": self._pipeline_failure_text(validation) or "manifest_validation_failed",
                "retryable": True,
                "blocking_modules": self._pipeline_manifest_labels(validation),
            }
        if not bool(stage.get("ok")):
            return {
                "ok": False,
                "category": "stage_failed",
                "failed_stage": "stage",
                "reason": self._pipeline_failure_text(stage) or "stage_failed",
                "retryable": True,
                "blocking_modules": self._pipeline_manifest_labels(validation),
            }
        if smoke and not bool(smoke.get("ok")):
            return {
                "ok": False,
                "category": "shadow_smoke",
                "failed_stage": "smoke",
                "reason": self._pipeline_failure_text(smoke) or "shadow_smoke_failed",
                "retryable": True,
                "blocking_modules": [],
            }
        if replay and not bool(replay.get("ok")):
            return {
                "ok": False,
                "category": "shadow_replay",
                "failed_stage": "replay",
                "reason": self._pipeline_failure_text(replay) or "shadow_replay_failed",
                "retryable": True,
                "blocking_modules": [],
            }
        if promotion and not bool(promotion.get("ok")):
            unsafe_refs = promotion.get("unsafe_refs")
            blocking_modules = list(dict(unsafe_refs or {}).keys()) if isinstance(unsafe_refs, dict) else []
            return {
                "ok": False,
                "category": "promotion_failed",
                "failed_stage": "promotion",
                "reason": self._pipeline_failure_text(promotion) or "shadow_promotion_failed",
                "retryable": False,
                "blocking_modules": blocking_modules,
            }
        return {
            "ok": True,
            "category": "none",
            "failed_stage": "",
            "reason": "",
            "retryable": False,
            "blocking_modules": [],
        }

    def _remediation_hints(
        self,
        *,
        classification: dict[str, object],
        validation: dict[str, object],
        smoke: dict[str, object],
        replay: dict[str, object],
        promote_if_healthy: bool,
    ) -> list[str]:
        category = str(classification.get("category") or "")
        blocking_modules = [str(item).strip() for item in classification.get("blocking_modules") or [] if str(item).strip()]
        hints: list[str] = []
        if category == "manifest_validation":
            if blocking_modules:
                hints.append(f"先修 shadow manifest 里这些模块引用: {', '.join(blocking_modules)}。")
            hints.append("优先查看 validation.errors，确认模块版本目录、manifest entrypoint 和接口方法是否齐全。")
            hints.append("manifest 未通过前不要 promote，保持 active manifest 不动。")
        elif category == "shadow_smoke":
            provider = smoke.get("provider")
            if isinstance(provider, dict) and provider.get("ok") is False and not provider.get("skipped"):
                hints.append("先修 provider 路径；当前 shadow smoke 已在最小请求上失败。")
            hints.append("查看 smoke.error、selected_modules 和 module_health，确认是模块逻辑错误还是 provider 初始化失败。")
            hints.append("修完后先重新跑 shadow smoke，再决定是否进入 replay。")
        elif category == "shadow_replay":
            hints.append("优先比对 replay.source_run_id 对应的 shadow log 和 replay.execution_trace，确认回放输入是否完整。")
            hints.append("如果 smoke 通过但 replay 失败，问题通常在多轮状态、附件上下文或最终整理链。")
        elif category == "promotion_failed":
            if str(classification.get("reason") or "") == "path_ref_not_promotable":
                hints.append("当前 shadow 还挂着 `path:` 临时模块引用。先把修好的模块变成正式版本引用，再 promote。")
            if str(classification.get("reason") or "") == "dependency_mismatch":
                hints.append("当前模块依赖声明和 shadow manifest 不匹配。先对齐依赖模块引用，再 promote。")
            if str(classification.get("reason") or "") == "api_version_incompatible":
                hints.append("当前模块 manifest 的 api_version 与稳定内核不兼容，不能直接 promote。")
            if str(classification.get("reason") or "") == "kind_mismatch":
                hints.append("当前模块 manifest 的 kind 与装配位点不一致，先改回正确 kind。")
            if str(classification.get("reason") or "") == "capability_missing":
                hints.append("当前模块 manifest 的 capabilities 缺少该 kind 必需能力，先补齐再 promote。")
            if str(classification.get("reason") or "") == "module_version_mismatch":
                hints.append("当前模块代码里的 version 和 manifest.toml 不一致，先同步版本号再 promote。")
            if str(classification.get("reason") or "") == "runtime_profile_invalid":
                hints.append("当前模块 runtime_profile 非法，先改成已注册 profile 或留空。")
            hints.append("先确认 validation/smoke/replay 全部为 ok，再检查 promote 的 rollback_pointer 和 active manifest 写入权限。")
        elif category == "stage_failed":
            hints.append("先确认 shadow manifest override 是否写入了有效模块引用。")
        elif category == "none" and promote_if_healthy:
            hints.append("当前 shadow pipeline 已通过；如果后续要切换 live，可直接 promote。")
        if category != "none":
            hints.append("修完后重新跑 /api/kernel/shadow/pipeline，让内核产出新的 upgrade attempt。")
        return hints

    def run_shadow_smoke(
        self,
        *,
        user_message: str = "给我今天的新闻",
        validate_provider: bool = True,
    ) -> dict[str, object]:
        from app.agent import OfficeAgent
        from app.models import ChatSettings

        shadow_manifest = self.load_shadow_manifest()
        validation = self.supervisor.validate_manifest(shadow_manifest)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        run_dir = self.context.runtime_dir / "shadow_runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        payload: dict[str, object] = {
            "ok": False,
            "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "shadow_manifest": shadow_manifest.to_dict(),
            "validation": validation,
            "runtime_dir": str(run_dir),
        }

        if not validation.get("ok"):
            payload["error"] = "shadow_manifest_invalid"
            self._write_json(run_dir / "smoke_result.json", payload)
            self._write_json(self._last_shadow_run_path(), payload)
            return payload

        smoke_config = replace(
            self.supervisor._config,
            runtime_dir=run_dir,
            active_manifest_path=run_dir / "active_manifest.json",
            shadow_manifest_path=run_dir / "shadow_manifest.json",
            rollback_pointer_path=run_dir / "rollback_pointer.json",
            module_health_path=run_dir / "module_health.json",
        )
        write_active_manifest(smoke_config.active_manifest_path, shadow_manifest)
        write_active_manifest(smoke_config.shadow_manifest_path, shadow_manifest)

        try:
            smoke_runtime = build_kernel_runtime(smoke_config)
            active_validation = smoke_runtime.validate_active_manifest()
            smoke_agent = OfficeAgent(smoke_config, kernel_runtime=smoke_runtime)
            settings = ChatSettings()
            route = smoke_agent._route_request_by_rules(
                user_message=user_message,
                attachment_metas=[],
                settings=settings,
            )
            finalizer_preview = smoke_agent._sanitize_final_answer_text(
                '{"rows":[{"姓名":"张三","分数":95},{"姓名":"李四","分数":88}]}',
                user_message="把数据整理成表格",
                attachment_metas=[],
            )
            provider_info: dict[str, object] = {}
            auth_summary = smoke_agent._debug_openai_auth_summary()
            if validate_provider and bool(auth_summary.get("available")):
                try:
                    runner = smoke_agent._build_llm(
                        model=smoke_config.default_model,
                        max_output_tokens=256,
                        use_responses_api=False,
                    )
                    provider_info = {
                        "ok": True,
                        "mode": str(auth_summary.get("mode") or ""),
                        "runner_class": runner.__class__.__name__,
                    }
                except Exception as exc:
                    provider_info = {
                        "ok": False,
                        "mode": str(auth_summary.get("mode") or ""),
                        "error": str(exc),
                    }
            else:
                provider_info = {
                    "ok": False,
                    "skipped": True,
                    "mode": str(auth_summary.get("mode") or ""),
                    "reason": str(auth_summary.get("reason") or ""),
                }

            payload.update(
                {
                    "ok": True,
                    "route_task_type": str(route.get("task_type") or ""),
                    "route_execution_policy": str(route.get("execution_policy") or ""),
                    "finalizer_preview": str(finalizer_preview or "")[:400],
                    "provider": provider_info,
                    "selected_modules": dict(smoke_runtime.registry.selected_refs),
                    "module_health": dict(smoke_runtime.health_snapshot().module_health),
                    "active_validation": active_validation,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            payload.update(
                {
                    "ok": False,
                    "error": str(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        self._write_json(run_dir / "smoke_result.json", payload)
        self._write_json(self._last_shadow_run_path(), payload)
        return payload

    def run_shadow_replay(self, *, replay_record: dict[str, object]) -> dict[str, object]:
        from app.agent import OfficeAgent
        from app.models import ChatSettings

        shadow_manifest = self.load_shadow_manifest()
        validation = self.supervisor.validate_manifest(shadow_manifest)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        run_dir = self.context.runtime_dir / "shadow_runs" / f"replay-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)

        payload: dict[str, object] = {
            "ok": False,
            "run_id": run_id,
            "source_run_id": str(replay_record.get("run_id") or ""),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "shadow_manifest": shadow_manifest.to_dict(),
            "validation": validation,
            "runtime_dir": str(run_dir),
        }
        if not validation.get("ok"):
            payload["error"] = "shadow_manifest_invalid"
            self._write_json(run_dir / "replay_result.json", payload)
            self._write_json(self._last_shadow_run_path(), payload)
            return payload

        smoke_config = replace(
            self.supervisor._config,
            runtime_dir=run_dir,
            active_manifest_path=run_dir / "active_manifest.json",
            shadow_manifest_path=run_dir / "shadow_manifest.json",
            rollback_pointer_path=run_dir / "rollback_pointer.json",
            module_health_path=run_dir / "module_health.json",
        )
        write_active_manifest(smoke_config.active_manifest_path, shadow_manifest)
        write_active_manifest(smoke_config.shadow_manifest_path, shadow_manifest)

        try:
            shadow_runtime = build_kernel_runtime(smoke_config)
            shadow_agent = OfficeAgent(smoke_config, kernel_runtime=shadow_runtime)
            settings_payload = replay_record.get("settings")
            settings = ChatSettings(**settings_payload) if isinstance(settings_payload, dict) else ChatSettings()
            attachment_metas = replay_record.get("attachment_metas")
            history_turns_before = replay_record.get("history_turns_before")
            route_state_input = replay_record.get("route_state_input")
            result = shadow_agent.run_chat(
                history_turns=list(history_turns_before) if isinstance(history_turns_before, list) else [],
                summary=str(replay_record.get("summary_before") or ""),
                user_message=str(replay_record.get("message") or replay_record.get("message_preview") or ""),
                attachment_metas=list(attachment_metas) if isinstance(attachment_metas, list) else [],
                settings=settings,
                session_id=str(replay_record.get("session_id") or ""),
                route_state=dict(route_state_input) if isinstance(route_state_input, dict) else {},
                progress_cb=None,
            )
            (
                text,
                tool_events,
                _attachment_note,
                execution_plan,
                execution_trace,
                pipeline_hooks,
                _debug_flow,
                _agent_panels,
                active_roles,
                current_role,
                _role_states,
                answer_bundle,
                token_usage,
                effective_model,
                route_state,
            ) = result
            payload.update(
                {
                    "ok": True,
                    "effective_model": effective_model,
                    "text_preview": str(text or "")[:600],
                    "tool_event_count": len(tool_events),
                    "execution_plan": execution_plan,
                    "execution_trace": execution_trace[-10:],
                    "pipeline_hook_count": len(pipeline_hooks),
                    "active_roles": active_roles,
                    "current_role": current_role,
                    "answer_bundle": answer_bundle,
                    "token_usage": token_usage,
                    "route_state": route_state,
                    "selected_modules": dict(shadow_runtime.registry.selected_refs),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            payload.update(
                {
                    "ok": False,
                    "error": str(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        self._write_json(run_dir / "replay_result.json", payload)
        self._write_json(self._last_shadow_run_path(), payload)
        return payload

    def _run_manifest_contracts(
        self,
        *,
        manifest: ActiveModuleManifest,
        manifest_source: str,
    ) -> dict[str, object]:
        from app.agent import OfficeAgent
        from app.models import ChatSettings

        probe = self.supervisor.probe_manifest_contracts(manifest)
        checks: list[dict[str, object]] = []
        capability_checks: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "ok": bool(probe.get("ok")),
            "manifest_source": manifest_source,
            "manifest": manifest.to_dict(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "probe": probe,
            "checks": checks,
            "capability_checks": capability_checks,
        }
        payload[f"{manifest_source}_manifest"] = manifest.to_dict()
        if not bool(probe.get("ok")):
            return payload

        run_id = self._pipeline_run_id()
        run_dir = self.context.runtime_dir / "shadow_runs" / f"{manifest_source}-contracts-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        contract_config = replace(
            self.supervisor._config,
            runtime_dir=run_dir,
            active_manifest_path=run_dir / "active_manifest.json",
            shadow_manifest_path=run_dir / "shadow_manifest.json",
            rollback_pointer_path=run_dir / "rollback_pointer.json",
            module_health_path=run_dir / "module_health.json",
        )
        write_active_manifest(contract_config.active_manifest_path, manifest)
        write_active_manifest(contract_config.shadow_manifest_path, manifest)
        contract_runtime = build_kernel_runtime(contract_config)
        contract_agent = OfficeAgent(contract_config, kernel_runtime=contract_runtime)
        settings = ChatSettings()
        route: dict[str, object] = {}

        try:
            route = contract_agent._route_request_by_rules(
                user_message="给我今天的新闻",
                attachment_metas=[],
                settings=settings,
            )
            checks.append(
                self._build_contract_check(
                    label="router",
                    kind="router",
                    ok=bool(str(route.get("task_type") or "").strip()),
                    detail=str(route.get("task_type") or ""),
                    resolved_ref=str((contract_runtime.registry.selected_refs or {}).get("router") or ""),
                    task_type=str(route.get("task_type") or ""),
                    execution_policy=str(route.get("execution_policy") or ""),
                )
            )
        except Exception as exc:
            checks.append(self._build_contract_check(label="router", kind="router", ok=False, detail=str(exc)))

        try:
            route_input = route if isinstance(route, dict) and route else {
                "task_type": "simple_qa",
                "execution_policy": "qa_direct",
                "use_worker_tools": False,
            }
            normalized = contract_agent._normalize_route_decision_impl(route=route_input, fallback=route_input, settings=settings)
            checks.append(
                self._build_contract_check(
                    label="policy",
                    kind="policy",
                    ok=bool(str(normalized.get("execution_policy") or "").strip()),
                    detail=str(normalized.get("execution_policy") or ""),
                    resolved_ref=str((contract_runtime.registry.selected_refs or {}).get("policy") or ""),
                )
            )
        except Exception as exc:
            checks.append(self._build_contract_check(label="policy", kind="policy", ok=False, detail=str(exc)))

        try:
            attachment_module = contract_runtime.registry.attachment_context
            session = {
                "id": "contract-session",
                "summary": "",
                "turns": [],
                "active_attachment_ids": [],
                "route_state": {},
                "attachment_route_states": {},
            }
            ctx = attachment_module.resolve_attachment_context(session=session, message="继续解释一下", requested_attachment_ids=None)
            attachment_module.apply_attachment_context_result(
                session=session,
                resolved_attachment_ids=[],
                attachment_context_mode=str(ctx.get("attachment_context_mode") or "none"),
                clear_attachment_context=False,
                requested_attachment_ids=ctx.get("requested_attachment_ids"),
            )
            scoped_state, scope = attachment_module.resolve_scoped_route_state(session=session, attachment_ids=[])
            attachment_module.store_scoped_route_state(session=session, attachment_ids=[], route_state={"primary_intent": "qa"})
            checks.append(
                self._build_contract_check(
                    label="attachment_context",
                    kind="attachment_context",
                    ok=True,
                    detail=str(scope or ""),
                    resolved_ref=str((contract_runtime.registry.selected_refs or {}).get("attachment_context") or ""),
                    route_state_scope=str(scope or ""),
                    resolved_state_keys=sorted(scoped_state.keys()) if isinstance(scoped_state, dict) else [],
                )
            )
        except Exception as exc:
            checks.append(self._build_contract_check(label="attachment_context", kind="attachment_context", ok=False, detail=str(exc)))

        try:
            sanitized = contract_agent._sanitize_final_answer_text(
                '{"rows":[{"姓名":"张三","分数":95},{"姓名":"李四","分数":88}]}',
                user_message="把数据整理成表格",
                attachment_metas=[],
            )
            checks.append(
                self._build_contract_check(
                    label="finalizer",
                    kind="finalizer",
                    ok="| 姓名 | 分数 |" in str(sanitized),
                    detail=str(sanitized)[:120],
                    resolved_ref=str((contract_runtime.registry.selected_refs or {}).get("finalizer") or ""),
                )
            )
        except Exception as exc:
            checks.append(self._build_contract_check(label="finalizer", kind="finalizer", ok=False, detail=str(exc)))

        try:
            tool_registry = contract_runtime.registry.tool_registry
            tools = tool_registry.build_langchain_tools(agent=contract_agent)
            checks.append(
                self._build_contract_check(
                    label="tool_registry",
                    kind="tool_registry",
                    ok=bool(tools),
                    detail=f"tool_count={len(tools)}",
                    resolved_ref=str((contract_runtime.registry.selected_refs or {}).get("tool_registry") or ""),
                    tool_count=len(tools),
                )
            )
        except Exception as exc:
            checks.append(self._build_contract_check(label="tool_registry", kind="tool_registry", ok=False, detail=str(exc)))

        auth_summary = contract_agent._debug_openai_auth_summary()
        for mode in sorted((contract_runtime.registry.providers or {}).keys()):
            provider = contract_runtime.registry.providers.get(mode)
            label = f"provider:{mode}"
            try:
                if mode == "api_key":
                    auth = contract_agent._auth_manager._resolve_api_key_auth()
                elif mode == "codex_auth":
                    auth = contract_agent._auth_manager._resolve_codex_auth()
                else:
                    auth = contract_agent._auth_manager.resolve()
                if not bool(getattr(auth, "available", False)):
                    raise RuntimeError(str(getattr(auth, "reason", "") or f"{mode} auth unavailable"))
                runner = provider.build_runner(  # type: ignore[union-attr]
                    agent=contract_agent,
                    auth=auth,
                    model=contract_config.default_model,
                    max_output_tokens=64,
                    use_responses_api=False,
                )
                checks.append(
                    self._build_contract_check(
                        label=label,
                        kind="provider",
                        ok=True,
                        detail=runner.__class__.__name__,
                        resolved_ref=str((contract_runtime.registry.selected_refs or {}).get(f"provider:{mode}") or ""),
                        runner_class=runner.__class__.__name__,
                    )
                )
            except Exception as exc:
                mode_matches = str(auth_summary.get("mode") or "").strip() == mode
                available = bool(auth_summary.get("available"))
                skipped = not (mode_matches and available)
                checks.append(
                    self._build_contract_check(
                        label=label,
                        kind="provider",
                        ok=skipped,
                        detail=str(exc),
                        resolved_ref=str((contract_runtime.registry.selected_refs or {}).get(f"provider:{mode}") or ""),
                        skipped=skipped,
                    )
                )

        capability_checks.extend(
            run_module_capability_smoke(
                runtime=contract_runtime,
                agent=contract_agent,
                settings=settings,
                artifact_root=run_dir,
            )
        )

        payload["ok"] = (
            bool(probe.get("ok"))
            and all(bool(item.get("ok")) for item in checks)
            and all(bool(item.get("ok")) for item in capability_checks)
        )
        return payload

    def run_shadow_contracts(self) -> dict[str, object]:
        return self._run_manifest_contracts(
            manifest=self.load_shadow_manifest(),
            manifest_source="shadow",
        )

    def run_active_contracts(self) -> dict[str, object]:
        return self._run_manifest_contracts(
            manifest=self.supervisor.load_active_manifest(),
            manifest_source="active",
        )

    def run_shadow_pipeline(
        self,
        *,
        overrides: dict[str, object] | None = None,
        smoke_message: str = "给我今天的新闻",
        validate_provider: bool = True,
        replay_record: dict[str, object] | None = None,
        promote_if_healthy: bool = False,
    ) -> dict[str, object]:
        run_id = self._pipeline_run_id()
        started_at = datetime.now(timezone.utc).isoformat()
        stage = self.stage_shadow_manifest(overrides=overrides or {})
        validation = stage.get("validation") if isinstance(stage.get("validation"), dict) else self.validate_shadow_manifest()
        promote_check = stage.get("promote_check") if isinstance(stage.get("promote_check"), dict) else self.shadow_promote_check()
        contracts = self.run_shadow_contracts()
        smoke: dict[str, object] = {}
        replay: dict[str, object] = {}
        promotion: dict[str, object] = {}

        if bool(validation.get("ok")) and bool(contracts.get("ok")):
            smoke = self.run_shadow_smoke(
                user_message=smoke_message,
                validate_provider=bool(validate_provider),
            )
            if isinstance(replay_record, dict) and replay_record:
                replay = self.run_shadow_replay(replay_record=replay_record)
            if req_ready := (
                bool(promote_if_healthy)
                and bool(smoke.get("ok"))
                and (not replay or bool(replay.get("ok")))
            ):
                promotion = self.promote_shadow_manifest()
            else:
                req_ready = False
        else:
            req_ready = False

        overall_ok = bool(stage.get("ok")) and bool(validation.get("ok"))
        overall_ok = overall_ok and bool(contracts.get("ok"))
        if smoke:
            overall_ok = overall_ok and bool(smoke.get("ok"))
        if replay:
            overall_ok = overall_ok and bool(replay.get("ok"))
        if promotion:
            overall_ok = overall_ok and bool(promotion.get("ok"))

        classification = self._classify_pipeline_failure(
            stage=stage,
            validation=validation,
            smoke=smoke,
            replay=replay,
            promotion=promotion,
        )
        remediation_hints = self._remediation_hints(
            classification=classification,
            validation=validation,
            smoke=smoke,
            replay=replay,
            promote_if_healthy=bool(promote_if_healthy),
        )
        payload: dict[str, object] = {
            "ok": overall_ok,
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "target_overrides": dict(overrides or {}),
            "smoke_message": str(smoke_message or ""),
            "validate_provider": bool(validate_provider),
            "replay_source_run_id": str(replay_record.get("run_id") or "") if isinstance(replay_record, dict) else "",
            "promote_if_healthy": bool(promote_if_healthy),
            "promotion_attempted": bool(req_ready),
            "stage": stage,
            "validation": validation,
            "promote_check": promote_check,
            "contracts": contracts,
            "smoke": smoke,
            "replay": replay,
            "promotion": promotion,
            "failure_classification": classification,
            "remediation_hints": remediation_hints,
        }
        self._write_json(self._upgrade_runs_dir() / f"{run_id}.json", payload)
        self._write_json(self._last_upgrade_run_path(), payload)
        return payload

    def run_shadow_auto_repair(
        self,
        *,
        base_upgrade_run: dict[str, object] | None = None,
        replay_record: dict[str, object] | None = None,
        smoke_message: str | None = None,
        validate_provider: bool | None = None,
        promote_if_healthy: bool | None = None,
        max_attempts: int = 1,
    ) -> dict[str, object]:
        base = dict(base_upgrade_run or self.read_last_upgrade_run())
        repair_run_id = self._pipeline_run_id()
        started_at = datetime.now(timezone.utc).isoformat()
        if not base:
            payload = {
                "ok": False,
                "run_id": repair_run_id,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "missing_base_upgrade_run",
            }
            self._write_json(self._repair_runs_dir() / f"{repair_run_id}.json", payload)
            self._write_json(self._last_repair_run_path(), payload)
            return payload

        classification = base.get("failure_classification")
        classification = dict(classification) if isinstance(classification, dict) else {}
        category = str(classification.get("category") or "")
        blocking_modules = [str(item).strip() for item in classification.get("blocking_modules") or [] if str(item).strip()]
        active_manifest = self.supervisor.load_active_manifest()
        attempt_limit = max(1, min(5, int(max_attempts)))
        attempts: list[dict[str, object]] = []
        applied_overrides: dict[str, object] = {}
        strategy = ""
        current_base = base

        for attempt_index in range(1, attempt_limit + 1):
            current_classification = current_base.get("failure_classification")
            current_classification = dict(current_classification) if isinstance(current_classification, dict) else {}
            current_category = str(current_classification.get("category") or "")
            current_blocks = [str(item).strip() for item in current_classification.get("blocking_modules") or [] if str(item).strip()]

            repair_plan: dict[str, object] = {"ok": False, "reason": "unsupported_failure_category", "strategy": "", "overrides": {}}
            if current_category in {"manifest_validation", "stage_failed"} and current_blocks:
                overrides: dict[str, object] = {}
                provider_overrides: dict[str, str] = {}
                for label in current_blocks:
                    override = self._safe_active_override_for_label(label, active_manifest)
                    if not override:
                        continue
                    key, ref = override
                    if key.startswith("providers."):
                        provider_overrides[key.split(".", 1)[1]] = ref
                    else:
                        overrides[key] = ref
                if provider_overrides:
                    overrides["providers"] = provider_overrides
                if overrides:
                    repair_plan = {
                        "ok": True,
                        "reason": "reset_blocking_modules_to_active",
                        "strategy": "reset_blocking_modules_to_active",
                        "overrides": overrides,
                    }
            elif current_category == "shadow_smoke":
                provider_info = current_base.get("smoke")
                provider_info = dict(provider_info) if isinstance(provider_info, dict) else {}
                provider_payload = provider_info.get("provider")
                provider_payload = dict(provider_payload) if isinstance(provider_payload, dict) else {}
                mode = str(provider_payload.get("mode") or "").strip()
                if mode:
                    active_ref = str(active_manifest.providers.get(mode) or "").strip()
                    if active_ref:
                        repair_plan = {
                            "ok": True,
                            "reason": "reset_provider_to_active",
                            "strategy": "reset_provider_to_active",
                            "overrides": {"providers": {mode: active_ref}},
                        }

            attempt_record: dict[str, object] = {
                "attempt_index": attempt_index,
                "category": current_category,
                "blocking_modules": current_blocks,
                "plan": repair_plan,
            }
            attempts.append(attempt_record)
            if not bool(repair_plan.get("ok")):
                break

            override_payload = dict(repair_plan.get("overrides") or {})
            if override_payload == applied_overrides:
                attempt_record["plan"] = dict(repair_plan) | {"ok": False, "reason": "duplicate_repair_plan"}
                break

            applied_overrides = override_payload
            strategy = str(repair_plan.get("strategy") or "")
            repaired_pipeline = self.run_shadow_pipeline(
                overrides=applied_overrides,
                smoke_message=str(smoke_message or current_base.get("smoke_message") or "给我今天的新闻"),
                validate_provider=bool(validate_provider if validate_provider is not None else current_base.get("validate_provider", True)),
                replay_record=replay_record,
                promote_if_healthy=bool(promote_if_healthy if promote_if_healthy is not None else current_base.get("promote_if_healthy", False)),
            )
            attempt_record["pipeline_run_id"] = str(repaired_pipeline.get("run_id") or "")
            attempt_record["pipeline_ok"] = bool(repaired_pipeline.get("ok"))
            current_base = repaired_pipeline
            if bool(repaired_pipeline.get("ok")):
                break

        payload = {
            "ok": bool(current_base.get("ok")),
            "run_id": repair_run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "base_upgrade_run_id": str(base.get("run_id") or ""),
            "base_failure_classification": classification,
            "strategy": strategy,
            "applied_overrides": applied_overrides,
            "attempts": attempts,
            "repaired_pipeline": current_base if isinstance(current_base, dict) else {},
        }
        patch_workspace = self._prepare_repair_tasks(
            repair_run_id=repair_run_id,
            classification=classification if classification else current_base.get("failure_classification") if isinstance(current_base, dict) else {},
            base_upgrade_run=base,
        )
        payload["repair_workspace_root"] = str(patch_workspace.get("workspace_root") or "")
        payload["repair_tasks"] = list(patch_workspace.get("tasks") or [])
        self._write_json(self._repair_runs_dir() / f"{repair_run_id}.json", payload)
        self._write_json(self._last_repair_run_path(), payload)
        return payload

    def run_shadow_patch_worker(
        self,
        *,
        repair_run: dict[str, object] | None = None,
        replay_record: dict[str, object] | None = None,
        max_tasks: int = 1,
        max_rounds: int = 2,
        auto_package_on_success: bool = True,
        promote_if_healthy: bool | None = None,
    ) -> dict[str, object]:
        from app.agent import OfficeAgent
        from app.models import ChatSettings

        repair_payload = dict(repair_run or self.read_last_repair_run())
        run_id = self._pipeline_run_id()
        started_at = datetime.now(timezone.utc).isoformat()
        tasks = repair_payload.get("repair_tasks")
        task_items = list(tasks) if isinstance(tasks, list) else []
        if not task_items:
            payload = {
                "ok": False,
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "missing_repair_tasks",
                "max_tasks_requested": max(1, min(10, int(max_tasks))),
                "max_rounds_requested": max(1, min(5, int(max_rounds))),
            }
            self._write_json(self._patch_worker_runs_dir() / f"{run_id}.json", payload)
            self._write_json(self._last_patch_worker_run_path(), payload)
            return payload

        limit = max(1, min(10, int(max_tasks)))
        base_pipeline = repair_payload.get("repaired_pipeline")
        base_pipeline = dict(base_pipeline) if isinstance(base_pipeline, dict) else {}
        smoke_message = str(base_pipeline.get("smoke_message") or "给我今天的新闻")
        validate_provider = bool(base_pipeline.get("validate_provider", True))
        promote_flag = bool(promote_if_healthy if promote_if_healthy is not None else base_pipeline.get("promote_if_healthy", False))
        round_limit = max(1, min(5, int(max_rounds)))
        rounds: list[dict[str, object]] = []
        executed_tasks: list[dict[str, object]] = []
        shadow_overrides: dict[str, object] = {}
        pipeline: dict[str, object] = dict(base_pipeline)
        package_run: dict[str, object] = {}
        packaged_pipeline: dict[str, object] = {}
        stop_reason = "max_rounds_reached"
        previous_round: dict[str, object] = {}

        for round_index in range(1, round_limit + 1):
            round_tasks: list[dict[str, object]] = []
            round_overrides: dict[str, object] = {}

            for task in task_items[:limit]:
                task_payload = dict(task or {})
                task_dir = Path(str(task_payload.get("workspace_dir") or "")).resolve()
                module_dir = task_dir / "module"
                repair_task_path = task_dir / "repair_task.json"
                repair_context = dict(task_payload)
                repair_context["current_round"] = round_index
                repair_context["max_rounds"] = round_limit
                repair_context["last_pipeline_failure"] = dict(pipeline.get("failure_classification") or previous_round.get("failure_classification") or {})
                repair_context["last_remediation_hints"] = list(pipeline.get("remediation_hints") or previous_round.get("remediation_hints") or [])
                repair_context["previous_round_changed_files"] = list(previous_round.get("changed_files") or [])
                self._write_json(repair_task_path, repair_context)
                before_hashes = self._hash_tree(module_dir)
                recipe_actions = self._apply_patch_worker_recipes(
                    task_payload=task_payload,
                    repair_context=repair_context,
                    module_dir=module_dir,
                )
                worker_runtime_dir = task_dir / "_runtime"
                worker_cfg = replace(
                    self.supervisor._config,
                    workspace_root=task_dir,
                    runtime_dir=worker_runtime_dir,
                    active_manifest_path=worker_runtime_dir / "active_manifest.json",
                    shadow_manifest_path=worker_runtime_dir / "shadow_manifest.json",
                    rollback_pointer_path=worker_runtime_dir / "rollback_pointer.json",
                    module_health_path=worker_runtime_dir / "module_health.json",
                    sessions_dir=task_dir / "_sessions",
                    uploads_dir=task_dir / "_uploads",
                    shadow_logs_dir=task_dir / "_shadow_logs",
                    token_stats_path=task_dir / "_token_stats.json",
                    allowed_roots=[task_dir],
                    default_extra_allowed_roots=[],
                    enable_shadow_logging=False,
                )
                worker_runtime = build_kernel_runtime(worker_cfg)
                worker_agent = OfficeAgent(worker_cfg, kernel_runtime=worker_runtime)
                settings = ChatSettings(enable_tools=True, response_style="short", max_output_tokens=12000)
                message = (
                    f"你正在修复一个 shadow 模块副本，第 {round_index}/{round_limit} 轮。"
                    f"先读取 {repair_task_path}，再检查并修改 {module_dir} 下文件。"
                    f"只允许修改 {module_dir} 内的文件，不要碰 live 代码，不要修改 {task_dir} 之外任何路径。"
                    "目标是让这个模块通过 repair_task.json 指定的修复目标。"
                    "如果上一轮还没修好，请优先根据 last_pipeline_failure 和 last_remediation_hints 继续修。"
                    "必须实际调用读写工具完成修改。完成后简要说明修改了哪些文件。"
                )
                (
                    text,
                    tool_events,
                    _attachment_note,
                    execution_plan,
                    execution_trace,
                    pipeline_hooks,
                    _debug_flow,
                    _agent_panels,
                    active_roles,
                    current_role,
                    _role_states,
                    answer_bundle,
                    token_usage,
                    effective_model,
                    route_state,
                ) = worker_agent.run_chat(
                    history_turns=[],
                    summary="",
                    user_message=message,
                    attachment_metas=[],
                    settings=settings,
                    session_id=f"repair-worker-{run_id}-r{round_index}",
                    route_state={},
                    progress_cb=None,
                )
                after_hashes = self._hash_tree(module_dir)
                changed_files = self._changed_files(before_hashes, after_hashes)
                override = self._shadow_override_from_task(task_payload)
                if "providers" in override:
                    round_overrides.setdefault("providers", {})
                    round_overrides["providers"].update(dict(override.get("providers") or {}))  # type: ignore[index]
                else:
                    round_overrides.update(override)
                task_result = {
                    "label": str(task_payload.get("label") or ""),
                    "round_index": round_index,
                    "workspace_dir": str(task_dir),
                    "module_dir": str(module_dir),
                    "recipe_actions": recipe_actions,
                    "changed_files": changed_files,
                    "tool_event_count": len(tool_events),
                    "execution_plan": execution_plan,
                    "execution_trace_tail": execution_trace[-10:],
                    "pipeline_hook_count": len(pipeline_hooks),
                    "active_roles": active_roles,
                    "current_role": current_role,
                    "answer_bundle": answer_bundle,
                    "text_preview": str(text or "")[:600],
                    "token_usage": token_usage,
                    "effective_model": effective_model,
                    "route_state": route_state,
                }
                round_tasks.append(task_result)
                executed_tasks.append(task_result)

            shadow_overrides = round_overrides
            pipeline = self.run_shadow_pipeline(
                overrides=shadow_overrides,
                smoke_message=smoke_message,
                validate_provider=validate_provider,
                replay_record=replay_record,
                promote_if_healthy=promote_flag,
            )
            round_changed_files = sorted(
                {
                    changed_file
                    for item in round_tasks
                    for changed_file in list(item.get("changed_files") or [])
                    if str(changed_file).strip()
                }
            )
            round_record = {
                "round_index": round_index,
                "executed_tasks": round_tasks,
                "changed_files": round_changed_files,
                "changed_file_count": len(round_changed_files),
                "shadow_overrides": dict(shadow_overrides),
                "pipeline_run_id": str(pipeline.get("run_id") or ""),
                "pipeline_ok": bool(pipeline.get("ok")),
                "failure_classification": dict(pipeline.get("failure_classification") or {}),
                "remediation_hints": list(pipeline.get("remediation_hints") or []),
            }
            rounds.append(round_record)
            previous_round = round_record

            if bool(pipeline.get("ok")):
                stop_reason = "pipeline_ok"
                break
            if round_index == round_limit:
                stop_reason = "max_rounds_reached"
                break

        final_classification = dict(pipeline.get("failure_classification") or {}) if isinstance(pipeline.get("failure_classification"), dict) else {}
        package_reason = str(final_classification.get("reason") or "")
        package_category = str(final_classification.get("category") or "")
        should_package = bool(pipeline.get("ok")) or (
            auto_package_on_success
            and package_category == "promotion_failed"
            and package_reason == "path_ref_not_promotable"
        )
        if should_package and auto_package_on_success:
            path_labels = [str(item.get("label") or "").strip() for item in task_items[:limit] if str(item.get("label") or "").strip()]
            if path_labels:
                package_run = self.package_shadow_modules(
                    labels=path_labels,
                    package_note="patch_worker_auto_package",
                    source_run_id=str(pipeline.get("run_id") or ""),
                    repair_run_id=str(repair_payload.get("run_id") or ""),
                    patch_worker_run_id=run_id,
                    runtime_profile=PATCH_WORKER_PROFILE.profile_id,
                )
                if bool(package_run.get("ok")):
                    packaged_pipeline = self.run_shadow_pipeline(
                        overrides={},
                        smoke_message=smoke_message,
                        validate_provider=validate_provider,
                        replay_record=replay_record,
                        promote_if_healthy=promote_flag,
                    )
                    pipeline = packaged_pipeline
                    stop_reason = "packaged_pipeline_ok" if bool(packaged_pipeline.get("ok")) else "packaged_pipeline_failed"
                else:
                    stop_reason = "package_failed"

        payload = {
            "ok": bool(pipeline.get("ok")),
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "base_repair_run_id": str(repair_payload.get("run_id") or ""),
            "max_tasks_requested": limit,
            "max_rounds_requested": round_limit,
            "stop_reason": stop_reason,
            "shadow_overrides": shadow_overrides,
            "executed_tasks": executed_tasks,
            "rounds": rounds,
            "round_count": len(rounds),
            "auto_package_on_success": bool(auto_package_on_success),
            "package_run": package_run,
            "packaged_pipeline": packaged_pipeline,
            "pipeline": pipeline,
        }
        patch_runs_dir = self._patch_worker_runs_dir()
        self._write_json(patch_runs_dir / f"{run_id}.json", payload)
        self._write_json(self._last_patch_worker_run_path(), payload)
        return payload

    def run_shadow_self_upgrade(
        self,
        *,
        base_upgrade_run: dict[str, object] | None = None,
        replay_record: dict[str, object] | None = None,
        smoke_message: str | None = None,
        validate_provider: bool | None = None,
        max_attempts: int = 1,
        max_tasks: int = 1,
        max_rounds: int = 2,
        promote_if_healthy: bool = True,
    ) -> dict[str, object]:
        run_id = self._pipeline_run_id()
        started_at = datetime.now(timezone.utc).isoformat()
        repair = self.run_shadow_auto_repair(
            base_upgrade_run=base_upgrade_run,
            replay_record=replay_record,
            smoke_message=smoke_message,
            validate_provider=validate_provider,
            promote_if_healthy=False,
            max_attempts=max_attempts,
        )
        repaired_pipeline = dict(repair.get("repaired_pipeline") or {}) if isinstance(repair.get("repaired_pipeline"), dict) else {}
        patch_worker: dict[str, object] = {}
        promotion: dict[str, object] = {}
        final_pipeline = repaired_pipeline
        stop_reason = "repair_failed"

        if bool(repaired_pipeline.get("ok")):
            stop_reason = "repair_pipeline_ok"
            if promote_if_healthy:
                promotion = self.promote_shadow_manifest()
                stop_reason = "promoted_after_repair" if bool(promotion.get("ok")) else "promotion_failed_after_repair"
        elif list(repair.get("repair_tasks") or []):
            patch_worker = self.run_shadow_patch_worker(
                repair_run=repair,
                replay_record=replay_record,
                max_tasks=max_tasks,
                max_rounds=max_rounds,
                auto_package_on_success=True,
                promote_if_healthy=promote_if_healthy,
            )
            final_pipeline = dict(
                patch_worker.get("packaged_pipeline")
                or patch_worker.get("pipeline")
                or {}
            )
            stop_reason = str(patch_worker.get("stop_reason") or "patch_worker_completed")
            if not promotion and isinstance(final_pipeline.get("promotion"), dict):
                promotion = dict(final_pipeline.get("promotion") or {})

        payload = {
            "ok": bool(final_pipeline.get("ok")) and (not promote_if_healthy or bool(promotion.get("ok")) or not promotion),
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "base_upgrade_run_id": str((base_upgrade_run or {}).get("run_id") or ""),
            "stop_reason": stop_reason,
            "repair": repair,
            "patch_worker": patch_worker,
            "promotion": promotion,
            "final_pipeline": final_pipeline,
        }
        self._write_json(self._upgrade_runs_dir() / f"{run_id}.self_upgrade.json", payload)
        return payload


def build_kernel_runtime(config: AppConfig) -> KernelRuntime:
    context = ModuleRuntimeContext(
        workspace_root=config.workspace_root,
        modules_dir=config.modules_dir,
        runtime_dir=config.runtime_dir,
    )
    loader = ModuleLoader(context)
    supervisor = KernelSupervisor(config, context=context, loader=loader)
    registry = supervisor.load_registry()
    return KernelRuntime(context=context, loader=loader, supervisor=supervisor, registry=registry)
