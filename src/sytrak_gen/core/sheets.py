"""Google Sheet = the only record store (service account access).

Two header rows (written automatically), data from row 3:

  row 1 |                                    | Verification 1 (merged E-L)       | Verification 2 (merged M-T)
  row 2 | A SEQ | B MAC | C Date Time | D Device Type
        | E Verified | F Manually Verified | G NFC Written | H Tag UID | I Signature | J Password | K Locked | L Updated At
        | M Verified | N Manually Verified | O NFC Read    | P Tag UID | Q Signature | R Password | S Locked | T Updated At

  A-D are written by Save. Each verification fills its own block: Verified = sticker matched,
  NFC Written = signed sticker written and every check passed (Verification 1), NFC Read = read-only
  check passed (Verification 2). Tag UID / Signature / Password / Locked hold the last NFC result.
  Tabs in an older layout (one header row, or the 12-column blocks) are upgraded in place
  (see SheetsClient.ensure_header).
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .qr import MAX_SEQ, build_payload, format_seq, normalize_mac

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"
NFC_COLUMNS = ["Tag UID", "Signature", "Password", "Locked"]
HEADER = ["SEQ", "MAC", "Date Time", "Device Type",
          "Verified", "Manually Verified", "NFC Written", *NFC_COLUMNS, "Updated At",   # Verification 1
          "Verified", "Manually Verified", "NFC Read", *NFC_COLUMNS, "Updated At"]      # Verification 2
BLOCK = 8                     # columns per verification block
BLOCK_START = {1: 4, 2: 12}   # 0-based column index of each block
GROUP_ROW = [""] * len(HEADER)
GROUP_ROW[BLOCK_START[1]], GROUP_ROW[BLOCK_START[2]] = "Verification 1", "Verification 2"
HEADER_ROWS = 2
LAST_COL = chr(ord("A") + len(HEADER) - 1)  # "T"
# Order of the fields inside a block (= VerifyStatus fields)
BLOCK_FIELDS = ("verified", "manually_verified", "nfc", "uid", "signature", "password", "locked", "updated_at")
GROUP_COLS = {g: dict(zip(BLOCK_FIELDS, (chr(65 + start + i) for i in range(BLOCK)))) for g, start in BLOCK_START.items()}
# The previous layout (12 columns, 4 per block) - upgraded automatically.
LEGACY_HEADER = ["SEQ", "MAC", "Date Time", "Device Type",
                 "Verified", "Manually Verified", "NFC Written", "Updated At",
                 "Verified", "Manually Verified", "NFC Read", "Updated At"]
YES = "Yes"

_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID_RE = re.compile(r"[#?&]gid=(\d+)")


class SheetError(Exception):
    pass


@dataclass(frozen=True)
class VerifyStatus:
    """One verification block of a row."""
    verified: bool = False           # sticker matched device + sheet
    manually_verified: bool = False
    nfc: bool = False                # 1: NFC Written (signed sticker OK), 2: NFC Read (read-only check OK)
    uid: str = ""                    # last NFC result: tag UID, Signature (Valid / Invalid / …),
    signature: str = ""              # Password (Validated / Failed) and Locked (Yes / No)
    password: str = ""
    locked: str = ""
    updated_at: str = ""

    def parts(self, nfc_text: str) -> list[str]:
        return [text for flag, text in ((self.verified, "Verified"), (self.manually_verified, "Manual"),
                                        (self.nfc, nfc_text)) if flag]


@dataclass(frozen=True)
class SheetRecord:
    seq: int
    mac: str
    created_at: str
    device_type: str = ""
    v1: VerifyStatus = field(default_factory=VerifyStatus)  # Verification 1 (write + read)
    v2: VerifyStatus = field(default_factory=VerifyStatus)  # Verification 2 (read only)
    row: int = field(default=0, compare=False)  # 1-based sheet row (0 = unknown)

    @property
    def seq_text(self) -> str:
        return format_seq(self.seq)

    def group(self, number: int) -> VerifyStatus:
        return self.v1 if number == 1 else self.v2

    @property
    def status(self) -> str:
        parts = []
        for name, group, nfc_text in (("V1", self.v1, "NFC written"), ("V2", self.v2, "NFC read")):
            done = group.parts(nfc_text)
            if done:
                parts.append(f"{name}: " + ", ".join(done))
        return "  |  ".join(parts)

    @property
    def payload(self) -> str:
        return build_payload(self.mac)


def parse_sheet_url(url: str) -> tuple[str, int | None]:
    """Return (spreadsheet_id, gid) from a Sheet URL (or a bare id)."""
    url = url.strip()
    m = _ID_RE.search(url)
    sheet_id = m.group(1) if m else url if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url) else ""
    if not sheet_id:
        raise SheetError("Not a Google Sheet URL")
    g = _GID_RE.search(url)
    return sheet_id, int(g.group(1)) if g else None


def parse_rows(rows: list[list[str]]) -> list[SheetRecord]:
    """Turn sheet rows into records, skipping the header and malformed rows."""
    records = []
    for index, row in enumerate(rows):
        if len(row) < 2:
            continue
        seq_text, mac = str(row[0]).strip(), normalize_mac(str(row[1]))
        if not seq_text.isdigit() or not mac:
            continue
        cells = [str(c).strip() for c in row] + [""] * len(HEADER)
        records.append(SheetRecord(
            int(seq_text), mac, cells[2], cells[3],
            v1=_group(cells, 1), v2=_group(cells, 2),
            row=index + 1,
        ))
    return records


def _group(cells: list[str], group: int) -> VerifyStatus:
    c = cells[BLOCK_START[group]:BLOCK_START[group] + BLOCK]
    return VerifyStatus(_is_yes(c[0]), _is_yes(c[1]), _is_yes(c[2]), *c[3:8])


def _is_header_row(index: int, cells: list[str]) -> bool:
    return index <= HEADER_ROWS and (not cells or cells[0] in ("", "SEQ"))


def _trim(row: list[str]) -> list[str]:
    while row and row[-1] == "":
        row.pop()
    return row


def upgrade_legacy_rows(rows: list[list[str]]) -> list[list[str]]:
    """12-column layout (4 columns per block) -> the 20-column layout; header rows replaced.

    Old block 1 E-H (Verified, Manual, NFC Written, Updated At) -> E, F, G, L;
    old block 2 I-L -> M, N, O, T. The new Tag UID / Signature / Password / Locked cells stay empty.
    """
    out = [list(GROUP_ROW), list(HEADER)]
    for row in rows[HEADER_ROWS:]:
        c = [str(x) for x in row] + [""] * len(LEGACY_HEADER)
        blank = [""] * len(NFC_COLUMNS)
        out.append(_trim(c[:4] + c[4:7] + blank + [c[7]] + c[8:11] + blank + [c[11]]))
    return out


def migrate_rows(rows: list[list[str]]) -> list[list[str]]:
    """Oldest single-header layout -> the current two-header layout (returned rows include both header rows).

    Old columns: E Verified | F Manually Verified | G NFC Written | H Updated At
                 [I NFC Verified / Verification 1] [J Verification 2]
    Old E-H become the Verification 1 block; an old I "Yes" also counts as NFC Written;
    an old J "Yes" becomes Verification 2 NFC Read.
    """
    legacy = [[""] * len(LEGACY_HEADER), list(LEGACY_HEADER)]
    for row in rows[1:]:
        c = [str(x) for x in row] + [""] * len(LEGACY_HEADER)
        nfc_written = c[6] or (YES if _is_yes(c[8]) else "")
        nfc_read = YES if _is_yes(c[9]) else ""
        legacy.append(c[:6] + [nfc_written, c[7], "", "", nfc_read, c[7] if nfc_read else ""])
    return upgrade_legacy_rows(legacy)


def audit_rows(rows: list[list[str]]) -> tuple[list[SheetRecord], list[str]]:
    """Parse a whole tab and list every problem: invalid rows, duplicate SEQs, duplicate MACs."""
    records = parse_rows(rows)
    problems: list[str] = []
    parsed_rows = {r.row for r in records}
    for index, row in enumerate(rows, start=1):
        cells = [str(c).strip() for c in row]
        if not any(cells) or index in parsed_rows or _is_header_row(index, cells):
            continue
        seq_cell = cells[0] if cells else ""
        mac_cell = cells[1] if len(cells) > 1 else ""
        problems.append(f"Row {index}: invalid entry (SEQ '{seq_cell}', MAC '{mac_cell}')")

    by_seq: dict[int, list[SheetRecord]] = {}
    by_mac: dict[str, list[SheetRecord]] = {}
    for r in records:
        by_seq.setdefault(r.seq, []).append(r)
        by_mac.setdefault(r.mac, []).append(r)
    for seq, group in sorted(by_seq.items()):
        if len(group) > 1:
            problems.append(f"SEQ {format_seq(seq)} is used {len(group)} times: rows "
                            + ", ".join(str(r.row) for r in group))
    for mac, group in sorted(by_mac.items(), key=lambda kv: kv[1][0].row):
        if len(group) > 1:
            problems.append(f"MAC {build_payload(mac)} is used {len(group)} times: rows "
                            + ", ".join(f"{r.row} (SEQ {r.seq_text})" for r in group))
    # One NFC tag per device: a tag UID that passed for one MAC must not appear on another.
    by_uid: dict[str, dict[str, SheetRecord]] = {}
    for r in records:
        for g in (r.v1, r.v2):
            if g.nfc and g.uid.strip():
                by_uid.setdefault(g.uid.strip().upper(), {})[r.mac] = r
    for uid, owners in by_uid.items():
        if len(owners) > 1:
            problems.append(f"Tag UID {uid} is used for {len(owners)} devices: "
                            + ", ".join(f"row {r.row} (MAC {build_payload(r.mac)})" for r in owners.values()))
    return records, problems


def _is_yes(cell: str) -> bool:
    return cell.strip().lower() in ("yes", "y", "true", "1", "✔")


def find_key_file(configured: str = "") -> Path | None:
    """Configured path → <app folder>/secrets/*.json → <data folder>/secrets/google_service_account.json
    (→ <data folder>/google_service_account.json, where older versions kept it)."""
    from .secrets import GOOGLE_KEY, find
    return find(GOOGLE_KEY, configured)


class SheetsClient:
    """Blocking client - call from worker threads, never the Tk thread."""

    def __init__(self, key_file: Path, sheet_url: str, tab: str = "", timeout: float = 20):
        self.key_file = key_file
        self.sheet_id, self.gid = parse_sheet_url(sheet_url)
        self.tab = tab.strip()
        self.timeout = timeout
        self._session = None
        self._tab: str | None = None
        self._header_ok = False

    @property
    def service_email(self) -> str:
        try:
            return json.loads(self.key_file.read_text(encoding="utf-8")).get("client_email", "")
        except (OSError, ValueError):
            return ""

    def _http(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            try:
                creds = service_account.Credentials.from_service_account_file(str(self.key_file), scopes=SCOPES)
            except (ValueError, OSError) as exc:
                raise SheetError(f"Invalid service account key: {exc}") from exc
            self._session = AuthorizedSession(creds)
        return self._session

    # Google allows ~60 reads + 60 writes per minute per account (all stations share the
    # service account). Rate-limit and temporary errors are retried with exponential backoff.
    RETRY_STATUS = {429, 500, 502, 503, 504}
    RETRY_DELAYS = (1, 2, 4, 8, 16, 32)

    def _call(self, method: str, url: str, **kwargs) -> dict:
        attempt = 0
        while True:
            try:
                resp = self._http().request(method, url, timeout=self.timeout, **kwargs)
            except SheetError:
                raise
            except Exception as exc:  # network / token refresh errors
                raise SheetError("Cannot reach Google - check the internet connection") from exc
            if resp.status_code not in self.RETRY_STATUS or attempt >= len(self.RETRY_DELAYS):
                break
            delay = self.RETRY_DELAYS[attempt]
            attempt += 1
            wait = delay + random.uniform(0, delay / 2)
            log.warning("Google Sheets %s (attempt %d) - retrying in %.1fs", resp.status_code, attempt, wait)
            time.sleep(wait)
        if resp.status_code >= 400:
            try:
                message = resp.json()["error"]["message"]
            except Exception:
                message = resp.text[:200]
            if resp.status_code == 403:
                message += f" - share the sheet with {self.service_email} as Editor"
            raise SheetError(f"Google Sheets error {resp.status_code}: {message}")
        return resp.json() if resp.content else {}

    def _tabs_meta(self) -> list[dict]:
        meta = self._call("GET", f"{API}/{self.sheet_id}", params={"fields": "sheets.properties(sheetId,title)"})
        return [s["properties"] for s in meta.get("sheets", [])]

    def tabs(self) -> list[str]:
        """Tab names in spreadsheet order."""
        return [s["title"] for s in self._tabs_meta()]

    def tab_title(self) -> str:
        """The tab records are read from / written to."""
        if self._tab is None:
            sheets = self._tabs_meta()
            if self.tab:
                match = next((s for s in sheets if s["title"] == self.tab), None)
                if match is None:
                    raise SheetError(f"Tab '{self.tab}' not found in the spreadsheet")
            else:
                match = next((s for s in sheets if s["sheetId"] == self.gid), None) if self.gid is not None else None
                match = match or (sheets[0] if sheets else None)
            if match is None:
                raise SheetError("Spreadsheet has no tabs")
            self._tab = match["title"]
        return self._tab

    @staticmethod
    def _quote_range(tab: str, cells: str) -> str:
        return quote(f"'{tab.replace(chr(39), chr(39) * 2)}'!{cells}", safe="")

    def _range(self, cells: str) -> str:
        return self._quote_range(self.tab_title(), cells)

    def _sheet_gid(self, tab: str) -> int:
        match = next((s for s in self._tabs_meta() if s["title"] == tab), None)
        if match is None:
            raise SheetError(f"Tab '{tab}' not found in the spreadsheet")
        return match["sheetId"]

    def _write_header(self, tab: str) -> None:
        self._call("PUT", f"{API}/{self.sheet_id}/values/{self._quote_range(tab, f'A1:{LAST_COL}{HEADER_ROWS}')}",
                   params={"valueInputOption": "RAW"}, json={"values": [GROUP_ROW, HEADER]})
        self._format_header(tab)

    def _format_header(self, tab: str) -> None:
        """Merge the two group titles, bold + centre the header rows, freeze them (cosmetic only)."""
        try:
            gid = self._sheet_gid(tab)
            rows = {"sheetId": gid, "startRowIndex": 0, "endRowIndex": HEADER_ROWS,
                    "startColumnIndex": 0, "endColumnIndex": len(HEADER)}
            group_ranges = [{"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
                             "startColumnIndex": start, "endColumnIndex": start + BLOCK}
                            for start in BLOCK_START.values()]
            requests = [{"unmergeCells": {"range": {**rows, "endRowIndex": 1}}}]
            requests += [{"mergeCells": {"range": r, "mergeType": "MERGE_ALL"}} for r in group_ranges]
            requests += [
                {"repeatCell": {"range": rows, "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                               "horizontalAlignment": "CENTER"}}}},
                {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": HEADER_ROWS}},
                                           "fields": "gridProperties.frozenRowCount"}},
            ]
            self._call("POST", f"{API}/{self.sheet_id}:batchUpdate", json={"requests": requests})
        except SheetError as exc:
            log.warning("Could not format the header of tab %s: %s", tab, exc)

    def ensure_header(self) -> None:
        if self._header_ok:
            return
        tab = self.tab_title()
        values = self._call("GET", f"{API}/{self.sheet_id}/values/{self._range(f'A1:{LAST_COL}{HEADER_ROWS}')}").get("values", [])
        rows = [[str(c).strip() for c in r] for r in values] + [[], []]
        first, second = rows[0], rows[1]
        if first[:1] == ["SEQ"]:
            self._migrate(tab)                       # oldest layout: one header row
        elif second == HEADER:
            pass
        elif second == LEGACY_HEADER:
            self._upgrade_legacy(tab)                # 12-column layout: add the NFC columns
        elif first[:1] in ([], [""]) and second[:1] in ([], [""], ["SEQ"]):
            self._write_header(tab)                  # empty tab or incomplete header
        else:  # never write the header over someone's data
            raise SheetError(f"Rows 1-2 of tab '{tab}' are not the SyTrak header - choose or create another tab")
        self._header_ok = True

    def _migrate(self, tab: str) -> None:
        """Insert the group-title row above the old header and move the old status columns into the blocks."""
        old = self._call("GET", f"{API}/{self.sheet_id}/values/{self._range(f'A:{LAST_COL}')}").get("values", [])
        if not old or not old[0] or str(old[0][0]).strip() != "SEQ":
            return  # another station has just migrated it
        log.warning("Upgrading tab %s to the Verification 1 / Verification 2 layout (%d rows)", tab, len(old))
        self._call("POST", f"{API}/{self.sheet_id}:batchUpdate", json={"requests": [{"insertDimension": {
            "range": {"sheetId": self._sheet_gid(tab), "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "inheritFromBefore": False}}]})
        self._write_layout(tab, migrate_rows(old))

    def _upgrade_legacy(self, tab: str) -> None:
        """12-column blocks -> 20-column blocks: move each row's status cells to their new columns."""
        old = self._call("GET", f"{API}/{self.sheet_id}/values/{self._range('A:L')}").get("values", [])
        if len(old) < HEADER_ROWS or [str(c).strip() for c in old[1]] != LEGACY_HEADER:
            return  # another station has just upgraded it
        log.warning("Upgrading tab %s to the layout with Tag UID / Signature / Password / Locked (%d rows)", tab, len(old))
        self._write_layout(tab, upgrade_legacy_rows(old))

    def _write_layout(self, tab: str, new: list[list[str]]) -> None:
        """Write both header rows and the status columns E..T of every data row, in one request."""
        quoted = tab.replace(chr(39), chr(39) * 2)
        data = [{"range": f"'{quoted}'!A1:{LAST_COL}{HEADER_ROWS}", "values": new[:HEADER_ROWS]}]
        status = [(r + [""] * len(HEADER))[4:len(HEADER)] for r in new[HEADER_ROWS:]]
        if status:
            data.append({"range": f"'{quoted}'!E{HEADER_ROWS + 1}:{LAST_COL}{len(new)}", "values": status})
        self._call("POST", f"{API}/{self.sheet_id}/values:batchUpdate", json={"valueInputOption": "RAW", "data": data})
        self._format_header(tab)

    def create_tab(self, name: str) -> str:
        """Add a new tab (with the header row) and return its name."""
        name = name.strip()
        if not name:
            raise SheetError("Enter a tab name")
        if name in self.tabs():
            raise SheetError(f"Tab '{name}' already exists")
        self._call("POST", f"{API}/{self.sheet_id}:batchUpdate",
                   json={"requests": [{"addSheet": {"properties": {"title": name}}}]})
        self._write_header(name)
        log.info("Created sheet tab %s", name)
        return name

    def test(self) -> str:
        meta = self._call("GET", f"{API}/{self.sheet_id}", params={"fields": "properties.title"})
        tab = self.tab_title()
        self.ensure_header()
        return f"{meta['properties']['title']} › {tab}"

    def _rows(self) -> list[list[str]]:
        self.ensure_header()  # an older tab is upgraded before its row numbers are used
        return self._call("GET", f"{API}/{self.sheet_id}/values/{self._range(f'A:{LAST_COL}')}").get("values", [])

    def records(self) -> list[SheetRecord]:
        return parse_rows(self._rows())

    def audit(self) -> tuple[list[SheetRecord], list[str]]:
        """Fresh read of the whole tab + every uniqueness / validity problem found."""
        return audit_rows(self._rows())

    def update_status(self, mac: str, group: int, *, verified: bool | None = None,
                      manually_verified: bool | None = None, nfc: bool | None = None,
                      uid: str | None = None, signature: str | None = None,
                      password: str | None = None, locked: str | None = None) -> SheetRecord:
        """Set the Verification ``group`` (1 or 2) block on the row of ``mac``; ``None`` leaves a cell unchanged."""
        if group not in GROUP_COLS:
            raise ValueError(f"unknown verification {group}")
        record = next((r for r in self.records() if r.mac == mac), None)
        if record is None or not record.row:
            raise SheetError(f"MAC {build_payload(mac)} not found in tab '{self.tab_title()}'")
        cols = GROUP_COLS[group]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Only the cells being changed (+ Updated At) are written, in one request, so two
        # updates that overlap (e.g. Verified then NFC Written) never undo each other.
        flags = {"verified": verified, "manually_verified": manually_verified, "nfc": nfc}
        texts = {"uid": uid, "signature": signature, "password": password, "locked": locked}
        values = {name: (YES if flag else "") for name, flag in flags.items() if flag is not None}
        values.update({name: str(text) for name, text in texts.items() if text is not None})
        values["updated_at"] = now
        tab = self.tab_title().replace("'", "''")
        data = [{"range": f"'{tab}'!{cols[name]}{record.row}", "values": [[value]]} for name, value in values.items()]
        self._call("POST", f"{API}/{self.sheet_id}/values:batchUpdate",
                   json={"valueInputOption": "RAW", "data": data})
        changes = {name: (value == YES if name in flags else value) for name, value in values.items()}
        new = replace(record.group(group), **changes)
        log.info("Sheet row %d (%s): Verification %d = %s", record.row, mac, group, new)
        return replace(record, v1=new if group == 1 else record.v1, v2=new if group == 2 else record.v2)

    MAX_SEQ_RETRIES = 10

    def save(self, mac: str, device_type: str, seq_start: int = 1) -> SheetRecord:
        """Register ``mac`` with a SEQ that is unique in this tab, even with many stations saving at once.

        Write-then-verify: the SEQ is chosen at save time, the row is appended,
        then the tab is read back. Google appends rows strictly one after
        another, so if two stations picked the same SEQ the row higher up in
        the sheet keeps it and the other station renumbers its own row to the
        next free SEQ and checks again.
        """
        if not device_type:
            raise SheetError("Choose the device type first")
        self.ensure_header()
        existing = self.records()
        taken = next((r for r in existing if r.mac == mac), None)
        if taken:
            raise SheetError(f"MAC already registered as SEQ {taken.seq_text}")

        seq = next_seq(existing, seq_start)
        if seq > MAX_SEQ:
            raise SheetError(f"Sequence limit {MAX_SEQ} reached in this tab - create a new tab")
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # RAW keeps "0001" as text instead of turning it into the number 1.
        result = self._call("POST", f"{API}/{self.sheet_id}/values/{self._range('A:D')}:append",
                            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                            json={"values": [[format_seq(seq), build_payload(mac), created_at, device_type]]})  # MAC without colons
        my_row = appended_row(result)

        for _ in range(self.MAX_SEQ_RETRIES):
            records = self.records()
            mine = next((r for r in records if r.row == my_row and r.mac == mac), None) if my_row else None
            mine = mine or next((r for r in records if r.mac == mac), None)
            if mine is None:
                raise SheetError("Row was written but cannot be found when reading back - check the sheet")
            my_row = mine.row
            target = resolve_seq(records, my_row, seq_start)
            if target == mine.seq:  # our SEQ is unique (or we are the earliest row holding it)
                log.info("Saved SEQ %s %s (%s) to row %d", mine.seq_text, mac, device_type, my_row)
                return mine
            if target > MAX_SEQ:
                raise SheetError(f"Sequence limit {MAX_SEQ} reached in this tab - create a new tab")
            log.warning("SEQ %s already used above row %d - renumbering it to %s",
                        mine.seq_text, my_row, format_seq(target))
            self._call("PUT", f"{API}/{self.sheet_id}/values/{self._range(f'A{my_row}')}",
                       params={"valueInputOption": "RAW"}, json={"values": [[format_seq(target)]]})
        raise SheetError("Could not get a unique SEQ (too many stations saving at once) - check the sheet")


def resolve_seq(records: list[SheetRecord], my_row: int, seq_start: int = 1) -> int:
    """The SEQ the row ``my_row`` should end up with so that every SEQ in the tab is unique.

    Every station computes this from the same rows and gets the same answer, so
    simultaneous savers settle in one step. Going down the sheet, the first row
    with a SEQ keeps it; each later duplicate takes the lowest number above the
    rows before it that no row is using - unique and without gaps.
    """
    used = {r.seq for r in records}
    top = seq_start - 1          # highest SEQ kept by the rows above
    taken: set[int] = set()      # SEQs settled so far, going down
    for r in sorted(records, key=lambda r: r.row):
        if r.seq in taken:
            final = top + 1
            while final in used or final in taken:
                final += 1
        else:
            final = r.seq
        taken.add(final)
        top = max(top, final)
        if r.row == my_row:
            return final
    return top + 1


def appended_row(append_response: dict) -> int:
    """Row number from a values:append response ("'Tab'!A12:D12" -> 12); 0 if unknown."""
    rng = append_response.get("updates", {}).get("updatedRange", "")
    m = re.search(r"![A-Z]+(\d+)", rng)
    return int(m.group(1)) if m else 0


def next_seq(records: list[SheetRecord], start: int = 1) -> int:
    return max([start] + [r.seq + 1 for r in records])
