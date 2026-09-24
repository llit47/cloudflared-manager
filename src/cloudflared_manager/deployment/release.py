"""Release preparation and atomic manager-owned filesystem operations."""

from __future__ import annotations

import fcntl
import ctypes
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from cloudflared_manager.deployment.errors import (
    HostOperationError,
    RollbackError,
    UpdateLockedError,
)
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.environment import require_safe_environment
from cloudflared_manager.deployment.validation import validate_sha

READY_MARKER = ".release-ready"
INCOMPLETE_MARKER = ".release-incomplete"
DEPLOYMENT_MARKER = ".cloudflared-manager-owned"
DEPLOYMENT_MARKER_CONTENT = b"cloudflared-manager deployment v1\n"
_TMPFILES_ASSET = "deploy/cloudflared-manager.tmpfiles.conf"
_TMPFILES_RULE = b"d /run/cloudflared-manager 0700 root root -\n"


class ProcessRunner(Protocol):
    def __call__(self, arguments: list[str], timeout: float) -> int:
        """Run one controlled candidate-preparation command."""


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    kind: str
    content: bytes | None = None
    target: str | None = None
    mode: int = 0o644


class DeploymentLock:
    """Non-blocking advisory lock for the fixed update path."""

    def __init__(self, path: Path, *, owner: tuple[int, int] | None = (0, 0)) -> None:
        self._path = path
        self._owner = owner
        self._stream: BinaryIO | None = None

    def __enter__(self) -> DeploymentLock:
        parent_existed = self._path.parent.exists() or self._path.parent.is_symlink()
        if not parent_existed:
            try:
                self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
                if self._owner is not None:
                    os.chown(self._path.parent, *self._owner)
            except OSError as error:
                raise UpdateLockedError("The manager update lock directory is unavailable.") from error
        parent_metadata = self._path.parent.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or (self._owner is not None and (
                parent_metadata.st_uid, parent_metadata.st_gid
            ) != self._owner)
        ):
            raise UpdateLockedError("The manager update lock directory is unsafe.")
        lock_existed = self._path.exists() or self._path.is_symlink()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as error:
            raise UpdateLockedError("The manager update lock is unavailable.") from error
        stream = os.fdopen(descriptor, "a+b")
        metadata = os.fstat(stream.fileno())
        if not lock_existed:
            os.fchmod(stream.fileno(), 0o600)
            if self._owner is not None:
                os.fchown(stream.fileno(), *self._owner)
            metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (self._owner is not None and (metadata.st_uid, metadata.st_gid) != self._owner)
        ):
            stream.close()
            raise UpdateLockedError("The manager update lock is unsafe.")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            stream.close()
            raise UpdateLockedError("Another Cloudflared Manager update is running.") from error
        self._stream = stream
        return self

    def __exit__(self, *_: object) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class ReleaseFilesystem:
    """Own fixed deployment paths without granting the service user write access."""

    def __init__(
        self,
        paths: DeploymentPaths,
        *,
        owner: tuple[int, int] | None = (0, 0),
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.paths = paths
        self.owner = owner
        self._process_runner = process_runner or _run_process

    def ensure_layout(self) -> None:
        self._ensure_install_root()
        self._ensure_owned_releases()
        self._ensure_directory(self.paths.config_root, 0o750)
        self._ensure_system_directory(self.paths.update_link.parent)

    def require_owned_layout(self) -> None:
        """Reject an install-root collision without the exact ownership marker."""

        root = self.paths.install_root
        marker = root / DEPLOYMENT_MARKER
        try:
            root_metadata = root.lstat()
            marker_metadata = marker.lstat()
        except OSError as error:
            raise HostOperationError(
                "The existing manager installation is not recognizable as manager-owned."
            ) from error
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_mode & 0o022
            or stat.S_ISLNK(marker_metadata.st_mode)
            or not stat.S_ISREG(marker_metadata.st_mode)
            or marker_metadata.st_mode & 0o022
        ):
            raise HostOperationError(
                "The existing manager installation is not recognizable as manager-owned."
            )
        try:
            content = marker.read_bytes()
        except OSError as error:
            raise HostOperationError(
                "The existing manager installation is not recognizable as manager-owned."
            ) from error
        if content != DEPLOYMENT_MARKER_CONTENT:
            raise HostOperationError(
                "The existing manager installation is not recognizable as manager-owned."
            )
        if self.owner is not None and (
            root_metadata.st_uid != self.owner[0]
            or root_metadata.st_gid != self.owner[1]
            or marker_metadata.st_uid != self.owner[0]
            or marker_metadata.st_gid != self.owner[1]
        ):
            raise HostOperationError(
                "The existing manager installation is not recognizable as manager-owned."
            )

    def prepare_release(self, source: Path, sha: str, python: Path) -> Path:
        """Build a release-local venv directly at its final immutable path."""

        self._require_owned_releases()
        revision = validate_sha(sha)
        target = self.paths.release(revision)
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
            raise HostOperationError("The selected Python interpreter is unavailable.")
        if self._is_ready_release(target, revision):
            return target
        if target.exists() or target.is_symlink():
            if self.read_current_sha(required=False) == revision:
                raise HostOperationError("The active release is incomplete and cannot be replaced.")
            self._remove_release(target)

        self._validate_source(source)
        try:
            shutil.copytree(source, target, symlinks=False)
            (target / INCOMPLETE_MARKER).write_text(revision + "\n", encoding="ascii")
            venv = target / ".venv"
            self._require_process([str(python), "-I", "-m", "venv", str(venv)], timeout=120)
            venv_python = venv / "bin" / "python"
            self._require_process(
                [
                    str(venv_python),
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-cache-dir",
                    str(target),
                ],
                timeout=600,
            )
            self._require_process(
                [str(venv_python), "-I", "-m", "cloudflared_manager.deployment.preflight"],
                timeout=30,
            )
            executable = venv / "bin" / "cloudflared-manager"
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise HostOperationError("The candidate manager executable is missing.")
            self._harden_tree(target)
            (target / INCOMPLETE_MARKER).unlink()
            self.atomic_write(
                target / READY_MARKER,
                (revision + "\n").encode("ascii"),
                0o644,
            )
            return target
        except (OSError, shutil.Error) as error:
            raise HostOperationError("The candidate release could not be prepared.") from error

    def read_current_sha(self, *, required: bool = True) -> str | None:
        self._require_owned_releases()
        current = self.paths.current
        if not current.exists() and not current.is_symlink():
            if required:
                raise HostOperationError("No active Cloudflared Manager release was found.")
            return None
        if not current.is_symlink():
            raise HostOperationError("The current release path is not a symlink.")
        target = PurePosixPath(os.readlink(current))
        if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
            raise HostOperationError("The current release link has an unsafe target.")
        try:
            revision = validate_sha(target.parts[1])
        except Exception as error:
            raise HostOperationError("The current release link is malformed.") from error
        if not self._is_ready_release(self.paths.release(revision), revision):
            raise HostOperationError("The current release is not complete.")
        return revision

    def switch_current(self, sha: str) -> str | None:
        revision = validate_sha(sha)
        target = self.paths.release(revision)
        if not self._is_ready_release(target, revision):
            raise HostOperationError("The candidate release is not ready.")
        previous = os.readlink(self.paths.current) if self.paths.current.is_symlink() else None
        if self.paths.current.exists() and not self.paths.current.is_symlink():
            raise HostOperationError("The current release path is unsafe.")
        temporary = self.paths.install_root / f".current.{secrets.token_hex(8)}"
        try:
            os.symlink(f"releases/{revision}", temporary)
            os.replace(temporary, self.paths.current)
            self._sync_directory(self.paths.install_root)
        except OSError as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise HostOperationError("The active release could not be switched atomically.") from error
        return previous

    def restore_current(self, target: str | None) -> None:
        if target is None:
            if self.paths.current.is_symlink():
                self.paths.current.unlink()
                self._sync_directory(self.paths.install_root)
            return
        parsed = PurePosixPath(target)
        if parsed.is_absolute() or len(parsed.parts) != 2 or parsed.parts[0] != "releases":
            raise HostOperationError("The rollback release target is unsafe.")
        revision = validate_sha(parsed.parts[1])
        if not self._is_ready_release(self.paths.release(revision), revision):
            raise HostOperationError("The rollback release is unavailable.")
        temporary = self.paths.install_root / f".current.rollback.{secrets.token_hex(8)}"
        try:
            os.symlink(str(parsed), temporary)
            os.replace(temporary, self.paths.current)
            self._sync_directory(self.paths.install_root)
        except OSError as error:
            raise HostOperationError("The previous release could not be restored.") from error

    def snapshot(self, path: Path) -> PathSnapshot:
        if path.is_symlink():
            return PathSnapshot(kind="symlink", target=os.readlink(path))
        if not path.exists():
            return PathSnapshot(kind="missing")
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise HostOperationError("A manager deployment path is not a regular file.")
        return PathSnapshot(
            kind="file",
            content=path.read_bytes(),
            mode=stat.S_IMODE(metadata.st_mode),
        )

    def restore_snapshot(self, path: Path, snapshot: PathSnapshot) -> None:
        if snapshot.kind == "missing":
            if path.is_symlink() or path.exists():
                path.unlink()
                self._sync_directory(path.parent)
            return
        if snapshot.kind == "symlink":
            if snapshot.target is None:
                raise HostOperationError("A deployment symlink backup is invalid.")
            self._atomic_symlink(path, snapshot.target)
            return
        if snapshot.kind == "file" and snapshot.content is not None:
            self.atomic_write(path, snapshot.content, snapshot.mode)
            return
        raise HostOperationError("A deployment file backup is invalid.")

    def install_unit(self, release: Path) -> bool:
        self.require_owned_layout()
        self._require_ready_release(release)
        candidate = release / "deploy" / "cloudflared-manager.service"
        content = candidate.read_bytes()
        self._validate_regular_asset(
            self.paths.unit_path,
            content,
            "deploy/cloudflared-manager.service",
        )
        if self._regular_asset_matches(self.paths.unit_path, content, 0o644):
            return False
        self.atomic_write(self.paths.unit_path, content, 0o644)
        return True

    def install_runtime_tmpfiles(self, release: Path) -> bool:
        """Install the fixed boot rule and apply it before starting the service."""

        self.require_owned_layout()
        self._require_ready_release(release)
        source = release / _TMPFILES_ASSET
        if not source.exists() and not source.is_symlink():
            if b"/run/cloudflared-manager" in (release / "deploy/cloudflared-manager.service").read_bytes():
                raise HostOperationError("The release lacks its required runtime directory rule.")
            return False
        if source.read_bytes() != _TMPFILES_RULE:
            raise HostOperationError("The release has an unsafe runtime directory rule.")
        target = self.paths.tmpfiles_path
        self._ensure_system_directory(target.parent)
        parent = target.parent.lstat()
        if (parent.st_mode & 0o022 or (self.owner is not None
            and (parent.st_uid, parent.st_gid) != self.owner)):
            raise HostOperationError("The tmpfiles configuration directory is unsafe.")
        self._validate_regular_asset(target, _TMPFILES_RULE, _TMPFILES_ASSET)
        self._ensure_system_directory(self.paths.runtime_root.parent)
        existed = self._require_safe_runtime_root(required=False)
        changed = not self._regular_asset_matches(target, _TMPFILES_RULE, 0o644)
        previous = self.snapshot(target) if changed else None
        try:
            if changed:
                self.atomic_write(target, _TMPFILES_RULE, 0o644)
            result = subprocess.run(
                [str(self.paths.tmpfiles_executable), "--create", str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10, check=False, shell=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
            )
            if result.returncode != 0:
                raise HostOperationError("The runtime directory could not be created.")
            self._require_safe_runtime_root(required=True)
        except Exception as error:
            if changed and previous is not None:
                try:
                    self.restore_snapshot(target, previous)
                except Exception:
                    raise RollbackError("The runtime rule could not be restored.") from error
            if isinstance(error, (OSError, subprocess.TimeoutExpired)):
                raise HostOperationError("The runtime directory could not be created.") from None
            raise
        return changed or not existed

    def _require_safe_runtime_root(self, *, required: bool) -> bool:
        path = self.paths.runtime_root
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022
            or (self.owner is not None and (parent.st_uid, parent.st_gid) != self.owner)):
            raise HostOperationError("The runtime directory parent is unsafe.")
        if not path.exists() and not path.is_symlink():
            if required:
                raise HostOperationError("The runtime directory is unavailable.")
            return False
        info = path.lstat()
        try:
            unsupported = any(name.startswith("system.")
                              for name in os.listxattr(path, follow_symlinks=False))
        except OSError:
            raise HostOperationError("The runtime directory metadata is unavailable.") from None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or (self.owner is not None and (info.st_uid, info.st_gid) != self.owner)
            or unsupported):
            raise HostOperationError("The runtime directory is unsafe.")
        return True

    def validate_deployment_assets(self, release: Path) -> None:
        """Preflight every external manager-owned path before mutating any of them."""

        self.require_owned_layout()
        self._require_ready_release(release)
        candidates = [
            (
                self.paths.unit_path,
                release / "deploy" / "cloudflared-manager.service",
                "deploy/cloudflared-manager.service",
            ),
            (self.paths.stable_update, release / "deploy" / "update.sh", "deploy/update.sh"),
            (self.paths.stable_config, release / "deploy" / "config.sh", "deploy/config.sh"),
        ]
        tmpfiles_source = release / _TMPFILES_ASSET
        if tmpfiles_source.exists() or tmpfiles_source.is_symlink():
            candidates.append((self.paths.tmpfiles_path, tmpfiles_source, _TMPFILES_ASSET))
        elif b"/run/cloudflared-manager" in (release / "deploy/cloudflared-manager.service").read_bytes():
            raise HostOperationError("The release lacks its required runtime directory rule.")
        mutation_assets = (
            (self.paths.mutation_helper_path, release / "deploy/privileged-mutation-helper.sh",
             "deploy/privileged-mutation-helper.sh"),
            (self.paths.mutation_sudoers_path, release / "deploy/cloudflared-manager-mutation-bridge.sudoers",
             "deploy/cloudflared-manager-mutation-bridge.sudoers"),
        )
        present = [asset.exists() or asset.is_symlink() for _, asset, _ in mutation_assets]
        if (release / "src/cloudflared_manager/activation/mutation_helper.py").exists() and not all(present):
            raise HostOperationError("The release lacks its required mutation bridge assets.")
        if any(present) and not all(present):
            raise HostOperationError("The release has an incomplete mutation bridge asset pair.")
        if all(present):
            for target, asset, relative in mutation_assets:
                metadata = asset.lstat()
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022
                    or (self.owner is not None and (metadata.st_uid, metadata.st_gid) != self.owner)):
                    raise HostOperationError("The release has an unsafe mutation bridge asset.")
                candidates.append((target, asset, relative))
        for target, candidate, relative in candidates:
            self._validate_regular_asset(target, candidate.read_bytes(), relative)
        self._validate_command_link(self.paths.update_link, self.paths.stable_update)
        self._validate_command_link(self.paths.config_link, self.paths.stable_config)

    def validate_first_install_paths(self, sha: str | None = None) -> None:
        """Reject external collisions except a provable pre-current unit write."""

        owned_root = self.paths.install_root.exists() or self.paths.install_root.is_symlink()
        if owned_root:
            self.require_owned_layout()

        environment = self.paths.environment_file
        if environment.exists() or environment.is_symlink():
            if not owned_root or sha is None:
                raise HostOperationError("First installation collides with an unowned environment file.")
            revision = validate_sha(sha)
            if not self._is_ready_release(self.paths.release(revision), revision):
                raise HostOperationError("The existing environment has no prepared manager release.")
            require_safe_environment(environment, owner=self.owner)

        unit_exists = self.paths.unit_path.exists() or self.paths.unit_path.is_symlink()
        if unit_exists:
            recoverable_unit = False
            if owned_root and sha is not None:
                revision = validate_sha(sha)
                release = self.paths.release(revision)
                if self._is_ready_release(release, revision):
                    candidate = release / "deploy" / "cloudflared-manager.service"
                    try:
                        content = candidate.read_bytes()
                    except OSError:
                        content = b""
                    recoverable_unit = bool(content) and self._regular_asset_matches(
                        self.paths.unit_path,
                        content,
                        0o644,
                    )
            if not recoverable_unit:
                raise HostOperationError(
                    "First installation collides with an existing manager-named deployment path."
                )

        for path in (
            self.paths.stable_update,
            self.paths.stable_config,
            self.paths.update_link,
            self.paths.config_link,
        ):
            if path.exists() or path.is_symlink():
                raise HostOperationError(
                    "First installation collides with an existing manager-named deployment path."
                )

    def install_stable_administration(self, release: Path) -> bool:
        """Replace stable scripts and links only after candidate health succeeds."""

        self.validate_deployment_assets(release)
        targets = (
            (self.paths.stable_update, release / "deploy" / "update.sh"),
            (self.paths.stable_config, release / "deploy" / "config.sh"),
        )
        expected_links = (
            (self.paths.update_link, self.paths.stable_update),
            (self.paths.config_link, self.paths.stable_config),
        )
        content_changed = any(
            not self._regular_asset_matches(target, source.read_bytes(), 0o755)
            for target, source in targets
        )
        links_changed = any(
            not self._command_link_matches(link, target)
            for link, target in expected_links
        )
        if not content_changed and not links_changed:
            return False
        snapshots = {path: self.snapshot(path) for path, _ in targets}
        link_snapshots = {
            self.paths.update_link: self.snapshot(self.paths.update_link),
            self.paths.config_link: self.snapshot(self.paths.config_link),
        }
        try:
            for target, source in targets:
                self.atomic_write(target, source.read_bytes(), 0o755)
            self._atomic_symlink(self.paths.update_link, str(self.paths.stable_update))
            self._atomic_symlink(self.paths.config_link, str(self.paths.stable_config))
        except (OSError, HostOperationError) as error:
            rollback_errors: list[Exception] = []
            for path, snapshot in snapshots.items():
                try:
                    self.restore_snapshot(path, snapshot)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            for path, snapshot in link_snapshots.items():
                try:
                    self.restore_snapshot(path, snapshot)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise RollbackError(
                    "Stable manager administration scripts could not be restored."
                ) from error
            raise HostOperationError(
                "Stable manager administration scripts could not be installed."
            ) from error
        return True

    def atomic_write(self, path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        parent_metadata = path.parent.lstat()
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
            raise HostOperationError("A manager deployment directory is unsafe.")
        if path.is_symlink():
            raise HostOperationError("Refusing to replace a symlinked deployment file.")
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.fchmod(descriptor, mode)
            if self.owner is not None:
                os.fchown(descriptor, self.owner[0], self.owner[1])
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
            self._sync_directory(path.parent)
        except OSError as error:
            raise HostOperationError("A manager deployment file could not be installed safely.") from error
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _atomic_symlink(self, path: Path, target: str) -> None:
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}"
        try:
            os.symlink(target, temporary)
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        except OSError as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise HostOperationError("A manager command link could not be installed safely.") from error

    def _ensure_directory(self, path: Path, mode: int) -> None:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_mode & 0o022
                or (self.owner is not None and (metadata.st_uid, metadata.st_gid) != self.owner)
            ):
                raise HostOperationError("A manager deployment directory is unsafe.")
            return
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostOperationError("A manager deployment path is not a directory.")
        os.chmod(path, mode)
        if self.owner is not None:
            os.chown(path, self.owner[0], self.owner[1])

    def _ensure_install_root(self) -> None:
        root = self.paths.install_root
        if root.exists() or root.is_symlink():
            self.require_owned_layout()
            return
        temporary: Path | None = None
        try:
            self._ensure_system_directory(root.parent)
            metadata = root.parent.lstat()
            if metadata.st_mode & 0o022 or (
                self.owner is not None and (metadata.st_uid, metadata.st_gid) != self.owner
            ):
                raise HostOperationError("The manager installation parent is unsafe.")
            temporary = Path(tempfile.mkdtemp(prefix=".cloudflared-manager.", dir=root.parent))
            if self.owner is not None:
                os.chown(temporary, *self.owner)
            self.atomic_write(temporary / DEPLOYMENT_MARKER, DEPLOYMENT_MARKER_CONTENT, 0o644)
            os.chmod(temporary, 0o755)
            self._sync_directory(temporary)
            self._publish_install_root(temporary, root)
            temporary = None
            self._sync_directory(root.parent)
        except OSError as error:
            raise HostOperationError("The manager installation root could not be created.") from error
        finally:
            if temporary is not None and temporary.exists():
                # Only this invocation's private staging directory is removable.
                shutil.rmtree(temporary)

    @staticmethod
    def _publish_install_root(temporary: Path, root: Path) -> None:
        """Linux atomic rename with RENAME_NOREPLACE: never replace a collision."""

        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise HostOperationError("Atomic manager root publication is unavailable.")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temporary), -100, os.fsencode(root), 1) != 0:
            raise OSError(ctypes.get_errno(), "Manager root publication failed")

    def _ensure_owned_releases(self) -> None:
        releases = self.paths.releases
        if not releases.exists() and not releases.is_symlink():
            try:
                releases.mkdir(mode=0o755, exist_ok=False)
                os.chmod(releases, 0o755)
                if self.owner is not None:
                    os.chown(releases, self.owner[0], self.owner[1])
            except OSError as error:
                raise HostOperationError(
                    "The manager releases directory could not be created safely."
                ) from error
        self._require_owned_releases()

    def _require_owned_releases(self) -> None:
        try:
            metadata = self.paths.releases.lstat()
        except OSError as error:
            raise HostOperationError("The manager releases directory is unsafe.") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o022
            or (
                self.owner is not None
                and (metadata.st_uid, metadata.st_gid) != self.owner
            )
        ):
            raise HostOperationError("The manager releases directory is unsafe.")

    @staticmethod
    def _ensure_system_directory(path: Path) -> None:
        existed = path.exists()
        if path.is_symlink():
            raise HostOperationError("A system command directory is a symlink.")
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostOperationError("A system command path is not a directory.")
        if not existed:
            os.chmod(path, 0o755)

    def _validate_source(self, source: Path) -> None:
        if source.is_symlink() or not source.is_dir():
            raise HostOperationError("The candidate source directory is unsafe.")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise HostOperationError("The candidate source contains an unsupported symlink.")
        for relative in (
            "pyproject.toml",
            "deploy/cloudflared-manager.service",
            _TMPFILES_ASSET,
            "deploy/update.sh",
            "deploy/config.sh",
        ):
            path = source / relative
            if path.is_symlink() or not path.is_file():
                raise HostOperationError("The candidate source layout is incomplete.")

    def _require_ready_release(self, release: Path) -> str:
        try:
            revision = validate_sha(release.name)
        except Exception as error:
            raise HostOperationError("The manager release path is invalid.") from error
        if release != self.paths.release(revision) or not self._is_ready_release(release, revision):
            raise HostOperationError("The manager release is not ready for deployment.")
        return revision

    def _validate_regular_asset(self, target: Path, candidate: bytes, relative: str) -> None:
        if not target.exists() and not target.is_symlink():
            return
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise HostOperationError(
                "A manager deployment path collides with an unmanaged filesystem object."
            )
        if metadata.st_mode & 0o022 or (
            self.owner is not None
            and (metadata.st_uid, metadata.st_gid) != self.owner
        ):
            raise HostOperationError(
                "A manager deployment path has unsafe ownership or permissions."
            )
        try:
            existing = target.read_bytes()
        except OSError as error:
            raise HostOperationError("A manager deployment path cannot be inspected safely.") from error
        if existing == candidate or existing in self._known_release_contents(relative):
            return
        raise HostOperationError(
            "A manager deployment path collides with unrecognized existing content."
        )

    def _validate_command_link(self, link: Path, target: Path) -> None:
        if not link.exists() and not link.is_symlink():
            return
        metadata = link.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            and os.readlink(link) == str(target)
            and (
                self.owner is None
                or (metadata.st_uid, metadata.st_gid) == self.owner
            )
        ):
            return
        raise HostOperationError(
            "A manager command path collides with an unmanaged filesystem object."
        )

    def _regular_asset_matches(self, path: Path, content: bytes, mode: int) -> bool:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return False
            if path.read_bytes() != content or stat.S_IMODE(metadata.st_mode) != mode:
                return False
        except OSError:
            return False
        return self.owner is None or (
            metadata.st_uid == self.owner[0] and metadata.st_gid == self.owner[1]
        )

    def _command_link_matches(self, link: Path, target: Path) -> bool:
        try:
            metadata = link.lstat()
            if not stat.S_ISLNK(metadata.st_mode) or os.readlink(link) != str(target):
                return False
        except OSError:
            return False
        return self.owner is None or (
            metadata.st_uid == self.owner[0] and metadata.st_gid == self.owner[1]
        )

    def _known_release_contents(self, relative: str) -> set[bytes]:
        self._require_owned_releases()
        contents: set[bytes] = set()
        try:
            releases = tuple(self.paths.releases.iterdir())
        except OSError as error:
            raise HostOperationError("Installed manager releases cannot be inspected safely.") from error
        for release in releases:
            try:
                revision = validate_sha(release.name)
            except Exception:
                continue
            if not self._is_ready_release(release, revision):
                continue
            asset = release / relative
            try:
                metadata = asset.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    contents.add(asset.read_bytes())
            except OSError:
                continue
        return contents

    def _is_ready_release(self, path: Path, sha: str) -> bool:
        self._require_owned_releases()
        marker_path = path / READY_MARKER
        executable = path / ".venv" / "bin" / "cloudflared-manager"
        try:
            path_metadata = path.lstat()
            marker_metadata = marker_path.lstat()
            executable_metadata = executable.lstat()
        except (OSError, UnicodeError):
            return False
        if (
            stat.S_ISLNK(path_metadata.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
            or stat.S_ISLNK(marker_metadata.st_mode)
            or not stat.S_ISREG(marker_metadata.st_mode)
            or stat.S_ISLNK(executable_metadata.st_mode)
            or not stat.S_ISREG(executable_metadata.st_mode)
            or path_metadata.st_mode & 0o022
            or marker_metadata.st_mode & 0o022
            or executable_metadata.st_mode & 0o022
        ):
            return False
        try:
            marker = marker_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        return (
            marker == sha
            and (
                self.owner is None
                or (
                    path_metadata.st_uid == self.owner[0]
                    and path_metadata.st_gid == self.owner[1]
                    and marker_metadata.st_uid == self.owner[0]
                    and marker_metadata.st_gid == self.owner[1]
                    and executable_metadata.st_uid == self.owner[0]
                    and executable_metadata.st_gid == self.owner[1]
                )
            )
        )

    def _remove_release(self, path: Path) -> None:
        self._require_owned_releases()
        expected_parent = self.paths.releases.resolve()
        if path.is_symlink() or path.parent.resolve() != expected_parent:
            raise HostOperationError("Refusing to remove an unsafe release path.")
        shutil.rmtree(path)

    def _require_process(self, arguments: list[str], timeout: float) -> None:
        if self._process_runner(arguments, timeout) != 0:
            raise HostOperationError("Candidate release preparation failed.")

    def _harden_tree(self, root: Path) -> None:
        for directory, names, files in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            if not directory_path.is_symlink():
                os.chmod(directory_path, 0o755)
                if self.owner is not None:
                    os.chown(directory_path, *self.owner)
            for name in [*names, *files]:
                path = directory_path / name
                if path.is_symlink():
                    continue
                metadata = path.lstat()
                mode = stat.S_IMODE(metadata.st_mode) & ~0o022
                os.chmod(path, mode)
                if self.owner is not None:
                    os.chown(path, *self.owner)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _run_process(arguments: list[str], timeout: float) -> int:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON"))
    }
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            env=environment,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HostOperationError("Candidate release preparation could not run.") from error
    return result.returncode
