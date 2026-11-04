"""Reusable widgets."""
from __future__ import annotations

import tkinter as tk
from datetime import datetime

import ttkbootstrap as ttk

from .theme import ICON_FONT, LABEL_BOLD, MONO_FONT, MUTED, PILL_FONT


class Console(ttk.Frame):
    """Read-only, colour-tagged activity log."""

    MAX_LINES = 3000

    def __init__(self, master, height: int = 8):
        super().__init__(master)
        c = ttk.Style().colors
        self.text = tk.Text(
            self, height=height, wrap="word", font=MONO_FONT, relief="flat", borderwidth=0,
            padx=10, pady=6, highlightthickness=1, highlightbackground=c.border, highlightcolor=c.border,
            state="disabled", cursor="arrow", background="#fbfbfc", foreground=c.fg,
        )
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview, bootstyle="round")
        self.text.configure(yscrollcommand=bar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        for tag, color in (("time", MUTED), ("muted", MUTED), ("info", c.fg), ("success", c.success),
                           ("warning", "#c77700"), ("error", c.danger), ("accent", c.primary)):
            self.text.tag_configure(tag, foreground=color)

    def write(self, text: str, level: str = "info") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.text.insert("end", f"{text}\n", level)
        excess = int(self.text.index("end-1c").split(".")[0]) - self.MAX_LINES
        if excess > 0:
            self.text.delete("1.0", f"{excess + 1}.0")
        self.text.configure(state="disabled")
        self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class StatusPill(ttk.Label):
    """Small coloured badge in the header."""

    def __init__(self, master, text: str = "", style: str = "secondary"):
        super().__init__(master, text=text, bootstyle=f"inverse-{style}", padding=(12, 5), font=PILL_FONT)

    def set(self, text: str, style: str) -> None:
        self.configure(text=text, bootstyle=f"inverse-{style}")


class CheckRow(ttk.Frame):
    """One verification check:  [icon]  Title / detail."""

    ICONS = {"pending": ("○", None), "busy": ("…", "warning"), "ok": ("✔", "success"), "fail": ("✖", "danger")}

    def __init__(self, master, title: str):
        super().__init__(master, padding=(4, 6))
        self.icon = ttk.Label(self, width=2, font=ICON_FONT, anchor="center")
        self.icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
        ttk.Label(self, text=title, font=LABEL_BOLD).grid(row=0, column=1, sticky="w")
        self.detail = ttk.Label(self, text="", style="Hint.TLabel", wraplength=420)
        self.detail.grid(row=1, column=1, sticky="w")
        self.columnconfigure(1, weight=1)
        self.state = "pending"
        self.set("pending", "Waiting…")

    def set(self, state: str, detail: str) -> None:
        self.state = state
        glyph, style = self.ICONS[state]
        if style:
            self.icon.configure(text=glyph, bootstyle=style)
        else:
            self.icon.configure(text=glyph, style="Hint.TLabel")
        self.icon.configure(font=ICON_FONT)
        self.detail.configure(text=detail)


def form_row(parent, row: int, label: str, widget, hint: str = "") -> int:
    """Grid a 'label | widget' row in a two-column form; returns the next free row."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
    widget.grid(row=row, column=1, sticky="ew", pady=4)
    if hint:
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=row + 1, column=1, sticky="w", pady=(0, 4))
        return row + 2
    return row + 1
