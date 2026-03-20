from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.agents.runtime_profiles import RUNTIME_PROFILES
from app.config import AppConfig
from app.core.module_code import read_python_module_version
from app.core.module_loader import ModuleLoader
from app.core.module_manifest import (
    DEFAULT_ACTIVE_MANIFEST,
    ActiveModuleManifest,
    active_manifest_from_dict,
    read_active_manifest,
    read_module_manifest,
    write_active_manifest,
)
from app.core.module_registry import KernelModuleRegistry
from app.core.module_types import ModuleHealthSnapshot, ModuleRuntimeContext, ModuleSelection


class KernelSupervisor:
    def __init__(self, config: AppConfig, *, context: ModuleRuntimeContext, loader: ModuleLoader) -> None:
        self._config = config
        self._context = context
        self._loader = loader
        self._ensure_runtime_files()

    def _ensure_runtime_files(self) -> None:
        self._config.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self._config.runtime_dir / "upgrade_runs").mkdir(parents=True, exist_ok=True)
        (self._config.runtime_dir / "repair_runs").mkdir(parents=True, exist_ok=True)
        (self._config.runtime_dir / "patch_worker_runs").mkdir(parents=True, exist_ok=True)
        (self._config.runtime_dir / "package_runs").mkdir(parents=True, exist_ok=True)
        (self._config.runtime_dir / "shadow_runs").mkdir(parents=True, exist_ok=True)
        if not self._config.active_manifest_path.is_file():
            write_active_manifest(self._config.active_manifest_path, active_manifest_from_dict(DEFAULT_ACTIVE_MANIFEST))
        if not self._config.shadow_manifest_path.is_file():
            write_active_manifest(self._config.shadow_manifest_path, active_manifest_from_dict(DEFAULT_ACTIVE_MANIFEST))
        for path, payload in (
            (self._config.rollback_pointer_path, {"active_manifest": str(self._config.active_manifest_path)}),
            (self._config.module_health_path, {}),
        ):
            if path.is_file():
                continue
            self._write_json(path, payload)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def load_active_manifest(self) -> ActiveModuleManifest:
        return read_active_manifest(self._config.active_manifest_path)

    def load_shadow_manifest(self) -> ActiveModuleManifest:
        return read_active_manifest(self._config.shadow_manifest_path)

    def write_shadow_manifest(self, manifest: ActiveModuleManifest) -> None:
        write_active_manifest(self._config.shadow_manifest_path, manifest)

    def read_rollback_pointer(self) -> dict[str, Any]:
        raw = self._read_json(self._config.rollback_pointer_path, {})
        if isinstance(raw, dict):
            return raw
        return {}

    def load_module_health(self) -> dict[str, dict[str, Any]]:
        raw = self._read_json(self._config.module_health_path, {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            key_text = str(key or "").strip()
            if key_text and isinstance(value, dict):
                out[key_text] = dict(value)
        return out

    def save_module_health(self, payload: dict[str, dict[str, Any]]) -> None:
        self._write_json(self._config.module_health_path, payload)

    def _validate_module_contract(self, *, kind: str, module: Any, ref: str) -> None:
        required_methods: dict[str, tuple[str, ...]] = {
            "router": ("route",),
            "policy": ("normalize_route",),
            "attachment_context": (
                "resolve_attachment_context",
                "apply_attachment_context_result",
                "resolve_scoped_route_state",
                "store_scoped_route_state",
            ),
            "finalizer": ("sanitize",),
            "tool_registry": ("build_langchain_tools", "describe_tools"),
            "provider": ("build_runner",),
        }
        missing = [name for name in required_methods.get(kind, ()) if not callable(getattr(module, name, None))]
        if missing:
            raise RuntimeError(f"{ref} missing methods: {', '.join(missing)}")

    def _validate_module_instance(self, module: Any, *, kind: str, ref: str) -> None:
        self._validate_module_contract(kind=kind, module=module, ref=ref)
        checker = getattr(module, "self_check", None)
        if not callable(checker):
            return
        result = checker()
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(str(result.get("reason") or f"{ref} self_check failed"))

    def validate_manifest(self, manifest: ActiveModuleManifest) -> dict[str, Any]:
        resolved: dict[str, str] = {}
        errors: list[str] = []

        def validate_one(kind: str, ref: str, *, label: str) -> None:
            try:
                module, reference = self._loader.load(ref, expected_kind=kind)
                self._validate_module_instance(module, kind=kind, ref=reference.ref)
                resolved[label] = reference.ref
            except Exception as exc:
                errors.append(f"{label}: {ref}: {exc}")

        validate_one("router", manifest.router, label="router")
        validate_one("policy", manifest.policy, label="policy")
        validate_one("attachment_context", manifest.attachment_context, label="attachment_context")
        validate_one("finalizer", manifest.finalizer, label="finalizer")
        validate_one("tool_registry", manifest.tool_registry, label="tool_registry")
        for mode, ref in manifest.providers.items():
            validate_one("provider", ref, label=f"provider:{mode}")

        return {
            "ok": not errors,
            "manifest": manifest.to_dict(),
            "resolved": resolved,
            "errors": errors,
        }

    def probe_manifest_contracts(self, manifest: ActiveModuleManifest) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def probe_one(kind: str, ref: str, *, label: str) -> None:
            entry: dict[str, Any] = {
                "label": label,
                "kind": kind,
                "requested_ref": ref,
                "ok": False,
            }
            try:
                module, reference = self._loader.load(ref, expected_kind=kind)
                self._validate_module_instance(module, kind=kind, ref=reference.ref)
                entry.update(
                    {
                        "ok": True,
                        "resolved_ref": reference.ref,
                        "module_id": reference.module_id,
                        "version": reference.version,
                        "capabilities": list(reference.capabilities),
                    }
                )
                checker = getattr(module, "self_check", None)
                if callable(checker):
                    raw = checker()
                    if isinstance(raw, dict):
                        entry["self_check"] = dict(raw)
            except Exception as exc:
                entry["error"] = str(exc)
            checks.append(entry)

        probe_one("router", manifest.router, label="router")
        probe_one("policy", manifest.policy, label="policy")
        probe_one("attachment_context", manifest.attachment_context, label="attachment_context")
        probe_one("finalizer", manifest.finalizer, label="finalizer")
        probe_one("tool_registry", manifest.tool_registry, label="tool_registry")
        for mode, ref in manifest.providers.items():
            probe_one("provider", ref, label=f"provider:{mode}")

        return {
            "ok": all(bool(item.get("ok")) for item in checks),
            "manifest": manifest.to_dict(),
            "checks": checks,
        }

    def validate_active_manifest(self) -> dict[str, Any]:
        return self.validate_manifest(self.load_active_manifest())

    def validate_shadow_manifest(self) -> dict[str, Any]:
        return self.validate_manifest(self.load_shadow_manifest())

    def shadow_promote_check(self) -> dict[str, Any]:
        return self.promote_check(self.load_shadow_manifest())

    def _manifest_ref_for_label(self, manifest: ActiveModuleManifest, label: str) -> str:
        raw = str(label or "").strip()
        if raw.startswith("provider:"):
            mode = raw.split(":", 1)[1].strip()
            return str(manifest.providers.get(mode) or "").strip()
        return str(getattr(manifest, raw, "") or "").strip()

    def _manifest_kind_for_label(self, label: str) -> str:
        raw = str(label or "").strip()
        if raw.startswith("provider:"):
            return "provider"
        if raw in {"router", "policy", "attachment_context", "finalizer", "tool_registry"}:
            return raw if raw != "policy" else "policy"
        return ""

    def _expected_capabilities_for_kind(self, kind: str) -> tuple[str, ...]:
        mapping = {
            "router": ("route",),
            "policy": ("normalize_route",),
            "attachment_context": ("resolve_attachment_context", "route_state_scope"),
            "finalizer": ("sanitize",),
            "tool_registry": ("build_langchain_tools", "describe_tools"),
            "provider": ("build_runner",),
        }
        return mapping.get(str(kind or "").strip(), ())

    def promote_check(self, manifest: ActiveModuleManifest) -> dict[str, Any]:
        unsafe_refs: dict[str, str] = {}
        compatibility_errors: list[dict[str, Any]] = []
        resolved: dict[str, str] = {}

        def collect(label: str, ref: str) -> None:
            ref_text = str(ref or "").strip()
            if ref_text.startswith("path:"):
                unsafe_refs[label] = ref_text
                return
            kind = self._manifest_kind_for_label(label)
            if not kind or not ref_text:
                return
            try:
                reference = self._loader.resolve_ref(ref_text, expected_kind=kind)
                module_manifest = read_module_manifest(reference.path / "manifest.toml")
                resolved[label] = reference.ref
                if str(module_manifest.kind or "").strip() != kind:
                    compatibility_errors.append(
                        {
                            "label": label,
                            "category": "kind_mismatch",
                            "expected": kind,
                            "actual": str(module_manifest.kind or ""),
                        }
                    )
                if str(module_manifest.api_version or "").strip() != "1":
                    compatibility_errors.append(
                        {
                            "label": label,
                            "category": "api_version_incompatible",
                            "expected": "1",
                            "actual": str(module_manifest.api_version or ""),
                        }
                    )
                missing_capabilities = [
                    capability
                    for capability in self._expected_capabilities_for_kind(kind)
                    if capability not in set(module_manifest.capabilities)
                ]
                if missing_capabilities:
                    compatibility_errors.append(
                        {
                            "label": label,
                            "category": "capability_missing",
                            "missing": missing_capabilities,
                            "actual_capabilities": list(module_manifest.capabilities),
                        }
                    )
                runtime_profile = str(module_manifest.runtime_profile or "").strip()
                if runtime_profile and runtime_profile not in RUNTIME_PROFILES:
                    compatibility_errors.append(
                        {
                            "label": label,
                            "category": "runtime_profile_invalid",
                            "runtime_profile": runtime_profile,
                        }
                    )
                code_version = read_python_module_version(reference.path)
                if code_version and code_version != str(module_manifest.version or "").strip():
                    compatibility_errors.append(
                        {
                            "label": label,
                            "category": "module_version_mismatch",
                            "manifest_version": str(module_manifest.version or ""),
                            "code_version": code_version,
                        }
                    )
                for dependency in module_manifest.depends_on:
                    dep_text = str(dependency or "").strip()
                    if not dep_text or "=" not in dep_text:
                        compatibility_errors.append(
                            {
                                "label": label,
                                "category": "invalid_dependency_entry",
                                "dependency": dep_text,
                            }
                        )
                        continue
                    dep_label, dep_ref = dep_text.split("=", 1)
                    dep_label = dep_label.strip()
                    dep_ref = dep_ref.strip()
                    current_ref = self._manifest_ref_for_label(manifest, dep_label)
                    if current_ref != dep_ref:
                        compatibility_errors.append(
                            {
                                "label": label,
                                "category": "dependency_mismatch",
                                "dependency_label": dep_label,
                                "expected_ref": dep_ref,
                                "actual_ref": current_ref,
                            }
                        )
            except Exception as exc:
                compatibility_errors.append(
                    {
                        "label": label,
                        "category": "module_resolve_failed",
                        "error": str(exc),
                    }
                )

        collect("router", manifest.router)
        collect("policy", manifest.policy)
        collect("attachment_context", manifest.attachment_context)
        collect("finalizer", manifest.finalizer)
        collect("tool_registry", manifest.tool_registry)
        for mode, ref in manifest.providers.items():
            collect(f"provider:{mode}", ref)

        if unsafe_refs:
            return {
                "ok": False,
                "reason": "path_ref_not_promotable",
                "unsafe_refs": unsafe_refs,
                "compatibility_errors": compatibility_errors,
                "resolved": resolved,
            }
        if compatibility_errors:
            first_category = str((compatibility_errors[0] or {}).get("category") or "compatibility_error")
            return {
                "ok": False,
                "reason": first_category,
                "unsafe_refs": {},
                "compatibility_errors": compatibility_errors,
                "resolved": resolved,
            }
        return {
            "ok": True,
            "reason": "",
            "unsafe_refs": {},
            "compatibility_errors": [],
            "resolved": resolved,
        }

    def promote_shadow_manifest(self) -> dict[str, Any]:
        shadow_manifest = self.load_shadow_manifest()
        validation = self.validate_manifest(shadow_manifest)
        if not validation["ok"]:
            return {"ok": False, "validation": validation}
        promote_check = self.promote_check(shadow_manifest)
        if not promote_check["ok"]:
            return {
                "ok": False,
                "reason": str(promote_check.get("reason") or "shadow_manifest_not_promotable"),
                "unsafe_refs": dict(promote_check.get("unsafe_refs") or {}),
                "validation": validation,
                "promote_check": promote_check,
            }

        current_active = self.load_active_manifest()
        rollback_payload = {
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "previous_active_manifest": current_active.to_dict(),
            "promoted_shadow_manifest": shadow_manifest.to_dict(),
        }
        self._write_json(self._config.rollback_pointer_path, rollback_payload)
        write_active_manifest(self._config.active_manifest_path, shadow_manifest)
        return {
            "ok": True,
            "validation": validation,
            "promote_check": promote_check,
            "active_manifest": shadow_manifest.to_dict(),
            "rollback_pointer": rollback_payload,
        }

    def rollback_active_manifest(self) -> dict[str, Any]:
        rollback_payload = self.read_rollback_pointer()
        previous_manifest = rollback_payload.get("previous_active_manifest")
        if not isinstance(previous_manifest, dict) or not previous_manifest:
            return {"ok": False, "reason": "rollback_pointer_missing"}

        manifest = active_manifest_from_dict(previous_manifest)
        validation = self.validate_manifest(manifest)
        if not validation["ok"]:
            return {"ok": False, "reason": "rollback_manifest_invalid", "validation": validation}

        write_active_manifest(self._config.active_manifest_path, manifest)
        return {
            "ok": True,
            "validation": validation,
            "active_manifest": manifest.to_dict(),
            "rollback_pointer": rollback_payload,
        }

    def _health_key(self, kind: str, mode: str | None = None) -> str:
        kind_text = str(kind or "").strip().lower()
        mode_text = str(mode or "").strip().lower()
        return f"{kind_text}:{mode_text}" if mode_text else kind_text

    def record_runtime_failure(
        self,
        *,
        kind: str,
        requested_ref: str,
        fallback_ref: str = "",
        error: str,
        mode: str | None = None,
    ) -> None:
        health = self.load_module_health()
        key = self._health_key(kind, mode=mode)
        current = dict(health.get(key) or {})
        failure_count = int(current.get("failure_count") or 0) + 1
        use_fallback = bool(fallback_ref and fallback_ref != requested_ref)
        current.update(
            {
                "status": "fallback" if use_fallback else "degraded",
                "failure_count": failure_count,
                "last_error": str(error or "")[:500],
                "last_failure_at": datetime.now(timezone.utc).isoformat(),
                "requested_ref": requested_ref,
                "selected_ref": fallback_ref if use_fallback else requested_ref,
                "fallback_ref": fallback_ref,
            }
        )
        health[key] = current
        self.save_module_health(health)

    def record_runtime_success(self, *, kind: str, selected_ref: str, mode: str | None = None) -> None:
        health = self.load_module_health()
        key = self._health_key(kind, mode=mode)
        current = dict(health.get(key) or {})
        current.update(
            {
                "status": "active",
                "failure_count": 0,
                "last_error": "",
                "selected_ref": selected_ref,
            }
        )
        health[key] = current
        self.save_module_health(health)

    def _resolve_kind(
        self,
        *,
        kind: str,
        requested_ref: str,
        fallback_ref: str,
    ) -> tuple[Any, ModuleSelection]:
        health = self.load_module_health().get(self._health_key(kind), {})
        preferred_ref = requested_ref
        if str(health.get("status") or "") == "fallback" and fallback_ref:
            preferred_ref = fallback_ref
        try:
            module, ref = self._loader.load(preferred_ref, expected_kind=kind)
            return module, ModuleSelection(
                kind=kind,
                requested_ref=requested_ref,
                resolved_ref=ref.ref,
                fallback_ref=fallback_ref,
                used_fallback=preferred_ref != requested_ref,
            )
        except Exception as exc:
            if fallback_ref and fallback_ref != preferred_ref:
                module, ref = self._loader.load(fallback_ref, expected_kind=kind)
                self.record_runtime_failure(kind=kind, requested_ref=requested_ref, fallback_ref=fallback_ref, error=str(exc))
                return module, ModuleSelection(
                    kind=kind,
                    requested_ref=requested_ref,
                    resolved_ref=ref.ref,
                    fallback_ref=fallback_ref,
                    used_fallback=True,
                )
            raise

    def load_registry(self) -> KernelModuleRegistry:
        active_manifest = self.load_active_manifest()
        default_manifest = active_manifest_from_dict(DEFAULT_ACTIVE_MANIFEST)

        router, router_selection = self._resolve_kind(
            kind="router",
            requested_ref=active_manifest.router,
            fallback_ref=default_manifest.router,
        )
        policy, policy_selection = self._resolve_kind(
            kind="policy",
            requested_ref=active_manifest.policy,
            fallback_ref=default_manifest.policy,
        )
        attachment_context, attachment_selection = self._resolve_kind(
            kind="attachment_context",
            requested_ref=active_manifest.attachment_context,
            fallback_ref=default_manifest.attachment_context,
        )
        finalizer, finalizer_selection = self._resolve_kind(
            kind="finalizer",
            requested_ref=active_manifest.finalizer,
            fallback_ref=default_manifest.finalizer,
        )
        tool_registry, tool_selection = self._resolve_kind(
            kind="tool_registry",
            requested_ref=active_manifest.tool_registry,
            fallback_ref=default_manifest.tool_registry,
        )

        providers: dict[str, Any] = {}
        selected_refs = {
            "router": router_selection.resolved_ref,
            "policy": policy_selection.resolved_ref,
            "attachment_context": attachment_selection.resolved_ref,
            "finalizer": finalizer_selection.resolved_ref,
            "tool_registry": tool_selection.resolved_ref,
        }
        for mode, requested_ref in active_manifest.providers.items():
            fallback_ref = default_manifest.providers.get(mode, requested_ref)
            provider, selection = self._resolve_kind(kind="provider", requested_ref=requested_ref, fallback_ref=fallback_ref)
            providers[str(mode)] = provider
            selected_refs[f"provider:{mode}"] = selection.resolved_ref

        return KernelModuleRegistry(
            router=router,
            policy=policy,
            attachment_context=attachment_context,
            finalizer=finalizer,
            tool_registry=tool_registry,
            providers=providers,
            selected_refs=selected_refs,
            active_manifest=active_manifest.to_dict(),
            module_health=self.load_module_health(),
        )

    def health_snapshot(self, registry: KernelModuleRegistry) -> ModuleHealthSnapshot:
        return ModuleHealthSnapshot(
            active_manifest=dict(registry.active_manifest),
            selected_modules=dict(registry.selected_refs),
            module_health=self.load_module_health(),
            runtime_files={
                "runtime_dir": str(self._config.runtime_dir),
                "active_manifest_path": str(self._config.active_manifest_path),
                "shadow_manifest_path": str(self._config.shadow_manifest_path),
                "rollback_pointer_path": str(self._config.rollback_pointer_path),
                "module_health_path": str(self._config.module_health_path),
                "last_shadow_run_path": str(self._config.runtime_dir / "last_shadow_run.json"),
                "last_upgrade_run_path": str(self._config.runtime_dir / "last_upgrade_run.json"),
                "last_repair_run_path": str(self._config.runtime_dir / "last_repair_run.json"),
                "last_patch_worker_run_path": str(self._config.runtime_dir / "last_patch_worker_run.json"),
                "last_package_run_path": str(self._config.runtime_dir / "last_package_run.json"),
                "shadow_runs_dir": str(self._config.runtime_dir / "shadow_runs"),
                "upgrade_runs_dir": str(self._config.runtime_dir / "upgrade_runs"),
                "repair_runs_dir": str(self._config.runtime_dir / "repair_runs"),
                "repair_workspaces_dir": str(self._config.runtime_dir / "repair_workspaces"),
                "patch_worker_runs_dir": str(self._config.runtime_dir / "patch_worker_runs"),
                "package_runs_dir": str(self._config.runtime_dir / "package_runs"),
            },
        )
