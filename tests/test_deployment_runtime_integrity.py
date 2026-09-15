"""Frozen PR #5 runtime-integrity regressions; all infrastructure is fake."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloudflared_manager.config import Settings
from cloudflared_manager.deployment.configurator import Configurator
from cloudflared_manager.deployment.environment import (
    atomic_write_environment,
    initial_environment,
)
from cloudflared_manager.deployment.errors import (
    HealthCheckError,
    HostOperationError,
    TransactionFailedError,
    UpdateLockedError,
    RollbackError,
)
from cloudflared_manager.deployment.health import verify_managed_health
from cloudflared_manager.deployment.health import DeploymentReadiness
from cloudflared_manager.deployment.reconciliation import DeploymentReconciler
from cloudflared_manager.deployment.release import DeploymentLock, ReleaseFilesystem
from cloudflared_manager.deployment.service import CommandOutput, SystemdManager
from cloudflared_manager.deployment.settings import ManagerSettings
from tests.deployment_support import FakePreparationRunner, FakeService, make_paths, make_source

SHA = "a" * 40
OTHER_SHA = "b" * 40


def config_id(host: str = "192.168.1.20", port: int = 8000, discovery: bool = True) -> str:
    encoded = json.dumps([host, port, discovery], separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RuntimeService(FakeService):
    def __init__(self, *, active: bool = True, needs_reload: bool = False) -> None:
        super().__init__(active=active)
        self.main_pid = 1234 if active else 0
        self.needs_reload = needs_reload
        self.next_pid: int | None = None

    def runtime_state(self):
        self.calls.append("runtime-state")
        pid = self.main_pid
        if self.next_pid is not None:
            self.main_pid, self.next_pid = self.next_pid, None
        return SimpleNamespace(active=self.active, main_pid=pid,
                               needs_daemon_reload=self.needs_reload)

    def daemon_reload(self) -> None:
        super().daemon_reload()
        self.needs_reload = False

    def start(self) -> None:
        super().start()
        self.main_pid = 1234

    def restart(self) -> None:
        super().restart()
        self.main_pid = 1234

    def stop(self) -> None:
        super().stop()
        self.main_pid = 0


def readiness(pid: int = 1234, identity: str | None = None) -> DeploymentReadiness:
    return DeploymentReadiness(pid, identity or config_id())


def installed(tmp_path: Path) -> tuple[ReleaseFilesystem, Path]:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    filesystem.ensure_layout()
    release = filesystem.prepare_release(
        make_source(tmp_path / "release", unit=b"A unit\n"), SHA,
        Path("/usr/bin/python3"),
    )
    filesystem.switch_current(SHA)
    filesystem.install_unit(release)
    filesystem.install_stable_administration(release)
    atomic_write_environment(
        paths.environment_file, initial_environment("192.168.1.20", 8000), owner=None
    )
    return filesystem, release


def test_application_has_separate_pid_and_config_bound_deployment_readiness() -> None:
    from cloudflared_manager.main import create_app
    from tests.test_app import get_from_app

    response = get_from_app(create_app(Settings(mode="test")), "/deployment-readiness")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready", "app": "cloudflared-manager", "pid": os.getpid(),
        "config_id": config_id("127.0.0.1", 8000, False),
    }


@pytest.mark.parametrize(
    "responder_pid,responder_id,changed_main_pid",
    [
        (4321, config_id(), None),
        (1234, "f" * 64, None),
        (1234, config_id(), 9999),
    ],
)
def test_managed_verifier_rejects_wrong_responder_or_runtime_identity(
    responder_pid: int, responder_id: str, changed_main_pid: int | None
) -> None:
    service = RuntimeService()
    service.next_pid = changed_main_pid
    with pytest.raises(HealthCheckError):
        verify_managed_health(
            service, lambda host, port: readiness(responder_pid, responder_id),
            "192.168.1.20", 8000, config_id(),
        )


def test_managed_verifier_accepts_stable_pid_and_matching_config() -> None:
    service = RuntimeService()
    verify_managed_health(
        service, lambda host, port: readiness(), "192.168.1.20", 8000, config_id()
    )
    assert service.calls == ["runtime-state", "runtime-state"]


def test_systemd_runtime_observation_fails_closed_on_malformed_main_pid(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SystemdManager(executable="/usr/bin/systemctl")
    monkeypatch.setattr(
        manager, "_run", lambda args: CommandOutput(
            0, "ActiveState=active\nMainPID=1x\nNeedDaemonReload=no\n"
        ),
    )
    with pytest.raises(HostOperationError):
        manager.runtime_state()


@pytest.mark.parametrize("change", ["port", "discovery"])
def test_repeated_config_command_reapplies_unapplied_persisted_value(
    tmp_path: Path, change: str
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    if change == "port":
        updates = {"CFM_BIND_PORT": "8081"}
        desired_id = config_id(port=8081)
    else:
        updates = {"CFM_RUNTIME_DISCOVERY_ENABLED": "false"}
        desired_id = config_id(discovery=False)
    document = initial_environment("192.168.1.20", 8000).updated(updates)
    atomic_write_environment(paths.environment_file, document, owner=None)
    service = RuntimeService()
    runtime_id = config_id()  # Process still has the previous configuration.

    def observe(host: str, port: int) -> dict[str, object]:
        return readiness(identity=desired_id if "restart" in service.calls else runtime_id)

    result = Configurator(paths, service, observe, environment_owner=None).apply(updates)
    assert result.changed is True
    assert service.calls.count("restart") == 1
    assert paths.environment_file.read_bytes() == document.render().encode()


def test_applied_same_value_config_is_a_true_noop(tmp_path: Path) -> None:
    filesystem, _ = installed(tmp_path)
    service = RuntimeService()
    result = Configurator(
        filesystem.paths, service, lambda host, port: readiness(), environment_owner=None
    ).apply({"CFM_BIND_PORT": "8000"})
    assert result.changed is False
    assert "restart" not in service.calls


@pytest.mark.parametrize("rollback_identity", ["pid", "config"])
def test_config_rollback_requires_previous_runtime_identity(
    tmp_path: Path, rollback_identity: str
) -> None:
    filesystem, _ = installed(tmp_path)
    service = RuntimeService()
    calls = 0

    def observe(host: str, port: int) -> DeploymentReadiness:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HealthCheckError("candidate failed")
        if rollback_identity == "pid":
            return readiness(pid=9999)
        return readiness(identity="f" * 64)

    with pytest.raises(RollbackError):
        Configurator(filesystem.paths, service, observe, environment_owner=None).apply(
            {"CFM_BIND_PORT": "9000"}
        )


@pytest.mark.parametrize("needs_reload", [False, True])
def test_matching_unit_bytes_follow_systemd_need_daemon_reload(
    tmp_path: Path, needs_reload: bool
) -> None:
    filesystem, release = installed(tmp_path)
    service = RuntimeService(needs_reload=needs_reload)
    result = DeploymentReconciler(
        filesystem, service, lambda host, port: readiness()
    ).reconcile(release, ManagerSettings("192.168.1.20", 8000, True))
    assert result.changed is needs_reload
    assert ("daemon-reload" in service.calls) is needs_reload
    assert ("restart" in service.calls) is needs_reload


@pytest.mark.parametrize("late_failure", ["enable", "administration"])
@pytest.mark.parametrize("previously_active", [False, True])
def test_verified_current_unit_repair_is_not_undone_by_later_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    late_failure: str, previously_active: bool
) -> None:
    filesystem, release = installed(tmp_path)
    paths = filesystem.paths
    other = filesystem.prepare_release(
        make_source(tmp_path / "other", unit=b"stale B unit\n"),
        OTHER_SHA, Path("/usr/bin/python3"),
    )
    filesystem.install_unit(other)
    service = RuntimeService(active=previously_active)
    if late_failure == "enable":
        service.enabled = False
        monkeypatch.setattr(service, "enable", lambda: (_ for _ in ()).throw(
            HostOperationError("synthetic enable failure")
        ))
    else:
        monkeypatch.setattr(
            filesystem, "install_stable_administration",
            lambda release: (_ for _ in ()).throw(
                HostOperationError("synthetic stable-admin failure")
            ),
        )
    with pytest.raises(TransactionFailedError):
        DeploymentReconciler(
            filesystem, service, lambda host, port: readiness()
        ).reconcile(release, ManagerSettings("192.168.1.20", 8000, True))
    assert paths.unit_path.read_bytes() == b"A unit\n"
    assert service.active is previously_active


def test_private_lock_path_and_directory_mode(tmp_path: Path) -> None:
    from cloudflared_manager.deployment.paths import DeploymentPaths

    assert DeploymentPaths().lock_path == Path("/run/cloudflared-manager/update.lock")
    path = tmp_path / "run" / "cloudflared-manager" / "update.lock"
    with DeploymentLock(path, owner=None):
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("collision", ["symlink", "file", "world-writable"])
def test_private_lock_refuses_unsafe_existing_runtime_directory(
    tmp_path: Path, collision: str
) -> None:
    parent = tmp_path / "run" / "cloudflared-manager"
    parent.parent.mkdir()
    if collision == "symlink":
        parent.symlink_to(tmp_path)
    elif collision == "file":
        parent.write_text("unrelated", encoding="ascii")
    else:
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
    with pytest.raises(UpdateLockedError):
        with DeploymentLock(parent / "update.lock", owner=None):
            pass


@pytest.mark.parametrize("collision", ["symlink", "directory"])
def test_private_lock_refuses_unsafe_existing_lock_inode(
    tmp_path: Path, collision: str
) -> None:
    parent = tmp_path / "run" / "cloudflared-manager"
    parent.mkdir(parents=True, mode=0o700)
    lock = parent / "update.lock"
    if collision == "symlink":
        lock.symlink_to(tmp_path / "elsewhere")
    else:
        lock.mkdir()
    with pytest.raises(UpdateLockedError):
        with DeploymentLock(lock, owner=None):
            pass
