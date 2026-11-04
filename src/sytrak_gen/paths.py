"""Filesystem locations that work both from source and from a PyInstaller build."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Root holding bundled read-only resources (the ``assets`` folder)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def app_dir() -> Path:
    """Folder containing the running exe (project root when run from source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return bundle_dir()


def asset_path(*parts: str) -> Path:
    return bundle_dir().joinpath("assets", *parts)


def user_data_dir() -> Path:
    """Writable per-user folder for settings and logs."""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
