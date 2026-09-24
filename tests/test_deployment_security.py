import re
import subprocess
from pathlib import Path

import pytest

from cloudflared_manager.deployment.errors import HostOperationError
from cloudflared_manager.deployment import release as release_module
from cloudflared_manager.deployment.release import ReleaseFilesystem
from cloudflared_manager.deployment.service import MANAGER_UNIT, SystemdManager
from tests.deployment_support import FakePreparationRunner, make_paths, make_source

ROOT = Path(__file__).parents[1]
SHELL_SCRIPTS = (ROOT / "install.sh", ROOT / "deploy" / "update.sh", ROOT / "deploy" / "config.sh")


def test_root_shell_surface_has_no_eval_env_source_or_constructed_shell() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in SHELL_SCRIPTS)

    assert "eval " not in combined
    assert "bash -c" not in combined
    assert "source /etc/cloudflared-manager" not in combined
    assert ". /etc/cloudflared-manager" not in combined
    assert "CFM_CLOUDFLARED_CONFIG_PATH" not in combined
    assert "TUNNEL_TOKEN" not in combined
    assert "0.0.0.0" not in combined
    assert "read " not in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_deployment_source_has_no_cloudflared_config_or_service_mutation() -> None:
    deployment_sources = [
        *(ROOT / "src" / "cloudflared_manager" / "deployment").glob("*.py")
    ]
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [*SHELL_SCRIPTS, *deployment_sources]
    )
    service_control = (
        ROOT / "src" / "cloudflared_manager" / "deployment" / "service.py"
    ).read_text(encoding="utf-8")

    assert re.search(r"/etc/cloudflared(?:/|$)", production, re.MULTILINE) is None
    assert "cloudflared.service" not in service_control
    assert "Cloudflare API token" not in production
    assert "shell=True" not in production
    assert "os.system" not in production
    assert "eval(" not in production


def test_candidate_foundation_has_no_activation_or_privileged_control_surface() -> None:
    editing_sources = [
        *(ROOT / "src" / "cloudflared_manager" / "cloudflared" / "editing").glob("*.py")
    ]
    production = "\n".join(
        path.read_text(encoding="utf-8") for path in editing_sources
    )

    assert "os.replace" not in production
    assert "os.rename" not in production
    assert "chmod(" not in production
    assert "chown(" not in production
    assert "systemctl" not in production
    assert "cloudflared.service" not in production
    assert "shell=True" not in production
    assert "os.system" not in production
    assert "Cloudflare API" not in production
    assert "sudoers" not in production


def test_systemd_mutation_surface_is_fixed_to_manager_unit(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        if "--property=ActiveState,MainPID,NeedDaemonReload" in arguments:
            return subprocess.CompletedProcess(
                arguments, 0,
                "ActiveState=active\nMainPID=1234\nNeedDaemonReload=no\n", ""
            )
        return subprocess.CompletedProcess(arguments, 0, "LoadState=loaded\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = SystemdManager("/usr/bin/systemctl")

    manager.daemon_reload()
    manager.enable()
    manager.disable()
    manager.start()
    manager.stop()
    manager.restart()
    manager.is_active()
    manager.is_enabled()
    manager.runtime_state()
    manager.sanitized_status()

    assert calls == [
        ["/usr/bin/systemctl", "daemon-reload"],
        ["/usr/bin/systemctl", "enable", MANAGER_UNIT],
        ["/usr/bin/systemctl", "disable", MANAGER_UNIT],
        ["/usr/bin/systemctl", "start", MANAGER_UNIT],
        ["/usr/bin/systemctl", "stop", MANAGER_UNIT],
        ["/usr/bin/systemctl", "restart", MANAGER_UNIT],
        ["/usr/bin/systemctl", "is-active", "--quiet", MANAGER_UNIT],
        ["/usr/bin/systemctl", "is-enabled", "--quiet", MANAGER_UNIT],
        [
            "/usr/bin/systemctl",
            "show",
            MANAGER_UNIT,
            "--no-pager",
            "--property=ActiveState,MainPID,NeedDaemonReload",
        ],
        [
            "/usr/bin/systemctl",
            "show",
            MANAGER_UNIT,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState",
        ],
    ]
    assert all("cloudflared.service" not in call for call in calls)


def test_root_python_and_pip_execution_use_isolated_mode() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    update_wrapper = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    config_wrapper = (ROOT / "deploy" / "config.sh").read_text(encoding="utf-8")

    assert "unset PYTHONHOME PYTHONPATH" in installer
    assert '"${python_path}" -I' in installer
    assert '"${manager_python}" -I -m' in update_wrapper
    assert '"${manager_python}" -I -m' in config_wrapper


def test_installer_exercises_real_pip_enabled_venv_before_download() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    trap = "trap cleanup EXIT"
    probe_path = 'venv_probe="${temporary_dir}/venv-probe"'
    create = '"${python_path}" -I -m venv "${venv_probe}"'
    verify_pip = '"${venv_probe}/bin/python" -I -m pip --version'
    first_download = '"${curl_path}" --disable'

    assert "venv --help" not in installer
    assert "--without-pip" not in installer
    assert installer.index(trap) < installer.index(probe_path)
    assert installer.index(probe_path) < installer.index(create)
    assert installer.index(create) < installer.index(verify_pip)
    assert installer.index(verify_pip) < installer.index(first_download)
    assert '"${rm_path}" -rf -- "${venv_probe}"' in installer
    assert (
        "the selected Python cannot create the required pip-enabled virtual environment; "
        "install the matching distribution venv package"
    ) in installer


def test_installer_curl_disables_ambient_config_before_other_options() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    invocations = [
        line.strip()
        for line in installer.splitlines()
        if line.lstrip().startswith('"${curl_path}"')
    ]

    assert len(invocations) == 2
    assert all(line.startswith('"${curl_path}" --disable ') for line in invocations)


def test_candidate_process_drops_ambient_python_and_pip_environment(monkeypatch) -> None:
    captured_environment: dict[str, str] = {}

    def fake_run(arguments, **kwargs):
        captured_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted-python")
    monkeypatch.setenv("PYTHONHOME", "/tmp/untrusted-home")
    monkeypatch.setenv("PIP_INDEX_URL", "https://invalid.example")
    monkeypatch.setattr(release_module.subprocess, "run", fake_run)

    assert release_module._run_process(["/venv/bin/python", "-I", "-c", "pass"], 1) == 0
    assert "PYTHONPATH" not in captured_environment
    assert "PYTHONHOME" not in captured_environment
    assert "PIP_INDEX_URL" not in captured_environment


def test_candidate_preflight_failure_never_creates_current_link(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_source(tmp_path)

    class FailedPreflight(FakePreparationRunner):
        def __call__(self, arguments: list[str], timeout: float) -> int:
            super().__call__(arguments, timeout)
            return 1 if arguments[-1] == "cloudflared_manager.deployment.preflight" else 0

    filesystem = ReleaseFilesystem(paths, owner=None, process_runner=FailedPreflight())
    filesystem.ensure_layout()

    with pytest.raises(HostOperationError, match="preparation failed"):
        filesystem.prepare_release(source, "5" * 40, Path("/usr/bin/python3"))

    assert not paths.current.exists()


def test_service_unit_runs_unprivileged_with_manager_owned_paths() -> None:
    unit = (ROOT / "deploy" / "cloudflared-manager.service").read_text(encoding="utf-8")

    assert "User=cloudflared-manager" in unit
    assert "Group=cloudflared-manager" in unit
    assert "EnvironmentFile=/etc/cloudflared-manager/cloudflared-manager.env" in unit
    assert "ExecStart=/opt/cloudflared-manager/current/.venv/bin/cloudflared-manager" in unit
    assert "NoNewPrivileges=false" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/etc/cloudflared-manager /run/cloudflared-manager" in unit
    assert not any("/etc/cloudflared " in line or line.endswith("/etc/cloudflared")
                   for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    # PR14's /proc/<MainPID>/environ and exe checks must work when cloudflared
    # runs under another UID; CAP_DAC_OVERRIDE alone cannot satisfy ptrace access.
    caps = next(line.removeprefix("CapabilityBoundingSet=").split()
                for line in unit.splitlines() if line.startswith("CapabilityBoundingSet="))
    assert set(caps) == {
        "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_SETGID",
        "CAP_SETUID", "CAP_SYS_PTRACE",
    }
    assert "AmbientCapabilities=\n" in unit
    assert "cloudflared.service" not in unit
