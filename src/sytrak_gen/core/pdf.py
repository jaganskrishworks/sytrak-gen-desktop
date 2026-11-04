"""A4 label sheet PDF - fixed layout, vector output (units: PDF points, 1 pt = 1/72 in).

Each label box (77.95 x 77.95, 0.86 solid black border outside):

    [ logo 20.07 x 7.04 ]
      spacer 1.76
    [ QR 53.88 x 53.88 ]
      spacer 2.69
    M : C93FA3ED944C  .  S : 0001      (IBM Plex Sans Medium 4.5, line-height 6.85)

The content block is centred horizontally and vertically in the box.
Boxes are GAP_X apart horizontally and GAP_Y apart vertically (border edge to
border edge), filled left-to-right then top-to-bottom from the top-left margin.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

import qrcode
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from ..paths import asset_path
from .qr import compact_mac, format_seq
from .sheets import SheetRecord

log = logging.getLogger(__name__)

# --- page -------------------------------------------------------------------
PAGE_W, PAGE_H = 595, 842          # A4
MARGIN_LEFT, MARGIN_TOP = 36, 34   # page edge to the outer edge of the first box's border
GAP_X, GAP_Y = 11, 9               # space between neighbouring boxes (border to border)

# --- one label box ----------------------------------------------------------
BOX = 77.95
BORDER = 0.86                      # solid #000000, outside the box
LOGO_W, LOGO_H = 20.07, 7.04
SPACER_LOGO_QR = 1.76
QR_SIZE = 53.88
SPACER_QR_TEXT = 2.69
FONT_FILE = "IBMPlexSans-Medium.ttf"
FONT_SIZE = 4.5
LINE_HEIGHT = 6.85

OUTER = BOX + 2 * BORDER           # box including its border (79.67)
PITCH_X = OUTER + GAP_X            # 90.67
PITCH_Y = OUTER + GAP_Y            # 88.67
# As many boxes as fit on the page from the top-left margin.
COLS = int((PAGE_W - MARGIN_LEFT + GAP_X) // PITCH_X)      # 6  (right space 25.98)
ROWS = int((PAGE_H - MARGIN_TOP + GAP_Y) // PITCH_Y)       # 9  (bottom space 18.97)
PER_PAGE = COLS * ROWS                                     # 54

CONTENT_H = LOGO_H + SPACER_LOGO_QR + QR_SIZE + SPACER_QR_TEXT + LINE_HEIGHT


def label_text(mac: str, seq: int) -> str:
    """e.g.  M : C93FA3ED944C  .  S : 0001"""
    return f"M : {compact_mac(mac)}  .  S : {format_seq(seq)}"


@lru_cache(maxsize=1)
def _font_name() -> str:
    path = asset_path("fonts", FONT_FILE)
    try:
        pdfmetrics.registerFont(TTFont("IBMPlexSans-Medium", str(path)))
        return "IBMPlexSans-Medium"
    except Exception:
        log.warning("Font %s not found - using Helvetica", path)
        return "Helvetica"


@lru_cache(maxsize=1)
def _logo() -> ImageReader | None:
    # High-resolution logo drawn at LOGO_W x LOGO_H (same aspect ratio) - stays sharp in print.
    for name in ("company-logo.png", "qr_company_logo.png"):
        path = asset_path("icons", name)
        if path.is_file():
            return ImageReader(str(path))
    return None


def _qr_matrix(payload: str) -> list[list[bool]]:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.get_matrix()


def _draw_qr(c: canvas.Canvas, payload: str, x: float, top: float) -> None:
    """Vector QR, QR_SIZE square, top-left corner at (x, top) in top-down coordinates."""
    matrix = _qr_matrix(payload)
    module = QR_SIZE / len(matrix)
    c.setFillColorRGB(0, 0, 0)
    for r, row in enumerate(matrix):
        y = PAGE_H - (top + (r + 1) * module)
        col = 0
        while col < len(row):  # one rectangle per horizontal run - no hairline gaps
            if not row[col]:
                col += 1
                continue
            start = col
            while col < len(row) and row[col]:
                col += 1
            c.rect(x + start * module, y, (col - start) * module, module, stroke=0, fill=1)


def _draw_label(c: canvas.Canvas, record: SheetRecord, box_x: float, box_top: float) -> None:
    # Border: stroke centred on a line BORDER/2 outside the box, so it is fully outside.
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(BORDER)
    half = BORDER / 2
    c.rect(box_x - half, PAGE_H - (box_top + BOX + half), BOX + BORDER, BOX + BORDER, stroke=1, fill=0)

    centre_x = box_x + BOX / 2
    y = box_top + (BOX - CONTENT_H) / 2  # top of the content block (top-down)

    logo = _logo()
    if logo is not None:
        c.drawImage(logo, centre_x - LOGO_W / 2, PAGE_H - (y + LOGO_H), LOGO_W, LOGO_H, mask="auto")
    y += LOGO_H + SPACER_LOGO_QR

    _draw_qr(c, record.payload, centre_x - QR_SIZE / 2, y)
    y += QR_SIZE + SPACER_QR_TEXT

    font = _font_name()
    ascent, descent = pdfmetrics.getAscentDescent(font, FONT_SIZE)
    baseline = y + (LINE_HEIGHT - (ascent - descent)) / 2 + ascent  # text centred in its line box
    c.setFillColorRGB(0, 0, 0)
    c.setFont(font, FONT_SIZE)
    c.drawCentredString(centre_x, PAGE_H - baseline, label_text(record.mac, record.seq))


def build_pdf(
    records: Iterable[SheetRecord],
    path: Path,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Lay labels out left-to-right, top-to-bottom, PER_PAGE per A4 page. Returns pages written."""
    records = list(records)
    if not records:
        raise ValueError("No QR codes selected")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle("SyTrak QR labels")
    pages = 0
    for i, record in enumerate(records):
        slot = i % PER_PAGE
        if slot == 0:
            if i:
                c.showPage()
            pages += 1
        row, col = divmod(slot, COLS)
        box_x = MARGIN_LEFT + col * PITCH_X + BORDER
        box_top = MARGIN_TOP + row * PITCH_Y + BORDER
        _draw_label(c, record, box_x, box_top)
        if progress:
            progress(i + 1, len(records))
    c.save()
    return pages
