"""Verify QR tab: pair a mobile, receive scanned QR, verify, write NFC."""
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
        self.raw_qr = ""
        self.qr_mac: str | None = None
        self.qr_seq: int | None = None
        self.jlink_mac: str | None = None
        self.record: SheetRecord | None = None
        self.manual = False
        self.verified_saved = False
        self.nfc_pending = False
        self.nfc_done = False

    # ------------------------------------------------------------- layout ---

    def _build(self) -> None:
        self.columnconfigure(0, weight=2, uniform="v")
        self.columnconfigure(1, weight=3, uniform="v")
        self.rowconfigure(2, weight=1, minsize=150)

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

        left = ttk.Labelframe(self, text=" Received from mobile ", padding=PAD)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, PAD), pady=(0, PAD))
        left.columnconfigure(1, weight=1)
        self.preview = ttk.Label(left, text="Waiting for a scan from the mobile…", style="Hint.TLabel", anchor="center")
        self.preview.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 8))
        left.rowconfigure(0, weight=1)
        ttk.Label(left, text="MAC", style="Hint.TLabel").grid(row=1, column=0, sticky="w")
        self.rx_mac = ttk.Label(left, text="—", font=("Consolas", 14, "bold"))
        self.rx_mac.grid(row=1, column=1, sticky="w", padx=(10, 0))
        ttk.Label(left, text="SEQ", style="Hint.TLabel").grid(row=2, column=0, sticky="w")
        self.rx_seq = ttk.Label(left, text="—", font=("Segoe UI", 14, "bold"))
        self.rx_seq.grid(row=2, column=1, sticky="w", padx=(10, 0))
        self.rx_meta = ttk.Label(left, text="", style="Hint.TLabel", wraplength=380)
        self.rx_meta.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        right = ttk.Labelframe(self, text=" Verification ", padding=PAD)
        right.grid(row=1, column=1, sticky="nsew", pady=(0, PAD))
        right.columnconfigure(0, weight=1)
        self.chk_qr = CheckRow(right, "QR received  (MAC)")
        self.chk_qr.grid(row=0, column=0, sticky="ew")
        jl = ttk.Frame(right)
        jl.grid(row=1, column=0, sticky="ew")
        jl.columnconfigure(0, weight=1)
        self.chk_jlink = CheckRow(jl, "J-Link MAC address matches QR")
        self.chk_jlink.grid(row=0, column=0, sticky="ew")
        self.reread_btn = ttk.Button(jl, text="Read again", bootstyle="primary-link", command=self._read_jlink)
        self.reread_btn.grid(row=0, column=1, sticky="ne")
        self.chk_store = CheckRow(right, "Verified with stored data  (Google Sheet → SEQ)")
        self.chk_store.grid(row=2, column=0, sticky="ew")

        self.banner = ttk.Label(right, text="", font=BANNER_FONT, anchor="center", padding=12)
        self.banner.grid(row=3, column=0, sticky="ew", pady=(10, 10))

        btns = ttk.Frame(right)
        btns.grid(row=4, column=0, sticky="ew")
        btns.columnconfigure((0, 1), weight=1, uniform="b")
        self.manual_btn = ttk.Button(btns, text="Verified Manually", bootstyle="warning-outline", command=self._verify_manually)
        self.manual_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=5)
        self.nfc_btn = ttk.Button(btns, text="Write to NFC", bootstyle="success", command=self._write_nfc)
        self.nfc_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0), ipady=5)
        self.nfc_lbl = ttk.Label(right, text="", font=LABEL_BOLD)
        self.nfc_lbl.grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.sheet_lbl = ttk.Label(right, text="", style="Hint.TLabel", wraplength=560)
        self.sheet_lbl.grid(row=6, column=0, sticky="w", pady=(4, 0))

        logbox = ttk.Labelframe(self, text=" Activity ", padding=(PAD, 4, PAD, PAD))
        logbox.grid(row=2, column=0, columnspan=2, sticky="nsew")
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.console = Console(logbox, height=5)
        self.console.grid(row=0, column=0, sticky="nsew")

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

    # -------------------------------------------------------------- verify ---

    def _on_scan(self, qr: str, mobile_id: str) -> None:
        self._reset_state()
        self.raw_qr = qr
        self.console.write(f"Scan received from {mobile_id}: {qr}", "accent")
        self.rx_meta.configure(text=f"Raw: {qr}")
        self.nfc_lbl.configure(text="")
        self.sheet_lbl.configure(text="")
        parsed = parse_payload(qr)
        if not parsed:
            self.rx_mac.configure(text="—")
            self.rx_seq.configure(text="—")
            self._clear_preview("Unrecognised QR code")
            self.chk_qr.set("fail", "QR content is not a SyTrak code")
            self.chk_jlink.set("pending", "—")
            self.chk_store.set("pending", "—")
            self._update_verdict()
            return

        self.qr_mac, self.qr_seq = parsed  # current QR codes carry only the MAC
        self.rx_mac.configure(text=compact_mac(self.qr_mac))
        self.rx_seq.configure(text=format_seq(self.qr_seq) if self.qr_seq is not None else "…")
        self._set_preview(render_label(qr, self.qr_mac, self.qr_seq))
        self.chk_qr.set("ok", f"MAC {compact_mac(self.qr_mac)}")

        self._check_sheet()
        self._read_jlink()

    def _check_sheet(self) -> None:
        """Find the scanned MAC in the selected sheet tab (gives the SEQ)."""
        mac, raw = self.qr_mac, self.raw_qr
        tab = self.app.generate_view.current_tab or "?"
        self.chk_store.set("busy", f"Looking up sheet tab '{tab}'…")

        def done(records, err):
            if raw != self.raw_qr:
                return  # a newer scan arrived meanwhile
            if err:
                self.chk_store.set("fail", f"Cannot read Google Sheet: {err}")
                self._update_verdict()
                return
            self.record = next((r for r in records if r.mac == mac), None)
            if self.record is None:
                self.chk_store.set("fail", f"MAC {compact_mac(mac)} is not in sheet tab '{tab}'")
                self.rx_seq.configure(text="—")
            elif self.qr_seq is not None and self.qr_seq != self.record.seq:
                self.chk_store.set("fail", f"QR says SEQ {format_seq(self.qr_seq)}, sheet has SEQ {self.record.seq_text}")
            else:
                self.qr_seq = self.record.seq
                self.rx_seq.configure(text=self.record.seq_text)
                self._set_preview(render_label(raw, mac, self.record.seq))
                self.chk_store.set("ok", f"Tab '{tab}': SEQ {self.record.seq_text}"
                                         f"{' · ' + self.record.device_type if self.record.device_type else ''}"
                                         f" · saved {self.record.created_at}")
            self._update_verdict()

        self.app.run_sheet(lambda client: client.records(), done)

    def _read_jlink(self) -> None:
        if self.qr_mac is None:
            return
        self.chk_jlink.set("busy", "Reading MAC over J-Link…")
        self.reread_btn.configure(state="disabled")
        self._update_verdict()
        self.app.probe_bar.read_mac(self._on_jlink_mac)

    def _on_jlink_mac(self, mac: str | None, err: str) -> None:
        self.reread_btn.configure(state="normal")
        self.jlink_mac = mac
        if not mac:
            self.chk_jlink.set("fail", f"Could not read: {err}")
        elif mac == self.qr_mac:
            self.chk_jlink.set("ok", f"{compact_mac(mac)}  =  QR MAC")
        else:
            self.chk_jlink.set("fail", f"J-Link {compact_mac(mac)}  ≠  QR {compact_mac(self.qr_mac or '')}")
        self._update_verdict()

    def _auto_verified(self) -> bool:
        return all(c.state == "ok" for c in (self.chk_qr, self.chk_jlink, self.chk_store))

    def _update_verdict(self) -> None:
        auto = self._auto_verified()
        if auto and self.record and not self.verified_saved:
            self.verified_saved = True
            self.console.write(f"SEQ {self.record.seq_text} verified", "success")
            self._mark_sheet("Verified", verified=True)

        if self.nfc_done:
            self.banner.configure(text="✔  NFC WRITTEN", bootstyle="inverse-success")
        elif auto:
            self.banner.configure(text="✔  BOTH ARE SAME — VERIFIED", bootstyle="inverse-success")
        elif self.manual:
            self.banner.configure(text="✔  VERIFIED MANUALLY", bootstyle="inverse-warning")
        elif any(c.state == "fail" for c in (self.chk_qr, self.chk_jlink, self.chk_store)):
            self.banner.configure(text="✖  NOT VERIFIED — MISMATCH", bootstyle="inverse-danger")
        elif self.qr_mac is None and not self.raw_qr:
            self.banner.configure(text="Waiting for scan…", bootstyle="inverse-light")
        else:
            self.banner.configure(text="Verifying…", bootstyle="inverse-info")

        can_write = (auto or self.manual) and self._paired_online() and not self.nfc_pending and not self.nfc_done
        self.nfc_btn.configure(state="normal" if can_write else "disabled")
        can_manual = bool(self.raw_qr) and self.qr_mac is not None and not auto and not self.manual \
            and "busy" not in (self.chk_jlink.state, self.chk_store.state)
        self.manual_btn.configure(state="normal" if can_manual else "disabled")

    def _verify_manually(self) -> None:
        answer = Messagebox.yesno(
            "The automatic checks did not all pass.\n\nConfirm you have checked this device manually?",
            "Verify manually", parent=self)
        if answer not in ("Yes", "yes", True):
            return
        self.manual = True
        self.console.write(f"MAC {compact_mac(self.qr_mac or '')} verified manually", "warning")
        if self.record:
            self._mark_sheet("Manually Verified", manually_verified=True)
        else:
            self.sheet_lbl.configure(text="Sheet: not updated - this MAC has no row in the selected tab", bootstyle="warning")
        self._update_verdict()

    def _write_nfc(self) -> None:
        if not (self.app.station and self.qr_mac):
            return
        seq_text = format_seq(self.qr_seq) if self.qr_seq is not None else ""
        mac = compact_mac(self.qr_mac)
        # The tag gets the same content as the QR code: the MAC without colons.
        if not self.app.station.request_nfc_write(seq_text, mac, build_payload(self.qr_mac)):
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
        ok, err = nfc_result_check(data, build_payload(self.qr_mac or ""))
        if ok:
            self.nfc_done = True
            self.nfc_lbl.configure(text=f"✔  NFC tag written and verified  (read back {data.get('read_back')})", bootstyle="success")
            self.console.write(f"NFC written + verified: tag reads {data.get('read_back')}", "success")
            self.app.set_status(f"MAC {compact_mac(self.qr_mac or '')}: NFC written and verified")
            if self.record:
                self._mark_sheet("NFC Written", nfc_written=True)
        else:
            self.nfc_lbl.configure(text=f"✖  NFC NOT OK: {err}  - press Write to NFC again", bootstyle="danger")
            self.console.write(f"NFC write failed: {err}", "error")
        self._update_verdict()

    # -------------------------------------------------------- sheet status ---

    def _mark_sheet(self, column: str, **flags: bool) -> None:
        """Write Verified / Manually Verified / NFC Written = Yes on this device's sheet row."""
        mac = self.qr_mac
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
            if mac == self.qr_mac:
                self.record = record
            self.app.generate_view.refresh()

        self.app.run_sheet(lambda client: client.update_status(mac, **flags), done)

    # ------------------------------------------------------------- preview ---

    def _set_preview(self, image: Image.Image) -> None:
        img = image.copy()
        img.thumbnail((170, 220), Image.LANCZOS)
        self._preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self._preview_img, text="")

    def _clear_preview(self, text: str) -> None:
        self._preview_img = None
        self.preview.configure(image="", text=text)
