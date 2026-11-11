"""A virtual NTAG21x and the phone's tag algorithm, for tests and tools/mobile_sim.py.

``PhoneDriver`` is the reference for what the mobile app does with a real tag
(sytrak_mobile/lib/services/ntag.dart follows the same steps), using raw
commands: GET_VERSION 60, READ 30 pg, WRITE A2 pg d0..d3, PWD_AUTH 1B p0..p3.
"""
from __future__ import annotations

import time

from .nfc_sticker import CHIPS, NDEF_START_PAGE, PAGE, Chip

GET_VERSION, READ, WRITE, PWD_AUTH = 0x60, 0x30, 0xA2, 0x1B
MAX_TRIES = 3


class TagIOError(IOError):
    """A NAK and an RF dropout look the same to the phone (spec §8)."""


class FakeNtag:
    """NTAG213 by default. PWD/PACK read as zeros; a refused command halts the tag until reconnect()."""

    VERSIONS = {"NTAG213": 0x0F, "NTAG215": 0x11, "NTAG216": 0x13}

    def __init__(self, uid_hex: str, chip: str = "NTAG213"):
        self.size_byte = self.VERSIONS[chip]
        self.chip: Chip = CHIPS[self.size_byte]
        self.uid = bytes.fromhex(uid_hex)
        self.mem = bytearray((self.chip.last_page + 1) * PAGE)
        self.mem[0:3] = self.uid[0:3]
        self.mem[4:8] = self.uid[3:7]
        self.mem[12:16] = bytes([0xE1, 0x10, self.chip.user_bytes // 8, 0x00])     # CC
        self.mem[16:20] = bytes([0x03, 0x00, 0xFE, 0x00])                           # empty NDEF
        c = self.chip.cfg0 * PAGE
        self.mem[c:c + 4] = bytes([0x04, 0x00, 0x00, 0xFF])                         # AUTH0 FF = open
        self.mem[c + 4:c + 8] = bytes([0x00, 0x05, 0x00, 0x00])
        self.mem[c + 8:c + 12] = b"\xFF" * 4                                       # factory PWD
        self.authed = self.halted = False
        self.drop_write_at: set[int] = set()   # pages whose next write is lost (RF dropout)

    # --- test helpers ---
    def load(self, page: int, data: bytes) -> None:
        self.mem[page * PAGE:page * PAGE + len(data)] = data

    def version(self) -> bytes:
        return bytes([0x00, 0x04, 0x04, 0x02, 0x01, 0x00, self.size_byte, 0x03])

    def dump(self) -> bytes:
        out = bytearray(self.mem)
        out[(self.chip.cfg0 + 2) * PAGE:] = b"\x00" * (2 * PAGE)
        return bytes(out)

    @property
    def pwd(self) -> bytes:
        return bytes(self.mem[(self.chip.cfg0 + 2) * PAGE:(self.chip.cfg0 + 3) * PAGE])

    @property
    def auth0(self) -> int:
        return self.mem[self.chip.cfg0 * PAGE + 3]

    # --- the RF interface ---
    def reconnect(self) -> None:
        self.authed = self.halted = False

    def _refuse(self, why: str):
        self.halted = True
        raise TagIOError(why)

    def transceive(self, cmd: bytes) -> bytes:
        if self.halted:
            raise TagIOError("tag halted (no answer)")
        op, pages = cmd[0], self.chip.last_page + 1
        if op == GET_VERSION:
            return self.version()
        if op == READ:
            dump = self.dump()
            return b"".join(dump[((cmd[1] + i) % pages) * PAGE:((cmd[1] + i) % pages + 1) * PAGE] for i in range(4))
        if op == WRITE:
            pg = cmd[1]
            if pg in self.drop_write_at:
                self.drop_write_at.discard(pg)
                self._refuse("RF dropout")
            if pg < 2 or pg >= pages or (pg >= self.auth0 and not self.authed):
                self._refuse("write refused (NAK)")
            self.mem[pg * PAGE:(pg + 1) * PAGE] = cmd[2:6]
            return b"\x0A"
        if op == PWD_AUTH:
            if bytes(cmd[1:5]) == self.pwd:
                self.authed = True
                pack = (self.chip.cfg0 + 3) * PAGE
                return bytes(self.mem[pack:pack + 2])
            self._refuse("wrong password")
        self._refuse("unknown command")


class PhoneDriver:
    """What the phone does with a tag for a job. Results are the evt/nfc_done payload."""

    def __init__(self, tag: FakeNtag, on_step=None):
        self.tag = tag
        self.on_step = on_step
        self.steps: list[dict] = []
        self.chip: Chip | None = None
        self.pages = b""
        self.version = b""

    # --- primitives ---
    def _step(self, step: str, ok: bool, detail: str = "") -> None:
        item = {"step": step, "ok": ok, "detail": detail}
        self.steps.append(item)
        if self.on_step:
            self.on_step(item)

    def _reconnect(self) -> None:
        self.tag.reconnect()
        time.sleep(0.02)   # ~20 ms after a field reset (spec §8)

    def _read_all(self) -> bytes:
        last = self.chip.last_page
        out = bytearray()
        for page in range(0, last + 1, 4):
            out += self.tag.transceive(bytes([READ, page]))
        return bytes(out[:(last + 1) * PAGE])

    def _auth(self, pwd: bytes, pack: bytes) -> bool:
        try:
            return self.tag.transceive(bytes([PWD_AUTH]) + pwd) == pack
        except TagIOError:
            return False

    def _write_pages(self, start: int, data: bytes, pwd: bytes, pack: bytes, protected: bool) -> None:
        """Write page by page; on a dropout reconnect (+ PWD_AUTH) and resume from that page."""
        pos, failures = 0, 0
        while pos < len(data):
            page = start + pos // PAGE
            try:
                self.tag.transceive(bytes([WRITE, page]) + data[pos:pos + PAGE])
                pos += PAGE
            except TagIOError as exc:
                failures += 1
                if failures >= MAX_TRIES:
                    raise
                self._step("write", True, f"page {page}: {exc} - reconnect and retry from page {page}")
                self._reconnect()
                if protected and not self._auth(pwd, pack):
                    raise TagIOError("PWD_AUTH refused after reconnect")

    def _done(self, ok: bool, step: str = "", error: str = "", password: str = "not checked",
              pwd_written: bool = False, locked: bool = False) -> dict:
        try:
            if self.tag.halted:
                self._reconnect()
            pages = self._read_all().hex().upper() if self.chip else self.pages.hex().upper()
        except TagIOError as exc:
            self._step("final read", False, str(exc))
            pages = self.pages.hex().upper()
        return {"ok": ok, "step": step, "error": error, "uid": self.tag.uid.hex().upper(),
                "version": self.version.hex().upper(), "pages": pages, "password": password,
                "pwd_written": pwd_written, "locked": locked, "steps": self.steps}

    # --- phases ---
    def inspect(self) -> dict:
        """First touch: UID, GET_VERSION, every page. Sent to the PC as evt/nfc_tag."""
        self.version = self.tag.transceive(bytes([GET_VERSION]))
        self.chip = CHIPS.get(self.version[6]) if len(self.version) >= 8 else None
        if self.chip:
            self.pages = self._read_all()
        self._step("tag", True, f"UID {self.tag.uid.hex().upper()}, " + (self.chip.name if self.chip else "unknown chip"))
        return {"uid": self.tag.uid.hex().upper(), "version": self.version.hex().upper(), "pages": self.pages.hex().upper()}

    def write_job(self, ndef: bytes, pwd: bytes, pack: bytes, lock: bool = False) -> dict:
        try:
            if self.chip is None:
                self.inspect()
            if self.chip is None:
                return self._done(False, "check", "Not an NTAG213/215/216")
            c = self.chip.cfg0 * PAGE
            cfg0, cfg1 = self.pages[c:c + 4], self.pages[c + 4:c + 8]
            if cfg1[0] & 0x40:
                return self._done(False, "check", "CFGLCK set - configuration frozen, use another tag")
            protected = cfg0[3] <= self.chip.last_page
            if protected and not self._auth(pwd, pack):
                return self._done(False, "pwd_auth", "Tag is locked with another password")

            self._write_pages(NDEF_START_PAGE, ndef, pwd, pack, protected)
            self._step("write", True, f"{len(ndef) // PAGE} pages from page {NDEF_START_PAGE}")
            for attempt in range(MAX_TRIES):
                back = self._read_all()[NDEF_START_PAGE * PAGE:NDEF_START_PAGE * PAGE + len(ndef)]
                if back == ndef:
                    self._step("readback", True, f"{len(ndef)} B identical")
                    break
                bad = next(i for i in range(0, len(ndef), PAGE) if back[i:i + PAGE] != ndef[i:i + PAGE])
                self._step("readback", False, f"page {NDEF_START_PAGE + bad // PAGE} differs - writing again")
                self._write_pages(NDEF_START_PAGE + bad // PAGE, ndef[bad:], pwd, pack, protected)
            else:
                return self._done(False, "readback", "Read-back still differs after 3 tries")

            self._write_pages(self.chip.cfg0 + 2, pwd + pack + b"\x00\x00", pwd, pack, protected)
            self._step("password", True, "PWD and PACK written")
            self._reconnect()
            validated = self._auth(pwd, pack)
            self._step("pwd_auth", validated, "PACK matches" if validated else "PWD_AUTH refused / wrong PACK")
            locked = False
            if not lock:
                self._step("lock", True, "skipped - locking is switched off on the PC")
            elif not validated:
                self._step("lock", False, "not locked - the password check failed")
            else:
                # 6. ACCESS: keep CFG1 bytes 1-3; clear PROT (reads stay open), CFGLCK and AUTHLIM.
                self._write_pages(self.chip.cfg0 + 1, bytes([cfg1[0] & 0x38]) + cfg1[1:4], pwd, pack, True)
                # 7. AUTH0 = 02 - always the last write.
                self._write_pages(self.chip.cfg0, cfg0[0:3] + b"\x02", pwd, pack, True)
                # 8. Read CFG0 back.
                locked = self.tag.transceive(bytes([READ, self.chip.cfg0]))[3] == 0x02
                self._step("lock", locked, "AUTH0 = 02: locked, reads stay open" if locked else "AUTH0 read back is not 02")
            # ok = every step ran; the PC judges the result (a failed password shows up as Password = failed).
            return self._done(True, password="validated" if validated else "failed", pwd_written=True, locked=locked)
        except TagIOError as exc:
            return self._done(False, "write", f"{exc} - hold the tag still and try again")

    def verify_job(self, pwd: bytes, pack: bytes) -> dict:
        try:
            if self.chip is None:
                self.inspect()
            validated = self._auth(pwd, pack)
            self._step("pwd_auth", validated, "PACK matches" if validated else "PWD_AUTH refused / wrong PACK")
            return self._done(True, password="validated" if validated else "failed")
        except TagIOError as exc:
            return self._done(False, "read", str(exc))
