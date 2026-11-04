"""Run J-Link Commander scripts and stream their output."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .detector import JLinkProbe, parse_emu_list

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_EXIT_COMMANDS = {"exit", "q", "qc"}


@dataclass
class TargetConfig:
    device: str
    interface: str = "SWD"
    speed: str = "4000"


@dataclass
class RunResult:
    returncode: int
    output: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


class JLinkCommander:
    def __init__(self, exe: Path):
        self.exe = Path(exe)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._timed_out = False
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self._proc is not None

    def build_args(
        self, script: Path, *, serial: str = "", ip: str = "", target: TargetConfig | None = None
    ) -> list[str]:
        args = [str(self.exe), "-NoGui", "1", "-ExitOnError", "1"]
        if ip:
            args += ["-IP", ip]
        elif serial:
            args += ["-USB", serial]
        if target and target.device:
            args += [
                "-Device", target.device,
                "-If", target.interface,
                "-Speed", target.speed,
                "-AutoConnect", "1",
            ]
        args += ["-CommandFile", str(script)]
        return args

    def run(
        self,
        commands: Iterable[str],
        *,
        probe: JLinkProbe | None = None,
        target: TargetConfig | None = None,
        on_output: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        script = [c.strip() for c in commands if c.strip()]
        if not script or script[-1].lower() not in _EXIT_COMMANDS:
            script.append("exit")

        fd, script_path = tempfile.mkstemp(prefix="sytrak_jlink_", suffix=".jlink")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(script) + "\n")

        serial = probe.serial if probe and probe.is_usb else ""
        ip = probe.address if probe and not probe.is_usb else ""
        args = self.build_args(Path(script_path), serial=serial, ip=ip, target=target)
        log.info("Running: %s  (script: %s)", subprocess.list2cmdline(args), "; ".join(script))

        result = RunResult(returncode=-1)
        timer = None
        try:
            with self._lock:
                if self._proc is not None:
                    raise RuntimeError("J-Link Commander is already running")
                self._timed_out = self._cancelled = False
                self._proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(self.exe.parent),
                    creationflags=_NO_WINDOW,
                )
                proc = self._proc
            if timeout:
                timer = threading.Timer(timeout, self._on_timeout)
                timer.daemon = True
                timer.start()
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                result.output.append(line)
                if on_output:
                    on_output(line)
            result.returncode = proc.wait()
        finally:
            if timer:
                timer.cancel()
            with self._lock:
                self._proc = None
            result.timed_out, result.cancelled = self._timed_out, self._cancelled
            try:
                os.unlink(script_path)
            except OSError:
                pass
        return result

    def stop(self) -> None:
        self._cancelled = True
        self._kill()

    def _on_timeout(self) -> None:
        self._timed_out = True
        self._kill()

    def _kill(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def list_emulators(self, timeout: float = 15.0) -> list[JLinkProbe]:
        result = self.run(["ShowEmuList"], timeout=timeout)
        return parse_emu_list("\n".join(result.output))
