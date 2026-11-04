import re

from sytrak_gen.core import pdf
from sytrak_gen.core.sheets import SheetRecord


def _records(n):
    return [SheetRecord(i, f"E9:EB:A0:ED:{i // 256:02X}:{i % 256:02X}", "2026-11-04 15:00:00", "UWB-K4W") for i in range(1, n + 1)]


def test_fixed_layout_fits_a4_edge_to_edge():
    assert (pdf.PAGE_W, pdf.PAGE_H) == (595, 842)
    assert (pdf.MARGIN_LEFT, pdf.MARGIN_TOP) == (25, 31)
    assert (pdf.GAP_X, pdf.GAP_Y) == (0, 0)
    assert pdf.PITCH_X == pdf.PITCH_Y == pdf.BOX + pdf.BORDER  # shared border line
    assert (pdf.COLS, pdf.ROWS, pdf.PER_PAGE) == (7, 10, 70)
    assert pdf.RIGHT_SPACE >= 0 and pdf.BOTTOM_SPACE >= 0
    # logo + spacer + QR + spacer + text line fits inside the box
    assert pdf.CONTENT_H <= pdf.BOX


def test_label_text_format():
    assert pdf.label_text("C9:3F:A3:ED:94:4C", 1) == "M : C93FA3ED944C  .  S : 0001"


def test_pdf_pages(tmp_path):
    path = tmp_path / "labels.pdf"
    assert pdf.build_pdf(_records(80), path) == 2  # 70 per page
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 2
    assert b"IBMPlexSans" in data  # IBM Plex Sans Medium embedded
