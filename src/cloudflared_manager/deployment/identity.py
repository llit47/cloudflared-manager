"""Creation and validation of the unprivileged manager service identity."""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
from pathlib import Path

from cloudflared_manager.deployment.errors import HostOperationError

SERVICE_IDENTITY = "cloudflared-manager"
SERVICE_HOME = "/nonexistent"
NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin")


def ensure_service_identity() -> None:
    """Create the dedicated system group/user, or strictly validate existing ones."""

    try:
        group = grp.getgrnam(SERVICE_IDENTITY)
    except KeyError:
        _run_account_command(
            "groupadd",
            ("--system", "--", SERVICE_IDENTITY),
        )
        try:
            group = grp.getgrnam(SERVICE_IDENTITY)
        except KeyError as error:
            raise HostOperationError("The dedicated service group was not created.") from error

    try:
        user = pwd.getpwnam(SERVICE_IDENTITY)
    except KeyError:
        shell = _nologin_shell()
        _run_account_command(
            "useradd",
            (
                "--system",
                "--gid",
                SERVICE_IDENTITY,
                "--home-dir",
                SERVICE_HOME,
                "--no-create-home",
                "--shell",
                shell,
                "--",
                SERVICE_IDENTITY,
            ),
        )
        try:
            user = pwd.getpwnam(SERVICE_IDENTITY)
        except KeyError as error:
            raise HostOperationError("The dedicated service user was not created.") from error

    if (
        user.pw_uid == 0
        or group.gr_gid == 0
        or user.pw_uid >= 1000
        or group.gr_gid >= 1000
        or user.pw_gid != group.gr_gid
    ):
        raise HostOperationError("The existing cloudflared-manager identity is unsafe.")
    if user.pw_dir != SERVICE_HOME or user.pw_shell not in NOLOGIN_SHELLS:
        raise HostOperationError("The existing cloudflared-manager identity is unsafe.")
    if any(member != SERVICE_IDENTITY for member in group.gr_mem):
        raise HostOperationError("The cloudflared-manager group has unexpected members.")
    try:
        memberships = set(os.getgrouplist(SERVICE_IDENTITY, user.pw_gid))
    except OSError as error:
        raise HostOperationError("The cloudflared-manager groups could not be verified.") from error
    if memberships != {group.gr_gid}:
        raise HostOperationError(
            "The cloudflared-manager account has unexpected supplementary groups."
        )


def _nologin_shell() -> str:
    for shell in NOLOGIN_SHELLS:
        if Path(shell).is_file():
            return shell
    raise HostOperationError("A non-login system shell is unavailable.")


def _run_account_command(program: str, arguments: tuple[str, ...]) -> None:
    executable = shutil.which(program)
    if executable is None or Path(executable).name != program:
        raise HostOperationError(f"{program} is required to create the service identity.")
    try:
        result = subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            timeout=20,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HostOperationError("The dedicated service identity could not be created.") from error
    if result.returncode != 0:
        raise HostOperationError("The dedicated service identity could not be created.")
