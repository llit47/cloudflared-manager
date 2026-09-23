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


def test_transitional_post_exchange_failure_waits_for_settlement(full, monkeypatch):
    from cloudflared_manager.activation import transaction

    source, paths, engine, io = full
    original_publish = JournalStore.publish
    def transitional(self, record, **kwargs):
        result = original_publish(self, record, **kwargs)
        if record.phase == 'CONFIG_COMMITTED':
            io.raw = io.healthy(ActiveState='activating', SubState='start')
        return result
    monkeypatch.setattr(JournalStore, 'publish', transitional)
    original_exchange = transaction.exchange
    exchanges = []
    def tracked_exchange(*args, **kwargs):
        if exchanges:
            assert phase(paths) == 'ROLLBACK_CONFIG'
        exchanges.append(phase(paths))
        return original_exchange(*args, **kwargs)
    monkeypatch.setattr(transaction, 'exchange', tracked_exchange)

    assert engine.run(insert, validator=Validator()).code == 'RECOVERY_REQUIRED'
    assert phase(paths) == 'ACTIVATION_FAILED'
    assert exchanges == ['CONFIG_COMMITTING']
    candidate = source.read_bytes()
    assert b'new.example.com' in candidate
    assert io.actions == []

    for _ in range(2):
        assert engine.recover().code == 'RECOVERY_REQUIRED'
        assert phase(paths) == 'ACTIVATION_FAILED'
        assert source.read_bytes() == candidate
        assert io.actions == []
        assert exchanges == ['CONFIG_COMMITTING']

    io.raw = io.healthy()
    assert engine.recover().code == 'FAILED_ROLLED_BACK'
    assert exchanges == ['CONFIG_COMMITTING', 'ROLLBACK_CONFIG']
    assert source.read_bytes() == _SOURCE
    assert io.actions == ['ROLLBACK_SERVICE']
    clean(paths)


def test_transitional_before_rollback_exchange_retains_intent(full, monkeypatch):
    from cloudflared_manager.activation import transaction

    source, paths, engine, io = full
    io.outcomes = ['nonzero', 'success']
    original_publish = JournalStore.publish
    transition_pending = [True]
    def become_transitional(self, record, **kwargs):
        result = original_publish(self, record, **kwargs)
        if record.phase == 'ROLLBACK_CONFIG' and transition_pending:
            transition_pending.clear()
            io.raw = io.healthy(ActiveState='activating', SubState='start')
        return result
    monkeypatch.setattr(JournalStore, 'publish', become_transitional)
    original_exchange = transaction.exchange
    exchanges = []
    def tracked_exchange(*args, **kwargs):
        exchanges.append(phase(paths))
        return original_exchange(*args, **kwargs)
    monkeypatch.setattr(transaction, 'exchange', tracked_exchange)

    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == 'RECOVERY_REQUIRED'
    record = JournalRecord.parse((paths.config_root / 'activation-journal/journal').read_bytes())
    assert record.phase == 'ROLLBACK_CONFIG'
    assert record.restoration is None
    candidate = source.read_bytes()
    assert b'new.example.com' in candidate
    assert exchanges == ['CONFIG_COMMITTING']
    assert io.actions == ['SERVICE_ACTIVATING']

    for _ in range(2):
        with pytest.raises(ActivationError) as caught:
            engine.recover()
        assert caught.value.code == 'RECOVERY_REQUIRED'
        assert phase(paths) == 'ROLLBACK_CONFIG'
        assert source.read_bytes() == candidate
        assert exchanges == ['CONFIG_COMMITTING']
        assert io.actions == ['SERVICE_ACTIVATING']

    io.raw = io.healthy()
    assert engine.recover().code == 'FAILED_ROLLED_BACK'
    assert exchanges == ['CONFIG_COMMITTING', 'ROLLBACK_CONFIG']
    assert source.read_bytes() == _SOURCE
    assert io.actions == ['SERVICE_ACTIVATING', 'ROLLBACK_SERVICE']
    clean(paths)


def test_transitional_before_backup_restoration_exchange(full, monkeypatch):
    from cloudflared_manager.activation import transaction

    source, paths, engine, io = full
    io.outcomes = ['nonzero', 'success']
    original_publish = JournalStore.publish
    def crash_after_failure(self, record, **kwargs):
        result = original_publish(self, record, **kwargs)
        if record.phase == 'ACTIVATION_FAILED':
            raise Crash()
        return result
    monkeypatch.setattr(JournalStore, 'publish', crash_after_failure)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    record = JournalRecord.parse((paths.config_root / 'activation-journal/journal').read_bytes())
    assert record.phase == 'ACTIVATION_FAILED'
    (source.parent / record.candidate_name).unlink()

    transition_pending = [True]
    def become_transitional(self, record, **kwargs):
        result = original_publish(self, record, **kwargs)
        if record.phase == 'ROLLBACK_CONFIG' and transition_pending:
            transition_pending.clear()
            io.raw = io.healthy(ActiveState='activating', SubState='start')
        return result
    monkeypatch.setattr(JournalStore, 'publish', become_transitional)
    original_exchange = transaction.exchange
    exchanges = []
    def tracked_exchange(*args, **kwargs):
        exchanges.append(phase(paths))
        return original_exchange(*args, **kwargs)
    monkeypatch.setattr(transaction, 'exchange', tracked_exchange)

    with pytest.raises(ActivationError) as caught:
        engine.recover()
    assert caught.value.code == 'RECOVERY_REQUIRED'
    record = JournalRecord.parse((paths.config_root / 'activation-journal/journal').read_bytes())
    assert record.phase == 'ROLLBACK_CONFIG' and record.restoration is not None
    assert b'new.example.com' in source.read_bytes()
    assert exchanges == []
    assert io.actions == ['SERVICE_ACTIVATING']

    io.raw = io.healthy()
    assert engine.recover().code == 'FAILED_ROLLED_BACK'
    assert exchanges == ['ROLLBACK_CONFIG']
    assert source.read_bytes() == _SOURCE
    assert io.actions == ['SERVICE_ACTIVATING', 'ROLLBACK_SERVICE']
    clean(paths)


def test_unavailable_post_exchange_observation_records_failure(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def unavailable(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'CONFIG_COMMITTED':
            io.forbid_observation = True
        return result
    monkeypatch.setattr(JournalStore, 'publish', unavailable)

    assert engine.run(insert, validator=Validator()).code == 'RECOVERY_REQUIRED'
    assert phase(paths) == 'ACTIVATION_FAILED'
    assert b'new.example.com' in source.read_bytes()
    assert io.actions == []


def test_crash_at_config_committed_does_not_invent_continuity_failure(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def crash(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'CONFIG_COMMITTED':
            raise Crash()
        return result
    monkeypatch.setattr(JournalStore, 'publish', crash)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    monkeypatch.setattr(JournalStore, 'publish', original)

    io.raw = io.healthy(ActiveState='activating', SubState='start')
    assert engine.recover().code == 'RECOVERY_REQUIRED'
    assert phase(paths) == 'CONFIG_COMMITTED'
    assert io.actions == []
    assert b'new.example.com' in source.read_bytes()

    io.raw = io.healthy()
    assert engine.recover().code == 'COMMITTED_SUCCESS'
    assert io.actions == ['SERVICE_ACTIVATING']
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


@pytest.mark.parametrize('target', ['SERVICE_ACTIVATING', 'SERVICE_VERIFIED', 'ROLLBACK_SERVICE',
                                    'ROLLBACK_VERIFIED', 'COMMIT_CLEANUP_PENDING', 'ROLLBACK_CLEANUP_PENDING'])
@pytest.mark.parametrize('after', [False, True])
def test_publication_failure_stops_without_dependent_action(full, monkeypatch, target, after):
    source, paths, engine, io = full
    if target.startswith('ROLLBACK'):
        io.outcomes = ['nonzero']
    original = JournalStore.publish
    actions_at_failure = []
    def fail(self, record, **kwargs):
        if record.phase == target:
            if after:
                original(self, record, **kwargs)
            actions_at_failure[:] = io.actions
            raise OSError('secret path')
        return original(self, record, **kwargs)
    monkeypatch.setattr(JournalStore, 'publish', fail)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert 'secret' not in repr(caught.value)
    assert io.actions == actions_at_failure
    assert list((paths.config_root / 'activation-backups').iterdir())
    monkeypatch.setattr(JournalStore, 'publish', original)
    assert engine.recover().code in {'COMMITTED_SUCCESS', 'FAILED_ROLLED_BACK'}
    clean(paths)


def test_recovery_never_restarts_changed_loaded_target(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def crash(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'SERVICE_ACTIVATING':
            raise Crash()
        return result
    monkeypatch.setattr(JournalStore, 'publish', crash)
    with pytest.raises(Crash):
        engine.run(insert, validator=Validator())
    monkeypatch.setattr(JournalStore, 'publish', original)
    io.raw = io.raw.replace(b'tunnel run', b'tunnel run --token secret')
    with pytest.raises(ActivationError):
        engine.recover()
    assert io.actions == []
    assert phase(paths) == 'SERVICE_ACTIVATING'
    assert b'new.example.com' in source.read_bytes()


def test_baseline_change_before_exchange_has_no_service_command(full, monkeypatch):
    source, paths, engine, io = full
    original = JournalStore.publish
    def changed(self, record, **kwargs):
        result = original(self, record, **kwargs)
        if record.phase == 'CONFIG_COMMITTING':
            io.pid += 1
            io.raw = io.healthy()
        return result
    monkeypatch.setattr(JournalStore, 'publish', changed)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == 'FAILED_PRECOMMIT'
    assert io.actions == []
    assert source.read_bytes() == _SOURCE
    clean(paths)


def test_unhealthy_initial_service_never_commits(full):
    source, paths, engine, io = full
    io.raw = io.healthy(ActiveState='failed', SubState='failed')
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == 'SERVICE_BASELINE_UNAVAILABLE'
    assert io.actions == []
    assert source.read_bytes() == _SOURCE
    clean(paths)


@pytest.mark.parametrize('failure_boundary', ['directory-fsync', 'published-authentication'])
def test_authorizing_phase_not_used_before_durable_reverification(full, monkeypatch, failure_boundary):
    from cloudflared_manager.activation import journal as journal_module
    source, paths, engine, io = full
    original_sync = journal_module.fsync_directory
    original_auth = engine._authenticate
    def fail_sync(directory):
        if directory.path == paths.config_root / 'activation-journal':
            leaf = directory.path / 'journal'
            if leaf.exists() and JournalRecord.parse(leaf.read_bytes()).phase == 'SERVICE_ACTIVATING':
                raise OSError('injected private path')
        original_sync(directory)
    def fail_auth(record, active, backups):
        original_auth(record, active, backups)
        if record.phase == 'SERVICE_ACTIVATING' and phase(paths) == 'SERVICE_ACTIVATING':
            raise OSError('injected private output')
    if failure_boundary == 'directory-fsync':
        monkeypatch.setattr(journal_module, 'fsync_directory', fail_sync)
    else:
        monkeypatch.setattr(engine, '_authenticate', fail_auth)
    with pytest.raises(ActivationError) as caught:
        engine.run(insert, validator=Validator())
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert io.actions == []
    assert phase(paths) == 'SERVICE_ACTIVATING'
    raw = (paths.config_root / 'activation-journal/journal').read_bytes()
    assert str(source).encode() not in raw
    assert b'ExecStart' not in raw
    monkeypatch.setattr(journal_module, 'fsync_directory', original_sync)
    monkeypatch.setattr(engine, '_authenticate', original_auth)
    assert engine.recover().code == 'COMMITTED_SUCCESS'
    assert io.actions == ['SERVICE_ACTIVATING']
