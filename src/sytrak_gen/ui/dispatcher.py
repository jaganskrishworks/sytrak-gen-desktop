"""Marshal calls from worker threads onto the Tk main thread."""
from __future__ import annotations

import logging
import queue
import tkinter as tk
from typing import Any, Callable

log = logging.getLogger(__name__)


class UiDispatcher:
    def __init__(self, root: tk.Misc, interval_ms: int = 40):
        self._root = root
        self._queue: queue.Queue[tuple[Callable, tuple, dict]] = queue.Queue()
        self._interval = interval_ms
        self._root.after(self._interval, self._drain)

    def post(self, fn: Callable[..., Any], *args, **kwargs) -> None:
        """Thread-safe: schedule ``fn(*args, **kwargs)`` on the UI thread."""
        self._queue.put((fn, args, kwargs))

    def _drain(self) -> None:
        try:
            for _ in range(500):  # keep the UI responsive under bursts of output
                fn, args, kwargs = self._queue.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        try:
            self._root.after(self._interval, self._drain)
        except tk.TclError:  # window destroyed
            pass
