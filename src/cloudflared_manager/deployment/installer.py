"""First-install transaction for the manager-owned deployment."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.deployment.environment import (
    atomic_write_environment,
    initial_environment,
    read_environment,
)
from cloudflared_manager.deployment.errors import (
    RollbackError,
    TransactionFailedError,
)
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.protocols import HealthVerifier, ManagerService
from cloudflared_manager.deployment.reconciliation import DeploymentReconciler
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import (
    ManagerSettings,
    complete_existing_environment,
    settings_from_document,
)
from cloudflared_manager.deployment.validation import validate_bind_host, validate_port, validate_sha


@dataclass(frozen=True, slots=True)
class InstallResult:
    sha: str
    settings: ManagerSettings
    changed: bool


class Installer:
    """Install once without taking over an existing manager deployment."""

    def __init__(
        self,
        paths: DeploymentPaths,
        filesystem: ReleaseFilesystem,
        service: ManagerService,
        health: HealthVerifier,
        ensure_identity: Callable[[], None],
        *,
        environment_owner: tuple[int, int] | None = (0, 0),
    ) -> None:
        self.paths = paths
        self.filesystem = filesystem
        self.service = service
        self.health = health
        self.ensure_identity = ensure_identity
        self.environment_owner = environment_owner

    def reconcile(self) -> InstallResult:
        """Repair a recognizable active installation without changing its configuration."""

        self.filesystem.require_owned_layout()
        self.ensure_identity()
        revision = self.filesystem.read_current_sha()
        document, _ = read_environment(self.paths.environment_file)
        settings = settings_from_document(document)
        reconciled = DeploymentReconciler(self.filesystem, self.service, self.health).reconcile(
            self.paths.release(revision),
            settings,
        )
        return InstallResult(sha=revision, settings=settings, changed=reconciled.changed)

    def install(
        self,
        source: Path,
        sha: str,
        python: Path,
        bind_host: str,
        bind_port: int,
    ) -> InstallResult:
        revision = validate_sha(sha)
        host = validate_bind_host(bind_host)
        port = validate_port(bind_port)
        if self.paths.current.exists() or self.paths.current.is_symlink():
            return self.reconcile()
        self.filesystem.validate_first_install_paths(revision)
        self.ensure_identity()
        self.filesystem.ensure_layout()
        release = self.filesystem.prepare_release(source, revision, python)
        self.filesystem.validate_deployment_assets(release)

        environment_existed = (
            self.paths.environment_file.exists()
            or self.paths.environment_file.is_symlink()
        )
        previous_environment: bytes | None = None
        if environment_existed:
            document, previous_environment = read_environment(self.paths.environment_file)
            document = complete_existing_environment(document, host, port)
        else:
            document = initial_environment(host, port)
        settings = settings_from_document(document)

        unit_snapshot = self.filesystem.snapshot(self.paths.unit_path)
        current_target: str | None = None
        unit_install_completed = False
        environment_attempted = False
        service_started = False
        enable_attempted = False
        try:
            environment_attempted = True
            atomic_write_environment(
                self.paths.environment_file,
                document,
                owner=self.environment_owner,
            )
            self.filesystem.install_unit(release)
            unit_install_completed = True
            self.service.daemon_reload()
            current_target = self.filesystem.switch_current(revision)
            self.service.start()
            service_started = True
            self.health(settings.bind_host, settings.bind_port)
            enable_attempted = True
            self.service.enable()
            self.filesystem.install_stable_administration(release)
            return InstallResult(sha=revision, settings=settings, changed=True)
        except Exception as error:
            rollback_errors: list[Exception] = [error] if isinstance(error, RollbackError) else []
            if enable_attempted:
                try:
                    self.service.disable()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            try:
                self.filesystem.restore_current(current_target)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            try:
                self.filesystem.restore_snapshot(self.paths.unit_path, unit_snapshot)
                if unit_install_completed:
                    self.service.daemon_reload()
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            if service_started:
                try:
                    self.service.stop()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if environment_attempted:
                try:
                    if previous_environment is None:
                        self.paths.environment_file.unlink(missing_ok=True)
                    else:
                        atomic_write_environment(
                            self.paths.environment_file,
                            previous_environment,
                            owner=self.environment_owner,
                        )
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise RollbackError(
                    "Installation failed and its manager deployment rollback was incomplete."
                ) from error
            raise TransactionFailedError(
                "Installation failed; manager deployment changes were rolled back."
            ) from error
