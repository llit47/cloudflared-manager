from pathlib import Path

import pytest
import yaml

from cloudflared_manager.cloudflared.editing import (
    EditableCloudflaredConfig,
    MutationOutcome,
    MutationRejectedError,
    RoundTripYamlError,
    UnsupportedConfigStructureError,
    read_config_source_snapshot,
)

REPRESENTATIVE_CONFIG = '''# tunnel comment
tunnel: "example-tunnel"
credentials-file: "/some/private/path.json"
protocol: quic

warp-routing:
  enabled: true

originRequest:
  connectTimeout: 30s

unknown-top-level:
  retained: retained-value

ingress:
  # existing service
  - hostname: existing.example.com
    service: "http://127.0.0.1:8000"
    originRequest:
      httpHostHeader: existing.internal
    unknown-rule-field: retained

  # terminal fallback
  - service: http_status:404
'''


def write_source(tmp_path: Path, contents: str | bytes) -> Path:
    path = tmp_path / "config.yml"
    if isinstance(contents, bytes):
        path.write_bytes(contents)
    else:
        path.write_text(contents, encoding="utf-8")
    return path


def load_editor(tmp_path: Path, contents: str) -> EditableCloudflaredConfig:
    return EditableCloudflaredConfig.from_snapshot(
        read_config_source_snapshot(write_source(tmp_path, contents))
    )


def test_insert_preserves_human_config_and_places_rule_before_fallback(
    tmp_path: Path,
) -> None:
    editor = load_editor(tmp_path, REPRESENTATIVE_CONFIG)

    outcome = editor.insert_ingress_before_terminal_catch_all(
        {
            "hostname": "new.example.com",
            "service": "http://127.0.0.1:9000",
            "originRequest": {"connectTimeout": "10s"},
        }
    )
    rendered = editor.render_changed().decode("utf-8")
    parsed = yaml.safe_load(rendered)

    assert outcome is MutationOutcome.CHANGED
    assert parsed["tunnel"] == "example-tunnel"
    assert parsed["credentials-file"] == "/some/private/path.json"
    assert parsed["protocol"] == "quic"
    assert parsed["warp-routing"] == {"enabled": True}
    assert parsed["originRequest"] == {"connectTimeout": "30s"}
    assert parsed["unknown-top-level"] == {"retained": "retained-value"}
    assert parsed["ingress"][0] == {
        "hostname": "existing.example.com",
        "service": "http://127.0.0.1:8000",
        "originRequest": {"httpHostHeader": "existing.internal"},
        "unknown-rule-field": "retained",
    }
    assert parsed["ingress"][-2] == {
        "hostname": "new.example.com",
        "service": "http://127.0.0.1:9000",
        "originRequest": {"connectTimeout": "10s"},
    }
    assert parsed["ingress"][-1] == {"service": "http_status:404"}
    assert '# tunnel comment\ntunnel: "example-tunnel"' in rendered
    assert 'credentials-file: "/some/private/path.json"' in rendered
    assert 'service: "http://127.0.0.1:8000"' in rendered
    assert "# existing service" in rendered
    assert rendered.index("new.example.com") < rendered.index("# terminal fallback")
    assert rendered.index("# terminal fallback") < rendered.index("http_status:404")


def test_anchors_and_aliases_survive_round_trip_mutation(tmp_path: Path) -> None:
    editor = load_editor(
        tmp_path,
        """originRequest: &origin-defaults
  connectTimeout: 30s
copied-origin-request: *origin-defaults
ingress:
  - hostname: existing.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
""",
    )

    editor.insert_ingress_before_terminal_catch_all(
        {"hostname": "new.example.com", "service": "http://127.0.0.1:9000"}
    )
    rendered = editor.render_changed().decode("utf-8")

    assert "&origin-defaults" in rendered
    assert "*origin-defaults" in rendered
    parsed = yaml.safe_load(rendered)
    assert parsed["originRequest"] == parsed["copied-origin-request"]


def test_unchanged_document_refuses_rendering(tmp_path: Path) -> None:
    editor = load_editor(tmp_path, REPRESENTATIVE_CONFIG)

    with pytest.raises(MutationRejectedError, match="unchanged"):
        editor.render_changed()


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("- one\n- two\n", "root must be a mapping"),
        ("tunnel: example\n", "define an ingress sequence"),
        ("ingress: route\n", "must be a sequence"),
        ("ingress: []\n", "must not be empty"),
        ("ingress:\n  - route\n", "must be a mapping"),
        (
            "ingress:\n  - service: http_status:404\n"
            "  - hostname: later.example.com\n    service: http://localhost\n",
            "catch-all ingress entry must be the final",
        ),
        (
            "ingress:\n  - hostname: app.example.com\n    service: http://localhost\n",
            "final ingress entry must be a catch-all",
        ),
        ("ingress:\n  - hostname: app.example.com\n  - service: http_status:404\n", "service"),
    ],
)
def test_invalid_ingress_structures_are_rejected(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    with pytest.raises(UnsupportedConfigStructureError, match=message):
        editor = load_editor(tmp_path, contents)
        editor.insert_ingress_before_terminal_catch_all(
            {"hostname": "new.example.com", "service": "http://localhost:9000"}
        )


@pytest.mark.parametrize(
    "contents",
    [
        "ingress: [\n",
        "value: !python/object:example dangerous\ningress:\n  - service: http_status:404\n",
    ],
)
def test_malformed_duplicate_or_tagged_yaml_fails_closed(
    tmp_path: Path,
    contents: str,
) -> None:
    path = write_source(tmp_path, contents)

    with pytest.raises(RoundTripYamlError):
        EditableCloudflaredConfig.from_snapshot(read_config_source_snapshot(path))


def test_duplicate_mapping_keys_are_rejected(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        """tunnel: first
tunnel: second
ingress:
  - service: http_status:404
""",
    )

    with pytest.raises(RoundTripYamlError):
        EditableCloudflaredConfig.from_snapshot(read_config_source_snapshot(path))


@pytest.mark.parametrize(
    "rule",
    [
        {"service": "http://localhost:9000"},
        {"hostname": "new.example.com", "service": ""},
        {"hostname": 42, "service": "http://localhost:9000"},
        {"hostname": "new.example.com", "service": "http://localhost", "bad": object()},
    ],
)
def test_insert_rejects_unsafe_new_rules(tmp_path: Path, rule: dict[str, object]) -> None:
    editor = load_editor(tmp_path, REPRESENTATIVE_CONFIG)

    with pytest.raises((MutationRejectedError, UnsupportedConfigStructureError)):
        editor.insert_ingress_before_terminal_catch_all(rule)


def test_invalid_utf8_is_rejected_without_content_disclosure(tmp_path: Path) -> None:
    path = write_source(tmp_path, b"secret-token: \xff\ningress: []\n")

    with pytest.raises(RoundTripYamlError) as captured:
        EditableCloudflaredConfig.from_snapshot(read_config_source_snapshot(path))

    assert "secret-token" not in str(captured.value)
