"""Google Sheet = the only record store (service account access).

Columns (header row written automatically):
  A SEQ | B MAC | C Date Time | D Device Type                 <- written by Save
  E Verified | F Manually Verified | G NFC Written | H Updated At  <- written by Verify QR
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..paths import app_dir, user_data_dir
from .qr import MAX_SEQ, build_payload, format_seq, normalize_mac

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"
HEADER = ["SEQ", "MAC", "Date Time", "Device Type", "Verified", "Manually Verified", "NFC Written", "Updated At"]
LAST_COL = chr(ord("A") + len(HEADER) - 1)  # "H"
YES = "Yes"
KEY_FILE_NAME = "google_service_account.json"

_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID_RE = re.compile(r"[#?&]gid=(\d+)")


class SheetError(Exception):
    pass


@dataclass(frozen=True)
class SheetRecord:
    seq: int
    mac: str
    created_at: str
    device_type: str = ""
    verified: bool = False
    manually_verified: bool = False
    nfc_written: bool = False
    row: int = field(default=0, compare=False)  # 1-based sheet row (0 = unknown)

    @property
    def seq_text(self) -> str:
        return format_seq(self.seq)

    @property
    def status(self) -> str:
        parts = []
        if self.verified:
            parts.append("Verified")
        if self.manually_verified:
            parts.append("Manual")
        if self.nfc_written:
            parts.append("NFC ✔")
        return " · ".join(parts)

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
            verified=_is_yes(cells[4]), manually_verified=_is_yes(cells[5]), nfc_written=_is_yes(cells[6]),
            row=index + 1,
        ))
    return records


def audit_rows(rows: list[list[str]]) -> tuple[list[SheetRecord], list[str]]:
    """Parse a whole tab and list every problem: invalid rows, duplicate SEQs, duplicate MACs."""
    records = parse_rows(rows)
    problems: list[str] = []
    parsed_rows = {r.row for r in records}
    for index, row in enumerate(rows[1:], start=2):  # row 1 is the header
        cells = [str(c).strip() for c in row]
        if not any(cells) or index in parsed_rows:
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
    return records, problems


def _is_yes(cell: str) -> bool:
    return cell.strip().lower() in ("yes", "y", "true", "1", "✔")


def find_key_file(configured: str = "") -> Path | None:
    """Configured path → <app folder>/secrets/*.json → %APPDATA%/SyTrakGen/google_service_account.json"""
    candidates = [Path(configured)] if configured else []
    candidates += sorted((app_dir() / "secrets").glob("*.json"))
    candidates.append(user_data_dir() / KEY_FILE_NAME)
    return next((p for p in candidates if p.is_file()), None)


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

    def _write_header(self, tab: str) -> None:
        self._call("PUT", f"{API}/{self.sheet_id}/values/{self._quote_range(tab, f'A1:{LAST_COL}1')}",
                   params={"valueInputOption": "RAW"}, json={"values": [HEADER]})

    def ensure_header(self) -> None:
        if self._header_ok:
            return
        values = self._call("GET", f"{API}/{self.sheet_id}/values/{self._range(f'A1:{LAST_COL}1')}").get("values")
        if not values or len(values[0]) < len(HEADER):
            # Empty tab, or an older header without the newer columns (they are only ever appended).
            self._write_header(self.tab_title())
        self._header_ok = True

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
        return self._call("GET", f"{API}/{self.sheet_id}/values/{self._range(f'A:{LAST_COL}')}").get("values", [])

    def records(self) -> list[SheetRecord]:
        return parse_rows(self._rows())

    def audit(self) -> tuple[list[SheetRecord], list[str]]:
        """Fresh read of the whole tab + every uniqueness / validity problem found."""
        return audit_rows(self._rows())

    def update_status(self, mac: str, *, verified: bool | None = None, manually_verified: bool | None = None,
                      nfc_written: bool | None = None) -> SheetRecord:
        """Set the Verify-QR columns (E-H) on the row of ``mac``; ``None`` leaves a column unchanged."""
        self.ensure_header()
        record = next((r for r in self.records() if r.mac == mac), None)
        if record is None or not record.row:
            raise SheetError(f"MAC {build_payload(mac)} not found in tab '{self.tab_title()}'")
        flags = [
            record.verified if verified is None else verified,
            record.manually_verified if manually_verified is None else manually_verified,
            record.nfc_written if nfc_written is None else nfc_written,
        ]
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cells = [YES if f else "" for f in flags] + [updated_at]
        self._call("PUT", f"{API}/{self.sheet_id}/values/{self._range(f'E{record.row}:{LAST_COL}{record.row}')}",
                   params={"valueInputOption": "RAW"}, json={"values": [cells]})
        log.info("Sheet row %d (%s): verified=%s manual=%s nfc=%s", record.row, mac, *flags)
        return SheetRecord(record.seq, record.mac, record.created_at, record.device_type, *flags, row=record.row)

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
                            json={"values": [[format_seq(seq), mac, created_at, device_type]]})
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
