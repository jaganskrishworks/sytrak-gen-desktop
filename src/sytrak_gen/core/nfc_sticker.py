"""SyTrak NFC sticker v1 (see docs/NFC_STICKER_PLAN.md and nfc_sticker_v1.md).

Tag: NFC Forum Type 2 (NTAG213/215/216) with a fixed 7-byte UID. NDEF TLV from page 4:

  record 1  Text "T", 02 'en' + MAC (12 uppercase hex)
  record 2  External "sytrak:s", 1 byte (version 1 << 4 | key ID) + ECDSA P-192/SHA-256 raw r||s (48 B)

Signed string (not stored on the tag): "sytraknfcv1-<UID>-<MAC>", uppercase hex.
Per-tag password: HMAC-SHA256(secret, "sytraknfcpwv1-<UID>") -> PWD = bytes 0-3, PACK = bytes 4-5.

This module only builds and judges bytes; the phone moves them to and from the tag.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

VERSION = 1
SIG_TYPE = b"sytrak:s"
SIG_LEN = 48
SIG_PAYLOAD_LEN = 1 + SIG_LEN
TNF_WELL_KNOWN, TNF_EXTERNAL = 0x01, 0x04
NDEF_START_PAGE = 4
PAGE = 4


@dataclass(frozen=True)
class Chip:
    name: str
    cfg0: int          # first configuration page (AUTH0 is its byte 3)
    user_bytes: int    # NDEF area from page 4

    @property
    def last_page(self) -> int:     # PACK page
        return self.cfg0 + 3

    @property
    def user_pages(self) -> range:  # page 4 .. the page before the dynamic lock page
        return range(NDEF_START_PAGE, NDEF_START_PAGE + self.user_bytes // PAGE)


# GET_VERSION byte 6 (storage size) -> chip
CHIPS = {0x0F: Chip("NTAG213", 41, 144), 0x11: Chip("NTAG215", 131, 504), 0x13: Chip("NTAG216", 227, 888)}

# Signature status words of the mapper (spec §9)
SIG_VALID, SIG_INVALID, SIG_MISSING = "valid", "invalid", "missing"
SIG_UNKNOWN_KEY, SIG_MALFORMED, SIG_UNSUPPORTED, SIG_NO_MAC = "unknown_key", "malformed", "unsupported_version", "no_mac"


class StickerError(ValueError):
    pass


def is_hex(text: str, nbytes: int) -> bool:
    return isinstance(text, str) and re.fullmatch("[0-9A-F]{%d}" % (2 * nbytes), text) is not None


def hexbytes(text: str) -> bytes:
    """Hex string (spaces allowed) -> bytes; raises StickerError."""
    try:
        return bytes.fromhex(str(text).replace(" ", ""))
    except ValueError as exc:
        raise StickerError(f"not hex: {str(text)[:20]}…") from exc


# ------------------------------------------------------------------ crypto ---

def message(uid: str, mac: str) -> bytes:
    return f"sytraknfcv1-{uid}-{mac}".encode()


def sign(key: ec.EllipticCurvePrivateKey, msg: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(24, "big") + s.to_bytes(24, "big")


def sig_payload(key: ec.EllipticCurvePrivateKey, key_id: int, uid: str, mac: str) -> bytes:
    """The 49-byte "sytrak:s" record payload for this UID + MAC."""
    if not 1 <= key_id <= 15:
        raise StickerError("key ID must be 1-15")
    return bytes([(VERSION << 4) | key_id]) + sign(key, message(uid, mac))


def verify(public_key: ec.EllipticCurvePublicKey, uid: str, mac: str, sig: bytes) -> bool:
    if len(sig) != SIG_LEN:
        return False
    der = encode_dss_signature(int.from_bytes(sig[:24], "big"), int.from_bytes(sig[24:], "big"))
    try:
        public_key.verify(der, message(uid, mac), ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def public_point(public_key: ec.EllipticCurvePublicKey) -> str:
    """Uncompressed public point, hex - the mapper's trusted-key entry."""
    return public_key.public_bytes(serialization.Encoding.X962,
                                   serialization.PublicFormat.UncompressedPoint).hex().upper()


def public_key_from_hex(point: str) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP192R1(), hexbytes(point))


def tag_password(secret: bytes, uid: str) -> tuple[bytes, bytes]:
    """(PWD 4 B, PACK 2 B) of this tag. Never log or store them."""
    digest = hmac.new(secret, f"sytraknfcpwv1-{uid}".encode(), hashlib.sha256).digest()
    return digest[0:4], digest[4:6]


# -------------------------------------------------------------------- NDEF ---

def build_ndef(mac: str, payload: bytes, asn: str | None = None) -> bytes:
    """NDEF TLV from page 4, padded to whole pages: MAC Text record + "sytrak:s" record (+ ASN)."""
    text = b"\x02en" + mac.encode()
    rec1 = bytes([0x91, 0x01, len(text)]) + b"T" + text                          # MB SR TNF=1
    rec2 = bytes([0x14, len(SIG_TYPE), len(payload)]) + SIG_TYPE + payload        # SR TNF=4
    if asn is None:
        msg = rec1 + bytes([rec2[0] | 0x40]) + rec2[1:]                           # ME
    else:
        at = b"\x02en" + asn.encode()
        msg = rec1 + rec2 + bytes([0x51, 0x01, len(at)]) + b"T" + at              # ME SR TNF=1
    if len(msg) > 254:
        raise StickerError("NDEF message too long")
    tlv = bytes([0x03, len(msg)]) + msg + b"\xFE"
    return tlv + b"\x00" * (-len(tlv) % PAGE)


@dataclass(frozen=True)
class Record:
    tnf: int
    type: bytes
    payload: bytes

    @property
    def type_text(self) -> str:
        return self.type.decode("ascii", errors="replace")

    @property
    def text(self) -> str | None:
        """Body of a Well-Known Text record, else None."""
        if self.tnf != TNF_WELL_KNOWN or self.type != b"T" or not self.payload:
            return None
        lang = self.payload[0] & 0x3F
        body = self.payload[1 + lang:]
        return body.decode("utf-16-be" if self.payload[0] & 0x80 else "utf-8", errors="replace")

    @property
    def is_signature(self) -> bool:
        return self.tnf == TNF_EXTERNAL and self.type == SIG_TYPE

    def describe(self) -> str:
        if self.text is not None:
            return f'Text "{self.text}"'
        kind = {0: "Empty", 1: "Well-known", 2: "MIME", 3: "URI", 4: "External", 5: "Unknown", 6: "Unchanged"}.get(self.tnf, "?")
        return f"{kind} '{self.type_text}', {len(self.payload)} B"


def find_ndef(user: bytes) -> tuple[bytes | None, str]:
    """The NDEF message from the TLV area (page 4 onward). Returns (message or None, problem)."""
    i = 0
    while i < len(user):
        t = user[i]
        if t == 0x00:                     # NULL TLV
            i += 1
            continue
        if t == 0xFE:
            return None, "no NDEF message (terminator first)"
        if i + 1 >= len(user):
            break
        length, head = user[i + 1], 2
        if length == 0xFF:                # 3-byte length format
            if i + 3 >= len(user):
                break
            length, head = int.from_bytes(user[i + 2:i + 4], "big"), 4
        value = user[i + head:i + head + length]
        if len(value) < length:
            return None, f"TLV 0x{t:02X} is cut off ({len(value)} of {length} B)"
        if t == 0x03:
            return value, "" if length else "empty NDEF message"
        i += head + length               # lock / memory control or proprietary TLV
    return None, "no NDEF TLV found"


def parse_records(msg: bytes) -> list[Record]:
    """Records of an NDEF message; raises StickerError if malformed (chunked records are not supported)."""
    records, i = [], 0
    while i < len(msg):
        hdr = msg[i]
        if hdr & 0x20:
            raise StickerError("chunked NDEF record")
        short, has_id = bool(hdr & 0x10), bool(hdr & 0x08)
        try:
            type_len = msg[i + 1]
            j = i + 2
            if short:
                pay_len, j = msg[j], j + 1
            else:
                pay_len, j = int.from_bytes(msg[j:j + 4], "big"), j + 4
            id_len = msg[j] if has_id else 0
            j += 1 if has_id else 0
        except IndexError as exc:
            raise StickerError("NDEF record header cut off") from exc
        rec_type = msg[j:j + type_len]
        payload = msg[j + type_len + id_len:j + type_len + id_len + pay_len]
        if len(rec_type) < type_len or len(payload) < pay_len:
            raise StickerError("NDEF record cut off")
        records.append(Record(hdr & 0x07, bytes(rec_type), bytes(payload)))
        i = j + type_len + id_len + pay_len
        if hdr & 0x40:   # ME
            break
    return records


# ------------------------------------------------------------- tag report ---

OK, FAIL, INFO, SKIP = "ok", "fail", "info", "skip"


@dataclass
class Check:
    title: str
    state: str          # ok / fail / info / skip
    detail: str = ""


@dataclass
class TagConfig:
    cfg0: bytes
    cfg1: bytes
    chip: Chip

    @property
    def auth0(self) -> int:
        return self.cfg0[3]

    @property
    def access(self) -> int:
        return self.cfg1[0]

    @property
    def prot(self) -> bool:       # reads need the password too
        return bool(self.access & 0x80)

    @property
    def cfglck(self) -> bool:     # configuration frozen for ever
        return bool(self.access & 0x40)

    @property
    def authlim(self) -> int:
        return self.access & 0x07

    @property
    def protected(self) -> bool:  # writes from AUTH0 on need the password
        return self.auth0 <= self.chip.last_page

    def describe(self) -> str:
        return (f"CFG0 {self.cfg0.hex(' ').upper()}  ·  CFG1 {self.cfg1.hex(' ').upper()}  ·  "
                f"AUTH0 {self.auth0:02X}  ·  ACCESS {self.access:02X} (PROT {int(self.prot)}, "
                f"CFGLCK {int(self.cfglck)}, AUTHLIM {self.authlim})")


@dataclass
class TagReport:
    """Everything the PC learnt about one tag, plus the verdict."""
    mode: str                       # "write" (Verification 1) or "verify" (Verification 2)
    uid: str = ""
    chip: Chip | None = None
    version: bytes = b""
    pages: bytes = b""              # page 0 onward as read by the phone
    config: TagConfig | None = None
    records: list[Record] = field(default_factory=list)
    ndef_problem: str = ""
    mac_on_tag: str = ""
    sig_status: str = SIG_MISSING
    sig_version: int | None = None
    key_id: int | None = None
    signature: bytes = b""
    password: str = ""              # validated / failed / not set / not checked
    written: bool | None = None     # V1: read-back == the bytes sent
    checks: list[Check] = field(default_factory=list)

    @property
    def locked(self) -> bool | None:
        return self.config.protected if self.config else None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.state != FAIL for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.state == FAIL]

    @property
    def signed_text(self) -> str:
        return message(self.uid, self.mac_on_tag).decode() if self.uid and self.mac_on_tag else ""

    def sheet_values(self) -> dict[str, str]:
        """Cells for the sheet's Tag UID / Signature / Password / Locked columns."""
        return {
            "uid": self.uid,
            "signature": {SIG_VALID: "Valid"}.get(self.sig_status, self.sig_status.replace("_", " ").capitalize()),
            "password": {"validated": "Validated"}.get(self.password, self.password.capitalize() or "Not checked"),
            "locked": "" if self.locked is None else ("Yes" if self.locked else "No"),
        }

    def page_dump(self) -> list[str]:
        """'p04  03 4F 91 01  |.O..|' per page, with what each page is."""
        lines = []
        cfg0 = self.chip.cfg0 if self.chip else None
        names = {0: "UID", 1: "UID", 2: "UID/lock", 3: "CC"}
        if cfg0:
            names.update({cfg0 - 1: "dyn lock", cfg0: "CFG0", cfg0 + 1: "CFG1", cfg0 + 2: "PWD (reads 0)", cfg0 + 3: "PACK (reads 0)"})
        for n in range(len(self.pages) // PAGE):
            chunk = self.pages[n * PAGE:(n + 1) * PAGE]
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"p{n:03d}  {chunk.hex(' ').upper()}  |{ascii_}|  {names.get(n, '')}".rstrip())
        return lines


def analyze(mode: str, uid: str, version_hex: str, pages_hex: str, *, device_mac: str,
            public_keys: dict[int, ec.EllipticCurvePublicKey], password: str = "not checked",
            expected_ndef: bytes | None = None, lock_required: bool = False) -> TagReport:
    """Judge one tag from the raw data the phone read. ``device_mac`` = J-Link MAC, 12 hex.
    ``lock_required``: an open tag (AUTH0 above the PACK page) fails the "Locked" check."""
    r = TagReport(mode=mode, uid=str(uid).upper(), password=password)
    add = r.checks.append
    try:
        r.version = hexbytes(version_hex) if version_hex else b""
        r.pages = hexbytes(pages_hex) if pages_hex else b""
    except StickerError as exc:
        add(Check("Tag data", FAIL, f"phone sent bad data: {exc}"))
        return r

    # --- tag ---
    r.chip = CHIPS.get(r.version[6]) if len(r.version) >= 8 else None
    uid_ok = is_hex(r.uid, 7)
    if not uid_ok:
        add(Check("Tag UID", FAIL, f"'{r.uid}' is not a fixed 7-byte UID (random-UID tags cannot be used)"))
    elif r.chip is None:
        add(Check("Tag UID", OK, r.uid))
        add(Check("Chip", FAIL, f"not an NTAG213/215/216 (GET_VERSION {r.version.hex(' ').upper() or 'none'})"))
    else:
        add(Check("Tag UID", OK, r.uid))
        add(Check("Chip", OK, f"{r.chip.name}, {r.chip.user_bytes} B user memory"))
    if r.chip and len(r.pages) >= (r.chip.cfg0 + 2) * PAGE:
        c = r.chip.cfg0 * PAGE
        r.config = TagConfig(r.pages[c:c + 4], r.pages[c + 4:c + 8], r.chip)
        if r.config.cfglck:
            add(Check("Configuration", FAIL, "CFGLCK is set - the configuration is frozen, use another tag"))
    if len(r.pages) >= 16 and r.pages[12] != 0xE1:
        add(Check("NDEF format", FAIL, f"capability container {r.pages[12:16].hex(' ').upper()} is not NDEF (E1 ..)"))

    # --- NDEF records ---
    user_end = (NDEF_START_PAGE * PAGE + r.chip.user_bytes) if r.chip else len(r.pages)
    user = r.pages[NDEF_START_PAGE * PAGE:user_end]
    msg, r.ndef_problem = find_ndef(user) if user else (None, "no pages read")
    if msg:
        try:
            r.records = parse_records(msg)
        except StickerError as exc:
            r.ndef_problem = str(exc)
    mac_rec = next((rec for rec in r.records if rec.text is not None), None)
    sig_rec = next((rec for rec in r.records if rec.is_signature), None)
    if mac_rec is None:
        add(Check("MAC record", FAIL, r.ndef_problem or "no Text record on the tag"))
    else:
        text = mac_rec.text.strip()
        if is_hex(text, 6):
            r.mac_on_tag = text
            add(Check("MAC record", OK, text))
        else:
            add(Check("MAC record", FAIL, f"'{text[:30]}' is not a 12-hex MAC"))
    if r.mac_on_tag:
        same = r.mac_on_tag == device_mac
        add(Check("MAC = device", OK if same else FAIL,
                  f"tag {r.mac_on_tag} = J-Link {device_mac}" if same else f"tag {r.mac_on_tag}  ≠  J-Link {device_mac}"))

    # --- signature ---
    if sig_rec is None:
        r.sig_status = SIG_MISSING
        add(Check("Signature record", FAIL, "no 'sytrak:s' record on the tag"))
    else:
        p = sig_rec.payload
        add(Check("Signature record", OK, f"present, {len(p)} B"))
        if len(p) != SIG_PAYLOAD_LEN:
            r.sig_status = SIG_MALFORMED
        else:
            r.sig_version, r.key_id, r.signature = p[0] >> 4, p[0] & 0x0F, p[1:]
            if r.sig_version != VERSION:
                r.sig_status = SIG_UNSUPPORTED
            elif not r.mac_on_tag:
                r.sig_status = SIG_NO_MAC
            elif r.key_id not in public_keys:
                r.sig_status = SIG_UNKNOWN_KEY
            elif not uid_ok:
                r.sig_status = SIG_INVALID
            else:
                r.sig_status = SIG_VALID if verify(public_keys[r.key_id], r.uid, r.mac_on_tag, r.signature) else SIG_INVALID
        detail = {SIG_VALID: f"valid - key {r.key_id}, signed for UID {r.uid} + MAC {r.mac_on_tag}",
                  SIG_INVALID: "does NOT match this UID + MAC (copied from another tag, or a foreign key)",
                  SIG_MALFORMED: f"payload is {len(p)} B, expected {SIG_PAYLOAD_LEN}",
                  SIG_UNSUPPORTED: f"format version {r.sig_version}, expected {VERSION}",
                  SIG_NO_MAC: "no MAC record to check it against",
                  SIG_UNKNOWN_KEY: f"key ID {r.key_id} is not the configured signing key"}[r.sig_status]
        add(Check("Signature valid", OK if r.sig_status == SIG_VALID else FAIL, detail))

    # --- written bytes (Verification 1) ---
    if expected_ndef is not None:
        got = r.pages[NDEF_START_PAGE * PAGE:NDEF_START_PAGE * PAGE + len(expected_ndef)]
        r.written = got == expected_ndef
        add(Check("Written data", OK if r.written else FAIL,
                  f"{len(expected_ndef)} B read back identical" if r.written
                  else f"read back differs from the {len(expected_ndef)} B written"))

    # --- password / lock ---
    pw_detail = {"validated": "PWD_AUTH answered the expected PACK",
                 "failed": "PWD_AUTH refused or answered a wrong PACK - the tag has another password",
                 "not set": "the password could not be written",
                 "not checked": "not checked (the tag never reached this step)"}
    add(Check("Password", OK if r.password == "validated" else FAIL, pw_detail.get(r.password, r.password)))
    if r.config:
        c = r.config
        if c.protected:
            detail = f"Yes - writes from page {c.auth0} need the password, reads stay open  (AUTH0 {c.auth0:02X})"
            add(Check("Locked", OK if lock_required else INFO, detail))
        elif lock_required:
            add(Check("Locked", FAIL, f"No - the tag is open (AUTH0 {c.auth0:02X}); write it again with Verification 1"))
        else:
            add(Check("Locked", INFO, f"No - locking is switched off in Settings  (AUTH0 {c.auth0:02X})"))
    elif lock_required:
        add(Check("Locked", FAIL, "configuration pages not read"))
    return r
