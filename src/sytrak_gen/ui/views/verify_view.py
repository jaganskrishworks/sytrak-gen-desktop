"""Verify QR tab (device first, everything started from this PC).

  1. Read the MAC from the device over J-Link
  2. Look it up in the Google Sheet -> SEQ (shown)
  3. Send SEQ + MAC to the paired mobile; it scans the sticker and confirms
  4. NFC (the phone only moves bytes, the PC signs and judges - core/nfc_job.py):
       Verification 1: write the signed sticker, set + check the tag password, lock it (AUTH0 = 02)
       Verification 2: read the sticker and check it (nothing written)
"""
from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from PIL import Image, ImageTk
from ttkbootstrap.dialogs import Messagebox

try:
    from ttkbootstrap.widgets.scrolled import ScrolledFrame
except ImportError:  # older ttkbootstrap
    from ttkbootstrap.scrolled import ScrolledFrame

from ...core import nfc_sticker as st
from ...core.nfc_job import VERIFY, WRITE, JobError, NfcJob
from ...core.qr import compact_mac, format_seq, parse_payload, render_label
from ...core.secrets import SecretError, load_signer
from ...core.sheets import SheetRecord
from ..theme import BANNER_FONT, LABEL_BOLD, MONO_FONT, PAD
from ..widgets import CheckRow, Console, KeyValueGrid, Responsive, SecretField

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)

STEP_NAMES = {"tag": "Tag detected", "write": "Write", "readback": "Read back", "password": "Password set",
              "pwd_auth": "Password check", "lock": "Lock", "read": "Read", "final read": "Final read"}


def device_duplicates(records: list[SheetRecord], mac: str) -> list[str]:
    """Duplicate problems that involve this device: its MAC on several rows, or its SEQ shared."""
    mine = [r for r in records if r.mac == mac]
    problems = []
    if len(mine) > 1:
        problems.append(f"MAC {compact_mac(mac)} is on {len(mine)} rows: "
                        + ", ".join(f"row {r.row} (SEQ {r.seq_text})" for r in mine))
    for r in mine[:1]:
        others = [o for o in records if o.seq == r.seq and o.mac != mac]
        if others:
            problems.append(f"SEQ {r.seq_text} is also used by "
                            + ", ".join(f"row {o.row} ({compact_mac(o.mac)})" for o in others))
    return problems


def sticker_check(qr_text: str, device_mac: str, expected_seq: int | None) -> tuple[bool, str]:
    """Does the scanned sticker belong to this device? Returns (ok, detail)."""
    parsed = parse_payload(qr_text)
    if not parsed:
        return False, f"Sticker is not a SyTrak QR code ('{qr_text[:40]}')"
    mac, seq = parsed
    if mac != device_mac:
        return False, f"Sticker {compact_mac(mac)}  ≠  device {compact_mac(device_mac)}"
    if seq is not None and expected_seq is not None and seq != expected_seq:  # older codes also carry the SEQ
        return False, f"Sticker says SEQ {format_seq(seq)}, sheet has SEQ {format_seq(expected_seq)}"
    return True, f"Sticker {compact_mac(mac)}  =  device MAC"


def report_rows(job: NfcJob, phone: dict) -> list[tuple[str, str, str | None]]:
    """Label / value / style rows describing everything known about the tag."""
    r = job.report
    rows: list[tuple[str, str, str | None]] = [
        ("Job", f"Verification {job.group} · {'write signed sticker' if job.mode == WRITE else 'verify sticker (read only)'}"
                f"  ·  {job.job_id}", None)]
    if r is None:
        return rows
    good, bad = "success", "danger"
    rows += [
        ("Tag UID", r.uid or "—", None),
        ("Chip", f"{r.chip.name}  ·  {r.chip.user_bytes} B user memory  ·  CFG0 page {r.chip.cfg0}" if r.chip
         else f"unknown (GET_VERSION {r.version.hex(' ').upper() or '—'})", None if r.chip else bad),
        ("GET_VERSION", r.version.hex(" ").upper() or "—", None),
        ("MAC on tag", r.mac_on_tag or "— (no MAC record)", good if r.mac_on_tag == job.mac else bad),
        ("Device MAC", f"{job.mac}  (J-Link)" + (f"  ·  SEQ {job.seq}" if job.seq else ""), None),
        ("Signature", {st.SIG_VALID: "✔ valid"}.get(r.sig_status, f"✖ {r.sig_status.replace('_', ' ')}")
         + (f"  ·  format v{r.sig_version}  ·  key ID {r.key_id}" if r.sig_version is not None else ""),
         good if r.sig_status == st.SIG_VALID else bad),
    ]
    if r.signature:
        rows.append(("Signature r‖s", f"{r.signature[:24].hex().upper()}\n{r.signature[24:].hex().upper()}", None))
    if r.signed_text:
        rows.append(("Signed text", r.signed_text, None))
    rows.append(("Password", {"validated": "✔ validated (PWD_AUTH answered the expected PACK)"}.get(r.password, f"✖ {r.password}")
                 + ("  ·  PWD/PACK written by this job" if phone.get("pwd_written") else ""),
                 good if r.password == "validated" else bad))
    if r.config:
        rows.append(("Locked", (f"✔ Yes - writes from page {r.config.auth0} need the password, reads stay open"
                                if r.config.protected else ("✖ No - the tag is open" if job.lock else "No - locking is off in Settings"))
                     + f"  ·  AUTH0 {r.config.auth0:02X}",
                     good if r.config.protected else (bad if job.lock else "warning")))
        rows.append(("Configuration", r.config.describe(), None))
    if r.written is not None:
        rows.append(("Written data", "✔ read back identical" if r.written else "✖ read back differs", good if r.written else bad))
    rows.append(("NDEF records", "\n".join(f"{i + 1}. {rec.describe()}" for i, rec in enumerate(r.records))
                 or f"none ({r.ndef_problem})", None if r.records else bad))
    return rows


@dataclass
class Mobile:
    id: str
    address: str = ""
    name: str = ""
    online: bool = True

    @property
    def label(self) -> str:
        name = f"{self.name}  ({self.id})" if self.name else self.id
        return name + ("" if self.online else "  — offline")


class VerifyView(ttk.Frame):
    def __init__(self, master, app: "MainWindow"):
        super().__init__(master, padding=PAD)
        self.app = app
        self.mobiles: dict[str, Mobile] = {}
        self._labels: dict[str, str] = {}
        self.paired: str | None = None
        self.mobile_var = tk.StringVar()
        self._preview_img: ImageTk.PhotoImage | None = None
        self.job: NfcJob | None = None
        self._read_gen = 0      # bumped when a J-Link read's result must be ignored
        self._reset_state()
        self._build()
        self._refresh_mobiles()
        self._update_verdict()
        self.after(1000, self._tick)

    def _reset_state(self) -> None:
        self.device_mac: str | None = None   # read over J-Link
        self.record: SheetRecord | None = None
        self.request_sent = False
        self.raw_qr = ""                      # last sticker scan from the mobile
        self.manual = False
        self.verified_saved: set[int] = set()  # verification blocks (1/2) where Verified = Yes was written
        self.manual_saved: set[int] = set()    # ... and Manually Verified = Yes
        self._cancel_job("a new device was read")
        self.job = None
        self._phone_steps: list[str] = []
        self._owner_lookup: tuple[str, str] | None = None   # (job id, tag UID) being checked in the sheet

    # ------------------------------------------------------------- layout ---

    def _build(self) -> None:
        conn = ttk.Labelframe(self, text=" Mobile connection ", padding=PAD)
        conn.pack(fill="x", pady=(0, PAD))
        conn.columnconfigure(5, weight=1)
        ttk.Label(conn, text="Mobile", font=LABEL_BOLD).grid(row=0, column=0, sticky="w")
        self.combo = ttk.Combobox(conn, textvariable=self.mobile_var, state="readonly", width=40)
        self.combo.grid(row=0, column=1, padx=(10, 8))
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self._update_buttons())
        self.connect_btn = ttk.Button(conn, text="Connect", bootstyle="success", command=self._connect, width=12)
        self.connect_btn.grid(row=0, column=2, padx=(0, 6))
        self.disconnect_btn = ttk.Button(conn, text="Disconnect", bootstyle="danger-outline", command=self._disconnect, width=12)
        self.disconnect_btn.grid(row=0, column=3)
        self.conn_status = ttk.Label(conn, text="", font=LABEL_BOLD)
        self.conn_status.grid(row=0, column=4, sticky="w", padx=(16, 0))
        self.station_hint = ttk.Label(conn, text="", style="Hint.TLabel", wraplength=1000)
        self.station_hint.grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        conn.bind("<Configure>", lambda e: self.station_hint.configure(wraplength=max(300, e.width - 40)))

        self.body = body = ScrolledFrame(self, autohide=True)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        gutter = (0, 18)   # room for the scroll bar on the right

        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="nsew", padx=gutter)
        left = self._build_device(top)
        right = self._build_verification(top)
        Responsive(top, self, 1100,
                   wide={left: dict(row=0, column=0, sticky="nsew", padx=(0, PAD)), right: dict(row=0, column=1, sticky="nsew")},
                   narrow={left: dict(row=0, column=0, columnspan=2, sticky="nsew"),
                           right: dict(row=1, column=0, columnspan=2, sticky="nsew", pady=(PAD, 0))},
                   columns=({0: 2, 1: 3}, {0: 1, 1: 0}))

        self._build_nfc(body).grid(row=1, column=0, sticky="nsew", pady=(PAD, 0), padx=gutter)

        logbox = ttk.Labelframe(body, text=" Activity log ", padding=(PAD, 4, PAD, PAD))
        logbox.grid(row=2, column=0, sticky="nsew", pady=(PAD, 0), padx=gutter)
        logbox.columnconfigure(0, weight=1)
        self.console = Console(logbox, height=9)
        self.console.grid(row=0, column=0, sticky="nsew")

        for chk in (self.chk_device, self.chk_store, self.chk_sticker):
            chk.set("pending", "—")

    def _build_device(self, parent) -> ttk.Labelframe:
        left = ttk.Labelframe(parent, text=" Device ", padding=PAD)
        left.columnconfigure(1, weight=1)
        # Same variable as the Generate QR dropdown: one shared tab selection.
        tabrow = ttk.Frame(left)
        tabrow.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        tabrow.columnconfigure(1, weight=1)
        ttk.Label(tabrow, text="Sheet tab", font=LABEL_BOLD).grid(row=0, column=0, sticky="w")
        self.tab_combo = ttk.Combobox(tabrow, textvariable=self.app.generate_view.tab_var, state="readonly")
        self.tab_combo.grid(row=0, column=1, sticky="ew", padx=(10, 6))
        self.tab_combo.bind("<<ComboboxSelected>>", lambda _e: self.app.generate_view._on_tab_selected())
        ttk.Button(tabrow, text="⟳", width=3, bootstyle="primary-outline",
                   command=self.app.generate_view.load_tabs).grid(row=0, column=2)
        ttk.Label(tabrow, text="The device is looked up (SEQ, duplicates) and its Verification / NFC results are saved in this tab",
                  style="Hint.TLabel", wraplength=380).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.read_btn = ttk.Button(left, text="①  Read MAC from device", bootstyle="primary", command=self._read_device)
        self.read_btn.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=5)
        ttk.Label(left, text="MAC", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.dev_mac_lbl = ttk.Label(left, text="—", font=("Consolas", 15, "bold"))
        self.dev_mac_lbl.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(10, 0))
        ttk.Label(left, text="SEQ", style="Hint.TLabel").grid(row=3, column=0, sticky="w")
        self.dev_seq_lbl = ttk.Label(left, text="—", font=("Segoe UI", 22, "bold"), bootstyle="primary")
        self.dev_seq_lbl.grid(row=3, column=1, sticky="w", padx=(10, 0))
        self.dev_meta = ttk.Label(left, text="", style="Hint.TLabel", wraplength=380)
        self.dev_meta.grid(row=4, column=0, columnspan=2, sticky="w")
        self.preview = ttk.Label(left, text="Read the device MAC to start", style="Hint.TLabel", anchor="center")
        self.preview.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(6, 6))
        left.rowconfigure(5, weight=1)
        self.send_btn = ttk.Button(left, text="②  Send to mobile for sticker check", bootstyle="info-outline",
                                   command=self._send_to_mobile, state="disabled")
        self.send_btn.grid(row=6, column=0, columnspan=2, sticky="ew")
        return left

    def _build_verification(self, parent) -> ttk.Labelframe:
        right = ttk.Labelframe(parent, text=" Verification ", padding=PAD)
        right.columnconfigure(0, weight=1)
        # Mode switch, top right: Verification 1 (write signed sticker) | Verification 2 (read only).
        modebar = ttk.Frame(right)
        modebar.grid(row=0, column=0, sticky="e", pady=(0, 6))
        self.mode_var = tk.IntVar(value=2 if self.app.settings.qr.verify_mode == 2 else 1)
        for value, text in ((1, "Verification 1  ·  Write signed sticker"), (2, "Verification 2  ·  Read only")):
            ttk.Radiobutton(modebar, text=text, value=value, variable=self.mode_var, bootstyle="primary-toolbutton",
                            command=self._on_mode_changed, padding=(12, 6)).pack(side="left")

        self.chk_device = CheckRow(right, "Device MAC  (J-Link)")
        self.chk_device.grid(row=1, column=0, sticky="ew")
        self.chk_store = CheckRow(right, "Stored data  (Google Sheet → SEQ)")
        self.chk_store.grid(row=2, column=0, sticky="ew")
        self.chk_sticker = CheckRow(right, "Sticker QR scanned on mobile  =  device MAC")
        self.chk_sticker.grid(row=3, column=0, sticky="ew")
        self.chk_nfc = CheckRow(right, "NFC sticker  (signature · MAC · password)")
        self.chk_nfc.grid(row=4, column=0, sticky="ew")

        self.banner = ttk.Label(right, text="", font=BANNER_FONT, anchor="center", padding=12)
        self.banner.grid(row=5, column=0, sticky="ew", pady=(10, 10))

        btns = ttk.Frame(right)
        btns.grid(row=6, column=0, sticky="ew")
        btns.columnconfigure(0, weight=1, uniform="b")
        btns.columnconfigure(1, weight=2, uniform="b")
        self.manual_btn = ttk.Button(btns, text="Verified Manually", bootstyle="warning-outline", command=self._verify_manually)
        self.manual_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=5)
        # One NFC button; what it does follows the Verification 1 / 2 switch.
        self.nfc_btn = ttk.Button(btns, text="", command=self._nfc_action)
        self.nfc_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0), ipady=5)
        self.sheet_lbl = ttk.Label(right, text="", style="Hint.TLabel", wraplength=560)
        self.sheet_lbl.grid(row=7, column=0, sticky="w", pady=(8, 0))
        right.bind("<Configure>", lambda e: self.sheet_lbl.configure(wraplength=max(250, e.width - 30)))
        return right

    def _build_nfc(self, parent) -> ttk.Labelframe:
        box = ttk.Labelframe(parent, text=" NFC sticker ", padding=PAD)
        box.columnconfigure(0, weight=1)
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.job_lbl = ttk.Label(head, text="No NFC job yet - read a device, then use the NFC button above.",
                                 font=LABEL_BOLD, wraplength=900)
        self.job_lbl.grid(row=0, column=0, sticky="w")
        self.cancel_btn = ttk.Button(head, text="Cancel NFC job", bootstyle="danger-outline", command=self._cancel_clicked)
        self.cancel_btn.grid(row=0, column=1, sticky="e")
        self.cancel_btn.grid_remove()
        head.bind("<Configure>", lambda e: self.job_lbl.configure(wraplength=max(250, e.width - 180)))
        pw_row = ttk.Frame(head)
        pw_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(pw_row, text="Tag password (admin)", style="Hint.TLabel").pack(side="left", padx=(0, 10))
        self.tag_pw_field = SecretField(pw_row, "tag password (Verify tab)", self._tag_password, width=48)
        self.tag_pw_field.pack(side="left")

        tabs = ttk.Notebook(box, bootstyle="secondary")
        tabs.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        summary = ttk.Frame(tabs, padding=PAD)
        summary.columnconfigure(0, weight=1)
        self.nfc_checks = ttk.Frame(summary)
        self.nfc_details = KeyValueGrid(summary, wraplength=620)
        Responsive(summary, self, 1200,
                   wide={self.nfc_checks: dict(row=0, column=0, sticky="nsew", padx=(0, PAD)),
                         self.nfc_details: dict(row=0, column=1, sticky="nsew")},
                   narrow={self.nfc_checks: dict(row=0, column=0, columnspan=2, sticky="nsew"),
                           self.nfc_details: dict(row=1, column=0, columnspan=2, sticky="nsew", pady=(PAD, 0))},
                   columns=({0: 2, 1: 3}, {0: 1, 1: 0}))
        tabs.add(summary, text="  Checks & details  ")

        self.dump_text = self._text_tab(tabs, "  Tag memory (all pages)  ", height=14)
        self.steps_text = self._text_tab(tabs, "  Phone steps  ", height=10)
        self._show_job()
        return box

    def _tag_password(self) -> str:
        """PWD / PACK of the tag of the current job - shown only behind the eye button."""
        job = self.job
        if job is None or not job.uid:
            raise ValueError("no tag yet - run an NFC job first")
        pwd, pack = job.signer.password(job.uid)
        return f"UID {job.uid}   PWD {pwd.hex().upper()}   PACK {pack.hex().upper()}"

    def _text_tab(self, tabs: ttk.Notebook, title: str, height: int) -> tk.Text:
        frame = ttk.Frame(tabs, padding=(PAD, PAD, 0, PAD))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, height=height, font=MONO_FONT, wrap="none", relief="flat", state="disabled",
                       background="#fbfbfc", highlightthickness=1, highlightbackground="#dee2e6")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview, bootstyle="round")
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview, bootstyle="round")
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tabs.add(frame, text=title)
        return text

    @staticmethod
    def _set_text(widget: tk.Text, lines: list[str]) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "\n".join(lines))
        widget.configure(state="disabled")

    def set_station_info(self, text: str) -> None:
        self.station_hint.configure(text=text)

    # ------------------------------------------------------------- mobiles ---

    def handle_event(self, kind: str, data: dict) -> None:
        mid = data.get("id", "")
        if kind == "mobile_connected":
            mobile = self.mobiles.setdefault(mid, Mobile(mid))
            mobile.address, mobile.online = data.get("address", ""), True
            self.console.write(f"Mobile connected: {mid} ({mobile.address})", "accent")
        elif kind == "mobile_hello":
            mobile = self.mobiles.setdefault(mid, Mobile(mid))
            mobile.name = data.get("name", "")
            if mid == self.paired:
                if self.job and self.job.state == "waiting":
                    self.app.station.start_nfc_job(self.job.request())   # reconnecting mobile gets the job again
                    self.console.write("Mobile reconnected - NFC job sent again", "accent")
                elif self.record and not self.raw_qr:
                    self._send_to_mobile(quiet=True)  # ... or the pending sticker request
        elif kind == "mobile_disconnected":
            if mid == self.paired and mid in self.mobiles:
                self.mobiles[mid].online = False
                self.console.write(f"Paired mobile went offline: {mid}"
                                   + (" - the NFC job waits for it to come back" if self.job and self.job.active else ""), "warning")
            else:
                self.mobiles.pop(mid, None)
                self.console.write(f"Mobile disconnected: {mid}", "muted")
        elif kind == "scan":
            self._on_scan(data.get("qr", ""), mid)
            return
        elif kind == "scan_rejected":
            self.console.write(f"Ignored scan from unpaired mobile {mid}", "warning")
            return
        elif kind == "nfc_tag":
            self._on_nfc_tag(data)
            return
        elif kind == "nfc_progress":
            self._on_nfc_progress(data)
            return
        elif kind == "nfc_done":
            self._on_nfc_done(data)
            return
        self._refresh_mobiles()

    def _refresh_mobiles(self) -> None:
        current = self._labels.get(self.mobile_var.get())
        self._labels = {m.label: m.id for m in self.mobiles.values()}
        self.combo.configure(values=list(self._labels))
        keep = self.paired or current
        if keep and keep in self.mobiles:
            self.mobile_var.set(self.mobiles[keep].label)
        elif len(self.mobiles) == 1:
            self.mobile_var.set(next(iter(self.mobiles.values())).label)
        else:
            self.mobile_var.set("")
        self._update_buttons()

    def _update_buttons(self) -> None:
        selected = self._labels.get(self.mobile_var.get())
        self.connect_btn.configure(state="normal" if selected and selected != self.paired and self.app.station_running else "disabled")
        self.disconnect_btn.configure(state="normal" if self.paired else "disabled")
        if self.paired:
            m = self.mobiles.get(self.paired)
            online = m is not None and m.online
            name = (m.name or m.id) if m else self.paired
            self.conn_status.configure(text=f"●  Connected to {name}" if online else f"●  {name} offline - waiting to reconnect",
                                       bootstyle="success" if online else "warning")
            self.app.set_mobile_status(name, online)
        else:
            n = len(self.mobiles)
            text = f"●  {n} mobile{'s' if n != 1 else ''} available - select and Connect" if n else "●  Waiting for a mobile to join…"
            self.conn_status.configure(text=text, bootstyle="secondary")
            self.app.set_mobile_status(None, False)
        self._update_verdict()

    def _connect(self) -> None:
        mid = self._labels.get(self.mobile_var.get())
        if not mid or not self.app.station:
            return
        if self.job and self.job.active:
            self._cancel_job("the mobile was changed")
        self.app.station.pair(mid)
        self.paired = mid
        self.console.write(f"Connected to mobile {mid}", "success")
        self._update_buttons()
        if self.record and not self.raw_qr:
            self._send_to_mobile()

    def _disconnect(self) -> None:
        self._cancel_job("the mobile was disconnected")
        if self.app.station:
            self.app.station.unpair()
        self.console.write(f"Disconnected from mobile {self.paired}", "muted")
        if self.paired and self.paired in self.mobiles and not self.mobiles[self.paired].online:
            self.mobiles.pop(self.paired)
        self.paired = None
        self._refresh_mobiles()

    def _paired_online(self) -> bool:
        m = self.mobiles.get(self.paired or "")
        return m is not None and m.online

    # -------------------------------------------- step 1: device + sheet ---

    def _read_device(self) -> None:
        self._reset_state()
        for lbl in (self.sheet_lbl, self.dev_meta):
            lbl.configure(text="")
        self.dev_mac_lbl.configure(text="—")
        self.dev_seq_lbl.configure(text="—")
        self._clear_preview("Reading…")
        self.chk_device.set("busy", "Reading MAC over J-Link…")
        self.chk_store.set("pending", "—")
        self.chk_sticker.set("pending", "—")
        self.read_btn.configure(state="disabled", text="Reading MAC…")
        self._show_job()
        self._update_verdict()
        self._read_gen += 1
        gen = self._read_gen
        self.app.probe_bar.read_mac(lambda mac, err: self._on_device_mac(mac, err) if gen == self._read_gen else None)

    def _on_device_mac(self, mac: str | None, err: str) -> None:
        self.read_btn.configure(state="normal", text="①  Read MAC from device")
        if not mac:
            self.chk_device.set("fail", f"Could not read: {err}")
            self._clear_preview("Read the device MAC to start")
            self._update_verdict()
            return
        self.device_mac = mac
        self.dev_mac_lbl.configure(text=compact_mac(mac))
        self.chk_device.set("ok", f"MAC {compact_mac(mac)}")
        self.console.write(f"Device MAC {compact_mac(mac)}", "accent")
        self._check_sheet()

    def on_tab_changed(self) -> None:
        """Sheet tab switched (here or on Generate QR): re-check the loaded device in the new tab."""
        tab = self.app.generate_view.current_tab
        self.tab_combo.set(tab)
        if not self.device_mac:
            return
        self.console.write(f"Sheet tab changed to '{tab}' - re-checking MAC {compact_mac(self.device_mac)}", "accent")
        self._cancel_job("the sheet tab was changed")
        self.job = None
        self.record = None
        self.request_sent = False
        self.raw_qr = ""
        self.manual = False
        self.verified_saved, self.manual_saved = set(), set()
        for lbl in (self.sheet_lbl, self.dev_meta):
            lbl.configure(text="")
        self.dev_seq_lbl.configure(text="—")
        self.chk_sticker.set("pending", "—")
        self._show_job()
        self._check_sheet()

    def _check_sheet(self) -> None:
        """Find the device MAC in the selected sheet tab -> SEQ, and make sure it is not duplicated."""
        mac = self.device_mac
        tab = self.app.generate_view.current_tab or "?"
        self.chk_store.set("busy", f"Looking up sheet tab '{tab}' and checking for duplicates…")
        self._update_verdict()

        def done(result, err):
            if mac != self.device_mac:
                return  # another device was read meanwhile
            if err:
                self.chk_store.set("fail", f"Cannot read Google Sheet: {err}")
            else:
                records, tab_problems = result
                self.record = next((r for r in records if r.mac == mac), None)
                dups = device_duplicates(records, mac) if self.record else []
                other_problems = len(tab_problems) - len(dups)
                if other_problems > 0:
                    self.console.write(f"Note: sheet tab '{tab}' has {other_problems} other problem(s) "
                                       "(duplicates / invalid rows) - Generate PDF will list them", "warning")
                if self.record is None:
                    self.chk_store.set("fail", f"MAC {compact_mac(mac)} is not in sheet tab '{tab}' - generate its QR first")
                    self._clear_preview("Not registered")
                elif dups:
                    r = self.record
                    self.dev_seq_lbl.configure(text=r.seq_text)
                    self._clear_preview("Duplicate in sheet")
                    self.chk_store.set("fail", "DUPLICATE: " + "  ·  ".join(dups) + "  - fix the sheet, then read again")
                    for d in dups:
                        self.console.write(f"Duplicate in sheet: {d}", "error")
                    self.record = None  # not trustworthy until the sheet is fixed
                else:
                    r = self.record
                    self.dev_seq_lbl.configure(text=r.seq_text)
                    self.dev_meta.configure(text=f"{r.device_type or '-'}  ·  saved {r.created_at}"
                                                 + (f"  ·  {r.status}" if r.status else ""))
                    self._set_preview(render_label(r.payload, r.mac, r.seq))
                    self.chk_store.set("ok", f"Tab '{tab}': SEQ {r.seq_text}"
                                             f"{' · ' + r.device_type if r.device_type else ''} · no duplicates")
                    self.chk_sticker.set("pending", "Send to the mobile and scan the sticker…")
                    self._log_previous_nfc(r)
                    if self._paired_online():
                        self._send_to_mobile()
            self._update_verdict()

        self.app.run_sheet(lambda client: client.audit(), done)

    def _log_previous_nfc(self, r: SheetRecord) -> None:
        for n in (1, 2):
            g = r.group(n)
            if g.uid or g.signature:
                self.console.write(f"Sheet, Verification {n}: tag {g.uid or '?'} · signature {g.signature or '?'} · "
                                   f"password {g.password or '?'} · locked {g.locked or '?'} ({g.updated_at})", "muted")

    # ----------------------------------------------- step 2: mobile scan ---

    def _send_to_mobile(self, quiet: bool = False) -> None:
        if not (self.record and self.app.station):
            return
        r = self.record
        if not self.app.station.request_verify(compact_mac(r.mac), r.seq_text, r.device_type):
            if not quiet:
                Messagebox.show_warning("Connect a mobile first.", "Send to mobile", parent=self)
            return
        self.request_sent = True
        self.raw_qr = ""
        self.chk_sticker.set("busy", f"Waiting for the mobile to scan the sticker of SEQ {r.seq_text}…")
        if not quiet:
            self.console.write(f"Sent SEQ {r.seq_text} / MAC {compact_mac(r.mac)} to the mobile - scan the sticker", "accent")
        self._update_verdict()

    def _on_scan(self, qr: str, mobile_id: str) -> None:
        self.console.write(f"Sticker scanned on {mobile_id}: {qr}", "accent")
        if not self.device_mac:
            self.chk_sticker.set("fail", "Read the device MAC first (step ①), then scan the sticker")
            if self.app.station:
                self.app.station.send_verify_result(False, "", "", "Read the device MAC on the PC first")
            self._update_verdict()
            return
        self.raw_qr = qr
        ok, detail = sticker_check(qr, self.device_mac, self.record.seq if self.record else None)
        self.chk_sticker.set("ok" if ok else "fail", detail)
        if self.app.station:
            seq_text = self.record.seq_text if self.record else ""
            self.app.station.send_verify_result(ok and self.record is not None, compact_mac(self.device_mac), seq_text,
                                                "" if ok else detail)
        self._update_verdict()

    # ---------------------------------------------------------- verdict ---

    def _checks(self) -> tuple[CheckRow, ...]:
        return (self.chk_device, self.chk_store, self.chk_sticker)

    def _auto_verified(self) -> bool:
        return all(c.state == "ok" for c in self._checks())

    def _update_verdict(self) -> None:
        auto = self._auto_verified()
        group = self.mode_var.get()
        if auto and self.record and group not in self.verified_saved:
            self.verified_saved.add(group)
            self.console.write(f"SEQ {self.record.seq_text} verified: device, sheet and sticker match", "success")
            self._mark_sheet(group, "Verified", verified=True)
        if self.manual and self.record and group not in self.manual_saved:
            self.manual_saved.add(group)
            self._mark_sheet(group, "Manually Verified", manually_verified=True)

        job = self.job
        if job and job.state == "done":
            text = ("✔  VERIFICATION 1 OK — SIGNED STICKER WRITTEN AND VERIFIED" if job.mode == WRITE
                    else "✔  VERIFICATION 2 OK — STICKER SIGNATURE, MAC AND PASSWORD VALID")
            self.banner.configure(text=text, bootstyle="inverse-success")
        elif job and job.state == "failed":
            self.banner.configure(text="✖  NFC STICKER NOT OK — see the NFC sticker details below", bootstyle="inverse-danger")
        elif job and job.state == "working":
            self.banner.configure(text="⏳  Phone is working on the tag — keep it still", bootstyle="inverse-info")
        elif job and job.state == "waiting":
            self.banner.configure(text="⏳  Tap the NFC tag on the phone", bootstyle="inverse-info")
        elif auto:
            self.banner.configure(text="✔  DEVICE, SHEET AND STICKER MATCH — VERIFIED", bootstyle="inverse-success")
        elif self.manual:
            self.banner.configure(text="✔  VERIFIED MANUALLY", bootstyle="inverse-warning")
        elif any(c.state == "fail" for c in self._checks()):
            self.banner.configure(text="✖  NOT VERIFIED — MISMATCH", bootstyle="inverse-danger")
        elif not self.device_mac and self.chk_device.state != "busy":
            self.banner.configure(text="①  Read the device MAC", bootstyle="inverse-light")
        elif self.record and not self.raw_qr:
            self.banner.configure(text="②  Scan the sticker on the mobile", bootstyle="inverse-info")
        else:
            self.banner.configure(text="Verifying…", bootstyle="inverse-info")

        busy_job = job is not None and job.active
        self.send_btn.configure(state="normal" if self.record and self._paired_online() and not busy_job else "disabled")
        self.send_btn.configure(text="②  Send again to mobile" if self.request_sent else "②  Send to mobile for sticker check")
        same_mode_done = job is not None and job.group == group and job.state == "done"
        if group == 1:   # Verification 1: write the signed sticker, then everything is checked
            enabled = (auto or self.manual) and self._paired_online() and not busy_job
            again = job is not None and job.group == 1 and job.state in ("failed", "cancelled", "done")
            self.nfc_btn.configure(text="①  Write signed sticker again" if again else "①  Write signed NFC sticker",
                                   bootstyle="success")
        else:            # Verification 2: read the existing tag only
            enabled = self.device_mac is not None and self._paired_online() and not busy_job
            self.nfc_btn.configure(text="②  Verify NFC sticker again" if same_mode_done else "②  Verify NFC sticker  (read only)",
                                   bootstyle="info")
        self.nfc_btn.configure(state="normal" if enabled else "disabled")
        busy = "busy" in (self.chk_device.state, self.chk_store.state)
        nfc_ok = job is not None and job.state == "done"
        can_manual = self.device_mac is not None and not auto and not self.manual and not busy and not nfc_ok
        self.manual_btn.configure(state="normal" if can_manual else "disabled")

    def _verify_manually(self) -> None:
        answer = Messagebox.yesno(
            "The automatic checks did not all pass.\n\nConfirm you have checked this device manually?",
            "Verify manually", parent=self)
        if answer not in ("Yes", "yes", True):
            return
        self.manual = True
        self.console.write(f"MAC {compact_mac(self.device_mac or '')} verified manually", "warning")
        if not self.record:   # with a record, _update_verdict writes Manually Verified to the active block
            self.sheet_lbl.configure(text="Sheet: not updated - this MAC has no row in the selected tab", bootstyle="warning")
        self._update_verdict()

    def _on_mode_changed(self) -> None:
        """Verification 1 and 2 are separate verifications: switching starts again from step ①.
        Nothing done for one (sticker check, manual check, NFC job) counts for the other."""
        mode = self.mode_var.get()
        self.app.settings.qr.verify_mode = mode
        self.app.save_settings()
        self._start_over(f"switched to Verification {mode}")
        self.console.write(("Verification 1: write the signed NFC sticker and check it" if mode == 1
                            else "Verification 2: read the NFC sticker and check it (nothing is written)")
                           + " - start again: ① Read MAC from device", "accent")
        self._update_verdict()

    def _start_over(self, reason: str) -> None:
        """Forget the device, sheet lookup, sticker scan, manual check and NFC job (nothing is written)."""
        self._read_gen += 1                     # a J-Link read still running is ignored when it returns
        self._cancel_job(reason)
        self._reset_state()
        for lbl in (self.sheet_lbl, self.dev_meta):
            lbl.configure(text="")
        self.dev_mac_lbl.configure(text="—")
        self.dev_seq_lbl.configure(text="—")
        self._clear_preview("Read the device MAC to start")
        self.read_btn.configure(state="normal", text="①  Read MAC from device")
        for chk in (self.chk_device, self.chk_store, self.chk_sticker):
            chk.set("pending", "—")
        self.tag_pw_field.hide()
        self._show_job()

    # ------------------------------------------------------------- NFC job ---

    def _nfc_action(self) -> None:
        if not (self.app.station and self.device_mac):
            return
        if not self._paired_online():
            Messagebox.show_warning("Connect a mobile first.", "NFC", parent=self)
            return
        nfc = self.app.settings.nfc
        try:
            signer = load_signer(nfc.key_file, nfc.secret_file, nfc.key_id)
        except SecretError as exc:
            self.console.write(f"NFC: {exc}", "error")
            Messagebox.show_error(f"{exc}\n\nOpen Settings › NFC signing (secrets), choose the files "
                                  "(Browse… or From URL…) and click Save settings.", "NFC signing", parent=self)
            return
        mode = WRITE if self.mode_var.get() == 1 else VERIFY
        if mode == WRITE and not self.record:
            Messagebox.show_warning("This MAC has no row in the selected sheet tab - generate its QR first.", "NFC", parent=self)
            return
        if self.job and self.job.active:
            self._cancel_job("a new NFC job was started")
        seq = self.record.seq_text if self.record else ""
        self.job = NfcJob(mode, compact_mac(self.device_mac), seq, signer, lock=nfc.lock)
        self._phone_steps = []
        self.tag_pw_field.hide()
        if not self.app.station.start_nfc_job(self.job.request()):
            self.job = None
            Messagebox.show_warning("Connect a mobile first.", "NFC", parent=self)
            return
        what = "write the signed sticker" if mode == WRITE else "verify the sticker (read only)"
        dev = " (DEV signing key)" if signer.is_dev else ""
        self.console.write(f"NFC job {self.job.job_id}: Verification {self.job.group}, {what} for MAC {self.job.mac}"
                           f"{' / SEQ ' + seq if seq else ''}{dev} - tap the tag on the phone", "accent")
        self.chk_nfc.set("busy", "Waiting for the tag to be tapped on the phone…")
        self._show_job()
        self._update_verdict()

    def _job_event_ok(self, data: dict, kind: str) -> bool:
        """Only events of the current job from the paired mobile are used."""
        job_id = data.get("job_id")
        if self.job is None or job_id != self.job.job_id:
            self.console.write(f"Ignored {kind} for job {job_id or '?'} (not the current NFC job)", "muted")
            return False
        return True

    def _on_nfc_tag(self, data: dict) -> None:
        """A tag was tapped: read the sheet fresh (one tag per device), then check + sign it and answer."""
        if not self._job_event_ok(data, "tag"):
            return
        job = self.job
        uid = str(data.get("uid", "")).upper()
        if job.reply is not None:                     # the phone resent the same tap: same answer
            self._answer_tag(job, data, None)
            return
        if self._owner_lookup == (job.job_id, uid):   # still reading the sheet for this tap
            return
        self._owner_lookup = (job.job_id, uid)
        self.chk_nfc.set("busy", f"Tag {uid}: checking in the sheet that it belongs to this device…")

        def done(records, err):
            if self._owner_lookup == (job.job_id, uid):
                self._owner_lookup = None
            if self.job is not job or not job.active:
                return
            if err:
                self._send_reply(job, uid, {"job_id": job.job_id, "ok": False,
                                            "reason": f"cannot check the sheet ({err}) - tap the tag again"})
                return
            self._answer_tag(job, data, records)

        self.app.run_sheet(lambda client: client.records(), done)

    def _answer_tag(self, job: NfcJob, data: dict, records) -> None:
        try:
            reply = job.on_tag(data, records) if records is not None else job.on_tag(data)
        except Exception as exc:     # signing must never take the UI down
            log.exception("NFC job %s: on_tag failed", job.job_id)
            reply = {"job_id": job.job_id, "ok": False, "reason": f"PC error: {exc.__class__.__name__}"}
        self._send_reply(job, str(data.get("uid", "")).upper(), reply)

    def _send_reply(self, job: NfcJob, uid: str, reply: dict) -> None:
        self.app.station.reply_nfc_job(reply)
        if reply.get("ok") and job.owner_check is not None:
            self.console.write(f"{'✔' if job.owner_check.state == st.OK else '✖'} Tag UID ↔ MAC: {job.owner_check.detail}",
                               "success" if job.owner_check.state == st.OK else "error")
        if reply.get("ok"):
            if reply.get("write"):
                n = len(reply["write"]["data"]) // 8
                self.console.write(f"Tag {uid} detected - signed it (key {job.signer.key_id}), "
                                   f"sent {n} pages + its password to the phone", "accent")
            else:
                self.console.write(f"Tag {uid} detected - sent its password to the phone for the check", "accent")
            self.chk_nfc.set("busy", f"Tag {uid}: the phone is working on it - keep it still…")
        else:
            self.console.write(f"Tag {uid or '?'} refused: {reply.get('reason')}", "error")
            self.chk_nfc.set("fail", f"Tag refused: {reply.get('reason')} - tap another tag, or Cancel")
        self._show_job()
        self._update_verdict()

    def _on_nfc_progress(self, data: dict) -> None:
        if not self._job_event_ok(data, "progress"):
            return
        line = self._step_text(data)
        self._phone_steps.append(line)
        self.console.write(f"Phone: {line}", "info" if data.get("ok", True) else "warning")
        self._show_job()

    @staticmethod
    def _step_text(step: dict) -> str:
        name = STEP_NAMES.get(str(step.get("step")), str(step.get("step", "?")))
        mark = "✔" if step.get("ok", True) else "✖"
        return f"{mark} {name}" + (f": {step.get('detail')}" if step.get("detail") else "")

    def _on_nfc_done(self, data: dict) -> None:
        if not self._job_event_ok(data, "result"):
            return
        job = self.job
        if not job.active:
            self.console.write(f"Ignored a repeated result for job {job.job_id}", "muted")
            return
        try:
            report = job.on_done(data)
        except JobError as exc:
            job.state, job.error = "failed", str(exc)
            report = None
            self.console.write(f"NFC job {job.job_id}: {exc}", "error")
        except Exception as exc:
            log.exception("NFC job %s: on_done failed", job.job_id)
            job.state, job.error = "failed", f"PC error while checking the tag: {exc.__class__.__name__}"
            report = None
        self._phone_steps = [self._step_text(s) for s in job.steps] or self._phone_steps

        name = f"Verification {job.group}"
        if report is not None:
            for c in report.checks:
                level = {"ok": "success", "fail": "error"}.get(c.state, "muted")
                self.console.write(f"  {'✔' if c.state == 'ok' else '✖' if c.state == 'fail' else '·'} {c.title}: {c.detail}", level)
        if job.state == "done":
            self.chk_nfc.set("ok", f"Tag {job.uid}: signature valid · MAC = device · password validated · "
                                   f"locked {'Yes' if report.locked else 'No'}")
            self.console.write(f"{name} OK: NFC sticker {job.uid} for MAC {job.mac}", "success")
            self.app.set_status(f"MAC {job.mac}: {name} OK")
        else:
            why = job.error or "; ".join(f"{c.title}: {c.detail}" for c in (report.failures if report else []))
            self.chk_nfc.set("fail", why[:300])
            self.console.write(f"{name} NOT OK: {why}", "error")

        if report is not None and self.record and job.mac == compact_mac(self.device_mac or ""):
            column = "NFC Written" if job.mode == WRITE else "NFC Read"
            self._mark_sheet(job.group, f"{column} + tag details", nfc=report.ok, **report.sheet_values())
            sheet_note = "Saving to the sheet…"
        else:
            sheet_note = "Not saved: this MAC has no row in the selected sheet tab"
        self.app.station.send_nfc_verdict(job.verdict(sheet_note))
        self._show_job()
        self._update_verdict()

    def _cancel_clicked(self) -> None:
        self._cancel_job("cancelled on the PC")
        self._update_verdict()

    def _cancel_job(self, reason: str) -> None:
        job = getattr(self, "job", None)
        if job is None or not job.active:
            return
        job.state, job.error = "cancelled", reason
        if self.app.station:
            self.app.station.cancel_nfc_job(job.job_id, reason)
        if hasattr(self, "console"):
            self.console.write(f"NFC job {job.job_id} cancelled: {reason}", "warning")
            self.chk_nfc.set("pending", f"Cancelled: {reason}")
            self._show_job()

    def _tick(self) -> None:
        try:
            if self.job and self.job.expired:
                job = self.job
                self._cancel_job("no answer from the phone in time - tap the tag, or check the phone is connected")
                job.state = "failed"
                self.chk_nfc.set("fail", "Timed out waiting for the phone")
                self._update_verdict()
        finally:
            self.after(1000, self._tick)

    def _show_job(self) -> None:
        """Refresh the NFC sticker panel from the current job."""
        job = self.job
        if self.tag_pw_field.shown and (job is None or not job.uid):
            self.tag_pw_field.hide()
        for child in self.nfc_checks.winfo_children():
            child.destroy()
        if job is None:
            self.job_lbl.configure(text="No NFC job yet - read a device, then use the NFC button above.", bootstyle="secondary")
            self.cancel_btn.grid_remove()
            self.nfc_details.set_rows([])
            self._set_text(self.dump_text, ["(the tag's pages appear here after an NFC job)"])
            self._set_text(self.steps_text, ["(each step the phone takes appears here)"])
            self.chk_nfc.set("pending", "—")
            return
        state_text = {"waiting": "⏳ waiting for the tag to be tapped on the phone",
                      "working": f"⏳ the phone is working on tag {job.uid}",
                      "done": "✔ all checks passed", "failed": "✖ NOT OK", "cancelled": f"cancelled - {job.error}"}[job.state]
        style = {"done": "success", "failed": "danger", "cancelled": "warning"}.get(job.state, "info")
        title = "Verification 1 · write signed sticker" if job.mode == WRITE else "Verification 2 · verify sticker"
        self.job_lbl.configure(text=f"{title}  ·  MAC {job.mac}  ·  {state_text}", bootstyle=style)
        if job.active:
            self.cancel_btn.grid()
        else:
            self.cancel_btn.grid_remove()

        if job.report:
            for c in job.report.checks:
                row = CheckRow(self.nfc_checks, c.title, wraplength=420, padding=(2, 3))
                row.set(c.state if c.state in CheckRow.ICONS else "info", c.detail)
                row.pack(fill="x")
        else:
            waiting = CheckRow(self.nfc_checks, "Tag", padding=(2, 3))
            waiting.set("busy" if job.active else "fail" if job.state == "failed" else "pending",
                        job.error or ("Tap the tag on the phone" if job.state == "waiting" else "Working…"))
            waiting.pack(fill="x")
        self.nfc_details.set_rows(report_rows(job, {"pwd_written": any(s.get("step") == "password" and s.get("ok")
                                                                       for s in job.steps)}))
        if job.report and job.report.pages:
            self._set_text(self.dump_text, [f"Tag {job.uid}  ·  {len(job.report.pages) // 4} pages  ·  "
                                            "PWD/PACK always read as 00", ""] + job.report.page_dump())
        self._set_text(self.steps_text, self._phone_steps or ["(no steps reported yet)"])

    # -------------------------------------------------------- sheet status ---

    def _mark_sheet(self, group: int, column: str, **fields) -> None:
        """Write e.g. Verified = Yes in the Verification ``group`` (1/2) block of this device's sheet row."""
        mac = self.device_mac
        if not mac:
            return
        tab = self.app.generate_view.current_tab or "?"
        column = f"Verification {group} › {column}"
        self.sheet_lbl.configure(text=f"Sheet: writing {column}…", style="Hint.TLabel")
        job_id = self.job.job_id if self.job else None

        def done(record, err):
            if err:
                self.sheet_lbl.configure(text=f"✖  Sheet NOT updated ({column}): {err}", bootstyle="danger")
                self.console.write(f"Sheet update failed ({column}): {err}", "error")
                return
            self.sheet_lbl.configure(text=f"✔  Sheet tab '{tab}' row {record.row}:  {record.status}", bootstyle="success")
            shown = ", ".join(f"{k} = {('Yes' if v else 'No') if isinstance(v, bool) else v}" for k, v in fields.items())
            self.console.write(f"Sheet row {record.row}: {column} ({shown})", "success")
            if mac == self.device_mac:
                self.record = record
            if job_id and self.job and self.job.job_id == job_id and self.app.station and "nfc" in fields:
                self.app.station.send_nfc_verdict(self.job.verdict(f"Saved to sheet row {record.row}"))
            self.app.generate_view.refresh()

        self.app.run_sheet(lambda client: client.update_status(mac, group, **fields), done)

    # ------------------------------------------------------------- preview ---

    def _set_preview(self, image: Image.Image) -> None:
        img = image.copy()
        img.thumbnail((150, 190), Image.LANCZOS)
        self._preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self._preview_img, text="")

    def _clear_preview(self, text: str) -> None:
        self._preview_img = None
        self.preview.configure(image="", text=text)
