"""Standalone exact-revision source bootstrap.

This module deliberately imports only the standard library until a verified
source archive has been extracted.  The root ``install.sh`` downloads this file
from an already resolved commit SHA, then executes it to validate the archive
before any repository code from that archive is imported.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

REPOSITORY = "llit47/cloudflared-manager"
MAIN_API_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
RAW_BASE_URL = f"https://raw.githubusercontent.com/{REPOSITORY}"
ARCHIVE_BASE_URL = f"https://codeload.github.com/{REPOSITORY}/tar.gz"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
_SHA = re.compile(r"^[0-9a-f]{40}$")


class BootstrapError(RuntimeError):
    """A sanitized source bootstrap failure."""


def validate_exact_sha(value: str) -> str:
    normalized = value.strip().lower()
    if _SHA.fullmatch(normalized) is None:
        raise BootstrapError("GitHub did not return an exact 40-character commit SHA.")
    return normalized


def resolve_main_sha(curl: str = "curl") -> str:
    """Resolve the public main branch to one immutable commit identifier."""

    result = _curl(curl, MAIN_API_URL)
    if len(result.stdout) > 1_048_576:
        raise BootstrapError("The GitHub revision response was unexpectedly large.")
    try:
        payload = json.loads(result.stdout)
        value = payload["sha"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise BootstrapError("The GitHub revision response was malformed.") from error
    if not isinstance(value, str):
        raise BootstrapError("The GitHub revision response did not contain a commit SHA.")
    return validate_exact_sha(value)


def download_archive(sha: str, destination: Path, curl: str = "curl") -> None:
    """Download one validated codeload archive to a new temporary path."""

    revision = validate_exact_sha(sha)
    if destination.exists() or destination.is_symlink():
        raise BootstrapError("The temporary archive destination already exists.")
    _curl(curl, f"{ARCHIVE_BASE_URL}/{revision}", destination)
    try:
        size = destination.stat().st_size
    except OSError as error:
        raise BootstrapError("The downloaded source archive is unavailable.") from error
    if not 0 < size <= MAX_ARCHIVE_BYTES:
        raise BootstrapError("The downloaded source archive has an unsafe size.")


def extract_archive(archive: Path, destination: Path, sha: str) -> Path:
    """Extract a regular-file-only GitHub archive after complete path checks."""

    revision = validate_exact_sha(sha)
    expected_root = f"cloudflared-manager-{revision}"
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    total_size = 0
    try:
        with tarfile.open(archive, mode="r:gz") as source:
            members = source.getmembers()
            if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                raise BootstrapError("The source archive has an unexpected layout.")
            for member in members:
                relative = _validated_member(member, expected_root)
                total_size += member.size
                if total_size > MAX_EXTRACTED_BYTES:
                    raise BootstrapError("The source archive expands beyond the safety limit.")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                extracted = source.extractfile(member)
                if extracted is None:
                    raise BootstrapError("The source archive contains an unreadable file.")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o755 if member.mode & 0o111 else 0o644)
                with extracted, os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(extracted, output)
        root = destination / expected_root
        for required in (
            root / "pyproject.toml",
            root / "deploy" / "cloudflared-manager.service",
            root / "deploy" / "update.sh",
            root / "deploy" / "config.sh",
            root / "src" / "cloudflared_manager" / "deployment" / "cli.py",
        ):
            if not required.is_file() or required.is_symlink():
                raise BootstrapError("The source archive is missing required deployment files.")
        return root
    except (OSError, tarfile.TarError) as error:
        raise BootstrapError("The source archive could not be validated and extracted.") from error


@contextlib.contextmanager
def downloaded_source(sha: str, curl: str = "curl") -> Iterator[Path]:
    """Yield an exact validated source tree and remove temporary data afterward."""

    with tempfile.TemporaryDirectory(prefix="cloudflared-manager-source-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar.gz"
        download_archive(sha, archive, curl)
        yield extract_archive(archive, root / "extracted", sha)


def _validated_member(member: tarfile.TarInfo, expected_root: str) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or path.parts[0] != expected_root:
        raise BootstrapError("The source archive contains an unexpected path.")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise BootstrapError("The source archive contains an unsafe path.")
    if not (member.isdir() or member.isfile()) or member.issym() or member.islnk():
        raise BootstrapError("The source archive contains an unsupported entry type.")
    return path


def _curl(curl: str, url: str, destination: Path | None = None) -> subprocess.CompletedProcess[str]:
    executable = Path(curl)
    if executable.name != "curl":
        raise BootstrapError("The source downloader is unavailable.")
    arguments = [
        str(executable),
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--connect-timeout",
        "10",
        "--max-time",
        "60",
        "--retry",
        "3",
        "--retry-all-errors",
    ]
    if destination is not None:
        arguments.extend(("--output", str(destination)))
    arguments.extend(("--", url))
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=75,
            shell=False,
        )
    except OSError as error:
        raise BootstrapError("The HTTPS source download could not be started.") from error
    if result.returncode != 0:
        raise BootstrapError("The HTTPS source download failed.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install an exact Cloudflared Manager release")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--python", required=True)
    arguments = parser.parse_args(argv)
    try:
        sha = validate_exact_sha(arguments.sha)
        python = Path(arguments.python)
        if not python.is_absolute() or not python.is_file():
            raise BootstrapError("The selected Python interpreter path is invalid.")
        curl = shutil.which("curl")
        if curl is None:
            raise BootstrapError("curl is required for the HTTPS source download.")
        with downloaded_source(sha, curl) as source:
            sys.dont_write_bytecode = True
            sys.path.insert(0, str(source / "src"))
            from cloudflared_manager.deployment.cli import install_from_source

            return install_from_source(source, sha, python)
    except BootstrapError as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("Installation failed because of an unexpected deployment error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
