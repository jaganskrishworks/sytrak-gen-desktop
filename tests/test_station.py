"""End-to-end: real paho client <-> embedded broker <-> StationService."""
import json
import queue
import socket
import time

import paho.mqtt.client as mqtt
import pytest

from sytrak_gen.core.network import topic_matches
from sytrak_gen.core.station import StationService


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_nfc_result_requires_matching_read_back():
    from sytrak_gen.ui.views.verify_view import nfc_result_check

    assert nfc_result_check({"ok": True, "read_back": "E9EBA0ED7C7C"}, "E9EBA0ED7C7C") == (True, "")
    assert not nfc_result_check({"ok": True, "read_back": "E9EBA0ED7C"}, "E9EBA0ED7C7C")[0]   # truncated write
    assert not nfc_result_check({"ok": True}, "E9EBA0ED7C7C")[0]                             # old app, no read-back
    assert nfc_result_check({"ok": False, "error": "Tag is read-only"}, "x") == (False, "Tag is read-only")


def test_topic_matches():
    assert topic_matches("a/+/c", "a/b/c")
    assert topic_matches("a/#", "a")
    assert topic_matches("a/#", "a/b/c")
    assert not topic_matches("a/+", "a/b/c")
    assert not topic_matches("#", "$SYS/x")


@pytest.fixture
def station():
    events: queue.Queue = queue.Queue()
    svc = StationService("SG-TEST", "Test PC", _free_port(), _free_port(), "", lambda k, d: events.put((k, d)))
    svc.start()
    yield svc, events
    svc.stop()


def _wait(events, kind, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            k, d = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if k == kind:
            return d
    raise AssertionError(f"no {kind} event")


def _mobile(port, mid, inbox):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=mid)
    c.on_message = lambda _c, _u, m: inbox.put((m.topic.rsplit("/", 1)[-1], json.loads(m.payload)))
    c.connect("127.0.0.1", port)
    c.loop_start()
    c.subscribe(f"sytrak/SG-TEST/mobile/{mid}/cmd/#", qos=1)
    return c


def test_pairing_scan_and_nfc(station):
    svc, events = station
    inbox_a, inbox_b = queue.Queue(), queue.Queue()
    a = _mobile(svc.port, "mob-a", inbox_a)
    b = _mobile(svc.port, "mob-b", inbox_b)
    try:
        assert _wait(events, "mobile_connected")["id"] in ("mob-a", "mob-b")
        a.publish("sytrak/SG-TEST/mobile/mob-a/evt/hello", json.dumps({"name": "Phone A"}), qos=1)
        assert _wait(events, "mobile_hello") == {"id": "mob-a", "name": "Phone A"}

        svc.pair("mob-a")
        assert inbox_a.get(timeout=3) == ("session", {"state": "paired", "station": "SG-TEST", "name": "Test PC"})

        # Unpaired mobile is rejected, paired mobile accepted.
        b.publish("sytrak/SG-TEST/mobile/mob-b/evt/scan", json.dumps({"qr": "x"}), qos=1)
        assert _wait(events, "scan_rejected")["id"] == "mob-b"
        assert inbox_b.get(timeout=3)[1]["accepted"] is False

        qr = '{"mac":"E9:EB:A0:ED:7C:7C","seq":"0001"}'
        a.publish("sytrak/SG-TEST/mobile/mob-a/evt/scan", json.dumps({"qr": qr}), qos=1)
        assert _wait(events, "scan") == {"id": "mob-a", "qr": qr}
        assert inbox_a.get(timeout=3) == ("scan_ack", {"accepted": True, "reason": ""})

        # A mobile cannot publish on behalf of another.
        b.publish("sytrak/SG-TEST/mobile/mob-a/evt/scan", json.dumps({"qr": "spoof"}), qos=1)

        assert svc.request_nfc_write("0001", "E9:EB:A0:ED:7C:7C", qr)
        cmd, data = inbox_a.get(timeout=3)
        assert cmd == "nfc_write" and data["seq"] == "0001"
        a.publish("sytrak/SG-TEST/mobile/mob-a/evt/nfc_result", json.dumps({"seq": "0001", "ok": True}), qos=1)
        assert _wait(events, "nfc_result") == {"id": "mob-a", "seq": "0001", "ok": True}
    finally:
        for c in (a, b):
            c.disconnect()
            c.loop_stop()
    assert _wait(events, "mobile_disconnected")


def test_password_required():
    port = _free_port()
    svc = StationService("SG-T2", "T2", port, _free_port(), "secret", lambda k, d: None)
    svc.start()
    try:
        bad = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x")
        bad.connect("127.0.0.1", port)
        bad.loop(timeout=2)
        assert not bad.is_connected()
        good = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="y")
        good.username_pw_set("sytrak", "secret")
        good.connect("127.0.0.1", port)
        good.loop(timeout=2)
        assert good.is_connected()
        good.disconnect()
    finally:
        svc.stop()
