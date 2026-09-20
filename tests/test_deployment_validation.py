import pytest

from cloudflared_manager.deployment.errors import ValidationError
from cloudflared_manager.deployment.validation import (
    validate_bind_host,
    validate_port,
    validate_python_version,
    validate_sha,
)

SHA = "a" * 40


def test_python_312_and_newer_are_supported() -> None:
    assert validate_python_version((3, 12, 0)) == (3, 12)
    assert validate_python_version((3, 14)) == (3, 14)


@pytest.mark.parametrize("version", [(3, 11), (2, 7), (3,)])
def test_unsupported_python_is_rejected(version: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        validate_python_version(version)


def test_exact_sha_is_accepted() -> None:
    assert validate_sha(SHA) == SHA


@pytest.mark.parametrize("value", ["a" * 39, "a" * 41, "g" * 40, "main", "../release"])
def test_malformed_sha_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_sha(value)


@pytest.mark.parametrize("value", ["10.0.0.2", "172.16.1.4", "192.168.10.20"])
def test_rfc1918_bind_address_is_accepted(value: str) -> None:
    assert validate_bind_host(value) == value


@pytest.mark.parametrize(
    "value",
    ["0.0.0.0", "127.0.0.1", "169.254.1.1", "224.0.0.1", "8.8.8.8", "::1", "bad"],
)
def test_non_lan_bind_addresses_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_bind_host(value)


@pytest.mark.parametrize("value", ["0", "80", "65536", "8.5", "abc", " 08000"])
def test_unsafe_ports_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_port(value)


def test_unprivileged_port_is_accepted() -> None:
    assert validate_port("8000") == 8000
