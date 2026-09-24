import os
import subprocess
import sys
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
        self.calls: list[tuple[tuple[str, ...], float, tuple[int, ...]]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        pass_fds: tuple[int, ...],
    ) -> ValidationCommandResult:
        self.calls.append((tuple(argv), timeout_seconds, pass_fds))
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
    binding = staged_candidate.validation_binding()
    assert runner.calls == [
        (
            (
                "/usr/local/bin/cloudflared",
                "tunnel",
                "--config",
                str(binding.path),
                "ingress",
                "validate",
            ),
            7.5,
            binding.pass_fds,
        )
    ]
    assert str(binding.path).startswith("/proc/self/fd/")
    assert binding.path.read_bytes() == CANDIDATE
    assert len(binding.pass_fds) == 1
    assert os.get_inheritable(binding.pass_fds[0]) is False


def test_service_bound_validator_uses_pinned_executable_not_path_lookup(
    staged_candidate, tmp_path, monkeypatch,
) -> None:
    observed = {}
    executable_file = tmp_path / "cloudflared"
    executable_file.write_bytes(b"binary placeholder")
    executable_fd = os.open(executable_file, os.O_RDONLY | os.O_CLOEXEC)
    service_path = Path("/opt/cloudflare/bin/cloudflared")
    def forbidden_lookup(_name):
        pytest.fail("service-bound validation must not search PATH")
    def fake_run(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        validator = CloudflaredCandidateValidator(
            executable=service_path, executable_fd=executable_fd,
            executable_finder=forbidden_lookup,
        )
        assert validator.validate(staged_candidate).accepted
        assert observed["argv"][0] == str(service_path)
        assert observed["executable"] == f"/proc/self/fd/{executable_fd}"
        assert executable_fd in observed["pass_fds"]
        assert observed["shell"] is False
        assert observed["argv"][3] == str(staged_candidate.validation_binding().path)
    finally:
        os.close(executable_fd)


def test_runner_executes_pinned_binary_when_visible_path_is_different(tmp_path) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    executable_fd = os.open(sys.executable, os.O_RDONLY | os.O_CLOEXEC)
    try:
        result = SubprocessValidationCommandRunner().run(
            [str(tmp_path / "cloudflared"), "-c", "import sys; sys.exit(0)"],
            timeout_seconds=3,
            pass_fds=(directory_fd,),
            executable_fd=executable_fd,
        )
        assert result.returncode == 0
    finally:
        os.close(executable_fd)
        os.close(directory_fd)


def test_nonzero_result_is_safe_hard_failure(staged_candidate) -> None:
    validator = CloudflaredCandidateValidator(
        runner=RecordingRunner(returncode=1),
        executable_finder=lambda _: "/usr/bin/cloudflared",
    )

    with pytest.raises(CloudflaredValidationRejectedError) as captured:
        validator.validate(staged_candidate)

    assert SECRET not in str(captured.value)
    assert SECRET not in repr(captured.value)


def test_validator_path_stays_bound_when_ancestor_namespace_is_swapped(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "source"
    source_directory.mkdir(mode=0o700)
    source_directory.chmod(0o700)
    source = source_directory / "config.yml"
    source.write_bytes(SOURCE)
    relative_name = "relative-credentials.json"
    original_relative = b"original-relative-resource"
    replacement_relative = b"replacement-relative-resource"
    (source_directory / relative_name).write_bytes(original_relative)
    candidate_contents = (
        f'credentials-file: "{relative_name}"\n'.encode() + CANDIDATE
    )
    candidate = CandidateFileStager().stage(
        read_config_source_snapshot(source),
        candidate_contents,
    )
    ordinary_candidate_path = candidate.path
    candidate_name = ordinary_candidate_path.name

    pinned_directory = tmp_path / "pinned-source"
    source_directory.rename(pinned_directory)
    replacement_directory = tmp_path / "source"
    replacement_directory.mkdir(mode=0o700)
    replacement_directory.chmod(0o700)
    replacement_candidate = replacement_directory / candidate_name
    replacement_candidate.write_bytes(b"ingress: []\n")
    replacement_candidate.chmod(0o600)
    (replacement_directory / relative_name).write_bytes(replacement_relative)

    class InspectingRunner:
        observed_candidate: bytes | None = None
        observed_relative: bytes | None = None
        ordinary_path_bytes: bytes | None = None
        passed_fds: tuple[int, ...] = ()

        def run(
            self,
            argv: Sequence[str],
            *,
            timeout_seconds: float,
            pass_fds: tuple[int, ...],
        ) -> ValidationCommandResult:
            bound_path = Path(argv[3])
            self.observed_candidate = bound_path.read_bytes()
            self.observed_relative = (bound_path.parent / relative_name).read_bytes()
            self.ordinary_path_bytes = ordinary_candidate_path.read_bytes()
            self.passed_fds = pass_fds
            return ValidationCommandResult(0, b"OK", b"")

    runner = InspectingRunner()
    validator = CloudflaredCandidateValidator(
        runner=runner,
        executable_finder=lambda _: "/usr/bin/cloudflared",
    )
    try:
        report = validator.validate(candidate)

        assert report.accepted is True
        assert runner.observed_candidate == candidate_contents
        assert runner.observed_relative == original_relative
        assert runner.ordinary_path_bytes == b"ingress: []\n"
        assert len(runner.passed_fds) == 1
        assert os.fstat(runner.passed_fds[0]).st_ino == pinned_directory.stat().st_ino
    finally:
        candidate.discard()

    assert not (pinned_directory / candidate_name).exists()
    assert replacement_candidate.read_bytes() == b"ingress: []\n"


@pytest.mark.parametrize("found", [None, "cloudflared", "/usr/bin/not-cloudflared"])
def test_unavailable_or_unsafe_executable_is_rejected(
    staged_candidate,
    found: str | None,
) -> None:
    validator = CloudflaredCandidateValidator(executable_finder=lambda _: found)

    with pytest.raises(CloudflaredValidatorUnavailableError):
        validator.validate(staged_candidate)


def test_subprocess_runner_never_uses_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    directory_fd = os.open(
        tmp_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    unrelated_fd = os.open(tmp_path / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert os.get_inheritable(directory_fd) is False
        assert os.get_inheritable(unrelated_fd) is False
        monkeypatch.setattr(subprocess, "run", fake_run)
        result = SubprocessValidationCommandRunner().run(
            ["/usr/bin/cloudflared", "--version"],
            timeout_seconds=3.0,
            pass_fds=(directory_fd,),
        )
    finally:
        os.close(unrelated_fd)
        os.close(directory_fd)

    assert result.returncode == 0
    assert captured["argv"] == ["/usr/bin/cloudflared", "--version"]
    assert captured["shell"] is False
    assert captured["timeout"] == 3.0
    assert captured["check"] is False
    assert captured["close_fds"] is True
    assert captured["pass_fds"] == (directory_fd,)
    assert "cwd" not in captured
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE


def test_subprocess_runner_rejects_uncontrolled_descriptors(tmp_path: Path) -> None:
    file_fd = os.open(tmp_path / "not-a-directory", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(CloudflaredValidationExecutionError, match="descriptor"):
            SubprocessValidationCommandRunner().run(
                ["/usr/bin/cloudflared"],
                timeout_seconds=2.0,
                pass_fds=(),
            )
        with pytest.raises(CloudflaredValidationExecutionError, match="unsafe"):
            SubprocessValidationCommandRunner().run(
                ["/usr/bin/cloudflared"],
                timeout_seconds=2.0,
                pass_fds=(file_fd,),
            )
    finally:
        os.close(file_fd)


def test_subprocess_runner_inherits_only_bound_directory_fd(
    staged_candidate,
) -> None:
    binding = staged_candidate.validation_binding()
    script = (
        "from pathlib import Path; import sys; "
        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())"
    )

    result = SubprocessValidationCommandRunner().run(
        [sys.executable, "-c", script, str(binding.path)],
        timeout_seconds=3.0,
        pass_fds=binding.pass_fds,
    )

    assert result.returncode == 0
    assert result.stdout == CANDIDATE
    assert result.stderr == b""
    assert os.get_inheritable(binding.pass_fds[0]) is False


def test_subprocess_timeout_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr=SECRET)

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        monkeypatch.setattr(subprocess, "run", timeout)

        with pytest.raises(CloudflaredValidationTimeoutError) as captured:
            SubprocessValidationCommandRunner().run(
                ["/usr/bin/cloudflared"],
                timeout_seconds=2.0,
                pass_fds=(directory_fd,),
            )
    finally:
        os.close(directory_fd)

    assert SECRET not in str(captured.value)


def test_subprocess_execution_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args, **kwargs):
        raise OSError(SECRET)

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        monkeypatch.setattr(subprocess, "run", unavailable)

        with pytest.raises(CloudflaredValidationExecutionError) as captured:
            SubprocessValidationCommandRunner().run(
                ["/usr/bin/cloudflared"],
                timeout_seconds=2.0,
                pass_fds=(directory_fd,),
            )
    finally:
        os.close(directory_fd)

    assert SECRET not in str(captured.value)


@pytest.mark.parametrize("timeout", [0, -1, 30.1])
def test_validator_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="safe bounds"):
        CloudflaredCandidateValidator(timeout_seconds=timeout)
