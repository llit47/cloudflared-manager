"""Hostile protocol and fixed invocation tests with fake privileged operations."""

import io
import json
import hashlib
import os
from typing import get_args
from types import SimpleNamespace

import pytest

from cloudflared_manager.activation.mutation_client import (
    HELPER_PATH, SUDO_PATH, MutationBridgeUnavailable, mutate,
)
from cloudflared_manager.activation.mutation_helper import dispatch, serve
from cloudflared_manager.activation.mutation_protocol import (
    Action, MutationProtocolRefused, MutationRequest, encode_request, parse_request,
    parse_response,
)
from cloudflared_manager.activation.transaction import ActivationError
from cloudflared_manager.cloudflared.editing.local_ingress import LocalRoute, RouteSelector
from cloudflared_manager.cloudflared.editing.errors import (
    ApplicationValidationError, CloudflaredValidationRejectedError,
    CandidateFileError, UnsupportedConfigStructureError,
)

REV = "a" * 64
FP = "b" * 64
ADD = MutationRequest("local_ingress_add", REV, LocalRoute("app.example.com", None, "http://127.0.0.1:8000"))


@pytest.mark.parametrize("raw", [
    b"", b"{", b"{}", b"[]", b'{"version":true}',
    b'{"version":1,"action":"recover","source_revision":"' + REV.encode() + b'"}',
    b'{"version":1,"action":"local_ingress_add","source_revision":"' + REV.encode() + b'","route":{},"unit":"ssh.service"}',
    b'{"version":1,"action":"local_ingress_add","source_revision":"' + REV.encode() + b'","route":{"hostname":"app.example.com","path":null,"service":"http://localhost","command":"id"}}',
    b'{"version":1,"version":1}', b'{"version":1}{}',
    encode_request(ADD) + b" " * 4096,
    encode_request(ADD).replace(b"app.example.com", b"app\\u0000.example.com"),
    encode_request(ADD).replace(b"http://127.0.0.1:8000", b"http://localhost\\nsecret"),
])
def test_strict_request_rejects_hostile_input(raw):
    with pytest.raises(MutationProtocolRefused):
        parse_request(raw)


def test_round_trip_all_allowlisted_operations():
    for request in (
        ADD,
        MutationRequest("local_ingress_edit", REV, ADD.route, RouteSelector(2, FP)),
        MutationRequest("local_ingress_delete", REV, target=RouteSelector(2, FP)),
    ):
        assert parse_request(encode_request(request)) == request


def test_protocol_v1_grant_is_exactly_three_local_ingress_actions():
    assert set(get_args(Action)) == {
        "local_ingress_add", "local_ingress_edit", "local_ingress_delete",
    }
    for action in ("local_ingress_enable", "local_ingress_disable", "dns_create",
                   "generic_yaml_set", "execute_command"):
        raw = json.dumps({"version": 1, "action": action, "source_revision": REV}).encode()
        with pytest.raises(MutationProtocolRefused):
            parse_request(raw)


def test_helper_refuses_nonroot_and_does_not_dispatch_invalid_request():
    calls = []
    operation = lambda request: calls.append(request) or SimpleNamespace(code="COMMITTED_SUCCESS")
    output = io.BytesIO()
    assert serve(io.BytesIO(encode_request(ADD)), output, operation=operation, euid=lambda: 1000) == 1
    assert parse_response(output.getvalue()) == (False, "PRIVILEGED_BOUNDARY_UNAVAILABLE")
    output = io.BytesIO()
    assert serve(io.BytesIO(b'{"version":1,"action":"recover"}'), output,
                 operation=operation, euid=lambda: 0) == 1
    assert calls == []


@pytest.mark.parametrize("result,expected", [
    ("COMMITTED_SUCCESS", (True, "CHANGED")),
    ("NO_CHANGE", (True, "NO_CHANGE")),
    ("FAILED_ROLLED_BACK", (False, "ACTIVATION_FAILED_ROLLED_BACK")),
    ("RECOVERY_REQUIRED", (False, "RECOVERY_REQUIRED")),
])
def test_helper_preserves_activation_result(result, expected):
    output = io.BytesIO()
    serve(io.BytesIO(encode_request(ADD)), output,
          operation=lambda request: SimpleNamespace(code=result), euid=lambda: 0)
    assert parse_response(output.getvalue()) == expected


def test_helper_sanitizes_activation_error():
    def fail(request):
        raise ActivationError("RECOVERY_REQUIRED", original="secret /etc/cloudflared/config.yml")
    output = io.BytesIO()
    serve(io.BytesIO(encode_request(ADD)), output, operation=fail, euid=lambda: 0)
    assert parse_response(output.getvalue()) == (False, "RECOVERY_REQUIRED")
    assert b"secret" not in output.getvalue()


@pytest.mark.parametrize("error,code", [
    (ApplicationValidationError("secret"), "VALIDATION_FAILED"),
    (CloudflaredValidationRejectedError("secret"), "VALIDATION_FAILED"),
    (UnsupportedConfigStructureError("secret"), "UNSUPPORTED_CONFIG"),
    (CandidateFileError("secret"), "RECOVERY_REQUIRED"),
    (ActivationError("FAILED_ROLLED_BACK", original="secret"), "ACTIVATION_FAILED_ROLLED_BACK"),
    (ActivationError("RECOVERY_REQUIRED", original="secret"), "RECOVERY_REQUIRED"),
])
def test_validation_and_rollback_failures_have_sanitized_distinct_codes(error, code):
    def fail(request):
        raise error
    output = io.BytesIO()
    serve(io.BytesIO(encode_request(ADD)), output, operation=fail, euid=lambda: 0)
    assert parse_response(output.getvalue()) == (False, code)
    assert b"secret" not in output.getvalue()


def test_client_fixed_sudo_argv_no_shell_and_no_raw_yaml():
    seen = []
    def runner(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"version":1,"ok":true,"code":"CHANGED"}\n')
    assert mutate(ADD, runner=runner).code == "CHANGED"
    argv, kwargs = seen[0]
    assert argv == [str(SUDO_PATH), "-n", str(HELPER_PATH)]
    assert kwargs["shell"] is False
    assert json.loads(kwargs["input"])["route"]["hostname"] == "app.example.com"
    assert b"yaml" not in kwargs["input"]


@pytest.mark.parametrize("response", [
    b"{", b'{"version":1,"ok":true,"code":"CHANGED"}{}',
    b'{"version":1,"ok":true,"code":"CHANGED","code":"CHANGED"}',
    b'{"version":1,"ok":true,"code":"CHANGED"}' + b" " * 512,
    b'{"version":1,"ok":true,"code":"RECOVERY_REQUIRED"}',
])
def test_client_rejects_invalid_or_oversized_response(response):
    with pytest.raises(MutationBridgeUnavailable):
        mutate(ADD, runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=response))


def test_recovery_bridge_remains_recovery_only():
    from cloudflared_manager.activation.bridge_protocol import parse_request as parse_recovery
    with pytest.raises(Exception):
        parse_recovery(encode_request(ADD))


def test_privileged_mutation_uses_transaction_and_rejects_duplicate_request(tmp_path, monkeypatch):
    from cloudflared_manager.activation import mutation_helper
    from cloudflared_manager.activation.transaction import FilesystemActivation
    from cloudflared_manager.cloudflared.editing.document import EditableCloudflaredConfig
    from cloudflared_manager.cloudflared.editing.source import read_config_source_snapshot
    from tests.deployment_support import make_paths
    from cloudflared_manager.deployment.release import DeploymentLock
    from tests.test_activation_filesystem import Authority, FakeService, Validator

    root = tmp_path / "cloudflared"
    root.mkdir(mode=0o700)
    source = root / "config.yml"
    source.write_bytes(b"ingress:\n  - hostname: app.example.com\n    service: http://127.0.0.1:8000\n  - service: http_status:404\n")
    paths = make_paths(tmp_path)
    paths.config_root.mkdir(parents=True)
    (tmp_path / "etc").chmod(0o700)
    paths.config_root.chmod(0o700)
    engine = FilesystemActivation(paths, authority=Authority(source), baseline=FakeService(),
                                  service=FakeService(), owner=os.getuid(), anchor=tmp_path)
    monkeypatch.setattr(mutation_helper, "CloudflaredCandidateValidator", Validator)
    snapshot = read_config_source_snapshot(source)
    selector = EditableCloudflaredConfig.from_snapshot(snapshot).local_route_selector(0)
    request = MutationRequest("local_ingress_edit", snapshot.sha256,
                              LocalRoute("new.example.com", None, "http://127.0.0.1:9000"), selector)
    assert mutation_helper.dispatch(request, lambda req: mutation_helper.execute(req, engine=engine, allowed_adopted_parent=root)) == (True, "CHANGED")
    assert b"new.example.com" in source.read_bytes()
    with DeploymentLock(paths.lock_path, owner=(os.getuid(), os.getegid())):
        assert mutation_helper.dispatch(request, lambda req: mutation_helper.execute(req, engine=engine, allowed_adopted_parent=root)) == (False, "BUSY")
    assert mutation_helper.dispatch(request, lambda req: mutation_helper.execute(req, engine=engine, allowed_adopted_parent=root)) == (False, "STALE_CONFLICT")
    assert hashlib.sha256(source.read_bytes()).hexdigest() != snapshot.sha256
