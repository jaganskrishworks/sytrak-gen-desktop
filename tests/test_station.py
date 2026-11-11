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


def _signer():
    from cryptography.hazmat.primitives.asymmetric import ec
    from sytrak_gen.core.secrets import Signer
    return Signer(ec.generate_private_key(ec.SECP192R1()), 1, b"\x33" * 32)


def test_nfc_job_write_then_verify():
    from sytrak_gen.core.nfc_job import VERIFY, WRITE, NfcJob
    from sytrak_gen.core.ntag_sim import FakeNtag, PhoneDriver
    from sytrak_gen.core.sheets import SheetRecord, VerifyStatus

    signer, tag = _signer(), FakeNtag("04C34DEA0B1390")
    before = [SheetRecord(1, "E9:EB:A0:ED:7C:7C", "t", row=3), SheetRecord(2, "AA:BB:CC:DD:EE:FF", "t", row=4)]
    after = [SheetRecord(1, "E9:EB:A0:ED:7C:7C", "t", row=3, v1=VerifyStatus(nfc=True, uid="04C34DEA0B1390")),
             before[1]]
    job = NfcJob(WRITE, "E9EBA0ED7C7C", "0001", signer)
    phone = PhoneDriver(tag)
    reply = job.on_tag({**phone.inspect()}, before)
    assert reply["ok"] and reply["write"]["page"] == 4 and reply["lock"] is True
    assert job.on_tag({**phone.inspect()}) is reply                       # resend -> same signature
    done = phone.write_job(bytes.fromhex(reply["write"]["data"]), bytes.fromhex(reply["pwd"]),
                           bytes.fromhex(reply["pack"]), lock=reply["lock"])
    assert done["locked"] and tag.auth0 == 0x02
    report = job.on_done(done)
    assert report.ok and job.state == "done", report.failures
    assert report.sheet_values() == {"uid": "04C34DEA0B1390", "signature": "Valid", "password": "Validated", "locked": "Yes"}
    verdict = job.verdict()
    assert verdict["ok"] and verdict["headline"] == "SIGNED STICKER WRITTEN AND VERIFIED"
    assert "pwd" not in verdict and "pack" not in verdict

    v2 = NfcJob(VERIFY, "E9EBA0ED7C7C", "0001", signer)
    phone = PhoneDriver(tag)
    reply = v2.on_tag(phone.inspect(), after)
    assert reply["ok"] and reply["write"] is None
    assert v2.on_done(phone.verify_job(bytes.fromhex(reply["pwd"]), bytes.fromhex(reply["pack"]))).ok

    wrong_device = NfcJob(VERIFY, "AABBCCDDEEFF", "0002", signer)
    phone = PhoneDriver(tag)
    reply = wrong_device.on_tag(phone.inspect(), after)
    report = wrong_device.on_done(phone.verify_job(bytes.fromhex(reply["pwd"]), bytes.fromhex(reply["pack"])))
    # A genuine sticker of another device: its signature is valid, but it is not this device's tag.
    assert not report.ok and [c.title for c in report.failures] == ["Tag UID ↔ MAC", "MAC = device"]


def test_one_tag_per_device():
    from sytrak_gen.core.nfc_job import VERIFY, WRITE, NfcJob, tag_owner_problem
    from sytrak_gen.core.ntag_sim import FakeNtag, PhoneDriver
    from sytrak_gen.core.sheets import SheetRecord, VerifyStatus, audit_rows

    a, b = "E9:EB:A0:ED:7C:7C", "AA:BB:CC:DD:EE:FF"
    tag_a, tag_b, fresh = "04C34DEA0B1390", "04B34DEA0B1390", "04000000000009"
    rows = [SheetRecord(1, a, "t", row=3, v1=VerifyStatus(nfc=True, uid=tag_a)),
            SheetRecord(2, b, "t", row=4, v1=VerifyStatus(nfc=True, uid=tag_b))]
    A = "E9EBA0ED7C7C"
    assert tag_owner_problem(WRITE, A, tag_a, rows) == ""                     # rewrite its own tag
    assert "already belongs to MAC AABBCCDDEEFF" in tag_owner_problem(WRITE, A, tag_b, rows)
    assert "already has tag 04C34DEA0B1390" in tag_owner_problem(WRITE, A, fresh, rows)
    assert tag_owner_problem(VERIFY, A, tag_a, rows) == ""
    assert "wrong tag" in tag_owner_problem(VERIFY, A, fresh, rows)
    assert "belongs to MAC AABBCCDDEEFF" in tag_owner_problem(VERIFY, A, tag_b, rows)
    new = [SheetRecord(3, "11:22:33:44:55:66", "t", row=5, v1=VerifyStatus(nfc=False, uid=fresh))]   # failed V1
    assert tag_owner_problem(WRITE, "112233445566", fresh, new) == ""          # a failed attempt reserves nothing
    assert "must pass first" in tag_owner_problem(VERIFY, "112233445566", fresh, new)

    # Device A passed V1 on tag X; its V2 is empty, failed on X, or failed on another tag Y.
    # X stays A's tag in every case; the Y of a failed V2 is not reserved.
    tag_y = "04000000000077"
    for a_v2 in (VerifyStatus(), VerifyStatus(nfc=False, uid=tag_a), VerifyStatus(nfc=False, uid=tag_y)):
        sheet = [SheetRecord(1, a, "t", row=3, v1=VerifyStatus(nfc=True, uid=tag_a), v2=a_v2),
                 SheetRecord(2, b, "t", row=4)]
        assert "belongs to MAC E9EBA0ED7C7C" in tag_owner_problem(WRITE, "AABBCCDDEEFF", tag_a, sheet)
        assert "belongs to MAC E9EBA0ED7C7C" in tag_owner_problem(VERIFY, "AABBCCDDEEFF", tag_a, sheet)
        assert tag_owner_problem(WRITE, "AABBCCDDEEFF", tag_y, sheet) == ""

    # Verification 1: another device's tag is refused before anything is written
    signer = _signer()
    job = NfcJob(WRITE, A, "0001", signer)
    other = FakeNtag(tag_b)
    reply = job.on_tag(PhoneDriver(other).inspect(), rows)
    assert not reply["ok"] and "belongs to MAC" in reply["reason"]
    assert other.mem[16:20] == bytes([0x03, 0x00, 0xFE, 0x00])

    # Verification 2: a valid sticker on the wrong tag still fails, and the result is recorded
    stranger = FakeNtag(fresh)
    PhoneDriver(stranger).write_job(signer.ndef(fresh, A), *signer.password(fresh), lock=True)
    v2 = NfcJob(VERIFY, A, "0001", signer)
    phone = PhoneDriver(stranger)
    reply = v2.on_tag(phone.inspect(), rows)
    assert reply["ok"]
    report = v2.on_done(phone.verify_job(bytes.fromhex(reply["pwd"]), bytes.fromhex(reply["pack"])))
    assert [c.title for c in report.failures] == ["Tag UID ↔ MAC"]
    assert report.sig_status == "valid"                                        # the signature alone is not enough

    dup = rows + [SheetRecord(9, "11:22:33:44:55:66", "t", row=6, v2=VerifyStatus(nfc=True, uid=tag_a))]
    assert any("Tag UID 04C34DEA0B1390 is used for 2 devices" in p for p in audit_rows(
        [["", ""], ["SEQ"]] + [[f"{r.seq:04d}", r.mac, "t", "", *(
            ["", "", "Yes" if r.v1.nfc else "", r.v1.uid, "", "", "", ""]),
            "", "", "Yes" if r.v2.nfc else "", r.v2.uid] for r in dup])[1])


def test_nfc_job_refuses_bad_tags_and_swaps():
    from sytrak_gen.core.nfc_job import WRITE, JobError, NfcJob
    from sytrak_gen.core.ntag_sim import FakeNtag, PhoneDriver

    job = NfcJob(WRITE, "E9EBA0ED7C7C", "0001", _signer())
    assert not job.on_tag({"uid": "08AABBCC", "version": "", "pages": ""})["ok"]        # random 4-byte UID
    locked = FakeNtag("04000000000001")
    locked.mem[10] = 0xFF                                                               # static lock bits
    assert "lock bits" in job.on_tag(PhoneDriver(locked).inspect())["reason"]
    assert job.on_tag(PhoneDriver(FakeNtag("04000000000002")).inspect())["ok"]         # job stayed open
    assert not job.on_tag(PhoneDriver(FakeNtag("04000000000003")).inspect())["ok"]     # swapped mid-job
    with pytest.raises(JobError):
        job.on_done({"ok": True, "uid": "04000000000003"})


def test_device_duplicates():
    from sytrak_gen.core.sheets import SheetRecord
    from sytrak_gen.ui.views.verify_view import device_duplicates

    a, b, c = "AA:AA:AA:AA:AA:01", "AA:AA:AA:AA:AA:02", "AA:AA:AA:AA:AA:03"
    clean = [SheetRecord(1, a, "t", row=2), SheetRecord(2, b, "t", row=3)]
    assert device_duplicates(clean, a) == []
    seq_clash = clean + [SheetRecord(1, c, "t", row=4)]
    assert device_duplicates(seq_clash, a) == ["SEQ 0001 is also used by row 4 (AAAAAAAAAA03)"]
    assert device_duplicates(seq_clash, b) == []  # other device unaffected
    mac_twice = clean + [SheetRecord(5, a, "t", row=6)]
    assert device_duplicates(mac_twice, a) == ["MAC AAAAAAAAAA01 is on 2 rows: row 2 (SEQ 0001), row 6 (SEQ 0005)"]


def test_sticker_check():
    from sytrak_gen.ui.views.verify_view import sticker_check

    dev = "E9:EB:A0:ED:7C:7C"
    assert sticker_check("E9EBA0ED7C7C", dev, 1) == (True, "Sticker E9EBA0ED7C7C  =  device MAC")
    ok, why = sticker_check("E9EBA0ED7C7D", dev, 1)
    assert not ok and "≠" in why
    assert not sticker_check("hello", dev, 1)[0]
    assert not sticker_check('{"mac":"E9:EB:A0:ED:7C:7C","seq":"0002"}', dev, 1)[0]  # old code, wrong SEQ


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

        # Device-first verify: PC sends the expected SEQ + MAC, then its verdict.
        assert svc.request_verify("E9EBA0ED7C7C", "0001", "UWB-K4W")
        assert inbox_a.get(timeout=3) == ("verify_request", {"mac": "E9EBA0ED7C7C", "seq": "0001", "device_type": "UWB-K4W"})
        svc.send_verify_result(True, "E9EBA0ED7C7C", "0001")
        assert inbox_a.get(timeout=3) == ("verify_result", {"ok": True, "mac": "E9EBA0ED7C7C", "seq": "0001", "reason": ""})

        # A mobile cannot publish on behalf of another.
        b.publish("sytrak/SG-TEST/mobile/mob-a/evt/scan", json.dumps({"qr": "spoof"}), qos=1)

        # NFC job: PC starts it, the paired mobile answers; an unpaired mobile is ignored.
        job = {"job_id": "j-1", "mode": "write", "mac": "E9EBA0ED7C7C", "seq": "0001"}
        assert svc.start_nfc_job(job)
        assert inbox_a.get(timeout=3) == ("nfc_job", job)
        b.publish("sytrak/SG-TEST/mobile/mob-b/evt/nfc_tag", json.dumps({"job_id": "j-1", "uid": "x"}), qos=1)
        a.publish("sytrak/SG-TEST/mobile/mob-a/evt/nfc_tag", json.dumps({"job_id": "j-1", "uid": "04C34DEA0B1390"}), qos=1)
        assert _wait(events, "nfc_tag") == {"id": "mob-a", "job_id": "j-1", "uid": "04C34DEA0B1390"}
        assert svc.reply_nfc_job({"job_id": "j-1", "ok": False, "reason": "test"})
        assert inbox_a.get(timeout=3)[0] == "nfc_job_reply"
        a.publish("sytrak/SG-TEST/mobile/mob-a/evt/nfc_done", json.dumps({"job_id": "j-1", "ok": False, "id": "spoof"}), qos=1)
        assert _wait(events, "nfc_done")["id"] == "mob-a"         # the id cannot be overridden by the payload
        assert svc.cancel_nfc_job("j-1", "stop")
        assert inbox_a.get(timeout=3) == ("nfc_cancel", {"job_id": "j-1", "reason": "stop"})
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
