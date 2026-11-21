"""Anchor QR screen (separate product from the tags): read the anchor sheet -> tick -> A4 sticker PDF.

Read only: nothing is written to the anchor sheet. Shown from the "Anchor" switch in the header.
"""
from __future__ import annotations

import logging
import re
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from PIL import Image, ImageTk
from ttkbootstrap.dialogs import Messagebox

from ...core import anchor_pdf
from ...core.anchor import AnchorRecord, AnchorSheet
from ...core.qr import thumbnail
from ...core.sheets import SheetError, find_key_file
from ..theme import LABEL_BOLD, PAD
from .generate_view import open_path

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)

PREVIEW_H = 430   # on-screen sticker preview height in px


class AnchorView(ttk.Frame):
    def __init__(self, master, app: "MainWindow"):
        super().__init__(master, padding=PAD)
        self.app = app
        self.records: list[AnchorRecord] = []
        self._checked: set[tuple[int, str]] = set()   # (SEQ, Ethernet MAC) ticked for the PDF
        self._by_iid: dict[str, AnchorRecord] = {}
        self._thumbs: dict[str, ImageTk.PhotoImage] = {}
        self._preview_img: ImageTk.PhotoImage | None = None
        self._client: AnchorSheet | None = None
        self._client_key: tuple | None = None
        self._loaded = False
        self._build()

    # ------------------------------------------------------------- layout ---

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, PAD))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Anchor sheet", font=LABEL_BOLD).grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value=self.app.settings.anchor.url)
        ttk.Entry(top, textvariable=self.url_var).grid(row=0, column=1, sticky="ew", padx=(10, 6))
        ttk.Label(top, text="Tab", font=LABEL_BOLD).grid(row=0, column=2, padx=(6, 6))
        self.tab_var = tk.StringVar(value=self.app.settings.anchor.tab)
        self.tab_combo = ttk.Combobox(top, textvariable=self.tab_var, state="readonly", width=20)
        self.tab_combo.grid(row=0, column=3)
        self.tab_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_tab_selected())
        self.load_btn = ttk.Button(top, text="⟳ Load", bootstyle="primary", command=self._on_load_clicked)
        self.load_btn.grid(row=0, column=4, padx=(6, 0))
        ttk.Button(top, text="Open Google Sheet", bootstyle="primary-link", command=self._open_sheet).grid(row=0, column=5)

        # --- list ---
        left = ttk.Labelframe(self, text=" Anchors (Google Sheet, read only) ", padding=PAD)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, PAD))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.select_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Select all", variable=self.select_all_var, bootstyle="primary",
                        command=self._on_select_all).pack(side="left")
        self.sel_count_lbl = ttk.Label(bar, text="0 selected", font=LABEL_BOLD)
        self.sel_count_lbl.pack(side="left", padx=(10, 16))
        self.sheet_lbl = ttk.Label(bar, text="Not loaded", style="Hint.TLabel")
        self.sheet_lbl.pack(side="left")

        pdf_bar = ttk.Frame(left)
        pdf_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(pdf_bar, text="A4 anchor stickers", font=LABEL_BOLD).pack(side="left")
        self.grid_hint = ttk.Label(pdf_bar, text="", style="Hint.TLabel")
        self.grid_hint.pack(side="left", padx=(12, 0))
        self.pdf_btn = ttk.Button(pdf_bar, text="⬇  Generate PDF", bootstyle="success", command=self._generate_pdf, state="disabled")
        self.pdf_btn.pack(side="right", ipadx=10)

        cols = (("seq", "Seq", 70), ("eth", "Ethernet MAC", 150), ("cdk", "CDK MAC (ID)", 150), ("type", "Device Type", 120))
        self.tree = ttk.Treeview(left, columns=[c[0] for c in cols], show="tree headings", style="Qr.Treeview", selectmode="browse")
        self.tree.heading("#0", text="☐   QR Code", anchor="w", command=self._toggle_all_from_heading)
        self.tree.column("#0", width=130, stretch=False, anchor="w")
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=key == "type")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview, bootstyle="round")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=2, column=0, sticky="nsew")
        scroll.grid(row=2, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<space>", lambda _e: self._toggle_focused())

        # --- preview ---
        right = ttk.Labelframe(self, text=" Sticker preview ", padding=PAD)
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        self.preview = ttk.Label(right, text="Select an anchor to preview its sticker", style="Hint.TLabel", anchor="center", width=34)
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview_caption = ttk.Label(right, text="", style="Hint.TLabel", anchor="center")
        self.preview_caption.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._update_selection()

    # -------------------------------------------------------------- sheet ---

    def on_show(self) -> None:
        """Called when the Anchor screen is opened: load the sheet the first time."""
        if not self._loaded:
            self.load()

    def _sheet(self) -> AnchorSheet:
        key_file = find_key_file(self.app.settings.sheet.key_file)
        if key_file is None:
            raise SheetError("Service account key not found - set it in Settings")
        url = self.app.settings.anchor.url.strip()
        if not url:
            raise SheetError("Enter the anchor Google Sheet URL")
        ident = (str(key_file), url, self.app.settings.anchor.tab)
        if self._client is None or self._client_key != ident:
            self._client = AnchorSheet(key_file, url, self.app.settings.anchor.tab)
            self._client_key = ident
        return self._client

    def _run(self, work, on_done) -> None:
        """``work(client)`` on a worker thread; ``on_done(result, error)`` on the UI thread."""
        try:
            client = self._sheet()
        except SheetError as exc:
            on_done(None, str(exc))
            return

        def run():
            try:
                result, err = work(client), None
            except SheetError as exc:
                result, err = None, str(exc)
            except Exception as exc:
                log.exception("Anchor sheet call failed")
                result, err = None, f"{exc.__class__.__name__}: {exc}"
            self.app.dispatcher.post(on_done, result, err)

        threading.Thread(target=run, daemon=True, name="anchor-sheet").start()

    def _on_load_clicked(self) -> None:
        url = self.url_var.get().strip()
        if url != self.app.settings.anchor.url:
            self.app.settings.anchor.url = url
            self.app.settings.anchor.tab = ""     # tabs belong to the previous spreadsheet
            self.tab_var.set("")
            self.app.save_settings()
        self.load()

    def _on_tab_selected(self) -> None:
        tab = self.tab_var.get()
        if tab != self.app.settings.anchor.tab:
            self.app.settings.anchor.tab = tab
            self.app.save_settings()
            self.load()

    def load(self, then=None, on_error=None) -> None:
        """Read tabs + rows; ``then(problems)`` after a successful load, ``on_error(message)`` if it fails."""
        self.load_btn.configure(state="disabled")
        self._status("Loading anchor sheet…", "secondary")

        def work(client: AnchorSheet):
            return client.tabs(), client.tab_title(), client.load()

        def done(result, err):
            self.load_btn.configure(state="normal")
            if err:
                self._status(f"✖  {err}", "danger")
                if on_error:
                    on_error(err)
                return
            self._loaded = True
            tabs, tab, (records, problems) = result
            self.tab_combo.configure(values=tabs)
            self.tab_var.set(tab)
            self.records = sorted(records, key=lambda r: (r.seq, r.row))
            self._fill_list()
            n = len(records)
            text = f"✔  tab '{tab}'  ·  {n} anchor{'s' if n != 1 else ''}"
            if problems:
                self._status(f"{text}  ·  ⚠ {len(problems)} problem(s) - see Generate PDF", "warning")
            else:
                self._status(text, "success")
            if then:
                then(problems)

        self._run(work, done)

    def _status(self, text: str, style: str) -> None:
        self.sheet_lbl.configure(text=text, bootstyle=style)

    # --------------------------------------------------------------- list ---

    @staticmethod
    def _key(r: AnchorRecord) -> tuple[int, str]:
        return (r.seq, r.eth_mac)

    @staticmethod
    def _iid(r: AnchorRecord) -> str:
        return f"row{r.row}"

    def _fill_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._by_iid = {self._iid(r): r for r in self.records}
        self._checked &= {self._key(r) for r in self.records}
        for r in self.records:
            if r.payload not in self._thumbs:
                self._thumbs[r.payload] = ImageTk.PhotoImage(thumbnail(r.payload, 64))
            self.tree.insert("", "end", iid=self._iid(r), image=self._thumbs[r.payload], text=self._box(r),
                             values=(r.seq_text, r.eth_mac, r.cdk_mac, r.device_type))
        self._update_selection()

    def _box(self, r: AnchorRecord) -> str:
        return "   ☑" if self._key(r) in self._checked else "   ☐"

    def _set_checked(self, r: AnchorRecord, checked: bool) -> None:
        (self._checked.add if checked else self._checked.discard)(self._key(r))
        if self.tree.exists(self._iid(r)):
            self.tree.item(self._iid(r), text=self._box(r))

    def _toggle_iid(self, iid: str) -> None:
        r = self._by_iid.get(iid)
        if r:
            self._set_checked(r, self._key(r) not in self._checked)
            self._update_selection()

    def _on_tree_click(self, event) -> str | None:
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
        pages = -(-n // anchor_pdf.PER_PAGE) if n else 0
        self.grid_hint.configure(
            text=f"{anchor_pdf.COLS} × {anchor_pdf.ROWS} = {anchor_pdf.PER_PAGE} per page · "
                 f"box {anchor_pdf.BOX_W} × {anchor_pdf.BOX_H} pt"
                 + (f"  ·  {n} selected → {pages} page{'s' if pages != 1 else ''}" if n else ""))

    def _show_selected(self) -> None:
        sel = self.tree.selection()
        record = self._by_iid.get(sel[0]) if sel else None
        if record is None:
            return
        scale = PREVIEW_H / (anchor_pdf.BOX_H + 2 * anchor_pdf.BORDER)
        img = anchor_pdf.render_preview(record, scale=scale * 2)   # 2x then downscale = smoother text
        img = img.resize((round(img.width / 2), round(img.height / 2)), Image.LANCZOS)
        self._preview_img = ImageTk.PhotoImage(img)
        self.preview.configure(image=self._preview_img, text="")
        self.preview_caption.configure(text=f"Row {record.row}  ·  QR content: {record.payload}")

    # ---------------------------------------------------------------- PDF ---

    def _generate_pdf(self) -> None:
        if not self._checked:
            return
        wanted = set(self._checked)
        self.pdf_btn.configure(state="disabled", text="Checking sheet…")
        self.app.set_status("Re-reading the anchor sheet to check for duplicates…")

        def checked(problems: list[str]) -> None:
            missing = sorted(wanted - {self._key(r) for r in self.records})
            problems = problems + [f"Ticked SEQ {seq:04d} / MAC {mac} is no longer in the sheet - re-check your selection"
                                   for seq, mac in missing]
            if problems:
                shown = "\n".join(f"•  {p}" for p in problems[:15])
                more = f"\n…and {len(problems) - 15} more" if len(problems) > 15 else ""
                Messagebox.show_error(
                    f"The anchor sheet has {len(problems)} problem{'s' if len(problems) != 1 else ''}. "
                    f"Fix them in the Google Sheet, then try again.\n\n{shown}{more}\n\nThe PDF was not created.",
                    "Sheet check failed", parent=self)
                self.app.set_status(f"Anchor PDF not created - {len(problems)} problem(s) in the sheet")
                return
            self._write_pdf([r for r in self.records if self._key(r) in wanted])

        def reset_button():
            self.pdf_btn.configure(text="⬇  Generate PDF", state="normal" if self._checked else "disabled")

        def failed(err: str) -> None:
            reset_button()
            Messagebox.show_error(f"Could not read the anchor sheet:\n{err}\n\nThe PDF was not created.",
                                  "Generate PDF", parent=self)
            self.app.set_status("Anchor PDF not created - sheet read failed")

        def then(problems):
            reset_button()
            checked(problems)

        self.load(then=then, on_error=failed)

    def _write_pdf(self, records: list[AnchorRecord]) -> None:
        if not records:
            return
        tab = re.sub(r'[\\/:*?"<>|]+', "_", self.tab_var.get() or "Anchor")
        path = filedialog.asksaveasfilename(
            parent=self, title="Save anchor stickers as PDF", defaultextension=".pdf",
            initialfile=f"Anchor_{tab}_SEQ{records[0].seq_text}-{records[-1].seq_text}.pdf",
            filetypes=[("PDF", "*.pdf")])
        if not path:
            return
        self.pdf_btn.configure(state="disabled", text="Generating…")
        self.app.set_status(f"Generating anchor PDF with {len(records)} stickers…")

        def progress(done, total):
            self.app.dispatcher.post(lambda: self.pdf_btn.configure(text=f"Generating {done}/{total}…"))

        def run():
            try:
                pages, err = anchor_pdf.build_pdf(records, Path(path), progress=progress), None
            except Exception as exc:
                log.exception("Anchor PDF generation failed")
                pages, err = 0, str(exc)
            self.app.dispatcher.post(finished, pages, err)

        def finished(pages, err):
            self.pdf_btn.configure(text="⬇  Generate PDF", state="normal" if self._checked else "disabled")
            if err:
                Messagebox.show_error(f"Could not create the PDF:\n{err}", "Generate PDF", parent=self)
                self.app.set_status("Anchor PDF not created")
                return
            self.app.set_status(f"Anchor PDF saved: {path}  ({len(records)} stickers, {pages} page{'s' if pages != 1 else ''})")
            open_path(Path(path))

        threading.Thread(target=run, daemon=True, name="anchor-pdf").start()

    def _open_sheet(self) -> None:
        url = self.url_var.get().strip()
        if url:
            webbrowser.open(url)
