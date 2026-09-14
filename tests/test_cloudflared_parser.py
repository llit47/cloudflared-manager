from pathlib import Path

import pytest

from cloudflared_manager.cloudflared import (
    ConfigFileNotFoundError,
    ConfigFileUnreadableError,
    ConfigInvalidYamlError,
    ConfigStructureError,
    parse_cloudflared_config,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cloudflared" / "config.yml"


def write_config(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_valid_fixture_parses_expected_projection_without_modification() -> None:
    original_contents = FIXTURE_PATH.read_text(encoding="utf-8")

    config = parse_cloudflared_config(FIXTURE_PATH)

    assert config.tunnel == "00000000-0000-4000-8000-000000000000"
    assert [rule.service for rule in config.ingress_rules] == [
        "http://localhost:8080",
        "http://localhost:3000",
        "http_status:404",
    ]
    assert config.ingress_rules[0].hostname == "dashboard.example.com"
    assert config.ingress_rules[0].path == "^/admin/.*"
    assert config.ingress_rules[1].hostname == "photos.example.com"
    assert config.ingress_rules[1].path is None
    assert FIXTURE_PATH.read_text(encoding="utf-8") == original_contents


def test_terminal_catch_all_is_preserved_and_excluded_from_hostname_routes() -> None:
    config = parse_cloudflared_config(FIXTURE_PATH)

    assert len(config.ingress_rules) == 3
    assert config.ingress_rules[-1].is_catch_all is True
    assert config.ingress_rules[-1].hostname is None
    assert config.ingress_rules[-1].path is None
    assert [rule.hostname for rule in config.hostname_routes] == [
        "dashboard.example.com",
        "photos.example.com",
    ]


def test_tunnel_is_optional_for_valid_configuration(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """ingress:
  - service: http_status:404
""",
    )

    config = parse_cloudflared_config(path)

    assert config.tunnel is None
    assert config.hostname_routes == ()
    assert config.ingress_rules[-1].is_catch_all is True


def test_missing_file_raises_safe_project_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "secret-token-config.yml"

    with pytest.raises(ConfigFileNotFoundError) as captured:
        parse_cloudflared_config(missing_path)

    assert str(missing_path) not in str(captured.value)
    assert "secret-token" not in str(captured.value)


def test_unreadable_path_raises_safe_project_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileUnreadableError, match="could not be read"):
        parse_cloudflared_config(tmp_path)


def test_invalid_yaml_does_not_echo_file_contents(tmp_path: Path) -> None:
    path = write_config(tmp_path, "api_token: do-not-expose\ningress: [")

    with pytest.raises(ConfigInvalidYamlError) as captured:
        parse_cloudflared_config(path)

    assert "do-not-expose" not in str(captured.value)
    assert "api_token" not in str(captured.value)


@pytest.mark.parametrize("document", ["- one\n- two\n", "just-a-string\n", "null\n"])
def test_yaml_root_must_be_a_mapping(tmp_path: Path, document: str) -> None:
    path = write_config(tmp_path, document)

    with pytest.raises(ConfigStructureError, match="root must be a mapping"):
        parse_cloudflared_config(path)


@pytest.mark.parametrize(
    "ingress_yaml",
    [
        "tunnel: fake-tunnel\n",
        "ingress: null\n",
        "ingress: {}\n",
        "ingress: route\n",
        "ingress: []\n",
    ],
)
def test_ingress_must_be_a_non_empty_list(
    tmp_path: Path,
    ingress_yaml: str,
) -> None:
    path = write_config(tmp_path, ingress_yaml)

    with pytest.raises(ConfigStructureError, match="non-empty list"):
        parse_cloudflared_config(path)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("not-a-mapping", "must be a mapping"),
        ({"hostname": 42, "service": "http://localhost:8000"}, "hostname"),
        ({"path": ["/admin"], "service": "http://localhost:8000"}, "path"),
    ],
)
def test_malformed_ingress_entries_are_rejected(
    tmp_path: Path,
    entry: object,
    message: str,
) -> None:
    import yaml

    document = {
        "ingress": [entry, {"service": "http_status:404"}],
    }
    path = write_config(tmp_path, yaml.safe_dump(document))

    with pytest.raises(ConfigStructureError, match=message):
        parse_cloudflared_config(path)


@pytest.mark.parametrize("service", [None, "", "   ", 8080])
def test_ingress_service_must_be_a_non_empty_string(
    tmp_path: Path,
    service: object,
) -> None:
    import yaml

    document = {
        "ingress": [
            {"hostname": "app.example.com", "service": service},
            {"service": "http_status:404"},
        ]
    }
    path = write_config(tmp_path, yaml.safe_dump(document))

    with pytest.raises(ConfigStructureError, match="non-empty 'service'"):
        parse_cloudflared_config(path)


def test_catch_all_must_be_last(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """ingress:
  - service: http_status:404
  - hostname: app.example.com
    service: http://localhost:8000
""",
    )

    with pytest.raises(ConfigStructureError, match="must be last"):
        parse_cloudflared_config(path)


def test_final_rule_must_be_a_catch_all(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """ingress:
  - hostname: app.example.com
    service: http://localhost:8000
""",
    )

    with pytest.raises(ConfigStructureError, match="final ingress rule"):
        parse_cloudflared_config(path)


def test_tunnel_must_be_a_non_empty_string_when_present(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """tunnel: 123
ingress:
  - service: http_status:404
""",
    )

    with pytest.raises(ConfigStructureError, match="Tunnel"):
        parse_cloudflared_config(path)
