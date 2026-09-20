"""Frozen PR #5 runtime-integrity regressions; all infrastructure is fake."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloudflared_manager.config import Settings
from cloudflared_manager.deployment import cli
from cloudflared_manager.deployment.configurator import Configurator
from cloudflared_manager.deployment.environment import (
    atomic_write_environment,
    initial_environment,
)
from cloudflared_manager.deployment.errors import (
    EnvironmentFileError,
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
        self.release_id = SHA
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
    return DeploymentReadiness(pid, identity or config_id(), SHA)


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
        "release_id": None,
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
            "192.168.1.20", 8000, config_id(), SHA,
        )


def test_managed_verifier_accepts_stable_pid_and_matching_config() -> None:
    service = RuntimeService()
    verify_managed_health(
        service, lambda host, port: readiness(), "192.168.1.20", 8000, config_id(), SHA
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


def test_systemd_running_release_uses_stable_process_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SystemdManager(executable="/usr/bin/systemctl")
    observations = 0

    def runtime(_arguments: tuple[str, ...]) -> CommandOutput:
        nonlocal observations
        observations += 1
        return CommandOutput(
            0,
            "ActiveState=active\nMainPID=1234\nNeedDaemonReload=no\n",
        )

    monkeypatch.setattr(manager, "_run", runtime)
    monkeypatch.setattr(
        "cloudflared_manager.deployment.service.os.readlink",
        lambda path: "/opt/cloudflared-manager/releases/" + SHA,
    )

    assert manager.running_release_id(Path("/opt/cloudflared-manager")) == SHA
    assert observations == 2


@pytest.mark.parametrize("change", ["port", "host", "discovery"])
def test_repeated_config_command_reapplies_unapplied_persisted_value(
    tmp_path: Path, change: str
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    if change == "port":
        updates = {"CFM_BIND_PORT": "8081"}
        desired_id = config_id(port=8081)
        expected_host = "192.168.1.20"
        expected_port = 8081
    elif change == "host":
        updates = {"CFM_BIND_HOST": "192.168.1.21"}
        desired_id = config_id(host="192.168.1.21")
        expected_host = "192.168.1.21"
        expected_port = 8000
    else:
        updates = {"CFM_RUNTIME_DISCOVERY_ENABLED": "false"}
        desired_id = config_id(discovery=False)
        expected_host = "192.168.1.20"
        expected_port = 8000
    document = initial_environment("192.168.1.20", 8000).updated(updates)
    atomic_write_environment(paths.environment_file, document, owner=None)
    service = RuntimeService()
    runtime_id = config_id()  # Process still has the previous configuration.

    observations: list[tuple[str, int]] = []

    def observe(host: str, port: int) -> dict[str, object]:
        observations.append((host, port))
        if change in {"port", "host"} and "restart" not in service.calls:
            raise HealthCheckError("persisted bind is not active yet")
        return readiness(identity=desired_id if "restart" in service.calls else runtime_id)

    result = Configurator(paths, service, observe, environment_owner=None).apply(updates)
    assert result.changed is True
    assert service.calls.count("restart") == 1
    assert observations == [
        (expected_host, expected_port),
        (expected_host, expected_port),
    ]
    assert paths.environment_file.read_bytes() == document.render().encode()


def test_corrective_restart_still_rejects_wrong_running_release(tmp_path: Path) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    document = initial_environment("192.168.1.20", 8000).updated(
        {"CFM_BIND_PORT": "8081"}
    )
    atomic_write_environment(paths.environment_file, document, owner=None)
    service = RuntimeService()

    def observe(host: str, port: int) -> DeploymentReadiness:
        if "restart" not in service.calls:
            raise HealthCheckError("persisted bind is not active yet")
        return DeploymentReadiness(service.main_pid, config_id(port=8081), OTHER_SHA)

    with pytest.raises(RollbackError):
        Configurator(paths, service, observe, environment_owner=None).apply(
            {"CFM_BIND_PORT": "8081"}
        )

    assert service.calls.count("restart") == 2
    assert paths.environment_file.read_bytes() == document.render().encode()


class RecordingLock:
    def __init__(self, path: Path, entered: list[bool]) -> None:
        self.entered = entered

    def __enter__(self) -> "RecordingLock":
        self.entered.append(True)
        return self

    def __exit__(self, *_: object) -> None:
        self.entered.append(False)


def test_stale_configurator_process_is_rejected_inside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    previous = paths.environment_file.read_bytes()
    service = RuntimeService()
    entered: list[bool] = []

    class LockedFilesystem:
        def __init__(self, actual_paths: object) -> None:
            assert entered == [True]

        def read_current_sha(self) -> str:
            assert entered == [True]
            return SHA

    monkeypatch.setattr(cli, "DeploymentLock", lambda path: RecordingLock(path, entered))
    monkeypatch.setattr(cli, "ReleaseFilesystem", LockedFilesystem)
    monkeypatch.setattr(cli, "PROCESS_RELEASE_ID", OTHER_SHA)

    with pytest.raises(HostOperationError, match="does not match"):
        cli._apply_config(
            paths,
            Configurator(paths, service, readiness, environment_owner=None),
            {"CFM_BIND_PORT": "8081"},
        )

    assert entered == [True, False]
    assert paths.environment_file.read_bytes() == previous
    assert service.calls == []


def test_stale_updater_process_is_rejected_inside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    entered: list[bool] = []
    update_calls: list[bool] = []
    resolved_main: list[str] = []

    class LockedFilesystem:
        def __init__(self, actual_paths: object) -> None:
            assert actual_paths == paths

        def read_current_sha(self) -> str:
            assert entered == [True]
            return SHA

    class RecordingUpdater:
        def __init__(self, *args: object) -> None:
            pass

        def update(self, *args: object) -> object:
            update_calls.append(True)
            raise AssertionError("stale updater must fail before update")

    def unexpected_resolve(curl: str) -> str:
        resolved_main.append(curl)
        raise AssertionError("stale updater must fail before resolving main")

    monkeypatch.setattr(cli, "_require_root", lambda: None)
    monkeypatch.setattr(cli, "DeploymentPaths", lambda: paths)
    monkeypatch.setattr(cli, "ReleaseFilesystem", LockedFilesystem)
    monkeypatch.setattr(cli, "SystemdManager", lambda: RuntimeService())
    monkeypatch.setattr(cli, "Updater", RecordingUpdater)
    monkeypatch.setattr(cli, "DeploymentLock", lambda path: RecordingLock(path, entered))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(cli, "PROCESS_RELEASE_ID", OTHER_SHA)
    monkeypatch.setattr(cli, "resolve_main_sha", unexpected_resolve)

    assert cli.update() == 1
    assert entered == [True, False]
    assert resolved_main == []
    assert update_calls == []


def test_configurator_rejects_unsafe_environment_before_preserving_content(
    tmp_path: Path,
) -> None:
    filesystem, _ = installed(tmp_path)
    path = filesystem.paths.environment_file
    tainted = path.read_bytes() + b"PYTHONPATH=/tmp/untrusted\n"
    path.write_bytes(tainted)
    path.chmod(0o666)
    service = RuntimeService()

    with pytest.raises(EnvironmentFileError, match="unsafe ownership or permissions"):
        Configurator(
            filesystem.paths,
            service,
            readiness,
            environment_owner=None,
        ).apply({"CFM_BIND_PORT": "8081"})

    assert path.read_bytes() == tainted
    assert path.stat().st_mode & 0o777 == 0o666
    assert service.calls == []


@pytest.mark.parametrize(("port", "changed"), [(8000, False), (8081, True)])
def test_current_configurator_process_uses_normal_configuration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int, changed: bool
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    service = RuntimeService()
    entered: list[bool] = []

    monkeypatch.setattr(cli, "DeploymentLock", lambda path: RecordingLock(path, entered))
    monkeypatch.setattr(cli, "ReleaseFilesystem", lambda actual_paths: filesystem)
    monkeypatch.setattr(cli, "PROCESS_RELEASE_ID", SHA)

    result = cli._apply_config(
        paths,
        Configurator(
            paths,
            service,
            lambda host, request_port: DeploymentReadiness(
                service.main_pid, config_id(port=request_port), SHA
            ),
            environment_owner=None,
        ),
        {"CFM_BIND_PORT": str(port)},
    )

    assert result.changed is changed
    assert entered == [True, False]
    assert service.calls.count("restart") == int(changed)


def test_applied_same_value_config_is_a_true_noop(tmp_path: Path) -> None:
    filesystem, _ = installed(tmp_path)
    service = RuntimeService()
    result = Configurator(
        filesystem.paths, service, lambda host, port: readiness(), environment_owner=None
    ).apply({"CFM_BIND_PORT": "8000"})
    assert result.changed is False
    assert "restart" not in service.calls


@pytest.mark.parametrize("corrective_failure", ["restart", "verification"])
@pytest.mark.parametrize("recovery", ["healthy", "restart-failure", "wrong-pid", "wrong-config"])
def test_unchanged_config_restart_failure_recovers_only_persisted_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrective_failure: str,
    recovery: str,
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    document = initial_environment("192.168.1.20", 8081).updated(
        {"CFM_RUNTIME_DISCOVERY_ENABLED": "false"}
    )
    persisted = ("# preserve exactly\nFUTURE=opaque\n" + document.render()).encode()
    paths.environment_file.write_bytes(persisted)
    desired_id = config_id(port=8081, discovery=False)
    service = RuntimeService()
    events: list[str] = []
    writes: list[object] = []
    restart_count = 0
    original_restart = service.restart

    def reject_write(*args, **kwargs) -> None:
        writes.append(args)
        raise AssertionError("Unchanged configuration must never be written")

    def restart() -> None:
        nonlocal restart_count
        restart_count += 1
        events.append(f"restart-{restart_count}")
        assert paths.environment_file.read_bytes() == persisted
        original_restart()
        if (restart_count == 1 and corrective_failure == "restart") or (
            restart_count == 2 and recovery == "restart-failure"
        ):
            service.active = False
            service.main_pid = 0
            raise HostOperationError("Synthetic failure after service side effects")

    def observe(host: str, port: int) -> DeploymentReadiness:
        events.append(f"readiness-{restart_count}")
        assert (host, port) == ("192.168.1.20", 8081)
        assert paths.environment_file.read_bytes() == persisted
        if restart_count < 2:
            return readiness(identity=config_id())  # Old/unproven runtime A.
        if recovery == "wrong-pid":
            return readiness(pid=9999, identity=desired_id)
        if recovery == "wrong-config":
            return readiness(identity=config_id())
        return readiness(pid=service.main_pid, identity=desired_id)

    monkeypatch.setattr(service, "restart", restart)
    monkeypatch.setattr(
        "cloudflared_manager.deployment.configurator.atomic_write_environment",
        reject_write,
    )
    expected_error = TransactionFailedError if recovery == "healthy" else RollbackError
    expected_message = (
        "corrective operation failed; the persisted configuration is healthy again"
        if recovery == "healthy"
        else "healthy operation using the persisted configuration could not be re-established"
    )

    with pytest.raises(expected_error, match=expected_message):
        Configurator(paths, service, observe, environment_owner=None).apply(
            {"CFM_BIND_PORT": "8081"}
        )

    expected_events = ["readiness-0", "restart-1"]
    if corrective_failure == "verification":
        expected_events.append("readiness-1")
    expected_events.append("restart-2")
    if recovery != "restart-failure":
        expected_events.append("readiness-2")
    assert events == expected_events
    assert restart_count == 2
    assert writes == []
    assert paths.environment_file.read_bytes() == persisted
    if recovery == "healthy":
        assert service.active
        assert service.calls[-2:] == ["runtime-state", "runtime-state"]


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
