import os
import stat
from pathlib import Path

import pytest

from cloudflared_manager.cloudflared.editing import (
    CandidateFileError,
    CandidateFileStager,
    read_config_source_snapshot,
)


SOURCE = b"ingress:\n  - service: http_status:404\n"
CANDIDATE = (
    b"ingress:\n"
    b"  - hostname: app.example.com\n"
    b"    service: http://localhost:8000\n"
    b"  - service: http_status:404\n"
)


def make_snapshot(tmp_path: Path):
    source = tmp_path / "config.yml"
    source.write_bytes(SOURCE)
    return source, read_config_source_snapshot(source)


def test_candidates_are_unique_restrictive_and_beside_source(tmp_path: Path) -> None:
    source, snapshot = make_snapshot(tmp_path)
    first = CandidateFileStager().stage(snapshot, CANDIDATE)
    second = CandidateFileStager().stage(snapshot, CANDIDATE)
    first_path = first.path
    second_path = second.path
    try:
        assert first_path != second_path
        assert first_path.parent == source.parent
        assert second_path.parent == source.parent
        assert first_path.read_bytes() == CANDIDATE
        assert second_path.read_bytes() == CANDIDATE
        assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(second_path.stat().st_mode) == 0o600
        assert source.read_bytes() == SOURCE
    finally:
        first.discard()
        second.discard()
    assert not first_path.exists()
    assert not second_path.exists()
    assert source.read_bytes() == SOURCE


def test_partial_write_failure_cleans_candidate_and_preserves_source(
    tmp_path: Path,
) -> None:
    source, snapshot = make_snapshot(tmp_path)
    calls = 0

    def failed_write(descriptor: int, contents: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return os.write(descriptor, contents[:1])
        raise OSError("injected secret-bearing failure")

    stager = CandidateFileStager(write=failed_write)

    with pytest.raises(CandidateFileError) as captured:
        stager.stage(snapshot, CANDIDATE)

    assert "secret-bearing" not in str(captured.value)
    assert source.read_bytes() == SOURCE
    assert list(tmp_path.glob(".cfm-candidate-*.yaml")) == []


def test_fsync_failure_cleans_candidate_and_preserves_source(tmp_path: Path) -> None:
    source, snapshot = make_snapshot(tmp_path)

    def failed_fsync(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    with pytest.raises(CandidateFileError):
        CandidateFileStager(fsync=failed_fsync).stage(snapshot, CANDIDATE)

    assert source.read_bytes() == SOURCE
    assert list(tmp_path.glob(".cfm-candidate-*.yaml")) == []


def test_close_failure_cleans_candidate_and_preserves_source(tmp_path: Path) -> None:
    source, snapshot = make_snapshot(tmp_path)
    called = False

    def failed_close(descriptor: int) -> None:
        nonlocal called
        if not called:
            called = True
            os.close(descriptor)
            raise OSError("injected close failure")
        os.close(descriptor)

    with pytest.raises(CandidateFileError):
        CandidateFileStager(close=failed_close).stage(snapshot, CANDIDATE)

    assert source.read_bytes() == SOURCE
    assert list(tmp_path.glob(".cfm-candidate-*.yaml")) == []


def test_exclusive_creation_does_not_follow_or_clobber_symlink(
    tmp_path: Path,
) -> None:
    source, snapshot = make_snapshot(tmp_path)
    token = "a" * 32
    collision = tmp_path / f".cfm-candidate-{token}.yaml"
    collision.symlink_to(source)
    stager = CandidateFileStager(token_factory=lambda _: token, attempts=1)

    with pytest.raises(CandidateFileError, match="unique"):
        stager.stage(snapshot, CANDIDATE)

    assert collision.is_symlink()
    assert source.read_bytes() == SOURCE


def test_group_writable_candidate_directory_fails_closed(tmp_path: Path) -> None:
    source, _ = make_snapshot(tmp_path)
    original_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    tmp_path.chmod(original_mode | stat.S_IWGRP)
    try:
        snapshot = read_config_source_snapshot(source)
        with pytest.raises(CandidateFileError, match="untrusted"):
            CandidateFileStager().stage(snapshot, CANDIDATE)
    finally:
        tmp_path.chmod(original_mode)

    assert source.read_bytes() == SOURCE
    assert list(tmp_path.glob(".cfm-candidate-*.yaml")) == []


def test_candidate_identity_change_fails_closed_without_touching_source(
    tmp_path: Path,
) -> None:
    source, snapshot = make_snapshot(tmp_path)
    candidate = CandidateFileStager().stage(snapshot, CANDIDATE)
    candidate_path = candidate.path
    candidate_path.unlink()
    candidate_path.symlink_to(source)

    with pytest.raises(CandidateFileError, match="identity|available"):
        candidate.require_intact()
    with pytest.raises(CandidateFileError, match="cleaned"):
        candidate.discard()

    assert candidate_path.is_symlink()
    assert source.read_bytes() == SOURCE


def test_candidate_content_change_with_same_inode_fails_closed(tmp_path: Path) -> None:
    source, snapshot = make_snapshot(tmp_path)
    candidate = CandidateFileStager().stage(snapshot, CANDIDATE)
    candidate_path = candidate.path
    replacement = CANDIDATE.replace(b"app.example.com", b"bad.example.com")
    assert len(replacement) == len(CANDIDATE)
    candidate_path.write_bytes(replacement)
    candidate_path.chmod(0o600)

    with pytest.raises(CandidateFileError, match="contents"):
        candidate.require_intact()

    candidate.discard()
    assert not candidate_path.exists()
    assert source.read_bytes() == SOURCE
