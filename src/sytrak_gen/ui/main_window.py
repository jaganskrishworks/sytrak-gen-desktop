"""Main application window."""
from __future__ import annotations

import logging
import sys
import threading
import time

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
from .views.mqtt_view import MqttView
from .views.settings_view import SettingsView
from .views.verify_view import VerifyView
from .widgets import StatusPill

log = logging.getLogger(__name__)


class MainWindow(ttk.Window):
    def __init__(self, settings: Settings):
        # iconphoto=None: stop ttkbootstrap from installing its own default icon, which
        # would override ours on Windows. Our icon is set in _set_icon().
        super().__init__(title=f"{APP_TITLE}  v{__version__}", themename=THEME, size=(1320, 900),
                         minsize=(980, 640), iconphoto=None)
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
        self.mqtt_view = MqttView(self.notebook, self)
        self.settings_view = SettingsView(self.notebook, self)
        self.notebook.add(self.generate_view, text="   Generate QR   ")
        self.notebook.add(self.verify_view, text="   Verify QR   ")
        self.notebook.add(self.mqtt_view, text="   MQTT   ")
        self.notebook.add(self.settings_view, text="   Settings   ")
        # Developer option: the MQTT tab stays hidden until the title is clicked 10 times
        # (it still records in the background, so it shows the history once enabled).
        self.notebook.hide(self.mqtt_view)
        self.dev_mode = False
        self._dev_taps: list[float] = []

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.place_window_center()
        self.after(100, self.start_station)

    # ------------------------------------------------------------- layout ---

    def _set_icon(self) -> None:
        try:
            ico, png = asset_path("icons", "app.ico"), asset_path("icons", "app.png")
            if sys.platform == "win32" and ico.exists():
                # .ico holds 16-256 px images: Windows picks the right size for the
                # title bar (small) and the taskbar / Alt-Tab (large).
                self.iconbitmap(default=str(ico))
            elif png.exists():
                self._icon_img = ttk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_img)
        except Exception:
            log.warning("Could not set window icon", exc_info=True)

    def _build_header(self) -> None:
        header = ttk.Frame(self, bootstyle="primary", padding=(20, 12))
        header.pack(fill="x")
        titles = ttk.Frame(header, bootstyle="primary")
        titles.pack(side="left")
        self.title_lbl = ttk.Label(titles, text=APP_TITLE, font=TITLE_FONT, bootstyle="inverse-primary")
        self.title_lbl.pack(anchor="w")
        self.title_lbl.bind("<Button-1>", lambda _e: self._dev_tap())
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

    # ---------------------------------------------------- developer options ---

    DEV_TAPS = 10
    DEV_TAP_GAP_S = 1.5     # a longer pause between clicks starts the count again

    def _dev_tap(self) -> None:
        """Click the title 10 times in a row to show the MQTT tab (developer option)."""
        if self.dev_mode:
            self.set_status("Developer options are already enabled")
            return
        now = time.monotonic()
        if self._dev_taps and now - self._dev_taps[-1] > self.DEV_TAP_GAP_S:
            self._dev_taps.clear()
            if "developer options" in self.status_lbl.cget("text"):
                self.set_status("Ready")      # the old countdown no longer applies
        self._dev_taps.append(now)
        left = self.DEV_TAPS - len(self._dev_taps)
        if left > 0:
            if len(self._dev_taps) >= 5:   # like Android: hint only near the end
                self.set_status(f"{left} more click{'s' if left != 1 else ''} to enable developer options")
            return
        self.dev_mode = True
        self._dev_taps.clear()
        self.notebook.add(self.mqtt_view)            # re-shows the hidden tab in its place
        self.notebook.select(self.mqtt_view)
        self.set_status("Developer options enabled: MQTT tab (until the app is closed)")
        log.info("Developer options enabled (MQTT tab)")

    # ------------------------------------------------------------ station ---

    def start_station(self) -> None:
        s = self.settings
        self.station = StationService(
            station_id=s.station_id, name=s.station_name, port=s.station.broker_port,
            discovery_port=s.station.discovery_port, password=s.station.password,
            on_event=lambda kind, data: self.dispatcher.post(self.verify_view.handle_event, kind, data),
        )
        # MQTT tab: every message / client event, marshalled onto the UI thread
        self.station.broker.on_traffic = lambda kind, info: self.dispatcher.post(self.mqtt_view.on_traffic, kind, info)
        try:
            self.station.start()
        except OSError as exc:
            log.error("Broker failed to start: %s", exc)
            self.station = None
            self.station_running = False
            self.mqtt_view.set_broker(f"port {s.station.broker_port}", f"not running: {exc.strerror or exc}", running=False)
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
        self.mqtt_view.set_broker(f"{ip}:{s.station.broker_port}", f"sytrak/{s.station_id}/#")
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
