"""Local editor and selector tests use disposable configuration only."""

import pytest
import yaml

from cloudflared_manager.cloudflared.editing import (
    EditableCloudflaredConfig, MutationOutcome, MutationRejectedError,
    StaleMutationError, UnsupportedConfigStructureError, read_config_source_snapshot,
)
from cloudflared_manager.cloudflared.editing.local_ingress import (
    MAX_ROUTE_POSITION, LocalRoute, RouteSelector,
)
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


def test_add_position_is_always_addressable_and_rejects_next_position(tmp_path):
    route = LocalRoute("new.example.com", None, "http://127.0.0.1:9000")
    def source(count):
        return ("ingress:\n" + "".join(
            f"  - hostname: route{i}.example.com\n    service: http://127.0.0.1:8000\n"
            for i in range(count)
        ) + "  - service: http_status:404\n")

    supported = editor(tmp_path, source(MAX_ROUTE_POSITION))
    assert supported.add_local_hostname_ingress(route) == MutationOutcome.CHANGED
    assert supported.local_route_selector(MAX_ROUTE_POSITION).position == MAX_ROUTE_POSITION
    assert RouteSelector(MAX_ROUTE_POSITION, "a" * 64).position == MAX_ROUTE_POSITION

    unsupported = editor(tmp_path, source(MAX_ROUTE_POSITION + 1))
    with pytest.raises(MutationRejectedError):
        unsupported.add_local_hostname_ingress(route)
    assert unsupported.changed is False
    with pytest.raises(MutationRejectedError):
        RouteSelector(MAX_ROUTE_POSITION + 1, "a" * 64)

    class ForbiddenStager:
        def stage(self, *_args):
            pytest.fail("an unaddressable Add must not stage a candidate")

    with pytest.raises(MutationRejectedError):
        prepare_validated_candidate(
            tmp_path / "config.yml", lambda document: document.add_local_hostname_ingress(route),
            stager=ForbiddenStager(),
        )


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


MERGED_ROUTE = """ingress:
  - &base
    hostname: one.example.com
    service: http://127.0.0.1:8000
  - <<: *base
    hostname: two.example.com
  - service: http_status:404
"""


@pytest.mark.parametrize("source,operation", [
    (MERGED_ROUTE, "add"),
    (MERGED_ROUTE, "edit"),
    (MERGED_ROUTE, "delete"),
    ("""ingress: &routes
  - hostname: one.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
copy: *routes
""", "edit"),
    ("""ingress:
  - &first
    hostname: one.example.com
    service: http://127.0.0.1:8000
  - *first
  - service: http_status:404
""", "delete"),
    ("""ingress:
  - hostname: one.example.com
    service: http://127.0.0.1:8000
    originRequest: &options
      connectTimeout: 30s
  - hostname: two.example.com
    service: http://127.0.0.1:8001
    originRequest: *options
  - service: http_status:404
""", "delete"),
    ("""base: &base
  service: http://127.0.0.1:8000
ingress:
  - <<: *base
    hostname: one.example.com
  - service: http_status:404
""", "edit"),
])
def test_local_operations_reject_anchors_aliases_and_merges(tmp_path, source, operation):
    document = editor(tmp_path, source)
    route = LocalRoute("new.example.com", None, "http://127.0.0.1:9000")
    selector = document.local_route_selector(0) if operation != "add" else None
    with pytest.raises(UnsupportedConfigStructureError):
        if operation == "add":
            document.add_local_hostname_ingress(route)
        elif operation == "edit":
            document.edit_local_hostname_ingress(selector, route)
        else:
            document.delete_local_hostname_ingress(selector)
    assert not document.changed


def test_merge_dependency_rejected_before_candidate_staging_or_activation(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(MERGED_ROUTE)
    observed = read_config_source_snapshot(path)
    class ForbiddenStager:
        def stage(self, snapshot, contents):
            pytest.fail("merge dependency reached candidate staging")
    class ForbiddenValidator:
        def validate(self, candidate):
            pytest.fail("merge dependency reached cloudflared validation")
    def edit(document):
        return document.edit_local_hostname_ingress(
            document.local_route_selector(0),
            LocalRoute("one.example.com", None, "http://127.0.0.1:9000"),
        )
    with pytest.raises(UnsupportedConfigStructureError):
        prepare_validated_candidate(path, edit, stager=ForbiddenStager(),
                                    cloudflared_validator=ForbiddenValidator(),
                                    expected_source_revision=observed.sha256)
    assert path.read_bytes() == observed.original_bytes
    assert list(tmp_path.glob(".cfm-candidate-*")) == []


@pytest.mark.parametrize("source,removed,following_comment", [
    ("""ingress:
  - hostname: one.example.com
    service: http://1
  # route two
  - hostname: two.example.com
    service: http://2
  - service: http_status:404
""", 0, "# route two"),
    ("""ingress:
  - hostname: zero.example.com
    service: http://0
  - hostname: one.example.com
    service: http://1 # deleted route
  # route two
  - hostname: two.example.com
    service: http://2
  - service: http_status:404
""", 1, "# route two"),
    ("""ingress:
  - hostname: one.example.com
    service: http://1
  # fallback
  - service: http_status:404
""", 0, "# fallback"),
    ("""ingress:
  - hostname: one.example.com
    service: http://1
    originRequest:
      connectTimeout: 30s
  # fallback
  - service: http_status:404
""", 0, "# fallback"),
    ("""ingress:
  - hostname: one.example.com
    service: http://1
    originRequest:
      headers:
        - foo
  # fallback
  - service: http_status:404
""", 0, "# fallback"),
    ("""ingress:
  - hostname: one.example.com
    service: http://1
    note: |
      some text
  # fallback
  - service: http_status:404
""", 0, "# fallback"),
])
def test_delete_preserves_comment_before_unchanged_following_route(
    tmp_path, source, removed, following_comment,
):
    document = editor(tmp_path, source)
    document.delete_local_hostname_ingress(document.local_route_selector(removed))
    rendered = document.render_changed().decode()
    assert rendered.count(following_comment) == 1
    assert rendered.index(following_comment) < rendered.index(
        "two.example.com" if following_comment == "# route two" else "http_status:404"
    )
    if "# deleted route" in source:
        assert "# deleted route" not in rendered
    parsed = yaml.safe_load(rendered)
    assert parsed["ingress"][-1] == {"service": "http_status:404"}
