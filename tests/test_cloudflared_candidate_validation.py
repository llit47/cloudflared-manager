import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from cloudflared_manager.cloudflared.editing import (
    CandidateFileStager,
    CloudflaredCandidateValidator,
    CloudflaredValidationExecutionError,
    CloudflaredValidationRejectedError,
    CloudflaredValidationTimeoutError,
    CloudflaredValidatorUnavailableError,
    SubprocessValidationCommandRunner,
    ValidationCommandResult,
    read_config_source_snapshot,
)

SOURCE = b"ingress:\n  - service: http_status:404\n"
CANDIDATE = (
    b"ingress:\n"
    b"  - hostname: app.example.com\n"
    b"    service: http://localhost:8000\n"
    b"  - service: http_status:404\n"
)
SECRET = "TOKEN_MUST_NOT_LEAK"


class RecordingRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> ValidationCommandResult:
        self.calls.append((tuple(argv), timeout_seconds))
        return ValidationCommandResult(
            self.returncode,
            f"stdout {SECRET}".encode(),
            f"stderr {SECRET}".encode(),
        )


@pytest.fixture
def staged_candidate(tmp_path: Path):
    source = tmp_path / "config.yml"
    source.write_bytes(SOURCE)
    candidate = CandidateFileStager().stage(
        read_config_source_snapshot(source), CANDIDATE
    )
    try:
        yield candidate
    finally:
        candidate.discard()


def test_validator_uses_exact_argv_and_bounded_timeout(staged_candidate) -> None:
    runner = RecordingRunner()
    validator = CloudflaredCandidateValidator(
        runner=runner,
        executable_finder=lambda _: "/usr/local/bin/cloudflared",
        timeout_seconds=7.5,
    )

    report = validator.validate(staged_candidate)

    assert report.accepted is True
    assert runner.calls == [
        (
            (
                "/usr/local/bin/cloudflared",
                "tunnel",
                "--config",
                str(staged_candidate.path),
                "ingress",
                "validate",
            ),
            7.5,
        )
    ]


def test_nonzero_result_is_safe_hard_failure(staged_candidate) -> None:
    validator = CloudflaredCandidateValidator(
        runner=RecordingRunner(returncode=1),
        executable_finder=lambda _: "/usr/bin/cloudflared",
    )

    with pytest.raises(CloudflaredValidationRejectedError) as captured:
        validator.validate(staged_candidate)

    assert SECRET not in str(captured.value)
    assert SECRET not in repr(captured.value)


@pytest.mark.parametrize("found", [None, "cloudflared", "/usr/bin/not-cloudflared"])
def test_unavailable_or_unsafe_executable_is_rejected(
    staged_candidate,
    found: str | None,
) -> None:
    validator = CloudflaredCandidateValidator(executable_finder=lambda _: found)

    with pytest.raises(CloudflaredValidatorUnavailableError):
        validator.validate(staged_candidate)


def test_subprocess_runner_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessValidationCommandRunner().run(
        ["/usr/bin/cloudflared", "--version"], timeout_seconds=3.0
    )

    assert result.returncode == 0
    assert captured["argv"] == ["/usr/bin/cloudflared", "--version"]
    assert captured["shell"] is False
    assert captured["timeout"] == 3.0
    assert captured["check"] is False
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE


def test_subprocess_timeout_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr=SECRET)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(CloudflaredValidationTimeoutError) as captured:
        SubprocessValidationCommandRunner().run(
            ["/usr/bin/cloudflared"], timeout_seconds=2.0
        )

    assert SECRET not in str(captured.value)


def test_subprocess_execution_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(subprocess, "run", unavailable)

    with pytest.raises(CloudflaredValidationExecutionError) as captured:
        SubprocessValidationCommandRunner().run(
            ["/usr/bin/cloudflared"], timeout_seconds=2.0
        )

    assert SECRET not in str(captured.value)


@pytest.mark.parametrize("timeout", [0, -1, 30.1])
def test_validator_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="safe bounds"):
        CloudflaredCandidateValidator(timeout_seconds=timeout)
