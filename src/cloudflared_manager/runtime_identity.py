"""Deterministic identity for non-secret mutable runtime settings."""

import hashlib
import json
import re
from pathlib import Path


def package_release_id(package_file: str) -> str | None:
    """Identify an installed release lexically, never by resolving `current`."""

    path = Path(package_file)
    if not path.is_absolute() or ".." in path.parts or len(path.parents) < 7:
        return None
    package, site, python, lib, venv, release, releases = path.parents[:7]
    if (
        package.name != "cloudflared_manager"
        or site.name != "site-packages"
        or re.fullmatch(r"python3\.\d+", python.name) is None
        or lib.name != "lib"
        or venv.name != ".venv"
        or releases.name != "releases"
        or re.fullmatch(r"[0-9a-f]{40}", release.name) is None
    ):
        return None
    try:
        if path.resolve(strict=True) != path:
            return None
    except (OSError, RuntimeError):
        return None
    return release.name


# The installed package lives in the final release-local venv. Cache the identity
# when this code loads; a later `current` switch cannot change this process's ID.
PROCESS_RELEASE_ID = package_release_id(__file__)


def runtime_config_id(bind_host: str, bind_port: int, discovery_enabled: bool) -> str:
    encoded = json.dumps(
        [bind_host, bind_port, discovery_enabled], separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
