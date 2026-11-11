"""Erase (Verification 1): make a written + locked tag new again, then write it again."""
from cryptography.hazmat.primitives.asymmetric import ec

from sytrak_gen.core import nfc_sticker as st
from sytrak_gen.core.nfc_job import ERASE, ERASED_FIELDS, WRITE, NfcJob, erase_sheet_groups
from sytrak_gen.core.ntag_sim import FakeNtag, PhoneDriver
from sytrak_gen.core.secrets import Signer
from sytrak_gen.core.sheets import SheetRecord, VerifyStatus

UID, MAC, OTHER = "04C34DEA0B1390", "E9EBA0ED7C7C", "AABBCCDDEEFF"


def signer() -> Signer:
    return Signer(ec.generate_private_key(ec.SECP192R1()), 1, b"\x33" * 32)


def written_tag(s: Signer, uid: str = UID, lock: bool = True) -> FakeNtag:
    tag = FakeNtag(uid)
    res = PhoneDriver(tag).write_job(s.ndef(uid, MAC), *s.password(uid), lock=lock)
    assert res["ok"] and (tag.auth0 == 0x02) == lock
    return tag


def factory(chip: str = "NTAG213") -> bytes:
    return FakeNtag(UID, chip).dump()


def run_erase(job: NfcJob, tag: FakeNtag, records):
    phone = PhoneDriver(tag)
    reply = job.on_tag(phone.inspect(), records)
    if not reply["ok"]:
        return reply, None
    done = phone.erase_job(bytes.fromhex(reply["write"]["data"]), bytes.fromhex(reply["pwd"]), bytes.fromhex(reply["pack"]))
    return reply, job.on_done(done)


def rows(v1_uid=UID, v2_uid="", other_uid=""):
    return [SheetRecord(1, "E9:EB:A0:ED:7C:7C", "t", row=3, v1=VerifyStatus(nfc=bool(v1_uid), uid=v1_uid),
                        v2=VerifyStatus(nfc=bool(v2_uid), uid=v2_uid)),
            SheetRecord(2, "AA:BB:CC:DD:EE:FF", "t", row=4, v1=VerifyStatus(nfc=bool(other_uid), uid=other_uid))]


def test_erase_locked_tag_back_to_factory_then_write_again():
    s = signer()
    tag = written_tag(s)
    job = NfcJob(ERASE, MAC, "0001", s)
    reply, report = run_erase(job, tag, rows(v2_uid=UID))
    assert reply["mode"] == "erase" and reply["lock"] is False and reply["write"]["page"] == 4
    assert bytes.fromhex(reply["write"]["data"]) == st.blank_user_area(st.CHIPS[0x0F])
    assert report.ok and job.state == "done", report.failures
    assert tag.dump() == factory()                       # every page as on a new tag
    assert tag.auth0 == 0xFF and tag.pwd == b"\xFF" * 4
    assert [c.title for c in report.checks][:2] == ["Tag UID", "Tag UID ↔ MAC"]
    assert job.erase_groups == [1, 2]                    # both blocks of this device named the tag
    assert job.verdict()["headline"] == "TAG ERASED - READY TO WRITE AGAIN"
    assert "pwd" not in job.verdict()

    # the erased tag can be written again (as a new tag) and verifies
    again = NfcJob(WRITE, MAC, "0001", s)
    phone = PhoneDriver(tag)
    w = again.on_tag(phone.inspect(), rows(v1_uid=""))
    assert w["ok"]
    assert again.on_done(phone.write_job(bytes.fromhex(w["write"]["data"]), bytes.fromhex(w["pwd"]),
                                         bytes.fromhex(w["pack"]), lock=True)).ok


def test_erase_open_tag_and_bigger_chips():
    s = signer()
    for chip in ("NTAG213", "NTAG215", "NTAG216"):
        tag = FakeNtag(UID, chip)
        PhoneDriver(tag).write_job(s.ndef(UID, MAC), *s.password(UID), lock=False)   # password set, not locked
        job = NfcJob(ERASE, MAC, "", s)
        _reply, report = run_erase(job, tag, rows())
        assert report.ok, (chip, report.failures)
        assert tag.dump() == factory(chip) and tag.pwd == b"\xFF" * 4


def test_erase_is_idempotent_and_free_tags_leave_the_sheet_alone():
    s = signer()
    fresh = FakeNtag("04000000000009")
    job = NfcJob(ERASE, MAC, "", s)
    _reply, report = run_erase(job, fresh, rows())           # a new tag: nothing to clear, still ok
    assert report.ok and job.erase_groups == []
    assert "belongs to no device" in report.checks[1].detail
    steps = {d["step"]: d["detail"] for d in job.steps}
    assert steps["erase"] == "NDEF area was already blank" and steps["unlock"] == "was not locked"


def test_erase_resumes_after_dropouts():
    s = signer()
    tag = written_tag(s)
    tag.drop_write_at = {5, 41, 43}       # a data page, CFG0 (unlock) and PACK
    _reply, report = run_erase(NfcJob(ERASE, MAC, "", s), tag, rows())
    assert report.ok, report.failures
    assert tag.dump() == factory()


def test_erase_refusals():
    s = signer()
    # another device's tag: refused before anything is changed
    theirs = written_tag(s, "04B34DEA0B1390")
    before = theirs.dump()
    reply, _ = run_erase(NfcJob(ERASE, MAC, "", s), theirs, rows(other_uid="04B34DEA0B1390"))
    assert not reply["ok"] and "belongs to MAC AABBCCDDEEFF" in reply["reason"] and "read that device" in reply["reason"]
    assert theirs.dump() == before

    # lock bits / CFGLCK can never be undone
    locked_bits = FakeNtag(UID)
    locked_bits.load(2, bytes([locked_bits.mem[8], 0x48, 0x0F, 0x00]))
    reply, _ = run_erase(NfcJob(ERASE, MAC, "", s), locked_bits, rows())
    assert not reply["ok"] and "static lock bits" in reply["reason"]
    frozen = FakeNtag(UID)
    frozen.load(frozen.chip.cfg0 + 1, bytes([0x40, 0x05, 0x00, 0x00]))
    reply, _ = run_erase(NfcJob(ERASE, MAC, "", s), frozen, rows())
    assert not reply["ok"] and "cannot be reset" in reply["reason"]


def test_erase_with_a_foreign_password_fails_and_changes_nothing():
    s = signer()
    foreign = written_tag(Signer(ec.generate_private_key(ec.SECP192R1()), 1, b"\x77" * 32))   # another secret
    before = foreign.dump()
    job = NfcJob(ERASE, MAC, "", s)
    _reply, report = run_erase(job, foreign, rows(v1_uid=""))
    assert not report.ok and job.state == "failed"
    assert report.checks[0].title == "Phone" and "another password" in report.checks[0].detail
    assert foreign.dump() == before


def test_analyze_erased_reports_leftovers():
    s = signer()
    tag = written_tag(s)
    report = st.analyze_erased(UID, tag.version().hex(), tag.dump().hex(), "failed")
    failed = {c.title for c in report.failures}
    assert {"SyTrak data removed", "NDEF area", "Lock removed", "Password reset"} <= failed


def test_erase_sheet_groups():
    assert erase_sheet_groups(MAC, UID, rows(v2_uid=UID)) == [1, 2]
    assert erase_sheet_groups(MAC, UID, rows(v1_uid="04000000000009")) == []
    assert erase_sheet_groups(OTHER, UID, rows()) == []
    assert ERASED_FIELDS["nfc"] is False and ERASED_FIELDS["uid"] == "" and ERASED_FIELDS["signature"] == "Erased"
