from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from pathlib import Path


def _sanitize_segment(raw: str, limit: int = 40) -> str:
    text = "".join(ch if ch.isalnum() else "-" for ch in str(raw or "").strip().lower())
    text = text.strip("-") or "x"
    if len(text) <= limit:
        return text
    return text[:limit]


class DockerSandboxManager:
    def __init__(
        self,
        workspace_root: Path,
        allowed_roots: list[Path],
        image: str,
        network: str,
        memory: str,
        cpus: str,
        pids_limit: int,
        container_prefix: str,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.allowed_roots = [p.resolve() for p in allowed_roots]
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = max(16, int(pids_limit))
        self.container_prefix = _sanitize_segment(container_prefix, 24)
        self._lock = threading.Lock()
        self._last_docker_check_ts = 0.0
        self._last_docker_ok = False
        self._mounts: list[tuple[Path, str]] = self._build_mounts()

    def _build_mounts(self) -> list[tuple[Path, str]]:
        seen: set[str] = set()
        mounts: list[tuple[Path, str]] = []
        for idx, root in enumerate([self.workspace_root, *self.allowed_roots]):
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            if root == self.workspace_root:
                mounts.append((root, "/workspace"))
            else:
                name = _sanitize_segment(root.name or f"root{idx}", 20)
                mounts.append((root, f"/allowed/{idx}-{name}"))
        return mounts

    def _docker_ok(self) -> bool:
        now = time.monotonic()
        if now - self._last_docker_check_ts < 5.0:
            return self._last_docker_ok
        self._last_docker_check_ts = now
        try:
            proc = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            self._last_docker_ok = proc.returncode == 0
        except Exception:
            self._last_docker_ok = False
        return self._last_docker_ok

    def docker_available(self) -> bool:
        return self._docker_ok()

    def _container_name(self, session_id: str) -> str:
        sid = _sanitize_segment(session_id or "anon", 30)
        ws_hash = hashlib.sha1(str(self.workspace_root).encode("utf-8")).hexdigest()[:8]
        return f"{self.container_prefix}-{ws_hash}-{sid}"

    def _path_to_container(self, host_path: Path) -> str:
        target = host_path.resolve()
        for root, mount_point in self._mounts:
            if target == root or root in target.parents:
                rel = target.relative_to(root)
                out = Path(mount_point) / rel
                # Docker path must use POSIX separators.
                return "/" + str(out).lstrip("/").replace("\\", "/")
        raise ValueError(f"Path is not mounted in docker sandbox: {target}")

    def _run(self, argv: list[str], timeout_sec: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
            check=False,
        )

    def _ensure_container(self, session_id: str) -> str:
        if not self._docker_ok():
            raise RuntimeError("Docker is not available. Please start Docker Desktop and retry.")

        name = self._container_name(session_id)
        with self._lock:
            inspect = self._run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout_sec=5)
            if inspect.returncode == 0:
                if inspect.stdout.strip().lower() != "true":
                    started = self._run(["docker", "start", name], timeout_sec=10)
                    if started.returncode != 0:
                        raise RuntimeError(f"Failed to start sandbox container: {started.stderr.strip() or started.stdout.strip()}")
                return name

            cmd: list[str] = [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--workdir",
                "/workspace",
                "--pids-limit",
                str(self.pids_limit),
                "--cpus",
                self.cpus,
                "--memory",
                self.memory,
            ]
            if self.network:
                cmd.extend(["--network", self.network])
            cmd.extend(["--label", "officetool.sandbox=1"])
            cmd.extend(["--label", f"officetool.workspace={self.workspace_root}"])
            for host_root, mount_point in self._mounts:
                cmd.extend(["-v", f"{host_root}:{mount_point}"])

            cmd.extend(
                [
                    self.image,
                    "sh",
                    "-lc",
                    "while true; do sleep 3600; done",
                ]
            )
            created = self._run(cmd, timeout_sec=30)
            if created.returncode != 0:
                raise RuntimeError(f"Failed to create sandbox container: {created.stderr.strip() or created.stdout.strip()}")
            return name

    def run_in_sandbox(
        self,
        *,
        session_id: str,
        argv: list[str],
        cwd: Path,
        timeout_sec: int,
    ) -> subprocess.CompletedProcess[str]:
        if not argv:
            raise RuntimeError("Empty command")
        container = self._ensure_container(session_id)
        container_cwd = self._path_to_container(cwd)
        exec_argv = ["docker", "exec", "-w", container_cwd, container, *argv]
        return self._run(exec_argv, timeout_sec=timeout_sec)
