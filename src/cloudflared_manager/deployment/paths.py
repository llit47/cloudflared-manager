"""Fixed production paths and safely injectable test paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cloudflared_manager.deployment.validation import validate_sha


@dataclass(frozen=True, slots=True)
class DeploymentPaths:
    """Manager-owned paths; production CLI entry points use these defaults only."""

    install_root: Path = Path("/opt/cloudflared-manager")
    config_root: Path = Path("/etc/cloudflared-manager")
    unit_path: Path = Path("/etc/systemd/system/cloudflared-manager.service")
    update_link: Path = Path("/usr/local/sbin/cfm-update")
    config_link: Path = Path("/usr/local/sbin/cfm-config")
    lock_path: Path = Path("/run/lock/cloudflared-manager-update.lock")

    @property
    def releases(self) -> Path:
        return self.install_root / "releases"

    @property
    def current(self) -> Path:
        return self.install_root / "current"

    @property
    def environment_file(self) -> Path:
        return self.config_root / "cloudflared-manager.env"

    @property
    def stable_update(self) -> Path:
        return self.install_root / "update.sh"

    @property
    def stable_config(self) -> Path:
        return self.install_root / "config.sh"

    def release(self, sha: str) -> Path:
        return self.releases / validate_sha(sha)
