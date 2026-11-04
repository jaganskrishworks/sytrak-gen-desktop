"""QR payload format and image rendering.

Payload: the MAC only, upper-case hex without colons, e.g.  E9EBA0ED7C7C
(the SEQ is looked up in the Google Sheet by MAC).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import qrcode
from PIL import Image, ImageDraw, ImageFont

from ..paths import asset_path

MAX_SEQ = 9999
_MAC_RE = re.compile(r"\b([0-9A-F]{2}(?:[:-]?[0-9A-F]{2}){5})\b", re.I)
_SEQ_RE = re.compile(r"\b(\d{4})\b")


def format_seq(seq: int) -> str:
    return f"{seq:04d}"


def normalize_mac(text: str) -> str | None:
    hexchars = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(hexchars) != 12:
        return None
    return ":".join(hexchars[i:i + 2] for i in range(0, 12, 2)).upper()


def compact_mac(mac: str) -> str:
    """E9:EB:A0:ED:7C:7C -> E9EBA0ED7C7C"""
    return re.sub(r"[^0-9A-Fa-f]", "", mac).upper()


def build_payload(mac: str) -> str:
    """QR content: the MAC without colons."""
    return compact_mac(mac)


def parse_payload(text: str) -> tuple[str, int | None] | None:
    """Return (mac, seq or None) from scanned QR text, or None if unrecognised.

    Current codes hold only the MAC ("E9EBA0ED7C7C"); older JSON codes
    ({"mac":..,"seq":..}) and "MAC;SEQ" text are still understood.
    """
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            mac = normalize_mac(str(data["mac"]))
            seq = int(str(data["seq"])) if data.get("seq") is not None else None
            return (mac, seq) if mac else None
    except (ValueError, KeyError, TypeError):
        pass
    m_mac = _MAC_RE.search(text)
    if not m_mac:
        return None
    m_seq = _SEQ_RE.search(_MAC_RE.sub(" ", text))
    return normalize_mac(m_mac.group(1)), int(m_seq.group(1)) if m_seq else None  # type: ignore[return-value]


def _font(size: int, bold: bool = False):
    names = ["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def qr_image(payload: str, box_size: int = 10, border: int = 1) -> Image.Image:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")


@lru_cache(maxsize=1)
def _logo() -> Image.Image | None:
    try:
        return Image.open(asset_path("icons", "company-logo.png")).convert("RGBA")
    except OSError:
        return None


def caption_text(mac: str, seq: int | None) -> str:
    return f"M : {compact_mac(mac)}  .  S : {format_seq(seq) if seq is not None else '----'}"


def render_label(payload: str, mac: str, seq: int | None) -> Image.Image:
    """Label layout:  company logo / QR code / "M:<MAC> . S:<SEQ>"."""
    code = qr_image(payload, box_size=20)
    w = code.width
    pad, gap = 24, 6

    logo = _logo()
    if logo is not None:
        logo_w = int(w * 0.4)
        logo = logo.resize((logo_w, round(logo.height * logo_w / logo.width)), Image.LANCZOS)
    logo_h = logo.height if logo is not None else 0

    caption = caption_text(mac, seq)
    size = 30  # caption font size in px - used as-is, never shrunk
    font = _font(size, bold=True)
    caption_h = size + 22

    # The label widens to fit the caption; logo and QR stay centred.
    label_w = max(w, int(font.getlength(caption)) + 2 * pad)
    img = Image.new("RGB", (label_w, pad + logo_h + gap + w + caption_h), "white")
    y = pad
    if logo is not None:
        img.paste(logo, ((label_w - logo.width) // 2, y), logo)
        y += logo_h + gap
    img.paste(code, ((label_w - w) // 2, y))
    y += w
    ImageDraw.Draw(img).text((label_w / 2, y + 4), caption, fill="black", font=font, anchor="mt")
    return img


def thumbnail(payload: str, size: int = 64) -> Image.Image:
    return qr_image(payload, box_size=2, border=1).resize((size, size), Image.NEAREST)
