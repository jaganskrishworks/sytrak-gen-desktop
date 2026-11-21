"""Build an anchor sticker PDF without opening the app - nothing is written anywhere.

    python tools/mock_anchor_pdf.py --open                 # the 6 sample anchors below
    python tools/mock_anchor_pdf.py --count 30 --open      # random anchors
    python tools/mock_anchor_pdf.py --file anchors.txt     # rows copied from the sheet (header + rows, tab or comma separated)
    python tools/mock_anchor_pdf.py --sheet --open         # read the real anchor Google Sheet (read only)
    python tools/mock_anchor_pdf.py --out test.pdf --png preview.png

Uses the same layout as the app (core/anchor_pdf.py), so size changes there show up here.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sytrak_gen.config import AnchorSettings  # noqa: E402
from sytrak_gen.core import anchor_pdf  # noqa: E402
from sytrak_gen.core.anchor import AnchorRecord, AnchorSheet, parse_anchor_rows  # noqa: E402

# Same columns as the anchor sheet; "CDK MAC" is printed as ID, "CDK ID" is ignored.
SAMPLE = """Ethernet MAC\tCDK MAC\tCDK ID\tSEQ\tDevice Type
907069F452B4\t907069F452B4\tC0B7FCC2B563613B\t1\tUWB-ANC01
907069F4539C\t907069F4539C\t343A9C2280DD3097\t2\tUWB-ANC01
907069F45AD4\t907069F45AD4\t4A26308BB2BAEFDC\t3\tUWB-ANC01
907069F45554\t907069F45554\t1A17CEE03E7EE685\t4\tUWB-ANC01
907069F45584\t907069F45584\t383B93C801058407\t5\tUWB-ANC01
907069F454E0\t907069F454E0\t66FA90F660DD9158\t6\tUWB-ANC01
"""


def parse_text(text: str) -> list[list[str]]:
    dialect = "excel-tab" if "\t" in text else "excel"
    return [row for row in csv.reader(io.StringIO(text), dialect=dialect)]


def random_anchors(count: int, seed: int) -> list[AnchorRecord]:
    rnd = random.Random(seed)
    macs = ["907069" + rnd.randbytes(3).hex().upper() for _ in range(count)]
    return [AnchorRecord(i + 1, mac, mac, "UWB-ANC01") for i, mac in enumerate(macs)]


def from_sheet() -> tuple[list[AnchorRecord], list[str]]:
    from sytrak_gen.config import Settings
    from sytrak_gen.core.sheets import find_key_file

    settings = Settings.load()
    key = find_key_file(settings.sheet.key_file)
    if key is None:
        raise SystemExit("Service account key not found (secrets/*.json)")
    url = settings.anchor.url or AnchorSettings().url
    return AnchorSheet(key, url, settings.anchor.tab).load()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--count", type=int, help="number of random anchors instead of the 6 samples")
    src.add_argument("--file", type=Path, help="text/CSV file: header row + rows, as copied from the sheet")
    src.add_argument("--sheet", action="store_true", help="read the anchor Google Sheet from the app settings")
    ap.add_argument("--seed", type=int, default=1, help="same seed = same random anchors")
    ap.add_argument("--out", type=Path, default=ROOT / "mock_anchor_stickers.pdf")
    ap.add_argument("--png", type=Path, help="also save a preview image of the first sticker")
    ap.add_argument("--open", action="store_true", help="open the PDF when done")
    args = ap.parse_args()

    problems: list[str] = []
    if args.count:
        records = random_anchors(args.count, args.seed)
    elif args.sheet:
        records, problems = from_sheet()
    else:
        text = args.file.read_text(encoding="utf-8-sig") if args.file else SAMPLE
        records, problems = parse_anchor_rows(parse_text(text))
    for p in problems:
        print(f"  ! {p}")
    if not records:
        print("No valid anchors")
        return 1

    records.sort(key=lambda r: r.seq)
    pages = anchor_pdf.build_pdf(records, args.out)
    print(f"Wrote {args.out}  ({len(records)} stickers, {anchor_pdf.COLS}x{anchor_pdf.ROWS} per page, "
          f"page {anchor_pdf.PAGE_W}x{anchor_pdf.PAGE_H}, {pages} page{'s' if pages != 1 else ''})")
    if args.png:
        anchor_pdf.render_preview(records[0], scale=2).save(args.png)
        print(f"Wrote {args.png}")
    if args.open and sys.platform == "win32":
        os.startfile(args.out)  # noqa: S606
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
