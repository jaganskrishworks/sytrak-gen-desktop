"""Generate QR tab: read MAC -> generate QR with 4-digit sequence -> save to Google Sheet."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from PIL import Image, ImageTk
from ttkbootstrap.dialogs import Messagebox, Querybox

from ...core import pdf
from ...core.qr import MAX_SEQ, build_payload, compact_mac, format_seq, render_label, thumbnail
from ...core.sheets import SheetRecord, next_seq
from ..theme import LABEL_BOLD, PAD

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)

PREVIEW_SIZE = 230
LIST_LIMIT = 5000


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class GenerateView(ttk.Frame):
    def __init__(self, master, app: "MainWindow"):
        super().__init__(master, padding=PAD)
        self.app = app
        self.mac: str | None = None
        self.records: list[SheetRecord] = []
        self._pending: tuple[int, Image.Image] | None = None
        self._busy = False
        self._preview_img: ImageTk.PhotoImage | None = None
        self._thumbs: dict[tuple[int, str], ImageTk.PhotoImage] = {}
        self._checked: set[tuple[int, str]] = set()  # (SEQ, MAC) ticked for the PDF
        self._by_iid: dict[str, SheetRecord] = {}
        self._generator_visible = True
        self._build()
        self.load_tabs()

    # ------------------------------------------------------------- layout ---

    def _build(self) -> None:
        self.columnconfigure(0, weight=1, uniform="top")
        self.columnconfigure(1, weight=1, uniform="top")
        self.rowconfigure(2, weight=1, minsize=240)

        tabbar = ttk.Frame(self)
        tabbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, PAD))
        ttk.Label(tabbar, text="Sheet tab", font=LABEL_BOLD).pack(side="left")
        self.tab_var = tk.StringVar(value=self.app.settings.sheet.tab)
        self.tab_combo = ttk.Combobox(tabbar, textvariable=self.tab_var, state="readonly", width=32)
        self.tab_combo.pack(side="left", padx=(10, 6))
        self.tab_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_tab_selected())
        ttk.Button(tabbar, text="⟳", width=3, bootstyle="primary-outline", command=self.load_tabs).pack(side="left")
        self.new_tab_btn = ttk.Button(tabbar, text="+ New tab", bootstyle="primary-outline", command=self._new_tab)
        self.new_tab_btn.pack(side="left", padx=(6, 0))
        self.tab_lbl = ttk.Label(tabbar, text="", font=LABEL_BOLD)
        self.tab_lbl.pack(side="left", padx=(16, 0))

        self.gen_left = left = ttk.Labelframe(self, text=" Device ", padding=(PAD + 6, PAD))
        left.grid(row=1, column=0, sticky="nsew", padx=(0, PAD), pady=(0, PAD))
        left.columnconfigure(0, weight=1)
        self.read_btn = ttk.Button(left, text="Read MAC Address", bootstyle="primary", command=self._read_mac)
        self.read_btn.grid(row=0, column=0, sticky="ew", ipady=6)
        ttk.Label(left, text="MAC ADDRESS", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.mac_lbl = ttk.Label(left, text="--:--:--:--:--:--", style="Mac.TLabel")
        self.mac_lbl.grid(row=2, column=0, sticky="w")
        self.info_lbl = ttk.Label(left, text="Connect the device to the J-Link and read its MAC.", style="Hint.TLabel", wraplength=440)
        self.info_lbl.grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Separator(left).grid(row=4, column=0, sticky="ew", pady=10)
        meta = ttk.Frame(left)
        meta.grid(row=5, column=0, rowspan=2, sticky="ew")
        meta.columnconfigure(1, weight=1)
        ttk.Label(meta, text="NEXT SEQUENCE", style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        self.seq_lbl = ttk.Label(meta, text="----", style="Seq.TLabel")
        self.seq_lbl.grid(row=1, column=0, sticky="w", padx=(0, 24))
        ttk.Label(meta, text="DEVICE TYPE  (required)", style="Hint.TLabel").grid(row=0, column=1, sticky="w")
        self.type_var = tk.StringVar(value=self.app.settings.qr.device_type)
        self.type_combo = ttk.Combobox(meta, textvariable=self.type_var, state="readonly",
                                       values=self.app.settings.qr.device_type_list, font=LABEL_BOLD)
        self.type_combo.grid(row=1, column=1, sticky="ew")
        self.type_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_type_selected())
        btns = ttk.Frame(left)
        btns.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        btns.columnconfigure((0, 1), weight=1, uniform="b")
        self.gen_btn = ttk.Button(btns, text="Generate", bootstyle="success", command=self._generate, state="disabled")
        self.gen_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=4)
        self.save_btn = ttk.Button(btns, text="Save", bootstyle="info", command=self._save, state="disabled")
        self.save_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0), ipady=4)

        self.gen_right = right = ttk.Labelframe(self, text=" QR Code ", padding=PAD)
        right.grid(row=1, column=1, sticky="nsew", pady=(0, PAD))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.preview = ttk.Label(right, text="QR preview appears here", style="Hint.TLabel", anchor="center")
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview_caption = ttk.Label(right, text="", font=LABEL_BOLD, anchor="center")
        self.preview_caption.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        self.bottom = bottom = ttk.Labelframe(self, text=" Saved QR codes (Google Sheet) ", padding=PAD)
        bottom.grid(row=2, column=0, columnspan=2, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(2, weight=1)
        bar = ttk.Frame(bottom)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.toggle_btn = ttk.Button(bar, text="▲  Hide generator", bootstyle="primary-outline", command=self._toggle_generator)
        self.toggle_btn.pack(side="left")
        self.select_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Select all", variable=self.select_all_var, bootstyle="primary",
                        command=self._on_select_all).pack(side="left", padx=(16, 0))
        self.sel_count_lbl = ttk.Label(bar, text="0 selected", font=LABEL_BOLD)
        self.sel_count_lbl.pack(side="left", padx=(10, 16))
        self.sheet_lbl = ttk.Label(bar, text="Loading Google Sheet…", style="Hint.TLabel")
        self.sheet_lbl.pack(side="left")
        ttk.Button(bar, text="Open Google Sheet", bootstyle="primary-link", command=self._open_sheet).pack(side="right")
        self.refresh_btn = ttk.Button(bar, text="⟳ Refresh", bootstyle="primary-link", command=self.refresh)
        self.refresh_btn.pack(side="right")

        pdf_bar = ttk.Frame(bottom)
        pdf_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(pdf_bar, text="A4 label sheet", font=LABEL_BOLD).pack(side="left")
        self.grid_hint = ttk.Label(pdf_bar, text="", style="Hint.TLabel")
        self.grid_hint.pack(side="left", padx=(12, 0))
        self.pdf_btn = ttk.Button(pdf_bar, text="⬇  Generate PDF", bootstyle="success", command=self._generate_pdf, state="disabled")
        self.pdf_btn.pack(side="right", ipadx=10)

        cols = (("datetime", "Date & Time", 170), ("seq", "Seq", 70), ("mac", "MAC Address", 190),
                ("type", "Device Type", 130), ("status", "Status", 220))
        self.tree = ttk.Treeview(bottom, columns=[c[0] for c in cols], show="tree headings", style="Qr.Treeview", selectmode="browse")
        self.tree.heading("#0", text="☐   QR Code", anchor="w", command=self._toggle_all_from_heading)
        self.tree.column("#0", width=130, stretch=False, anchor="w")
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=key == "status")
        scroll = ttk.Scrollbar(bottom, orient="vertical", command=self.tree.yview, bootstyle="round")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=2, column=0, sticky="nsew")
        scroll.grid(row=2, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<space>", lambda _e: self._toggle_focused())
        self._update_grid_hint()

    # --------------------------------------------------------------- tabs ---

    @property
    def current_tab(self) -> str:
        return self.tab_var.get()

    def load_tabs(self) -> None:
        """Fill the tab dropdown, resolve the active tab, then load its records."""
        def work(client):
            tabs = client.tabs()
            try:
                return tabs, client.tab_title(), None
            except Exception as exc:  # configured tab was renamed / deleted
                return tabs, None, str(exc)

        def done(result, err):
            if err:
                self._sheet_status(f"✖  Google Sheet: {err}", "danger")
                self._show_tab(None)
                return
            tabs, current, tab_err = result
            self._set_tab_values(tabs)
            if tab_err:
                self.tab_var.set("")
                self._show_tab(None)
                self._sheet_status(f"✖  {tab_err} - choose a tab", "danger")
                return
            self.tab_var.set(current)
            self._show_tab(current)
            self.refresh()

        self.app.run_sheet(work, done)

    def _set_tab_values(self, tabs: list[str]) -> None:
        """Tab list for this dropdown and the shared one on the Verify tab."""
        self.tab_combo.configure(values=tabs)
        verify = getattr(self.app, "verify_view", None)
        if verify is not None:
            verify.tab_combo.configure(values=tabs)

    def _show_tab(self, tab: str | None) -> None:
        if tab:
            self.tab_lbl.configure(text=f"●  Saving to tab:  {tab}", bootstyle="success")
            self.bottom.configure(text=f" Saved QR codes  ·  tab '{tab}' ")
        else:
            self.tab_lbl.configure(text="●  No tab selected", bootstyle="danger")
            self.bottom.configure(text=" Saved QR codes (Google Sheet) ")
        self.app.set_tab_status(tab)

    def _on_tab_selected(self) -> None:
        tab = self.tab_var.get()
        if tab == self.app.settings.sheet.tab:
            return
        self.app.settings.sheet.tab = tab
        self.app.save_settings()
        self._pending = None  # a preview's SEQ belonged to the previous tab
        self.save_btn.configure(state="disabled")
        self.gen_btn.configure(state="normal" if self.mac else "disabled")
        self._clear_preview()
        self.records = []
        self._fill_list()
        self._show_tab(tab)
        self.app.set_status(f"Now using sheet tab '{tab}'")
        self.refresh(then=self._check_mac if self.mac else None)
        verify = getattr(self.app, "verify_view", None)
        if verify is not None:
            verify.on_tab_changed()

    def _new_tab(self) -> None:
        name = Querybox.get_string("Name of the new sheet tab:", "New tab", parent=self)
        if not name or not name.strip():
            return
        self.new_tab_btn.configure(state="disabled")

        def done(created, err):
            self.new_tab_btn.configure(state="normal")
            if err:
                Messagebox.show_error(err, "New tab", parent=self)
                return
            self._set_tab_values([*self.tab_combo.cget("values"), created])
            self.tab_var.set(created)
            self._on_tab_selected()

        self.app.run_sheet(lambda client: client.create_tab(name), done)

    # -------------------------------------------------------------- sheet ---

    def refresh(self, then=None, on_error=None) -> None:
        """Reload records from the sheet; ``then()`` runs after a successful load."""
        self.refresh_btn.configure(state="disabled")

        def done(records, err):
            self.refresh_btn.configure(state="normal")
            if err:
                self._sheet_status(f"✖  Google Sheet: {err}", "danger")
                if on_error:
                    self._info(f"✖  Cannot check Google Sheet - {err}", "danger")
                    on_error()
                return
            self.records = records
            self._fill_list()
            self._update_next_seq()
            n = len(records)
            self._sheet_status(f"✔  Google Sheet connected  ·  {n} QR code{'s' if n != 1 else ''}", "success")
            if then:
                then()

        self.app.run_sheet(lambda client: client.records(), done)

    def _sheet_status(self, text: str, style: str) -> None:
        self.sheet_lbl.configure(text=text, bootstyle=style)

    # List rows are identified by sheet row (unique even if the sheet holds a duplicate
    # SEQ); ticks are remembered by (SEQ, MAC) so they always mean the same device.

    @staticmethod
    def _iid(r: SheetRecord) -> str:
        return f"row{r.row}"

    @staticmethod
    def _key(r: SheetRecord) -> tuple[int, str]:
        return (r.seq, r.mac)

    def _fill_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._by_iid = {self._iid(r): r for r in self.records}
        self._checked &= {self._key(r) for r in self.records}
        for r in sorted(self.records, key=lambda r: (r.seq, r.row), reverse=True)[:LIST_LIMIT]:
            key = self._key(r)
            if key not in self._thumbs:
                self._thumbs[key] = ImageTk.PhotoImage(thumbnail(r.payload, 64))
            self.tree.insert("", "end", iid=self._iid(r), image=self._thumbs[key], text=self._box(r),
                             values=(r.created_at, r.seq_text, compact_mac(r.mac), r.device_type, r.status))
        self._update_selection()

    # ---------------------------------------------------- list selection ---

    def _box(self, r: SheetRecord) -> str:
        return "   ☑" if self._key(r) in self._checked else "   ☐"

    def _set_checked(self, r: SheetRecord, checked: bool) -> None:
        (self._checked.add if checked else self._checked.discard)(self._key(r))
        if self.tree.exists(self._iid(r)):
            self.tree.item(self._iid(r), text=self._box(r))

    def _toggle_iid(self, iid: str) -> None:
        r = self._by_iid.get(iid)
        if r:
            self._set_checked(r, self._key(r) not in self._checked)
            self._update_selection()

    def _on_tree_click(self, event) -> str | None:
        # Clicks on the checkbox/thumbnail column tick the row; elsewhere they just preview.
        if self.tree.identify_region(event.x, event.y) != "tree" or self.tree.identify_column(event.x) != "#0":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_iid(iid)
        return "break"

    def _toggle_focused(self) -> str:
        iid = self.tree.focus()
        if iid:
            self._toggle_iid(iid)
        return "break"

    def _on_select_all(self) -> None:
        checked = self.select_all_var.get()
        for r in self.records:
            self._set_checked(r, checked)
        self._update_selection()

    def _toggle_all_from_heading(self) -> None:
        self.select_all_var.set(not (self.records and len(self._checked) == len(self.records)))
        self._on_select_all()

    def _update_selection(self) -> None:
        n, total = len(self._checked), len(self.records)
        all_on = bool(total) and n == total
        self.select_all_var.set(all_on)
        self.tree.heading("#0", text=("☑" if all_on else "☐") + "   QR Code")
        self.sel_count_lbl.configure(text=f"{n} of {total} selected")
        self.pdf_btn.configure(state="normal" if n else "disabled")
        self._update_grid_hint()

    def _update_grid_hint(self) -> None:
        n = len(self._checked)
        pages = -(-n // pdf.PER_PAGE) if n else 0
        self.grid_hint.configure(
            text=f"fixed layout · {pdf.COLS} × {pdf.ROWS} = {pdf.PER_PAGE} labels per page · box {pdf.BOX} pt"
                 + (f"  ·  {n} selected → {pages} page{'s' if pages != 1 else ''}" if n else ""))

    def _toggle_generator(self) -> None:
        self._generator_visible = not self._generator_visible
        if self._generator_visible:
            self.gen_left.grid()
            self.gen_right.grid()
            self.toggle_btn.configure(text="▲  Hide generator")
        else:
            self.gen_left.grid_remove()
            self.gen_right.grid_remove()
            self.toggle_btn.configure(text="▼  Show generator")

    # ---------------------------------------------------------------- PDF ---

    def _generate_pdf(self) -> None:
        if not self._checked:
            return
        wanted = set(self._checked)
        self.pdf_btn.configure(state="disabled", text="Checking sheet…")
        self.app.set_status("Checking the whole sheet tab for duplicate SEQs and MACs…")

        def audited(result, err):
            self.pdf_btn.configure(text="⬇  Generate PDF", state="normal" if self._checked else "disabled")
            if err:
                Messagebox.show_error(f"Could not check the Google Sheet:\n{err}\n\nThe PDF was not created.",
                                      "Generate PDF", parent=self)
                self.app.set_status("PDF not created - sheet check failed")
                return
            records, problems = result
            self.records = records  # show exactly what was checked
            self._fill_list()
            self._update_next_seq()
            current = {self._key(r) for r in records}
            missing = sorted(wanted - current)
            if missing:
                problems = problems + [
                    f"Ticked SEQ {format_seq(seq)} / MAC {build_payload(mac)} is no longer in the sheet "
                    "(changed or removed) - re-check your selection" for seq, mac in missing]
            if problems:
                shown = "\n".join(f"•  {p}" for p in problems[:15])
                more = f"\n…and {len(problems) - 15} more" if len(problems) > 15 else ""
                Messagebox.show_error(
                    f"The sheet tab '{self.current_tab}' has {len(problems)} problem{'s' if len(problems) != 1 else ''}. "
                    f"Fix them in the Google Sheet, then try again.\n\n{shown}{more}\n\nThe PDF was not created.",
                    "Sheet check failed", parent=self)
                self.app.set_status(f"PDF not created - {len(problems)} problem(s) in the sheet")
                return
            self.app.set_status(f"Sheet check passed: {len(records)} rows, all SEQs and MACs unique")
            self._write_pdf(sorted((r for r in records if self._key(r) in wanted), key=lambda r: r.seq))

        self.app.run_sheet(lambda client: client.audit(), audited)

    def _write_pdf(self, records: list[SheetRecord]) -> None:
        if not records:
            return
        tab = re.sub(r'[\\/:*?"<>|]+', "_", self.current_tab or "QR")
        path = filedialog.asksaveasfilename(
            parent=self, title="Save QR labels as PDF", defaultextension=".pdf",
            initialfile=f"{tab}_SEQ{records[0].seq_text}-{records[-1].seq_text}.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not path:
            return
        self.pdf_btn.configure(state="disabled", text="Generating…")
        self.app.set_status(f"Generating PDF with {len(records)} labels…")

        def progress(done, total):
            self.app.dispatcher.post(lambda: self.pdf_btn.configure(text=f"Generating {done}/{total}…"))

        def run():
            try:
                pages, err = pdf.build_pdf(records, Path(path), progress=progress), None
            except Exception as exc:
                log.exception("PDF generation failed")
                pages, err = 0, str(exc)
            self.app.dispatcher.post(finished, pages, err)

        def finished(pages, err):
            self.pdf_btn.configure(text="⬇  Generate PDF", state="normal" if self._checked else "disabled")
            if err:
                Messagebox.show_error(f"Could not create the PDF:\n{err}", "Generate PDF", parent=self)
                self.app.set_status("PDF not created")
                return
            self.app.set_status(f"PDF saved: {path}  ({len(records)} labels, {pages} page{'s' if pages != 1 else ''})")
            open_path(Path(path))

        threading.Thread(target=run, daemon=True, name="pdf").start()

    def _update_next_seq(self) -> None:
        self.seq_lbl.configure(text=format_seq(next_seq(self.records, self.app.settings.qr.seq_start)))

    def find_by_seq(self, seq: int) -> SheetRecord | None:
        return next((r for r in self.records if r.seq == seq), None)

    # --------------------------------------------------------------- flow ---

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.read_btn.configure(state="disabled" if busy else "normal")

    def _read_mac(self) -> None:
        self._pending = None
        self.save_btn.configure(state="disabled")
        self.gen_btn.configure(state="disabled")
        self._set_busy(True)
        self.read_btn.configure(text="Reading MAC…")
        self._info("Reading MAC address over J-Link…", "Hint.TLabel")
        self.app.probe_bar.read_mac(self._on_mac)

    def _on_mac(self, mac: str | None, err: str) -> None:
        self.read_btn.configure(text="Read MAC Address")
        if not mac:
            self._set_busy(False)
            self.mac = None
            self.mac_lbl.configure(text="--:--:--:--:--:--")
            self._info(f"✖  {err}", "danger")
            return
        self.mac = mac
        self.mac_lbl.configure(text=compact_mac(mac))
        self._info("Checking Google Sheet…", "Hint.TLabel")
        # Re-read the sheet: another station may have registered this device.
        self.refresh(then=self._check_mac, on_error=lambda: self._set_busy(False))

    def _check_mac(self) -> None:
        self._set_busy(False)
        existing = next((r for r in self.records if r.mac == self.mac), None)
        if existing:
            self._info(f"Already registered as SEQ {existing.seq_text} on {existing.created_at}.", "warning")
            self._show_record(existing)
            self._select_in_list(existing.seq)
        else:
            self._info("✔  New device - click Generate.", "success")
            self.gen_btn.configure(state="normal")
            self._clear_preview()

    @property
    def device_type(self) -> str:
        return self.type_var.get().strip()

    def set_device_types(self, types: list[str]) -> None:
        self.type_combo.configure(values=types)
        if self.device_type not in types:
            self.type_var.set("")

    def _on_type_selected(self) -> None:
        self.app.settings.qr.device_type = self.device_type
        self.app.save_settings()
        if self._pending:  # keep the preview caption in sync
            seq, image = self._pending
            self._set_preview(image, f"SEQ {format_seq(seq)}   ·   {compact_mac(self.mac)}   ·   {self.device_type}   (not saved)")

    def _generate(self) -> None:
        if not self.mac:
            return
        if not self.device_type:
            self._info("✖  Choose the DEVICE TYPE first.", "danger")
            self.type_combo.focus_set()
            return
        seq = next_seq(self.records, self.app.settings.qr.seq_start)
        if seq > MAX_SEQ:
            self._info(f"✖  Sequence limit {MAX_SEQ} reached.", "danger")
            return
        image = render_label(build_payload(self.mac), self.mac, seq)
        self._pending = (seq, image)
        self._set_preview(image, f"SEQ {format_seq(seq)} (expected)   ·   {compact_mac(self.mac)}   ·   {self.device_type}   (not saved)")
        self.gen_btn.configure(state="disabled")
        self.save_btn.configure(state="normal")
        self._info(f"Preview ready - click Save to write it to tab '{self.current_tab}'. "
                   "The final SEQ is fixed when it is saved.", "Hint.TLabel")

    def _save(self) -> None:
        if not (self._pending and self.mac):
            return
        seq, image = self._pending
        mac, device_type = self.mac, self.device_type
        if not device_type:
            self._info("✖  Choose the DEVICE TYPE first.", "danger")
            return
        self.save_btn.configure(state="disabled", text="Saving…")
        self._set_busy(True)
        self._info("Writing to Google Sheet…", "Hint.TLabel")

        def done(record: SheetRecord | None, err: str | None):
            self.save_btn.configure(text="Save")
            self._set_busy(False)
            if err:
                self._info(f"✖  NOT SAVED - {err}", "danger")
                if "already registered" not in err:
                    self.save_btn.configure(state="normal")  # e.g. offline: retry with Save
                self.app.set_status("QR code not saved")
                return
            self._pending = None
            label = image
            note = ""
            if record.seq != seq:  # another station saved first - the sheet gave us the next free SEQ
                label = render_label(build_payload(record.mac), record.mac, record.seq)
                note = f"  (SEQ {format_seq(seq)} was taken by another station)"
            self._set_preview(label, f"SEQ {record.seq_text}   ·   {compact_mac(record.mac)}   ·   {record.device_type}")
            self._info(f"✔  Saved SEQ {record.seq_text} ({record.device_type}) to sheet tab '{self.current_tab}'{note}",
                       "success")
            self.app.set_status(f"Saved SEQ {record.seq_text} ({compact_mac(record.mac)}) to tab '{self.current_tab}'")
            # Reload so the list also shows rows other stations saved meanwhile.
            self.refresh(then=lambda: self._select_in_list(record.seq))

        self.app.run_sheet(lambda client: client.save(mac, device_type, self.app.settings.qr.seq_start), done)

    # ------------------------------------------------------------ helpers ---

    def _info(self, text: str, style: str) -> None:
        if style.endswith(".TLabel"):
            self.info_lbl.configure(text=text, style=style)
        else:
            self.info_lbl.configure(text=text, bootstyle=style)

    def _set_preview(self, image: Image.Image, caption: str) -> None:
        img = image.copy()
        img.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE + 90), Image.LANCZOS)
        self._preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self._preview_img, text="")
        self.preview_caption.configure(text=caption)

    def _clear_preview(self) -> None:
        self._preview_img = None
        self.preview.configure(image="", text="QR preview appears here")
        self.preview_caption.configure(text="")

    def _show_record(self, record: SheetRecord) -> None:
        self._set_preview(render_label(record.payload, record.mac, record.seq),
                          f"SEQ {record.seq_text}   ·   {compact_mac(record.mac)}   ·   {record.device_type or '-'}   ·   {record.created_at}")

    def _select_in_list(self, seq: int) -> None:
        record = self.find_by_seq(seq)
        if record and self.tree.exists(self._iid(record)):
            self.tree.selection_set(self._iid(record))
            self.tree.see(self._iid(record))

    def _show_selected(self) -> None:
        sel = self.tree.selection()
        record = self._by_iid.get(sel[0]) if sel else None
        if record and not self._pending:
            self._show_record(record)

    def _open_sheet(self) -> None:
        if self.app.settings.sheet.url:
            webbrowser.open(self.app.settings.sheet.url)
