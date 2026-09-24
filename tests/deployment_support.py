from __future__ import annotations

import os
import shlex
from pathlib import Path

from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.health import DeploymentReadiness
from cloudflared_manager.runtime_identity import runtime_config_id


def fake_readiness(
    host: str,
    port: int,
    *,
    discovery: bool = True,
    cloudflared_config_path: Path | None = None,
    pid: int = 1234,
    release_id: str = "3" * 40,
) -> DeploymentReadiness:
    return DeploymentReadiness(
        pid,
        runtime_config_id(host, port, discovery, cloudflared_config_path),
        release_id,
    )


def make_paths(root: Path) -> DeploymentPaths:
    tmpfiles_executable = root / "usr" / "bin" / "systemd-tmpfiles"
    tmpfiles_executable.parent.mkdir(parents=True, exist_ok=True)
    runtime_root = root / "run" / "cloudflared-manager"
    tmpfiles_executable.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = --create ] || exit 1\n"
        f"mkdir -p -- {shlex.quote(str(runtime_root))}\n"
        f"chmod 0700 -- {shlex.quote(str(runtime_root))}\n",
        encoding="utf-8",
    )
    tmpfiles_executable.chmod(0o755)
    return DeploymentPaths(
        install_root=root / "opt" / "cloudflared-manager",
        config_root=root / "etc" / "cloudflared-manager",
        unit_path=root / "etc" / "systemd" / "system" / "cloudflared-manager.service",
        update_link=root / "usr" / "local" / "sbin" / "cfm-update",
        config_link=root / "usr" / "local" / "sbin" / "cfm-config",
        runtime_root=runtime_root,
        sudoers_path=root / "etc" / "sudoers.d" / "cloudflared-manager-bridge",
        tmpfiles_path=root / "etc" / "tmpfiles.d" / "cloudflared-manager.conf",
        tmpfiles_executable=tmpfiles_executable,
    )


def make_source(
    root: Path,
    *,
    unit: bytes = b"[Service]\nVersion=fixture\n",
    administration_version: str = "candidate",
) -> Path:
    source = root / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "src" / "cloudflared_manager").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (source / "deploy" / "cloudflared-manager.service").write_bytes(unit)
    (source / "deploy" / "cloudflared-manager.tmpfiles.conf").write_text(
        "d /run/cloudflared-manager 0700 root root -\n", encoding="ascii",
    )
    (source / "deploy" / "update.sh").write_text(
        f"#!/bin/bash\n# {administration_version} update\n",
        encoding="utf-8",
    )
    (source / "deploy" / "config.sh").write_text(
        f"#!/bin/bash\n# {administration_version} config\n",
        encoding="utf-8",
    )
    (source / "src" / "cloudflared_manager" / "__init__.py").write_text("", encoding="utf-8")
    return source


class FakePreparationRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, arguments: list[str], timeout: float) -> int:
        self.calls.append((arguments, timeout))
        if arguments[1:4] == ["-I", "-m", "venv"]:
            venv = Path(arguments[4])
            (venv / "bin").mkdir(parents=True)
            python = venv / "bin" / "python"
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
        if len(arguments) > 4 and arguments[1:4] == ["-I", "-m", "pip"]:
            venv = Path(arguments[0]).parents[1]
            executable = venv / "bin" / "cloudflared-manager"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        return 0


class FakeService:
    def __init__(self, *, active: bool = True, enabled: bool = True) -> None:
        self.calls: list[str] = []
        self.states = ("loaded", "active", "running")
        self.active = active
        self.enabled = enabled
        self.main_pid = 1234 if active else 0
        self.needs_daemon_reload = False

    def daemon_reload(self) -> None:
        self.calls.append("daemon-reload")
        self.needs_daemon_reload = False

    def enable(self) -> None:
        self.calls.append("enable")
        self.enabled = True

    def disable(self) -> None:
        self.calls.append("disable")
        self.enabled = False

    def start(self) -> None:
        self.calls.append("start")
        self.active = True
        self.main_pid = 1234

    def stop(self) -> None:
        self.calls.append("stop")
        self.active = False
        self.main_pid = 0

    def restart(self) -> None:
        self.calls.append("restart")
        self.active = True
        self.main_pid = 1234

    def runtime_state(self):
        from cloudflared_manager.deployment.service import ManagerRuntimeState
        return ManagerRuntimeState(self.active, self.main_pid, self.needs_daemon_reload)

    def running_release_id(self, install_root: Path) -> str:
        return getattr(self, "release_id", "3" * 40)

    def is_active(self) -> bool:
        self.calls.append("is-active")
        return self.active

    def is_enabled(self) -> bool:
        self.calls.append("is-enabled")
        return self.enabled

    def sanitized_status(self):
        self.calls.append("status")
        return self.states
