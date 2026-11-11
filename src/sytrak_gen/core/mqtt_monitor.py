"""What the MQTT tab shows: every message through the embedded broker, as a topic tree.

Thread-safe to feed from the broker thread? No - the UI marshals ``on_traffic`` calls onto
the Tk thread first; this model is plain data.

Secrets never reach the screen: tag passwords (``pwd`` / ``pack``) in cmd/nfc_job_reply are
replaced by a mask before anything is stored.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

HISTORY_PER_TOPIC = 100
MASK = "•••• (hidden)"
SECRET_KEYS = {"pwd", "pack", "secret"}   # evt/nfc_done "password" is only a check result
DIRECTIONS = {"station": "PC → phone", "client": "phone → PC", "will": "last will (phone lost)"}


def redact(value):
    """Copy of a JSON value with secret fields masked (any depth)."""
    if isinstance(value, dict):
        return {k: (MASK if k.lower() in SECRET_KEYS and v not in (None, "", False) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass
class Message:
    time: datetime
    topic: str
    payload: bytes            # already redacted
    qos: int
    retain: bool
    source: str               # publishing client id ("" = this PC)
    origin: str               # station | client | will
    is_json: bool = False
    data: object = None       # parsed + redacted JSON
    seq: int = 0              # arrival order (set by TrafficModel.add)

    @property
    def direction(self) -> str:
        return DIRECTIONS.get(self.origin, self.origin)

    @property
    def size(self) -> int:
        return len(self.payload)

    @property
    def stamp(self) -> str:
        return self.time.strftime("%H:%M:%S.%f")[:-3]

    def text(self) -> str:
        """Payload for reading: pretty JSON, else text, else hex."""
        if self.is_json:
            return json.dumps(self.data, indent=2, ensure_ascii=False)
        try:
            return self.payload.decode("utf-8")
        except UnicodeDecodeError:
            return "hex: " + self.payload.hex(" ").upper()

    def preview(self, limit: int = 90) -> str:
        if self.is_json:
            s = json.dumps(self.data, separators=(", ", ": "), ensure_ascii=False)
        else:
            s = self.text().replace("\n", " ")
        if not self.payload:
            s = "(empty)"
        return s if len(s) <= limit else s[:limit - 1] + "…"


def make_message(topic: str, payload: bytes, qos: int = 0, retain: bool = False,
                 source: str = "", origin: str = "station", time: datetime | None = None) -> Message:
    """Build a Message with the payload parsed and secrets masked."""
    is_json, data = False, None
    try:
        data = json.loads(payload.decode("utf-8"))
        is_json = isinstance(data, (dict, list))
    except (UnicodeDecodeError, ValueError):
        pass
    if is_json:
        data = redact(data)
        payload = json.dumps(data, ensure_ascii=False).encode()
    return Message(time or datetime.now(), topic, payload, qos, retain, source, origin, is_json, data)


@dataclass
class TopicNode:
    name: str
    path: str                                   # "" for the root
    children: dict[str, "TopicNode"] = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_PER_TOPIC))
    count: int = 0                              # messages on exactly this topic
    total: int = 0                              # ... including all sub-topics
    retained: Message | None = None             # current retained value (cleared by an empty retain)

    @property
    def last(self) -> Message | None:
        return self.history[-1] if self.history else None

    @property
    def topic_count(self) -> int:
        """Topics with messages at or below this node."""
        return (1 if self.count else 0) + sum(c.topic_count for c in self.children.values())


@dataclass
class Client:
    id: str
    address: str = ""
    online: bool = False
    since: datetime | None = None
    keepalive: int = 0
    will: str = ""
    subscriptions: list[str] = field(default_factory=list)
    sent: int = 0
    last_seen: datetime | None = None


class TrafficModel:
    def __init__(self):
        self.root = TopicNode("", "")
        self.clients: dict[str, Client] = {}
        self.messages = 0
        self.bytes = 0

    def clear(self, keep_clients: bool = True) -> None:
        self.root = TopicNode("", "")
        self.messages = self.bytes = 0
        if not keep_clients:
            self.clients.clear()

    def node(self, topic: str) -> TopicNode | None:
        node = self.root
        for part in topic.split("/"):
            node = node.children.get(part)
            if node is None:
                return None
        return node

    def add(self, msg: Message) -> list[TopicNode]:
        """Store a message; returns the nodes on its path (root's children first, leaf last)."""
        node, path = self.root, []
        node.total += 1
        parts = msg.topic.split("/")
        for i, part in enumerate(parts):
            child = node.children.get(part)
            if child is None:
                child = node.children[part] = TopicNode(part, "/".join(parts[:i + 1]))
            child.total += 1
            path.append(child)
            node = child
        node.count += 1
        self.messages += 1
        msg.seq = self.messages
        node.history.append(msg)
        if msg.retain:
            node.retained = msg if msg.payload else None
        self.bytes += msg.size
        if msg.source:
            c = self.clients.setdefault(msg.source, Client(msg.source))
            c.sent += 1
            c.last_seen = msg.time
        return path

    def on_connect(self, cid: str, address: str = "", keepalive: int = 0, will: str = "", **_):
        c = self.clients.setdefault(cid, Client(cid))
        c.address, c.online, c.since, c.keepalive, c.will = address, True, datetime.now(), keepalive, will
        c.subscriptions = []
        c.last_seen = c.since
        return c

    def on_disconnect(self, cid: str, **_):
        c = self.clients.setdefault(cid, Client(cid))
        c.online = False
        c.last_seen = datetime.now()
        return c

    def on_subscribe(self, cid: str, filters: list[str], **_):
        c = self.clients.setdefault(cid, Client(cid))
        c.subscriptions = sorted(set(c.subscriptions) | set(filters))
        return c

    def on_unsubscribe(self, cid: str, filters: list[str], **_):
        c = self.clients.setdefault(cid, Client(cid))
        c.subscriptions = [s for s in c.subscriptions if s not in filters]
        return c

    def walk(self, node: TopicNode | None = None):
        """All nodes depth-first (root excluded), children sorted by name."""
        node = node or self.root
        for name in sorted(node.children):
            child = node.children[name]
            yield child
            yield from self.walk(child)

    def matches(self, node: TopicNode, needle: str) -> bool:
        """Filter: the topic path (or a sub-topic) contains the text, or its last payload does."""
        needle = needle.lower()
        if needle in node.path.lower():
            return True
        last = node.last
        if last and needle in last.preview(500).lower():
            return True
        return any(self.matches(c, needle) for c in node.children.values())

    def export(self) -> list[dict]:
        """Every stored message (oldest first) for saving as JSON - secrets already masked."""
        msgs = sorted((m for node in self.walk() for m in node.history), key=lambda m: m.seq)
        return [{"time": m.time.isoformat(timespec="milliseconds"), "topic": m.topic,
                 "direction": m.direction, "client": m.source or "PC", "qos": m.qos,
                 "retain": m.retain, "payload": m.data if m.is_json else m.text()} for m in msgs]
