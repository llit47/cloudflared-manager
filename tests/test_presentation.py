from pathlib import Path

from cloudflared_manager.cloudflared import (
    CloudflaredConfig,
    CloudflaredRuntime,
    ConfigFileNotFoundError,
    IngressRule,
    ManagementMode,
    RuntimeDiscoveryError,
)
from cloudflared_manager.config import Settings
from cloudflared_manager.web.presentation import build_dashboard_view

FAKE_SECRET = "TEST_SECRET_MUST_NOT_LEAK"


def runtime_result(**overrides) -> CloudflaredRuntime:
    values = {
        "executable_path": Path("/opt/example/bin/cloudflared"),
        "version": "2026.9.1",
        "systemd_available": True,
        "service_exists": True,
        "load_state": "loaded",
        "active_state": "active",
        "sub_state": "running",
        "enabled_state": "enabled",
        "main_pid": 4242,
        "management_mode": ManagementMode.LOCAL_CONFIG,
        "explicit_config_path": Path("/private/cloudflared/config.yml"),
    }
    values.update(overrides)
    return CloudflaredRuntime(**values)


def test_no_path_does_not_call_config_loader() -> None:
    def unexpected_loader(path: Path) -> CloudflaredConfig:
        raise AssertionError(f"loader called with {path}")

    view = build_dashboard_view(Settings(mode="test"), unexpected_loader)

    assert view.configuration.label == "Not configured"
    assert view.routes == ()


def test_presentation_excludes_catch_all_and_redacts_url_secrets() -> None:
    config = CloudflaredConfig(
        tunnel="fake-tunnel",
        ingress_rules=(
            IngressRule(
                hostname="app.example.com",
                path="^/private/.*",
                service="https://user:password@localhost:8443/base?token=secret#value",
            ),
            IngressRule(service="http_status:404", is_catch_all=True),
        ),
    )

    view = build_dashboard_view(
        Settings(mode="test", cloudflared_config_path=Path("fixture.yml")),
        lambda path: config,
    )

    assert view.route_count == 1
    assert view.routes[0].service == "https://localhost:8443/base"
    assert "password" not in view.routes[0].service
    assert "secret" not in view.routes[0].service
    assert "http_status:404" not in {route.service for route in view.routes}


def test_expected_loader_error_becomes_safe_dashboard_state() -> None:
    def missing_loader(path: Path) -> CloudflaredConfig:
        raise ConfigFileNotFoundError("safe internal error")

    view = build_dashboard_view(
        Settings(
            mode="test",
            cloudflared_config_path=Path("/private/secret-config.yml"),
        ),
        missing_loader,
    )

    assert view.configuration.label == "Load error"
    assert view.routes == ()
    assert "/private" not in view.configuration.description
    assert "secret-config" not in view.configuration.description


def test_disabled_runtime_discovery_does_not_call_provider() -> None:
    def unexpected_discovery(enabled: bool) -> CloudflaredRuntime:
        raise AssertionError(f"runtime discovery called with {enabled}")

    view = build_dashboard_view(
        Settings(mode="test"),
        discover_runtime=unexpected_discovery,
    )

    assert view.cloudflared.label == "Not connected"
    assert "disabled" in view.cloudflared.description


def test_running_runtime_is_presented_without_local_paths() -> None:
    runtime = runtime_result()

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=lambda enabled: runtime,
    )

    assert view.cloudflared.label == "Running"
    assert view.cloudflared.tone == "success"
    assert "version 2026.9.1" in view.cloudflared.description
    assert "active/running" in view.cloudflared.description
    assert "Local configuration mode" in view.cloudflared.description
    assert "/opt/example" not in repr(view)
    assert "/private/cloudflared" not in repr(view)


def test_binary_alone_is_not_presented_as_running() -> None:
    runtime = runtime_result(
        service_exists=False,
        active_state="active",
        sub_state="running",
        enabled_state=None,
        main_pid=None,
        management_mode=ManagementMode.UNKNOWN,
        explicit_config_path=None,
    )

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=lambda enabled: runtime,
    )

    assert view.cloudflared.label == "Installed"
    assert view.cloudflared.tone == "neutral"
    assert "not loaded" in view.cloudflared.description


def test_inactive_loaded_service_is_presented_as_stopped() -> None:
    runtime = runtime_result(
        active_state="inactive",
        sub_state="dead",
        enabled_state="disabled",
        main_pid=None,
    )

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=lambda enabled: runtime,
    )

    assert view.cloudflared.label == "Stopped"
    assert "inactive/dead" in view.cloudflared.description
    assert "Startup state: disabled" in view.cloudflared.description


def test_token_managed_runtime_presentation_contains_no_secret() -> None:
    runtime = runtime_result(
        management_mode=ManagementMode.REMOTE_TOKEN,
        explicit_config_path=None,
    )

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=lambda enabled: runtime,
    )

    assert "Token-managed mode detected" in view.cloudflared.description
    assert FAKE_SECRET not in repr(view)


def test_discovered_config_path_is_not_implicitly_loaded() -> None:
    def unexpected_loader(path: Path) -> CloudflaredConfig:
        raise AssertionError(f"config loader called with {path}")

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        load_config=unexpected_loader,
        discover_runtime=lambda enabled: runtime_result(),
    )

    assert view.configuration.label == "Not configured"
    assert view.routes == ()


def test_runtime_discovery_error_becomes_safe_status() -> None:
    def failed_discovery(enabled: bool) -> CloudflaredRuntime:
        raise RuntimeDiscoveryError(FAKE_SECRET)

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=failed_discovery,
    )

    assert view.cloudflared.label == "Unavailable"
    assert FAKE_SECRET not in repr(view)
