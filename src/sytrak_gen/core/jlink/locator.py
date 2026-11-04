"""Locate the J-Link Commander executable.

Search order:
  1. Path configured in Settings
  2. Bundled copy:  assets/bin/jlink/  (shipped inside the exe)
  3. Side-by-side:  <exe folder>/jlink/
  4. SEGGER install folders (newest version first)
  5. System PATH
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from ...paths import app_dir, asset_path

EXE_NAMES = ["JLink.exe"] if sys.platform == "win32" else ["JLinkExe", "JLink"]

_VERSION_RE = re.compile(r"V(\d+)([a-z]?)", re.I)


def _version_key(folder: Path) -> tuple[int, str]:
    m = _VERSION_RE.search(folder.name)
    return (int(m.group(1)), m.group(2).lower()) if m else (-1, "")


def _install_roots() -> list[Path]:
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        return [Path(r) / "SEGGER" for r in roots if r]
    if sys.platform == "darwin":
        return [Path("/Applications/SEGGER")]
    return [Path("/opt/SEGGER")]


def _candidates(custom: str | None):
    if custom:
        p = Path(custom)
        yield p
        for name in EXE_NAMES:
            yield p / name
    for folder in (asset_path("bin", "jlink"), app_dir() / "jlink"):
        for name in EXE_NAMES:
            yield folder / name
    for root in _install_roots():
        if not root.is_dir():
            continue
        folders = sorted((d for d in root.glob("JLink*") if d.is_dir()), key=_version_key, reverse=True)
        for folder in folders:
            for name in EXE_NAMES:
                yield folder / name
    for name in EXE_NAMES:
        found = shutil.which(name)
        if found:
            yield Path(found)


def find_jlink_exe(custom: str | None = None) -> Path | None:
    for candidate in _candidates(custom):
        if candidate.is_file():
            return candidate.resolve()
    return None


def describe_exe(exe: Path | None) -> str:
    """Short human label, e.g. 'JLink_V924a'."""
    if exe is None:
        return "not found"
    return exe.parent.name if _VERSION_RE.search(exe.parent.name) else exe.name
