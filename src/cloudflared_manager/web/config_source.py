"""Browser-safe presentation of config adoption and runtime relationships."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cloudflared_manager.cloudflared import CloudflaredRuntime, ManagementMode

ConfigSourceTone = Literal["neutral", "success", "warning", "error"]
RuntimeSnapshotState = Literal["disabled", "unavailable", "available"]


@dataclass(frozen=True, slots=True)
class ConfigSourceView:
    """Sanitized config-source status safe to expose to a browser."""

    label: str
    tone: ConfigSourceTone
    description: str


def unadopted_config_source(
    runtime_state: RuntimeSnapshotState,
    runtime: CloudflaredRuntime | None,
) -> ConfigSourceView:
    """Describe config detection without exposing or loading a candidate path."""

    if runtime_state == "disabled":
        return ConfigSourceView(
            label="Discovery disabled",
            tone="neutral",
            description=(
                "No configuration is adopted. Runtime discovery is disabled, so a "
                "local config candidate cannot be checked."
            ),
        )

    if runtime_state == "unavailable" or runtime is None:
        return ConfigSourceView(
            label="Discovery unavailable",
            tone="warning",
            description=(
                "No configuration is adopted. Runtime inspection is unavailable, so "
                "a local config candidate could not be determined."
            ),
        )

    if not runtime.systemd_available or runtime.service_exists is None:
        return ConfigSourceView(
            label="Discovery unavailable",
            tone="warning",
            description=(
                "No configuration is adopted. cloudflared.service could not be "
                "inspected, so a local config candidate could not be determined."
            ),
        )
    if runtime.service_exists is False:
        return ConfigSourceView(
            label="Local config not detected",
            tone="neutral",
            description=(
                "No configuration is adopted, and cloudflared.service is not loaded."
            ),
        )
    if not _service_was_discovered(runtime):
        return ConfigSourceView(
            label="Local config not detected",
            tone="neutral",
            description=(
                "No configuration is adopted, and a usable loaded cloudflared.service "
                "could not be verified."
            ),
        )

    if runtime.management_mode is ManagementMode.REMOTE_TOKEN:
        return ConfigSourceView(
            label="Token-managed",
            tone="neutral",
            description=(
                "cloudflared.service is token-managed, so there is no local config "
                "candidate to adopt."
            ),
        )
    if (
        runtime.management_mode is ManagementMode.LOCAL_CONFIG
        and runtime.explicit_config_path is not None
    ):
        return ConfigSourceView(
            label="Detected · Not adopted",
            tone="warning",
            description=(
                "An existing local configuration was detected, but Cloudflared Manager "
                "is not reading or managing it. A root administrator can adopt it with "
                "sudo cfm-config cloudflared-config adopt-detected."
            ),
        )
    if runtime.management_mode is ManagementMode.LOCAL_CONFIG:
        return ConfigSourceView(
            label="Local config not detected",
            tone="neutral",
            description=(
                "Local-config mode was detected, but cloudflared.service did not expose "
                "a usable explicit config candidate."
            ),
        )
    return ConfigSourceView(
        label="Not adopted",
        tone="neutral",
        description=(
            "No configuration is adopted, and no usable local config candidate was "
            "detected."
        ),
    )


def adopted_config_source(
    adopted_path: Path,
    runtime_state: RuntimeSnapshotState,
    runtime: CloudflaredRuntime | None,
) -> ConfigSourceView:
    """Describe a loaded adopted config's relationship to one runtime snapshot."""

    if runtime_state == "disabled":
        return ConfigSourceView(
            label="Adopted · Unverified",
            tone="warning",
            description=(
                "Cloudflared Manager loaded the explicitly adopted configuration. "
                "Runtime discovery is disabled, so its relationship to the live service "
                "cannot be verified."
            ),
        )

    if runtime_state == "unavailable" or runtime is None:
        return ConfigSourceView(
            label="Adopted · Unverified",
            tone="warning",
            description=(
                "Cloudflared Manager loaded the explicitly adopted configuration, but "
                "runtime inspection is unavailable."
            ),
        )

    if _service_was_discovered(runtime) and (
        runtime.management_mode is ManagementMode.LOCAL_CONFIG
        and runtime.explicit_config_path is not None
    ):
        if runtime.explicit_config_path == adopted_path:
            return ConfigSourceView(
                label="Adopted",
                tone="success",
                description=(
                    "Cloudflared Manager loaded the explicitly adopted configuration, "
                    "and it matches the config referenced by cloudflared.service."
                ),
            )
        return ConfigSourceView(
            label="Adopted · Service differs",
            tone="warning",
            description=(
                "Cloudflared Manager loaded its adopted configuration, but "
                "cloudflared.service currently references a different configuration. "
                "No change was made automatically."
            ),
        )

    if not _service_was_discovered(runtime):
        reason = "cloudflared.service could not be verified"
    elif runtime.management_mode is ManagementMode.REMOTE_TOKEN:
        reason = "cloudflared.service is token-managed"
    elif runtime.management_mode is ManagementMode.LOCAL_CONFIG:
        reason = "the service did not expose a usable explicit config candidate"
    else:
        reason = "the service configuration mode could not be determined"
    return ConfigSourceView(
        label="Adopted · Unverified",
        tone="warning",
        description=(
            "Cloudflared Manager loaded the explicitly adopted configuration, but its "
            f"relationship to the live service cannot be verified because {reason}."
        ),
    )


def _service_was_discovered(runtime: CloudflaredRuntime) -> bool:
    return (
        runtime.systemd_available
        and runtime.service_exists is True
        and runtime.load_state == "loaded"
    )


def config_source_load_error() -> ConfigSourceView:
    """Represent an explicitly adopted config that could not be loaded safely."""

    return ConfigSourceView(
        label="Adopted · Load error",
        tone="error",
        description=(
            "A configuration is explicitly adopted, but Cloudflared Manager could not "
            "safely load it. Runtime status is reported separately."
        ),
    )
