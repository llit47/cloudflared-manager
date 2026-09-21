"""Disposable filesystem fixtures for the internal PR12 transaction."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from cloudflared_manager.activation.journal import BaselineFacts, JournalStore
from cloudflared_manager.activation.barrier import ActivationBarrierError, ActivationRecoveryBarrier
from cloudflared_manager.activation.filesystem import FilesystemRefused, PinnedDirectory
from cloudflared_manager.activation.transaction import ActivationError, FilesystemActivation
from cloudflared_manager.cloudflared.editing import CloudflaredValidationReport
from tests.deployment_support import make_paths

_SOURCE = b"""tunnel: test-tunnel
ingress:
  - hostname: existing.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
"""


class Authority:
    def __init__(self, source: Path) -> None:
        self.source = source

    def current(self) -> tuple[str, Path]:
        return "a" * 40, self.source


class Baseline:
    def observe(self, *, adopted_fingerprint: str, source_digest: str) -> BaselineFacts:
        return BaselineFacts("cloudflared.service", "loaded", "active", "running",
                             1234, 123456, 1, 2, adopted_fingerprint,
                             source_digest, 1000, 0)


class Validator:
    def validate(self, candidate):
        candidate.require_intact()
        return CloudflaredValidationReport()


def insert(document):
    return document.insert_ingress_before_terminal_catch_all(
        {"hostname": "new.example.com", "service": "http://127.0.0.1:9000"}
    )


@pytest.fixture
def fixture(tmp_path):
    source_dir = tmp_path / "cloudflared"
    source_dir.mkdir()
    source_dir.chmod(0o700)
    source = source_dir / "config.yml"
    source.write_bytes(_SOURCE)
    paths = make_paths(tmp_path)
    paths.config_root.mkdir(parents=True)
    (tmp_path / "etc").chmod(0o700)
    paths.config_root.chmod(0o700)
    engine = FilesystemActivation(paths, authority=Authority(source), baseline=Baseline(),
                                  owner=os.getuid(), anchor=tmp_path)
    return source, paths, engine, tmp_path


def test_commit_keeps_exact_backup_and_recovery_journal(fixture):
    source, paths, engine, root = fixture
    result = engine.run(insert, validator=Validator())
    assert result.code == "SERVICE_ACTIVATION_PENDING"
    assert b"new.example.com" in source.read_bytes()
    backup_files = list((paths.config_root / "activation-backups").iterdir())
    assert len(backup_files) == 1
    assert backup_files[0].read_bytes() == _SOURCE
    assert backup_files[0].stat().st_mode & 0o777 == 0o600
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        record = JournalStore(directory, owner=os.getuid()).recover_staging()
    assert record is not None
    assert record.phase == "CONFIG_COMMITTED"
    assert record.source.sha256 == hashlib.sha256(_SOURCE).hexdigest()
    assert record.backup.sha256 == record.source.sha256
    assert record.candidate.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


def test_final_baseline_change_aborts_before_exchange(fixture):
    source, paths, engine, root = fixture

    class ChangingBaseline(Baseline):
        calls = 0

        def observe(self, **kwargs):
            self.calls += 1
            result = super().observe(**kwargs)
            if self.calls == 3:
                return BaselineFacts(
                    result.unit, result.load_state, result.active_state, result.sub_state,
                    result.main_pid + 1, result.process_start_ticks, result.executable_device,
                    result.executable_inode, result.adopted_fingerprint, result.source_digest,
                    result.stable_milliseconds, result.restart_counter,
                )
            return result

    engine.baseline = ChangingBaseline()
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "FAILED_PRECOMMIT"
    assert caught.value.original == "BASELINE_CHANGED"
    assert source.read_bytes() == _SOURCE
    assert list((paths.config_root / "activation-journal").iterdir()) == []
    assert list((paths.config_root / "activation-backups").iterdir()) == []
    assert list(source.parent.glob(".cfm-candidate-*")) == []


def test_failed_post_exchange_publication_rolls_back_with_durable_intent(fixture, monkeypatch):
    source, paths, engine, root = fixture
    original_publish = JournalStore.publish

    def fail_committed(self, record, *, authenticate):
        if record.phase == "CONFIG_COMMITTED":
            raise FilesystemRefused("INJECTED_PUBLICATION_FAILURE")
        return original_publish(self, record, authenticate=authenticate)

    monkeypatch.setattr(JournalStore, "publish", fail_committed)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "CONFIG_RESTORED_SERVICE_PENDING"
    assert caught.value.original == "INJECTED_PUBLICATION_FAILURE"
    assert source.read_bytes() == _SOURCE
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        record = JournalStore(directory, owner=os.getuid()).load()
    assert record is not None and record.phase == "ROLLBACK_CONFIG"
    assert list((paths.config_root / "activation-backups").iterdir())[0].read_bytes() == _SOURCE


def test_operator_replacement_during_exchange_is_preserved_for_manual_recovery(fixture, monkeypatch):
    source, paths, engine, root = fixture
    from cloudflared_manager.activation import transaction

    original_exchange = transaction.exchange
    operator = b"operator change\n"

    def racing_exchange(directory, first, second):
        replacement = source.with_name("operator.new")
        replacement.write_bytes(operator)
        replacement.chmod(0o600)
        os.replace(replacement, source)
        original_exchange(directory, first, second)

    monkeypatch.setattr(transaction, "exchange", racing_exchange)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert b"new.example.com" in source.read_bytes()
    staged = list(source.parent.glob(".cfm-candidate-*"))
    assert len(staged) == 1 and staged[0].read_bytes() == operator
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        record = JournalStore(directory, owner=os.getuid()).load()
    assert record is not None and record.phase == "CONFIG_COMMITTING"


def test_pending_journal_blocks_manager_authority_changes(fixture):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    before = source.read_bytes()
    journal_path = paths.config_root / "activation-journal" / "journal"
    journal_before = journal_path.read_bytes()
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    with pytest.raises(ActivationBarrierError) as caught:
        barrier.require_clean()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert source.read_bytes() == before
    assert journal_path.read_bytes() == journal_before


def test_staging_only_is_never_promoted_and_clean_namespace_is_proved(fixture):
    source, paths, engine, root = fixture
    journal_dir = paths.config_root / "activation-journal"
    journal_dir.mkdir(mode=0o700)
    staged = journal_dir / "journal.next"
    staged.write_bytes(b"partial publication")
    staged.chmod(0o600)
    ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid()).require_clean()
    assert list(journal_dir.iterdir()) == []
    assert source.read_bytes() == _SOURCE


def test_unknown_journal_object_fails_closed(fixture):
    source, paths, engine, root = fixture
    journal_dir = paths.config_root / "activation-journal"
    journal_dir.mkdir(mode=0o700)
    unknown = journal_dir / "journal.old"
    unknown.write_bytes(b"unknown")
    with pytest.raises(ActivationBarrierError) as caught:
        ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid()).require_clean()
    assert caught.value.code == "UNSAFE_JOURNAL_NAMESPACE"
    assert unknown.read_bytes() == b"unknown"
    assert source.read_bytes() == _SOURCE


def test_absent_cleanup_artifacts_allow_durable_journal_retirement(fixture, monkeypatch):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    journal_dir = paths.config_root / "activation-journal"
    with PinnedDirectory(journal_dir, anchor=root, owner=os.getuid()) as directory:
        journal = JournalStore(directory, owner=os.getuid())
        record = journal.load()
        assert record is not None
        for phase in ("SERVICE_ACTIVATING", "SERVICE_VERIFIED"):
            record = journal.publish(record.successor(phase), authenticate=lambda item: None)
        record = journal.publish(record.successor("COMMIT_CLEANUP_PENDING", cleanup={
            "candidate": record.source, "backup": record.backup,
        }), authenticate=lambda item: None)
    (source.parent / record.candidate_name).unlink()
    (paths.config_root / "activation-backups" / record.backup_name).unlink()
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
    barrier.require_clean()
    assert list(journal_dir.iterdir()) == []
    assert b"new.example.com" in source.read_bytes()


def test_failed_reverse_exchange_keeps_first_failure_and_rollback_intent(fixture, monkeypatch):
    source, paths, engine, root = fixture
    from cloudflared_manager.activation import transaction

    original_publish = JournalStore.publish
    original_exchange = transaction.exchange
    exchanges = 0

    def fail_committed(self, record, *, authenticate):
        if record.phase == "CONFIG_COMMITTED":
            raise FilesystemRefused("COMMIT_PUBLICATION_FAILED")
        return original_publish(self, record, authenticate=authenticate)

    def fail_reverse(directory, first, second):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise FilesystemRefused("REVERSE_EXCHANGE_FAILED")
        original_exchange(directory, first, second)

    monkeypatch.setattr(JournalStore, "publish", fail_committed)
    monkeypatch.setattr(transaction, "exchange", fail_reverse)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.original == "COMMIT_PUBLICATION_FAILED"
    assert caught.value.rollback == "REVERSE_EXCHANGE_FAILED"
    assert b"new.example.com" in source.read_bytes()
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        record = JournalStore(directory, owner=os.getuid()).load()
    assert record is not None and record.phase == "ROLLBACK_CONFIG"


def test_explicit_recovery_finishes_interrupted_config_rollback(fixture, monkeypatch):
    source, paths, engine, root = fixture
    from cloudflared_manager.activation import transaction

    original_publish = JournalStore.publish
    original_exchange = transaction.exchange
    exchanges = 0

    def fail_committed(self, record, *, authenticate):
        if record.phase == "CONFIG_COMMITTED":
            raise FilesystemRefused("COMMIT_PUBLICATION_FAILED")
        return original_publish(self, record, authenticate=authenticate)

    def fail_reverse(directory, first, second):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise FilesystemRefused("REVERSE_EXCHANGE_FAILED")
        original_exchange(directory, first, second)

    monkeypatch.setattr(JournalStore, "publish", fail_committed)
    monkeypatch.setattr(transaction, "exchange", fail_reverse)
    with pytest.raises(ActivationError):
        engine.run(insert, validator=Validator())
    monkeypatch.setattr(transaction, "exchange", original_exchange)
    recovered = engine.recover()
    assert recovered.code == "CONFIG_RESTORED_SERVICE_PENDING"
    assert source.read_bytes() == _SOURCE
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"


def test_unsupported_source_xattr_is_rejected_without_journal(fixture):
    source, paths, engine, root = fixture
    try:
        os.setxattr(source, "user.cfm-test", b"value")
    except OSError:
        pytest.skip("fixture filesystem does not support user xattrs")
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "UNSUPPORTED_METADATA"
    assert source.read_bytes() == _SOURCE
    assert list((paths.config_root / "activation-journal").iterdir()) == []
    assert list((paths.config_root / "activation-backups").iterdir()) == []


def test_backup_short_write_aborts_without_recovery_journal(fixture, monkeypatch):
    source, paths, engine, root = fixture
    from cloudflared_manager.activation import state

    original_write = state.os.write

    def no_backup_write(fd, data):
        return 0

    monkeypatch.setattr(state.os, "write", no_backup_write)
    try:
        with pytest.raises(ActivationError) as caught:
            engine.run(insert, validator=Validator())
    finally:
        monkeypatch.setattr(state.os, "write", original_write)
    assert caught.value.code == "BACKUP_WRITE_FAILED"
    assert source.read_bytes() == _SOURCE
    assert list((paths.config_root / "activation-journal").iterdir()) == []
    assert list((paths.config_root / "activation-backups").iterdir()) == []
