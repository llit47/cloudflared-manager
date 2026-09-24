"""Strict service tests use only fake IO and deterministic clocks."""
import hashlib
from pathlib import Path

import pytest

from cloudflared_manager.activation.service import (
    ServiceRefused, StrictService, parse_exec, parse_show, parse_start,
)

EXEC = ('{ path=/usr/bin/cloudflared ; argv[]=/usr/bin/cloudflared --no-autoupdate '
        '--config /private/config.yml tunnel run ; ignore_errors=no ; start_time=[n/a] ; '
        'stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }')


def output(**changes):
    values = dict(Id='cloudflared.service', LoadState='loaded', ActiveState='active',
                  SubState='running', Type='notify', MainPID='42', NRestarts='0',
                  ExecStart=EXEC, Job='', NeedDaemonReload='no')
    values.update(changes)
    return ''.join(f'{key}={value}\n' for key, value in values.items()).encode()


class IO:
    raw = output()
    identity = (123, 1, 2)
    calls = 0
    def show(self):
        return self.raw
    def canonical(self, path):
        return path
    def process(self, pid, executable):
        return self.identity
    def executable(self, path):
        return self.identity[1:]
    def restart(self):
        self.calls += 1
        return True


class Authority:
    def current(self):
        return 'a' * 40, Path('/private/config.yml')


@pytest.fixture
def service():
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    return StrictService(Authority(), io=IO(), sleep=sleep, clock=lambda: now[0])


def observe(service):
    return service.observe(adopted_fingerprint=hashlib.sha256(b'/private/config.yml').hexdigest(),
                           source_digest='a' * 64)


def test_baseline_and_new_process(service):
    assert b'Job=\n' in service._io.raw
    assert parse_show(service._io.raw).settled
    baseline = observe(service)
    assert baseline.stable_milliseconds == 1000
    witness = service.validate_restart(baseline)
    assert (witness.main_pid, witness.process_start_ticks) == (42, 123)
    service.confirm_restart_witness(baseline, witness)
    assert service.restart()
    service._io.raw = output(MainPID='43')
    service._io.identity = (124, 1, 2)
    service.verify(baseline, witness, activation=True)


def test_verified_executable_path_comes_from_stable_loaded_service(service):
    executable = '/opt/cloudflare/bin/cloudflared'
    service._io.raw = output(ExecStart=EXEC.replace('/usr/bin/cloudflared', executable))
    facts, path = service.observe_with_executable(
        adopted_fingerprint=hashlib.sha256(b'/private/config.yml').hexdigest(),
        source_digest='a' * 64,
    )
    assert path == Path(executable)
    assert (facts.executable_device, facts.executable_inode) == (1, 2)


def test_loaded_executable_change_during_stable_observation_fails(service):
    original_show = service._io.show
    calls = [0]
    def changed_show():
        calls[0] += 1
        if calls[0] > 2:
            return output(ExecStart=EXEC.replace('/usr/bin/cloudflared',
                                                '/opt/cloudflare/bin/cloudflared'))
        return original_show()
    service._io.show = changed_show
    with pytest.raises(ServiceRefused):
        observe(service)


def test_queued_job_is_parsed_but_not_settled(service):
    service._io.raw = output(Job='123')
    assert not parse_show(service._io.raw).settled
    assert not service.settled()
    with pytest.raises(ServiceRefused):
        observe(service)


@pytest.mark.parametrize('job', ['0', '-1', 'abc', ' 123', '01', '1.0'])
def test_malformed_job_is_rejected(job):
    with pytest.raises(ServiceRefused):
        parse_show(output(Job=job))


@pytest.mark.parametrize('change', [
    dict(Id='other.service'), dict(LoadState='not-found'), dict(ActiveState='inactive'),
    dict(ActiveState='failed'), dict(Type='simple'), dict(MainPID='0'), dict(MainPID='-1'),
    dict(MainPID='42x'), dict(NRestarts='-1'), dict(Job='1'), dict(NeedDaemonReload='yes'),
    dict(ExecStart=EXEC.replace('/private/config.yml', '/wrong/config.yml')),
])
def test_bad_baseline(service, change):
    service._io.raw = output(**change)
    with pytest.raises(ServiceRefused):
        observe(service)
    assert service._io.calls == 0


@pytest.mark.parametrize('state', ['activating', 'deactivating', 'reloading', 'refreshing', 'maintenance', 'unknown'])
def test_transitional(service, state):
    service._io.raw = output(ActiveState=state)
    assert not service.settled()
    with pytest.raises(ServiceRefused):
        observe(service)


@pytest.mark.parametrize('raw', [b'', output() + b'Id=cloudflared.service\n', b'x' * 65537,
                               output() + b'unknown=secret\n', output() + b'\xff'])
def test_strict_output(raw):
    with pytest.raises(ServiceRefused) as caught:
        parse_show(raw)
    assert 'secret' not in repr(caught.value)


@pytest.mark.parametrize('raw', [
    '', EXEC + EXEC, EXEC.replace('ignore_errors=no', 'ignore_errors=yes'),
    EXEC.replace('--config /private/config.yml ', ''),
    EXEC.replace('tunnel run', 'tunnel run --token secret'),
    EXEC.replace('tunnel run', 'tunnel run --token-file=/secret'),
    EXEC.replace('tunnel run', 'tunnel run --config /other'),
    EXEC.replace('/usr/bin/cloudflared', 'cloudflared'),
    EXEC.replace('tunnel run', 'tunnel ingress validate'),
    EXEC.replace('--config /private/config.yml', '--config " /private/config.yml"'),
])
def test_strict_exec(raw):
    with pytest.raises(ServiceRefused):
        parse_exec(raw)


@pytest.mark.parametrize('changed', ['pid', 'start', 'executable', 'counter'])
def test_unstable(service, changed):
    original = service._sleep
    def sleep(seconds):
        original(seconds)
        if changed == 'pid':
            service._io.raw = output(MainPID='43')
        elif changed == 'counter':
            service._io.raw = output(NRestarts='1')
        else:
            service._io.identity = (124, 1, 2) if changed == 'start' else (123, 1, 3)
    service._sleep = sleep
    with pytest.raises(ServiceRefused):
        observe(service)


def test_exit_zero_is_not_verification(service):
    baseline = observe(service)
    witness = service.validate_restart(baseline)
    assert service.restart()
    service._io.raw = output(NRestarts='1')
    with pytest.raises(ServiceRefused):
        service.verify(baseline, witness, activation=True)
    with pytest.raises(ServiceRefused):
        service.verify(baseline, witness, activation=False)


@pytest.mark.parametrize('same', ['pid', 'start', 'both'])
def test_restart_requires_new_pid_and_start_identity(service, same):
    baseline = observe(service)
    witness = service.validate_restart(baseline)
    service._io.raw = output(MainPID='42' if same in {'pid', 'both'} else '43')
    service._io.identity = (123 if same in {'start', 'both'} else 124, 1, 2)
    with pytest.raises(ServiceRefused):
        service.verify(baseline, witness, activation=False)


def test_activation_still_requires_new_process_vs_precommit_baseline(service):
    baseline = observe(service)
    service._io.raw = output(MainPID='43')
    service._io.identity = (124, 1, 2)
    witness = service.validate_restart(baseline)
    service._io.raw = output()
    service._io.identity = (123, 1, 2)
    with pytest.raises(ServiceRefused):
        service.verify(baseline, witness, activation=True)


def test_pre_restart_witness_rejects_intervening_process_change(service):
    baseline = observe(service)
    witness = service.validate_restart(baseline)
    service._io.raw = output(MainPID='43')
    service._io.identity = (124, 1, 2)
    with pytest.raises(ServiceRefused):
        service.confirm_restart_witness(baseline, witness)


@pytest.mark.parametrize('state, substate', [('failed', 'failed'), ('inactive', 'dead')])
def test_settled_no_process_restart_witness(service, state, substate):
    baseline = observe(service)
    service._io.raw = output(ActiveState=state, SubState=substate, MainPID='0')
    witness = service.validate_restart(baseline)
    assert witness.main_pid is None and witness.process_start_ticks is None
    service.confirm_restart_witness(baseline, witness)
    service._io.raw = output(MainPID='43')
    service._io.identity = (124, 1, 2)
    service.verify(baseline, witness, activation=False)


def test_no_process_witness_change_blocks_restart(service):
    baseline = observe(service)
    service._io.raw = output(ActiveState='failed', SubState='failed', MainPID='0')
    witness = service.validate_restart(baseline)
    service._io.raw = output(ActiveState='inactive', SubState='dead', MainPID='0')
    with pytest.raises(ServiceRefused):
        service.confirm_restart_witness(baseline, witness)


def test_proc_start_parser():
    raw = b'42 (name with ) spaces) S ' + b'0 ' * 18 + b'123 0 0'
    assert parse_start(raw, 42) == 123
    for bad in [raw.replace(b'42', b'43', 1), raw.replace(b') S ', b') Z '), b'secret']:
        with pytest.raises(ServiceRefused):
            parse_start(bad, 42)


def test_fixed_runner_policy_and_output_bounds(monkeypatch):
    import os
    import subprocess
    import sys
    from contextlib import contextmanager
    from cloudflared_manager.activation import service_io

    real_popen = subprocess.Popen
    scripts = iter(["print('bounded')", "print('x' * 70000)", "raise SystemExit(1)"])
    calls = []
    @contextmanager
    def executable(path):
        assert str(path) == '/usr/bin/systemctl'
        fd = os.open('/dev/null', os.O_RDONLY)
        try:
            yield fd, (1, 2)
        finally:
            os.close(fd)
    def popen(argv, **kwargs):
        calls.append(argv)
        assert kwargs['shell'] is False
        assert kwargs['env'] == service_io._ENV
        assert kwargs['close_fds'] is True
        assert kwargs['executable'].startswith('/proc/self/fd/')
        assert len(kwargs['pass_fds']) == 1
        return real_popen([sys.executable, '-c', next(scripts)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    monkeypatch.setattr(service_io, 'verified_executable', executable)
    monkeypatch.setattr(service_io.subprocess, 'Popen', popen)
    io = service_io.LinuxServiceIO()
    assert io.show() == b'bounded\n'
    with pytest.raises(ServiceRefused):
        io.show()
    assert io.restart() is False
    assert calls[-1] == ('/usr/bin/systemctl', 'restart', 'cloudflared.service')
    with pytest.raises(TypeError):
        io.restart('other.service')


def test_restart_transport_errors_are_sanitized(service):
    def fail():
        raise RuntimeError('secret token /private/path')
    service._io.restart = fail
    assert service.restart() is False
    service._io.show = fail
    assert service.settled() is False
    with pytest.raises(ServiceRefused) as caught:
        observe(service)
    assert 'secret' not in str(caught.value)


def test_clock_cannot_claim_unobserved_stability(service):
    service._sleep = lambda seconds: None
    with pytest.raises(ServiceRefused):
        observe(service)


@pytest.mark.parametrize('case', ['good', 'wrong-exe', 'token-env', 'oversized-env', 'bad-stat', 'pid-reuse'])
def test_proc_io_is_bounded_and_binds_executable(tmp_path, monkeypatch, case):
    import os
    from contextlib import contextmanager
    from cloudflared_manager.activation import service_io
    raw = b'42 (cloudflared) S ' + b'0 ' * 18 + b'123 0 0'
    (tmp_path / 'stat').write_bytes(raw if case != 'bad-stat' else b'secret')
    (tmp_path / 'environ').write_bytes(b'TUNNEL_TOKEN=secret\0' if case == 'token-env' else
                                      b'x' * 65537 if case == 'oversized-env' else b'LANG=C\0')
    (tmp_path / 'exe').write_bytes(b'binary')
    info = (tmp_path / 'exe').stat()
    @contextmanager
    def executable(path):
        yield 999, (info.st_dev, info.st_ino + (case == 'wrong-exe'))
    real_open = os.open
    def proc_open(path, *args, **kwargs):
        if path == '/proc/42':
            path = tmp_path
        return real_open(path, *args, **kwargs)
    original_read = service_io._read_proc
    reads = 0
    def read(directory, leaf, limit):
        nonlocal reads
        data = original_read(directory, leaf, limit)
        if leaf == 'stat':
            reads += 1
            if case == 'pid-reuse' and reads == 2:
                data = data.replace(b'123', b'124')
        return data
    monkeypatch.setattr(service_io, 'verified_executable', executable)
    monkeypatch.setattr(service_io.os, 'open', proc_open)
    monkeypatch.setattr(service_io, '_read_proc', read)
    io = service_io.LinuxServiceIO()
    if case == 'good':
        assert io.process(42, Path('/usr/bin/cloudflared')) == (123, info.st_dev, info.st_ino)
    else:
        with pytest.raises(ServiceRefused) as caught:
            io.process(42, Path('/usr/bin/cloudflared'))
        assert 'secret' not in repr(caught.value)


def test_bounded_restart_timeout_kills_only_command_client(monkeypatch):
    import os
    from contextlib import contextmanager
    from cloudflared_manager.activation import service_io
    read_fd, write_fd = os.pipe()
    class Process:
        stdout = os.fdopen(read_fd, 'rb')
        killed = False
        def poll(self):
            return None
        def kill(self):
            self.killed = True
        def wait(self, timeout):
            assert timeout <= 5
            return -9
    process = Process()
    @contextmanager
    def executable(path):
        yield 999, (1, 2)
    times = iter([0, 31])
    monkeypatch.setattr(service_io, 'verified_executable', executable)
    monkeypatch.setattr(service_io.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(service_io.time, 'monotonic', lambda: next(times))
    try:
        with pytest.raises(ServiceRefused):
            service_io.LinuxServiceIO().restart()
        assert process.killed
        assert process.stdout.closed
    finally:
        os.close(write_fd)


@pytest.mark.parametrize('change', [dict(Type='simple'), dict(ExecStart=EXEC.replace('tunnel run', 'tunnel run --token secret')),
                                   dict(ExecStart=EXEC.replace('/private/config.yml', '/other'))])
def test_changed_target_refused_before_restart(service, change):
    baseline = observe(service)
    service._io.raw = output(**change)
    with pytest.raises(ServiceRefused):
        service.validate_restart(baseline)
    assert service._io.calls == 0


def test_executable_changed_since_baseline(service):
    baseline = observe(service)
    service._io.identity = (124, 1, 3)
    with pytest.raises(ServiceRefused):
        service.validate_restart(baseline)
    with pytest.raises(ServiceRefused):
        service.verify(baseline, baseline, activation=False)


def test_private_observation_reprs_are_redacted():
    assert repr(parse_show(output())) == 'Shape()'
    assert repr(parse_exec(EXEC)) == 'ExecFacts()'


@pytest.mark.parametrize('case', ['good', 'symlink', 'writable', 'setuid', 'not-executable', 'hardlink', 'non-root'])
def test_verified_executable_policy(tmp_path, monkeypatch, case):
    import os
    from contextlib import contextmanager
    from cloudflared_manager.activation import service_io
    executable = tmp_path / 'systemctl'
    executable.write_bytes(b'fake executable')
    executable.chmod(0o755)
    if case == 'symlink':
        link = tmp_path / 'alias'
        link.symlink_to(executable)
        executable = link
    elif case == 'writable':
        executable.chmod(0o775)
    elif case == 'setuid':
        executable.chmod(0o4755)
    elif case == 'not-executable':
        executable.chmod(0o644)
    elif case == 'hardlink':
        os.link(executable, tmp_path / 'hardlink')
    class Directory:
        def __init__(self, path):
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        def revalidate(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            os.close(self.fd)
    real_fstat, real_stat = os.fstat, os.stat
    def root_stat(info):
        values = list(info)
        values[4] = 1000 if case == 'non-root' else 0
        return os.stat_result(values)
    monkeypatch.setattr(service_io, 'PinnedDirectory', Directory)
    monkeypatch.setattr(service_io.os, 'fstat', lambda fd: root_stat(real_fstat(fd)))
    monkeypatch.setattr(service_io.os, 'stat', lambda *a, **kw: root_stat(real_stat(*a, **kw)))
    if case == 'good':
        with service_io.verified_executable(executable) as (fd, identity):
            assert identity == (real_fstat(fd).st_dev, real_fstat(fd).st_ino)
    else:
        with pytest.raises(ServiceRefused):
            with service_io.verified_executable(executable):
                pytest.fail('unsafe executable accepted')
