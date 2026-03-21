from __future__ import annotations

from dataclasses import replace
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.core.bootstrap import build_kernel_runtime
from app.core.module_code import read_python_module_version, sync_python_module_version
from app.core.module_manifest import read_module_manifest, write_module_manifest


def _runtime_config(agent: Any, runtime_dir: Path, *, modules_dir: Path | None = None):
    kwargs = {
        "runtime_dir": runtime_dir,
        "active_manifest_path": runtime_dir / "active_manifest.json",
        "shadow_manifest_path": runtime_dir / "shadow_manifest.json",
        "rollback_pointer_path": runtime_dir / "rollback_pointer.json",
        "module_health_path": runtime_dir / "module_health.json",
    }
    if modules_dir is not None:
        kwargs["modules_dir"] = modules_dir
    return replace(agent.config, **kwargs)


def debug_kernel_shadow_stage_and_smoke(agent: Any, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-smoke-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        stage = runtime.stage_shadow_manifest(overrides={"router": str(target_router_ref)})
        smoke = runtime.run_shadow_smoke(user_message="给我今天的新闻", validate_provider=False)
        return {"stage": stage, "smoke": smoke}


def debug_kernel_shadow_upgrade_flow(agent: Any, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        shadow = runtime.load_shadow_manifest()
        shadow.router = str(target_router_ref or shadow.router)
        runtime.write_shadow_manifest(shadow)
        validation = runtime.validate_shadow_manifest()
        promotion = runtime.promote_shadow_manifest()
        active_after = runtime.supervisor.load_active_manifest().to_dict()
        rollback = runtime.rollback_active_manifest()
        active_restored = runtime.supervisor.load_active_manifest().to_dict()
        return {
            "validation": validation,
            "promotion": promotion,
            "rollback": rollback,
            "active_after": active_after,
            "active_restored": active_restored,
        }


def debug_kernel_shadow_validation_rejects_broken_manifest(
    agent: Any,
    broken_router_ref: str = "router_rules@999.0.0",
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-bad-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        initial_active = runtime.supervisor.load_active_manifest().to_dict()
        shadow = runtime.load_shadow_manifest()
        shadow.router = str(broken_router_ref or shadow.router)
        runtime.write_shadow_manifest(shadow)
        validation = runtime.validate_shadow_manifest()
        promotion = runtime.promote_shadow_manifest()
        active_after_attempt = runtime.supervisor.load_active_manifest().to_dict()
        return {
            "initial_active": initial_active,
            "validation": validation,
            "promotion": promotion,
            "active_after_attempt": active_after_attempt,
        }


def debug_kernel_shadow_replay(agent: Any, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-replay-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        runtime.stage_shadow_manifest(overrides={"router": str(target_router_ref)})
        replay_record = {
            "run_id": "synthetic-replay",
            "session_id": "synthetic-session",
            "message": "把数据整理成表格",
            "settings": {"enable_tools": True, "response_style": "short"},
            "summary_before": "",
            "history_turns_before": [],
            "attachment_metas": [],
            "route_state_input": {},
        }
        return {"replay": runtime.run_shadow_replay(replay_record=replay_record)}


def debug_kernel_shadow_contracts(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-contracts-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        return {"contracts": runtime.run_shadow_contracts()}


def debug_kernel_active_contracts(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-active-contracts-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        return {"contracts": runtime.run_active_contracts()}


def debug_kernel_shadow_pipeline(agent: Any, target_router_ref: str = "router_rules@2.0.0") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-pipeline-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        pipeline = runtime.run_shadow_pipeline(
            overrides={"router": str(target_router_ref)},
            smoke_message="给我今天的新闻",
            validate_provider=False,
            replay_record={
                "run_id": "synthetic-pipeline",
                "session_id": "synthetic-session",
                "message": "给我今天的新闻",
                "settings": {"enable_tools": True, "response_style": "short"},
                "summary_before": "",
                "history_turns_before": [],
                "attachment_metas": [],
                "route_state_input": {},
            },
            promote_if_healthy=True,
        )
        rollback = runtime.rollback_active_manifest()
        return {
            "pipeline": pipeline,
            "stage": dict(pipeline.get("stage") or {}),
            "validation": dict(pipeline.get("validation") or {}),
            "smoke": dict(pipeline.get("smoke") or {}),
            "replay": dict(pipeline.get("replay") or {}),
            "promotion": dict(pipeline.get("promotion") or {}),
            "rollback": rollback,
            "last_upgrade_run": runtime.read_last_upgrade_run(),
            "upgrade_runs": runtime.list_upgrade_runs(limit=5),
        }


def debug_kernel_shadow_pipeline_classifies_broken_manifest(
    agent: Any,
    broken_router_ref: str = "router_rules@999.0.0",
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-pipeline-bad-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        pipeline = runtime.run_shadow_pipeline(
            overrides={"router": str(broken_router_ref)},
            smoke_message="给我今天的新闻",
            validate_provider=False,
            replay_record=None,
            promote_if_healthy=False,
        )
        return {
            "pipeline": pipeline,
            "last_upgrade_run": runtime.read_last_upgrade_run(),
            "upgrade_runs": runtime.list_upgrade_runs(limit=5),
        }


def debug_kernel_shadow_auto_repair_broken_manifest(
    agent: Any,
    broken_router_ref: str = "router_rules@999.0.0",
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-repair-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        broken_pipeline = runtime.run_shadow_pipeline(
            overrides={"router": str(broken_router_ref)},
            smoke_message="给我今天的新闻",
            validate_provider=False,
            replay_record=None,
            promote_if_healthy=False,
        )
        repair = runtime.run_shadow_auto_repair(
            base_upgrade_run=broken_pipeline,
            replay_record=None,
            smoke_message="给我今天的新闻",
            validate_provider=False,
            promote_if_healthy=False,
            max_attempts=2,
        )
        return {
            "broken_pipeline": broken_pipeline,
            "repair": repair,
            "last_repair_run": runtime.read_last_repair_run(),
            "repair_runs": runtime.list_repair_runs(limit=5),
            "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
        }


def debug_kernel_shadow_promote_rejects_path_refs(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-promote-path-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        cfg = _runtime_config(agent, runtime_dir)
        runtime = build_kernel_runtime(cfg)
        source_dir = cfg.modules_dir / "router_rules" / "v1"
        path_module_dir = runtime_dir / "shadow_router_path"
        shutil.copytree(source_dir, path_module_dir)
        stage = runtime.stage_shadow_manifest(overrides={"router": f"path:{path_module_dir}"})
        promote_check = runtime.shadow_promote_check()
        promotion = runtime.promote_shadow_manifest()
        return {
            "stage": stage,
            "promote_check": promote_check,
            "promotion": promotion,
            "shadow_manifest": runtime.load_shadow_manifest().to_dict(),
            "active_manifest": runtime.supervisor.load_active_manifest().to_dict(),
        }


def debug_kernel_shadow_patch_worker_persists_missing_tasks(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-patch-empty-") as tmp_dir:
        runtime_dir = Path(tmp_dir).resolve()
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir))
        patch_worker = runtime.run_shadow_patch_worker(
            repair_run={"run_id": "synthetic-repair", "repair_tasks": []},
            replay_record=None,
            max_tasks=1,
            max_rounds=3,
            promote_if_healthy=False,
        )
        return {
            "patch_worker": patch_worker,
            "last_patch_worker_run": runtime.read_last_patch_worker_run(),
            "patch_worker_runs": runtime.list_patch_worker_runs(limit=5),
        }


def debug_kernel_shadow_package_path_router(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-package-") as tmp_dir:
        root = Path(tmp_dir).resolve()
        runtime_dir = root / "runtime"
        modules_dir = root / "modules"
        shutil.copytree(agent.config.modules_dir, modules_dir)
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir, modules_dir=modules_dir))
        source_router_dir = modules_dir / "router_rules" / "v1"
        stage = runtime.stage_shadow_manifest(overrides={"router": f"path:{source_router_dir}"})
        package_run = runtime.package_shadow_modules(
            labels=["router"],
            package_note="debug package path router",
            source_run_id="synthetic-upgrade",
            patch_worker_run_id="synthetic-patch",
            runtime_profile="patch_worker",
        )
        return {
            "stage": stage,
            "package_run": package_run,
            "last_package_run": runtime.read_last_package_run(),
            "package_runs": runtime.list_package_runs(limit=5),
            "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
        }


def debug_kernel_shadow_package_syncs_module_version(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-package-version-") as tmp_dir:
        root = Path(tmp_dir).resolve()
        runtime_dir = root / "runtime"
        modules_dir = root / "modules"
        shutil.copytree(agent.config.modules_dir, modules_dir)
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir, modules_dir=modules_dir))
        source_router_dir = modules_dir / "router_rules" / "v1"
        runtime.stage_shadow_manifest(overrides={"router": f"path:{source_router_dir}"})
        package_run = runtime.package_shadow_modules(labels=["router"], runtime_profile="patch_worker")
        packaged = ((package_run.get("packaged_modules") or [{}])[0] or {})
        packaged_ref = str(packaged.get("packaged_ref") or "")
        module_id, version = packaged_ref.split("@", 1)
        packaged_dir = modules_dir / module_id / f"v{version.split('.', 1)[0]}"
        packaged_manifest = read_module_manifest(packaged_dir / "manifest.toml")
        code_version = read_python_module_version(packaged_dir)
        return {
            "package_run": package_run,
            "packaged_ref": packaged_ref,
            "manifest_version": packaged_manifest.version,
            "code_version": code_version,
            "versions_match": packaged_manifest.version == code_version,
            "code_version_sync": packaged.get("code_version_sync") or {},
        }


def debug_kernel_shadow_promote_rejects_module_version_mismatch(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-version-mismatch-") as tmp_dir:
        root = Path(tmp_dir).resolve()
        runtime_dir = root / "runtime"
        modules_dir = root / "modules"
        shutil.copytree(agent.config.modules_dir, modules_dir)
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir, modules_dir=modules_dir))
        source_router_dir = modules_dir / "router_rules" / "v1"
        runtime.stage_shadow_manifest(overrides={"router": f"path:{source_router_dir}"})
        package_run = runtime.package_shadow_modules(labels=["router"], runtime_profile="patch_worker")
        packaged_ref = str(((package_run.get("packaged_modules") or [{}])[0] or {}).get("packaged_ref") or "")
        module_id, version = packaged_ref.split("@", 1)
        packaged_dir = modules_dir / module_id / f"v{version.split('.', 1)[0]}"
        sync_python_module_version(packaged_dir, "0.0.1")
        runtime.stage_shadow_manifest(overrides={"router": packaged_ref})
        return {
            "package_run": package_run,
            "promote_check": runtime.shadow_promote_check(),
            "promotion": runtime.promote_shadow_manifest(),
            "code_version": read_python_module_version(packaged_dir),
            "manifest_version": read_module_manifest(packaged_dir / "manifest.toml").version,
        }


def debug_kernel_shadow_promote_rejects_dependency_mismatch(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-shadow-dependency-") as tmp_dir:
        root = Path(tmp_dir).resolve()
        runtime_dir = root / "runtime"
        modules_dir = root / "modules"
        shutil.copytree(agent.config.modules_dir, modules_dir)
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir, modules_dir=modules_dir))
        source_router_dir = modules_dir / "router_rules" / "v1"
        runtime.stage_shadow_manifest(overrides={"router": f"path:{source_router_dir}"})
        package_run = runtime.package_shadow_modules(labels=["router"], runtime_profile="patch_worker")
        packaged_ref = str(((package_run.get("packaged_modules") or [{}])[0] or {}).get("packaged_ref") or "")
        packaged_manifest_path = modules_dir / "router_rules" / "v3" / "manifest.toml"
        packaged_manifest = read_module_manifest(packaged_manifest_path)
        broken_manifest = type(packaged_manifest)(
            id=packaged_manifest.id,
            version=packaged_manifest.version,
            api_version=packaged_manifest.api_version,
            kind=packaged_manifest.kind,
            entrypoint=packaged_manifest.entrypoint,
            capabilities=packaged_manifest.capabilities,
            depends_on=("policy=policy_resolver@999.0.0",),
            runtime_profile=packaged_manifest.runtime_profile,
            source_ref=packaged_manifest.source_ref,
            packaged_at=packaged_manifest.packaged_at,
            path=packaged_manifest.path,
        )
        write_module_manifest(packaged_manifest_path, broken_manifest)
        runtime.stage_shadow_manifest(overrides={"router": packaged_ref})
        return {
            "package_run": package_run,
            "promote_check": runtime.shadow_promote_check(),
            "promotion": runtime.promote_shadow_manifest(),
            "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
        }


def debug_kernel_shadow_self_upgrade_flow(agent: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="officetool-kernel-self-upgrade-") as tmp_dir:
        root = Path(tmp_dir).resolve()
        runtime_dir = root / "runtime"
        modules_dir = root / "modules"
        shutil.copytree(agent.config.modules_dir, modules_dir)
        runtime = build_kernel_runtime(_runtime_config(agent, runtime_dir, modules_dir=modules_dir))
        source_router_dir = modules_dir / "router_rules" / "v1"
        base_pipeline = runtime.run_shadow_pipeline(
            overrides={"router": f"path:{source_router_dir}"},
            smoke_message="给我今天的新闻",
            validate_provider=False,
            replay_record=None,
            promote_if_healthy=True,
        )
        self_upgrade = runtime.run_shadow_self_upgrade(
            base_upgrade_run=base_pipeline,
            replay_record=None,
            smoke_message="给我今天的新闻",
            validate_provider=False,
            max_attempts=1,
            max_tasks=1,
            max_rounds=2,
            promote_if_healthy=True,
        )
        return {
            "base_pipeline": base_pipeline,
            "self_upgrade": self_upgrade,
            "active_manifest_after": runtime.supervisor.load_active_manifest().to_dict(),
            "shadow_manifest_after": runtime.load_shadow_manifest().to_dict(),
            "last_package_run": runtime.read_last_package_run(),
            "last_patch_worker_run": runtime.read_last_patch_worker_run(),
        }
