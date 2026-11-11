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

    ICONS = {"pending": ("○", None), "busy": ("…", "warning"), "ok": ("✔", "success"), "fail": ("✖", "danger"),
             "info": ("ℹ", "info"), "skip": ("–", None)}

    def __init__(self, master, title: str, wraplength: int = 420, padding=(4, 6)):
        super().__init__(master, padding=padding)
        self.icon = ttk.Label(self, width=2, font=ICON_FONT, anchor="center")
        self.icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
        ttk.Label(self, text=title, font=LABEL_BOLD).grid(row=0, column=1, sticky="w")
        self.detail = ttk.Label(self, text="", style="Hint.TLabel", wraplength=wraplength)
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


class Responsive:
    """Two layouts for a set of grid children: side by side when ``watch`` is wide, stacked when narrow.

    ``wide`` / ``narrow`` map each widget to its grid options; ``columns`` gives the column
    weights of ``parent`` for each layout. Re-grids only when the width crosses ``breakpoint``.
    """

    def __init__(self, parent, watch, breakpoint: int, wide: dict, narrow: dict,
                 columns: tuple[dict, dict] = ({0: 1, 1: 1}, {0: 1, 1: 0})):
        self.parent, self.breakpoint = parent, breakpoint
        self.layouts, self.columns = (wide, narrow), columns
        self.narrow: bool | None = None
        watch.bind("<Configure>", self._on_configure, add="+")
        self.apply(False)

    def _on_configure(self, event) -> None:
        if event.width > 1:
            self.apply(event.width < self.breakpoint)

    def apply(self, narrow: bool) -> None:
        if narrow == self.narrow:
            return
        self.narrow = narrow
        for col, weight in self.columns[narrow].items():
            self.parent.columnconfigure(col, weight=weight, uniform="r" if weight and not narrow else "")
        for widget, opts in self.layouts[narrow].items():
            widget.grid_forget()
            widget.grid(**opts)


class SecretField(ttk.Frame):
    """A masked secret with an eye button (admin view).

    ``get_value()`` is called only when the eye is clicked, so the secret is not kept in the
    widget while hidden. It hides itself again after ``auto_hide_s``. Revealing is logged, the
    value never is.
    """

    MASK = "••••••••••••"

    def __init__(self, master, name: str, get_value, auto_hide_s: int = 30, lines: int = 1, width: int = 40):
        super().__init__(master)
        self.name, self.get_value, self.auto_hide_ms, self.lines = name, get_value, auto_hide_s * 1000, lines
        self.columnconfigure(0, weight=1)
        self.text = tk.Text(self, height=1, width=width, font=MONO_FONT, wrap="char", relief="flat",
                            highlightthickness=1, highlightbackground="#dee2e6", background="#fbfbfc", padx=6, pady=3)
        self.text.grid(row=0, column=0, sticky="ew")
        self.eye = ttk.Button(self, text="👁", width=3, bootstyle="secondary-outline", command=self.toggle)
        self.eye.grid(row=0, column=1, sticky="n", padx=(6, 0))
        self.copy_btn = ttk.Button(self, text="Copy", bootstyle="secondary-link", command=self.copy, state="disabled")
        self.copy_btn.grid(row=0, column=2, sticky="n")
        self.shown = False
        self._timer = None
        self.hide()

    def _put(self, value: str, height: int) -> None:
        self.text.configure(state="normal", height=height)
        self.text.delete("1.0", "end")
        self.text.insert("end", value)
        self.text.configure(state="disabled")

    def toggle(self) -> None:
        self.hide() if self.shown else self.show()

    def show(self) -> None:
        import logging
        try:
            value = str(self.get_value())
        except Exception as exc:   # e.g. no key configured
            self._put(f"✖ {exc}", 1)
            return
        lines = max(1, min(self.lines, value.count("\n") + 1))
        self._put(value, lines)
        self.shown = True
        self.eye.configure(text="🙈", bootstyle="warning-outline")
        self.copy_btn.configure(state="normal")
        logging.getLogger(__name__).info("Admin view: %s revealed on screen", self.name)   # never the value
        if self._timer:
            self.after_cancel(self._timer)
        self._timer = self.after(self.auto_hide_ms, self.hide)

    def hide(self) -> None:
        if self._timer:
            self.after_cancel(self._timer)
            self._timer = None
        self._put(self.MASK, 1)
        self.shown = False
        self.eye.configure(text="👁", bootstyle="secondary-outline")
        self.copy_btn.configure(state="disabled")

    def copy(self) -> None:
        if self.shown:
            self.clipboard_clear()
            self.clipboard_append(self.text.get("1.0", "end-1c"))


class KeyValueGrid(ttk.Frame):
    """Label / value rows that can be refilled; values are selectable (copy) and wrap."""

    def __init__(self, master, wraplength: int = 520):
        super().__init__(master)
        self.columnconfigure(1, weight=1)
        self.wraplength = wraplength
        self._rows: list[tuple[ttk.Label, ttk.Label]] = []

    def set_rows(self, rows: list[tuple[str, str, str | None]]) -> None:
        """rows = (label, value, bootstyle or None)."""
        for a, b in self._rows:
            a.destroy()
            b.destroy()
        self._rows = []
        for i, (label, value, style) in enumerate(rows):
            a = ttk.Label(self, text=label, style="Hint.TLabel")
            a.grid(row=i, column=0, sticky="nw", padx=(0, 12), pady=1)
            b = ttk.Label(self, text=value, wraplength=self.wraplength, justify="left",
                          **({"bootstyle": style} if style else {}))
            b.grid(row=i, column=1, sticky="w", pady=1)
            self._rows.append((a, b))


def form_row(parent, row: int, label: str, widget, hint: str = "") -> int:
    """Grid a 'label | widget' row in a two-column form; returns the next free row."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
    widget.grid(row=row, column=1, sticky="ew", pady=4)
    if hint:
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=row + 1, column=1, sticky="w", pady=(0, 4))
        return row + 2
    return row + 1
