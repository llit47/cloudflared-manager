import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from cloudflared_manager.deployment import bootstrap

SHA = "b" * 40


def test_install_handoff_imports_without_site_packages() -> None:
    source_root = Path(__file__).parents[1] / "src"
    script = (
        f"import sys; sys.path.insert(0, {str(source_root)!r}); "
        "import cloudflared_manager.deployment.cli"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def _archive(path: Path, *, unsafe_name: str | None = None) -> None:
    root = f"cloudflared-manager-{SHA}"
    files = {
        "pyproject.toml": b"[project]\nname='test'\n",
        "deploy/cloudflared-manager.service": b"[Service]\n",
        "deploy/cloudflared-manager.tmpfiles.conf": b"d /run/cloudflared-manager 0700 root root -\n",
        "deploy/update.sh": b"#!/bin/bash\n",
        "deploy/config.sh": b"#!/bin/bash\n",
        "src/cloudflared_manager/deployment/cli.py": b"# fixture\n",
    }
    with tarfile.open(path, "w:gz") as archive:
        for relative, content in files.items():
            name = unsafe_name if unsafe_name is not None else f"{root}/{relative}"
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if relative.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(content))
            unsafe_name = None


def test_safe_archive_extracts_expected_layout(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    _archive(archive)

    root = bootstrap.extract_archive(archive, tmp_path / "extract", SHA)

    assert root.name == f"cloudflared-manager-{SHA}"
    assert (root / "deploy" / "update.sh").is_file()


@pytest.mark.parametrize(
    "unsafe_name",
    ["../../etc/passwd", f"cloudflared-manager-{SHA}/../outside"],
)
def test_archive_parent_traversal_is_rejected(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    archive = tmp_path / "source.tar.gz"
    _archive(archive, unsafe_name=unsafe_name)

    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.extract_archive(archive, tmp_path / "extract", SHA)


def test_archive_symlink_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    root = f"cloudflared-manager-{SHA}"
    with tarfile.open(archive, "w:gz") as source:
        info = tarfile.TarInfo(f"{root}/deploy/update.sh")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        source.addfile(info)

    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.extract_archive(archive, tmp_path / "extract", SHA)


def test_main_revision_response_requires_exact_sha(monkeypatch) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_curl",
        lambda curl, url: subprocess.CompletedProcess([], 0, json.dumps({"sha": SHA}), ""),
    )
    assert bootstrap.resolve_main_sha("/usr/bin/curl") == SHA

    monkeypatch.setattr(
        bootstrap,
        "_curl",
        lambda curl, url: subprocess.CompletedProcess([], 0, '{"sha":"main"}', ""),
    )
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.resolve_main_sha("/usr/bin/curl")


def test_failed_download_does_not_surface_raw_output(monkeypatch, tmp_path: Path) -> None:
    fake_secret = "TEST_DOWNLOAD_SECRET_MUST_NOT_LEAK"

    def failed_run(*args, **kwargs):
        return subprocess.CompletedProcess([], 22, "", fake_secret)

    monkeypatch.setattr(subprocess, "run", failed_run)

    with pytest.raises(bootstrap.BootstrapError) as captured:
        bootstrap.download_archive(SHA, tmp_path / "archive", "/usr/bin/curl")

    assert fake_secret not in str(captured.value)


def test_curl_disables_ambient_config_before_all_other_options(monkeypatch) -> None:
    calls: list[list[str]] = []

    def successful_run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", successful_run)

    bootstrap._curl("/usr/bin/curl", "https://example.com/archive")

    assert calls[0][0:2] == ["/usr/bin/curl", "--disable"]
    assert "--proto" in calls[0]
    assert "=https" in calls[0]
    assert "--tlsv1.2" in calls[0]
    assert "--location" in calls[0]
    assert "--retry" in calls[0]
    assert "--connect-timeout" in calls[0]
    assert "--max-time" in calls[0]
