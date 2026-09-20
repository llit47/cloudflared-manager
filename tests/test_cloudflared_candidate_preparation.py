from pathlib import Path

import pytest

from cloudflared_manager.cloudflared import ConfigInvalidYamlError
from cloudflared_manager.cloudflared.editing import (
    ApplicationValidationError,
    CandidateFileError,
    CloudflaredValidationRejectedError,
    CloudflaredValidationReport,
    MutationOutcome,
    PreparationOutcome,
    SourceConfigChangedError,
    prepare_validated_candidate,
)

SOURCE_TEXT = """# source remains unchanged
tunnel: "example-tunnel"
ingress:
  - hostname: existing.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
"""


class AcceptingValidator:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.calls = 0

    def validate(self, candidate):
        self.calls += 1
        if self.events is not None:
            self.events.append("cloudflared")
        candidate.require_intact()
        return CloudflaredValidationReport()


def write_source(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(SOURCE_TEXT, encoding="utf-8")
    return path


def insert_rule(document) -> MutationOutcome:
    return document.insert_ingress_before_terminal_catch_all(
        {"hostname": "new.example.com", "service": "http://127.0.0.1:9000"}
    )


def candidates(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob(".cfm-candidate-*.yaml"))


def test_success_runs_application_parser_before_external_validation(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path)
    original = source.read_bytes()
    events: list[str] = []
    validator = AcceptingValidator(events)

    def application_parser(path: Path) -> object:
        events.append("application")
        assert path.parent.parent == Path("/proc/self/fd")
        assert str(tmp_path) not in str(path)
        assert "new.example.com" in path.read_text(encoding="utf-8")
        return object()

    result = prepare_validated_candidate(
        source,
        insert_rule,
        application_parser=application_parser,
        cloudflared_validator=validator,
    )
    candidate_path = result.candidate.path if result.candidate else None
    try:
        assert result.outcome is PreparationOutcome.VALIDATED_CANDIDATE
        assert result.changed is True
        assert result.cloudflared_validation.accepted is True
        assert events == ["application", "cloudflared"]
        assert candidate_path is not None and candidate_path.exists()
        assert source.read_bytes() == original
    finally:
        result.discard()

    assert candidate_path is not None and not candidate_path.exists()
    assert source.read_bytes() == original


def test_noop_is_explicit_and_creates_no_candidate(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    original = source.read_bytes()
    validator = AcceptingValidator()

    result = prepare_validated_candidate(
        source,
        lambda document: MutationOutcome.NO_CHANGE,
        cloudflared_validator=validator,
    )

    assert result.outcome is PreparationOutcome.NO_CHANGE
    assert result.changed is False
    assert result.candidate is None
    assert validator.calls == 0
    assert candidates(tmp_path) == []
    assert source.read_bytes() == original


def test_application_rejection_prevents_external_acceptance_and_cleans_candidate(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path)
    original = source.read_bytes()
    validator = AcceptingValidator()

    def rejecting_parser(path: Path) -> object:
        raise ConfigInvalidYamlError("safe parser failure")

    with pytest.raises(ApplicationValidationError):
        prepare_validated_candidate(
            source,
            insert_rule,
            application_parser=rejecting_parser,
            cloudflared_validator=validator,
        )

    assert validator.calls == 0
    assert candidates(tmp_path) == []
    assert source.read_bytes() == original


def test_cloudflared_rejection_cleans_candidate_without_changing_source(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path)
    original = source.read_bytes()

    class RejectingValidator:
        def validate(self, candidate):
            candidate.require_intact()
            raise CloudflaredValidationRejectedError(
                "Cloudflared rejected the candidate configuration."
            )

    with pytest.raises(CloudflaredValidationRejectedError):
        prepare_validated_candidate(
            source,
            insert_rule,
            cloudflared_validator=RejectingValidator(),
        )

    assert candidates(tmp_path) == []
    assert source.read_bytes() == original


def test_source_change_during_external_validation_rejects_and_cleans_candidate(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path)

    class SourceChangingValidator:
        def validate(self, candidate):
            candidate.require_intact()
            source.write_text(SOURCE_TEXT + "# concurrent edit\n", encoding="utf-8")
            return CloudflaredValidationReport()

    with pytest.raises(SourceConfigChangedError):
        prepare_validated_candidate(
            source,
            insert_rule,
            cloudflared_validator=SourceChangingValidator(),
        )

    assert candidates(tmp_path) == []
    assert source.read_text(encoding="utf-8").endswith("# concurrent edit\n")


def test_candidate_tampering_during_validation_is_rejected_and_cleaned(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path)
    original = source.read_bytes()

    class CandidateChangingValidator:
        def validate(self, candidate):
            contents = candidate.path.read_bytes()
            candidate.path.write_bytes(contents.replace(b"new.example", b"bad.example"))
            return CloudflaredValidationReport()

    with pytest.raises(CandidateFileError, match="candidate file contents changed"):
        prepare_validated_candidate(
            source,
            insert_rule,
            cloudflared_validator=CandidateChangingValidator(),
        )

    assert candidates(tmp_path) == []
    assert source.read_bytes() == original


def test_default_application_parser_accepts_valid_candidate(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    result = prepare_validated_candidate(
        source,
        insert_rule,
        cloudflared_validator=AcceptingValidator(),
    )
    try:
        assert result.changed is True
    finally:
        result.discard()
