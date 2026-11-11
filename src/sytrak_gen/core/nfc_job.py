"""One NFC job = one tag write (Verification 1) or one tag check (Verification 2), driven by the PC.

  PC  cmd/nfc_job        {job_id, mode, mac, seq}                 -> phone waits for a tap
  ph  evt/nfc_tag        {job_id, uid, version, pages}            -> NfcJob.on_tag() checks + signs
  PC  cmd/nfc_job_reply  {job_id, ok, reason, write, pwd, pack, lock}
  ph  evt/nfc_progress   {job_id, step, ok, detail}               (log only)
  ph  evt/nfc_done       {job_id, ok, step, error, uid, version, pages, password, pwd_written, steps}
                                                                  -> NfcJob.on_done() judges the tag
  PC  cmd/nfc_verdict    {job_id, ok, mode, headline, checks}     -> phone shows the result

No Tk here: the Verify tab drives it and shows the results.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from . import nfc_sticker as st
from .qr import compact_mac
from .secrets import Signer

WRITE, VERIFY = "write", "verify"
JOB_TIMEOUT_S = 120


class JobError(Exception):
    pass


def registered_uid(record) -> str:
    """The tag registered for a sheet row: its Verification 1 Tag UID, once NFC Written = Yes."""
    return record.v1.uid.strip().upper() if record is not None and record.v1.nfc and record.v1.uid.strip() else ""


def tag_owner_problem(mode: str, mac: str, uid: str, records: list) -> str:
    """One tag UID belongs to one MAC only. ``mac``: 12 hex; ``records``: fresh rows of the sheet tab.

    Returns why this tag may not be used for this device ("" = fine)."""
    uid = uid.upper()
    me = next((r for r in records if compact_mac(r.mac) == mac), None)
    for r in records:
        if compact_mac(r.mac) == mac:
            continue
        owned = {registered_uid(r)} | ({r.v2.uid.strip().upper()} if r.v2.nfc else set())
        if uid in owned:
            return (f"tag {uid} already belongs to MAC {compact_mac(r.mac)} (SEQ {r.seq_text}, row {r.row}) - "
                    "one tag per device: use this device's own tag")
    mine = registered_uid(me)
    if mode == WRITE:
        if mine and mine != uid:
            return (f"this device already has tag {mine} (Verification 1) - use that tag, or clear its "
                    "Tag UID in the sheet to give it a new tag")
        return ""
    if me is None:
        return "this MAC is not in the sheet tab"
    if not mine:
        return "no tag is registered for this device yet - Verification 1 must pass first"
    if mine != uid:
        return f"wrong tag: this device's tag is {mine}, the tapped tag is {uid}"
    return ""


@dataclass
class NfcJob:
    mode: str                       # WRITE (Verification 1) or VERIFY (Verification 2)
    mac: str                        # device MAC, 12 uppercase hex
    seq: str
    signer: Signer
    lock: bool = True               # write: lock the tag (AUTH0 = 02); both: a tag that is not locked fails
    job_id: str = field(default_factory=lambda: "j-" + uuid.uuid4().hex[:10])
    started: float = field(default_factory=time.monotonic)
    state: str = "waiting"          # waiting -> working -> done | failed | cancelled
    uid: str = ""
    ndef: bytes | None = None
    reply: dict | None = None       # cached: a resent evt/nfc_tag gets the same answer
    report: st.TagReport | None = None
    owner_check: st.Check | None = None   # "Tag UID ↔ MAC" result from the sheet (one tag per device)
    error: str = ""
    steps: list[dict] = field(default_factory=list)

    @property
    def group(self) -> int:
        return 1 if self.mode == WRITE else 2

    @property
    def active(self) -> bool:
        return self.state in ("waiting", "working")

    @property
    def expired(self) -> bool:
        return self.active and time.monotonic() - self.started > JOB_TIMEOUT_S

    def request(self) -> dict:
        return {"job_id": self.job_id, "mode": self.mode, "mac": self.mac, "seq": self.seq}

    # ------------------------------------------------------------ evt/nfc_tag ---

    def on_tag(self, data: dict, records: list | None = None) -> dict:
        """First touch of a tag: check it, sign it. Returns cmd/nfc_job_reply (also for a refusal).

        ``records``: fresh rows of the sheet tab for the one-tag-per-device rule. Verification 1
        refuses a tag that belongs to another device before anything is written; Verification 2
        reads the tag anyway and fails the "Tag UID ↔ MAC" check, so the result is recorded."""
        uid = str(data.get("uid", "")).upper()
        if self.reply is not None:
            if uid == self.uid:
                return self.reply                     # phone resent: same answer, same signature
            return self._refuse(f"another tag ({uid}) was tapped - tap the first tag ({self.uid}) or start again")
        if not self.active:
            return self._refuse("this job is no longer active on the PC")
        problem = self._tag_problem(uid, data)
        owner = tag_owner_problem(self.mode, self.mac, uid, records) if records is not None and not problem else ""
        if owner and self.mode == WRITE:
            problem = owner
        if problem:   # the job stays open: the operator can tap another tag
            self.error = problem
            return self._refuse(problem)
        if records is None:
            self.owner_check = st.Check("Tag UID ↔ MAC", st.FAIL, "the sheet could not be checked")
        elif owner:
            self.owner_check = st.Check("Tag UID ↔ MAC", st.FAIL, owner)
        else:
            me = next((r for r in records if compact_mac(r.mac) == self.mac), None)
            known = registered_uid(me) == uid
            self.owner_check = st.Check("Tag UID ↔ MAC", st.OK,
                                        f"tag {uid} is this device's registered tag" if known else
                                        f"tag {uid} is free - it becomes this device's tag (one tag per device)")
        self.uid = uid
        pwd, pack = self.signer.password(uid)
        write = None
        if self.mode == WRITE:
            self.ndef = self.signer.ndef(uid, self.mac)
            write = {"page": st.NDEF_START_PAGE, "data": self.ndef.hex().upper()}
        self.state = "working"
        self.reply = {"job_id": self.job_id, "ok": True, "reason": "", "write": write,
                      "pwd": pwd.hex().upper(), "pack": pack.hex().upper(),
                      "lock": self.lock and self.mode == WRITE}
        return self.reply

    def _refuse(self, reason: str) -> dict:
        return {"job_id": self.job_id, "ok": False, "reason": reason}

    def _tag_problem(self, uid: str, data: dict) -> str:
        if not st.is_hex(uid, 7):
            return f"UID '{uid or '?'}' is not a fixed 7-byte UID - this tag cannot be used, take another one"
        try:
            version = st.hexbytes(data.get("version", ""))
            pages = st.hexbytes(data.get("pages", ""))
        except st.StickerError:
            return "the phone sent unreadable tag data - tap again"
        chip = st.CHIPS.get(version[6]) if len(version) >= 8 else None
        if chip is None:
            return "not an NTAG213/215/216 tag - take another one"
        if self.mode == VERIFY:
            return ""
        if len(pages) < (chip.cfg0 + 2) * st.PAGE:
            return "the phone could not read the whole tag - hold it still and tap again"
        config = st.TagConfig(pages[chip.cfg0 * 4:chip.cfg0 * 4 + 4], pages[chip.cfg0 * 4 + 4:chip.cfg0 * 4 + 8], chip)
        if config.cfglck:
            return "the tag's configuration is frozen (CFGLCK) - take another one"
        if pages[12] != 0xE1:
            return f"the tag is not NDEF formatted (CC {pages[12:16].hex(' ').upper()}) - take another one"
        if pages[10] or pages[11]:
            return "the tag has lock bits set (pages may be read-only) - take another one"
        if len(st.build_ndef(self.mac, bytes(st.SIG_PAYLOAD_LEN))) > chip.user_bytes:
            return f"the sticker data does not fit on a {chip.name}"
        return ""

    # ----------------------------------------------------------- evt/nfc_done ---

    def on_done(self, data: dict) -> st.TagReport:
        """The phone finished: judge the tag from what it read. Sets state done/failed."""
        uid = str(data.get("uid", "")).upper()
        self.steps = [s for s in data.get("steps", []) if isinstance(s, dict)]
        if self.uid and uid and uid != self.uid:
            raise JobError(f"the phone finished on another tag ({uid}, expected {self.uid})")
        self.uid = self.uid or uid
        self.report = st.analyze(self.mode, self.uid, str(data.get("version", "")), str(data.get("pages", "")),
                                 device_mac=self.mac, public_keys=self.signer.public_keys,
                                 password=str(data.get("password") or "not checked"),
                                 expected_ndef=self.ndef if self.mode == WRITE else None,
                                 lock_required=self.lock)
        if self.owner_check is not None:
            self.report.checks.insert(1, self.owner_check)
        if not data.get("ok"):
            self.error = str(data.get("error") or "the phone stopped")
            step = str(data.get("step") or "")
            self.report.checks.insert(0, st.Check("Phone", st.FAIL, f"{self.error}" + (f"  (step: {step})" if step else "")))
        self.state = "done" if self.report.ok else "failed"
        return self.report

    def verdict(self, sheet_note: str = "") -> dict:
        """cmd/nfc_verdict: what the phone shows the operator."""
        r = self.report
        ok = bool(r and r.ok)
        title = ("SIGNED STICKER WRITTEN AND VERIFIED" if self.mode == WRITE else "STICKER VERIFIED") if ok else "NOT VERIFIED"
        checks = [{"title": c.title, "state": c.state, "detail": c.detail} for c in (r.checks if r else [])]
        return {"job_id": self.job_id, "ok": ok, "mode": self.mode, "headline": title,
                "uid": self.uid, "mac": self.mac, "seq": self.seq, "checks": checks,
                "error": "" if ok else (self.error or "; ".join(c.title for c in (r.failures if r else []))),
                "sheet": sheet_note}
