"""SEQ stays unique when several stations save into the same tab at the same moment."""
import re
from pathlib import Path
from urllib.parse import unquote

from sytrak_gen.core.sheets import HEADER, SheetsClient, appended_row, parse_rows

URL = "https://docs.google.com/spreadsheets/d/1yH55rsyUhYIXG6Krr0JinLy5dPnLPdUcjEJ9hi1j-qo/edit#gid=0"


class FakeSheet:
    def __init__(self):
        self.rows = [list(HEADER)]


class Station(SheetsClient):
    """SheetsClient backed by an in-memory tab; ``on_read[n]`` runs before the n-th read."""

    def __init__(self, sheet: FakeSheet, on_read=None):
        super().__init__(Path("key.json"), URL, tab="T")
        self.sheet = sheet
        self.on_read = on_read or {}
        self.reads = 0
        self._tab, self._header_ok = "T", True

    def records(self):
        self.reads += 1
        hook = self.on_read.get(self.reads)
        if hook:
            result = hook()
            if result is not None:  # a stale view of the sheet
                return result
        return parse_rows(self.sheet.rows)

    def _call(self, method, url, params=None, json=None, **kw):
        if url.endswith(":append"):
            self.sheet.rows.append(list(json["values"][0]))
            n = len(self.sheet.rows)
            return {"updates": {"updatedRange": f"'T'!A{n}:D{n}"}}
        if method == "PUT":
            row = int(re.search(r"!A(\d+)", unquote(url)).group(1))
            self.sheet.rows[row - 1][0] = json["values"][0][0]
            return {}
        raise AssertionError(f"unexpected call {method} {url}")


def other_station_appends(sheet, seq_text, mac):
    sheet.rows.append([seq_text, mac, "t", "UWB-K4W"])


def test_resolve_seq_gives_each_duplicate_its_own_number():
    from sytrak_gen.core.sheets import SheetRecord, resolve_seq

    rows = [SheetRecord(1, f"AA:AA:AA:AA:AA:0{i}", "t", row=i + 2) for i in range(5)]  # five rows all SEQ 1
    assert [resolve_seq(rows, r.row) for r in rows] == [1, 2, 3, 4, 5]
    mixed = [SheetRecord(4, "AA:AA:AA:AA:AA:01", "t", row=2), SheetRecord(7, "AA:AA:AA:AA:AA:02", "t", row=3),
             SheetRecord(4, "AA:AA:AA:AA:AA:03", "t", row=4)]
    assert [resolve_seq(mixed, r.row) for r in mixed] == [4, 7, 8]


def test_appended_row_parsing():
    assert appended_row({"updates": {"updatedRange": "'My Tab'!A12:D12"}}) == 12
    assert appended_row({}) == 0


def test_single_station_gets_next_seq():
    sheet = FakeSheet()
    rec = Station(sheet).save("AA:AA:AA:AA:AA:01", "UWB-K4W")
    assert rec.seq == 1 and sheet.rows[1][0] == "0001"
    assert Station(sheet).save("AA:AA:AA:AA:AA:02", "UWB-K4W").seq == 2


def test_earlier_row_keeps_the_seq():
    # Both stations picked 0001; ours was appended first, the other right after.
    sheet = FakeSheet()
    me = Station(sheet, on_read={2: lambda: other_station_appends(sheet, "0001", "BB:BB:BB:BB:BB:BB")})
    rec = me.save("AA:AA:AA:AA:AA:AA", "UWB-K4W")
    assert rec.seq == 1 and rec.row == 2


def test_later_row_renumbers_itself():
    # Another station appended 0001 between our read and our append: we must move to 0002.
    sheet = FakeSheet()
    other_station_appends(sheet, "0001", "BB:BB:BB:BB:BB:BB")
    me = Station(sheet, on_read={1: lambda: []})  # our first read was stale (sheet looked empty)
    rec = me.save("AA:AA:AA:AA:AA:AA", "UWB-K4W")
    assert (rec.seq, rec.row) == (2, 3)
    assert [r[0] for r in sheet.rows[1:]] == ["0001", "0002"]


def test_many_stations_same_moment_all_unique():
    # Five stations all read an empty sheet and all append SEQ 0001.
    sheet = FakeSheet()
    stations = [Station(sheet, on_read={1: lambda: []}) for _ in range(5)]
    macs = [f"AA:AA:AA:AA:AA:0{i}" for i in range(5)]
    for st, mac in zip(stations, macs):
        st._call("POST", "x:append", json={"values": [["0001", mac, "t", "UWB-K4W"]]})
    # Then each verifies (in any order) - reuse the save() verify loop via a fresh save of an existing row.
    results = []
    for st, mac in reversed(list(zip(stations, macs))):
        st.on_read = {}
        results.append(_verify_existing(st, mac))
    seqs = [r[0] for r in sheet.rows[1:]]
    assert sorted(seqs) == ["0001", "0002", "0003", "0004", "0005"]


def _verify_existing(st, mac):
    """Run only the verify/renumber loop of save() for a row that is already appended."""
    original = st._call

    def call(method, url, params=None, json=None, **kw):
        if url.endswith(":append"):  # row already there - report its position instead of appending
            row = next(i + 1 for i, r in enumerate(st.sheet.rows) if r[1] == mac)
            return {"updates": {"updatedRange": f"'T'!A{row}:D{row}"}}
        return original(method, url, params=params, json=json, **kw)

    st._call = call
    st.on_read = {1: lambda: []}  # skip the "MAC already registered" pre-check
    return st.save(mac, "UWB-K4W")
