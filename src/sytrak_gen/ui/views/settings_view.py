"""Settings tab: J-Link / MAC source, station (broker), Google Sheet, NFC signing secrets."""
from __future__ import annotations

import logging
import sys
import threading
import tkinter as tk
from tkinter import filedialog
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from ttkbootstrap.dialogs import Messagebox, Querybox

try:
    from ttkbootstrap.widgets.scrolled import ScrolledFrame
except ImportError:  # older ttkbootstrap
    from ttkbootstrap.scrolled import ScrolledFrame

from ... import APP_TITLE, __version__
from ...core import secrets
from ...core.jlink import MAC_SOURCES, find_jlink_exe
from ...core.sheets import SheetError, SheetsClient, find_key_file, parse_sheet_url
from ...paths import logs_dir, user_data_dir
from ..theme import LABEL_BOLD, PAD
from ..widgets import Responsive, SecretField, form_row
from .generate_view import open_path

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)

INTERFACES = ("SWD", "JTAG")
SPEEDS = ("auto", "1000", "4000", "8000", "12000")


class SecretPicker(ttk.Frame):
    """[path] [Browse…] [From URL…] + a live check of the file that will be used.

    Browse / From URL validate the file and keep a private copy in <data folder>/secrets/.
    An empty path means: look in secrets/ next to the app, then the private copy.
    """

    def __init__(self, master, view: "SettingsView", kind: secrets.SecretKind,
                 path_var: tk.StringVar, url_var: tk.StringVar | None = None, describe=None):
        super().__init__(master)
        self.view, self.kind, self.path_var, self.url_var = view, kind, path_var, url_var
        self.describe = describe or (lambda info: info)
        self.columnconfigure(0, weight=1)
        entry = ttk.Entry(self, textvariable=path_var)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        entry.bind("<FocusOut>", lambda _e: self.refresh())
        self.browse_btn = ttk.Button(self, text="Browse…", bootstyle="primary-outline", command=self.browse)
        self.browse_btn.grid(row=0, column=1)
        self.url_btn = ttk.Button(self, text="From URL…", bootstyle="primary-outline", command=self.from_url)
        self.url_btn.grid(row=0, column=2, padx=(6, 0))
        self.status = ttk.Label(self, text="", style="Hint.TLabel", wraplength=520, justify="left")
        self.status.grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))
        self.bind("<Configure>", lambda e: self.status.configure(wraplength=max(260, e.width - 10)))
        self.refresh()

    def browse(self) -> None:
        path = filedialog.askopenfilename(parent=self, title=self.kind.title, filetypes=list(self.kind.filetypes))
        if not path:
            return
        try:
            copy, _info = secrets.import_file(self.kind, path)
        except secrets.SecretError as exc:
            Messagebox.show_error(f"{self.kind.title}:\n{exc}", "Settings", parent=self)
            return
        self.path_var.set(str(copy))
        if self.url_var is not None:
            self.url_var.set("")
        self.refresh()
        self.view.mark_dirty(f"{self.kind.title} imported - click Save settings")

    def from_url(self) -> None:
        current = self.url_var.get() if self.url_var is not None else ""
        url = Querybox.get_string(f"Direct download URL of the {self.kind.title}\n(the file is checked before it is used)",
                                  f"{self.kind.title} from URL", initialvalue=current, parent=self)
        if not url or not url.strip():
            return
        self.browse_btn.configure(state="disabled")
        self.url_btn.configure(state="disabled")
        self.status.configure(text="Downloading…", style="Hint.TLabel")

        def work():
            try:
                result, err = secrets.download(self.kind, url), None
            except secrets.SecretError as exc:
                result, err = None, str(exc)
            except Exception as exc:  # never let a download crash the UI thread
                log.exception("Secret download failed")
                result, err = None, f"{exc.__class__.__name__}: {exc}"
            self.view.app.dispatcher.post(done, result, err)

        def done(result, err):
            self.browse_btn.configure(state="normal")
            self.url_btn.configure(state="normal")
            if err:
                self.status.configure(text=f"✖  {err}", bootstyle="danger")
                return
            self.path_var.set(str(result[0]))
            if self.url_var is not None:
                self.url_var.set(url.strip())
            self.refresh()
            self.view.mark_dirty(f"{self.kind.title} downloaded - click Save settings")

        threading.Thread(target=work, daemon=True, name="secret-download").start()

    def refresh(self) -> bool:
        """Show which file will be used and whether it is valid. Returns True if valid."""
        path = secrets.find(self.kind, self.path_var.get().strip())
        if path is None:
            self.status.configure(text=f"✖  No {self.kind.title} found - Browse… or From URL…", bootstyle="danger")
            return False
        try:
            info = self.kind.validate(path.read_bytes())
        except (OSError, secrets.SecretError) as exc:
            self.status.configure(text=f"✖  {path}: {exc}", bootstyle="danger")
            return False
        source = f"\nFrom {self.url_var.get()}" if self.url_var is not None and self.url_var.get() else ""
        self.status.configure(text=f"✔  {path}\n{self.describe(info)}{source}",
                              bootstyle="warning" if "DEV key" in info else "success")
        return True


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
        self.nfc_key_var = tk.StringVar(value=s.nfc.key_file)
        self.nfc_key_url_var = tk.StringVar(value=s.nfc.key_url)
        self.nfc_key_id_var = tk.StringVar(value=str(s.nfc.key_id))
        self.nfc_secret_var = tk.StringVar(value=s.nfc.secret_file)
        self.nfc_secret_url_var = tk.StringVar(value=s.nfc.secret_url)
        self.nfc_lock_var = tk.BooleanVar(value=s.nfc.lock)
        self._build()
        self._update_detected()
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

        jl = ttk.Labelframe(body, text=" J-Link & MAC ", padding=PAD)
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
        st.columnconfigure(1, weight=1)
        r = form_row(st, 0, "Station name", ttk.Entry(st, textvariable=self.station_name_var), hint=f"Shown on the mobile. Empty = PC name")
        r = form_row(st, r, "Station ID", ttk.Entry(st, textvariable=self.station_id_var), hint=f"Unique per PC. Empty = {self.app.settings.station_id}")
        r = form_row(st, r, "Broker port", ttk.Entry(st, textvariable=self.port_var))
        r = form_row(st, r, "Password", ttk.Entry(st, textvariable=self.password_var, show="•"),
                     hint="Optional. Mobile logs in as user 'sytrak' with this password")

        gs = ttk.Labelframe(body, text=" Google Sheet ", padding=PAD)
        gs.columnconfigure(1, weight=1)
        r = form_row(gs, 0, "Sheet URL", ttk.Entry(gs, textvariable=self.sheet_url_var))
        self.google_picker = SecretPicker(gs, self, secrets.GOOGLE_KEY, self.key_var,
                                          describe=lambda email: f"Share the sheet with: {email}  (Editor)")
        r = form_row(gs, r, "Key file", self.google_picker,
                     hint="Service account JSON. Empty = secrets\\*.json next to the app")
        r = form_row(gs, r, "Device types", ttk.Entry(gs, textvariable=self.types_var),
                     hint="Choices for DEVICE TYPE, comma separated, e.g.  UWB-K4W, UWB-K5")
        r = form_row(gs, r, "First sequence", ttk.Entry(gs, textvariable=self.seq_start_var, width=8),
                     hint="Used only while the sheet tab is empty (1-9999)")
        test_row = ttk.Frame(gs)
        test_row.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.test_btn = ttk.Button(test_row, text="Test connection", bootstyle="info-outline", command=self._test_sheet)
        self.test_btn.pack(side="left")
        self.test_lbl = ttk.Label(test_row, text="", style="Hint.TLabel", wraplength=380)
        self.test_lbl.pack(side="left", padx=(10, 0))

        nfc = ttk.Labelframe(body, text=" NFC signing (secrets) ", padding=PAD)
        nfc.columnconfigure(1, weight=1)
        ttk.Label(nfc, text="The PC signs every sticker and derives each tag's password. These files are secrets: "
                            "they stay on this PC (private copy in the data folder) and are never sent to the phone or logged.",
                  style="Hint.TLabel", wraplength=900, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.key_picker = SecretPicker(nfc, self, secrets.SIGNING_KEY, self.nfc_key_var, self.nfc_key_url_var)
        r = form_row(nfc, 1, "Signing key", self.key_picker, hint="ECDSA P-192 private key (.pem). Empty = secrets\\*.pem next to the app")
        self.secret_picker = SecretPicker(nfc, self, secrets.PASSWORD_SECRET, self.nfc_secret_var, self.nfc_secret_url_var)
        r = form_row(nfc, r, "Password secret", self.secret_picker,
                     hint="Hex secret (.secret) for the per-tag password. Empty = secrets\\*.secret next to the app")
        r = form_row(nfc, r, "Key ID", ttk.Spinbox(nfc, from_=1, to=15, textvariable=self.nfc_key_id_var, width=6),
                     hint="1-15, stored in the sticker's signature record; the mappers must trust this key ID")
        r = form_row(nfc, r, "Lock tags", ttk.Checkbutton(nfc, text="Lock the tag after writing (Verification 1)",
                                                         variable=self.nfc_lock_var, bootstyle="round-toggle"),
                     hint="AUTH0 = 02: writing then needs the tag's password; reads stay open for the mappers. "
                          "Verification 1 and 2 then fail an open tag. Lock bits / CFGLCK are never set, so a "
                          "locked tag can still be rewritten with its password. Switch off only for test tags.")
        sign_row = ttk.Frame(nfc)
        sign_row.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(sign_row, text="Test signing", bootstyle="info-outline", command=self._test_signing).pack(side="left")
        self.sign_lbl = ttk.Label(sign_row, text="", style="Hint.TLabel", wraplength=700)
        self.sign_lbl.pack(side="left", padx=(10, 0))

        # --- admin view: every secret behind an eye button (hidden again after 30 s, never logged) ---
        admin = ttk.Labelframe(nfc, text=" Admin view (secrets - click 👁 to show) ", padding=PAD)
        admin.grid(row=r + 1, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        admin.columnconfigure(1, weight=1)
        key = lambda: self.nfc_key_var.get().strip()            # noqa: E731 - current selection, saved or not
        secret = lambda: self.nfc_secret_var.get().strip()      # noqa: E731
        self.public_var = tk.StringVar()
        pub = ttk.Entry(admin, textvariable=self.public_var, state="readonly", font=("Consolas", 9))
        a = form_row(admin, 0, "Public key", pub, hint="Not secret: give it to the mapper team as trusted key ID " + self.nfc_key_id_var.get())
        a = form_row(admin, a, "Signing key", SecretField(admin, "NFC signing key", lambda: secrets.reveal_signing_key(key()), lines=8))
        a = form_row(admin, a, "Password secret", SecretField(admin, "NFC password secret", lambda: secrets.reveal_password_secret(secret()), lines=2))
        a = form_row(admin, a, "Google key", SecretField(admin, "Google service account key",
                                                         lambda: secrets.reveal_google_key(self.key_var.get().strip()), lines=10))
        lookup = ttk.Frame(admin)
        lookup.columnconfigure(1, weight=1)
        self.lookup_uid_var = tk.StringVar()
        ttk.Entry(lookup, textvariable=self.lookup_uid_var, width=18, font=("Consolas", 10)).grid(row=0, column=0, sticky="n", padx=(0, 8))
        self.lookup_field = SecretField(lookup, "tag password",
                                        lambda: secrets.tag_password_text(self.lookup_uid_var.get(), secret()))
        self.lookup_field.grid(row=0, column=1, sticky="ew")
        self.lookup_uid_var.trace_add("write", lambda *_: self.lookup_field.hide())
        form_row(admin, a, "Tag password", lookup,
                 hint="Enter a tag UID, then 👁: its PWD / PACK - to check it in NXP TagInfo (PWD_AUTH) or un-protect a test tag")
        self._update_public_key()

        wide = {jl: dict(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, PAD)),
                st: dict(row=0, column=1, sticky="nsew"),
                gs: dict(row=1, column=1, sticky="nsew", pady=(PAD, 0)),
                nfc: dict(row=2, column=0, columnspan=2, sticky="nsew", pady=(PAD, 0))}
        narrow = {w: dict(row=i, column=0, columnspan=2, sticky="nsew", pady=(0 if i == 0 else PAD, 0))
                  for i, w in enumerate((jl, st, gs, nfc))}
        self._responsive = Responsive(body, self, 1000, wide, narrow)

    def mark_dirty(self, text: str) -> None:
        self.saved_lbl.configure(text=text, bootstyle="warning")
        self._update_public_key()

    def _update_public_key(self) -> None:
        if not hasattr(self, "public_var"):
            return
        try:
            self.public_var.set(secrets.public_key_hex(self.nfc_key_var.get().strip()))
        except secrets.SecretError as exc:
            self.public_var.set(f"✖ {exc}")

    def _test_signing(self) -> None:
        from ...core import nfc_sticker as st
        try:
            key_id = int(self.nfc_key_id_var.get())
            signer = secrets.load_signer(self.nfc_key_var.get().strip(), self.nfc_secret_var.get().strip(), key_id)
            uid, mac = "04000000000000", "000000000000"
            ndef = signer.ndef(uid, mac)
            records = st.parse_records(st.find_ndef(ndef)[0])
            sig = next(r for r in records if r.is_signature).payload
            if not st.verify(signer.key.public_key(), uid, mac, sig[1:]):
                raise secrets.SecretError("the signature does not verify")
        except ValueError:
            self.sign_lbl.configure(text="✖  Key ID must be a number 1-15", bootstyle="danger")
            return
        except secrets.SecretError as exc:
            self.sign_lbl.configure(text=f"✖  {exc}", bootstyle="danger")
            return
        dev = "  ·  DEV key: stickers are accepted by dev mappers only" if signer.is_dev else ""
        self.sign_lbl.configure(text=f"✔  Signing works: key ID {key_id}, {len(ndef)} B sticker{dev}",
                                bootstyle="warning" if signer.is_dev else "success")

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
        try:
            key_id = int(self.nfc_key_id_var.get())
            if not 1 <= key_id <= 15:
                raise ValueError
        except ValueError:
            Messagebox.show_error("The NFC key ID must be a number from 1 to 15.", "Settings", parent=self)
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
        s.nfc.key_file = self.nfc_key_var.get().strip()
        s.nfc.key_url = self.nfc_key_url_var.get().strip()
        s.nfc.key_id = key_id
        s.nfc.secret_file = self.nfc_secret_var.get().strip()
        s.nfc.secret_url = self.nfc_secret_url_var.get().strip()
        s.nfc.lock = self.nfc_lock_var.get()
        self.app.save_settings()
        self.app.generate_view.set_device_types(types)

        self.app.probe_bar.refresh_exe()
        if sheet_changed:
            self.app.generate_view.refresh()
        else:
            self.app.generate_view._update_next_seq()
        self._update_detected()
        for picker in (self.google_picker, self.key_picker, self.secret_picker):
            picker.refresh()
        if station_changed:
            self.app.restart_station()
        self.app.set_status("Settings saved")
        self.saved_lbl.configure(text="✔  Saved", bootstyle="success")
        self.after(4000, lambda: self.saved_lbl.configure(text=""))
