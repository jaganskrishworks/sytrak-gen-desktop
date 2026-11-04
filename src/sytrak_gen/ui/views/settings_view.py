"""Settings tab: J-Link / MAC source, station (broker), Google Sheet."""
from __future__ import annotations

import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from ttkbootstrap.dialogs import Messagebox

try:
    from ttkbootstrap.widgets import ScrolledFrame
except ImportError:  # ttkbootstrap < 1.13
    from ttkbootstrap.scrolled import ScrolledFrame

from ... import APP_TITLE, __version__
from ...core.jlink import MAC_SOURCES, find_jlink_exe
from ...core.sheets import KEY_FILE_NAME, SheetError, SheetsClient, find_key_file, parse_sheet_url
from ...paths import logs_dir, user_data_dir
from ..theme import LABEL_BOLD, PAD
from ..widgets import form_row
from .generate_view import open_path

if TYPE_CHECKING:
    from ..main_window import MainWindow

INTERFACES = ("SWD", "JTAG")
SPEEDS = ("auto", "1000", "4000", "8000", "12000")


class SettingsView(ttk.Frame):
    def __init__(self, master, app: "MainWindow"):
        super().__init__(master, padding=PAD)
        self.app = app
        s = app.settings
        self.exe_var = tk.StringVar(value=s.jlink.exe_path)
        self.device_var = tk.StringVar(value=s.jlink.device)
        self.interface_var = tk.StringVar(value=s.jlink.interface)
        self.speed_var = tk.StringVar(value=s.jlink.speed)
        self.mac_source_var = tk.StringVar(value=MAC_SOURCES.get(s.jlink.mac_source, MAC_SOURCES["nrf_ficr"]))
        self.mac_addr_var = tk.StringVar(value=s.jlink.mac_address)
        self.mac_rev_var = tk.BooleanVar(value=s.jlink.mac_reverse)
        self.station_name_var = tk.StringVar(value=s.station.name)
        self.station_id_var = tk.StringVar(value=s.station.station_id)
        self.port_var = tk.StringVar(value=str(s.station.broker_port))
        self.password_var = tk.StringVar(value=s.station.password)
        self.sheet_url_var = tk.StringVar(value=s.sheet.url)
        self.key_var = tk.StringVar(value=s.sheet.key_file)
        self.seq_start_var = tk.StringVar(value=str(s.qr.seq_start))
        self.types_var = tk.StringVar(value=s.qr.device_types)
        self._build()
        self._update_detected()
        self._update_key_label()
        self._on_mac_source()

    def _build(self) -> None:
        # Footer first, packed to the bottom, so Save stays visible at any window size.
        ttk.Separator(self).pack(side="bottom", fill="x")
        bottom = ttk.Frame(self, padding=(0, PAD, 0, 0))
        bottom.pack(side="bottom", fill="x")
        bottom.lift()
        ttk.Label(bottom, text=f"{APP_TITLE} v{__version__}", font=LABEL_BOLD).pack(side="left")
        ttk.Button(bottom, text="Data folder", bootstyle="primary-link", command=lambda: open_path(user_data_dir())).pack(side="left", padx=(12, 0))
        ttk.Button(bottom, text="Logs", bootstyle="primary-link", command=lambda: open_path(logs_dir())).pack(side="left")
        ttk.Button(bottom, text="💾  Save settings", bootstyle="success", command=self.save).pack(side="right", ipadx=14, ipady=3)
        self.saved_lbl = ttk.Label(bottom, text="", font=LABEL_BOLD)
        self.saved_lbl.pack(side="right", padx=(0, 14))

        body = ScrolledFrame(self, autohide=True)
        body.pack(side="top", fill="both", expand=True)
        body.columnconfigure((0, 1), weight=1, uniform="s")

        jl = ttk.Labelframe(body, text=" J-Link & MAC ", padding=PAD)
        jl.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, PAD))
        jl.columnconfigure(1, weight=1)
        path_row = ttk.Frame(jl)
        path_row.columnconfigure(0, weight=1)
        ttk.Entry(path_row, textvariable=self.exe_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(path_row, text="Browse…", bootstyle="primary-outline", command=self._browse_exe).grid(row=0, column=1)
        r = form_row(jl, 0, "JLink.exe", path_row, hint="Empty = auto-detect (bundled → next to exe → SEGGER install)")
        self.detected_lbl = ttk.Label(jl, text="", style="Hint.TLabel")
        self.detected_lbl.grid(row=r, column=1, sticky="w", pady=(0, 6))
        r = form_row(jl, r + 1, "Target device", ttk.Entry(jl, textvariable=self.device_var), hint="J-Link device name, e.g. nRF52833_xxAA")
        r = form_row(jl, r, "Interface", ttk.Combobox(jl, textvariable=self.interface_var, values=INTERFACES, state="readonly"))
        r = form_row(jl, r, "Speed (kHz)", ttk.Combobox(jl, textvariable=self.speed_var, values=SPEEDS))
        src = ttk.Combobox(jl, textvariable=self.mac_source_var, values=list(MAC_SOURCES.values()), state="readonly")
        src.bind("<<ComboboxSelected>>", lambda _e: self._on_mac_source())
        r = form_row(jl, r, "MAC source", src)
        self.addr_entry = ttk.Entry(jl, textvariable=self.mac_addr_var)
        r = form_row(jl, r, "MAC address @", self.addr_entry, hint="Memory address of the 6 MAC bytes (custom source only)")
        self.rev_chk = ttk.Checkbutton(jl, text="Bytes stored LSB first (reverse)", variable=self.mac_rev_var, bootstyle="round-toggle")
        self.rev_chk.grid(row=r, column=1, sticky="w", pady=4)

        st = ttk.Labelframe(body, text=" Station (mobile connection) ", padding=PAD)
        st.grid(row=0, column=1, sticky="nsew")
        st.columnconfigure(1, weight=1)
        r = form_row(st, 0, "Station name", ttk.Entry(st, textvariable=self.station_name_var), hint=f"Shown on the mobile. Empty = PC name")
        r = form_row(st, r, "Station ID", ttk.Entry(st, textvariable=self.station_id_var), hint=f"Unique per PC. Empty = {self.app.settings.station_id}")
        r = form_row(st, r, "Broker port", ttk.Entry(st, textvariable=self.port_var))
        r = form_row(st, r, "Password", ttk.Entry(st, textvariable=self.password_var, show="•"),
                     hint="Optional. Mobile logs in as user 'sytrak' with this password")

        gs = ttk.Labelframe(body, text=" Google Sheet ", padding=PAD)
        gs.grid(row=1, column=1, sticky="nsew", pady=(PAD, 0))
        gs.columnconfigure(1, weight=1)
        r = form_row(gs, 0, "Sheet URL", ttk.Entry(gs, textvariable=self.sheet_url_var))
        key_row = ttk.Frame(gs)
        key_row.columnconfigure(0, weight=1)
        ttk.Entry(key_row, textvariable=self.key_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(key_row, text="Browse…", bootstyle="primary-outline", command=self._browse_key).grid(row=0, column=1)
        r = form_row(gs, r, "Key file", key_row, hint="Service account JSON. Empty = secrets\\*.json next to the app")
        self.key_lbl = ttk.Label(gs, text="", style="Hint.TLabel", wraplength=480)
        self.key_lbl.grid(row=r, column=1, sticky="w")
        r = form_row(gs, r + 1, "Device types", ttk.Entry(gs, textvariable=self.types_var),
                     hint="Choices for DEVICE TYPE, comma separated, e.g.  UWB-K4W, UWB-K5")
        r = form_row(gs, r, "First sequence", ttk.Entry(gs, textvariable=self.seq_start_var, width=8),
                     hint="Used only while the sheet tab is empty (1-9999)")
        test_row = ttk.Frame(gs)
        test_row.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.test_btn = ttk.Button(test_row, text="Test connection", bootstyle="info-outline", command=self._test_sheet)
        self.test_btn.pack(side="left")
        self.test_lbl = ttk.Label(test_row, text="", style="Hint.TLabel", wraplength=380)
        self.test_lbl.pack(side="left", padx=(10, 0))

    def _mac_source_key(self) -> str:
        return next((k for k, v in MAC_SOURCES.items() if v == self.mac_source_var.get()), "nrf_ficr")

    def _on_mac_source(self) -> None:
        state = "normal" if self._mac_source_key() == "memory" else "disabled"
        self.addr_entry.configure(state=state)
        self.rev_chk.configure(state=state)

    def _browse_exe(self) -> None:
        types = [("JLink.exe", "JLink.exe"), ("All files", "*.*")] if sys.platform == "win32" else [("All files", "*")]
        path = filedialog.askopenfilename(parent=self, title="Locate J-Link Commander", filetypes=types)
        if path:
            self.exe_var.set(path)
            self._update_detected()

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Service account key (JSON)",
                                          filetypes=[("JSON key", "*.json"), ("All files", "*.*")])
        if not path:
            return
        # Keep a private copy so the original download can be deleted.
        target = user_data_dir() / KEY_FILE_NAME
        try:
            shutil.copyfile(path, target)
        except OSError as exc:
            Messagebox.show_error(f"Could not copy the key file:\n{exc}", "Google Sheet", parent=self)
            return
        self.key_var.set(str(target))
        self._update_key_label()

    def _update_key_label(self) -> None:
        key = find_key_file(self.key_var.get().strip())
        if key:
            try:
                email = SheetsClient(key, self.sheet_url_var.get() or "x" * 20).service_email
            except SheetError:
                email = ""
            self.key_lbl.configure(text=f"✔  {key}" + (f"\nShare the sheet with: {email}" if email else ""), bootstyle="success")
        else:
            self.key_lbl.configure(text="✖  No service account key found", bootstyle="danger")

    def _test_sheet(self) -> None:
        url, key = self.sheet_url_var.get().strip(), find_key_file(self.key_var.get().strip())
        try:
            parse_sheet_url(url)
            if key is None:
                raise SheetError("No service account key found")
        except SheetError as exc:
            self.test_lbl.configure(text=f"✖  {exc}", bootstyle="danger")
            return
        self.test_btn.configure(state="disabled")
        self.test_lbl.configure(text="Connecting…", style="Hint.TLabel")
        client = SheetsClient(key, url)

        def run():
            try:
                result, err = client.test(), None
            except SheetError as exc:
                result, err = None, str(exc)
            except Exception as exc:
                result, err = None, f"{exc.__class__.__name__}: {exc}"
            self.app.dispatcher.post(done, result, err)

        def done(result, err):
            self.test_btn.configure(state="normal")
            if err:
                self.test_lbl.configure(text=f"✖  {err}", bootstyle="danger")
            else:
                self.test_lbl.configure(text=f"✔  Connected: {result}", bootstyle="success")

        threading.Thread(target=run, daemon=True, name="sheet-test").start()

    def _update_detected(self) -> None:
        exe = find_jlink_exe(self.exe_var.get().strip() or None)
        if exe:
            self.detected_lbl.configure(text=f"✔  Using {exe}", bootstyle="success")
        else:
            self.detected_lbl.configure(text="✖  J-Link Commander not found", bootstyle="danger")

    def save(self) -> None:
        s = self.app.settings
        try:
            port = int(self.port_var.get())
            seq_start = int(self.seq_start_var.get())
            if not (1 <= port <= 65535 and 1 <= seq_start <= 9999):
                raise ValueError
            if self._mac_source_key() == "memory":
                int(self.mac_addr_var.get(), 0)
        except ValueError:
            Messagebox.show_error("Check the port (1-65535), first sequence (1-9999) and MAC address.", "Settings", parent=self)
            return
        try:
            parse_sheet_url(self.sheet_url_var.get())
        except SheetError:
            Messagebox.show_error("The Google Sheet URL is not valid.", "Settings", parent=self)
            return
        types = [t.strip() for t in self.types_var.get().split(",") if t.strip()]
        if not types:
            Messagebox.show_error("Enter at least one device type (e.g. UWB-K4W).", "Settings", parent=self)
            return

        station_changed = (
            port != s.station.broker_port
            or self.password_var.get() != s.station.password
            or self.station_id_var.get().strip() != s.station.station_id
            or self.station_name_var.get().strip() != s.station.name
        )
        s.jlink.exe_path = self.exe_var.get().strip()
        s.jlink.device = self.device_var.get().strip()
        s.jlink.interface = self.interface_var.get()
        s.jlink.speed = self.speed_var.get().strip() or "4000"
        s.jlink.mac_source = self._mac_source_key()
        s.jlink.mac_address = self.mac_addr_var.get().strip()
        s.jlink.mac_reverse = self.mac_rev_var.get()
        s.station.name = self.station_name_var.get().strip()
        s.station.station_id = self.station_id_var.get().strip()
        s.station.broker_port = port
        s.station.password = self.password_var.get()
        sheet_changed = (self.sheet_url_var.get().strip() != s.sheet.url or self.key_var.get().strip() != s.sheet.key_file)
        s.sheet.url = self.sheet_url_var.get().strip()
        s.sheet.key_file = self.key_var.get().strip()
        s.qr.seq_start = seq_start
        s.qr.device_types = ", ".join(types)
        self.app.save_settings()
        self.app.generate_view.set_device_types(types)

        self.app.probe_bar.refresh_exe()
        if sheet_changed:
            self.app.generate_view.refresh()
        else:
            self.app.generate_view._update_next_seq()
        self._update_detected()
        self._update_key_label()
        if station_changed:
            self.app.restart_station()
        self.app.set_status("Settings saved")
        self.saved_lbl.configure(text="✔  Saved", bootstyle="success")
        self.after(4000, lambda: self.saved_lbl.configure(text=""))
