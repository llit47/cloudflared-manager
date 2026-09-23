"""Hostile JSON and sudo contract tests; never touch the adopted host config."""

import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from cloudflared_manager.activation.bridge_client import (
    HELPER_PATH, SUDO_PATH, BridgeUnavailable, recover,
)
from cloudflared_manager.activation.bridge_helper import serve
from cloudflared_manager.activation.bridge_protocol import parse_request, ProtocolRefused
from cloudflared_manager.activation.transaction import ActivationError


@pytest.mark.parametrize("raw,code", [
    (b"", "INVALID_REQUEST"),
    (b"{", "INVALID_REQUEST"),
    (b"{}", "UNSUPPORTED_VERSION"),
    (b'{"version":2,"operation":"recover"}', "UNSUPPORTED_VERSION"),
    (b'{"version":true,"operation":"recover"}', "UNSUPPORTED_VERSION"),
    (b'{"version":1}', "UNKNOWN_OPERATION"),
    (b'{"version":1,"operation":123}', "UNKNOWN_OPERATION"),
    (b'{"version":1,"operation":"restart"}', "UNKNOWN_OPERATION"),
    (b'{"version":1,"operation":"recover","path":"/etc/passwd"}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover","path":"../../etc/passwd"}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover","unit":"ssh.service"}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover","command":"; id"}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover","argv":"$(touch /tmp/pwned)"}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover","version":1}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover"}{}', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover"}\x00', "INVALID_REQUEST"),
    (b'{"version":1,"operation":"recover"}' + b" " * 4096, "INVALID_REQUEST"),
])
def test_rejects_hostile_requests(raw, code):
    with pytest.raises(ProtocolRefused) as caught:
        parse_request(raw)
    assert caught.value.code == code


def test_helper_returns_one_sanitized_json_and_only_calls_recovery_after_validation():
    calls = []
    def operation():
        calls.append(1)
        return SimpleNamespace(code="NO_RECOVERY_REQUIRED")
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"version":1,"operation":"recover"}\n'), output,
                 recover=operation, euid=lambda: 0) == 0
    assert json.loads(output.getvalue()) == {
        "version": 1, "ok": True, "code": "NO_RECOVERY_REQUIRED",
    }
    assert calls == [1]
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"version":1,"operation":"recover","unit":"x"}'),
                 output, recover=operation, euid=lambda: 0) == 1
    assert calls == [1]


def test_helper_fails_closed_without_root():
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"version":1,"operation":"recover"}'), output,
                 recover=lambda: pytest.fail("must not run"), euid=lambda: 1000) == 1
    assert json.loads(output.getvalue())["code"] == "PRIVILEGED_BOUNDARY_UNAVAILABLE"


@pytest.mark.parametrize("result,expected", [
    ("FAILED_ROLLED_BACK", (False, "FAILED_ROLLED_BACK")),
    ("RECOVERY_REQUIRED", (False, "RECOVERY_REQUIRED")),
    ("COMMITTED_SUCCESS", (True, "COMMITTED_SUCCESS")),
])
def test_helper_preserves_recovery_outcome(result, expected):
    output = io.BytesIO()
    status = serve(io.BytesIO(b'{"version":1,"operation":"recover"}'), output,
                   recover=lambda: SimpleNamespace(code=result), euid=lambda: 0)
    response = json.loads(output.getvalue())
    assert (response["ok"], response["code"]) == expected
    assert status == (0 if expected[0] else 1)


def test_helper_sanitizes_errors():
    def failure():
        raise ActivationError("RECOVERY_REQUIRED", original="secret /etc/cloudflared/config.yml")
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"version":1,"operation":"recover"}'), output,
                 recover=failure, euid=lambda: 0) == 1
    assert b"secret" not in output.getvalue()
    assert json.loads(output.getvalue())["code"] == "RECOVERY_REQUIRED"


def test_production_dispatch_rejects_adopted_path_outside_fixed_cloudflared_directory(monkeypatch):
    from pathlib import Path
    from cloudflared_manager.activation import bridge_helper
    class Authority:
        def __init__(self, paths):
            pass
        def current(self):
            return "a" * 40, Path("/etc/passwd.yml")
    monkeypatch.setattr(bridge_helper, "DeployedAuthority", Authority)
    monkeypatch.setattr(bridge_helper, "FilesystemActivation",
                        lambda paths: pytest.fail("must not construct transaction"))
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"version":1,"operation":"recover"}'), output,
                 recover=bridge_helper._production_recovery, euid=lambda: 0) == 1
    assert json.loads(output.getvalue())["code"] == "UNSUPPORTED_ADOPTED_PATH"


def test_client_uses_fixed_sudo_argv_and_no_shell():
    seen = []
    def runner(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"version":1,"ok":true,"code":"NO_RECOVERY_REQUIRED"}\n')
    assert recover(runner=runner).ok
    argv, kwargs = seen[0]
    assert argv == [str(SUDO_PATH), "-n", str(HELPER_PATH)]
    assert kwargs["shell"] is False
    assert json.loads(kwargs["input"])["operation"] == "recover"


@pytest.mark.parametrize("failure", [
    FileNotFoundError(), subprocess.TimeoutExpired("sudo", 120),
])
def test_client_reports_sudo_or_helper_unavailable(failure):
    def runner(*args, **kwargs):
        raise failure
    with pytest.raises(BridgeUnavailable):
        recover(runner=runner)


def test_client_propagates_helper_failure_without_stderr():
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b'{"version":1,"ok":false,"code":"RECOVERY_REQUIRED"}\n',
                               stderr=b"secret")
    assert recover(runner=runner).code == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("stdout,returncode", [
    (b"sudo: permission denied", 1),
    (b'{"version":1,"ok":true,"code":"NO_RECOVERY_REQUIRED"}', 1),
    (b'{"version":1,"ok":true,"code":"NO_RECOVERY_REQUIRED"}' + b"x" * 4096, 0),
])
def test_client_rejects_invalid_boundary_output(stdout, returncode):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")
    with pytest.raises(BridgeUnavailable):
        recover(runner=runner)
