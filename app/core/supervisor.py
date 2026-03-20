from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.core.module_loader import ModuleLoader
from app.core.module_manifest import DEFAULT_ACTIVE_MANIFEST, ActiveModuleManifest, active_manifest_from_dict, read_active_manifest, write_active_manifest
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

    def promote_shadow_manifest(self) -> dict[str, Any]:
        shadow_manifest = self.load_shadow_manifest()
        validation = self.validate_manifest(shadow_manifest)
        if not validation["ok"]:
            return {"ok": False, "validation": validation}

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
                "shadow_runs_dir": str(self._config.runtime_dir / "shadow_runs"),
                "upgrade_runs_dir": str(self._config.runtime_dir / "upgrade_runs"),
                "repair_runs_dir": str(self._config.runtime_dir / "repair_runs"),
                "repair_workspaces_dir": str(self._config.runtime_dir / "repair_workspaces"),
            },
        )
