"""Station <-> mobile protocol on top of the embedded broker.

Topics (S = station id, M = mobile id = the mobile's MQTT client id):

  mobile -> station   sytrak/S/mobile/M/evt/hello        {"name": "Pixel 7"}
                      sytrak/S/mobile/M/evt/scan         {"qr": "<scanned text>"}
                      sytrak/S/mobile/M/evt/nfc_result   {"seq": "0001", "ok": true, "error": ""}
                      sytrak/S/mobile/M/status           "online" / "offline" (last will)
  station -> mobile   sytrak/S/mobile/M/cmd/session        {"state": "paired"|"unpaired", ...}  (retained)
                      sytrak/S/mobile/M/cmd/verify_request {"mac": "E9EBA0ED7C7C", "seq": "0001", "device_type": "..."}
                      sytrak/S/mobile/M/cmd/scan_ack       {"accepted": true, "reason": ""}
                      sytrak/S/mobile/M/cmd/verify_result  {"ok": true, "mac": "...", "seq": "0001", "reason": ""}
                      sytrak/S/mobile/M/cmd/nfc_write      {"seq": "0001", "mac": "...", "data": "..."}

Every station hosts its own broker, so several stations (each with its own
mobile) can work side by side on the same network. Only the mobile the
operator paired with is accepted.

Events reach the UI through ``on_event(kind, data)`` - called on the broker
thread, so the UI must marshal them.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from .network import DiscoveryBeacon, EmbeddedBroker

log = logging.getLogger(__name__)

TOPIC_ROOT = "sytrak"
BROKER_USERNAME = "sytrak"


def _decode(payload: bytes) -> dict:
    text = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"raw": text}
    except ValueError:
        return {"raw": text}


class StationService:
    def __init__(
        self,
        station_id: str,
        name: str,
        port: int,
        discovery_port: int,
        password: str,
        on_event: Callable[[str, dict], None],
    ):
        self.station_id = station_id
        self.name = name
        self.port = port
        self.password = password
        self.on_event = on_event
        self.paired_id: str | None = None
        self.broker = EmbeddedBroker(
            port=port,
            username=BROKER_USERNAME if password else None,
            password=password or None,
            on_connect=lambda cid, addr: self.on_event("mobile_connected", {"id": cid, "address": addr}),
            on_disconnect=lambda cid: self.on_event("mobile_disconnected", {"id": cid}),
            on_publish=self._on_publish,
        )
        self.beacon = DiscoveryBeacon(discovery_port, self._beacon_info)

    def start(self) -> None:
        self.broker.start()  # raises OSError if the port is taken
        self.beacon.start()

    def stop(self) -> None:
        if self.paired_id:
            self.unpair()
        self.beacon.stop()
        self.broker.stop()

    def _beacon_info(self) -> dict:
        return {
            "type": "sytrak-gen",
            "v": 1,
            "station": self.station_id,
            "name": self.name,
            "mqtt_port": self.port,
            "auth": bool(self.password),
            "paired": self.paired_id or "",
        }

    def topic(self, mobile_id: str, *parts: str) -> str:
        return "/".join((TOPIC_ROOT, self.station_id, "mobile", mobile_id, *parts))

    def send(self, mobile_id: str, name: str, data: dict, retain: bool = False) -> None:
        self.broker.publish(self.topic(mobile_id, "cmd", name), json.dumps(data), qos=1, retain=retain)

    # ------------------------------------------------------------ pairing ---

    def pair(self, mobile_id: str) -> None:
        if self.paired_id and self.paired_id != mobile_id:
            self.unpair()
        self.paired_id = mobile_id
        self.send(mobile_id, "session", {"state": "paired", "station": self.station_id, "name": self.name}, retain=True)

    def unpair(self) -> None:
        if self.paired_id:
            self.send(self.paired_id, "session", {"state": "unpaired", "station": self.station_id, "name": self.name}, retain=True)
        self.paired_id = None

    def request_verify(self, mac: str, seq: str, device_type: str = "") -> bool:
        """Ask the paired mobile to scan the sticker of this device (it shows the expected MAC/SEQ)."""
        if not self.paired_id:
            return False
        self.send(self.paired_id, "verify_request", {"mac": mac, "seq": seq, "device_type": device_type})
        return True

    def send_verify_result(self, ok: bool, mac: str, seq: str, reason: str = "") -> None:
        if self.paired_id:
            self.send(self.paired_id, "verify_result", {"ok": ok, "mac": mac, "seq": seq, "reason": reason})

    def request_nfc_write(self, seq: str, mac: str, data: str) -> bool:
        if not self.paired_id:
            return False
        self.send(self.paired_id, "nfc_write", {"seq": seq, "mac": mac, "data": data})
        return True

    # ------------------------------------------------- broker thread hook ---

    def _on_publish(self, client_id: str, topic: str, payload: bytes) -> None:
        prefix = f"{TOPIC_ROOT}/{self.station_id}/mobile/"
        if not topic.startswith(prefix):
            return
        parts = topic[len(prefix):].split("/")
        if len(parts) != 3 or parts[1] != "evt":
            return
        mobile_id, _, event = parts
        if mobile_id != client_id:  # a mobile may only speak for itself
            log.warning("Ignoring %s published by %s", topic, client_id)
            return
        data = _decode(payload)

        if event == "hello":
            self.on_event("mobile_hello", {"id": mobile_id, "name": str(data.get("name", ""))})
            if mobile_id == self.paired_id:  # re-sync a reconnecting mobile
                self.send(mobile_id, "session", {"state": "paired", "station": self.station_id, "name": self.name}, retain=True)
        elif event == "scan":
            qr = str(data.get("qr", data.get("raw", "")))
            if mobile_id != self.paired_id:
                self.send(mobile_id, "scan_ack", {"accepted": False, "reason": "Not paired with this station"})
                self.on_event("scan_rejected", {"id": mobile_id, "qr": qr})
            else:
                self.send(mobile_id, "scan_ack", {"accepted": True, "reason": ""})
                self.on_event("scan", {"id": mobile_id, "qr": qr})
        elif event == "nfc_result":
            self.on_event("nfc_result", {"id": mobile_id, **data})
