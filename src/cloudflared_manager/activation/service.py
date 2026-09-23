"""Strict, internal cloudflared service observation; no discovery fallbacks."""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cloudflared_manager.activation.journal import BaselineFacts

UNIT = "cloudflared.service"
STABILITY_SECONDS = 1.0
MAX_OBSERVATION_BYTES = 65536
PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "Type", "MainPID",
              "NRestarts", "ExecStart", "Job", "NeedDaemonReload")


class ServiceRefused(Exception):
    def __init__(self) -> None:
        super().__init__("SERVICE_OBSERVATION_UNAVAILABLE")


class ServiceIO(Protocol):
    def show(self) -> bytes: ...
    def restart(self) -> bool: ...
    def process(self, pid: int, executable: Path) -> tuple[int, int, int]: ...
    def canonical(self, path: Path) -> Path: ...


@dataclass(frozen=True, repr=False)
class Shape:
    values: dict[str, str]

    def __repr__(self) -> str:
        return "Shape()"

    @property
    def settled(self) -> bool:
        return self.values["Job"] == "0" and (
            self.values["ActiveState"], self.values["SubState"]
        ) in {("active", "running"), ("inactive", "dead"), ("failed", "failed")}


def parse_show(raw: bytes) -> Shape:
    try:
        if len(raw) > MAX_OBSERVATION_BYTES:
            raise ValueError
        values: dict[str, str] = {}
        for line in raw.decode("utf-8", errors="strict").splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in PROPERTIES or key in values or "\x00" in value:
                raise ValueError
            values[key] = value
        if set(values) != set(PROPERTIES):
            raise ValueError
        if values["Id"] != UNIT or values["LoadState"] != "loaded":
            raise ValueError
        for key in ("MainPID", "NRestarts", "Job"):
            if re.fullmatch(r"0|[1-9][0-9]{0,18}", values[key]) is None:
                raise ValueError
        for key in ("ActiveState", "SubState", "Type"):
            if re.fullmatch(r"[a-z][a-z-]{0,31}", values[key]) is None:
                raise ValueError
        if values["NeedDaemonReload"] != "no":
            raise ValueError
        return Shape(values)
    except (ValueError, UnicodeError, TypeError):
        raise ServiceRefused() from None


_PATH = r"/[A-Za-z0-9._+/-]+"
_EXEC = re.compile(
    r"\{ path=(" + _PATH + r") ; argv\[\]=(.*?) ; ignore_errors=no ; "
    r"start_time=[^;{}\n]* ; stop_time=[^;{}\n]* ; pid=[0-9]+ ; "
    r"code=(?:\(null\)|[a-z]+|[0-9]+) ; status=[0-9]+(?:/[A-Z0-9]+)? \}"
)


def parse_exec(raw: str) -> tuple[Path, Path]:
    """Accept one unambiguous local-config tunnel run, rejecting unknown flags.

    Escaped/quoted systemd argv is deliberately unsupported: show's string
    rendering does not preserve enough argument-boundary evidence for guessing.
    """
    match = _EXEC.fullmatch(raw) if len(raw) <= MAX_OBSERVATION_BYTES else None
    if match is None:
        raise ServiceRefused()
    executable, argv = match.groups()
    args = argv.split(" ")
    if not args or args.pop(0) != executable or Path(executable).name != "cloudflared":
        raise ServiceRefused()
    config = None
    words = []
    while args:
        arg = args.pop(0)
        if arg == "--no-autoupdate":
            continue
        if arg == "--config" or arg.startswith("--config="):
            value = arg.partition("=")[2] if "=" in arg else (args.pop(0) if args else "")
            if config is not None or re.fullmatch(_PATH, value) is None:
                raise ServiceRefused()
            config = Path(value)
        else:
            words.append(arg)
    if words != ["tunnel", "run"] or config is None:
        raise ServiceRefused()
    for path in (Path(executable), config):
        if ".." in path.parts or str(path).startswith("//"):
            raise ServiceRefused()
    return Path(executable), config


def parse_start(raw: bytes, pid: int) -> int:
    try:
        if len(raw) > 4096 or not raw.startswith(str(pid).encode() + b" ("):
            raise ValueError
        end = raw.rindex(b") ")
        fields = raw[end + 2:].split()
        if len(fields) < 20 or fields[0] not in {b"R", b"S", b"D", b"I"}:
            raise ValueError
        ticks = fields[19]
        if re.fullmatch(rb"[1-9][0-9]{0,19}", ticks) is None:
            raise ValueError
        return int(ticks)
    except (ValueError, IndexError):
        raise ServiceRefused() from None


class StrictService:
    """Inject only IO/time boundaries; policy is fixed and has no UI inputs."""

    def __init__(self, authority, *, io: ServiceIO | None = None,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        if io is None:
            from cloudflared_manager.activation.service_io import LinuxServiceIO
            io = LinuxServiceIO()
        self._authority = authority
        self._io = io
        self._sleep = sleep
        self._clock = clock

    def observe(self, *, adopted_fingerprint: str, source_digest: str) -> BaselineFacts:
        try:
            start = self._clock()
            first, shape = self._once(adopted_fingerprint, source_digest)
            self._sleep(STABILITY_SECONDS)
            second, repeated = self._once(adopted_fingerprint, source_digest)
            elapsed = self._clock() - start
            if first != second or shape != repeated or not STABILITY_SECONDS <= elapsed <= 15:
                raise ServiceRefused()
            return first
        except Exception:
            raise ServiceRefused() from None

    def _once(self, fingerprint: str, digest: str) -> tuple[BaselineFacts, Shape]:
        _, adopted = self._authority.current()
        if hashlib.sha256(os.fsencode(adopted)).hexdigest() != fingerprint:
            raise ServiceRefused()
        shape = parse_show(self._io.show())
        values = shape.values
        if (not shape.settled or values["ActiveState"] != "active" or values["Type"] != "notify"
            or int(values["MainPID"]) <= 0):
            raise ServiceRefused()
        executable, config = parse_exec(values["ExecStart"])
        if self._io.canonical(config) != adopted:
            raise ServiceRefused()
        pid = int(values["MainPID"])
        ticks, device, inode = self._io.process(pid, executable)
        if parse_show(self._io.show()) != shape:
            raise ServiceRefused()
        if self._io.process(pid, executable) != (ticks, device, inode):
            raise ServiceRefused()
        result = BaselineFacts(UNIT, "loaded", "active", "running", pid, ticks, device, inode,
                               fingerprint, digest, int(STABILITY_SECONDS * 1000), int(values["NRestarts"]))
        return BaselineFacts.parse(result.record()), shape

    def settled(self) -> bool:
        try:
            first = parse_show(self._io.show())
            if not first.settled:
                self._sleep(STABILITY_SECONDS)
                first = parse_show(self._io.show())
            return first.settled
        except Exception:
            return False

    def restart(self) -> bool:
        try:
            return self._io.restart() is True
        except Exception:
            return False

    def verify(self, baseline: BaselineFacts, *, activation: bool) -> None:
        observed = self.observe(adopted_fingerprint=baseline.adopted_fingerprint,
                                source_digest=baseline.source_digest)
        if ((observed.executable_device, observed.executable_inode)
            != (baseline.executable_device, baseline.executable_inode)):
            raise ServiceRefused()
        if activation and (observed.main_pid == baseline.main_pid
                           or observed.process_start_ticks == baseline.process_start_ticks):
            raise ServiceRefused()
