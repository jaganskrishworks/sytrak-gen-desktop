import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from sytrak_gen.core import nfc_sticker as st
from sytrak_gen.core.ntag_sim import FakeNtag, PhoneDriver
from sytrak_gen.core.secrets import Signer

# nfc_sticker_v1.md §10 - throwaway test key 1
SPEC_PUBLIC = ("0416456B8B398D5FBB816F4889EB4B38AB08984E4D7DB864"
               "2C755BD9539916AC7E382F4381CBB82CA28FDD40BA33E15AF6")
SPEC_UID, SPEC_MAC = "04B34DEA0B1390", "E9EBA0ED7C7C"
SPEC_TLV = ("034F91010F5402656E45394542413045443743374354"
            "083173797472616B3A73111C5297523D2B705C54D593"
            "433C02747AC29F460B39CEB218344647FDD7427D3095"
            "DBC5F300B26EC99F43E323AE0ACD3BFE")


def spec_tag() -> FakeNtag:
    tag = FakeNtag(SPEC_UID)
    tag.load(4, bytes.fromhex(SPEC_TLV))
    return tag


def test_spec_vector_verifies_with_spec_public_key():
    tag = spec_tag()
    report = st.analyze("verify", SPEC_UID, tag.version().hex(), tag.dump().hex(), device_mac=SPEC_MAC,
                        public_keys={1: st.public_key_from_hex(SPEC_PUBLIC)}, password="validated")
    assert report.mac_on_tag == SPEC_MAC
    assert report.sig_status == st.SIG_VALID and report.key_id == 1
    assert report.chip.name == "NTAG213"
    assert report.ok, report.failures
    assert report.locked is False
    assert report.signed_text == "sytraknfcv1-04B34DEA0B1390-E9EBA0ED7C7C"


def test_copied_data_on_other_uid_is_invalid():
    tag = FakeNtag("04AABBCCDDEEFF")
    tag.load(4, bytes.fromhex(SPEC_TLV))
    report = st.analyze("verify", "04AABBCCDDEEFF", tag.version().hex(), tag.dump().hex(), device_mac=SPEC_MAC,
                        public_keys={1: st.public_key_from_hex(SPEC_PUBLIC)}, password="validated")
    assert report.sig_status == st.SIG_INVALID and not report.ok


def test_unknown_key_and_wrong_device():
    tag = spec_tag()
    other = ec.generate_private_key(ec.SECP192R1()).public_key()
    report = st.analyze("verify", SPEC_UID, tag.version().hex(), tag.dump().hex(), device_mac="AABBCCDDEEFF",
                        public_keys={2: other}, password="failed")
    assert report.sig_status == st.SIG_UNKNOWN_KEY
    titles = {c.title for c in report.failures}
    assert {"MAC = device", "Signature valid", "Password"} <= titles


def test_blank_tag_reports_missing_records():
    tag = FakeNtag(SPEC_UID)
    report = st.analyze("verify", SPEC_UID, tag.version().hex(), tag.dump().hex(), device_mac=SPEC_MAC,
                        public_keys={}, password="failed")
    assert report.sig_status == st.SIG_MISSING
    assert not report.mac_on_tag and not report.ok


def test_build_ndef_matches_spec_layout():
    payload = bytes.fromhex(SPEC_TLV)[32:32 + 49]   # TLV head 2 + record 1 (19) + record 2 head 3 + type 8
    assert st.build_ndef(SPEC_MAC, payload).hex().upper().startswith(SPEC_TLV)
    assert len(st.build_ndef(SPEC_MAC, payload)) == 84


def test_parse_records_long_and_chunked():
    long_rec = bytes([0xC1, 1, 0, 0, 0, 5]) + b"T" + b"\x02enAB"   # MB ME, not short
    assert st.parse_records(long_rec)[0].text == "AB"
    with pytest.raises(st.StickerError):
        st.parse_records(bytes([0xB1, 1, 3]) + b"T\x02en")          # chunked


def test_password_is_deterministic_per_uid():
    pwd, pack = st.tag_password(b"\x11" * 32, SPEC_UID)
    assert (pwd, pack) == st.tag_password(b"\x11" * 32, SPEC_UID)
    assert len(pwd) == 4 and len(pack) == 2
    assert st.tag_password(b"\x11" * 32, "04AABBCCDDEEFF") != (pwd, pack)


# ------------------------------------------------- full flow on a fake tag ---

def make_signer() -> Signer:
    return Signer(ec.generate_private_key(ec.SECP192R1()), 1, b"\x5A" * 32)


def test_write_then_verify_flow():
    signer, tag = make_signer(), FakeNtag(SPEC_UID)
    ndef = signer.ndef(SPEC_UID, SPEC_MAC)
    pwd, pack = signer.password(SPEC_UID)
    result = PhoneDriver(tag).write_job(ndef, pwd, pack)
    assert result["ok"], result
    report = st.analyze("write", result["uid"], result["version"], result["pages"], device_mac=SPEC_MAC,
                        public_keys=signer.public_keys, password=result["password"], expected_ndef=ndef)
    assert report.ok, report.failures
    assert report.written and report.password == "validated" and report.locked is False
    assert tag.pwd == pwd and tag.auth0 == 0xFF          # password set, NOT locked

    # Verification 2: read only, nothing changes
    before = tag.dump()
    result = PhoneDriver(tag).verify_job(pwd, pack)
    assert result["ok"] and result["password"] == "validated"
    assert tag.dump() == before


def test_write_resumes_after_dropout():
    signer, tag = make_signer(), FakeNtag(SPEC_UID)
    tag.drop_write_at = {9}                    # one page write is lost once
    ndef = signer.ndef(SPEC_UID, SPEC_MAC)
    result = PhoneDriver(tag).write_job(ndef, *signer.password(SPEC_UID))
    assert result["ok"], result
    assert any("retry" in s["detail"] for s in result["steps"])
    report = st.analyze("write", result["uid"], result["version"], result["pages"], device_mac=SPEC_MAC,
                        public_keys=signer.public_keys, password=result["password"], expected_ndef=ndef)
    assert report.ok, report.failures


def test_verify_with_wrong_password_reports_failed_and_still_reads():
    signer, tag = make_signer(), FakeNtag(SPEC_UID)
    ndef = signer.ndef(SPEC_UID, SPEC_MAC)
    PhoneDriver(tag).write_job(ndef, *signer.password(SPEC_UID))
    result = PhoneDriver(tag).verify_job(b"\x00\x00\x00\x00", b"\x00\x00")
    assert result["password"] == "failed"
    assert result["pages"]                       # reconnected after the halt and read the tag
    report = st.analyze("verify", result["uid"], result["version"], result["pages"], device_mac=SPEC_MAC,
                        public_keys=signer.public_keys, password=result["password"])
    assert report.sig_status == st.SIG_VALID and not report.ok


def test_configured_secrets_folder_key(tmp_path):
    """Uses the dev key + secret in <project>/secrets/ when present (never committed)."""
    from sytrak_gen.core import secrets
    try:
        signer = secrets.load_signer()
    except secrets.SecretError:
        pytest.skip("no NFC key/secret in secrets/")
    ndef = signer.ndef(SPEC_UID, SPEC_MAC)
    tag = FakeNtag(SPEC_UID)
    result = PhoneDriver(tag).write_job(ndef, *signer.password(SPEC_UID))
    report = st.analyze("write", result["uid"], result["version"], result["pages"], device_mac=SPEC_MAC,
                        public_keys=signer.public_keys, password=result["password"], expected_ndef=ndef)
    assert report.ok, report.failures


def test_admin_reveal_helpers(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from sytrak_gen.core import secrets
    key = ec.generate_private_key(ec.SECP192R1())
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    sec = tmp_path / "p.secret"
    sec.write_text("5A" * 32)
    assert secrets.reveal_password_secret(str(sec)) == "5A" * 32
    assert secrets.reveal_signing_key(str(pem)).startswith("Private value  ")
    assert secrets.public_key_hex(str(pem)) == st.public_point(key.public_key())
    pwd, pack = st.tag_password(b"\x5A" * 32, SPEC_UID)
    assert secrets.tag_password_text("04:b3:4d:ea:0b:13:90", str(sec)) == \
        f"UID {SPEC_UID}   PWD {pwd.hex().upper()}   PACK {pack.hex().upper()}"
    with pytest.raises(secrets.SecretError):
        secrets.tag_password_text("1234", str(sec))


def test_secret_validation():
    from sytrak_gen.core import secrets
    with pytest.raises(secrets.SecretError):
        secrets.PASSWORD_SECRET.validate(b"zz-not-hex")
    with pytest.raises(secrets.SecretError):
        secrets.PASSWORD_SECRET.validate(b"00ff")                 # too short
    with pytest.raises(secrets.SecretError):
        secrets.SIGNING_KEY.validate(b"-----BEGIN PRIVATE KEY-----\nxx\n-----END PRIVATE KEY-----")
    with pytest.raises(secrets.SecretError):
        secrets.GOOGLE_KEY.validate(b"<html>sign in</html>")
    assert secrets.PASSWORD_SECRET.validate(b"11" * 32 + b"\n") == "32 bytes"


def test_lock_then_rewrite_with_password():
    signer, tag = make_signer(), FakeNtag(SPEC_UID)
    ndef = signer.ndef(SPEC_UID, SPEC_MAC)
    pwd, pack = signer.password(SPEC_UID)
    result = PhoneDriver(tag).write_job(ndef, pwd, pack, lock=True)
    assert result["ok"] and result["locked"], result
    assert tag.auth0 == 0x02 and tag.mem[(tag.chip.cfg0 + 1) * 4] & 0xC7 == 0   # PROT, CFGLCK, AUTHLIM clear
    report = st.analyze("write", result["uid"], result["version"], result["pages"], device_mac=SPEC_MAC,
                        public_keys=signer.public_keys, password=result["password"], expected_ndef=ndef, lock_required=True)
    assert report.ok and report.locked, report.failures

    # Without the password nothing can be written any more; reads still work.
    tag.reconnect()
    with pytest.raises(IOError):
        tag.transceive(bytes([0xA2, 4, 0, 0, 0, 0]))
    tag.reconnect()
    assert PhoneDriver(tag).verify_job(pwd, pack)["password"] == "validated"

    # Verification 1 again on the locked tag: PWD_AUTH first, then it rewrites it.
    again = PhoneDriver(tag).write_job(signer.ndef(SPEC_UID, SPEC_MAC), pwd, pack, lock=True)
    assert again["ok"] and again["locked"], again

    # An open tag fails "Locked" when locking is required.
    open_tag = FakeNtag("04AABBCCDDEEFF")
    res = PhoneDriver(open_tag).write_job(signer.ndef("04AABBCCDDEEFF", SPEC_MAC), *signer.password("04AABBCCDDEEFF"))
    rep = st.analyze("verify", res["uid"], res["version"], res["pages"], device_mac=SPEC_MAC,
                     public_keys=signer.public_keys, password=res["password"], lock_required=True)
    assert [c.title for c in rep.failures] == ["Locked"]


def test_never_lock_with_a_failed_password():
    signer, tag = make_signer(), FakeNtag(SPEC_UID)
    ndef = signer.ndef(SPEC_UID, SPEC_MAC)
    pwd, pack = signer.password(SPEC_UID)
    real = tag.transceive
    pwd_page = tag.chip.cfg0 + 2

    def lossy(cmd: bytes) -> bytes:   # the tag ACKs the PWD write but does not keep it
        return b"\x0A" if cmd[0] == 0xA2 and cmd[1] == pwd_page else real(cmd)

    tag.transceive = lossy
    result = PhoneDriver(tag).write_job(ndef, pwd, pack, lock=True)
    assert result["password"] == "failed" and not result["locked"] and tag.auth0 == 0xFF


def test_cfglck_tag_is_refused():
    tag = FakeNtag(SPEC_UID)
    tag.mem[(tag.chip.cfg0 + 1) * 4] = 0x40
    signer = make_signer()
    result = PhoneDriver(tag).write_job(signer.ndef(SPEC_UID, SPEC_MAC), *signer.password(SPEC_UID))
    assert not result["ok"] and result["step"] == "check"
