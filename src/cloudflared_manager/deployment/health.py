"""Bounded verification of the public manager health contract."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from cloudflared_manager.deployment.errors import HealthCheckError
from cloudflared_manager.deployment.protocols import HealthVerifier, ManagerService
from cloudflared_manager.deployment.validation import validate_bind_host, validate_port

EXPECTED_HEALTH = {"status": "ok", "app": "cloudflared-manager"}
EXPECTED_READINESS_KEYS = {"status", "app", "pid", "config_id"}
MAX_HEALTH_BYTES = 1024


@dataclass(frozen=True, slots=True)
class HealthResponse:
    status: int
    body: bytes


HealthFetcher = Callable[[str, float], HealthResponse]


@dataclass(frozen=True, slots=True)
class DeploymentReadiness:
    pid: int
    config_id: str


def verify_managed_health(
    service: ManagerService,
    http_health: HealthVerifier,
    bind_host: str,
    bind_port: int,
    expected_config_id: str,
) -> None:
    """Require both the HTTP contract and the managed systemd unit to be healthy."""

    before = service.runtime_state()
    if not before.active or before.main_pid <= 0:
        raise HealthCheckError("The managed Cloudflared Manager service is not active.")
    readiness = http_health(bind_host, bind_port)
    after = service.runtime_state()
    if (
        not after.active
        or before.main_pid != after.main_pid
        or not isinstance(readiness, DeploymentReadiness)
        or readiness.pid != after.main_pid
        or readiness.config_id != expected_config_id
    ):
        raise HealthCheckError("The managed Cloudflared Manager readiness identity is invalid.")


def wait_for_readiness(
    bind_host: str,
    bind_port: int,
    *,
    fetcher: HealthFetcher | None = None,
    attempts: int = 20,
    connection_timeout: float = 2.0,
    retry_interval: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
) -> DeploymentReadiness:
    host = validate_bind_host(bind_host)
    port = validate_port(bind_port)
    if not 1 <= attempts <= 120 or not 0 < connection_timeout <= 10:
        raise HealthCheckError("The readiness retry policy is invalid.")
    if not 0 <= retry_interval <= 5:
        raise HealthCheckError("The readiness retry policy is invalid.")
    request = fetcher or _fetch
    url = f"http://{host}:{port}/deployment-readiness"
    for attempt in range(attempts):
        try:
            response = request(url, connection_timeout)
            payload = json.loads(response.body[: MAX_HEALTH_BYTES + 1])
            valid = (
                response.status == 200 and len(response.body) <= MAX_HEALTH_BYTES
                and isinstance(payload, dict) and set(payload) == EXPECTED_READINESS_KEYS
                and payload["status"] == "ready" and payload["app"] == "cloudflared-manager"
                and type(payload["pid"]) is int and payload["pid"] > 0
                and isinstance(payload["config_id"], str)
                and len(payload["config_id"]) == 64
                and all(character in "0123456789abcdef" for character in payload["config_id"])
            )
            if valid:
                return DeploymentReadiness(payload["pid"], payload["config_id"])
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            pass
        if attempt + 1 < attempts:
            sleeper(retry_interval)
    raise HealthCheckError("Cloudflared Manager did not become ready in time.")


def wait_for_health(
    bind_host: str,
    bind_port: int,
    *,
    fetcher: HealthFetcher | None = None,
    attempts: int = 20,
    connection_timeout: float = 2.0,
    retry_interval: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Require an exact successful health response within a bounded window."""

    host = validate_bind_host(bind_host)
    port = validate_port(bind_port)
    if not 1 <= attempts <= 120 or not 0 < connection_timeout <= 10:
        raise HealthCheckError("The health-check retry policy is invalid.")
    if not 0 <= retry_interval <= 5:
        raise HealthCheckError("The health-check retry policy is invalid.")
    request = fetcher or _fetch
    url = f"http://{host}:{port}/healthz"
    for attempt in range(attempts):
        try:
            response = request(url, connection_timeout)
            payload = json.loads(response.body[: MAX_HEALTH_BYTES + 1])
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            pass
        else:
            if (
                response.status == 200
                and len(response.body) <= MAX_HEALTH_BYTES
                and payload == EXPECTED_HEALTH
            ):
                return
        if attempt + 1 < attempts:
            sleeper(retry_interval)
    raise HealthCheckError("Cloudflared Manager did not become healthy in time.")


def _fetch(url: str, timeout: float) -> HealthResponse:
    request = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return HealthResponse(status=response.status, body=response.read(MAX_HEALTH_BYTES + 1))
