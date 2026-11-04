"""Main application window."""
from __future__ import annotations

import logging
import sys
import threading

import ttkbootstrap as ttk
from ttkbootstrap.dialogs import Messagebox

from .. import APP_TITLE, __version__
from ..config import Settings
from ..core.network import local_ipv4_addresses, primary_ipv4
from ..core.sheets import SheetError, SheetsClient, find_key_file
from ..core.station import BROKER_USERNAME, StationService
from ..paths import asset_path
from .dispatcher import UiDispatcher
from .probe_bar import ProbeBar
from .theme import PAD, SUBTITLE_FONT, THEME, TITLE_FONT, configure_styles
from .views.generate_view import GenerateView
from .views.settings_view import SettingsView
from .views.verify_view import VerifyView
from .widgets import StatusPill

log = logging.getLogger(__name__)


class MainWindow(ttk.Window):
    def __init__(self, settings: Settings):
        super().__init__(title=f"{APP_TITLE}  v{__version__}", themename=THEME, size=(1320, 900), minsize=(1150, 760))
        self.settings = settings
        configure_styles(self.style)
        self._set_icon()
        self.dispatcher = UiDispatcher(self)
        self._sheet: SheetsClient | None = None
        self._sheet_key: tuple[str, str, str] | None = None
        self.station: StationService | None = None
        self.station_running = False

        self._build_header()
        self.probe_bar = ProbeBar(self, self)
        self.probe_bar.pack(fill="x")
        ttk.Separator(self).pack(fill="x")
        self._build_statusbar()

        self.notebook = ttk.Notebook(self, bootstyle="primary")
        self.notebook.pack(fill="both", expand=True, padx=PAD, pady=(PAD, 0))
        self.generate_view = GenerateView(self.notebook, self)
        self.verify_view = VerifyView(self.notebook, self)
        self.settings_view = SettingsView(self.notebook, self)
        self.notebook.add(self.generate_view, text="   Generate QR   ")
        self.notebook.add(self.verify_view, text="   Verify QR   ")
        self.notebook.add(self.settings_view, text="   Settings   ")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.place_window_center()
        self.after(100, self.start_station)

    # ------------------------------------------------------------- layout ---

    def _set_icon(self) -> None:
        try:
            ico, png = asset_path("icons", "app.ico"), asset_path("icons", "app.png")
            if sys.platform == "win32" and ico.exists():
                self.iconbitmap(default=str(ico))
            elif png.exists():
                self.iconphoto(True, ttk.PhotoImage(file=str(png)))
        except Exception:
            log.warning("Could not set window icon", exc_info=True)

    def _build_header(self) -> None:
        header = ttk.Frame(self, bootstyle="primary", padding=(20, 12))
        header.pack(fill="x")
        titles = ttk.Frame(header, bootstyle="primary")
        titles.pack(side="left")
        ttk.Label(titles, text=APP_TITLE, font=TITLE_FONT, bootstyle="inverse-primary").pack(anchor="w")
        ttk.Label(titles, text="Device MAC → QR → Verify → NFC", font=SUBTITLE_FONT, bootstyle="inverse-primary").pack(anchor="w")
        pills = ttk.Frame(header, bootstyle="primary")
        pills.pack(side="right")
        self.mobile_pill = StatusPill(pills, "Mobile  not connected", "light")
        self.mobile_pill.pack(side="right", padx=(8, 0))
        self.station_pill = StatusPill(pills, "Station  starting…", "light")
        self.station_pill.pack(side="right")
        self.tab_pill = StatusPill(pills, "Sheet tab  …", "light")
        self.tab_pill.pack(side="right", padx=(0, 8))

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(PAD + 4, 6))
        bar.pack(side="bottom", fill="x")
        self.status_lbl = ttk.Label(bar, text="Ready", style="Hint.TLabel")
        self.status_lbl.pack(side="left")
        ttk.Label(bar, text=f"v{__version__}", style="Hint.TLabel").pack(side="right")

    # ------------------------------------------------------------ station ---

    def start_station(self) -> None:
        s = self.settings
        self.station = StationService(
            station_id=s.station_id, name=s.station_name, port=s.station.broker_port,
            discovery_port=s.station.discovery_port, password=s.station.password,
            on_event=lambda kind, data: self.dispatcher.post(self.verify_view.handle_event, kind, data),
        )
        try:
            self.station.start()
        except OSError as exc:
            log.error("Broker failed to start: %s", exc)
            self.station = None
            self.station_running = False
            self.station_pill.set(f"Station  port {s.station.broker_port} in use", "danger")
            self.verify_view.set_station_info(
                f"✖  Could not start the mobile server on port {s.station.broker_port} ({exc.strerror or exc}). "
                "Change the port in Settings or close the other program using it.")
            self.verify_view.console.write(f"Mobile server failed: {exc}", "error")
            self.verify_view._update_buttons()
            return
        self.station_running = True
        ip = primary_ipv4() or "?"
        self.station_pill.set(f"Station  {s.station_name}  ·  {ip}:{s.station.broker_port}", "success")
        others = [a for a in local_ipv4_addresses() if a != ip]
        auth = f"   ·   login {BROKER_USERNAME} / ••••" if s.station.password else ""
        self.verify_view.set_station_info(
            f"Station  {s.station_name}  ({s.station_id})   ·   mobile server  {ip}:{s.station.broker_port}{auth}"
            "   ·   on the phone (same Wi-Fi) pick this station, then select it above and Connect")
        extra = f"  (also {', '.join(others)})" if others else ""
        self.verify_view.console.write(f"Mobile server listening on {ip}:{s.station.broker_port}{extra}", "muted")
        self.verify_view._update_buttons()

    def restart_station(self) -> None:
        if self.station:
            self.station.stop()
        self.verify_view.paired = None
        self.verify_view.mobiles.clear()
        self.verify_view._refresh_mobiles()
        self.start_station()

    # -------------------------------------------------------- google sheet ---

    def sheet_client(self) -> SheetsClient:
        """Client for the configured sheet (rebuilt when Settings change)."""
        key_file = find_key_file(self.settings.sheet.key_file)
        if key_file is None:
            raise SheetError("Service account key not found - set it in Settings")
        if not self.settings.sheet.url:
            raise SheetError("Google Sheet URL not set - see Settings")
        ident = (str(key_file), self.settings.sheet.url, self.settings.sheet.tab)
        if self._sheet is None or self._sheet_key != ident:
            self._sheet = SheetsClient(key_file, self.settings.sheet.url, self.settings.sheet.tab)
            self._sheet_key = ident
        return self._sheet

    def run_sheet(self, work, on_done) -> None:
        """Run ``work(client)`` on a worker thread; ``on_done(result, error)`` on the UI thread."""
        try:
            client = self.sheet_client()
        except SheetError as exc:
            on_done(None, str(exc))
            return

        def run():
            try:
                result, err = work(client), None
            except SheetError as exc:
                result, err = None, str(exc)
            except Exception as exc:
                log.exception("Google Sheet call failed")
                result, err = None, f"{exc.__class__.__name__}: {exc}"
            self.dispatcher.post(on_done, result, err)

        threading.Thread(target=run, daemon=True, name="sheet").start()

    # ------------------------------------------------------------ helpers ---

    def set_status(self, text: str) -> None:
        self.status_lbl.configure(text=text)

    def set_tab_status(self, tab: str | None) -> None:
        if tab:
            self.tab_pill.set(f"Sheet tab  {tab}", "success")
        else:
            self.tab_pill.set("Sheet tab  not selected", "danger")

    def set_mobile_status(self, name: str | None, online: bool) -> None:
        if name is None:
            self.mobile_pill.set("Mobile  not connected", "light")
        else:
            self.mobile_pill.set(f"Mobile  {name}", "success" if online else "warning")

    def save_settings(self) -> None:
        self.settings.save()

    # ---------------------------------------------------------- lifecycle ---

    def report_callback_exception(self, exc, val, tb):  # Tk hook
        log.error("Unhandled UI error", exc_info=(exc, val, tb))
        Messagebox.show_error(f"{exc.__name__}: {val}\n\nDetails were written to the log file.", "Unexpected error", parent=self)

    def on_close(self) -> None:
        try:
            self.probe_bar.shutdown()
            if self.station:
                self.station.stop()
            self.save_settings()
        finally:
            self.destroy()
