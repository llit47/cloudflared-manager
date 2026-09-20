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


def loaded_config() -> CloudflaredConfig:
    return CloudflaredConfig(
        tunnel="fake-tunnel",
        ingress_rules=(
            IngressRule(hostname="app.example.com", service="http://localhost:8080"),
            IngressRule(service="http_status:404", is_catch_all=True),
        ),
    )


def test_no_path_does_not_call_config_loader() -> None:
    def unexpected_loader(path: Path) -> CloudflaredConfig:
        raise AssertionError(f"loader called with {path}")

    view = build_dashboard_view(Settings(mode="test"), unexpected_loader)

    assert view.configuration.label == "Not configured"
    assert view.config_source.label == "Discovery disabled"
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
    assert view.config_source.label == "Adopted · Load error"
    assert view.routes == ()
    assert "/private" not in view.configuration.description
    assert "secret-config" not in view.configuration.description
    assert "/private" not in repr(view)
    assert "secret-config" not in repr(view)


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


def test_unavailable_service_inspection_has_distinct_config_source_state() -> None:
    runtime = runtime_result(
        systemd_available=False,
        service_exists=None,
        load_state=None,
        active_state=None,
        sub_state=None,
        enabled_state=None,
        main_pid=None,
        management_mode=ManagementMode.UNKNOWN,
        explicit_config_path=None,
    )

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=lambda enabled: runtime,
    )

    assert view.config_source.label == "Discovery unavailable"
    assert view.config_source.tone == "warning"


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
    assert view.config_source.label == "Token-managed"
    assert "no local config candidate" in view.config_source.description
    assert FAKE_SECRET not in repr(view)


def test_detected_config_is_not_adopted_or_implicitly_loaded() -> None:
    detected_path = Path("/private/cloudflared/detected-config.yml")

    def unexpected_loader(path: Path) -> CloudflaredConfig:
        raise AssertionError(f"config loader called with {path}")

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        load_config=unexpected_loader,
        discover_runtime=lambda enabled: runtime_result(
            explicit_config_path=detected_path,
        ),
    )

    assert view.configuration.label == "Not configured"
    assert view.config_source.label == "Detected · Not adopted"
    assert view.config_source.tone == "warning"
    assert "sudo cfm-config cloudflared-config adopt-detected" in (
        view.config_source.description
    )
    assert view.routes == ()
    assert str(detected_path) not in repr(view)


def test_adopted_config_matching_runtime_is_successful() -> None:
    adopted_path = Path("/srv/cloudflared/adopted.yml")
    loaded_paths: list[Path] = []

    def loader(path: Path) -> CloudflaredConfig:
        loaded_paths.append(path)
        return loaded_config()

    view = build_dashboard_view(
        Settings(
            mode="test",
            runtime_discovery_enabled=True,
            cloudflared_config_path=adopted_path,
        ),
        load_config=loader,
        discover_runtime=lambda enabled: runtime_result(
            explicit_config_path=adopted_path,
        ),
    )

    assert loaded_paths == [adopted_path]
    assert view.config_source.label == "Adopted"
    assert view.config_source.tone == "success"
    assert view.configuration.label == "Loaded"
    assert view.route_count == 1
    assert str(adopted_path) not in repr(view)


def test_adopted_config_different_from_runtime_is_warning_without_paths() -> None:
    adopted_path = Path("/srv/cloudflared/adopted.yml")
    detected_path = Path("/private/cloudflared/different.yml")

    view = build_dashboard_view(
        Settings(
            mode="test",
            runtime_discovery_enabled=True,
            cloudflared_config_path=adopted_path,
        ),
        load_config=lambda path: loaded_config(),
        discover_runtime=lambda enabled: runtime_result(
            explicit_config_path=detected_path,
        ),
    )

    assert view.config_source.label == "Adopted · Service differs"
    assert view.config_source.tone == "warning"
    assert "No change was made automatically" in view.config_source.description
    assert str(adopted_path) not in repr(view)
    assert str(detected_path) not in repr(view)


def test_adopted_config_does_not_claim_match_without_a_discovered_service() -> None:
    adopted_path = Path("/srv/cloudflared/adopted.yml")

    view = build_dashboard_view(
        Settings(
            mode="test",
            runtime_discovery_enabled=True,
            cloudflared_config_path=adopted_path,
        ),
        load_config=lambda path: loaded_config(),
        discover_runtime=lambda enabled: runtime_result(
            service_exists=False,
            load_state="not-found",
            explicit_config_path=adopted_path,
        ),
    )

    assert view.config_source.label == "Adopted · Unverified"
    assert "could not be verified" in view.config_source.description


def test_adopted_config_remains_loaded_when_discovery_is_disabled() -> None:
    adopted_path = Path("/srv/cloudflared/adopted.yml")

    def unexpected_discovery(enabled: bool) -> CloudflaredRuntime:
        raise AssertionError(f"runtime discovery called with {enabled}")

    view = build_dashboard_view(
        Settings(mode="test", cloudflared_config_path=adopted_path),
        load_config=lambda path: loaded_config(),
        discover_runtime=unexpected_discovery,
    )

    assert view.config_source.label == "Adopted · Unverified"
    assert "disabled" in view.config_source.description
    assert view.configuration.label == "Loaded"
    assert view.route_count == 1


def test_adopted_config_remains_loaded_when_discovery_fails() -> None:
    def failed_discovery(enabled: bool) -> CloudflaredRuntime:
        raise RuntimeDiscoveryError(FAKE_SECRET)

    view = build_dashboard_view(
        Settings(
            mode="test",
            runtime_discovery_enabled=True,
            cloudflared_config_path=Path("/srv/cloudflared/adopted.yml"),
        ),
        load_config=lambda path: loaded_config(),
        discover_runtime=failed_discovery,
    )

    assert view.config_source.label == "Adopted · Unverified"
    assert "unavailable" in view.config_source.description
    assert view.configuration.label == "Loaded"
    assert view.route_count == 1
    assert FAKE_SECRET not in repr(view)


def test_dashboard_build_discovers_runtime_only_once() -> None:
    calls: list[bool] = []

    def counting_discovery(enabled: bool) -> CloudflaredRuntime:
        calls.append(enabled)
        return runtime_result()

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=counting_discovery,
    )

    assert calls == [True]
    assert view.cloudflared.label == "Running"
    assert view.config_source.label == "Detected · Not adopted"


def test_runtime_discovery_error_becomes_safe_status() -> None:
    def failed_discovery(enabled: bool) -> CloudflaredRuntime:
        raise RuntimeDiscoveryError(FAKE_SECRET)

    view = build_dashboard_view(
        Settings(mode="test", runtime_discovery_enabled=True),
        discover_runtime=failed_discovery,
    )

    assert view.cloudflared.label == "Unavailable"
    assert FAKE_SECRET not in repr(view)
