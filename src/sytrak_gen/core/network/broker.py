"""Minimal embedded MQTT 3.1.1 broker (asyncio, runs in a background thread).

Supports what the mobile app needs: CONNECT (optional username/password),
PUBLISH QoS 0/1/2 inbound (delivered at most QoS 1), SUBSCRIBE/UNSUBSCRIBE
with + and # wildcards, retained messages, last-will, keepalive and PING.
Sessions are always clean (no offline queueing).

The host application hooks in directly: ``on_publish`` sees every message a
client publishes and ``publish()`` injects messages from any thread.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14


class ProtocolError(Exception):
    pass


def topic_matches(filt: str, topic: str) -> bool:
    f, t = filt.split("/"), topic.split("/")
    if topic.startswith("$") and f[0] in ("+", "#"):
        return False
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


def _encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _packet(header: int, body: bytes = b"") -> bytes:
    return bytes([header]) + _encode_len(len(body)) + body


def _mqtt_str(data: bytes) -> bytes:
    return len(data).to_bytes(2, "big") + data


def _read_str(body: bytes, pos: int) -> tuple[bytes, int]:
    n = int.from_bytes(body[pos:pos + 2], "big")
    end = pos + 2 + n
    if end > len(body):
        raise ProtocolError("truncated string")
    return body[pos + 2:end], end


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = (await reader.readexactly(1))[0]
    length, mult = 0, 1
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    else:
        raise ProtocolError("bad remaining length")
    return header, (await reader.readexactly(length)) if length else b""


@dataclass
class _Client:
    id: str
    address: str
    writer: asyncio.StreamWriter
    subs: dict[str, int] = field(default_factory=dict)
    will: tuple[str, bytes, int, bool] | None = None
    next_pid: int = 1

    def send(self, data: bytes) -> None:
        try:
            self.writer.write(data)
        except Exception:  # connection already gone
            pass

    def deliver(self, topic: str, payload: bytes, qos: int, retain: bool = False) -> None:
        body = _mqtt_str(topic.encode())
        if qos:
            body += self.next_pid.to_bytes(2, "big")
            self.next_pid = self.next_pid % 0xFFFF + 1
        self.send(_packet((PUBLISH << 4) | (qos << 1) | int(retain), body + payload))


class EmbeddedBroker:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        on_connect: Callable[[str, str], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        on_publish: Callable[[str, str, bytes], None] | None = None,
    ):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.on_connect, self.on_disconnect, self.on_publish = on_connect, on_disconnect, on_publish
        # Monitoring (MQTT tab): on_traffic(kind, info) for "message", "connect", "disconnect",
        # "subscribe", "unsubscribe" - called on the broker thread, must not block.
        self.on_traffic: Callable[[str, dict], None] | None = None
        self._clients: dict[str, _Client] = {}
        self._retained: dict[str, tuple[bytes, int]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.base_events.Server | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._anon = 0

    # ------------------------------------------------------------ control ---

    @property
    def running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def start(self) -> None:
        """Start listening; raises OSError if the port cannot be bound."""
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="mqtt-broker")
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise self._error

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)
        self._thread = None

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        """Thread-safe publish from the host application."""
        data = payload.encode() if isinstance(payload, str) else payload
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._route, topic, data, qos, retain)

    def client_ids(self) -> list[str]:
        return list(self._clients)

    def _traffic(self, kind: str, **info) -> None:
        hook = self.on_traffic
        if hook is None:
            return
        try:
            hook(kind, info)
        except Exception:
            log.exception("on_traffic hook failed")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            self._server = loop.run_until_complete(asyncio.start_server(self._handle, self.host, self.port))
        except OSError as exc:
            self._error = exc
            self._loop = None
            loop.close()
            self._ready.set()
            return
        log.info("MQTT broker listening on %s:%d", self.host, self.port)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._server.close()
            for client in list(self._clients.values()):
                client.writer.close()
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()

            async def drain() -> None:
                # gather() must run inside the loop: Python 3.14 has no implicit loop in this thread.
                await asyncio.gather(*tasks, return_exceptions=True)

            loop.run_until_complete(drain())
            loop.close()
            self._loop = None
            self._clients.clear()
            log.info("MQTT broker stopped")

    # ------------------------------------------------------------ routing ---

    def _route(self, topic: str, payload: bytes, qos: int, retain: bool,
               source: str = "", kind: str = "station") -> None:
        """Deliver to subscribers. source = publishing client id ("" = this app); kind = station|client|will."""
        self._traffic("message", topic=topic, payload=payload, qos=qos, retain=retain, source=source, origin=kind)
        if retain:
            if payload:
                self._retained[topic] = (payload, qos)
            else:
                self._retained.pop(topic, None)
        for client in list(self._clients.values()):
            granted = [q for f, q in client.subs.items() if topic_matches(f, topic)]
            if granted:
                client.deliver(topic, payload, min(qos, max(granted), 1))

    # ------------------------------------------------------------ session ---

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        address = peer[0] if peer else "?"
        client: _Client | None = None
        clean_exit = False
        try:
            header, body = await asyncio.wait_for(_read_packet(reader), 10)
            if header >> 4 != CONNECT:
                return
            client, keepalive = self._accept(body, address, writer)
            if client is None:
                return
            timeout = keepalive * 1.5 if keepalive else None
            while True:
                header, body = await asyncio.wait_for(_read_packet(reader), timeout)
                kind = header >> 4
                if kind == PUBLISH:
                    self._on_publish_packet(client, header, body)
                elif kind == PUBREL:
                    client.send(_packet(PUBCOMP << 4, body[:2]))
                elif kind == SUBSCRIBE:
                    self._on_subscribe(client, body)
                elif kind == UNSUBSCRIBE:
                    self._on_unsubscribe(client, body)
                elif kind == PINGREQ:
                    client.send(_packet(PINGRESP << 4))
                elif kind == DISCONNECT:
                    clean_exit = True
                    break
                elif kind in (PUBACK, PUBREC, PUBCOMP):
                    continue
                else:
                    raise ProtocolError(f"unexpected packet type {kind}")
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ProtocolError, IndexError, UnicodeDecodeError) as exc:
            log.debug("Client %s dropped: %r", client.id if client else address, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MQTT client handler error")
        finally:
            if client and self._clients.get(client.id) is client:
                del self._clients[client.id]
                self._traffic("disconnect", id=client.id, clean=clean_exit)
                if client.will and not clean_exit:
                    self._route(*client.will, source=client.id, kind="will")
                log.info("MQTT client disconnected: %s", client.id)
                if self.on_disconnect:
                    self.on_disconnect(client.id)
            writer.close()

    def _accept(self, body: bytes, address: str, writer: asyncio.StreamWriter) -> tuple[_Client | None, int]:
        _proto, pos = _read_str(body, 0)
        level, flags = body[pos], body[pos + 1]
        keepalive = int.from_bytes(body[pos + 2:pos + 4], "big")
        pos += 4
        if level not in (3, 4):
            writer.write(_packet(CONNACK << 4, b"\x00\x01"))  # unacceptable protocol version
            return None, 0
        raw_id, pos = _read_str(body, pos)
        will = None
        if flags & 0x04:
            will_topic, pos = _read_str(body, pos)
            will_msg, pos = _read_str(body, pos)
            will = (will_topic.decode(), will_msg, (flags >> 3) & 0x03, bool(flags & 0x20))
        username = password = None
        if flags & 0x80:
            raw_user, pos = _read_str(body, pos)
            username = raw_user.decode()
        if flags & 0x40:
            raw_pass, pos = _read_str(body, pos)
            password = raw_pass.decode(errors="replace")
        if self.password and (username != self.username or password != self.password):
            writer.write(_packet(CONNACK << 4, b"\x00\x04"))  # bad username or password
            log.warning("MQTT auth failed from %s", address)
            return None, 0

        client_id = raw_id.decode()
        if not client_id:
            self._anon += 1
            client_id = f"anon-{self._anon}"
        old = self._clients.get(client_id)
        if old:  # session takeover
            old.writer.close()
        client = _Client(id=client_id, address=address, writer=writer, will=will)
        self._clients[client_id] = client
        writer.write(_packet(CONNACK << 4, b"\x00\x00"))
        log.info("MQTT client connected: %s from %s", client_id, address)
        self._traffic("connect", id=client_id, address=address, keepalive=keepalive,
                      will=will[0] if will else "", takeover=bool(old))
        if self.on_connect:
            self.on_connect(client_id, address)
        return client, keepalive

    def _on_publish_packet(self, client: _Client, header: int, body: bytes) -> None:
        qos, retain = (header >> 1) & 0x03, bool(header & 0x01)
        raw_topic, pos = _read_str(body, 0)
        if qos:
            pid = body[pos:pos + 2]
            pos += 2
            client.send(_packet((PUBACK if qos == 1 else PUBREC) << 4, pid))
        topic, payload = raw_topic.decode(), body[pos:]
        self._route(topic, payload, qos, retain, source=client.id, kind="client")
        if self.on_publish:
            try:
                self.on_publish(client.id, topic, payload)
            except Exception:
                log.exception("on_publish hook failed")

    def _on_subscribe(self, client: _Client, body: bytes) -> None:
        pid, pos = body[:2], 2
        granted, filters = bytearray(), []
        while pos < len(body):
            raw, pos = _read_str(body, pos)
            qos = min(body[pos] & 0x03, 1)
            pos += 1
            client.subs[raw.decode()] = qos
            granted.append(qos)
            filters.append((raw.decode(), qos))
        client.send(_packet((SUBACK << 4), pid + bytes(granted)))
        self._traffic("subscribe", id=client.id, filters=[f for f, _q in filters])
        for filt, qos in filters:
            for topic, (payload, msg_qos) in self._retained.items():
                if topic_matches(filt, topic):
                    client.deliver(topic, payload, min(qos, msg_qos), retain=True)

    def _on_unsubscribe(self, client: _Client, body: bytes) -> None:
        pid, pos = body[:2], 2
        removed = []
        while pos < len(body):
            raw, pos = _read_str(body, pos)
            client.subs.pop(raw.decode(), None)
            removed.append(raw.decode())
        client.send(_packet(UNSUBACK << 4, pid))
        self._traffic("unsubscribe", id=client.id, filters=removed)
