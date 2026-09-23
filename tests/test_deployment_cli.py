from pathlib import Path
from contextlib import nullcontext

from cloudflared_manager.deployment import cli
from cloudflared_manager.deployment.adoption import AdoptionStatus
from cloudflared_manager.deployment.configurator import ConfigResult
from cloudflared_manager.deployment.settings import ManagerSettings


def test_bridge_install_is_explicit_root_admin_action(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_require_root", lambda: None)
    monkeypatch.setattr(cli, "DeploymentLock", lambda path: nullcontext())
    monkeypatch.setattr(cli, "PROCESS_RELEASE_ID", "a" * 40)
    class Filesystem:
        def __init__(self, paths):
            pass
        def read_current_sha(self):
            return "a" * 40
    class Installer:
        def __init__(self, paths, filesystem):
            pass
        def install(self, release):
            calls.append(release)
            return True
    monkeypatch.setattr(cli, "ReleaseFilesystem", Filesystem)
    monkeypatch.setattr(cli, "BridgeInstaller", Installer)
    assert cli.configure(["install-bridge"]) == 0
    assert calls == [cli.DeploymentPaths().release("a" * 40)]
    assert "installed" in capsys.readouterr().out


class _LocalAddresses:
    def __init__(self, output: str) -> None:
        self.output = output

    def all_global_addresses(self) -> str:
        return self.output


def _isolate_config_cli(monkeypatch, addresses: str, applied: list[dict[str, str]]) -> None:
    monkeypatch.setattr(cli, "_require_root", lambda: None)
    monkeypatch.setattr(cli, "SystemdManager", lambda: object())
    monkeypatch.setattr(cli, "IpNetworkInspector", lambda: _LocalAddresses(addresses))
    monkeypatch.setattr(cli, "Configurator", lambda paths, service, health: object())

    def apply(paths, configurator, updates):
        applied.append(updates)
        return ConfigResult(
            changed=True,
            settings=ManagerSettings(
                bind_host=updates["CFM_BIND_HOST"],
                bind_port=8000,
                runtime_discovery_enabled=True,
            ),
        )

    monkeypatch.setattr(cli, "_apply_config", apply)


def test_set_bind_rejects_unassigned_address_before_configuration_mutation(
    monkeypatch,
    capsys,
) -> None:
    applied: list[dict[str, str]] = []
    _isolate_config_cli(
        monkeypatch,
        "2: eth0 inet 192.168.50.10/24 scope global eth0\n",
        applied,
    )

    result = cli.configure(["set-bind", "192.168.50.123"])

    assert result == 1
    assert applied == []
    assert "not assigned" in capsys.readouterr().err


def test_set_bind_accepts_address_on_secondary_local_interface(
    monkeypatch,
) -> None:
    applied: list[dict[str, str]] = []
    _isolate_config_cli(
        monkeypatch,
        "3: enp2s0 inet 192.168.50.123/24 scope global secondary enp2s0\n",
        applied,
    )

    result = cli.configure(["set-bind", "192.168.50.123"])

    assert result == 0
    assert applied == [{"CFM_BIND_HOST": "192.168.50.123"}]


def test_cloudflared_config_status_reports_adopted_and_detected_paths(
    monkeypatch,
    capsys,
) -> None:
    adopted = Path("/etc/cloudflared/config.yml")
    settings = ManagerSettings("192.168.1.20", 8000, True, adopted)
    runtime = type(
        "Runtime",
        (),
        {
            "management_mode": cli.ManagementMode.LOCAL_CONFIG,
            "explicit_config_path": adopted,
        },
    )()
    status = AdoptionStatus(settings, runtime, True)

    monkeypatch.setattr(cli, "_require_root", lambda: None)
    monkeypatch.setattr(cli, "SystemdManager", lambda: object())
    monkeypatch.setattr(cli, "Configurator", lambda *args: object())
    monkeypatch.setattr(
        cli,
        "CloudflaredConfigAdopter",
        lambda configurator: type("Adopter", (), {"status": lambda self: status})(),
    )

    assert cli.configure(["cloudflared-config", "status"]) == 0
    output = capsys.readouterr().out
    assert "Adoption state: adopted" in output
    assert f"Adopted config path: {adopted}" in output
    assert f"Detected local config candidate: {adopted}" in output


def test_cloudflared_config_subcommands_dispatch_without_path_argument(
    monkeypatch,
) -> None:
    calls: list[bool] = []
    settings = ManagerSettings("192.168.1.20", 8000, True)

    monkeypatch.setattr(cli, "_require_root", lambda: None)
    monkeypatch.setattr(cli, "SystemdManager", lambda: object())
    monkeypatch.setattr(cli, "Configurator", lambda *args: object())
    monkeypatch.setattr(cli, "CloudflaredConfigAdopter", lambda configurator: object())
    monkeypatch.setattr(
        cli,
        "_apply_adoption",
        lambda paths, adopter, *, clear: (
            calls.append(clear) or ConfigResult(True, settings)
        ),
    )

    assert cli.configure(["cloudflared-config", "adopt-detected"]) == 0
    assert cli.configure(["cloudflared-config", "clear"]) == 0
    assert calls == [False, True]
