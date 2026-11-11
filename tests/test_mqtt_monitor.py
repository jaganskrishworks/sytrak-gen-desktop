"""MQTT tab model + the broker's traffic hook."""
import json
import queue
import socket
import time

import paho.mqtt.client as mqtt

from sytrak_gen.core.mqtt_monitor import MASK, TrafficModel, make_message, redact
from sytrak_gen.core.station import StationService


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_redact_masks_tag_passwords_only():
    data = {"job_id": "j-1", "ok": True, "pwd": "A1B2C3D4", "pack": "E5F6", "write": {"page": 4},
            "password": "ok", "nested": [{"pack": "1234"}], "empty_pwd": None}
    out = redact(data)
    assert out["pwd"] == MASK and out["pack"] == MASK and out["nested"][0]["pack"] == MASK
    assert out["password"] == "ok"          # nfc_done check result, not a secret
    assert out["job_id"] == "j-1" and out["write"] == {"page": 4}
    assert data["pwd"] == "A1B2C3D4"        # original untouched


def test_message_never_keeps_the_secret():
    m = make_message("sytrak/S/mobile/M/cmd/nfc_job_reply", json.dumps({"pwd": "A1B2C3D4", "pack": "E5F6"}).encode())
    assert b"A1B2C3D4" not in m.payload and "A1B2C3D4" not in m.text() and "E5F6" not in m.preview()
    assert m.is_json and m.data["pwd"] == MASK


def test_payload_formats():
    assert make_message("t", b"online").text() == "online"
    assert make_message("t", b"\xff\x00").text() == "hex: FF 00"
    assert make_message("t", b"").preview() == "(empty)"
    long = make_message("t", json.dumps({"pages": "00" * 200}).encode())
    assert len(long.preview(50)) == 50 and long.preview(50).endswith("…")


def test_topic_tree_counts_history_and_retained():
    model = TrafficModel()
    base = "sytrak/SG/mobile/mob-a"
    path = model.add(make_message(f"{base}/status", b"online", retain=True, source="mob-a", origin="client"))
    assert [n.name for n in path] == ["sytrak", "SG", "mobile", "mob-a", "status"]
    model.add(make_message(f"{base}/cmd/session", b'{"state": "paired"}', retain=True))
    model.add(make_message(f"{base}/evt/hello", b'{"name": "Pixel"}', source="mob-a", origin="client"))
    model.add(make_message(f"{base}/status", b"offline", retain=True, source="mob-a", origin="will"))

    assert model.messages == 4 and model.root.topic_count == 3
    status = model.node(f"{base}/status")
    assert status.count == 2 and status.last.text() == "offline" and status.retained.origin == "will"
    assert model.node(base).total == 4 and model.node(f"{base}/cmd").total == 1
    assert model.clients["mob-a"].sent == 3
    model.add(make_message(f"{base}/status", b"", retain=True))      # empty retained = cleared
    assert status.retained is None

    assert model.matches(model.node("sytrak"), "hello")              # topic text
    assert model.matches(model.node(f"{base}/evt"), "pixel")          # payload text
    assert not model.matches(model.node(f"{base}/cmd"), "pixel")
    assert [r["topic"].rsplit("/", 1)[-1] for r in model.export()] == ["status", "session", "hello", "status", "status"]

    model.clear()
    assert model.messages == 0 and not model.root.children and "mob-a" in model.clients


def test_clients():
    model = TrafficModel()
    model.on_connect("mob-a", "10.0.0.5", 10, "sytrak/S/mobile/mob-a/status")
    model.on_subscribe("mob-a", ["sytrak/S/mobile/mob-a/cmd/#"])
    c = model.clients["mob-a"]
    assert c.online and c.address == "10.0.0.5" and c.subscriptions == ["sytrak/S/mobile/mob-a/cmd/#"]
    model.on_unsubscribe("mob-a", ["sytrak/S/mobile/mob-a/cmd/#"])
    model.on_disconnect("mob-a")
    assert not c.online and c.subscriptions == []


def test_broker_reports_all_traffic():
    traffic: queue.Queue = queue.Queue()
    svc = StationService("SG-MON", "Mon", _free_port(), _free_port(), "", lambda k, d: None)
    svc.broker.on_traffic = lambda kind, info: traffic.put((kind, info))
    svc.start()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mob-x")
    c.will_set("sytrak/SG-MON/mobile/mob-x/status", "offline", qos=1, retain=True)
    try:
        c.connect("127.0.0.1", svc.port)
        c.loop_start()
        c.subscribe("sytrak/SG-MON/mobile/mob-x/cmd/#", qos=1)
        time.sleep(0.3)
        c.publish("sytrak/SG-MON/mobile/mob-x/evt/hello", json.dumps({"name": "X"}), qos=1)
        time.sleep(0.3)
        svc.pair("mob-x")
        time.sleep(0.3)
        c.socket().close()            # drop without DISCONNECT: the will fires
        time.sleep(0.5)
    finally:
        c.loop_stop()
        svc.stop()

    seen = []
    while not traffic.empty():
        seen.append(traffic.get())
    kinds = [k for k, _ in seen]
    assert kinds[0] == "connect" and "subscribe" in kinds and "disconnect" in kinds
    msgs = [(i["topic"].rsplit("/", 1)[-1], i["origin"], i["source"]) for k, i in seen if k == "message"]
    assert ("hello", "client", "mob-x") in msgs
    assert ("session", "station", "") in msgs
    assert ("status", "will", "mob-x") in msgs
