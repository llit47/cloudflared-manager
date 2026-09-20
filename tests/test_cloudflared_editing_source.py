import os
import stat
from pathlib import Path

import pytest

from cloudflared_manager.cloudflared.editing import (
    SourceConfigChangedError,
    SourceConfigUnreadableError,
    read_config_source_snapshot,
    require_source_unchanged,
)
from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES


def test_snapshot_captures_immutable_bytes_digest_and_file_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private-config.yml"
    contents = b"ingress:\n  - service: http_status:404\n"
    path.write_bytes(contents)
    path.chmod(0o640)
    metadata = path.stat()

    snapshot = read_config_source_snapshot(path)

    assert snapshot.original_bytes == contents
    assert snapshot.sha256 == "e9cf854e46bd6a852b198c5b6898ade2ef4c6c99ca3a5867272db4a471df037e"
    assert snapshot.size == len(contents)
    assert (snapshot.device, snapshot.inode) == (metadata.st_dev, metadata.st_ino)
    assert snapshot.permission_mode == 0o640
    assert (snapshot.uid, snapshot.gid) == (metadata.st_uid, metadata.st_gid)
    assert (snapshot.parent_device, snapshot.parent_inode) == (
        tmp_path.stat().st_dev,
        tmp_path.stat().st_ino,
    )
    assert "private-config" not in repr(snapshot)
    assert "http_status" not in repr(snapshot)


def test_source_change_is_detected_by_content_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("ingress:\n  - service: http_status:404\n", encoding="utf-8")
    snapshot = read_config_source_snapshot(path)
    path.write_text("ingress:\n  - service: http_status:503\n", encoding="utf-8")

    with pytest.raises(SourceConfigChangedError, match="changed"):
        require_source_unchanged(snapshot)


def test_source_symlink_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "real.yml"
    target.write_text("ingress:\n  - service: http_status:404\n", encoding="utf-8")
    link = tmp_path / "config.yml"
    link.symlink_to(target)

    with pytest.raises(SourceConfigUnreadableError, match="canonical"):
        read_config_source_snapshot(link)


def test_missing_source_error_does_not_disclose_path(tmp_path: Path) -> None:
    missing = tmp_path / "secret-token-config.yml"

    with pytest.raises(SourceConfigUnreadableError) as captured:
        read_config_source_snapshot(missing)

    assert str(missing) not in str(captured.value)
    assert "secret-token" not in str(captured.value)


def test_symlinked_parent_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    source = real / "config.yml"
    source.write_text("ingress:\n  - service: http_status:404\n", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(SourceConfigUnreadableError, match="canonical"):
        read_config_source_snapshot(alias / "config.yml")


def test_non_regular_and_oversized_sources_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigUnreadableError, match="regular file"):
        read_config_source_snapshot(tmp_path)

    path = tmp_path / "large.yml"
    path.write_bytes(b"x" * (MAX_CLOUDFLARED_CONFIG_BYTES + 1))
    with pytest.raises(SourceConfigUnreadableError, match="size limit"):
        read_config_source_snapshot(path)


def test_snapshot_is_frozen(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("ingress:\n  - service: http_status:404\n", encoding="utf-8")
    snapshot = read_config_source_snapshot(path)

    with pytest.raises((AttributeError, TypeError)):
        snapshot.size = 0  # type: ignore[misc]

    assert stat.S_ISREG(os.lstat(path).st_mode)
