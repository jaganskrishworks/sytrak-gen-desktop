import re

from sytrak_gen.core import pdf
from sytrak_gen.core.sheets import SheetRecord


def _records(n):
    return [SheetRecord(i, f"E9:EB:A0:ED:{i // 256:02X}:{i % 256:02X}", "2026-11-04 15:00:00", "UWB-K4W") for i in range(1, n + 1)]


def test_fixed_layout_fits_a4_with_gaps():
    assert (pdf.PAGE_W, pdf.PAGE_H) == (595, 842)
    assert (pdf.GAP_X, pdf.GAP_Y) == (11, 9)
    assert (pdf.COLS, pdf.ROWS, pdf.PER_PAGE) == (6, 9, 54)
    right = pdf.MARGIN_LEFT + pdf.COLS * pdf.OUTER + (pdf.COLS - 1) * pdf.GAP_X
    bottom = pdf.MARGIN_TOP + pdf.ROWS * pdf.OUTER + (pdf.ROWS - 1) * pdf.GAP_Y
    assert right <= pdf.PAGE_W and bottom <= pdf.PAGE_H
    assert round(pdf.PAGE_W - right, 2) == 25.98 and round(pdf.PAGE_H - bottom, 2) == 18.97
    # logo + spacer + QR + spacer + text line fits inside the box
    assert pdf.CONTENT_H <= pdf.BOX


def test_label_text_format():
    assert pdf.label_text("C9:3F:A3:ED:94:4C", 1) == "M : C93FA3ED944C  .  S : 0001"


def test_pdf_pages(tmp_path):
    path = tmp_path / "labels.pdf"
    assert pdf.build_pdf(_records(60), path) == 2  # 54 per page
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 2
    assert b"IBMPlexSans" in data  # IBM Plex Sans Medium embedded
