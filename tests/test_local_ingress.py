"""Local editor and selector tests use disposable configuration only."""

import pytest
import yaml

from cloudflared_manager.cloudflared.editing import (
    EditableCloudflaredConfig, MutationOutcome, MutationRejectedError,
    StaleMutationError, UnsupportedConfigStructureError, read_config_source_snapshot,
)
from cloudflared_manager.cloudflared.editing.local_ingress import LocalRoute, RouteSelector
from cloudflared_manager.cloudflared.editing.preparation import prepare_validated_candidate

SOURCE = """# retained
other: value
ingress:
  - hostname: one.example.com
    service: http://127.0.0.1:8000
    originRequest:
      connectTimeout: 30s
  - hostname: two.example.com
    service: http://127.0.0.1:8001
  # fallback
  - service: http_status:404
"""


def editor(tmp_path, source=SOURCE):
    path = tmp_path / "config.yml"
    path.write_text(source)
    return EditableCloudflaredConfig.from_snapshot(read_config_source_snapshot(path))


def test_add_before_catch_all_and_duplicate_matcher_rejected(tmp_path):
    document = editor(tmp_path)
    assert document.add_local_hostname_ingress(LocalRoute("new.example.com", None, "http://127.0.0.1:9000")) == MutationOutcome.CHANGED
    rendered = document.render_changed().decode()
    assert rendered.index("new.example.com") < rendered.index("# fallback")
    assert yaml.safe_load(rendered)["ingress"][-1] == {"service": "http_status:404"}
    with pytest.raises(MutationRejectedError):
        editor(tmp_path).add_local_hostname_ingress(LocalRoute("one.example.com", None, "http://127.0.0.1:9000"))


def test_edit_preserves_unrelated_yaml_and_noop(tmp_path):
    document = editor(tmp_path)
    selected = document.local_route_selector(0)
    assert document.edit_local_hostname_ingress(selected, LocalRoute("one.example.com", None, "http://127.0.0.1:8000")) == MutationOutcome.NO_CHANGE
    assert document.edit_local_hostname_ingress(selected, LocalRoute("new.example.com", "/api", "https://127.0.0.1:8443")) == MutationOutcome.CHANGED
    rendered = document.render_changed().decode()
    parsed = yaml.safe_load(rendered)
    assert parsed["other"] == "value"
    assert parsed["ingress"][0]["originRequest"] == {"connectTimeout": "30s"}
    assert parsed["ingress"][1] == {"hostname": "two.example.com", "service": "http://127.0.0.1:8001"}
    assert "# retained" in rendered


def test_delete_targets_exact_position_and_rejects_catch_all(tmp_path):
    document = editor(tmp_path)
    selector = document.local_route_selector(1)
    assert document.delete_local_hostname_ingress(selector) == MutationOutcome.CHANGED
    assert [item.get("hostname") for item in yaml.safe_load(document.render_changed())["ingress"]] == ["one.example.com", None]
    with pytest.raises((MutationRejectedError, StaleMutationError)):
        editor(tmp_path).delete_local_hostname_ingress(RouteSelector(2, selector.fingerprint))


def test_reordered_route_selector_fails_closed(tmp_path):
    original = editor(tmp_path)
    selector = original.local_route_selector(0)
    changed = editor(tmp_path, SOURCE.replace("one.example.com", "other.example.com"))
    with pytest.raises(StaleMutationError):
        changed.delete_local_hostname_ingress(selector)


def test_identical_routes_are_disambiguated_by_position(tmp_path):
    duplicate = SOURCE.replace("two.example.com", "one.example.com").replace("8001", "8000")
    document = editor(tmp_path, duplicate)
    first = document.local_route_selector(0)
    second = document.local_route_selector(1)
    assert first.fingerprint == second.fingerprint
    document.delete_local_hostname_ingress(second)
    assert len(yaml.safe_load(document.render_changed())["ingress"]) == 2


@pytest.mark.parametrize("route", [
    ("bad/name", None, "http://localhost"),
    ("one.example.com", "\n", "http://localhost"),
    ("one.example.com", None, "$(id)\n"),
])
def test_hostile_domain_values_rejected(route):
    with pytest.raises(MutationRejectedError):
        LocalRoute(*route)


def test_supported_regex_path_and_no_disable_or_filesystem_service():
    assert LocalRoute("one.example.com", "^/admin/.*", "http://127.0.0.1:8000").path == "^/admin/.*"
    for service in ("http_status:404", "file:///etc/passwd", "unix:///run/service.sock"):
        with pytest.raises(MutationRejectedError):
            LocalRoute("one.example.com", None, service)


def test_manual_source_edit_rejected_before_mutation_or_staging(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(SOURCE)
    observed = read_config_source_snapshot(path)
    path.write_text(SOURCE.replace("other: value", "other: manually-edited"))
    def forbidden_mutation(document):
        pytest.fail("stale request reached editor")
    class Validator:
        def validate(self, candidate):
            pytest.fail("stale request reached validator")
    with pytest.raises(StaleMutationError):
        prepare_validated_candidate(path, forbidden_mutation, cloudflared_validator=Validator(),
                                    expected_source_revision=observed.sha256)
    assert list(tmp_path.glob(".cfm-candidate-*")) == []


def test_edit_rejects_route_mapping_aliased_elsewhere(tmp_path):
    source = """ingress:
  - &shared
    hostname: one.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
unrelated: *shared
"""
    document = editor(tmp_path, source)
    selected = document.local_route_selector(0)
    with pytest.raises(UnsupportedConfigStructureError):
        document.edit_local_hostname_ingress(
            selected, LocalRoute("new.example.com", None, "http://127.0.0.1:9000"))
