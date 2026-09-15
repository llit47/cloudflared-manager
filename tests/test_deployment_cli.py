from cloudflared_manager.deployment import cli
from cloudflared_manager.deployment.configurator import ConfigResult
from cloudflared_manager.deployment.settings import ManagerSettings


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
