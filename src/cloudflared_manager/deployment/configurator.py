"""Safe manager EnvironmentFile configuration transaction."""

from __future__ import annotations

from dataclasses import dataclass

from cloudflared_manager.deployment.environment import (
    atomic_write_environment,
    read_environment,
    require_safe_environment,
)
from cloudflared_manager.deployment.errors import RollbackError, TransactionFailedError
from cloudflared_manager.deployment.health import verify_managed_health, verify_running_release
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.protocols import HealthVerifier, ManagerService
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.settings import ManagerSettings, settings_from_document


@dataclass(frozen=True, slots=True)
class ConfigResult:
    changed: bool
    settings: ManagerSettings


class Configurator:
    """Read and transactionally update supported manager configuration."""

    def __init__(
        self,
        paths: DeploymentPaths,
        service: ManagerService,
        health: HealthVerifier,
        *,
        environment_owner: tuple[int, int] | None = (0, 0),
    ) -> None:
        self.paths = paths
        self.service = service
        self.health = health
        self.environment_owner = environment_owner

    def status(self) -> tuple[ManagerSettings, tuple[str | None, str | None, str | None]]:
        return self.settings(), self.service.sanitized_status()

    def settings(self) -> ManagerSettings:
        document, _ = read_environment(self.paths.environment_file)
        return settings_from_document(document)

    def apply(self, updates: dict[str, str | None]) -> ConfigResult:
        require_safe_environment(
            self.paths.environment_file,
            owner=self.environment_owner,
        )
        document, previous = read_environment(self.paths.environment_file)
        previous_settings = settings_from_document(document)
        candidate = document.updated(updates)
        candidate_settings = settings_from_document(candidate)
        filesystem = ReleaseFilesystem(self.paths, owner=self.environment_owner)
        filesystem.require_owned_layout()
        expected_release = filesystem.read_current_sha()
        # A config-only operation must not adopt a pending release switch. This
        # process-bound check does not assume the persisted bind is applied yet.
        verify_running_release(self.service, self.paths.install_root, expected_release)
        if candidate.render() == document.render():
            try:
                verify_managed_health(
                    self.service, self.health, previous_settings.bind_host,
                    previous_settings.bind_port, previous_settings.config_id,
                    expected_release,
                )
                return ConfigResult(changed=False, settings=previous_settings)
            except Exception:
                try:
                    self.service.restart()
                    verify_managed_health(
                        self.service, self.health, previous_settings.bind_host,
                        previous_settings.bind_port, previous_settings.config_id,
                        expected_release,
                    )
                except Exception as error:
                    # The persisted settings remain authoritative for both attempts.
                    try:
                        self.service.restart()
                        verify_managed_health(
                            self.service, self.health, previous_settings.bind_host,
                            previous_settings.bind_port, previous_settings.config_id,
                            expected_release,
                        )
                    except Exception as recovery_error:
                        raise RollbackError(
                            "The corrective operation failed; healthy operation using the "
                            "persisted configuration could not be re-established."
                        ) from recovery_error
                    raise TransactionFailedError(
                        "The corrective operation failed; the persisted configuration "
                        "is healthy again."
                    ) from error
                return ConfigResult(changed=True, settings=previous_settings)

        try:
            atomic_write_environment(
                self.paths.environment_file,
                candidate,
                owner=self.environment_owner,
            )
            self.service.restart()
            verify_managed_health(
                self.service,
                self.health,
                candidate_settings.bind_host,
                candidate_settings.bind_port,
                candidate_settings.config_id,
                expected_release,
            )
            return ConfigResult(changed=True, settings=candidate_settings)
        except Exception as error:
            try:
                atomic_write_environment(
                    self.paths.environment_file,
                    previous,
                    owner=self.environment_owner,
                )
                self.service.restart()
                verify_managed_health(
                    self.service,
                    self.health,
                    previous_settings.bind_host,
                    previous_settings.bind_port,
                    previous_settings.config_id,
                    expected_release,
                )
            except Exception as rollback_error:
                raise RollbackError(
                    "Configuration failed and the previous manager configuration is not healthy."
                ) from rollback_error
            raise TransactionFailedError(
                "Configuration failed; the previous configuration was restored and is healthy."
            ) from error
