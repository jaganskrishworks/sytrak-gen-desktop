"""Build the app icons from assets/icons/company-logo.png (dev-time only).

Uses the pin mark (left part of the logo) - the "SYTRAK RTLS" text is
unreadable at icon sizes.

    python tools/make_icon.py

Writes
  assets/icons/app.ico, app.png                        (this Windows app + exe)
  ../sytrak_mobile/assets/icon/app_icon.png            (mobile, 1024 px on white)
  ../sytrak_mobile/assets/icon/app_icon_foreground.png (Android adaptive foreground)
Then in sytrak_mobile run:  dart run flutter_launcher_icons
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "assets" / "icons" / "company-logo.png"
MOBILE = ROOT.parent / "sytrak_mobile" / "assets" / "icon"


def pin_mark() -> Image.Image:
    """Crop the pin mark: the first opaque block of columns in the logo."""
    logo = Image.open(LOGO).convert("RGBA")
    alpha = logo.getchannel("A")
    end = next(x for x in range(logo.width)
               if x > 10 and all(alpha.getpixel((x, y)) <= 20 for y in range(logo.height)))
    return logo.crop((0, 0, end, logo.height)).crop(logo.crop((0, 0, end, logo.height)).getbbox())


def square(mark: Image.Image, size: int, fill: float, background=None) -> Image.Image:
    """Centre the mark on a square canvas, scaled so its larger side is fill * size."""
    canvas = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
    scale = fill * size / max(mark.size)
    m = mark.resize((round(mark.width * scale), round(mark.height * scale)), Image.LANCZOS)
    canvas.alpha_composite(m, ((size - m.width) // 2, (size - m.height) // 2))
    return canvas


def main() -> None:
    mark = pin_mark()

    out = ROOT / "assets" / "icons"
    icon = square(mark, 256, 0.94)
    icon.save(out / "app.png")
    icon.save(out / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {out / 'app.ico'} and app.png")

    if (ROOT.parent / "sytrak_mobile" / "pubspec.yaml").is_file():
        MOBILE.mkdir(parents=True, exist_ok=True)
        square(mark, 1024, 0.72, (255, 255, 255, 255)).convert("RGB").save(MOBILE / "app_icon.png")
        # Android masks adaptive icons to a circle/squircle: keep the mark in the inner safe zone.
        square(mark, 1024, 0.52).save(MOBILE / "app_icon_foreground.png")
        print(f"Wrote {MOBILE / 'app_icon.png'} and app_icon_foreground.png")


if __name__ == "__main__":
    main()
