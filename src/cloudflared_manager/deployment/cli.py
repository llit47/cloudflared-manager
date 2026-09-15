"""Root administration entry points for install, update, and safe configuration."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
from pathlib import Path

from cloudflared_manager.cloudflared.discovery import discover_cloudflared
from cloudflared_manager.deployment.bootstrap import BootstrapError, downloaded_source, resolve_main_sha
from cloudflared_manager.deployment.configurator import ConfigResult, Configurator
from cloudflared_manager.deployment.environment import read_environment
from cloudflared_manager.deployment.errors import (
    DeploymentError,
    RollbackError,
    TransactionFailedError,
)
from cloudflared_manager.deployment.health import wait_for_health
from cloudflared_manager.deployment.identity import ensure_service_identity
from cloudflared_manager.deployment.installer import Installer
from cloudflared_manager.deployment.network import select_lan_address, validate_local_bind_address
from cloudflared_manager.deployment.paths import DeploymentPaths
from cloudflared_manager.deployment.release import DeploymentLock, ReleaseFilesystem
from cloudflared_manager.deployment.service import IpNetworkInspector, SystemdManager
from cloudflared_manager.deployment.updater import Updater
from cloudflared_manager.deployment.validation import validate_port, validate_sha


def install_from_source(source: Path, sha: str, python: Path) -> int:
    """Production bootstrap handoff after exact source archive validation."""

    try:
        _require_root()
        paths = DeploymentPaths()
        with DeploymentLock(paths.lock_path):
            filesystem = ReleaseFilesystem(paths)
            service = SystemdManager()
            installer = Installer(
                paths,
                filesystem,
                service,
                _health,
                ensure_service_identity,
            )
            existing_current = paths.current.exists() or paths.current.is_symlink()
            if existing_current:
                result = installer.reconcile()
            else:
                inspector = IpNetworkInspector()
                bind_host = select_lan_address(
                    inspector,
                    os.environ.get("CFM_INSTALL_BIND_HOST"),
                )
                bind_port = validate_port(os.environ.get("CFM_INSTALL_BIND_PORT", "8000"))
                result = installer.install(
                    source,
                    validate_sha(sha),
                    python,
                    bind_host,
                    bind_port,
                )
            runtime = discover_cloudflared(True)
        _print_install_summary(
            (
                "reconciled"
                if existing_current and result.changed
                else "already installed"
                if existing_current
                else "installed successfully"
            ),
            result.sha,
            result.settings.bind_host,
            result.settings.bind_port,
            result.settings.runtime_discovery_enabled,
            runtime,
        )
        return 0
    except (DeploymentError, BootstrapError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return _error_code(error)
    except Exception:
        print("Installation failed because of an unexpected deployment error.", file=sys.stderr)
        return 1


def update() -> int:
    """Resolve main, install one exact candidate, and roll back failed health."""

    try:
        _require_root()
        paths = DeploymentPaths()
        filesystem = ReleaseFilesystem(paths)
        service = SystemdManager()
        updater = Updater(paths, filesystem, service, _health)
        curl = shutil.which("curl")
        if curl is None:
            raise DeploymentError("curl is required to resolve and download updates.")
        with DeploymentLock(paths.lock_path):
            candidate_sha = resolve_main_sha(curl)
            current_sha = filesystem.read_current_sha()
            source_context = (
                contextlib.nullcontext(None)
                if candidate_sha == current_sha
                else downloaded_source(candidate_sha, curl)
            )
            with source_context as source:
                result = updater.update(source, candidate_sha, Path(sys.executable))
        if candidate_sha == current_sha:
            if result.changed:
                print(f"Cloudflared Manager deployment reconciled at {result.sha}.")
            else:
                print(f"Cloudflared Manager is already up to date ({result.sha}).")
        else:
            print(f"Cloudflared Manager updated successfully to {result.sha}.")
        return 0
    except (DeploymentError, BootstrapError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return _error_code(error)
    except Exception:
        print("Update failed because of an unexpected deployment error.", file=sys.stderr)
        return 1


def configure(arguments: list[str]) -> int:
    """Run the supported read-only-release administration interface."""

    parser = argparse.ArgumentParser(prog="cfm-config")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="show sanitized manager status")
    bind_parser = subparsers.add_parser("set-bind", help="set a concrete RFC1918 bind address")
    bind_parser.add_argument("address")
    port_parser = subparsers.add_parser("set-port", help="set an unprivileged TCP port")
    port_parser.add_argument("port")
    discovery_parser = subparsers.add_parser("discovery", help="enable or disable runtime discovery")
    discovery_parser.add_argument("state", choices=("enable", "disable"))
    parsed = parser.parse_args(arguments)

    try:
        _require_root()
        paths = DeploymentPaths()
        service = SystemdManager()
        configurator = Configurator(paths, service, _health)
        if parsed.command is None:
            if not sys.stdin.isatty():
                parser.error("a subcommand is required when stdin is not interactive")
            return _interactive_config(paths, configurator)
        if parsed.command == "status":
            _print_status(configurator)
            return 0
        if parsed.command == "set-bind":
            updates = {
                "CFM_BIND_HOST": validate_local_bind_address(
                    IpNetworkInspector(),
                    parsed.address,
                )
            }
        elif parsed.command == "set-port":
            updates = {"CFM_BIND_PORT": str(validate_port(parsed.port))}
        else:
            updates = {
                "CFM_RUNTIME_DISCOVERY_ENABLED": (
                    "true" if parsed.state == "enable" else "false"
                )
            }
        result = _apply_config(paths, configurator, updates)
        _print_config_result(result)
        return 0
    except DeploymentError as error:
        print(f"Configuration failed: {error}", file=sys.stderr)
        return _error_code(error)
    except Exception:
        print("Configuration failed because of an unexpected deployment error.", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudflared-manager-deploy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("update")
    config_parser = subparsers.add_parser("config")
    config_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    if parsed.command == "update":
        return update()
    return configure(parsed.arguments)


def _interactive_config(paths: DeploymentPaths, configurator: Configurator) -> int:
    while True:
        print("\nCloudflared Manager configuration (capability: READ-ONLY)")
        print("1. Show status")
        print("2. Change bind address")
        print("3. Change bind port")
        print("4. Enable runtime discovery")
        print("5. Disable runtime discovery")
        print("6. Exit")
        choice = input("Selection: ").strip()
        if choice == "1":
            _print_status(configurator)
        elif choice == "2":
            value = validate_local_bind_address(
                IpNetworkInspector(),
                input("RFC1918 bind address: ").strip(),
            )
            _print_config_result(
                _apply_config(paths, configurator, {"CFM_BIND_HOST": value})
            )
        elif choice == "3":
            value = validate_port(input("Bind port (1024-65535): ").strip())
            _print_config_result(
                _apply_config(paths, configurator, {"CFM_BIND_PORT": str(value)})
            )
        elif choice == "4":
            _print_config_result(
                _apply_config(
                    paths,
                    configurator,
                    {"CFM_RUNTIME_DISCOVERY_ENABLED": "true"},
                )
            )
        elif choice == "5":
            _print_config_result(
                _apply_config(
                    paths,
                    configurator,
                    {"CFM_RUNTIME_DISCOVERY_ENABLED": "false"},
                )
            )
        elif choice == "6":
            return 0
        else:
            print("Choose a number from 1 through 6.")


def _print_status(configurator: Configurator) -> None:
    settings, states = configurator.status()
    load_state, active_state, sub_state = states
    _print_settings(settings.bind_host, settings.bind_port, settings.runtime_discovery_enabled)
    print("Capability: READ-ONLY")
    print("Cloudflare API: not configured / not supported by this release")
    print(f"Manager service: {active_state or 'unavailable'}")
    if load_state is not None:
        print(f"Service load state: {load_state}")
    if sub_state is not None:
        print(f"Service sub-state: {sub_state}")


def _apply_config(
    paths: DeploymentPaths,
    configurator: Configurator,
    updates: dict[str, str],
) -> ConfigResult:
    with DeploymentLock(paths.lock_path):
        return configurator.apply(updates)


def _print_config_result(result: ConfigResult) -> None:
    if result.changed:
        print("Manager configuration updated and verified healthy.")
    else:
        print("Manager configuration already has the requested value; no restart was needed.")
    _print_settings(
        result.settings.bind_host,
        result.settings.bind_port,
        result.settings.runtime_discovery_enabled,
    )


def _print_settings(host: str, port: int, discovery: bool) -> None:
    print(f"Manager URL: http://{host}:{port}")
    print(f"Runtime discovery: {'enabled' if discovery else 'disabled'}")


def _print_install_summary(
    outcome: str,
    sha: str,
    host: str,
    port: int,
    discovery_enabled: bool,
    runtime: object,
) -> None:
    binary_detected = getattr(runtime, "executable_exists", False)
    version = getattr(runtime, "version", None)
    active_state = getattr(runtime, "active_state", None)
    enabled_state = getattr(runtime, "enabled_state", None)
    print(f"\nCloudflared Manager {outcome}")
    print(f"Version: {sha}")
    print(f"Manager: http://{host}:{port}")
    print("Capability: READ-ONLY")
    print(f"Runtime discovery: {'enabled' if discovery_enabled else 'disabled'}")
    print(f"Cloudflared: {'detected' if binary_detected else 'not detected'}")
    if version is not None:
        print(f"Cloudflared version: {version}")
    print(f"Cloudflared service: {active_state or 'unavailable'}")
    print(f"Cloudflared startup: {enabled_state or 'unavailable'}")
    print("Cloudflared config: not adopted")
    print("Cloudflare API: not configured")
    print("Commands: sudo cfm-config | sudo cfm-update")


def _health(host: str, port: int) -> None:
    wait_for_health(host, port)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise DeploymentError("This administration command must run as root.")


def _error_code(error: Exception) -> int:
    if isinstance(error, RollbackError):
        return 3
    if isinstance(error, TransactionFailedError):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
