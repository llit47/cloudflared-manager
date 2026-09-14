from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cloudflared_manager.cloudflared import (
    CommandExecutionError,
    CommandTimedOutError,
    CommandUnavailableError,
    ManagementMode,
)
from cloudflared_manager.cloudflared.discovery import discover_cloudflared
from cloudflared_manager.cloudflared.runtime import (
    CommandResult,
    DiscoveryCommand,
    SubprocessCommandRunner,
)

FAKE_SECRET = "TEST_SECRET_MUST_NOT_LEAK"
FAKE_TOKEN_FILE_PATH = "/nonexistent/test-token-file"
BIN_PATH = Path("/opt/example/bin/cloudflared")


@dataclass
class FakeCommandRunner:
    responses: dict[DiscoveryCommand, CommandResult | Exception]
    calls: list[tuple[DiscoveryCommand, Path | None]] = field(default_factory=list)

    def run(
        self,
        command: DiscoveryCommand,
        *,
        cloudflared_executable: Path | None = None,
    ) -> CommandResult:
        self.calls.append((command, cloudflared_executable))
        response = self.responses[command]
        if isinstance(response, Exception):
            raise response
        return response


def command_result(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def systemd_output(
    *,
    load: str = "loaded",
    active: str = "inactive",
    sub: str = "dead",
    pid: str = "0",
    exec_start: str = "",
) -> str:
    return (
        f"LoadState={load}\n"
        f"ActiveState={active}\n"
        f"SubState={sub}\n"
        f"MainPID={pid}\n"
        f"ExecStart={exec_start}\n"
    )


def exec_start(arguments: str) -> str:
    return (
        "{ path=/opt/example/bin/cloudflared ; "
        f"argv[]=/opt/example/bin/cloudflared {arguments} ; "
        "ignore_errors=no ; }"
    )


def no_binary(_: str) -> None:
    return None


def binary(_: str) -> str:
    return str(BIN_PATH)


def test_discovery_disabled_performs_no_lookup_or_command() -> None:
    runner = FakeCommandRunner({})

    def unexpected_lookup(name: str) -> str:
        raise AssertionError(f"unexpected executable lookup: {name}")

    result = discover_cloudflared(
        False,
        runner,
        executable_finder=unexpected_lookup,
    )

    assert result is None
    assert runner.calls == []


@pytest.mark.parametrize(
    "show_failure",
    [
        CommandUnavailableError("systemd unavailable"),
        CommandTimedOutError("inspection timed out"),
    ],
)
def test_missing_binary_and_systemd_are_expected(
    show_failure: Exception,
) -> None:
    runner = FakeCommandRunner(
        {DiscoveryCommand.SYSTEMD_SHOW: show_failure}
    )

    result = discover_cloudflared(True, runner, executable_finder=no_binary)

    assert result is not None
    assert result.executable_exists is False
    assert result.version is None
    assert result.systemd_available is False
    assert result.service_exists is None
    assert runner.calls == [(DiscoveryCommand.SYSTEMD_SHOW, None)]


def test_binary_and_version_are_detected_without_loaded_unit() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(load="not-found")
            ),
            DiscoveryCommand.CLOUDFLARED_VERSION: command_result(
                "cloudflared version 2026.9.1 (built 2026-09-01)\n"
            ),
        }
    )

    result = discover_cloudflared(True, runner, executable_finder=binary)

    assert result is not None
    assert result.executable_path == BIN_PATH
    assert result.version == "2026.9.1"
    assert result.systemd_available is True
    assert result.service_exists is False


@pytest.mark.parametrize(
    "version_response",
    [
        command_result("unexpected output"),
        command_result("cloudflared version 2026.9.1", returncode=1),
        CommandTimedOutError("safe timeout"),
    ],
)
def test_version_failure_does_not_fail_discovery(
    version_response: CommandResult | Exception,
) -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(load="not-found")
            ),
            DiscoveryCommand.CLOUDFLARED_VERSION: version_response,
        }
    )

    result = discover_cloudflared(True, runner, executable_finder=binary)

    assert result is not None
    assert result.executable_exists is True
    assert result.version is None


def test_loaded_inactive_service_and_nonzero_disabled_state_are_preserved() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(systemd_output()),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result(
                "disabled\n",
                returncode=1,
            ),
        }
    )

    result = discover_cloudflared(True, runner, executable_finder=no_binary)

    assert result is not None
    assert result.service_exists is True
    assert result.load_state == "loaded"
    assert result.active_state == "inactive"
    assert result.sub_state == "dead"
    assert result.enabled_state == "disabled"
    assert result.main_pid is None


def test_active_running_service_and_main_pid_are_parsed() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(active="active", sub="running", pid="4242")
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("enabled\n"),
        }
    )

    result = discover_cloudflared(True, runner, executable_finder=no_binary)

    assert result is not None
    assert result.active_state == "active"
    assert result.sub_state == "running"
    assert result.main_pid == 4242
    assert result.enabled_state == "enabled"


@pytest.mark.parametrize(
    "arguments",
    [
        "--no-autoupdate tunnel --config /srv/example/config.yml run",
        "--no-autoupdate tunnel --config=/srv/example/config.yml run",
    ],
)
def test_explicit_config_forms_are_detected(arguments: str) -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(exec_start=exec_start(arguments))
            ),
            DiscoveryCommand.CLOUDFLARED_VERSION: command_result(
                "cloudflared version 2026.9.1"
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("enabled\n"),
        }
    )

    result = discover_cloudflared(
        True,
        runner,
        executable_finder=no_binary,
        executable_checker=lambda path: path == BIN_PATH,
    )

    assert result is not None
    assert result.executable_path == BIN_PATH
    assert result.explicit_config_path == Path("/srv/example/config.yml")
    assert result.management_mode is ManagementMode.LOCAL_CONFIG


def test_service_without_config_or_token_has_unknown_management_mode() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(exec_start=exec_start("tunnel run"))
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("static\n"),
        }
    )

    result = discover_cloudflared(
        True,
        runner,
        executable_finder=no_binary,
        executable_checker=lambda path: False,
    )

    assert result is not None
    assert result.explicit_config_path is None
    assert result.management_mode is ManagementMode.UNKNOWN


def test_malformed_exec_start_is_unknown_and_does_not_raise() -> None:
    malformed = (
        "{ path=/opt/example/bin/cloudflared ; "
        "argv[]=/opt/example/bin/cloudflared --config 'unterminated ; "
        "ignore_errors=no ; }"
    )
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(exec_start=malformed)
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("enabled\n"),
        }
    )

    result = discover_cloudflared(
        True,
        runner,
        executable_finder=no_binary,
        executable_checker=lambda path: False,
    )

    assert result is not None
    assert result.management_mode is ManagementMode.UNKNOWN
    assert result.explicit_config_path is None


def test_multiple_exec_start_entries_are_not_guessed() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(
                    exec_start=(
                        exec_start("tunnel --config /srv/example/first.yml run")
                        + " "
                        + exec_start("tunnel --config /srv/example/second.yml run")
                    )
                )
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("enabled\n"),
        }
    )

    result = discover_cloudflared(
        True,
        runner,
        executable_finder=no_binary,
        executable_checker=lambda path: False,
    )

    assert result is not None
    assert result.explicit_config_path is None
    assert result.management_mode is ManagementMode.UNKNOWN


@pytest.mark.parametrize(
    "token_arguments",
    [
        f"tunnel run --token {FAKE_SECRET}",
        f"tunnel run --token={FAKE_SECRET}",
    ],
)
def test_token_managed_service_discards_secret(token_arguments: str) -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(exec_start=exec_start(token_arguments))
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("enabled\n"),
        }
    )

    result = discover_cloudflared(
        True,
        runner,
        executable_finder=no_binary,
        executable_checker=lambda path: False,
    )

    assert result is not None
    assert result.management_mode is ManagementMode.REMOTE_TOKEN
    assert result.explicit_config_path is None
    assert FAKE_SECRET not in repr(result)


@pytest.mark.parametrize(
    "token_arguments",
    [
        f"tunnel run --token-file {FAKE_TOKEN_FILE_PATH}",
        f"tunnel run --token-file={FAKE_TOKEN_FILE_PATH}",
    ],
)
def test_token_file_managed_service_discards_path(token_arguments: str) -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(exec_start=exec_start(token_arguments))
            ),
            DiscoveryCommand.SYSTEMD_IS_ENABLED: command_result("enabled\n"),
        }
    )

    result = discover_cloudflared(
        True,
        runner,
        executable_finder=no_binary,
        executable_checker=lambda path: False,
    )

    assert result is not None
    assert result.management_mode is ManagementMode.REMOTE_TOKEN
    assert result.explicit_config_path is None
    assert FAKE_TOKEN_FILE_PATH not in repr(result)


def test_unexpected_systemd_output_yields_unknown_safe_facts() -> None:
    runner = FakeCommandRunner(
        {DiscoveryCommand.SYSTEMD_SHOW: command_result("not property output\n")}
    )

    result = discover_cloudflared(True, runner, executable_finder=no_binary)

    assert result is not None
    assert result.systemd_available is True
    assert result.service_exists is None
    assert result.management_mode is ManagementMode.UNKNOWN


def test_nonzero_systemd_inspection_without_properties_is_unavailable() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                "",
                returncode=1,
                stderr="Failed to connect to bus",
            )
        }
    )

    result = discover_cloudflared(True, runner, executable_finder=no_binary)

    assert result is not None
    assert result.systemd_available is False
    assert result.service_exists is None
    assert result.management_mode is ManagementMode.UNKNOWN


def test_unexpected_version_and_systemd_values_are_not_exposed() -> None:
    runner = FakeCommandRunner(
        {
            DiscoveryCommand.SYSTEMD_SHOW: command_result(
                systemd_output(
                    load=f"loaded {FAKE_SECRET}",
                    active=f"active {FAKE_SECRET}",
                    sub=f"running {FAKE_SECRET}",
                    pid=FAKE_SECRET,
                    exec_start=FAKE_SECRET,
                )
            ),
            DiscoveryCommand.CLOUDFLARED_VERSION: command_result(
                f"cloudflared version 1.2.{FAKE_SECRET}"
            ),
        }
    )

    result = discover_cloudflared(True, runner, executable_finder=binary)

    assert result is not None
    assert result.version is None
    assert result.service_exists is None
    assert result.management_mode is ManagementMode.UNKNOWN
    assert FAKE_SECRET not in repr(result)


@pytest.mark.parametrize(
    ("command", "executable", "expected"),
    [
        (
            DiscoveryCommand.CLOUDFLARED_VERSION,
            BIN_PATH,
            [str(BIN_PATH), "--version"],
        ),
        (
            DiscoveryCommand.SYSTEMD_SHOW,
            None,
            [
                "/usr/bin/systemctl",
                "show",
                "cloudflared.service",
                "--no-pager",
                "--property=LoadState,ActiveState,SubState,MainPID,ExecStart",
            ],
        ),
        (
            DiscoveryCommand.SYSTEMD_IS_ENABLED,
            None,
            [
                "/usr/bin/systemctl",
                "is-enabled",
                "cloudflared.service",
            ],
        ),
    ],
)
def test_subprocess_runner_uses_exact_arrays_without_shell(
    monkeypatch,
    command: DiscoveryCommand,
    executable: Path | None,
    expected: list[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 3, "disabled\n", "safe error")

    monkeypatch.setattr(
        "cloudflared_manager.cloudflared.runtime.shutil.which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(
        "cloudflared_manager.cloudflared.runtime.subprocess.run",
        fake_run,
    )

    result = SubprocessCommandRunner(timeout_seconds=1.5).run(
        command,
        cloudflared_executable=executable,
    )

    assert captured["argv"] == expected
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 1.5
    assert kwargs["capture_output"] is True
    assert result.returncode == 3


def test_subprocess_timeout_error_never_contains_captured_secret(monkeypatch) -> None:
    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout"],
            output=FAKE_SECRET,
            stderr=FAKE_SECRET,
        )

    monkeypatch.setattr(
        "cloudflared_manager.cloudflared.runtime.subprocess.run",
        fake_run,
    )

    with pytest.raises(CommandTimedOutError) as captured:
        SubprocessCommandRunner().run(
            DiscoveryCommand.CLOUDFLARED_VERSION,
            cloudflared_executable=BIN_PATH,
        )

    assert FAKE_SECRET not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_subprocess_execution_error_is_safe(monkeypatch) -> None:
    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        raise OSError(FAKE_SECRET)

    monkeypatch.setattr(
        "cloudflared_manager.cloudflared.runtime.subprocess.run",
        fake_run,
    )

    with pytest.raises(CommandExecutionError) as captured:
        SubprocessCommandRunner().run(
            DiscoveryCommand.CLOUDFLARED_VERSION,
            cloudflared_executable=BIN_PATH,
        )

    assert FAKE_SECRET not in str(captured.value)
    assert captured.value.__context__ is None
