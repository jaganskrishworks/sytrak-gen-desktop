"""Mobile app simulator - behaves like the phone app (dev/test only).

    python tools/mobile_sim.py                       # discover stations, join the first one
    python tools/mobile_sim.py --host 192.168.1.20   # connect directly
    python tools/mobile_sim.py --scan '{"mac":"E9:EB:A0:ED:7C:7C","seq":"0001"}'

Once the station operator clicks Connect, type a QR text and press Enter to
"scan" it. NFC write requests are answered automatically (use --nfc-fail to
simulate a failure).
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt

DISCOVERY_PORT = 47474


def discover(timeout: float = 4.0) -> list[tuple[str, dict]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    sock.bind(("", 0))
    found: dict[str, tuple[str, dict]] = {}
    end = time.time() + timeout
    while time.time() < end:
        sock.sendto(b"SYTRAK_DISCOVER", ("255.255.255.255", DISCOVERY_PORT))
        try:
            data, addr = sock.recvfrom(2048)
            info = json.loads(data)
            if info.get("type") == "sytrak-gen":
                found[info["station"]] = (addr[0], info)
        except (socket.timeout, ValueError):
            pass
    return list(found.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--station", help="station id (required with --host)")
    ap.add_argument("--id", default=f"mob-{uuid.uuid4().hex[:6]}")
    ap.add_argument("--name", default="Simulated Phone")
    ap.add_argument("--password", default="")
    ap.add_argument("--scan", help="QR text to send as soon as the station pairs")
    ap.add_argument("--nfc-fail", action="store_true")
    ap.add_argument("--nfc-corrupt", default="", help="pretend the tag reads back this text (tests verify failure)")
    ap.add_argument("--exit-after", type=float, default=0, help="quit after N seconds (0 = interactive)")
    args = ap.parse_args()

    if args.host:
        host, port, station = args.host, args.port, args.station
    else:
        print("Discovering stations…")
        stations = discover()
        if not stations:
            print("No station found")
            return 1
        for ip, info in stations:
            print(f"  {info['station']}  '{info['name']}'  at {ip}:{info['mqtt_port']}")
        host, info = stations[0]
        port, station = info["mqtt_port"], info["station"]

    base = f"sytrak/{station}/mobile/{args.id}"
    paired = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.id)
    if args.password:
        client.username_pw_set("sytrak", args.password)
    client.will_set(f"{base}/status", "offline", retain=True)

    def on_connect(c, _u, _f, rc, _p):
        print(f"Connected to {host}:{port} as {args.id} ({rc})")
        c.subscribe(f"{base}/cmd/#", qos=1)
        c.publish(f"{base}/status", "online", retain=True)
        c.publish(f"{base}/evt/hello", json.dumps({"name": args.name}), qos=1)

    def on_message(c, _u, msg):
        cmd = msg.topic.rsplit("/", 1)[-1]
        data = json.loads(msg.payload or b"{}")
        print(f"<- {cmd}: {data}")
        if cmd == "session":
            (paired.set if data.get("state") == "paired" else paired.clear)()
        elif cmd == "nfc_write":
            time.sleep(1.0)  # "user taps WRITE"
            result = {"seq": data.get("seq"), "ok": not args.nfc_fail, "error": "Tag not writable" if args.nfc_fail else ""}
            if not args.nfc_fail:
                result["read_back"] = args.nfc_corrupt or data.get("data")  # what a real phone reads back
            c.publish(f"{base}/evt/nfc_result", json.dumps(result), qos=1)
            print(f"-> nfc_result {result}")

    client.on_connect, client.on_message = on_connect, on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()

    def send_scan(text: str) -> None:
        client.publish(f"{base}/evt/scan", json.dumps({"qr": text}), qos=1)
        print(f"-> scan {text}")

    try:
        if args.scan:
            print("Waiting for the station to pair (click Connect on the PC)…")
            paired.wait()
            send_scan(args.scan)
        if args.exit_after:
            time.sleep(args.exit_after)
        else:
            print("Type QR text + Enter to scan, Ctrl+C to quit")
            for line in sys.stdin:
                if line.strip():
                    send_scan(line.strip())
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
