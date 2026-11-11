"""Mobile app simulator - behaves like the phone app (dev/test only).

    python tools/mobile_sim.py                       # discover stations, join the first one
    python tools/mobile_sim.py --host 192.168.1.20   # connect directly
    python tools/mobile_sim.py --scan '{"mac":"E9:EB:A0:ED:7C:7C","seq":"0001"}'

Once the station operator clicks Connect, type a QR text and press Enter to
"scan" it. NFC jobs (cmd/nfc_job) are run on a virtual NTAG213 that keeps its
memory between jobs, so Verification 1 then Verification 2 works like a real tag.
Failure options: --tag-uid, --swap-tag, --tag-dropout, --wrong-password, --random-uid, --cfglck.
"""
from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sytrak_gen.core.ntag_sim import FakeNtag, PhoneDriver  # noqa: E402

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
    ap.add_argument("--auto-sticker", action="store_true",
                    help="answer each verify_request by 'scanning' the requested MAC")
    ap.add_argument("--sticker", default="", help="with --auto-sticker: scan this text instead (tests a wrong sticker)")
    ap.add_argument("--tag-uid", default="04C34DEA0B1390", help="UID of the virtual NTAG213")
    ap.add_argument("--swap-tag", action="store_true", help="finish each job on another tag (tests the UID check)")
    ap.add_argument("--tag-dropout", type=int, default=0, help="lose the write of this page once (tests resume)")
    ap.add_argument("--wrong-password", action="store_true", help="verify jobs use a wrong password in PWD_AUTH")
    ap.add_argument("--random-uid", action="store_true", help="report a 4-byte random UID (tag is refused)")
    ap.add_argument("--cfglck", action="store_true", help="the tag's configuration is frozen (tag is refused)")
    ap.add_argument("--exit-after", type=float, default=0, help="quit after N seconds (0 = interactive)")
    args = ap.parse_args()

    tag = FakeNtag(args.tag_uid)
    if args.cfglck:
        tag.mem[(tag.chip.cfg0 + 1) * 4] = 0x40
    replies: dict[str, queue.Queue] = {}

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

    def publish(c, name: str, data: dict) -> None:
        c.publish(f"{base}/evt/{name}", json.dumps(data), qos=1)
        shown = {k: v for k, v in data.items() if k not in ("pages", "steps")}
        print(f"-> {name}: {shown}")

    def run_job(c, job: dict) -> None:
        """What the phone does: tap -> evt/nfc_tag -> wait for the PC's reply -> write/check -> evt/nfc_done."""
        jid = job["job_id"]
        time.sleep(1.0)   # "the operator taps the tag"
        driver = PhoneDriver(tag, on_step=lambda s: publish(c, "nfc_progress", {"job_id": jid, **s}))
        first = driver.inspect()
        if args.random_uid:
            first["uid"] = "08A1B2C3"
        replies[jid] = queue.Queue()
        for attempt in range(3):   # resend the same tag data if the PC does not answer
            publish(c, "nfc_tag", {"job_id": jid, "attempt": attempt + 1, **first})
            try:
                reply = replies[jid].get(timeout=5)
                break
            except queue.Empty:
                print("   no reply from the PC - resending")
        else:
            publish(c, "nfc_done", {"job_id": jid, "ok": False, "step": "pc", "error": "No answer from the PC", **first})
            return
        if not reply.get("ok"):
            print(f"   PC refused the tag: {reply.get('reason')}")
            return
        pwd, pack = bytes.fromhex(reply["pwd"]), bytes.fromhex(reply["pack"])
        if args.wrong_password and not reply.get("write"):   # a tag that holds another password
            pwd = b"\x00\x00\x00\x00"
        if args.tag_dropout:
            tag.drop_write_at = {args.tag_dropout}
        if job.get("mode") == "erase":
            result = driver.erase_job(bytes.fromhex(reply["write"]["data"]), pwd, pack)
        elif reply.get("write"):
            result = driver.write_job(bytes.fromhex(reply["write"]["data"]), pwd, pack, lock=bool(reply.get("lock")))
        else:
            result = driver.verify_job(pwd, pack)
        if args.swap_tag:
            result["uid"] = "04FFFFFFFFFFFF"
        publish(c, "nfc_done", {"job_id": jid, **result})

    def on_message(c, _u, msg):
        cmd = msg.topic.rsplit("/", 1)[-1]
        data = json.loads(msg.payload or b"{}")
        shown = {k: ("••••" if k in ("pwd", "pack") else v) for k, v in data.items() if k != "checks"}
        print(f"<- {cmd}: {shown}")
        if cmd == "nfc_verdict":
            for check in data.get("checks", []):
                print(f"     {check['state']:>4}  {check['title']}: {check['detail']}")
        if cmd == "session":
            (paired.set if data.get("state") == "paired" else paired.clear)()
        elif cmd == "verify_request" and args.auto_sticker:
            # Pretend the operator scans the sticker of the requested device (or a wrong one).
            time.sleep(1.0)
            sticker = args.sticker or data.get("mac", "")
            c.publish(f"{base}/evt/scan", json.dumps({"qr": sticker}), qos=1)
            print(f"-> scan (sticker) {sticker}")
        elif cmd == "nfc_job":
            threading.Thread(target=run_job, args=(c, data), daemon=True).start()
        elif cmd == "nfc_job_reply" and data.get("job_id") in replies:
            replies[data["job_id"]].put(data)

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
