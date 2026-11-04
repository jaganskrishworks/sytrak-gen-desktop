"""Verify QR tab (device first).

  1. Read the MAC from the device over J-Link
  2. Look it up in the Google Sheet -> SEQ (shown)
  3. Send SEQ + MAC to the paired mobile; it scans the sticker and confirms
  4. Sticker MAC == device MAC == sheet  -> verified -> Write to NFC
"""
from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from PIL import Image, ImageTk
from ttkbootstrap.dialogs import Messagebox

from ...core.qr import build_payload, compact_mac, format_seq, parse_payload, render_label
from ...core.sheets import SheetRecord
from ..theme import BANNER_FONT, LABEL_BOLD, PAD
from ..widgets import CheckRow, Console

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)


def nfc_result_check(data: dict, expected: str) -> tuple[bool, str]:
    """Accept an NFC result only if the phone read back exactly what we asked it to write."""
    if not data.get("ok"):
        return False, str(data.get("error") or "unknown error")
    read_back = data.get("read_back")
    if read_back is None:
        return False, "phone did not read the tag back - update the mobile app"
    if str(read_back).strip() != expected:
        return False, f"verify failed - tag reads '{read_back}', expected '{expected}'"
    return True, ""


def nfc_read_check(data: dict, expected: str) -> tuple[bool, str]:
    """Accept a tag read only if the phone read exactly the device MAC (no colons)."""
    if not data.get("ok"):
        return False, str(data.get("error") or "could not read the tag")
    text = str(data.get("text", "")).strip()
    if not text:
        return False, "tag is empty"
    if text != expected:
        return False, f"tag reads '{text}', expected '{expected}'"
    return True, text


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
        self._reset_state()
        self._build()
        self._refresh_mobiles()
        self._update_verdict()

    def _reset_state(self) -> None:
        self.device_mac: str | None = None   # read over J-Link
        self.record: SheetRecord | None = None
        self.request_sent = False
        self.raw_qr = ""                      # last sticker scan from the mobile
        self.manual = False
        self.verified_saved = False
        self.nfc_pending = False
        self.nfc_done = False
        self.nfc_read_pending = False
        self.nfc_read_ok = False

    # ------------------------------------------------------------- layout ---

    def _build(self) -> None:
        self.columnconfigure(0, weight=2, uniform="v")
        self.columnconfigure(1, weight=3, uniform="v")
        self.rowconfigure(2, weight=1, minsize=130)

        conn = ttk.Labelframe(self, text=" Mobile connection ", padding=PAD)
        conn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, PAD))
        conn.columnconfigure(5, weight=1)
        ttk.Label(conn, text="Mobile", font=LABEL_BOLD).grid(row=0, column=0, sticky="w")
        self.combo = ttk.Combobox(conn, textvariable=self.mobile_var, state="readonly", width=44)
        self.combo.grid(row=0, column=1, padx=(10, 8))
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self._update_buttons())
        self.connect_btn = ttk.Button(conn, text="Connect", bootstyle="success", command=self._connect, width=12)
        self.connect_btn.grid(row=0, column=2, padx=(0, 6))
        self.disconnect_btn = ttk.Button(conn, text="Disconnect", bootstyle="danger-outline", command=self._disconnect, width=12)
        self.disconnect_btn.grid(row=0, column=3)
        self.conn_status = ttk.Label(conn, text="", font=LABEL_BOLD)
        self.conn_status.grid(row=0, column=4, sticky="w", padx=(16, 0))
        self.station_hint = ttk.Label(conn, text="", style="Hint.TLabel", wraplength=1150)
        self.station_hint.grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))

        # --- left: the device on the J-Link ---
        left = ttk.Labelframe(self, text=" Device ", padding=PAD)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, PAD), pady=(0, PAD))
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
        ttk.Label(tabrow, text="The device is looked up (SEQ, duplicates) and marked Verified / NFC in this tab",
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

        # --- right: checks / verdict / NFC ---
        right = ttk.Labelframe(self, text=" Verification ", padding=PAD)
        right.grid(row=1, column=1, sticky="nsew", pady=(0, PAD))
        right.columnconfigure(0, weight=1)

        # Mode switch, top right: Verification 1 (write + verify NFC) | Verification 2 (read only).
        modebar = ttk.Frame(right)
        modebar.grid(row=0, column=0, sticky="e", pady=(0, 6))
        self.mode_var = tk.IntVar(value=2 if self.app.settings.qr.verify_mode == 2 else 1)
        for value, text in ((1, "Verification 1  ·  Write + Read"), (2, "Verification 2  ·  Read only")):
            ttk.Radiobutton(modebar, text=text, value=value, variable=self.mode_var, bootstyle="primary-toolbutton",
                            command=self._on_mode_changed, padding=(14, 6)).pack(side="left")

        self.chk_device = CheckRow(right, "Device MAC  (J-Link)")
        self.chk_device.grid(row=1, column=0, sticky="ew")
        self.chk_store = CheckRow(right, "Stored data  (Google Sheet → SEQ)")
        self.chk_store.grid(row=2, column=0, sticky="ew")
        self.chk_sticker = CheckRow(right, "Sticker QR scanned on mobile  =  device MAC")
        self.chk_sticker.grid(row=3, column=0, sticky="ew")

        self.banner = ttk.Label(right, text="", font=BANNER_FONT, anchor="center", padding=12)
        self.banner.grid(row=4, column=0, sticky="ew", pady=(10, 10))

        btns = ttk.Frame(right)
        btns.grid(row=5, column=0, sticky="ew")
        btns.columnconfigure(0, weight=1, uniform="b")
        btns.columnconfigure(1, weight=2, uniform="b")
        self.manual_btn = ttk.Button(btns, text="Verified Manually", bootstyle="warning-outline", command=self._verify_manually)
        self.manual_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=5)
        # One NFC button; what it does follows the Verification 1 / 2 switch.
        self.nfc_btn = ttk.Button(btns, text="", command=self._nfc_action)
        self.nfc_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0), ipady=5)
        self.nfc_lbl = ttk.Label(right, text="", font=LABEL_BOLD, wraplength=560)
        self.nfc_lbl.grid(row=6, column=0, sticky="w", pady=(10, 0))
        self.nfc_read_lbl = ttk.Label(right, text="", font=LABEL_BOLD, wraplength=560)
        self.nfc_read_lbl.grid(row=7, column=0, sticky="w", pady=(4, 0))
        self.sheet_lbl = ttk.Label(right, text="", style="Hint.TLabel", wraplength=560)
        self.sheet_lbl.grid(row=8, column=0, sticky="w", pady=(4, 0))

        logbox = ttk.Labelframe(self, text=" Activity ", padding=(PAD, 4, PAD, PAD))
        logbox.grid(row=2, column=0, columnspan=2, sticky="nsew")
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.console = Console(logbox, height=5)
        self.console.grid(row=0, column=0, sticky="nsew")

        for chk in (self.chk_device, self.chk_store, self.chk_sticker):
            chk.set("pending", "—")

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
            if mid == self.paired and self.record and not self.raw_qr:
                self._send_to_mobile(quiet=True)  # reconnecting mobile gets the pending request again
        elif kind == "mobile_disconnected":
            if mid == self.paired and mid in self.mobiles:
                self.mobiles[mid].online = False
                self.console.write(f"Paired mobile went offline: {mid}", "warning")
            else:
                self.mobiles.pop(mid, None)
                self.console.write(f"Mobile disconnected: {mid}", "muted")
        elif kind == "scan":
            self._on_scan(data.get("qr", ""), mid)
            return
        elif kind == "scan_rejected":
            self.console.write(f"Ignored scan from unpaired mobile {mid}", "warning")
            return
        elif kind == "nfc_result":
            self._on_nfc_result(data)
            return
        elif kind == "nfc_read_result":
            self._on_nfc_read_result(data)
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
        self.app.station.pair(mid)
        self.paired = mid
        self.console.write(f"Connected to mobile {mid}", "success")
        self._update_buttons()
        if self.record and not self.raw_qr:
            self._send_to_mobile()

    def _disconnect(self) -> None:
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
        for lbl in (self.nfc_lbl, self.nfc_read_lbl, self.sheet_lbl, self.dev_meta):
            lbl.configure(text="")
        self.dev_mac_lbl.configure(text="—")
        self.dev_seq_lbl.configure(text="—")
        self._clear_preview("Reading…")
        self.chk_device.set("busy", "Reading MAC over J-Link…")
        self.chk_store.set("pending", "—")
        self.chk_sticker.set("pending", "—")
        self.read_btn.configure(state="disabled", text="Reading MAC…")
        self._update_verdict()
        self.app.probe_bar.read_mac(self._on_device_mac)

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
        self.record = None
        self.request_sent = False
        self.raw_qr = ""
        self.manual = self.verified_saved = self.nfc_pending = self.nfc_done = False
        self.nfc_read_pending = self.nfc_read_ok = False
        for lbl in (self.nfc_lbl, self.nfc_read_lbl, self.sheet_lbl, self.dev_meta):
            lbl.configure(text="")
        self.dev_seq_lbl.configure(text="—")
        self.chk_sticker.set("pending", "—")
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
                    if self._paired_online():
                        self._send_to_mobile()
            self._update_verdict()

        self.app.run_sheet(lambda client: client.audit(), done)

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
        if auto and self.record and not self.verified_saved:
            self.verified_saved = True
            self.console.write(f"SEQ {self.record.seq_text} verified: device, sheet and sticker match", "success")
            self._mark_sheet("Verified", verified=True)

        if self.nfc_done and self.nfc_read_ok:
            self.banner.configure(text="✔  NFC WRITTEN AND VERIFIED", bootstyle="inverse-success")
        elif self.nfc_read_ok:
            self.banner.configure(text="✔  NFC TAG VERIFIED — MATCHES DEVICE", bootstyle="inverse-success")
        elif self.nfc_done:
            self.banner.configure(text="✔  NFC WRITTEN — tap the tag again to verify", bootstyle="inverse-info")
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

        self.send_btn.configure(state="normal" if self.record and self._paired_online() and not self.nfc_done else "disabled")
        self.send_btn.configure(text="②  Send again to mobile" if self.request_sent else "②  Send to mobile for sticker check")
        nfc_busy = self.nfc_pending or self.nfc_read_pending
        if self.mode_var.get() == 1:   # Verification 1: write the tag, then read it back to verify
            enabled = (auto or self.manual) and self._paired_online() and not nfc_busy and not self.nfc_done
            self.nfc_btn.configure(text="①  Write + Verify NFC", bootstyle="success")
        else:                          # Verification 2: read the existing tag only
            enabled = self.device_mac is not None and self._paired_online() and not nfc_busy
            self.nfc_btn.configure(text="②  Verify NFC  (read only)", bootstyle="info")
        self.nfc_btn.configure(state="normal" if enabled else "disabled")
        busy = "busy" in (self.chk_device.state, self.chk_store.state)
        can_manual = self.device_mac is not None and not auto and not self.manual and not busy and not self.nfc_done
        self.manual_btn.configure(state="normal" if can_manual else "disabled")

    def _verify_manually(self) -> None:
        answer = Messagebox.yesno(
            "The automatic checks did not all pass.\n\nConfirm you have checked this device manually?",
            "Verify manually", parent=self)
        if answer not in ("Yes", "yes", True):
            return
        self.manual = True
        self.console.write(f"MAC {compact_mac(self.device_mac or '')} verified manually", "warning")
        if self.record:
            self._mark_sheet("Manually Verified", manually_verified=True)
        else:
            self.sheet_lbl.configure(text="Sheet: not updated - this MAC has no row in the selected tab", bootstyle="warning")
        self._update_verdict()

    # ------------------------------------------------------------- NFC ---

    def _write_nfc(self) -> None:
        if not (self.app.station and self.device_mac):
            return
        seq_text = self.record.seq_text if self.record else ""
        mac = compact_mac(self.device_mac)
        # The tag gets the same content as the QR code: the MAC without colons.
        if not self.app.station.request_nfc_write(seq_text, mac, build_payload(self.device_mac)):
            Messagebox.show_warning("Connect a mobile first.", "Write to NFC", parent=self)
            return
        self.nfc_pending = True
        self.nfc_lbl.configure(text="⏳  Sent to mobile - tap the NFC tag and press WRITE on the phone…", bootstyle="info")
        self.console.write(f"NFC write requested for MAC {mac}" + (f" (SEQ {seq_text})" if seq_text else ""), "accent")
        self._update_verdict()

    def _on_nfc_result(self, data: dict) -> None:
        if data.get("id") != self.paired or not self.nfc_pending:
            return
        self.nfc_pending = False
        ok, err = nfc_result_check(data, build_payload(self.device_mac or ""))
        if ok:
            self.nfc_done = True
            self.nfc_lbl.configure(text=f"✔  NFC tag written  (read back {data.get('read_back')})", bootstyle="success")
            self.console.write(f"NFC written: tag reads {data.get('read_back')}", "success")
            if self.record:
                self._mark_sheet("NFC Written", nfc_written=True)
            # Write + Verify: ask for a separate read of the tag to verify it.
            self._read_nfc(after_write=True)
        else:
            self.nfc_lbl.configure(text=f"✖  NFC NOT OK: {err}  - press Write + Verify NFC again", bootstyle="danger")
            self.console.write(f"NFC write failed: {err}", "error")
        self._update_verdict()

    def _read_nfc(self, after_write: bool = False) -> None:
        """Ask the phone to read the tag and check it holds this device's MAC (no writing)."""
        if not (self.app.station and self.device_mac):
            return
        expected = build_payload(self.device_mac)
        seq_text = self.record.seq_text if self.record else ""
        if not self.app.station.request_nfc_read(seq_text, compact_mac(self.device_mac), expected):
            Messagebox.show_warning("Connect a mobile first.", "Verify NFC", parent=self)
            return
        self.nfc_read_pending = True
        self.nfc_read_ok = False
        step = "Verify: tap the tag again on the phone" if after_write else "Tap the NFC tag on the phone"
        self.nfc_read_lbl.configure(text=f"⏳  {step} - reading it to verify…", bootstyle="info")
        self.console.write(f"NFC read requested to verify MAC {expected}", "accent")
        self._update_verdict()

    def _on_nfc_read_result(self, data: dict) -> None:
        if data.get("id") != self.paired:
            return
        if not self.device_mac:
            self.console.write(f"NFC tag read on the phone ({data.get('text', '')}) - read a device first to verify it", "warning")
            return
        self.nfc_read_pending = False
        ok, detail = nfc_read_check(data, build_payload(self.device_mac))
        if ok:
            self.nfc_read_ok = True
            self.nfc_read_lbl.configure(text=f"✔  NFC tag verified: reads {detail}  =  device MAC", bootstyle="success")
            self.console.write(f"NFC tag verified: {detail}", "success")
            self.app.set_status(f"MAC {compact_mac(self.device_mac)}: NFC tag verified")
            if self.record:
                self._mark_sheet("NFC Verified", nfc_verified=True)
        else:
            self.nfc_read_ok = False
            self.nfc_read_lbl.configure(text=f"✖  NFC tag NOT verified: {detail}", bootstyle="danger")
            self.console.write(f"NFC verify failed: {detail}", "error")
        self._update_verdict()

    def _nfc_action(self) -> None:
        if self.mode_var.get() == 1:
            self._write_nfc()   # then reads the tag back automatically (see _on_nfc_result)
        else:
            self._read_nfc()

    def _on_mode_changed(self) -> None:
        mode = self.mode_var.get()
        self.app.settings.qr.verify_mode = mode
        self.app.save_settings()
        self.console.write("Verification 1: write + read the NFC tag" if mode == 1
                           else "Verification 2: read the NFC tag only (nothing is written)", "muted")
        self._update_verdict()

    # -------------------------------------------------------- sheet status ---

    def _mark_sheet(self, column: str, **flags: bool) -> None:
        """Write Verified / Manually Verified / NFC Written = Yes on this device's sheet row."""
        mac = self.device_mac
        if not mac:
            return
        tab = self.app.generate_view.current_tab or "?"
        self.sheet_lbl.configure(text=f"Sheet: writing {column} = Yes…", style="Hint.TLabel")

        def done(record, err):
            if err:
                self.sheet_lbl.configure(text=f"✖  Sheet NOT updated ({column}): {err}", bootstyle="danger")
                self.console.write(f"Sheet update failed ({column}): {err}", "error")
                return
            self.sheet_lbl.configure(text=f"✔  Sheet tab '{tab}' row {record.row}:  {record.status}", bootstyle="success")
            self.console.write(f"Sheet row {record.row}: {column} = Yes", "success")
            if mac == self.device_mac:
                self.record = record
            self.app.generate_view.refresh()

        self.app.run_sheet(lambda client: client.update_status(mac, **flags), done)

    # ------------------------------------------------------------- preview ---

    def _set_preview(self, image: Image.Image) -> None:
        img = image.copy()
        img.thumbnail((150, 190), Image.LANCZOS)
        self._preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self._preview_img, text="")

    def _clear_preview(self, text: str) -> None:
        self._preview_img = None
        self.preview.configure(image="", text=text)
