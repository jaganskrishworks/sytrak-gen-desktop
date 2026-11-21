"""Anchor sticker sheet PDF (separate product from the tag labels in pdf.py).

Units: PDF points (1 pt = 1/72 in); the design's "px" values are used as points 1:1.
A4 landscape 842 x 595, 3 x 1 stickers per page, grid centred on the page.

One sticker, everything centred horizontally. Vertical positions are measured from the
top (inner edge) of the sticker box, each element placed below the previous one:

    sticker box 231.02 x 413.85      border 1 outside, outer corner radius 3
      7      ┌ logo box 166.20 x 103 ┐   border 1.4 inside
      33.92  │   [ logo 140 x 49.15 ] │   (33.92 from the sticker top)
             └────────────────────────┘
      10.57      ( circle 28.34 )        border 1 outside
      97.08
               [ QR 52.04 ]              QR content = <Ethernet MAC>_<CDK MAC>
      1.39       S: 0001                 Inter Bold 10.93 / line-height 13
      9.94       ───────── 60.89         line 1.24 thick, #989898
      9.94   MAC: 907069F452B4           Inter Bold 19.63 / line-height 24   (Ethernet MAC)
      8.86   ID: 907069F452B4            Inter Bold 19.63 / line-height 24   (CDK MAC)
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from ..paths import asset_path
from . import pdf as tag_pdf          # shared helpers only (logo, QR matrix) - its layout is not used
from .anchor import AnchorRecord
from .qr import qr_image

log = logging.getLogger(__name__)

# --- page -------------------------------------------------------------------
PAGE_W, PAGE_H = 842, 595            # A4 landscape (portrait 595 x 842 rotated 90°)

# --- 1. sticker box ---------------------------------------------------------
BOX_W, BOX_H = 231.02, 413.85
BORDER = 1                           # solid black, OUTSIDE the box
RADIUS = 3                           # corner radius of the border's outer edge

# --- 2. logo box (frame around the logo) ------------------------------------
LOGO_BOX_W, LOGO_BOX_H = 166.20, 103
LOGO_BOX_BORDER = 1.4                # solid black, INSIDE the logo box
LOGO_BOX_RADIUS = 0                  # corner radius of its outer edge (0 = square)
LOGO_BOX_TOP = 7                     # sticker top -> logo box top

# --- 3. company logo --------------------------------------------------------
LOGO_W, LOGO_H = 140.00, 49.15
LOGO_TOP = 33.92                     # sticker top -> logo top (centres it in the logo box)

# --- 4. circle --------------------------------------------------------------
CIRCLE_D = 28.34                     # diameter
CIRCLE_BORDER = 1                    # solid black, OUTSIDE the circle
SPACER_LOGO_BOX_CIRCLE = 10.57       # logo box bottom -> circle top

# --- 5. QR ------------------------------------------------------------------
QR_SIZE = 52.04
SPACER_CIRCLE_QR = 97.08             # circle bottom -> QR top

# --- fonts --------------------------------------------------------------------
FONT_FILE = "Inter-Bold.ttf"         # Inter, weight 700, letter-spacing 0
FONT_NAME = "Inter-Bold"
FALLBACK_FONT = "Helvetica-Bold"     # used only if the font file is missing

# --- 6. sequence ------------------------------------------------------------
SPACER_QR_SEQ = 1.39                 # QR bottom -> sequence line box top
SEQ_FONT_SIZE, SEQ_LINE_HEIGHT = 10.93, 13
SEQ_FORMAT = "S: {seq}"              # {seq} = 4 digits, e.g. S: 0001

# --- 7. divider line --------------------------------------------------------
SPACER_SEQ_LINE = 9.94               # sequence bottom -> line
LINE_W = 60.89
LINE_THICKNESS = 1.24
LINE_COLOR = "#989898"
LINE_RADIUS = 10                     # > 0 = rounded ends (like CSS border-radius; capped at half the thickness)

# --- 8. MAC -----------------------------------------------------------------
SPACER_LINE_MAC = 9.94               # line -> MAC line box top
MAC_FONT_SIZE, MAC_LINE_HEIGHT = 19.63, 24
MAC_FORMAT = "MAC: {mac}"
MAC_LEFT = 8                         # left-aligned, 8 from the sticker box's left edge (None = centred)

# --- 9. ID ------------------------------------------------------------------
SPACER_MAC_ID = 8.86                 # MAC bottom -> ID line box top
ID_FONT_SIZE, ID_LINE_HEIGHT = 19.63, 24
ID_FORMAT = "ID: {id}"
ID_LEFT = 8                          # left-aligned, 8 from the sticker box's left edge (None = centred)

# --- derived: vertical positions inside the sticker (from its top) ----------
CIRCLE_TOP = LOGO_BOX_TOP + LOGO_BOX_H + SPACER_LOGO_BOX_CIRCLE
QR_TOP = CIRCLE_TOP + CIRCLE_D + SPACER_CIRCLE_QR
SEQ_TOP = QR_TOP + QR_SIZE + SPACER_QR_SEQ
LINE_Y = SEQ_TOP + SEQ_LINE_HEIGHT + SPACER_SEQ_LINE
MAC_TOP = LINE_Y + SPACER_LINE_MAC
ID_TOP = MAC_TOP + MAC_LINE_HEIGHT + SPACER_MAC_ID
CONTENT_BOTTOM = ID_TOP + ID_LINE_HEIGHT

# --- derived: page grid -----------------------------------------------------
OUTER_W, OUTER_H = BOX_W + 2 * BORDER, BOX_H + 2 * BORDER
PITCH_X, PITCH_Y = BOX_W + BORDER, BOX_H + BORDER   # neighbours touch and share one border line
COLS = int((PAGE_W - (OUTER_W - PITCH_X)) // PITCH_X)       # 3
ROWS = int((PAGE_H - (OUTER_H - PITCH_Y)) // PITCH_Y)       # 1
PER_PAGE = COLS * ROWS
MARGIN_LEFT = (PAGE_W - ((COLS - 1) * PITCH_X + OUTER_W)) / 2
MARGIN_TOP = (PAGE_H - ((ROWS - 1) * PITCH_Y + OUTER_H)) / 2


def text_lines(record: AnchorRecord) -> dict[str, str]:
    return {
        "seq": SEQ_FORMAT.format(seq=record.seq_text),
        "mac": MAC_FORMAT.format(mac=record.eth_mac),
        "id": ID_FORMAT.format(id=record.cdk_mac),
    }


def _text_rows(record: AnchorRecord) -> list[tuple[str, float, float, float, float | None]]:
    """(text, top, font size, line height, left) for each text line; left None = centred."""
    t = text_lines(record)
    return [(t["seq"], SEQ_TOP, SEQ_FONT_SIZE, SEQ_LINE_HEIGHT, None),
            (t["mac"], MAC_TOP, MAC_FONT_SIZE, MAC_LINE_HEIGHT, MAC_LEFT),
            (t["id"], ID_TOP, ID_FONT_SIZE, ID_LINE_HEIGHT, ID_LEFT)]


def _hex_rgb(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


@lru_cache(maxsize=1)
def font_name() -> str:
    path = asset_path("fonts", FONT_FILE)
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
        return FONT_NAME
    except Exception:
        log.warning("Font %s not found - using %s", path, FALLBACK_FONT)
        return FALLBACK_FONT


# --------------------------------------------------------------------- PDF ---

def _draw_qr(c: canvas.Canvas, payload: str, x: float, top: float) -> None:
    """Vector QR, QR_SIZE square, top-left corner at (x, top) in top-down page coordinates."""
    matrix = tag_pdf._qr_matrix(payload)
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


def _rect(c: canvas.Canvas, x: float, top: float, w: float, h: float, radius: float) -> None:
    """Stroked rectangle; (x, top) top-left in top-down page coordinates."""
    if radius > 0:
        c.roundRect(x, PAGE_H - (top + h), w, h, radius, stroke=1, fill=0)
    else:
        c.rect(x, PAGE_H - (top + h), w, h, stroke=1, fill=0)


def _draw_sticker(c: canvas.Canvas, record: AnchorRecord, box_x: float, box_top: float) -> None:
    """box_x / box_top = top-left of the sticker box (inside its border), top-down coordinates."""
    cx = box_x + BOX_W / 2
    c.setStrokeColorRGB(0, 0, 0)

    # 1. sticker border: stroke centred BORDER/2 outside the box (outer edge radius = RADIUS)
    if BORDER > 0:
        half = BORDER / 2
        c.setLineWidth(BORDER)
        _rect(c, box_x - half, box_top - half, BOX_W + BORDER, BOX_H + BORDER, max(RADIUS - half, 0))

    # 2. logo box: stroke centred LOGO_BOX_BORDER/2 inside the box
    if LOGO_BOX_BORDER > 0:
        half = LOGO_BOX_BORDER / 2
        c.setLineWidth(LOGO_BOX_BORDER)
        _rect(c, cx - LOGO_BOX_W / 2 + half, box_top + LOGO_BOX_TOP + half,
              LOGO_BOX_W - LOGO_BOX_BORDER, LOGO_BOX_H - LOGO_BOX_BORDER, max(LOGO_BOX_RADIUS - half, 0))

    # 3. logo
    logo = tag_pdf._logo()
    if logo is not None:
        c.drawImage(logo, cx - LOGO_W / 2, PAGE_H - (box_top + LOGO_TOP + LOGO_H), LOGO_W, LOGO_H, mask="auto")

    # 4. circle: stroke centred CIRCLE_BORDER/2 outside the circle
    if CIRCLE_BORDER > 0:
        c.setLineWidth(CIRCLE_BORDER)
        c.circle(cx, PAGE_H - (box_top + CIRCLE_TOP + CIRCLE_D / 2), CIRCLE_D / 2 + CIRCLE_BORDER / 2, stroke=1, fill=0)

    # 5. QR
    _draw_qr(c, record.payload, cx - QR_SIZE / 2, box_top + QR_TOP)

    # 6, 8, 9. text, each centred vertically in its line box (like CSS line-height)
    font = font_name()
    c.setFillColorRGB(0, 0, 0)
    for text, top, size, line_height, left in _text_rows(record):
        ascent, descent = pdfmetrics.getAscentDescent(font, size)
        baseline = box_top + top + (line_height - (ascent - descent)) / 2 + ascent
        c.setFont(font, size)
        if left is None:
            c.drawCentredString(cx, PAGE_H - baseline, text)
        else:
            c.drawString(box_x + left, PAGE_H - baseline, text)

    # 7. divider line
    if LINE_THICKNESS > 0:
        c.setStrokeColorRGB(*_hex_rgb(LINE_COLOR))
        c.setLineWidth(LINE_THICKNESS)
        y = PAGE_H - (box_top + LINE_Y)
        # Rounded: round caps add half the thickness at each end, so the line is drawn shorter
        # by that much and the total length stays LINE_W.
        inset = LINE_THICKNESS / 2 if LINE_RADIUS > 0 else 0
        c.setLineCap(1 if LINE_RADIUS > 0 else 0)
        c.line(cx - LINE_W / 2 + inset, y, cx + LINE_W / 2 - inset, y)
        c.setLineCap(0)
        c.setStrokeColorRGB(0, 0, 0)


def build_pdf(
    records: Iterable[AnchorRecord],
    path: Path,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Stickers left-to-right, top-to-bottom, PER_PAGE per A4 page. Returns pages written."""
    records = list(records)
    if not records:
        raise ValueError("No anchors selected")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle("SyTrak anchor stickers")
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
        _draw_sticker(c, record, box_x, box_top)
        if progress:
            progress(i + 1, len(records))
    c.save()
    return pages


# ------------------------------------------------------- on-screen preview ---

@lru_cache(maxsize=8)
def _pil_font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(asset_path("fonts", FONT_FILE)), px)
    except OSError:
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def _pil_logo() -> Image.Image | None:
    try:
        return Image.open(asset_path("icons", "company-logo.png")).convert("RGBA")
    except OSError:
        return None


def render_preview(record: AnchorRecord, scale: float = 1.0) -> Image.Image:
    """Bitmap of one sticker (box + border) using the same layout numbers as the PDF."""
    def s(v: float) -> float:
        return v * scale

    def r(v: float) -> int:
        return round(v * scale)

    pad = BORDER
    w, h = r(BOX_W + 2 * pad), r(BOX_H + 2 * pad)
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    cx = w / 2
    top = s(pad)                 # sticker box top (inside its border)

    if BORDER > 0:
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=r(RADIUS), outline="black", width=max(r(BORDER), 1))
    if LOGO_BOX_BORDER > 0:
        x0, y0 = cx - s(LOGO_BOX_W) / 2, top + s(LOGO_BOX_TOP)
        draw.rounded_rectangle((x0, y0, x0 + s(LOGO_BOX_W), y0 + s(LOGO_BOX_H)), radius=r(LOGO_BOX_RADIUS),
                               outline="black", width=max(r(LOGO_BOX_BORDER), 1))
    logo = _pil_logo()
    if logo is not None:
        logo = logo.resize((r(LOGO_W), r(LOGO_H)), Image.LANCZOS)
        img.paste(logo, (round(cx - logo.width / 2), round(top + s(LOGO_TOP))), logo)
    if CIRCLE_BORDER > 0:
        rad = s(CIRCLE_D / 2 + CIRCLE_BORDER)
        cy = top + s(CIRCLE_TOP + CIRCLE_D / 2)
        draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), outline="black", width=max(r(CIRCLE_BORDER), 1))

    code = qr_image(record.payload, box_size=8, border=0).resize((r(QR_SIZE), r(QR_SIZE)), Image.NEAREST)
    img.paste(code, (round(cx - code.width / 2), round(top + s(QR_TOP))))

    for text, line_top, size, line_height, left in _text_rows(record):
        y = top + s(line_top + line_height / 2)
        font = _pil_font(max(r(size), 6))
        if left is None:
            draw.text((cx, y), text, fill="black", font=font, anchor="mm")
        else:
            draw.text((s(pad + left), y), text, fill="black", font=font, anchor="lm")

    if LINE_THICKNESS > 0:
        y, half = top + s(LINE_Y), max(s(LINE_THICKNESS), 1) / 2
        radius = min(s(LINE_RADIUS), half) if LINE_RADIUS > 0 else 0
        draw.rounded_rectangle((cx - s(LINE_W) / 2, y - half, cx + s(LINE_W) / 2, y + half), radius=radius, fill=LINE_COLOR)
    return img
