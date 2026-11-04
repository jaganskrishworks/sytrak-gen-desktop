"""Top bar: single J-Link selection, connection status, MAC reading."""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import ttkbootstrap as ttk

from ..core.jlink import (
    JLinkCommander,
    JLinkProbe,
    MacReadError,
    TargetConfig,
    detect_probes,
    find_jlink_exe,
    list_usb_probes,
    read_mac,
)
from .theme import LABEL_BOLD, PAD

if TYPE_CHECKING:
    from .main_window import MainWindow

log = logging.getLogger(__name__)

MacCallback = Callable[[str | None, str], None]  # (mac or None, error)


class ProbeBar(ttk.Frame):
    def __init__(self, master, app: "MainWindow"):
        super().__init__(master, padding=(PAD + 6, 10))
        self.app = app
        self.cfg = app.settings.jlink
        self.exe: Path | None = None
        self.commander: JLinkCommander | None = None
        self._probes: dict[str, JLinkProbe] = {}
        self._labels: dict[str, str] = {}  # combobox label -> probe key
        self._names: dict[str, str] = {}  # serial -> product name from Commander
        self._selected_key: str | None = None
        self._scan_busy = False
        self._mac_busy = False
        self.probe_var = tk.StringVar()
        self._build()
        self.refresh_exe()
        self.after(200, lambda: self.scan(deep=True))
        self.after(self.cfg.auto_scan_interval_ms, self._tick)

    def _build(self) -> None:
        ttk.Label(self, text="J-Link", font=LABEL_BOLD).pack(side="left")
        self.combo = ttk.Combobox(self, textvariable=self.probe_var, state="readonly", width=42)
        self.combo.pack(side="left", padx=(10, 6))
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self._on_select())
        ttk.Button(self, text="⟳", width=3, bootstyle="primary-outline", command=lambda: self.scan(deep=True)).pack(side="left")
        self.status = ttk.Label(self, text="", font=LABEL_BOLD)
        self.status.pack(side="left", padx=(16, 0))
        self.target_lbl = ttk.Label(self, text="", style="Hint.TLabel")
        self.target_lbl.pack(side="right")

    # ---------------------------------------------------------------- exe ---

    def refresh_exe(self) -> None:
        self.exe = find_jlink_exe(self.cfg.exe_path or None)
        self.commander = JLinkCommander(self.exe) if self.exe else None
        exe_text = f"{self.exe.parent.name}" if self.exe else "JLink.exe NOT FOUND"
        self.target_lbl.configure(text=f"Target: {self.cfg.device or '-'}   ·   {exe_text}")
        self._update_status()

    # --------------------------------------------------------------- scan ---

    def _tick(self) -> None:
        self.scan(deep=False)
        self.after(max(500, self.cfg.auto_scan_interval_ms), self._tick)

    def scan(self, deep: bool) -> None:
        if self._scan_busy:
            return
        busy = self._mac_busy or (self.commander is not None and self.commander.running)
        exe = self.exe if deep and not busy else None
        self._scan_busy = True

        def work():
            probes = detect_probes(exe) if exe else list_usb_probes()
            self.app.dispatcher.post(self._apply, probes, exe is not None)

        threading.Thread(target=work, daemon=True, name="jlink-scan").start()

    def _apply(self, probes: list[JLinkProbe], deep: bool) -> None:
        self._scan_busy = False
        if deep:
            self._names.update({p.serial: p.product for p in probes if p.serial})
        for p in probes:
            if p.serial in self._names:
                p.product = self._names[p.serial]
        new = {p.key: p for p in probes}
        added = [p for k, p in new.items() if k not in self._probes]
        if new == self._probes and not deep:
            return
        self._probes = new

        self._labels = {self._label(p): k for k, p in new.items()}
        self.combo.configure(values=list(self._labels))
        if self._selected_key not in new:
            self._selected_key = None
            self.probe_var.set("")
            self._auto_select()
        else:
            self.probe_var.set(self._label(new[self._selected_key]))
        self._update_status()

        # Learn the product name of newly plugged probes.
        if not deep and self.exe and any(p.serial and p.serial not in self._names for p in added):
            self.after(300, lambda: self.scan(deep=True))

    @staticmethod
    def _label(p: JLinkProbe) -> str:
        name = p.product.split(", Nickname")[0]
        return f"{name}  —  S/N {p.serial or '?'}"

    def _auto_select(self) -> None:
        last = next((k for k, p in self._probes.items() if p.serial and p.serial == self.cfg.last_serial), None)
        if last is None and len(self._probes) == 1:
            last = next(iter(self._probes))
        if last:
            self._select(last)

    def _on_select(self) -> None:
        key = self._labels.get(self.probe_var.get())
        if key:
            self._select(key)

    def _select(self, key: str) -> None:
        self._selected_key = key
        probe = self._probes[key]
        self.probe_var.set(self._label(probe))
        if probe.serial and probe.serial != self.cfg.last_serial:
            self.cfg.last_serial = probe.serial
            self.app.save_settings()
        self._update_status()

    def _update_status(self) -> None:
        probe = self.selected_probe()
        if probe:
            self.status.configure(text=f"●  Connected   S/N {probe.serial}", bootstyle="success")
        elif self._probes:
            self.status.configure(text="●  Select a J-Link", bootstyle="warning")
        elif self.cfg.last_serial:
            self.status.configure(text="●  Disconnected", bootstyle="danger")
        else:
            self.status.configure(text="●  No J-Link detected", bootstyle="danger")

    def selected_probe(self) -> JLinkProbe | None:
        return self._probes.get(self._selected_key) if self._selected_key else None

    # ---------------------------------------------------------------- MAC ---

    def read_mac(self, on_done: MacCallback) -> None:
        probe = self.selected_probe()
        if self.commander is None:
            on_done(None, "J-Link Commander (JLink.exe) not found - set it in Settings")
            return
        if probe is None:
            on_done(None, "No J-Link selected")
            return
        if self._mac_busy or self.commander.running:
            on_done(None, "J-Link is busy, try again")
            return
        cfg = self.cfg
        if not cfg.device:
            on_done(None, "Set the target device in Settings")
            return
        target = TargetConfig(device=cfg.device, interface=cfg.interface, speed=cfg.speed)
        commander = self.commander
        self._mac_busy = True

        def work():
            try:
                mac, err = read_mac(commander, probe, target, cfg.mac_source, cfg.mac_address,
                                    cfg.mac_reverse, cfg.command_timeout_s), ""
            except MacReadError as exc:
                mac, err = None, str(exc)
            except Exception as exc:
                log.exception("MAC read failed")
                mac, err = None, str(exc)
            self.app.dispatcher.post(self._mac_done, on_done, mac, err)

        threading.Thread(target=work, daemon=True, name="jlink-mac").start()

    def _mac_done(self, on_done: MacCallback, mac: str | None, err: str) -> None:
        self._mac_busy = False
        if mac:
            log.info("Read MAC %s", mac)
        else:
            log.warning("MAC read failed: %s", err)
        on_done(mac, err)

    def shutdown(self) -> None:
        if self.commander and self.commander.running:
            self.commander.stop()
