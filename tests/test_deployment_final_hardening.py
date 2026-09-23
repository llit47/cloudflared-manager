"""Frozen PR #5 deployment transaction regressions."""

from pathlib import Path
from functools import partial

import pytest

from cloudflared_manager.deployment import cli
from cloudflared_manager.deployment.configurator import Configurator
from cloudflared_manager.deployment.environment import atomic_write_environment, initial_environment
from cloudflared_manager.deployment.errors import (
    HealthCheckError,
    RollbackError,
    TransactionFailedError,
)
from cloudflared_manager.deployment.installer import InstallResult, Installer
from cloudflared_manager.deployment.reconciliation import DeploymentReconciler
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import ManagerSettings
from cloudflared_manager.deployment.updater import Updater
from tests.deployment_support import (
    FakePreparationRunner,
    FakeService,
    fake_readiness,
    make_paths,
    make_source,
)

A_SHA = "a" * 40
B_SHA = "b" * 40
fake_readiness = partial(fake_readiness, release_id=A_SHA)


def installed(tmp_path: Path) -> tuple[ReleaseFilesystem, Path]:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())
    filesystem.ensure_layout()
    release = filesystem.prepare_release(
        make_source(tmp_path / "a", unit=b"A unit\n"), A_SHA, Path("/usr/bin/python3")
    )
    filesystem.switch_current(A_SHA)
    filesystem.install_unit(release)
    filesystem.install_runtime_tmpfiles(release)
    filesystem.install_stable_administration(release)
    atomic_write_environment(
        paths.environment_file, initial_environment("192.168.1.20", 8000), owner=None
    )
    return filesystem, release


def test_first_install_rejects_http_health_when_managed_service_is_inactive(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=FakePreparationRunner())

    class FailedManagedStart(FakeService):
        def start(self) -> None:
            self.calls.append("start")  # Another process can still answer HTTP.

    service = FailedManagedStart(active=False)
    with pytest.raises(TransactionFailedError):
        Installer(paths, filesystem, service, fake_readiness, lambda: None,
                  environment_owner=None).install(
            make_source(tmp_path / "candidate"), B_SHA, Path("/usr/bin/python3"),
            "192.168.1.20", 8000,
        )
    assert not paths.current.exists()
    assert service.active is False


def test_update_rejects_http_health_when_candidate_service_is_inactive(
    tmp_path: Path,
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths

    class FailedCandidateRestart(FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.restarts = 0

        def restart(self) -> None:
            self.calls.append("restart")
            self.restarts += 1
            self.active = self.restarts != 1

    service = FailedCandidateRestart()
    with pytest.raises(TransactionFailedError):
        Updater(paths, filesystem, service, fake_readiness).update(
            make_source(tmp_path / "b", unit=b"B unit\n"), B_SHA, Path("/usr/bin/python3")
        )
    assert filesystem.read_current_sha() == A_SHA
    assert service.active is True


def test_config_rejects_http_health_when_candidate_service_is_inactive(
    tmp_path: Path,
) -> None:
    filesystem, _ = installed(tmp_path)

    class FailedCandidateRestart(FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.restarts = 0

        def restart(self) -> None:
            self.calls.append("restart")
            self.restarts += 1
            self.active = self.restarts != 1

    service = FailedCandidateRestart()
    service.release_id = A_SHA
    with pytest.raises(TransactionFailedError):
        Configurator(filesystem.paths, service, fake_readiness,
                     environment_owner=None).apply({"CFM_BIND_PORT": "9000"})
    assert service.active is True


def test_reconciler_recovery_rejects_http_health_when_service_stays_inactive(
    tmp_path: Path,
) -> None:
    filesystem, release = installed(tmp_path)

    class FailedManagedStart(FakeService):
        def start(self) -> None:
            self.calls.append("start")

    service = FailedManagedStart(active=False)
    settings = ManagerSettings("192.168.1.20", 8000, True)
    with pytest.raises(TransactionFailedError):
        DeploymentReconciler(filesystem, service, fake_readiness).reconcile(
            release, settings
        )
    assert service.active is False
    assert service.calls[-1] == "stop"


@pytest.mark.parametrize("transaction", ["update", "config"])
def test_rollback_cannot_claim_health_when_http_succeeds_but_service_is_inactive(
    tmp_path: Path, transaction: str
) -> None:
    filesystem, _ = installed(tmp_path)

    class FailedRollbackRestart(FakeService):
        def restart(self) -> None:
            self.calls.append("restart")
            self.active = False

    service = FailedRollbackRestart()
    service.release_id = A_SHA
    calls = 0

    def http_health(host: str, port: int) -> None:
        nonlocal calls
        calls += 1
        if (transaction == "config" and calls == 1) or (
            transaction == "update" and filesystem.read_current_sha() == B_SHA
        ):
            raise HealthCheckError("candidate failed")
        return fake_readiness(host, port)

    with pytest.raises(RollbackError):
        if transaction == "update":
            Updater(filesystem.paths, filesystem, service, http_health).update(
                make_source(tmp_path / "b", unit=b"B unit\n"), B_SHA,
                Path("/usr/bin/python3"),
            )
        else:
            Configurator(filesystem.paths, service, http_health,
                         environment_owner=None).apply({"CFM_BIND_PORT": "9000"})
    assert service.active is False


def test_reconciler_rollback_to_prior_inactive_state_does_not_require_active_health(
    tmp_path: Path,
) -> None:
    filesystem, release = installed(tmp_path)
    service = FakeService(active=False)
    checks = 0

    def http_health(host: str, port: int) -> None:
        nonlocal checks
        checks += 1
        raise HealthCheckError("recovered service unhealthy")

    with pytest.raises(TransactionFailedError):
        DeploymentReconciler(filesystem, service, http_health).reconcile(
            release, ManagerSettings("192.168.1.20", 8000, True)
        )
    assert service.active is False
    assert service.calls[-1] == "stop"
    assert checks == 1


@pytest.mark.parametrize("stale_reloaded", [False, True])
def test_update_reconciles_current_unit_before_candidate_rollback_baseline(
    tmp_path: Path, stale_reloaded: bool
) -> None:
    filesystem, _ = installed(tmp_path)
    paths = filesystem.paths
    source = make_source(tmp_path / "b", unit=b"B unit\n")
    candidate = filesystem.prepare_release(source, B_SHA, Path("/usr/bin/python3"))
    filesystem.install_unit(candidate)  # Interrupted before current switched.

    class LoadedUnitService(FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.loaded_unit = b"B unit\n" if stale_reloaded else b"A unit\n"

        def daemon_reload(self) -> None:
            super().daemon_reload()
            self.loaded_unit = paths.unit_path.read_bytes()

    service = LoadedUnitService()
    candidate_checks = 0

    def http_health(host: str, port: int) -> None:
        nonlocal candidate_checks
        if filesystem.read_current_sha() == B_SHA:
            candidate_checks += 1
            raise HealthCheckError("B candidate failed")
        return fake_readiness(host, port)

    with pytest.raises(TransactionFailedError):
        Updater(paths, filesystem, service, http_health).update(
            source, B_SHA, Path("/usr/bin/python3")
        )
    assert candidate_checks == 1
    assert filesystem.read_current_sha() == A_SHA
    assert paths.unit_path.read_bytes() == b"A unit\n"
    assert service.loaded_unit == b"A unit\n"
    assert service.active is True


def test_install_summary_respects_disabled_persisted_runtime_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = make_paths(tmp_path)
    paths.current.parent.mkdir(parents=True)
    paths.current.write_text("fake existing deployment", encoding="ascii")
    discovery_calls: list[bool] = []
    settings = ManagerSettings("192.168.1.20", 8000, False)

    class FakeInstaller:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def reconcile(self) -> InstallResult:
            return InstallResult(A_SHA, settings, False)

    class FakeLock:
        def __init__(self, path: Path) -> None:
            pass

        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(cli, "_require_root", lambda: None)
    monkeypatch.setattr(cli, "DeploymentPaths", lambda: paths)
    monkeypatch.setattr(cli, "DeploymentLock", FakeLock)
    monkeypatch.setattr(cli, "ActivationRecoveryBarrier", lambda paths: type(
        "CleanGate", (), {"require_clean": lambda self: None}
    )())
    monkeypatch.setattr(cli, "ReleaseFilesystem", lambda paths: object())
    monkeypatch.setattr(cli, "SystemdManager", lambda: object())
    monkeypatch.setattr(cli, "Installer", FakeInstaller)
    monkeypatch.setattr(cli, "discover_cloudflared", lambda enabled: discovery_calls.append(enabled))
    monkeypatch.setattr(cli, "_print_install_summary", lambda *args: None)

    assert cli.install_from_source(tmp_path, A_SHA, Path("/usr/bin/python3")) == 0
    assert discovery_calls == [False]
