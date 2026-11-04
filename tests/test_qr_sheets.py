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
    assert (first.verified, first.manually_verified, first.nfc_written, first.row) == (True, False, True, 2)
    assert first.status == "Verified · NFC ✔"
    assert (second.verified, second.nfc_written, second.row, second.status) == (False, False, 3, "")


def test_device_type_list():
    from sytrak_gen.config import QrSettings

    assert QrSettings().device_type_list == ["UWB-K4W"]
    assert QrSettings(device_types="UWB-K4W, UWB-K5 ,,").device_type_list == ["UWB-K4W", "UWB-K5"]
