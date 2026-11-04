"""Application entry point."""
from __future__ import annotations

import logging
import sys

from . import APP_TITLE, __version__
from .config import Settings
from .logger import setup_logging


def _enable_high_dpi() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _set_taskbar_identity() -> None:
    """Give the process its own taskbar identity.

    Without this, a run from source is grouped under python.exe and the taskbar
    shows Python's icon instead of ours.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Krishworks.SyTrakGen")
    except Exception:
        pass


def main() -> int:
    log_file = setup_logging()
    log = logging.getLogger("sytrak_gen")
    log.info("Starting %s v%s (log: %s)", APP_TITLE, __version__, log_file)
    _enable_high_dpi()
    _set_taskbar_identity()

    from .ui.main_window import MainWindow

    window = MainWindow(Settings.load())
    window.mainloop()
    log.info("Exited")
    return 0
