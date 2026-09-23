"""Linux IO for the fixed internal service contract, with bounded capture."""
from __future__ import annotations

import os
import selectors
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from cloudflared_manager.activation.filesystem import PinnedDirectory
from cloudflared_manager.activation.service import (
    MAX_OBSERVATION_BYTES, PROPERTIES, UNIT, ServiceRefused, parse_start,
)

_SYSTEMCTL = Path("/usr/bin/systemctl")
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "SYSTEMD_PAGER": "cat"}
_SHOW = (str(_SYSTEMCTL), "show", UNIT, "--no-pager", "--property=" + ",".join(PROPERTIES))
_RESTART = (str(_SYSTEMCTL), "restart", UNIT)


@contextmanager
def verified_executable(path: Path):
    """Pin a canonical regular root-owned executable under trusted ancestry."""
    fd = None
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ServiceRefused()
        with PinnedDirectory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent.fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o6022
                or not info.st_mode & 0o111 or info.st_nlink != 1):
                raise ServiceRefused()
            parent.revalidate()
            visible = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
            if visible != info:
                raise ServiceRefused()
            yield fd, (info.st_dev, info.st_ino)
    except Exception:
        raise ServiceRefused() from None
    finally:
        if fd is not None:
            os.close(fd)


def _command(*, restart: bool) -> tuple[bool, bytes]:
    process = None
    try:
        with verified_executable(_SYSTEMCTL) as (fd, _):
            process = subprocess.Popen(
                _RESTART if restart else _SHOW, executable=f"/proc/self/fd/{fd}",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                shell=False, close_fds=True, pass_fds=(fd,), env=dict(_ENV), cwd="/",
            )
            deadline = time.monotonic() + (30 if restart else 5)
            output = bytearray()
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ServiceRefused()
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, min(4096, MAX_OBSERVATION_BYTES + 1 - len(output)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > MAX_OBSERVATION_BYTES:
                                raise ServiceRefused()
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            return code == 0, bytes(output)
    except Exception:
        raise ServiceRefused() from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def _read_proc(directory: int, leaf: str, limit: int) -> bytes:
    fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        result = bytearray()
        while True:
            chunk = os.read(fd, min(4096, limit + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > limit:
                raise ServiceRefused()
    finally:
        os.close(fd)


class LinuxServiceIO:
    def show(self) -> bytes:
        success, raw = _command(restart=False)
        if not success:
            raise ServiceRefused()
        return raw

    def restart(self) -> bool:
        success, _ = _command(restart=True)
        return success

    def canonical(self, path: Path) -> Path:
        try:
            return path.resolve(strict=True)
        except Exception:
            raise ServiceRefused() from None

    def process(self, pid: int, executable: Path) -> tuple[int, int, int]:
        directory = None
        try:
            if type(pid) is not int or not 0 < pid < 2**31:
                raise ServiceRefused()
            with verified_executable(executable) as (_, identity):
                directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                first = parse_start(_read_proc(directory, "stat", 4096), pid)
                env = _read_proc(directory, "environ", MAX_OBSERVATION_BYTES)
                # Environment aliases must not override the explicit local-mode contract.
                if any(item.partition(b"=")[0] in {b"TUNNEL_TOKEN", b"TUNNEL_TOKEN_FILE", b"TUNNEL_CONFIG"}
                       for item in env.split(b"\x00")):
                    raise ServiceRefused()
                actual = os.stat("exe", dir_fd=directory)
                if (actual.st_dev, actual.st_ino) != identity:
                    raise ServiceRefused()
                second = parse_start(_read_proc(directory, "stat", 4096), pid)
                if first != second:
                    raise ServiceRefused()
                return first, *identity
        except Exception:
            raise ServiceRefused() from None
        finally:
            if directory is not None:
                os.close(directory)
