from collections import deque
from contextlib import AbstractContextManager
from typing import Self

import pytest

from cloudflared_manager.deployment import health
from cloudflared_manager.deployment.errors import HealthCheckError
from cloudflared_manager.deployment.health import HealthResponse, wait_for_health


def test_health_check_retries_then_accepts_exact_contract() -> None:
    responses = deque(
        [
            OSError("not ready"),
            HealthResponse(503, b"{}"),
            HealthResponse(200, b'{"status":"ok","app":"cloudflared-manager"}'),
        ]
    )
    sleeps: list[float] = []

    def fetch(url: str, timeout: float) -> HealthResponse:
        assert url == "http://192.168.1.20:8000/healthz"
        assert timeout == 1
        response = responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    wait_for_health(
        "192.168.1.20",
        8000,
        fetcher=fetch,
        attempts=3,
        connection_timeout=1,
        retry_interval=0.25,
        sleeper=sleeps.append,
    )

    assert sleeps == [0.25, 0.25]


def test_health_check_is_bounded_and_rejects_extra_diagnostics() -> None:
    calls = 0

    def fetch(url: str, timeout: float) -> HealthResponse:
        nonlocal calls
        calls += 1
        return HealthResponse(
            200,
            b'{"status":"ok","app":"cloudflared-manager","path":"/private"}',
        )

    with pytest.raises(HealthCheckError):
        wait_for_health(
            "10.0.0.2",
            8000,
            fetcher=fetch,
            attempts=4,
            retry_interval=0,
        )

    assert calls == 4


def test_default_health_fetcher_explicitly_bypasses_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(AbstractContextManager["Response"]):
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == health.MAX_HEALTH_BYTES + 1
            return b'{"status":"ok","app":"cloudflared-manager"}'

    class DirectOpener:
        def open(self, request, timeout: float) -> Response:
            assert request.full_url == "http://192.168.1.20:8000/healthz"
            assert timeout == 1
            return Response()

    handlers: list[object] = []

    def build_opener(*requested_handlers: object) -> DirectOpener:
        handlers.extend(requested_handlers)
        return DirectOpener()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setattr(health.urllib.request, "build_opener", build_opener)

    response = health._fetch("http://192.168.1.20:8000/healthz", 1)

    assert response.status == 200
    assert len(handlers) == 1
    assert isinstance(handlers[0], health.urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
