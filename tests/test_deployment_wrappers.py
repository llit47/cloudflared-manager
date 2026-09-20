"""Rootless tests for the stable administration shell wrappers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SHA = "a" * 40


def _run_wrapper(
    tmp_path: Path,
    name: str,
    target: str,
    *,
    interpreter: str = "executable",
    arguments: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    install_root = tmp_path / "cloudflared-manager"
    install_root.mkdir()
    (install_root / "current").symlink_to(target)

    manager_python = install_root / "releases" / SHA / ".venv" / "bin" / "python"
    if interpreter != "missing":
        manager_python.parent.mkdir(parents=True)
        manager_python.write_text(
            "#!/bin/bash\nprintf '%s\\n' \"$0\" \"$@\"\n",
            encoding="utf-8",
        )
        manager_python.chmod(0o755 if interpreter == "executable" else 0o644)

    source = (ROOT / "deploy" / name).read_text(encoding="utf-8")
    source = source.replace("${EUID}", "0")
    source = source.replace(
        "readonly install_root='/opt/cloudflared-manager'",
        f"readonly install_root='{install_root}'",
    )
    wrapper = tmp_path / name
    wrapper.write_text(source, encoding="utf-8")
    wrapper.chmod(0o755)
    return subprocess.run(
        [str(wrapper), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )


@pytest.mark.parametrize(
    ("name", "arguments", "command"),
    [
        ("config.sh", ("set-port", "8000"), "config"),
        ("update.sh", (), "update"),
    ],
)
def test_wrapper_executes_physical_release_interpreter(
    tmp_path: Path,
    name: str,
    arguments: tuple[str, ...],
    command: str,
) -> None:
    result = _run_wrapper(
        tmp_path,
        name,
        f"releases/{SHA}",
        arguments=arguments,
    )

    manager_python = tmp_path / "cloudflared-manager" / "releases" / SHA / ".venv/bin/python"
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        str(manager_python),
        "-I",
        "-m",
        "cloudflared_manager.deployment.cli",
        command,
        *arguments,
    ]


@pytest.mark.parametrize(
    "target",
    [
        "current",
        f"/opt/cloudflared-manager/releases/{SHA}",
        f"releases/../releases/{SHA}",
        f"releases/{SHA}/extra",
        "releases/not-a-sha",
        f"releases/{'A' * 40}",
    ],
)
def test_wrapper_rejects_noncanonical_current_target(tmp_path: Path, target: str) -> None:
    result = _run_wrapper(tmp_path, "config.sh", target)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "release is invalid" in result.stderr


@pytest.mark.parametrize("name", ["config.sh", "update.sh"])
@pytest.mark.parametrize("trailing_newlines", ["\n", "\n\n"])
def test_wrapper_rejects_current_target_with_trailing_newlines(
    tmp_path: Path, name: str, trailing_newlines: str
) -> None:
    result = _run_wrapper(
        tmp_path,
        name,
        f"releases/{SHA}{trailing_newlines}",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "release is invalid" in result.stderr


@pytest.mark.parametrize("interpreter", ["missing", "non-executable"])
def test_wrapper_rejects_unusable_physical_interpreter(
    tmp_path: Path, interpreter: str
) -> None:
    result = _run_wrapper(
        tmp_path,
        "config.sh",
        f"releases/{SHA}",
        interpreter=interpreter,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "runtime is unavailable" in result.stderr
