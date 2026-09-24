"""Explicit, read-only adoption of a discovered cloudflared config path."""

from __future__ import annotations

import os
import pwd
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.cloudflared import (
    CloudflaredConfig,
    CloudflaredConfigError,
    CloudflaredRuntime,
    ManagementMode,
    RuntimeDiscoveryError,
    parse_cloudflared_config,
)
from cloudflared_manager.cloudflared.discovery import (
    RuntimeDiscoveryProvider,
    discover_cloudflared,
)
from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES
from cloudflared_manager.deployment.configurator import ConfigResult, Configurator
from cloudflared_manager.deployment.errors import HostOperationError, ValidationError
from cloudflared_manager.deployment.identity import SERVICE_IDENTITY
from cloudflared_manager.deployment.settings import ManagerSettings
from cloudflared_manager.deployment.validation import validate_cloudflared_config_path
from cloudflared_manager.deployment.write_boundary import (
    require_adopted_write_boundary, require_nonprivate_adoption,
)

ConfigParser = Callable[[Path], CloudflaredConfig]
IdentityProvider = Callable[[], tuple[int, int]]
PathValidator = Callable[[Path], None]
_MANAGER_SERVICE_HIDDEN_PREFIXES = (
    Path("/root"),
    Path("/home"),
    Path("/run/user"),
    Path("/tmp"),
    Path("/var/tmp"),
)


@dataclass(frozen=True, slots=True)
class AdoptionStatus:
    """Sanitized state for the root-only configuration interface."""

    settings: ManagerSettings
    runtime: CloudflaredRuntime | None
    discovery_succeeded: bool

    @property
    def detected_path(self) -> Path | None:
        if (
            self.runtime is not None
            and self.runtime.management_mode is ManagementMode.LOCAL_CONFIG
        ):
            return self.runtime.explicit_config_path
        return None


class CloudflaredConfigAdopter:
    """Coordinate explicit adoption without gaining cloudflared write access."""

    def __init__(
        self,
        configurator: Configurator,
        *,
        runtime_discovery: RuntimeDiscoveryProvider = discover_cloudflared,
        config_parser: ConfigParser = parse_cloudflared_config,
        identity_provider: IdentityProvider | None = None,
        sandbox_path_validator: PathValidator | None = None,
    ) -> None:
        self.configurator = configurator
        self.runtime_discovery = runtime_discovery
        self.config_parser = config_parser
        self.identity_provider = identity_provider or _service_identity
        self.sandbox_path_validator = (
            sandbox_path_validator or _require_manager_service_sandbox_visible
        )

    def status(self) -> AdoptionStatus:
        settings = self.configurator.settings()
        if not settings.runtime_discovery_enabled:
            return AdoptionStatus(settings, None, False)
        try:
            runtime = self.runtime_discovery(True)
        except RuntimeDiscoveryError:
            return AdoptionStatus(settings, None, False)
        succeeded = bool(
            runtime is not None
            and runtime.systemd_available
            and runtime.service_exists is True
            and runtime.load_state == "loaded"
        )
        return AdoptionStatus(settings, runtime, succeeded)

    def adopt_detected(self) -> ConfigResult:
        status = self.status()
        if not status.settings.runtime_discovery_enabled:
            raise HostOperationError(
                "Runtime discovery must be enabled before config adoption."
            )
        if not status.discovery_succeeded or status.runtime is None:
            raise HostOperationError(
                "Cloudflared runtime discovery did not find a usable local service."
            )
        runtime = status.runtime
        if runtime.management_mode is not ManagementMode.LOCAL_CONFIG:
            raise HostOperationError(
                "The detected cloudflared service is not in local-config mode."
            )
        if runtime.explicit_config_path is None:
            raise HostOperationError(
                "The detected cloudflared service has no explicit config path."
            )

        candidate = validate_cloudflared_config_path(runtime.explicit_config_path)
        self.sandbox_path_validator(candidate)
        service_uid, service_gid = self.identity_provider()
        require_nonprivate_adoption(candidate, self.configurator.paths)
        require_adopted_write_boundary(candidate, service_uid=service_uid)
        _require_service_readable_regular_file(candidate, service_uid, service_gid)
        try:
            self.config_parser(candidate)
        except CloudflaredConfigError as error:
            raise ValidationError(
                "The detected cloudflared configuration is not valid for adoption."
            ) from error
        return self.configurator.apply(
            {"CFM_CLOUDFLARED_CONFIG_PATH": str(candidate)}
        )

    def clear(self) -> ConfigResult:
        return self.configurator.apply({"CFM_CLOUDFLARED_CONFIG_PATH": None})


def _service_identity() -> tuple[int, int]:
    try:
        user = pwd.getpwnam(SERVICE_IDENTITY)
    except KeyError as error:
        raise HostOperationError(
            "The cloudflared-manager service identity is unavailable."
        ) from error
    return user.pw_uid, user.pw_gid


def _require_manager_service_sandbox_visible(path: Path) -> None:
    """Reject paths hidden by the fixed ProtectHome/PrivateTmp unit settings."""

    if any(
        path == prefix or prefix in path.parents
        for prefix in _MANAGER_SERVICE_HIDDEN_PREFIXES
    ):
        raise ValidationError(
            "The detected cloudflared configuration is hidden by the manager "
            "service sandbox."
        )


def _require_service_readable_regular_file(path: Path, uid: int, gid: int) -> None:
    """Fail closed before root adopts a file the manager could not read."""

    try:
        if path.resolve(strict=True) != path:
            raise ValidationError(
                "The detected cloudflared configuration path is not canonical."
            )
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(
                "The detected cloudflared configuration is not a regular file."
            )
        if metadata.st_size > MAX_CLOUDFLARED_CONFIG_BYTES:
            raise ValidationError(
                "The detected cloudflared configuration is unexpectedly large."
            )
        if not _mode_allows(metadata, uid, gid, stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH):
            raise ValidationError(
                "The detected cloudflared configuration is not readable by the manager service."
            )
        for parent in reversed(path.parents):
            parent_metadata = parent.lstat()
            if not stat.S_ISDIR(parent_metadata.st_mode) or not _mode_allows(
                parent_metadata,
                uid,
                gid,
                stat.S_IXUSR,
                stat.S_IXGRP,
                stat.S_IXOTH,
            ):
                raise ValidationError(
                    "The detected cloudflared configuration is not reachable by the "
                    "manager service."
                )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        os.close(descriptor)
    except FileNotFoundError as error:
        raise ValidationError(
            "The detected cloudflared configuration file does not exist."
        ) from error
    except ValidationError:
        raise
    except (OSError, RuntimeError) as error:
        raise ValidationError(
            "The detected cloudflared configuration file cannot be inspected safely."
        ) from error


def _mode_allows(
    metadata: os.stat_result,
    uid: int,
    gid: int,
    owner_bit: int,
    group_bit: int,
    other_bit: int,
) -> bool:
    if metadata.st_uid == uid:
        return bool(metadata.st_mode & owner_bit)
    if metadata.st_gid == gid:
        return bool(metadata.st_mode & group_bit)
    return bool(metadata.st_mode & other_bit)
