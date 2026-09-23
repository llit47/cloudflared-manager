"""Full PR14 transaction tests: temporary configs and strict observer with fake IO."""
import os
from dataclasses import replace

import pytest

from cloudflared_manager.activation.journal import JournalRecord, JournalStore
from cloudflared_manager.activation.filesystem import FilesystemRefused
from cloudflared_manager.activation.service import StrictService
from cloudflared_manager.activation.transaction import ActivationError, FilesystemActivation
from tests.test_activation_filesystem import fixture, insert, Validator, _SOURCE
from tests.test_activation_service import IO, output, EXEC


class Crash(BaseException):
    pass


class TransactionIO(IO):
    def __init__(self, source, paths):
        self.source, self.paths = source, paths
        self.identity = (123, 1, 2)
        self.pid = 42
        self.raw = self.healthy()
        self.actions = []
        self.outcomes = []
        self.forbid_observation = False

    def healthy(self, **kwargs):
        return output(MainPID=str(self.pid), ExecStart=EXEC.replace('/private/config.yml', str(self.source)), **kwargs)

    def show(self):
        assert not self.forbid_observation, 'verified boundary must never reconsider live state'
        return self.raw

    def restart(self):
        record = JournalRecord.parse((self.paths.config_root / 'activation-journal/journal').read_bytes())
        assert record.phase in {'SERVICE_ACTIVATING', 'ROLLBACK_SERVICE'}
        assert not (self.paths.config_root / 'activation-journal/journal.next').exists()
        self.actions.append(record.phase)
        outcome = self.outcomes.pop(0) if self.outcomes else 'success'
        self.pid += 1
        self.identity = (self.identity[0] + 1, 1, 2)
        self.raw = self.healthy()
        if outcome == 'failed-health':
            self.raw = self.healthy(ActiveState='failed', SubState='failed')
        if outcome in {'timeout-transitional', 'nonzero-transitional', 'zero-transitional'}:
            self.raw = self.healthy(ActiveState='activating', SubState='start')
        if outcome.startswith('timeout'):
            raise TimeoutError('secret command output')
        return outcome not in {'nonzero', 'nonzero-transitional'}


@pytest.fixture
def full(fixture):
    source, paths, old, root = fixture
    io = TransactionIO(source, paths)
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    service = StrictService(old.authority, io=io, sleep=sleep, clock=lambda: now[0])
    engine = FilesystemActivation(paths, authority=old.authority, service=service,
                                  owner=os.getuid(), anchor=root)
    return source, paths, engine, io


def phase(paths):
    return JournalRecord.parse((paths.config_root / 'activation-journal/journal').read_bytes()).phase


def clean(paths):
    assert list((paths.config_root / 'activation-journal').iterdir()) == []
    assert list((paths.config_root / 'activation-backups').iterdir()) == []


def test_complete_success(full):
    source, paths, engine, io = full
    assert engine.run(insert, validator=Validator()).code == 'COMMITTED_SUCCESS'
    assert b'new.example.com' in source.read_bytes()
    assert io.actions == ['SERVICE_ACTIVATING']
    clean(paths)
    assert engine.recover().code == 'NO_RECOVERY_REQUIRED'


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'failed-health'])
def test_settled_failure_rolls_back_and_restarts(full, failure):
    source, paths, engine, io = full
    io.outcomes = [failure, 'success']
    assert engine.run(insert, validator=Validator()).code == 'FAILED_ROLLED_BACK'
    assert source.read_bytes() == _SOURCE
    assert io.actions == ['SERVICE_ACTIVATING', 'ROLLBACK_SERVICE']
    clean(paths)


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'failed-health'])
def test_config_restored_service_failed_is_distinct(full, failure):
    source, paths, engine, io = full
    io.outcomes = ['nonzero', failure]
    assert engine.run(insert, validator=Validator()).code == 'CONFIG_RESTORED_SERVICE_RECOVERY_FAILED'
    assert source.read_bytes() == _SOURCE
    assert phase(paths) == 'ROLLBACK_SERVICE'
    assert engine.recover().code == 'FAILED_ROLLED_BACK'
    clean(paths)


@pytest.mark.parametrize('failure', ['timeout-transitional', 'nonzero-transitional', 'zero-transitional'])
@pytest.mark.parametrize('rollback', [False, True])
def test_transitional_failure_never_races_config(full, failure, rollback):
    source, paths, engine, io = full
    io.outcomes = ['nonzero', failure] if rollback else [failure]
    assert engine.run(insert, validator=Validator()).code == 'RECOVERY_REQUIRED'
    expected = 'ROLLBACK_SERVICE' if rollback else 'SERVICE_ACTIVATING'
    assert phase(paths) == expected
    content = source.read_bytes()
    assert (content == _SOURCE) == rollback
    before = list(io.actions)
    for _ in range(2):
        assert engine.recover().code == 'RECOVERY_REQUIRED'
        assert io.actions == before
        assert source.read_bytes() == content
        assert phase(paths) == expected
    io.raw = io.healthy()
    assert engine.recover().code == ('FAILED_ROLLED_BACK' if rollback else 'COMMITTED_SUCCESS')
    assert io.actions == before + [expected]
    clean(paths)


@pytest.mark.parametrize('target', ['CONFIG_COMMITTED', 'SERVICE_ACTIVATING', 'SERVICE_VERIFIED',
                                    'COMMIT_CLEANUP_PENDING', 'ROLLBACK_CONFIG', 'ROLLBACK_SERVICE',
                                    'ROLLBACK_VERIFIED', 'ROLLBACK_CLEANUP_PENDING'])
def test_recovery_after_every_service_phase(full, monkeypatch, target):
    source, paths, engine, io = full
    rollback = target.startswith('ROLLBACK')
    if rollback:
        io.outcomes = ['nonzero']
    original = JournalStore.publish
    def crash(self, record, **kwargs):
        result = original(self, record, **kwargs)
        # ROLLBACK_CONFIG is first an intent, before restoration.
        if record.phase == target:
            raise Crash()
        return result
    monkeypatch.setattr(JournalStore, 'publish', crash)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    assert phase(paths) == target
    monkeypatch.setattr(JournalStore, 'publish', original)
    before = list(io.actions)
    if target in {'SERVICE_VERIFIED', 'ROLLBACK_VERIFIED', 'COMMIT_CLEANUP_PENDING', 'ROLLBACK_CLEANUP_PENDING'}:
        io.forbid_observation = True
    assert engine.recover().code == ('FAILED_ROLLED_BACK' if rollback else 'COMMITTED_SUCCESS')
    if io.forbid_observation:
        assert io.actions == before
    else:
        assert io.actions == before + ['ROLLBACK_SERVICE' if rollback else 'SERVICE_ACTIVATING']
    clean(paths)
    assert engine.recover().code == 'NO_RECOVERY_REQUIRED'


def test_service_verified_cannot_select_failure(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def crash(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'SERVICE_VERIFIED':
            raise Crash()
        return result
    monkeypatch.setattr(JournalStore, 'publish', crash)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    record = JournalRecord.parse((paths.config_root / 'activation-journal/journal').read_bytes())
    with pytest.raises(FilesystemRefused):
        record.successor('ACTIVATION_FAILED')
    forged = replace(record, phase='ACTIVATION_FAILED', predecessor_phase='SERVICE_VERIFIED',
                     generation=record.generation + 1, predecessor_generation=record.generation)
    with pytest.raises(FilesystemRefused):
        JournalRecord.parse(forged.bytes())


def test_post_exchange_continuity_failure_rolls_back(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def changed(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'CONFIG_COMMITTED':
            io.pid += 1
            io.raw = io.healthy()
        return result
    monkeypatch.setattr(JournalStore, 'publish', changed)
    assert engine.run(insert, validator=Validator()).code == 'FAILED_ROLLED_BACK'
    assert io.actions == ['ROLLBACK_SERVICE']
    assert source.read_bytes() == _SOURCE
    clean(paths)


@pytest.mark.parametrize('target', ['SERVICE_ACTIVATING', 'ROLLBACK_SERVICE'])
def test_no_restart_before_publication_completes(full, monkeypatch, target):
    source, paths, engine, io = full
    if target == 'ROLLBACK_SERVICE':
        io.outcomes = ['nonzero']
    original = JournalStore.publish
    def crash(self, record, **kwargs):
        if record.phase == target:
            raise Crash()
        return original(self, record, **kwargs)
    monkeypatch.setattr(JournalStore, 'publish', crash)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    assert target not in io.actions


def test_changed_active_after_service_verified_preserved(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def crash(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'SERVICE_VERIFIED':
            raise Crash()
        return result
    monkeypatch.setattr(JournalStore, 'publish', crash)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    monkeypatch.setattr(JournalStore, 'publish', original)
    source.write_bytes(b'operator config')
    before = list(io.actions)
    with pytest.raises(ActivationError):
        engine.recover()
    assert io.actions == before
    assert source.read_bytes() == b'operator config'
