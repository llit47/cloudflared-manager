from pathlib import Path

import pytest

from cloudflared_manager.deployment.environment import (
    atomic_write_environment,
    initial_environment,
    read_environment,
)
from cloudflared_manager.deployment.errors import (
    HealthCheckError,
    HostOperationError,
    RollbackError,
    TransactionFailedError,
    ValidationError,
)
from cloudflared_manager.deployment.configurator import Configurator
from cloudflared_manager.deployment.installer import Installer
from cloudflared_manager.deployment.reconciliation import DeploymentReconciler
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import settings_from_document
from cloudflared_manager.deployment.updater import Updater
from tests.deployment_support import (
    FakePreparationRunner,
    FakeService,
    make_paths,
    make_source,
)

OLD_SHA = "3" * 40
NEW_SHA = "4" * 40


def _filesystem(tmp_path: Path):
    paths = make_paths(tmp_path)
    runner = FakePreparationRunner()
    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=runner)
    filesystem.ensure_layout()
    return paths, filesystem


def _installed(
    tmp_path: Path,
    *,
    unit: bytes = b"old unit\n",
    stable_administration: bool = True,
):
    paths, filesystem = _filesystem(tmp_path)
    source = make_source(
        tmp_path / "old",
        unit=unit,
        administration_version="old",
    )
    release = filesystem.prepare_release(source, OLD_SHA, Path("/usr/bin/python3"))
    filesystem.switch_current(OLD_SHA)
    filesystem.install_unit(release)
    if stable_administration:
        filesystem.install_stable_administration(release)
    atomic_write_environment(
        paths.environment_file,
        initial_environment("192.168.1.20", 8000),
        owner=None,
    )
    return paths, filesystem


def test_first_install_creates_environment_release_and_stable_commands(tmp_path: Path) -> None:
    paths, filesystem = _filesystem(tmp_path)
    source = make_source(tmp_path / "candidate")
    service = FakeService()
    identities: list[str] = []
    health: list[tuple[str, int]] = []
    installer = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: health.append((host, port)),
        lambda: identities.append("created"),
        environment_owner=None,
    )

    result = installer.install(
        source,
        NEW_SHA,
        Path("/usr/bin/python3"),
        "192.168.1.30",
        8000,
    )

    assert result.sha == NEW_SHA
    assert result.changed is True
    assert filesystem.read_current_sha() == NEW_SHA
    assert identities == ["created"]
    assert health == [("192.168.1.30", 8000)]
    assert service.calls == ["daemon-reload", "start", "is-active", "enable"]
    assert paths.stable_update.read_text().startswith("#!/bin/bash")
    assert paths.update_link.is_symlink()
    assert paths.stable_update.stat().st_mode & 0o777 == 0o755
    assert paths.stable_config.stat().st_mode & 0o777 == 0o755
    assert paths.unit_path.stat().st_mode & 0o777 == 0o644
    assert paths.config_root.stat().st_mode & 0o777 == 0o750
    document, _ = read_environment(paths.environment_file)
    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in document.render()


def test_first_install_collision_is_rejected_before_identity_creation(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.unit_path.parent.mkdir(parents=True)
    paths.unit_path.write_text("unrelated service\n", encoding="utf-8")
    identities: list[str] = []
    filesystem = ReleaseFilesystem(
        paths,
        owner=None,
        process_runner=FakePreparationRunner(),
    )

    with pytest.raises(HostOperationError, match="collides"):
        Installer(
            paths,
            filesystem,
            FakeService(),
            lambda host, port: None,
            lambda: identities.append("created"),
            environment_owner=None,
        ).install(
            make_source(tmp_path / "candidate"),
            NEW_SHA,
            Path("/usr/bin/python3"),
            "192.168.1.30",
            8000,
        )

    assert identities == []
    assert not paths.install_root.exists()
    assert paths.unit_path.read_text() == "unrelated service\n"


def test_first_install_recovers_unit_written_before_current_switch(tmp_path: Path) -> None:
    paths, filesystem = _filesystem(tmp_path)
    source = make_source(tmp_path / "candidate")
    release = filesystem.prepare_release(source, NEW_SHA, Path("/usr/bin/python3"))
    filesystem.install_unit(release)
    atomic_write_environment(
        paths.environment_file,
        initial_environment("192.168.1.30", 8000),
        owner=None,
    )
    service = FakeService(active=False, enabled=False)

    result = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: None,
        lambda: None,
        environment_owner=None,
    ).install(
        source,
        NEW_SHA,
        Path("/usr/bin/python3"),
        "192.168.1.30",
        8000,
    )

    assert result.sha == NEW_SHA
    assert filesystem.read_current_sha() == NEW_SHA
    assert service.calls == ["daemon-reload", "start", "is-active", "enable"]
    assert paths.stable_update.is_file()
    assert paths.stable_config.is_file()
    assert paths.update_link.is_symlink()
    assert paths.config_link.is_symlink()


def test_first_install_refuses_unrecognized_unit_in_marked_partial_layout(
    tmp_path: Path,
) -> None:
    paths, filesystem = _filesystem(tmp_path)
    source = make_source(tmp_path / "candidate")
    filesystem.prepare_release(source, NEW_SHA, Path("/usr/bin/python3"))
    paths.unit_path.parent.mkdir(parents=True, exist_ok=True)
    paths.unit_path.write_text("unrelated service\n", encoding="utf-8")
    identities: list[str] = []

    with pytest.raises(HostOperationError, match="collides"):
        Installer(
            paths,
            filesystem,
            FakeService(),
            lambda host, port: None,
            lambda: identities.append("created"),
            environment_owner=None,
        ).install(
            source,
            NEW_SHA,
            Path("/usr/bin/python3"),
            "192.168.1.30",
            8000,
        )

    assert identities == []
    assert not paths.current.exists()
    assert paths.unit_path.read_text() == "unrelated service\n"


def test_first_install_refuses_matching_partial_unit_without_ownership_marker(
    tmp_path: Path,
) -> None:
    paths, filesystem = _filesystem(tmp_path)
    source = make_source(tmp_path / "candidate")
    release = filesystem.prepare_release(source, NEW_SHA, Path("/usr/bin/python3"))
    filesystem.install_unit(release)
    (paths.install_root / ".cloudflared-manager-owned").unlink()
    identities: list[str] = []

    with pytest.raises(HostOperationError, match="not recognizable"):
        Installer(
            paths,
            filesystem,
            FakeService(),
            lambda host, port: None,
            lambda: identities.append("created"),
            environment_owner=None,
        ).install(
            source,
            NEW_SHA,
            Path("/usr/bin/python3"),
            "192.168.1.30",
            8000,
        )

    assert identities == []
    assert not paths.current.exists()
    assert paths.unit_path.read_bytes() == (
        release / "deploy" / "cloudflared-manager.service"
    ).read_bytes()


def test_installer_rerun_reconciles_complete_healthy_install_without_restart(
    tmp_path: Path,
) -> None:
    paths, filesystem = _installed(tmp_path)
    identities: list[str] = []
    health: list[tuple[str, int]] = []
    service = FakeService()

    installer = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: health.append((host, port)),
        lambda: identities.append("validated"),
        environment_owner=None,
    )

    result = installer.install(
        make_source(tmp_path / "new"),
        NEW_SHA,
        Path("/usr/bin/python3"),
        "192.168.1.30",
        8000,
    )

    assert result.sha == OLD_SHA
    assert result.changed is False
    assert identities == ["validated"]
    assert health == [("192.168.1.20", 8000)]
    assert service.calls == ["is-active", "is-enabled", "is-active"]
    assert filesystem.read_current_sha() == OLD_SHA


def test_installer_rerun_repairs_missing_stable_scripts(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    paths.stable_update.unlink()
    paths.stable_config.unlink()
    service = FakeService()

    Installer(
        paths,
        filesystem,
        service,
        lambda host, port: None,
        lambda: None,
        environment_owner=None,
    ).reconcile()

    assert "old update" in paths.stable_update.read_text()
    assert "old config" in paths.stable_config.read_text()
    assert service.calls == ["is-active", "is-enabled", "is-active"]


def test_installer_rerun_repairs_missing_command_links(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    paths.update_link.unlink()
    paths.config_link.unlink()

    Installer(
        paths,
        filesystem,
        FakeService(),
        lambda host, port: None,
        lambda: None,
        environment_owner=None,
    ).reconcile()

    assert paths.update_link.readlink() == paths.stable_update
    assert paths.config_link.readlink() == paths.stable_config


def test_installer_rerun_enables_manager_service_when_needed(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    service = FakeService(enabled=False)

    Installer(
        paths,
        filesystem,
        service,
        lambda host, port: None,
        lambda: None,
        environment_owner=None,
    ).reconcile()

    assert service.calls == ["is-active", "is-enabled", "is-active", "enable"]


def test_installer_rerun_repairs_missing_unit_and_starts_manager(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    paths.unit_path.unlink()
    service = FakeService(active=False)
    health: list[tuple[str, int]] = []

    Installer(
        paths,
        filesystem,
        service,
        lambda host, port: health.append((host, port)),
        lambda: None,
        environment_owner=None,
    ).reconcile()

    assert paths.unit_path.read_bytes() == b"old unit\n"
    assert service.calls == [
        "is-active", "is-enabled", "daemon-reload", "start", "is-active"
    ]
    assert health == [("192.168.1.20", 8000)]


def test_reconciliation_failed_changed_unit_restart_restores_prior_active_manager(
    tmp_path: Path,
) -> None:
    paths, filesystem = _installed(tmp_path)
    candidate = filesystem.prepare_release(
        make_source(tmp_path / "new", unit=b"new unit\n"),
        NEW_SHA,
        Path("/usr/bin/python3"),
    )
    filesystem.switch_current(NEW_SHA)

    class FailedFirstRestart(FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_restart = True

        def restart(self) -> None:
            self.calls.append("restart")
            if self.fail_next_restart:
                self.fail_next_restart = False
                self.active = False
                raise HostOperationError("replacement manager restart failed")
            self.active = True

    service = FailedFirstRestart()
    checked: list[tuple[str, int]] = []

    def health(host: str, port: int) -> None:
        assert paths.unit_path.read_bytes() == b"old unit\n"
        assert service.calls == [
            "is-active", "is-enabled", "daemon-reload", "restart",
            "daemon-reload", "restart",
        ]
        checked.append((host, port))

    settings = settings_from_document(read_environment(paths.environment_file)[0])
    with pytest.raises(TransactionFailedError, match="prior manager state was restored"):
        DeploymentReconciler(filesystem, service, health).reconcile(candidate, settings)

    assert paths.unit_path.read_bytes() == b"old unit\n"
    assert service.active is True
    assert checked == [("192.168.1.20", 8000)]


def test_reconciliation_failed_unhealthy_service_restart_restores_prior_active_state(
    tmp_path: Path,
) -> None:
    paths, filesystem = _installed(tmp_path)

    class FailedFirstRestart(FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_restart = True

        def restart(self) -> None:
            self.calls.append("restart")
            if self.fail_next_restart:
                self.fail_next_restart = False
                self.active = False
                raise HostOperationError("recovery restart failed")
            self.active = True

    service = FailedFirstRestart()
    health_calls = 0

    def health(host: str, port: int) -> None:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise HealthCheckError("manager is unhealthy")
        assert service.calls == ["is-active", "is-enabled", "restart", "restart"]
        assert service.active is True

    settings = settings_from_document(read_environment(paths.environment_file)[0])
    with pytest.raises(TransactionFailedError, match="prior manager state was restored"):
        DeploymentReconciler(filesystem, service, health).reconcile(
            paths.release(OLD_SHA), settings
        )

    assert health_calls == 2
    assert service.active is True
    assert paths.unit_path.read_bytes() == b"old unit\n"


def test_reconciliation_reports_rollback_failure_if_prior_active_state_cannot_return(
    tmp_path: Path,
) -> None:
    paths, filesystem = _installed(tmp_path)
    candidate = filesystem.prepare_release(
        make_source(tmp_path / "new", unit=b"new unit\n"),
        NEW_SHA,
        Path("/usr/bin/python3"),
    )
    filesystem.switch_current(NEW_SHA)

    class AlwaysFailedRestart(FakeService):
        def restart(self) -> None:
            self.calls.append("restart")
            self.active = False
            raise HostOperationError("manager restart failed")

    service = AlwaysFailedRestart()
    settings = settings_from_document(read_environment(paths.environment_file)[0])

    with pytest.raises(RollbackError, match="rollback was incomplete"):
        DeploymentReconciler(
            filesystem,
            service,
            lambda host, port: (_ for _ in ()).throw(
                AssertionError("unrestored manager must not be health-checked")
            ),
        ).reconcile(candidate, settings)

    assert paths.unit_path.read_bytes() == b"old unit\n"
    assert service.calls == [
        "is-active", "is-enabled", "daemon-reload", "restart",
        "daemon-reload", "restart",
    ]
    assert service.active is False


def test_reconciliation_failed_start_restores_prior_inactive_state(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)

    class FailedStart(FakeService):
        def start(self) -> None:
            self.calls.append("start")
            self.active = True
            raise HostOperationError("manager start failed after being attempted")

    service = FailedStart(active=False)
    settings = settings_from_document(read_environment(paths.environment_file)[0])

    def unexpected_health(host: str, port: int) -> None:
        raise AssertionError("failed start must not be health-checked")

    with pytest.raises(TransactionFailedError, match="prior manager state was restored"):
        DeploymentReconciler(
            filesystem,
            service,
            unexpected_health,
        ).reconcile(paths.release(OLD_SHA), settings)

    assert service.calls == ["is-active", "is-enabled", "start", "stop"]
    assert service.active is False


def test_reconciliation_restores_unit_when_install_mutates_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, filesystem = _installed(tmp_path)
    prior_unit = paths.unit_path.read_bytes()
    service = FakeService()
    settings = settings_from_document(read_environment(paths.environment_file)[0])

    def partially_install_unit(_release: Path) -> bool:
        paths.unit_path.write_bytes(b"candidate unit\n")
        raise HostOperationError("synthetic unit installation failure")

    monkeypatch.setattr(filesystem, "install_unit", partially_install_unit)

    with pytest.raises(TransactionFailedError, match="prior manager state was restored"):
        DeploymentReconciler(filesystem, service, lambda host, port: None).reconcile(
            paths.release(OLD_SHA), settings
        )

    assert paths.unit_path.read_bytes() == prior_unit
    assert service.calls == ["is-active", "is-enabled"]


def test_reconciliation_reports_failure_if_partial_unit_install_cannot_be_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, filesystem = _installed(tmp_path)
    service = FakeService()
    settings = settings_from_document(read_environment(paths.environment_file)[0])

    def partially_install_unit(_release: Path) -> bool:
        paths.unit_path.write_bytes(b"candidate unit\n")
        raise HostOperationError("synthetic unit installation failure")

    def fail_restoration(_path: Path, _snapshot: object) -> None:
        raise HostOperationError("synthetic snapshot restoration failure")

    monkeypatch.setattr(filesystem, "install_unit", partially_install_unit)
    monkeypatch.setattr(filesystem, "restore_snapshot", fail_restoration)

    with pytest.raises(RollbackError, match="rollback was incomplete"):
        DeploymentReconciler(filesystem, service, lambda host, port: None).reconcile(
            paths.release(OLD_SHA), settings
        )

    assert paths.unit_path.read_bytes() == b"candidate unit\n"
    assert service.calls == ["is-active", "is-enabled"]


def test_reconciliation_does_not_restore_unchanged_unit_after_later_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, filesystem = _installed(tmp_path)
    service = FakeService()
    settings = settings_from_document(read_environment(paths.environment_file)[0])
    prior_unit = paths.unit_path.read_bytes()
    restore_calls: list[Path] = []

    monkeypatch.setattr(filesystem, "install_unit", lambda release: False)
    monkeypatch.setattr(
        filesystem,
        "restore_snapshot",
        lambda path, snapshot: restore_calls.append(path),
    )

    def fail_administration(_release: Path) -> bool:
        raise HostOperationError("synthetic administration failure")

    monkeypatch.setattr(filesystem, "install_stable_administration", fail_administration)

    with pytest.raises(TransactionFailedError, match="prior manager state was restored"):
        DeploymentReconciler(filesystem, service, lambda host, port: None).reconcile(
            paths.release(OLD_SHA), settings
        )

    assert restore_calls == []
    assert paths.unit_path.read_bytes() == prior_unit
    assert service.calls == ["is-active", "is-enabled", "is-active"]


def test_installer_refuses_unowned_current_collision(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    (paths.install_root / ".cloudflared-manager-owned").unlink()
    service = FakeService()

    with pytest.raises(HostOperationError, match="not recognizable"):
        Installer(
            paths,
            filesystem,
            service,
            lambda host, port: None,
            lambda: None,
            environment_owner=None,
        ).reconcile()

    assert service.calls == []


def test_installer_refuses_unrecognized_stable_script_collision(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    paths.stable_update.write_text("#!/bin/sh\n# unrelated\n", encoding="utf-8")
    service = FakeService()

    with pytest.raises(HostOperationError, match="collides"):
        Installer(
            paths,
            filesystem,
            service,
            lambda host, port: None,
            lambda: None,
            environment_owner=None,
        ).reconcile()

    assert paths.stable_update.read_text() == "#!/bin/sh\n# unrelated\n"
    assert service.calls == []


def test_failed_first_install_health_rolls_back_manager_owned_state(tmp_path: Path) -> None:
    paths, filesystem = _filesystem(tmp_path)
    service = FakeService()
    installer = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: (_ for _ in ()).throw(HealthCheckError("failed")),
        lambda: None,
        environment_owner=None,
    )

    with pytest.raises(TransactionFailedError, match="rolled back"):
        installer.install(
            make_source(tmp_path / "candidate"),
            NEW_SHA,
            Path("/usr/bin/python3"),
            "192.168.1.30",
            8000,
        )

    assert not paths.current.exists()
    assert not paths.environment_file.exists()
    assert not paths.unit_path.exists()
    assert service.calls == ["daemon-reload", "start", "daemon-reload", "stop"]


def test_failed_first_install_start_attempt_restores_inactive_service_and_files(
    tmp_path: Path,
) -> None:
    paths, filesystem = _filesystem(tmp_path)
    previous_environment = (
        b"# retained before failed install\n"
        b"CFM_APP_NAME=cloudflared-manager\n"
        b"CFM_MODE=production\n"
        b"CFM_BIND_HOST=192.168.1.30\n"
        b"CFM_BIND_PORT=8000\n"
        b"CFM_RUNTIME_DISCOVERY_ENABLED=true\n"
    )
    paths.environment_file.write_bytes(previous_environment)

    class StartFailsAfterSideEffect(FakeService):
        def start(self) -> None:
            self.calls.append("start")
            self.active = True
            raise HostOperationError("manager start result was unavailable")

    service = StartFailsAfterSideEffect(active=False, enabled=False)
    installer = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: (_ for _ in ()).throw(
            AssertionError("failed start must not be health-checked")
        ),
        lambda: None,
        environment_owner=None,
    )

    with pytest.raises(TransactionFailedError, match="changes were rolled back"):
        installer.install(
            make_source(tmp_path / "candidate"),
            NEW_SHA,
            Path("/usr/bin/python3"),
            "192.168.1.30",
            8000,
        )

    assert not paths.current.exists()
    assert not paths.unit_path.exists()
    assert paths.environment_file.read_bytes() == previous_environment
    assert service.calls == ["daemon-reload", "start", "daemon-reload", "stop"]
    assert service.active is False


def test_failed_first_install_start_and_stop_reports_incomplete_rollback(
    tmp_path: Path,
) -> None:
    paths, filesystem = _filesystem(tmp_path)
    previous_environment = initial_environment("192.168.1.30", 8000).render().encode()
    paths.environment_file.write_bytes(previous_environment)

    class StartAndStopFailAfterSideEffects(FakeService):
        def start(self) -> None:
            self.calls.append("start")
            self.active = True
            raise HostOperationError("manager start result was unavailable")

        def stop(self) -> None:
            self.calls.append("stop")
            raise HostOperationError("manager stop failed")

    service = StartAndStopFailAfterSideEffects(active=False, enabled=False)
    installer = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: None,
        lambda: None,
        environment_owner=None,
    )

    with pytest.raises(RollbackError, match="rollback was incomplete"):
        installer.install(
            make_source(tmp_path / "candidate"),
            NEW_SHA,
            Path("/usr/bin/python3"),
            "192.168.1.30",
            8000,
        )

    assert not paths.current.exists()
    assert not paths.unit_path.exists()
    assert paths.environment_file.read_bytes() == previous_environment
    assert service.calls == ["daemon-reload", "start", "daemon-reload", "stop"]
    assert service.active is True


def test_first_install_preserves_existing_environment_comments_and_values(
    tmp_path: Path,
) -> None:
    paths, filesystem = _filesystem(tmp_path)
    existing = (
        "# retained operator note\n"
        "FUTURE_OPTION=retained\n"
        "CFM_APP_NAME=cloudflared-manager\n"
        "CFM_MODE=production\n"
        "CFM_BIND_HOST=10.0.0.9\n"
        "CFM_BIND_PORT=9000\n"
        "CFM_RUNTIME_DISCOVERY_ENABLED=false\n"
    )
    paths.environment_file.write_text(existing, encoding="utf-8")
    service = FakeService()
    installer = Installer(
        paths,
        filesystem,
        service,
        lambda host, port: None,
        lambda: None,
        environment_owner=None,
    )

    result = installer.install(
        make_source(tmp_path / "candidate"),
        NEW_SHA,
        Path("/usr/bin/python3"),
        "192.168.1.30",
        8000,
    )

    assert result.settings.bind_host == "10.0.0.9"
    assert result.settings.bind_port == 9000
    assert result.settings.runtime_discovery_enabled is False
    assert paths.environment_file.read_text() == existing


def test_update_same_sha_is_no_op(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    service = FakeService()
    checked: list[tuple[str, int]] = []

    result = Updater(
        paths,
        filesystem,
        service,
        lambda host, port: checked.append((host, port)),
    ).update(
        None,
        OLD_SHA,
        Path("/usr/bin/python3"),
    )

    assert result.changed is False
    assert service.calls == ["is-active", "is-enabled", "is-active"]
    assert checked == [("192.168.1.20", 8000)]


def test_update_same_sha_repairs_missing_stable_administration(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    paths.stable_update.unlink()
    paths.stable_config.unlink()
    paths.update_link.unlink()
    paths.config_link.unlink()
    service = FakeService()

    result = Updater(paths, filesystem, service, lambda host, port: None).update(
        None,
        OLD_SHA,
        Path("/usr/bin/python3"),
    )

    assert result.changed is True
    assert paths.stable_update.exists()
    assert paths.stable_config.exists()
    assert paths.update_link.is_symlink()
    assert paths.config_link.is_symlink()
    assert service.calls == ["is-active", "is-enabled", "is-active"]


def test_update_same_sha_replaces_stale_scripts_from_ready_retained_release(
    tmp_path: Path,
) -> None:
    paths, filesystem = _installed(tmp_path)
    new_release = filesystem.prepare_release(
        make_source(
            tmp_path / "new",
            unit=b"old unit\n",
            administration_version="new",
        ),
        NEW_SHA,
        Path("/usr/bin/python3"),
    )
    filesystem.switch_current(NEW_SHA)
    service = FakeService()

    result = Updater(paths, filesystem, service, lambda host, port: None).update(
        None,
        NEW_SHA,
        Path("/usr/bin/python3"),
    )

    assert result.changed is True
    assert paths.stable_update.read_bytes() == (new_release / "deploy" / "update.sh").read_bytes()
    assert paths.stable_config.read_bytes() == (new_release / "deploy" / "config.sh").read_bytes()
    assert service.calls == ["is-active", "is-enabled", "is-active"]


def test_successful_update_switches_release_after_preflight_and_preserves_config(
    tmp_path: Path,
) -> None:
    paths, filesystem = _installed(tmp_path)
    previous_environment = paths.environment_file.read_bytes()
    source = make_source(
        tmp_path / "new",
        unit=b"new unit\n",
        administration_version="new",
    )
    service = FakeService()

    def health(host: str, port: int) -> None:
        if filesystem.read_current_sha() == OLD_SHA:
            return
        assert filesystem.read_current_sha() == NEW_SHA
        assert "old update" in paths.stable_update.read_text()

    result = Updater(paths, filesystem, service, health).update(
        source,
        NEW_SHA,
        Path("/usr/bin/python3"),
    )

    assert result.changed is True
    assert filesystem.read_current_sha() == NEW_SHA
    assert paths.environment_file.read_bytes() == previous_environment
    assert service.calls == [
        "is-active", "is-enabled", "is-active",
        "daemon-reload", "restart", "is-active",
    ]
    assert paths.unit_path.read_bytes() == b"new unit\n"
    assert "new update" in paths.stable_update.read_text()
    assert "new config" in paths.stable_config.read_text()


def test_update_reloads_unit_already_written_by_interrupted_attempt(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    source = make_source(tmp_path / "new", unit=b"new unit\n")
    candidate = filesystem.prepare_release(source, NEW_SHA, Path("/usr/bin/python3"))
    assert filesystem.install_unit(candidate) is True
    assert filesystem.read_current_sha() == OLD_SHA
    service = FakeService()

    def health(host: str, port: int) -> None:
        if filesystem.read_current_sha() == OLD_SHA:
            return
        assert filesystem.read_current_sha() == NEW_SHA

    result = Updater(paths, filesystem, service, health).update(
        source,
        NEW_SHA,
        Path("/usr/bin/python3"),
    )

    assert result.changed is True
    assert service.calls == [
        "is-active", "is-enabled", "daemon-reload", "restart", "is-active",
        "daemon-reload", "restart", "is-active",
    ]
    assert paths.unit_path.read_bytes() == b"new unit\n"


def test_failed_candidate_health_restores_release_and_systemd_unit(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    source = make_source(tmp_path / "new", unit=b"new unit\n")
    service = FakeService()
    health_calls = 0

    def health(host: str, port: int) -> None:
        nonlocal health_calls
        health_calls += 1
        if filesystem.read_current_sha() == NEW_SHA:
            raise HealthCheckError("candidate failed")

    with pytest.raises(TransactionFailedError, match="restored"):
        Updater(paths, filesystem, service, health).update(
            source,
            NEW_SHA,
            Path("/usr/bin/python3"),
        )

    assert filesystem.read_current_sha() == OLD_SHA
    assert paths.unit_path.read_bytes() == b"old unit\n"
    assert service.calls == [
        "is-active", "is-enabled", "is-active",
        "daemon-reload", "restart", "daemon-reload", "restart", "is-active",
    ]
    assert health_calls == 3


def test_failed_candidate_and_failed_rollback_health_are_distinct(tmp_path: Path) -> None:
    paths, filesystem = _installed(tmp_path)
    source = make_source(tmp_path / "new")

    health_calls = 0

    def health(host: str, port: int) -> None:
        nonlocal health_calls
        health_calls += 1
        if health_calls > 1:
            raise HealthCheckError("failed")

    with pytest.raises(RollbackError, match="could not be verified healthy"):
        Updater(
            paths,
            filesystem,
            FakeService(),
            health,
        ).update(source, NEW_SHA, Path("/usr/bin/python3"))

    assert filesystem.read_current_sha() == OLD_SHA


def test_config_change_restarts_and_preserves_unknown_content(tmp_path: Path) -> None:
    paths, _ = _installed(tmp_path)
    original = paths.environment_file.read_text()
    paths.environment_file.write_text("# custom\nFUTURE=keep\n" + original, encoding="utf-8")
    service = FakeService()
    health: list[tuple[str, int]] = []
    configurator = Configurator(
        paths,
        service,
        lambda host, port: health.append((host, port)),
        environment_owner=None,
    )

    result = configurator.apply({"CFM_BIND_PORT": "9000"})

    assert result.changed is True
    assert service.calls == ["restart", "is-active"]
    assert health == [("192.168.1.20", 9000)]
    assert "# custom\nFUTURE=keep\n" in paths.environment_file.read_text()


def test_config_change_preserves_unknown_secret_without_interpreting_or_exposing_it(
    tmp_path: Path,
    capsys,
) -> None:
    paths, _ = _installed(tmp_path)
    fake_secret = "TEST_FUTURE_SECRET_MUST_NOT_LEAK"
    original = paths.environment_file.read_text()
    paths.environment_file.write_text(
        f"CFM_CLOUDFLARE_API_TOKEN={fake_secret}\n" + original,
        encoding="utf-8",
    )
    service = FakeService()
    configurator = Configurator(
        paths,
        service,
        lambda host, port: None,
        environment_owner=None,
    )

    result = configurator.apply({"CFM_BIND_PORT": "8081"})
    status = configurator.status()

    assert f"CFM_CLOUDFLARE_API_TOKEN={fake_secret}\n" in paths.environment_file.read_text()
    assert fake_secret not in repr(result)
    assert fake_secret not in repr(status)
    captured = capsys.readouterr()
    assert fake_secret not in captured.out
    assert fake_secret not in captured.err


def test_config_health_failure_restores_exact_previous_file(tmp_path: Path) -> None:
    paths, _ = _installed(tmp_path)
    previous = paths.environment_file.read_bytes()
    service = FakeService()
    health_calls = 0

    def health(host: str, port: int) -> None:
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise HealthCheckError("candidate failed")

    with pytest.raises(TransactionFailedError, match="restored"):
        Configurator(
            paths,
            service,
            health,
            environment_owner=None,
        ).apply({"CFM_BIND_HOST": "10.0.0.8"})

    assert paths.environment_file.read_bytes() == previous
    assert service.calls == ["restart", "restart", "is-active"]
    assert health_calls == 2


def test_config_rollback_health_failure_is_critical(tmp_path: Path) -> None:
    paths, _ = _installed(tmp_path)

    with pytest.raises(RollbackError):
        Configurator(
            paths,
            FakeService(),
            lambda host, port: (_ for _ in ()).throw(HealthCheckError("failed")),
            environment_owner=None,
        ).apply({"CFM_BIND_PORT": "9000"})


def test_invalid_config_causes_no_mutation_or_restart(tmp_path: Path) -> None:
    paths, _ = _installed(tmp_path)
    previous = paths.environment_file.read_bytes()
    service = FakeService()

    with pytest.raises(ValidationError):
        Configurator(
            paths,
            service,
            lambda host, port: None,
            environment_owner=None,
        ).apply({"CFM_BIND_HOST": "0.0.0.0"})

    assert paths.environment_file.read_bytes() == previous
    assert service.calls == []


def test_unchanged_config_avoids_restart(tmp_path: Path) -> None:
    paths, _ = _installed(tmp_path)
    service = FakeService()

    result = Configurator(
        paths,
        service,
        lambda host, port: None,
        environment_owner=None,
    ).apply({"CFM_BIND_PORT": "8000"})

    assert result.changed is False
    assert service.calls == []
