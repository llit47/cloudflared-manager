import pytest

from cloudflared_manager.deployment.errors import NetworkSelectionError, ValidationError
from cloudflared_manager.deployment.network import select_lan_address


class FakeNetwork:
    def __init__(self, routes: str, addresses: str) -> None:
        self.routes = routes
        self.addresses = addresses
        self.address_interface: str | None = None

    def default_routes(self) -> str:
        return self.routes

    def global_addresses(self, interface: str) -> str:
        self.address_interface = interface
        return self.addresses

    def all_global_addresses(self) -> str:
        return self.addresses


def test_selects_single_rfc1918_address_on_preferred_default_route() -> None:
    network = FakeNetwork(
        "default via 192.168.1.1 dev enp1s0 proto dhcp metric 100\n"
        "default via 172.18.0.1 dev docker0 metric 500\n",
        "2: enp1s0    inet 192.168.1.20/24 brd 192.168.1.255 scope global enp1s0\n",
    )

    assert select_lan_address(network) == "192.168.1.20"
    assert network.address_interface == "enp1s0"


def test_no_lan_candidate_fails_actionably() -> None:
    network = FakeNetwork(
        "default via 203.0.113.1 dev eth0 metric 100\n",
        "2: eth0 inet 203.0.113.10/24 scope global eth0\n",
    )

    with pytest.raises(NetworkSelectionError, match="CFM_INSTALL_BIND_HOST"):
        select_lan_address(network)


def test_multiple_addresses_fail_instead_of_guessing() -> None:
    network = FakeNetwork(
        "default via 10.0.0.1 dev eth0 metric 100\n",
        "2: eth0 inet 10.0.0.2/24 scope global eth0\n"
        "2: eth0 inet 10.0.0.3/24 scope global secondary eth0\n",
    )

    with pytest.raises(NetworkSelectionError, match="multiple"):
        select_lan_address(network)


def test_equal_default_route_metrics_fail_instead_of_guessing() -> None:
    network = FakeNetwork(
        "default via 10.0.0.1 dev eth0 metric 100\n"
        "default via 192.168.1.1 dev wlan0 metric 100\n",
        "",
    )

    with pytest.raises(NetworkSelectionError, match="Multiple"):
        select_lan_address(network)


def test_explicit_override_must_be_assigned_locally_without_default_route_lookup() -> None:
    class ExplicitNetwork:
        def default_routes(self) -> str:
            raise AssertionError("default-route inspection must not run")

        def global_addresses(self, interface: str) -> str:
            raise AssertionError("interface-specific inspection must not run")

        def all_global_addresses(self) -> str:
            return "3: enp2s0 inet 10.20.30.40/24 scope global secondary enp2s0\n"

    assert select_lan_address(ExplicitNetwork(), "10.20.30.40") == "10.20.30.40"


def test_explicit_unassigned_rfc1918_override_is_rejected() -> None:
    network = FakeNetwork(
        "",
        "3: enp2s0 inet 10.20.30.41/24 scope global enp2s0\n",
    )

    with pytest.raises(NetworkSelectionError, match="not assigned"):
        select_lan_address(network, "10.20.30.40")


def test_explicit_override_rejects_non_global_or_virtual_interface_address() -> None:
    network = FakeNetwork(
        "",
        "2: eth0 inet 10.20.30.40/24 scope link eth0\n"
        "3: docker0 inet 10.20.30.40/24 scope global docker0\n",
    )

    with pytest.raises(NetworkSelectionError, match="not assigned"):
        select_lan_address(network, "10.20.30.40")


def test_explicit_wildcard_override_is_rejected() -> None:
    with pytest.raises(ValidationError):
        select_lan_address(FakeNetwork("", ""), "0.0.0.0")


def test_malformed_interface_from_route_output_is_not_executed() -> None:
    network = FakeNetwork("default via 10.0.0.1 dev eth0;touch metric 1\n", "")

    with pytest.raises(NetworkSelectionError):
        select_lan_address(network)

    assert network.address_interface is None
