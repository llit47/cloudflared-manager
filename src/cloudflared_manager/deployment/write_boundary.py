"""Fail closed if the manager service could write through its shared mount view."""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

from cloudflared_manager.activation.filesystem import FilesystemRefused, PinnedDirectory
from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment.identity import SERVICE_IDENTITY
from cloudflared_manager.deployment.paths import DeploymentPaths

CLOUDFLARED_ROOT = Path("/etc/cloudflared")
_MAX_OBJECTS = 1024
_MAX_DEPTH = 8


def require_write_boundary(
    paths: DeploymentPaths,
    *,
    adopted: Path | None,
    cloudflared_root: Path = CLOUDFLARED_ROOT,
    trusted_uid: int = 0,
    service_uid: int | None = None,
    anchor: Path = Path("/"),
    probe_as_service: bool = False,
) -> None:
    """Reject unsafe host metadata before the isolated root recovery grant.

    Production calls use root-owned paths. Tests inject a disposable trust
    anchor and owner; no check changes host metadata.
    """
    if service_uid is None:
        try:
            service_uid = pwd.getpwnam(SERVICE_IDENTITY).pw_uid
        except KeyError:
            raise HostOperationError("The manager service identity is unavailable.") from None
    if service_uid == trusted_uid:
        raise HostOperationError("The manager service identity is unsafe.")
    if probe_as_service and os.geteuid() != service_uid:
        raise HostOperationError("The manager process identity is unsafe.")
    if adopted is not None:
        require_nonprivate_adoption(adopted, paths)
    try:
        if probe_as_service:
            # The service cannot open these root-private directories. Their
            # metadata is visible via lstat from the trusted parent.
            _private_runtime(paths.config_root, trusted_uid, service_uid, 0o750)
            _private_runtime(paths.runtime_root, trusted_uid, service_uid, 0o700)
        else:
            with PinnedDirectory(paths.config_root, anchor=anchor, owner=trusted_uid) as config:
                if config.facts.mode != 0o750:
                    raise HostOperationError("The manager configuration directory is unsafe.")
                _no_acl_or_write(config.path, os.fstat(config.fd), trusted_uid, service_uid,
                                 False, require_root_owner=True)
                _fixed_file(paths.environment_file, trusted_uid, service_uid)
                config.revalidate()
            with PinnedDirectory(paths.runtime_root, anchor=anchor, owner=trusted_uid) as runtime:
                if runtime.facts.mode != 0o700:
                    raise HostOperationError("The manager runtime state is unsafe.")
                _no_acl_or_write(runtime.path, os.fstat(runtime.fd), trusted_uid, service_uid,
                                 False, require_root_owner=True)
                runtime.revalidate()
        _cloudflared_tree(cloudflared_root, trusted_uid, service_uid,
                          anchor, probe_as_service,
                          required=adopted is not None and cloudflared_root in adopted.parents)
    except (FilesystemRefused, OSError, RuntimeError) as error:
        raise HostOperationError("The manager write boundary is unsafe.") from None


def require_adopted_write_boundary(adopted: Path, *, service_uid: int,
                                   cloudflared_root: Path = CLOUDFLARED_ROOT,
                                   trusted_uid: int = 0,
                                   anchor: Path = Path("/")) -> None:
    """Reject unsafe adoption before persisting a privileged config path."""
    if cloudflared_root not in adopted.parents:
        return
    if service_uid == trusted_uid:
        raise HostOperationError("The manager service identity is unsafe.")
    try:
        _cloudflared_tree(cloudflared_root, trusted_uid, service_uid,
                          anchor, False, required=True)
    except (FilesystemRefused, OSError, RuntimeError):
        raise HostOperationError("The cloudflared write boundary is unsafe.") from None


def require_nonprivate_adoption(adopted: Path, paths: DeploymentPaths) -> None:
    if (adopted == paths.config_root or paths.config_root in adopted.parents
        or adopted == paths.runtime_root or paths.runtime_root in adopted.parents):
        raise HostOperationError("The adopted config is inside manager-private state.")


def _cloudflared_tree(root: Path, trusted_uid: int, service_uid: int,
                      anchor: Path, probe_as_service: bool, *, required: bool) -> None:
    if not root.exists() and not root.is_symlink():
        if required:
            raise HostOperationError("The adopted config directory is unavailable.")
        return
    if probe_as_service and not os.access(root, os.X_OK):
        # Root installation/adoption already inspected the inaccessible tree.
        # The web UID cannot reach any object beneath this root-only directory.
        _no_acl_or_write(root, root.lstat(), trusted_uid, service_uid,
                         True, require_root_owner=True)
        return
    with PinnedDirectory(root, anchor=anchor, owner=trusted_uid) as cloudflared:
        _no_acl_or_write(cloudflared.path, os.fstat(cloudflared.fd), trusted_uid,
                         service_uid, probe_as_service, require_root_owner=True)
        remaining = [_MAX_OBJECTS]
        _scan_tree(cloudflared.path, trusted_uid, service_uid,
                   probe_as_service, remaining, depth=0)
        cloudflared.revalidate()


def _scan_tree(path: Path, trusted_uid: int, service_uid: int,
               probe_as_service: bool, remaining: list[int], *, depth: int) -> None:
    if depth >= _MAX_DEPTH:
        raise HostOperationError("The cloudflared directory is too deep to verify.")
    try:
        entries = os.scandir(path)
    except PermissionError:
        # A root-owned, non-writable child that the service cannot traverse
        # contains no reachable web write target. Root install/adoption scans it.
        if probe_as_service and depth > 0 and not os.access(path, os.X_OK):
            return
        raise
    with entries:
        for entry in entries:
            remaining[0] -= 1
            if remaining[0] < 0:
                raise HostOperationError("The cloudflared directory is too large to verify.")
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _no_acl_or_write(Path(entry.path), info, trusted_uid, service_uid,
                                 probe_as_service, require_root_owner=True)
                _scan_tree(Path(entry.path), trusted_uid, service_uid,
                           probe_as_service, remaining, depth=depth + 1)
            elif stat.S_ISREG(info.st_mode):
                _no_acl_or_write(Path(entry.path), info, trusted_uid, service_uid,
                                 probe_as_service, require_root_owner=False)
            else:
                raise HostOperationError("The cloudflared directory contains an unsafe object.")


def _private_runtime(path: Path, trusted_uid: int, service_uid: int,
                     expected_mode: int) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != trusted_uid
        or stat.S_IMODE(info.st_mode) != expected_mode or os.access(path, os.W_OK)):
        raise HostOperationError("A manager-private directory is unsafe.")


def _fixed_file(path: Path, trusted_uid: int, service_uid: int) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise HostOperationError("The manager environment file is unsafe.")
    _no_acl_or_write(path, info, trusted_uid, service_uid,
                     False, require_root_owner=True)


def _no_acl_or_write(path: Path, info: os.stat_result, trusted_uid: int,
                     service_uid: int, probe_as_service: bool,
                     *, require_root_owner: bool) -> None:
    if (require_root_owner and info.st_uid != trusted_uid
        or not require_root_owner and info.st_uid == service_uid):
        raise HostOperationError("A writable-path object has unsafe ownership.")
    if info.st_mode & 0o022:
        raise HostOperationError("A writable-path object has unsafe permissions.")
    # Includes POSIX access/default ACLs, NFSv4 ACLs, and rich ACL variants.
    # A default ACL on a directory could grant writes to future children.
    if not probe_as_service and any(
        name.startswith("system.")
        for name in os.listxattr(path, follow_symlinks=False)
    ):
        raise HostOperationError("A writable-path object has unsupported system metadata.")
    if probe_as_service and os.access(path, os.W_OK):
        raise HostOperationError("The manager service can write a privileged path.")
