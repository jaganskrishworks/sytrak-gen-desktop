"""Anchor devices (a separate product from the tags): records read from their own Google Sheet.

The anchor sheet is read-only for this app. Its header row names the columns, in any order:

    Ethernet MAC | CDK MAC      | CDK ID           | SEQ | Device Type
    907069F452B4 | 907069F452B4 | C0B7FCC2B563613B | 1   | UWB-ANC01

Used: Ethernet MAC, CDK MAC (printed as "ID" and used in the QR), SEQ, Device Type.
The CDK ID column is ignored. The header is found by name (first row that has
"Ethernet MAC" and "SEQ"), so extra columns or a title row above it do not matter.
SEQ is printed with 4 digits (1 -> 0001).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .qr import MAX_SEQ, format_seq
from .sheets import API, SheetsClient

# Header names (compared case-insensitively, spaces ignored)
COL_ETH = "ethernetmac"
COL_CDK_MAC = "cdkmac"
COL_SEQ = "seq"
COL_TYPE = "devicetype"

_MAC_RE = re.compile(r"[0-9A-F]{12}")


@dataclass(frozen=True)
class AnchorRecord:
    seq: int
    eth_mac: str          # Ethernet MAC: 12 hex digits, upper case, no separators
    cdk_mac: str          # CDK MAC: 12 hex digits, upper case, no separators (printed as "ID")
    device_type: str = ""
    row: int = 0          # 1-based sheet row (0 = unknown)

    @property
    def seq_text(self) -> str:
        return format_seq(self.seq)

    @property
    def payload(self) -> str:
        """QR content: <Ethernet MAC>_<CDK MAC>, e.g. 907069F452B4_907069F452B4."""
        return f"{self.eth_mac}_{self.cdk_mac}"


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", "", str(cell)).lower()


def _clean_hex(cell: str) -> str:
    return re.sub(r"[\s:.-]", "", str(cell)).upper()


def parse_anchor_rows(rows: list[list[str]]) -> tuple[list[AnchorRecord], list[str]]:
    """Sheet values -> (valid records, problems). Problems cover bad cells and duplicates."""
    header_index, cols = -1, {}
    for i, row in enumerate(rows):
        names = [_norm_header(c) for c in row]
        if COL_ETH in names and COL_SEQ in names:
            header_index, cols = i, {name: names.index(name) for name in (COL_ETH, COL_CDK_MAC, COL_SEQ, COL_TYPE) if name in names}
            break
    if header_index < 0:
        return [], ["Header row with 'Ethernet MAC' and 'SEQ' columns not found"]
    if COL_CDK_MAC not in cols:
        return [], ["'CDK MAC' column not found in the header row"]

    def cell(row: list[str], name: str) -> str:
        i = cols.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    records: list[AnchorRecord] = []
    problems: list[str] = []
    for i in range(header_index + 1, len(rows)):
        row, number = rows[i], i + 1
        if not any(str(c).strip() for c in row):
            continue
        seq_text, eth, cdk = cell(row, COL_SEQ), _clean_hex(cell(row, COL_ETH)), _clean_hex(cell(row, COL_CDK_MAC))
        bad = []
        if not seq_text.isdigit() or not 1 <= int(seq_text) <= MAX_SEQ:
            bad.append(f"SEQ '{seq_text}'")
        if not _MAC_RE.fullmatch(eth):
            bad.append(f"Ethernet MAC '{cell(row, COL_ETH)}'")
        if not _MAC_RE.fullmatch(cdk):
            bad.append(f"CDK MAC '{cell(row, COL_CDK_MAC)}'")
        if bad:
            problems.append(f"Row {number}: invalid " + ", ".join(bad))
            continue
        records.append(AnchorRecord(int(seq_text), eth, cdk, cell(row, COL_TYPE), number))

    for label, key in (("SEQ", lambda r: r.seq_text), ("Ethernet MAC", lambda r: r.eth_mac), ("CDK MAC", lambda r: r.cdk_mac)):
        seen: dict[str, list[AnchorRecord]] = {}
        for r in records:
            seen.setdefault(key(r), []).append(r)
        for value, group in seen.items():
            if len(group) > 1:
                problems.append(f"{label} {value} is used {len(group)} times: rows " + ", ".join(str(r.row) for r in group))
    return records, problems


class AnchorSheet(SheetsClient):
    """Read-only access to the anchor sheet. Never writes a header or any cell."""

    def values(self) -> list[list[str]]:
        return self._call("GET", f"{API}/{self.sheet_id}/values/{self._range('A:Z')}").get("values", [])

    def load(self) -> tuple[list[AnchorRecord], list[str]]:
        return parse_anchor_rows(self.values())
