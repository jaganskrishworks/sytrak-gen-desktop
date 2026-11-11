import pytest

from sytrak_gen.core.qr import build_payload, caption_text, normalize_mac, parse_payload, render_label, thumbnail
from sytrak_gen.core.sheets import SheetError, SheetRecord, next_seq, parse_rows, parse_sheet_url


def test_payload_is_mac_without_colons():
    payload = build_payload("E9:EB:A0:ED:7C:7C")
    assert payload == "E9EBA0ED7C7C"
    assert parse_payload(payload) == ("E9:EB:A0:ED:7C:7C", None)
    assert caption_text("E9:EB:A0:ED:7C:7C", 1) == "M : E9EBA0ED7C7C  .  S : 0001"


def test_parse_payload_older_formats():
    assert parse_payload('{"mac":"E9:EB:A0:ED:7C:7C","seq":"0007"}') == ("E9:EB:A0:ED:7C:7C", 7)
    assert parse_payload("e9eba0ed7c7c;0012") == ("E9:EB:A0:ED:7C:7C", 12)
    assert parse_payload("hello world") is None


def test_normalize_mac():
    assert normalize_mac("e9eb.a0ed.7c7c") == "E9:EB:A0:ED:7C:7C"
    assert normalize_mac("123") is None


def test_render_images():
    img = render_label(build_payload("E9:EB:A0:ED:7C:7C"), "E9:EB:A0:ED:7C:7C", 1)
    assert img.height > img.width > 200
    assert thumbnail("x", 64).size == (64, 64)


def test_parse_sheet_url():
    url = "https://docs.google.com/spreadsheets/d/1yH55rsyUhYIXG6Krr0JinLy5dPnLPdUcjEJ9hi1j-qo/edit?gid=0#gid=0"
    assert parse_sheet_url(url) == ("1yH55rsyUhYIXG6Krr0JinLy5dPnLPdUcjEJ9hi1j-qo", 0)
    assert parse_sheet_url(url.split("/edit")[0])[1] is None
    with pytest.raises(SheetError):
        parse_sheet_url("https://example.com")


def test_parse_rows_skips_header_and_junk():
    rows = [["SEQ", "MAC", "Date Time", "Device Type"], ["0001", "E9:EB:A0:ED:7C:7C", "2026-11-04 15:00:00", "UWB-K4W"],
            [], ["x", "y"], ["2", "aabbccddeeff"]]
    records = parse_rows(rows)
    assert records == [SheetRecord(1, "E9:EB:A0:ED:7C:7C", "2026-11-04 15:00:00", "UWB-K4W"),
                       SheetRecord(2, "AA:BB:CC:DD:EE:FF", "", "")]
    assert records[0].seq_text == "0001"
    assert next_seq(records) == 3
    assert next_seq([], start=100) == 100


def test_parse_rows_reads_status_columns_and_row_numbers():
    rows = [["SEQ", "MAC", "Date Time", "Device Type", "Verified", "Manually Verified", "NFC Written", "Updated At"],
            ["0001", "E9:EB:A0:ED:7C:7C", "t", "UWB-K4W", "Yes", "", "Yes", "t2"],
            ["0002", "AA:BB:CC:DD:EE:FF", "t", "UWB-K4W"]]  # older row without status cells
    first, second = parse_rows(rows)
    assert (first.v1.verified, first.v1.manually_verified, first.v1.nfc, first.row) == (True, False, True, 2)
    assert first.status == "V1: Verified, NFC written"
    assert (second.v1.verified, second.v1.nfc, second.row, second.status) == (False, False, 3, "")


def test_parse_rows_reads_verification_1_and_2_blocks():
    from sytrak_gen.core.sheets import GROUP_ROW, HEADER

    assert len(HEADER) == 20 and HEADER[11] == HEADER[19] == "Updated At"
    rows = [GROUP_ROW, HEADER,
            ["0001", "E9EBA0ED7C7C", "t", "UWB-K4W", "Yes", "", "Yes", "04B34DEA0B1390", "Valid", "Validated", "No", "t2"],
            ["0002", "AABBCCDDEEFF", "t", "UWB-K4W"] + [""] * 8 + ["Yes", "Yes", "Yes", "04AA", "Invalid", "Failed", "No", "t3"]]
    one, two = parse_rows(rows)
    assert (one.row, one.v1.verified, one.v1.nfc, one.v2.verified) == (3, True, True, False)
    assert (one.v1.uid, one.v1.signature, one.v1.password, one.v1.locked, one.v1.updated_at) == \
        ("04B34DEA0B1390", "Valid", "Validated", "No", "t2")
    assert (two.row, two.v1.verified, two.v2.verified, two.v2.manually_verified, two.v2.nfc, two.v2.updated_at) == \
        (4, False, True, True, True, "t3")
    assert two.v2.signature == "Invalid"
    assert two.status == "V2: Verified, Manual, NFC read"


def test_upgrade_12_column_layout():
    from sytrak_gen.core.sheets import GROUP_COLS, GROUP_ROW, HEADER, LEGACY_HEADER, upgrade_legacy_rows

    old = [["", "", "", "", "Verification 1", "", "", "", "Verification 2"], LEGACY_HEADER,
           ["0001", "E9EBA0ED7C7C", "t", "UWB-K4W", "Yes", "", "Yes", "t2", "", "", "Yes", "t3"],
           ["0002", "AABBCCDDEEFF", "t", "UWB-K4W"]]
    new = upgrade_legacy_rows(old)
    assert new[:2] == [GROUP_ROW, HEADER]
    one, two = parse_rows(new)
    assert (one.v1.verified, one.v1.nfc, one.v1.updated_at, one.v1.uid) == (True, True, "t2", "")
    assert (one.v2.nfc, one.v2.updated_at) == (True, "t3")
    assert new[3] == ["0002", "AABBCCDDEEFF", "t", "UWB-K4W"]
    assert GROUP_COLS[1]["uid"] == "H" and GROUP_COLS[2]["locked"] == "S" and GROUP_COLS[2]["updated_at"] == "T"


def test_migrate_old_single_header_layout():
    from sytrak_gen.core.sheets import GROUP_ROW, HEADER, audit_rows, migrate_rows

    old = [["SEQ", "MAC", "Date Time", "Device Type", "Verified", "Manually Verified", "NFC Written", "Updated At",
            "Verification 1", "Verification 2"],
           ["0001", "E9EBA0ED7C7C", "t", "UWB-K4W", "Yes", "", "", "t2", "Yes", ""],
           ["0002", "AABBCCDDEEFF", "t", "UWB-K4W", "", "", "", "t3", "", "Yes"]]
    new = migrate_rows(old)
    assert new[:2] == [GROUP_ROW, HEADER]
    one, two = parse_rows(new)
    assert (one.row, one.v1.verified, one.v1.nfc) == (3, True, True)       # old Verification 1 -> NFC Written
    assert (two.row, two.v2.nfc, two.v2.updated_at) == (4, True, "t3")     # old Verification 2 -> NFC Read
    assert audit_rows(new)[1] == []                                         # header rows are not "invalid entries"


def test_audit_rows_finds_every_problem():
    from sytrak_gen.core.sheets import audit_rows

    header = ["SEQ", "MAC", "Date Time", "Device Type"]
    clean = [header, ["0001", "AA:AA:AA:AA:AA:01", "t"], ["0002", "AA:AA:AA:AA:AA:02", "t"]]
    assert audit_rows(clean)[1] == []

    bad = [header,
           ["0001", "AA:AA:AA:AA:AA:01", "t"],
           ["0002", "AA:AA:AA:AA:AA:02", "t"],
           ["0002", "AA:AA:AA:AA:AA:03", "t"],      # duplicate SEQ
           ["0004", "aaaaaaaaaa01", "t"],            # duplicate MAC (different format)
           ["x12", "AA:AA:AA:AA:AA:05", "t"],        # invalid SEQ
           [],                                        # blank row - ignored
           ["0006", "not-a-mac", "t"]]               # invalid MAC
    records, problems = audit_rows(bad)
    assert len(records) == 4
    text = "\n".join(problems)
    assert "SEQ 0002 is used 2 times: rows 3, 4" in text
    assert "MAC AAAAAAAAAA01 is used 2 times: rows 2 (SEQ 0001), 5 (SEQ 0004)" in text
    assert "Row 6: invalid entry" in text and "Row 8: invalid entry" in text
    assert len(problems) == 4


def test_device_type_list():
    from sytrak_gen.config import QrSettings

    assert QrSettings().device_type_list == ["UWB-K4W"]
    assert QrSettings(device_types="UWB-K4W, UWB-K5 ,,").device_type_list == ["UWB-K4W", "UWB-K5"]
