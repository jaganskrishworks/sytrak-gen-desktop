"""MQTT tab: live view of the embedded broker, like MQTT Explorer.

Summary cards on top; below, every topic as an expandable tree (latest value, message count,
time), the selected message (direction, client, QoS, retained, formatted JSON), its history and
the connected clients. Wide windows show two columns, narrow ones stack the panels.
Tag passwords (PWD/PACK) are masked before they are stored.
"""
from __future__ import annotations

import json
import logging
import tkinter as tk
from datetime import datetime
from tkinter import filedialog
from typing import TYPE_CHECKING

import ttkbootstrap as ttk
from ttkbootstrap.dialogs import Messagebox

from ...core.mqtt_monitor import Message, TopicNode, TrafficModel, make_message
from ..theme import FONT_FAMILY, LABEL_BOLD, MONO_FONT, MUTED, PAD
from ..widgets import StatusPill

if TYPE_CHECKING:
    from ..main_window import MainWindow

log = logging.getLogger(__name__)

FLASH_MS = 1200
BREAKPOINT = 1150          # tab width below this: panels stacked
ORIGIN_COLORS = {"station": "#1c64d1", "client": "#1d8a4b", "will": "#c77700"}
ORIGIN_PILLS = {"station": ("PC  →  phone", "primary"), "client": ("phone  →  PC", "success"),
                "will": ("last will  ·  phone lost", "warning")}
TOPIC_FONT = ("Consolas", 11, "bold")
EMPTY_TEXT = "Waiting for messages…\n\nConnect a phone in  Verify QR › Mobile connection.\nEvery message to and from it appears here live."


def _iid(path: str) -> str:
    return "t:" + path


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


class MqttView(ttk.Frame):
    def __init__(self, master, app: "MainWindow"):
        super().__init__(master, padding=PAD)
        self.app = app
        self.model = TrafficModel()
        self.paused = False
        self._missed = 0
        self._selected: str | None = None        # topic path shown on the right
        self._shown: Message | None = None       # message shown in the payload box
        self._pinned = False                     # a history row was picked: don't jump to the newest
        self._branch_time: dict[str, datetime] = {}
        self._flash_gen: dict[str, int] = {}
        self._filter_job = None
        self._last_msg: datetime | None = None
        self._narrow: bool | None = None
        self._hint_visible = True
        self.broker = ("—", "", False)            # address, topic root, running

        self._build_toolbar()
        self._build_stats()
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, pady=(PAD, 0))
        self.topics_box = self._build_tree(self.body)
        self.detail_box = self._build_detail(self.body)
        self.history_box = self._build_history(self.body)
        self.clients_box = self._build_clients(self.body)
        self.bind("<Configure>", lambda e: self._layout(e.width < BREAKPOINT) if e.widget is self else None, add="+")
        self._layout(False)
        self._show_message(None, "Select a topic in the tree to see its messages.")
        self._update_empty()
        self._update_status()

    # ------------------------------------------------------------- layout ---

    def _layout(self, narrow: bool) -> None:
        """Topics + clients | message + history. Narrow windows: compact summary row, equal columns."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        b, gap = self.body, PAD // 2
        if not self.topics_box.winfo_manager():
            self.topics_box.grid(row=0, column=0, sticky="nsew", padx=(0, gap), pady=(0, gap))
            self.clients_box.grid(row=1, column=0, sticky="nsew", padx=(0, gap), pady=(gap, 0))
            self.detail_box.grid(row=0, column=1, sticky="nsew", padx=(gap, 0), pady=(0, gap))
            self.history_box.grid(row=1, column=1, sticky="nsew", padx=(gap, 0), pady=(gap, 0))
            b.rowconfigure(0, weight=3)
            b.rowconfigure(1, weight=1)
        b.columnconfigure(0, weight=1 if narrow else 11, uniform="c")
        b.columnconfigure(1, weight=1 if narrow else 9, uniform="c")
        self._stats_compact(narrow)
        if narrow:
            self.filter_entry.configure(width=24)
            self.tree.configure(displaycolumns=("value", "msgs"))
            self.tree.column("#0", width=190)
            self._hint_visible = False
        else:
            self.filter_entry.configure(width=34)
            self.tree.configure(displaycolumns=("value", "msgs", "time"))
            self.tree.column("#0", width=230)
            self._hint_visible = True
        self._schedule_filter(refilter=False)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        search = ttk.Frame(bar)
        search.pack(side="left", fill="x", expand=True)
        ttk.Label(search, text="🔍", font=(FONT_FAMILY, 11)).pack(side="left", padx=(0, 6))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._schedule_filter())
        self.filter_entry = ttk.Entry(search, textvariable=self.filter_var, width=34)
        self.filter_entry.pack(side="left")
        self.filter_clear = ttk.Button(search, text="✕", width=2, bootstyle="secondary-link",
                                       command=lambda: self.filter_var.set(""))
        self.filter_hint = ttk.Label(search, text="Filter by topic or value", style="Hint.TLabel")
        self.paused_pill = StatusPill(search, "", "warning")

        ttk.Button(bar, text="💾  Save…", bootstyle="primary", command=self._export).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Clear", bootstyle="danger-outline", command=self._clear).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="⊟  Collapse", bootstyle="primary-outline",
                   command=lambda: self._open_all(False)).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="⊞  Expand", bootstyle="primary-outline",
                   command=lambda: self._open_all(True)).pack(side="right", padx=(6, 0))
        self.pause_btn = ttk.Button(bar, text="⏸  Pause", bootstyle="warning-outline", command=self._toggle_pause, width=11)
        self.pause_btn.pack(side="right", padx=(6, 0))

    def _build_stats(self) -> None:
        """Summary cards: broker, clients, topics, messages, data, last message."""
        self.stats_row = ttk.Frame(self)
        self.stats_row.pack(fill="x", pady=(PAD, 0))
        self.stats: dict[str, tuple[ttk.Frame, ttk.Label, ttk.Label]] = {}
        for key, title in (("broker", "Broker"), ("clients", "Online"), ("topics", "Topics"),
                           ("messages", "Messages"), ("data", "Data"), ("last", "Last message")):
            card = ttk.Frame(self.stats_row, style="Stat.TFrame", padding=(14, 6))
            ttk.Label(card, text=title.upper(), style="StatHint.TLabel").pack(anchor="w")
            value = ttk.Label(card, text="—", style="StatValue.TLabel")
            value.pack(anchor="w", pady=(2, 0))
            sub = ttk.Label(card, text="", style="StatHint.TLabel")
            sub.pack(anchor="w")
            self.stats[key] = (card, value, sub)

    def _stats_compact(self, compact: bool) -> None:
        """One row of cards; compact = smaller numbers, no second line (narrow windows)."""
        for i, (card, value, sub) in enumerate(self.stats.values()):
            if not card.winfo_manager():
                card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else PAD // 2, 0))
                self.stats_row.columnconfigure(i, weight=2 if i == 0 else 1, uniform="s")
            card.configure(padding=(10, 4) if compact else (14, 6))
            value.configure(style="StatValueSm.TLabel" if compact else "StatValue.TLabel")
            if compact:
                sub.pack_forget()
            elif not sub.winfo_manager():
                sub.pack(anchor="w")

    def _card(self, parent, title: str) -> ttk.Labelframe:
        return ttk.Labelframe(parent, text=f" {title} ", padding=(PAD, 6, PAD, PAD))

    @staticmethod
    def _with_bars(frame: ttk.Frame, widget, horizontal: bool = False):
        ys = ttk.Scrollbar(frame, orient="vertical", command=widget.yview, bootstyle="round")
        widget.configure(yscrollcommand=ys.set)
        widget.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        if horizontal:
            xs = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview, bootstyle="round")
            widget.configure(xscrollcommand=xs.set)
            xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return widget

    def _build_tree(self, parent) -> ttk.Labelframe:
        box = self._card(parent, "Topics")
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        t = ttk.Treeview(frame, columns=("value", "msgs", "time"), selectmode="browse", style="Data.Treeview", height=8)
        t.heading("#0", text="Topic", anchor="w")
        t.heading("value", text="Latest value", anchor="w")
        t.heading("msgs", text="Msgs", anchor="e")
        t.heading("time", text="Last", anchor="w")
        t.column("#0", width=230, minwidth=150, stretch=False)
        t.column("value", width=220, minwidth=120, stretch=True)
        t.column("msgs", width=56, minwidth=46, anchor="e", stretch=False)
        t.column("time", width=78, minwidth=70, stretch=False)
        self.tree = self._with_bars(frame, t, horizontal=True)
        for origin, color in ORIGIN_COLORS.items():
            self.tree.tag_configure(origin, foreground=color)
        self.tree.tag_configure("branch", foreground="#495057", font=(FONT_FAMILY, 10, "bold"))
        self.tree.tag_configure("fresh", background="#fff3bf")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        self.empty_lbl = ttk.Label(frame, text=EMPTY_TEXT, style="Hint.TLabel", justify="center",
                                   anchor="center", background="white")
        legend = ttk.Frame(box)
        legend.pack(fill="x", pady=(8, 0))
        for origin, text in (("station", "PC → phone  (cmd)"), ("client", "phone → PC  (evt, status)"),
                             ("will", "last will  (phone lost)")):
            dot = ttk.Frame(legend)
            dot.pack(side="left", padx=(0, 16))
            ttk.Label(dot, text="●", foreground=ORIGIN_COLORS[origin], font=(FONT_FAMILY, 11)).pack(side="left")
            ttk.Label(dot, text=text, style="Hint.TLabel").pack(side="left", padx=(4, 0))
        ttk.Label(legend, text="🔒 PWD/PACK hidden", style="Hint.TLabel").pack(side="right")
        return box

    def _build_clients(self, parent) -> ttk.Labelframe:
        box = self._card(parent, "Clients")
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        cols = (("client", "Client", 150, False), ("state", "State", 95, False), ("address", "Address", 95, False),
                ("since", "Connected", 90, False), ("sent", "Sent", 50, False), ("subs", "Subscriptions", 140, True))
        t = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", height=3, style="Data.Treeview")
        for col, text, width, stretch in cols:
            t.heading(col, text=text, anchor="w")
            t.column(col, width=width, minwidth=44, stretch=stretch)
        self.clients = self._with_bars(frame, t, horizontal=True)
        self.clients.tag_configure("offline", foreground=MUTED)
        self.clients.tag_configure("online", foreground="#212529")
        self.clients.tag_configure("paired", foreground=ORIGIN_COLORS["client"])
        return box

    def _build_detail(self, parent) -> ttk.Labelframe:
        box = self._card(parent, "Message")
        head = ttk.Frame(box)
        head.pack(fill="x")
        self.topic_var = tk.StringVar(value="")
        ttk.Entry(head, textvariable=self.topic_var, state="readonly", font=TOPIC_FONT).pack(side="left", fill="x", expand=True)
        ttk.Button(head, text="Copy", bootstyle="primary-link",
                   command=lambda: self._copy(self._selected or "", "Topic")).pack(side="left", padx=(4, 0))

        pills = ttk.Frame(box)
        pills.pack(fill="x", pady=(8, 0))
        self.dir_pill = StatusPill(pills, "", "secondary")
        self.qos_pill = StatusPill(pills, "", "secondary")
        self.retain_pill = StatusPill(pills, "RETAINED", "info")
        self.size_pill = StatusPill(pills, "", "light")

        meta = ttk.Frame(box)
        meta.pack(fill="x", pady=(8, 4))
        self.meta: dict[str, ttk.Label] = {}
        for col, (label, key) in enumerate((("Client", "client"), ("Time", "time"), ("Count", "count"))):
            ttk.Label(meta, text=label, style="Hint.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 16))
            lbl = ttk.Label(meta, text="—", font=LABEL_BOLD)
            lbl.grid(row=1, column=col, sticky="w", padx=(0, 16))
            self.meta[key] = lbl
            meta.columnconfigure(col, weight=1)

        pbar = ttk.Frame(box)
        pbar.pack(fill="x", pady=(6, 2))
        ttk.Label(pbar, text="PAYLOAD", style="Hint.TLabel").pack(side="left")
        ttk.Button(pbar, text="Copy payload", bootstyle="primary-link",
                   command=lambda: self._copy(self._shown.text() if self._shown else "", "Payload")).pack(side="right")
        self.latest_btn = ttk.Button(pbar, text="↻  Show latest", bootstyle="warning-link", command=self._show_latest)

        c = ttk.Style().colors
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=MONO_FONT, relief="flat", borderwidth=0, padx=12, pady=10,
                       highlightthickness=1, highlightbackground=c.border, highlightcolor=c.border,
                       state="disabled", background="#f8f9fb", foreground=c.fg, height=8, spacing1=1, spacing3=1)
        self.payload = self._with_bars(frame, text)
        self.payload.tag_configure("key", foreground="#1c64d1")
        self.payload.tag_configure("str", foreground="#1d8a4b")
        self.payload.tag_configure("num", foreground="#a23ac2")
        self.payload.tag_configure("muted", foreground=MUTED, font=(FONT_FAMILY, 10))
        return box

    def _build_history(self, parent) -> ttk.Labelframe:
        box = self._card(parent, "History  ·  newest first")
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        t = ttk.Treeview(frame, columns=("time", "dir", "value"), show="headings", selectmode="browse",
                         height=3, style="Data.Treeview")
        for col, text, width, stretch in (("time", "Time", 112, False), ("dir", "Direction", 112, False),
                                          ("value", "Value", 300, True)):
            t.heading(col, text=text, anchor="w")
            t.column(col, width=width, minwidth=60, stretch=stretch)
        self.history = self._with_bars(frame, t)
        for origin, color in ORIGIN_COLORS.items():
            self.history.tag_configure(origin, foreground=color)
        self.history.bind("<<TreeviewSelect>>", lambda _e: self._on_history())
        return box

    # ------------------------------------------------------- broker events ---

    def set_broker(self, address: str, root: str = "", running: bool = True) -> None:
        """Station (re)started: forget the old broker's clients (its retained topics are gone too)."""
        self.broker = (address, root, running)
        for c in self.model.clients.values():
            c.online = False
        self._refresh_clients()
        self._update_status()

    def on_traffic(self, kind: str, info: dict) -> None:
        """UI thread (posted by the broker hook)."""
        try:
            if kind == "message":
                self._on_message(make_message(info["topic"], info.get("payload", b""), info.get("qos", 0),
                                              info.get("retain", False), info.get("source", ""), info.get("origin", "station")))
            elif kind == "connect":
                self.model.on_connect(info.get("id", "?"), info.get("address", ""), info.get("keepalive", 0), info.get("will", ""))
            elif kind == "disconnect":
                self.model.on_disconnect(info.get("id", "?"))
            elif kind == "subscribe":
                self.model.on_subscribe(info.get("id", "?"), list(info.get("filters", [])))
            elif kind == "unsubscribe":
                self.model.on_unsubscribe(info.get("id", "?"), list(info.get("filters", [])))
            if kind != "message" and not self.paused:
                self._refresh_clients()
        except Exception:
            log.exception("MQTT view: could not show %s", kind)
        self._update_status()

    def _on_message(self, msg: Message) -> None:
        path = self.model.add(msg)
        self._last_msg = msg.time
        for node in path:
            self._branch_time[node.path] = msg.time
        if self.paused:
            self._missed += 1
            return
        needle = self.filter_var.get().strip()
        if needle and not self.model.matches(path[-1], needle):
            return
        for node in path:
            self._upsert(node)
        self._flash(path)
        self._update_empty()
        if msg.source:
            self._refresh_clients()
        if self._selected == msg.topic:
            self._fill_history(path[-1])
            if not self._pinned:
                self._show_message(msg)

    # ----------------------------------------------------------------- tree ---

    def _row(self, node: TopicNode) -> tuple[tuple, tuple]:
        last = node.last
        if last is not None:
            value = last.preview()
            if node.children:
                value += f"   (+{node.topic_count - 1} sub-topics)"
            tags = (last.origin,)
        else:
            n = node.topic_count
            value = f"{n} topic{'s' if n != 1 else ''}"
            tags = ("branch",)
        t = self._branch_time.get(node.path)
        return (value, node.total, t.strftime("%H:%M:%S") if t else ""), tags

    def _upsert(self, node: TopicNode) -> None:
        iid = _iid(node.path)
        values, tags = self._row(node)
        if self.tree.exists(iid):
            keep = ("fresh",) if "fresh" in self.tree.item(iid, "tags") else ()
            self.tree.item(iid, values=values, tags=tags + keep)
            return
        parent_iid = _iid(node.path.rpartition("/")[0]) if "/" in node.path else ""
        siblings = self.tree.get_children(parent_iid)
        index = sum(1 for s in siblings if self.tree.item(s, "text") < node.name)
        self.tree.insert(parent_iid, index, iid=iid, text=node.name, values=values, tags=tags, open=True)

    def _flash(self, path: list[TopicNode]) -> None:
        for node in path:
            iid = _iid(node.path)
            if not self.tree.exists(iid):
                continue
            gen = self._flash_gen.get(iid, 0) + 1
            self._flash_gen[iid] = gen
            tags = tuple(t for t in self.tree.item(iid, "tags") if t != "fresh")
            self.tree.item(iid, tags=tags + ("fresh",))
            self.after(FLASH_MS, self._unflash, iid, gen)

    def _unflash(self, iid: str, gen: int) -> None:
        if self._flash_gen.get(iid) != gen or not self.tree.exists(iid):
            return
        self.tree.item(iid, tags=tuple(t for t in self.tree.item(iid, "tags") if t != "fresh"))

    def _update_empty(self) -> None:
        """Placeholder over the tree while it has no rows."""
        if self.tree.get_children(""):
            self.empty_lbl.place_forget()
            return
        needle = self.filter_var.get().strip()
        self.empty_lbl.configure(text=f"No topic or value matches “{needle}”." if needle and self.model.messages else EMPTY_TEXT)
        self.empty_lbl.place(relx=0.5, rely=0.55, anchor="center")

    def _rebuild(self) -> None:
        open_state = {i: self.tree.item(i, "open") for i in self._all_items()}
        self.tree.delete(*self.tree.get_children(""))
        needle = self.filter_var.get().strip()

        def add(node: TopicNode, parent_iid: str) -> None:
            for name in sorted(node.children):
                child = node.children[name]
                if needle and not self.model.matches(child, needle):
                    continue
                values, tags = self._row(child)
                iid = _iid(child.path)
                self.tree.insert(parent_iid, "end", iid=iid, text=child.name, values=values, tags=tags,
                                 open=True if needle else open_state.get(iid, True))
                add(child, iid)

        add(self.model.root, "")
        if self._selected and self.tree.exists(_iid(self._selected)):
            self.tree.selection_set(_iid(self._selected))
            self.tree.see(_iid(self._selected))
        self._update_empty()
        self._refresh_clients()

    def _all_items(self, parent: str = ""):
        for iid in self.tree.get_children(parent):
            yield iid
            yield from self._all_items(iid)

    def _open_all(self, open_: bool) -> None:
        for iid in self._all_items():
            self.tree.item(iid, open=open_)

    def _schedule_filter(self, refilter: bool = True) -> None:
        """Show ✕ while there is filter text, else the hint (wide windows only); then re-filter."""
        self.filter_clear.pack_forget()
        self.filter_hint.pack_forget()
        if self.filter_var.get():
            self.filter_clear.pack(side="left", padx=(2, 0), before=self.paused_pill if self.paused_pill.winfo_manager() else None)
        elif self._hint_visible:
            self.filter_hint.pack(side="left", padx=(8, 0), before=self.paused_pill if self.paused_pill.winfo_manager() else None)
        if not refilter:
            return
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(250, self._apply_filter)

    def _apply_filter(self) -> None:
        self._filter_job = None
        self._rebuild()

    # --------------------------------------------------------- right side ---

    def _on_select(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0][2:]
        self._selected = path
        self._pinned = False
        self.latest_btn.pack_forget()
        self.topic_var.set(path)
        node = self.model.node(path)
        if node is None:
            return
        self._fill_history(node)
        if node.last is not None:
            self._show_message(node.last)
        else:
            self._show_message(None, f"No messages on this level itself.\n\n{node.topic_count} topics and "
                                     f"{node.total} messages below it - expand it in the tree.")

    def _fill_history(self, node: TopicNode) -> None:
        keep = self.history.selection()
        self.history.delete(*self.history.get_children())
        for i, m in enumerate(reversed(node.history)):
            self.history.insert("", "end", iid=f"h{len(node.history) - 1 - i}",
                                values=(m.stamp, m.direction, m.preview(140)), tags=(m.origin,))
        if keep and self.history.exists(keep[0]) and self._pinned:
            self.history.selection_set(keep[0])

    def _on_history(self) -> None:
        sel = self.history.selection()
        node = self.model.node(self._selected or "")
        if not sel or node is None:
            return
        index = int(sel[0][1:])
        if index >= len(node.history):
            return
        msg = node.history[index]
        self._pinned = msg is not node.last
        if self._pinned:
            self.latest_btn.pack(side="right", padx=(0, 8))
        else:
            self.latest_btn.pack_forget()
        self._show_message(msg)

    def _show_latest(self) -> None:
        self._pinned = False
        self.latest_btn.pack_forget()
        node = self.model.node(self._selected or "")
        if node and node.last:
            self.history.selection_remove(*self.history.selection())
            self._show_message(node.last)

    def _show_message(self, msg: Message | None, note: str = "") -> None:
        self._shown = msg
        for pill in (self.dir_pill, self.qos_pill, self.retain_pill, self.size_pill):
            pill.pack_forget()
        if msg is None:
            for lbl in self.meta.values():
                lbl.configure(text="—")
        else:
            text, style = ORIGIN_PILLS.get(msg.origin, (msg.direction, "secondary"))
            self.dir_pill.set(text, style)
            self.dir_pill.pack(side="left")
            self.qos_pill.set(f"QoS {msg.qos}", "secondary")
            self.qos_pill.pack(side="left", padx=(6, 0))
            if msg.retain:
                self.retain_pill.pack(side="left", padx=(6, 0))
            self.size_pill.set(_size(msg.size) + ("  ·  JSON" if msg.is_json else ""), "light")
            self.size_pill.pack(side="left", padx=(6, 0))
            client = msg.source or "this PC"
            paired = self.app.station.paired_id if self.app.station else None
            if msg.source and msg.source == paired:
                client += "  (paired)"
            node = self.model.node(msg.topic)
            self.meta["client"].configure(text=client)
            self.meta["time"].configure(text=msg.time.strftime("%H:%M:%S.%f" if self._narrow else "%d %b  %H:%M:%S.%f")[:-3])
            self.meta["count"].configure(text=f"{node.count if node else 1} message{'s' if not node or node.count != 1 else ''}")
        self.payload.configure(state="normal")
        self.payload.delete("1.0", "end")
        if msg is None:
            self.payload.insert("end", note, "muted")
        elif not msg.payload:
            self.payload.insert("end", "(empty payload - clears a retained message)", "muted")
        elif msg.is_json:
            self._insert_json(msg.text())
        else:
            self.payload.insert("end", msg.text(), "str")
        self.payload.configure(state="disabled")

    def _insert_json(self, text: str) -> None:
        """Pretty JSON with simple colouring: keys, strings, numbers/booleans."""
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            self.payload.insert("end", line[:len(line) - len(stripped)])
            if stripped.startswith('"') and '": ' in stripped:
                key, _, rest = stripped.partition('": ')
                self.payload.insert("end", key + '"', "key")
                self.payload.insert("end", ": ")
                stripped = rest
            value = stripped.rstrip("\n").rstrip(",")
            tail = stripped[len(value):]
            tag = "str" if value.startswith('"') else ("num" if value[:1].isdigit() or value[:1] == "-"
                                                        or value in ("true", "false", "null") else "")
            self.payload.insert("end", value, tag)
            self.payload.insert("end", tail)

    # -------------------------------------------------------------- clients ---

    def _refresh_clients(self) -> None:
        paired = self.app.station.paired_id if self.app.station else None
        names = getattr(getattr(self.app, "verify_view", None), "mobiles", {}) or {}
        self.clients.delete(*self.clients.get_children())
        for c in sorted(self.model.clients.values(), key=lambda c: (not c.online, c.id)):
            name = getattr(names.get(c.id), "name", "")
            if c.online:
                state = "●  paired" if c.id == paired else "●  online"
                tag = "paired" if c.id == paired else "online"
            else:
                state, tag = "○  offline", "offline"
            self.clients.insert("", "end", values=(
                name or c.id, state, c.address,
                c.since.strftime("%H:%M:%S") if c.since else "", c.sent, ", ".join(c.subscriptions) or "—"),
                tags=(tag,))

    # -------------------------------------------------------------- toolbar ---

    def _update_status(self) -> None:
        address, root, running = self.broker
        online = sum(1 for c in self.model.clients.values() if c.online)
        known = len(self.model.clients)
        rows = {
            "broker": (("●  " if running else "○  ") + address, root or ("running" if running else "not running")),
            "clients": (str(online), f"client{'s' if online != 1 else ''}  ·  {known} seen" if known else "no clients yet"),
            "topics": (str(self.model.root.topic_count), "in the tree"),
            "messages": (str(self.model.messages), f"{self._missed} while paused" if self.paused else "since start / clear"),
            "data": (_size(self.model.bytes), "payloads"),
            "last": (self._last_msg.strftime("%H:%M:%S") if self._last_msg else "—",
                     self._last_msg.strftime("%d %b %Y") if self._last_msg else "no messages yet"),
        }
        for key, (value, sub) in rows.items():
            _card, v, s = self.stats[key]
            v.configure(text=value)
            s.configure(text=sub)
        self.stats["broker"][1].configure(foreground="#1d8a4b" if running else "#d9480f")
        if self.paused:
            self.paused_pill.set(f"⏸  PAUSED  ·  {self._missed} new", "warning")
            self.paused_pill.pack(side="left", padx=(12, 0))
        else:
            self.paused_pill.pack_forget()

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_btn.configure(text="▶  Resume" if self.paused else "⏸  Pause",
                                 bootstyle="success" if self.paused else "warning-outline")
        if not self.paused:
            self._missed = 0
            self._rebuild()
            if self._selected and (node := self.model.node(self._selected)):
                self._fill_history(node)
                if not self._pinned and node.last:
                    self._show_message(node.last)
        self._update_status()

    def _clear(self) -> None:
        self.model.clear(keep_clients=True)
        self._branch_time.clear()
        self._selected = None
        self._pinned = False
        self._last_msg = None
        self.tree.delete(*self.tree.get_children(""))
        self.history.delete(*self.history.get_children())
        self.topic_var.set("")
        self._show_message(None, "Cleared. New messages appear as they arrive.")
        self._missed = 0
        self._update_empty()
        self._update_status()

    def _export(self) -> None:
        rows = self.model.export()
        if not rows:
            Messagebox.show_info("No messages yet.", "MQTT", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Save MQTT messages", defaultextension=".json",
            initialfile=datetime.now().strftime("mqtt_%Y%m%d_%H%M%S.json"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            Messagebox.show_error(f"Could not save: {exc}", "MQTT", parent=self)
            return
        self.app.set_status(f"Saved {len(rows)} MQTT messages to {path}")

    def _copy(self, text: str, what: str = "Text") -> None:
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.set_status(f"{what} copied to the clipboard")
