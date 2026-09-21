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


def test_pending_journal_blocks_manager_authority_changes(fixture, monkeypatch):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    before = source.read_bytes()
    journal_path = paths.config_root / "activation-journal" / "journal"
    journal_before = journal_path.read_bytes()
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
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


@pytest.mark.parametrize("staged_bytes", [b"partial", b"complete-successor"])
def test_published_journal_authenticates_before_discarding_interrupted_staging(
    fixture, monkeypatch, staged_bytes,
):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    directory = paths.config_root / "activation-journal"
    published_path = directory / "journal"
    published_bytes = published_path.read_bytes()
    if staged_bytes == b"complete-successor":
        with PinnedDirectory(directory, anchor=root, owner=os.getuid()) as pinned:
            record = JournalStore(pinned, owner=os.getuid()).load()
        assert record is not None
        staged_bytes = record.successor("SERVICE_ACTIVATING").bytes()
    staged = directory / "journal.next"
    staged.write_bytes(staged_bytes)
    staged.chmod(0o600)
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
    with pytest.raises(ActivationBarrierError) as caught:
        barrier.require_clean()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert published_path.read_bytes() == published_bytes
    assert not staged.exists()
    assert b"new.example.com" in source.read_bytes()
    assert engine.recover().code == "SERVICE_ACTIVATION_PENDING"


def test_invalid_published_journal_does_not_use_valid_staging(fixture, monkeypatch):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    directory = paths.config_root / "activation-journal"
    with PinnedDirectory(directory, anchor=root, owner=os.getuid()) as pinned:
        record = JournalStore(pinned, owner=os.getuid()).load()
    assert record is not None
    staged = directory / "journal.next"
    staged.write_bytes(record.successor("SERVICE_ACTIVATING").bytes())
    staged.chmod(0o600)
    (directory / "journal").write_bytes(b"corrupt authority")
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
    with pytest.raises(ActivationBarrierError):
        barrier.require_clean()
    assert staged.exists()
    assert (directory / "journal").read_bytes() == b"corrupt authority"


def test_unauthenticated_published_artifact_preserves_staging(fixture, monkeypatch):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    directory = paths.config_root / "activation-journal"
    staged = directory / "journal.next"
    staged.write_bytes(b"partial")
    staged.chmod(0o600)
    backup = next((paths.config_root / "activation-backups").iterdir())
    backup.write_bytes(b"tampered")
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
    with pytest.raises(ActivationBarrierError):
        barrier.require_clean()
    assert staged.read_bytes() == b"partial"
    assert backup.read_bytes() == b"tampered"


def test_unsafe_staging_symlink_is_never_deleted(fixture, monkeypatch):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    directory = paths.config_root / "activation-journal"
    staged = directory / "journal.next"
    staged.symlink_to(directory / "journal")
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
    with pytest.raises(ActivationBarrierError):
        barrier.require_clean()
    assert staged.is_symlink()


def test_published_journal_identity_change_preserves_interrupted_staging(fixture):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    directory = paths.config_root / "activation-journal"
    journal_path = directory / "journal"
    staged = directory / "journal.next"
    staged.write_bytes(b"partial")
    staged.chmod(0o600)
    with PinnedDirectory(directory, anchor=root, owner=os.getuid()) as pinned:
        store = JournalStore(pinned, owner=os.getuid())

        def swap_published(_record):
            replacement = directory / "replacement"
            replacement.write_bytes(journal_path.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, journal_path)

        with pytest.raises(FilesystemRefused):
            store.recover_staging(authenticate=swap_published)
    assert staged.read_bytes() == b"partial"


def test_unjournaled_backup_blocks_new_transaction(fixture):
    source, paths, engine, root = fixture
    backup_dir = paths.config_root / "activation-backups"
    backup_dir.mkdir(mode=0o700)
    orphan = backup_dir / ("backup-" + "f" * 32)
    orphan.write_bytes(_SOURCE)
    orphan.chmod(0o600)
    with pytest.raises(FilesystemRefused) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "ORPHAN_BACKUP_REQUIRES_REVIEW"
    assert source.read_bytes() == _SOURCE
    assert orphan.read_bytes() == _SOURCE


def test_unjournaled_candidate_blocks_new_transaction(fixture):
    source, paths, engine, root = fixture
    orphan = source.parent / (".cfm-candidate-" + "f" * 32 + ".yaml")
    orphan.write_bytes(b"unknown")
    orphan.chmod(0o600)
    with pytest.raises(FilesystemRefused) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "ORPHAN_CANDIDATE_REQUIRES_REVIEW"
    assert source.read_bytes() == _SOURCE
    assert orphan.read_bytes() == b"unknown"


def _missing_displaced_original(fixture):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    with engine._stores() as (journal, backups):
        record = journal.load()
        assert record is not None and record.phase == "CONFIG_COMMITTED"
        (source.parent / record.candidate_name).unlink()
        record = journal.publish(record.successor("ACTIVATION_FAILED"),
                                 authenticate=lambda item: engine._authenticate_record(item, backups))
    return record


def test_authenticated_backup_restores_missing_displaced_original(fixture):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    recovered = engine.recover()
    assert recovered.code == "CONFIG_RESTORED_SERVICE_PENDING"
    assert source.read_bytes() == _SOURCE
    assert source.stat().st_ino != record.source.inode
    assert source.stat().st_mode & 0o777 == record.source.mode
    assert (source.parent / record.restoration_name).read_bytes().find(b"new.example.com") >= 0
    assert not (source.parent / record.candidate_name).exists()
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        restored = JournalStore(directory, owner=os.getuid()).load()
    assert restored is not None and restored.phase == "ROLLBACK_CONFIG"
    assert restored.restoration is not None
    assert restored.restoration.inode == source.stat().st_ino
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"


def test_corrupt_backup_blocks_missing_original_restoration(fixture):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    (paths.config_root / "activation-backups" / record.backup_name).write_bytes(b"tampered")
    active = source.read_bytes()
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert source.read_bytes() == active
    assert not (source.parent / record.restoration_name).exists()


def test_unjournaled_restoration_is_preserved_and_blocks_recovery(fixture):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    unjournaled = source.parent / record.restoration_name
    unjournaled.write_bytes(_SOURCE)
    unjournaled.chmod(record.source.mode)
    active = source.read_bytes()
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.original == "UNJOURNALED_RESTORATION"
    assert source.read_bytes() == active
    assert unjournaled.read_bytes() == _SOURCE


def test_failed_restore_exchange_keeps_journaled_stage_for_recovery(fixture, monkeypatch):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    from cloudflared_manager.activation import transaction

    original_exchange = transaction.exchange
    monkeypatch.setattr(transaction, "exchange", lambda *_: (_ for _ in ()).throw(
        FilesystemRefused("INJECTED_RESTORE_EXCHANGE_FAILURE")))
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.original == "INJECTED_RESTORE_EXCHANGE_FAILURE"
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        staged_record = JournalStore(directory, owner=os.getuid()).load()
    assert staged_record is not None and staged_record.restoration is not None
    assert source.read_bytes() != _SOURCE
    assert (source.parent / record.restoration_name).read_bytes() == _SOURCE
    monkeypatch.setattr(transaction, "exchange", original_exchange)
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"
    assert source.read_bytes() == _SOURCE


def test_tampered_journaled_restoration_identity_blocks_exchange(fixture, monkeypatch):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    from cloudflared_manager.activation import transaction

    original_exchange = transaction.exchange
    monkeypatch.setattr(transaction, "exchange", lambda *_: (_ for _ in ()).throw(
        FilesystemRefused("INJECTED_RESTORE_EXCHANGE_FAILURE")))
    with pytest.raises(ActivationError):
        engine.recover()
    restored = source.parent / record.restoration_name
    replacement = source.parent / "replacement"
    replacement.write_bytes(_SOURCE)
    replacement.chmod(record.source.mode)
    os.replace(replacement, restored)
    active = source.read_bytes()
    monkeypatch.setattr(transaction, "exchange", original_exchange)
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert source.read_bytes() == active
    assert restored.read_bytes() == _SOURCE


def test_interrupted_restoration_write_keeps_recoverable_intent(fixture, monkeypatch):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    from cloudflared_manager.activation import transaction

    original_write = transaction.os.write

    def fail_restoration_write(fd, data):
        if ".cfm-restore-" in os.readlink(f"/proc/self/fd/{fd}"):
            return 0
        return original_write(fd, data)

    monkeypatch.setattr(transaction.os, "write", fail_restoration_write)
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.original == "RESTORATION_WRITE_FAILED"
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        pending = JournalStore(directory, owner=os.getuid()).load()
    assert pending is not None and pending.phase == "ROLLBACK_CONFIG" and pending.restoration is None
    assert source.read_bytes() != _SOURCE
    assert (source.parent / record.restoration_name).exists()


def test_config_committing_recovery_uses_backup_if_displaced_original_is_missing(fixture, monkeypatch):
    source, paths, engine, root = fixture
    original_publish = JournalStore.publish

    def interrupt_committed(self, record, *, authenticate):
        if record.phase == "CONFIG_COMMITTED":
            with PinnedDirectory(paths.config_root / "activation-journal", anchor=root,
                                 owner=os.getuid()) as directory:
                current = JournalStore(directory, owner=os.getuid()).load()
            assert current is not None
            (source.parent / current.candidate_name).unlink()
            raise FilesystemRefused("INJECTED_COMMIT_INTERRUPTION")
        return original_publish(self, record, authenticate=authenticate)

    monkeypatch.setattr(JournalStore, "publish", interrupt_committed)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "CONFIG_RESTORED_SERVICE_PENDING"
    assert caught.value.original == "INJECTED_COMMIT_INTERRUPTION"
    assert source.read_bytes() == _SOURCE


def test_failed_restoration_identity_publication_leaves_unjournaled_artifact(fixture, monkeypatch):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    original_publish = JournalStore.publish

    def fail_restoration_publication(self, next_record, *, authenticate):
        if next_record.restoration is not None:
            raise FilesystemRefused("INJECTED_RESTORATION_PUBLICATION_FAILURE")
        return original_publish(self, next_record, authenticate=authenticate)

    monkeypatch.setattr(JournalStore, "publish", fail_restoration_publication)
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.original == "INJECTED_RESTORATION_PUBLICATION_FAILURE"
    assert (source.parent / record.restoration_name).read_bytes() == _SOURCE
    assert source.read_bytes() != _SOURCE
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        pending = JournalStore(directory, owner=os.getuid()).load()
    assert pending is not None and pending.phase == "ROLLBACK_CONFIG" and pending.restoration is None
    monkeypatch.setattr(JournalStore, "publish", original_publish)
    interrupted = paths.config_root / "activation-journal" / "journal.next"
    interrupted.write_bytes(b"partial restoration successor")
    interrupted.chmod(0o600)
    with pytest.raises(ActivationError) as blocked:
        engine.recover()
    assert blocked.value.original == "UNJOURNALED_RESTORATION"
    assert not interrupted.exists()
    assert (source.parent / record.restoration_name).read_bytes() == _SOURCE


def test_restored_backup_artifacts_are_retired_after_durable_rollback_decision(fixture):
    source, paths, engine, root = fixture
    initial = _missing_displaced_original(fixture)
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"
    with engine._stores() as (journal, backups):
        record = journal.load()
        assert record is not None and record.restoration is not None
        for phase in ("ROLLBACK_SERVICE", "ROLLBACK_VERIFIED"):
            record = journal.publish(record.successor(phase),
                                     authenticate=lambda item: engine._authenticate_record(item, backups))
        record = journal.publish(record.successor("ROLLBACK_CLEANUP_PENDING", cleanup={
            "candidate": record.candidate, "backup": record.backup,
        }), authenticate=lambda item: engine._authenticate_record(item, backups))
    assert engine.recover().code == "FAILED_ROLLED_BACK"
    assert source.read_bytes() == _SOURCE
    assert not (source.parent / initial.restoration_name).exists()
    assert not (source.parent / initial.candidate_name).exists()
    assert list((paths.config_root / "activation-backups").iterdir()) == []
    assert list((paths.config_root / "activation-journal").iterdir()) == []


def test_barrier_retires_completed_backup_rollback_after_artifacts_are_absent(fixture, monkeypatch):
    source, paths, engine, root = fixture
    initial = _missing_displaced_original(fixture)
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"
    with engine._stores() as (journal, backups):
        record = journal.load()
        assert record is not None
        for phase in ("ROLLBACK_SERVICE", "ROLLBACK_VERIFIED"):
            record = journal.publish(record.successor(phase),
                                     authenticate=lambda item: engine._authenticate_record(item, backups))
        record = journal.publish(record.successor("ROLLBACK_CLEANUP_PENDING", cleanup={
            "candidate": record.candidate, "backup": record.backup,
        }), authenticate=lambda item: engine._authenticate_record(item, backups))
    (source.parent / initial.restoration_name).unlink()
    (paths.config_root / "activation-backups" / initial.backup_name).unlink()
    barrier = ActivationRecoveryBarrier(paths, anchor=root, owner=os.getuid())
    monkeypatch.setattr(barrier, "_authority", lambda: ("a" * 40, source))
    barrier.require_clean()
    assert list((paths.config_root / "activation-journal").iterdir()) == []
    assert source.read_bytes() == _SOURCE


def test_recovered_commit_requires_active_file_fsync_before_advancing(fixture, monkeypatch):
    source, paths, engine, root = fixture
    from cloudflared_manager.activation import transaction

    original_publish = JournalStore.publish
    original_fsync = transaction.os.fsync

    def leave_committing(self, record, *, authenticate):
        if record.phase in {"CONFIG_COMMITTED", "ACTIVATION_FAILED"}:
            raise FilesystemRefused("INJECTED_PUBLICATION_FAILURE")
        return original_publish(self, record, authenticate=authenticate)

    monkeypatch.setattr(JournalStore, "publish", leave_committing)
    with pytest.raises(ActivationError):
        engine.run(insert, validator=Validator())
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        record = JournalStore(directory, owner=os.getuid()).load()
    assert record is not None and record.phase == "CONFIG_COMMITTING"
    monkeypatch.setattr(JournalStore, "publish", original_publish)

    def fail_active_fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(source):
            raise OSError("injected active fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(transaction.os, "fsync", fail_active_fsync)
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        still_pending = JournalStore(directory, owner=os.getuid()).load()
    assert still_pending is not None and still_pending.phase == "CONFIG_COMMITTING"
    monkeypatch.setattr(transaction.os, "fsync", original_fsync)
    assert engine.recover().code == "SERVICE_ACTIVATION_PENDING"


def test_descriptor_close_failure_preserves_earlier_rollback_failure(fixture, monkeypatch):
    source, paths, engine, root = fixture
    from cloudflared_manager.activation import transaction

    original_publish = JournalStore.publish
    original_exchange = transaction.exchange
    original_close = transaction.CandidateCommitHandle.close
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
        return original_exchange(directory, first, second)

    def fail_close(handle):
        original_close(handle)
        raise OSError("injected close failure")

    monkeypatch.setattr(JournalStore, "publish", fail_committed)
    monkeypatch.setattr(transaction, "exchange", fail_reverse)
    monkeypatch.setattr(transaction.CandidateCommitHandle, "close", fail_close)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert caught.value.original == "COMMIT_PUBLICATION_FAILED"
    assert caught.value.rollback == "REVERSE_EXCHANGE_FAILED;DESCRIPTOR_CLOSE_FAILED"
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        record = JournalStore(directory, owner=os.getuid()).load()
    assert record is not None and record.phase == "ROLLBACK_CONFIG"


def test_restored_backup_requires_active_file_fsync_on_recovery(fixture, monkeypatch):
    source, paths, engine, root = fixture
    record = _missing_displaced_original(fixture)
    from cloudflared_manager.activation import transaction

    original_fsync = transaction.os.fsync

    def fail_restored_fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(source) and source.read_bytes() == _SOURCE:
            raise OSError("injected restored file fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(transaction.os, "fsync", fail_restored_fsync)
    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == "RECOVERY_REQUIRED"
    assert source.read_bytes() == _SOURCE
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        pending = JournalStore(directory, owner=os.getuid()).load()
    assert pending is not None and pending.phase == "ROLLBACK_CONFIG" and pending.restoration is not None
    monkeypatch.setattr(transaction.os, "fsync", original_fsync)
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"


def test_crash_reverted_namespace_under_activation_failed_publishes_rollback_intent(fixture):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    with engine._stores() as (journal, backups):
        record = journal.load()
        assert record is not None and record.phase == "CONFIG_COMMITTED"
        record = journal.publish(record.successor("ACTIVATION_FAILED"),
                                 authenticate=lambda item: engine._authenticate_record(item, backups))
    from cloudflared_manager.activation.filesystem import exchange

    with PinnedDirectory(source.parent, anchor=root, owner=os.getuid()) as active:
        exchange(active, source.name, record.candidate_name)
    assert source.read_bytes() == _SOURCE
    assert engine.recover().code == "CONFIG_RESTORED_SERVICE_PENDING"
    with PinnedDirectory(paths.config_root / "activation-journal", anchor=root, owner=os.getuid()) as directory:
        pending = JournalStore(directory, owner=os.getuid()).load()
    assert pending is not None and pending.phase == "ROLLBACK_CONFIG"


def test_journal_retirement_rejects_in_place_authority_swap(fixture):
    source, paths, engine, root = fixture
    engine.run(insert, validator=Validator())
    directory = paths.config_root / "activation-journal"
    journal_path = directory / "journal"
    with PinnedDirectory(directory, anchor=root, owner=os.getuid()) as pinned:
        store = JournalStore(pinned, owner=os.getuid())
        record = store.load()
        assert record is not None
        for phase in ("SERVICE_ACTIVATING", "SERVICE_VERIFIED"):
            record = store.publish(record.successor(phase), authenticate=lambda item: None)
        record = store.publish(record.successor("COMMIT_CLEANUP_PENDING", cleanup={
            "candidate": record.source, "backup": record.backup,
        }), authenticate=lambda item: None)

        def replace_published(_record):
            replacement = directory / "replacement"
            replacement.write_bytes(journal_path.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, journal_path)

        with pytest.raises(FilesystemRefused):
            store.retire(record, authenticate=replace_published)
    assert journal_path.exists()
