import re

from sytrak_gen.core import anchor_pdf
from sytrak_gen.core.anchor import AnchorRecord, parse_anchor_rows

HEADER = ["Ethernet MAC", "CDK MAC", "CDK ID", "SEQ", "Device Type"]


def test_parse_rows_uses_cdk_mac_and_ignores_cdk_id():
    rows = [HEADER,
            ["907069F452B4", "907069F452B4", "C0B7FCC2B563613B", "1", "UWB-ANC01"],
            ["90:70:69:f4:53:9c", "90-70-69-F4-53-9C", "343a9c2280dd3097", "2", "UWB-ANC01"],
            [],
            ["7C0C5F553540", "7C0C5F553540", "", "17", "UWB-ANC01"]]   # empty CDK ID is fine
    records, problems = parse_anchor_rows(rows)
    assert problems == []
    assert [r.seq_text for r in records] == ["0001", "0002", "0017"]
    assert records[1].eth_mac == "907069F4539C" and records[1].cdk_mac == "907069F4539C"
    assert records[2].row == 5
    assert records[0].payload == "907069F452B4_907069F452B4"   # <Ethernet MAC>_<CDK MAC>


def test_header_found_by_name_in_any_order():
    rows = [["Anchors"], ["SEQ", "Device Type", "CDK ID", "CDK MAC", "Ethernet MAC"],
            ["3", "UWB-ANC01", "4A26308BB2BAEFDC", "907069F45AD5", "907069F45AD4"]]
    records, problems = parse_anchor_rows(rows)
    assert problems == [] and records == [AnchorRecord(3, "907069F45AD4", "907069F45AD5", "UWB-ANC01", 3)]


def test_invalid_and_duplicate_rows_are_reported():
    rows = [HEADER,
            ["907069F452B4", "907069F452B4", "", "1", "UWB-ANC01"],
            ["907069F452B4", "907069F4539C", "", "1", "UWB-ANC01"],
            ["NOTAMAC", "C0B7FCC2B563613B", "", "x", ""]]          # a 16-digit value is not a CDK MAC
    records, problems = parse_anchor_rows(rows)
    assert len(records) == 2
    assert any(p.startswith("Row 4: invalid") and "CDK MAC" in p for p in problems)
    assert any(p.startswith("SEQ 0001 is used 2 times") for p in problems)
    assert any(p.startswith("Ethernet MAC 907069F452B4 is used 2 times") for p in problems)


def test_missing_header_or_cdk_mac_column():
    assert parse_anchor_rows([["a", "b"]])[1] == ["Header row with 'Ethernet MAC' and 'SEQ' columns not found"]
    old = [["Ethernet MAC", "CDK MAC/ID", "SEQ"], ["907069F452B4", "C0B7FCC2B563613B", "1"]]
    assert parse_anchor_rows(old)[1] == ["'CDK MAC' column not found in the header row"]


def test_layout_fits_a4():
    assert (anchor_pdf.PAGE_W, anchor_pdf.PAGE_H) == (842, 595)   # landscape
    assert (anchor_pdf.COLS, anchor_pdf.ROWS, anchor_pdf.PER_PAGE) == (3, 1, 3)
    assert anchor_pdf.MARGIN_LEFT >= 0 and anchor_pdf.MARGIN_TOP >= 0
    assert anchor_pdf.CONTENT_BOTTOM <= anchor_pdf.BOX_H


def test_vertical_positions_follow_the_spec():
    a = anchor_pdf
    assert round(a.CIRCLE_TOP, 2) == round(7 + 103 + 10.57, 2)
    assert round(a.QR_TOP, 2) == round(a.CIRCLE_TOP + 28.34 + 97.08, 2)
    assert round(a.ID_TOP + a.ID_LINE_HEIGHT, 2) == round(a.CONTENT_BOTTOM, 2)
    # logo is centred inside its frame
    assert abs((a.LOGO_TOP - a.LOGO_BOX_TOP) - (a.LOGO_BOX_TOP + a.LOGO_BOX_H - a.LOGO_TOP - a.LOGO_H)) < 0.05


def test_text_fits_the_box():
    from reportlab.pdfbase import pdfmetrics
    r = AnchorRecord(123, "907069F452B4", "907069F452B4")
    font = anchor_pdf.font_name()
    for text, _top, size, _lh, left in anchor_pdf._text_rows(r):
        assert (left or 0) + pdfmetrics.stringWidth(text, font, size) < anchor_pdf.BOX_W
    assert (anchor_pdf.MAC_LEFT, anchor_pdf.ID_LEFT) == (8, 8)   # MAC and ID left-aligned 8 from the box


def test_text_lines():
    r = AnchorRecord(123, "907069F452B4", "907069F452B5")
    assert anchor_pdf.text_lines(r) == {"seq": "S: 0123", "mac": "MAC: 907069F452B4", "id": "ID: 907069F452B5"}
    assert anchor_pdf.font_name() == "Inter-Bold"


def test_pdf_pages(tmp_path):
    records = [AnchorRecord(i, f"907069F4{i:04X}", f"907069F5{i:04X}") for i in range(1, 6)]
    path = tmp_path / "anchors.pdf"
    assert anchor_pdf.build_pdf(records, path) == 2   # 3 per page
    data = path.read_bytes()
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 2


def test_preview_size():
    img = anchor_pdf.render_preview(AnchorRecord(1, "907069F452B4", "907069F452B4"), scale=1)
    a = anchor_pdf
    assert img.size == (round(a.BOX_W + 2 * a.BORDER), round(a.BOX_H + 2 * a.BORDER))
