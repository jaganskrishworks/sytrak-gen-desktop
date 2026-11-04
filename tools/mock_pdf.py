"""Build a label PDF from mock MACs - nothing is written to the Google Sheet.

    python tools/mock_pdf.py                          # 150 random MACs (fixed 6 x 9 A4 layout)
    python tools/mock_pdf.py --count 40
    python tools/mock_pdf.py --macs macs.txt          # one MAC per line (any format)
    python tools/mock_pdf.py --start-seq 101 --out test.pdf --open

Uses the same layout as the app (core/pdf.py), so size changes there show up here.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sytrak_gen.core.pdf import COLS, ROWS, build_pdf  # noqa: E402
from sytrak_gen.core.qr import normalize_mac  # noqa: E402
from sytrak_gen.core.sheets import SheetRecord  # noqa: E402


def random_macs(count: int, seed: int) -> list[str]:
    rnd = random.Random(seed)
    macs: set[str] = set()
    while len(macs) < count:
        macs.add(":".join(f"{b:02X}" for b in [0xE9, 0xEB, *rnd.randbytes(4)]))
    return sorted(macs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=150, help="number of random MACs (ignored with --macs)")
    ap.add_argument("--macs", type=Path, help="text file with one MAC per line")
    ap.add_argument("--start-seq", type=int, default=1)
    ap.add_argument("--device-type", default="UWB-K4W")
    ap.add_argument("--seed", type=int, default=1, help="same seed = same random MACs")
    ap.add_argument("--out", type=Path, default=ROOT / "mock_labels.pdf")
    ap.add_argument("--open", action="store_true", help="open the PDF when done")
    args = ap.parse_args()

    if args.macs:
        lines = args.macs.read_text(encoding="utf-8").splitlines()
        macs = [m for m in (normalize_mac(line) for line in lines if line.strip()) if m]
        if not macs:
            print(f"No valid MACs in {args.macs}")
            return 1
    else:
        macs = random_macs(args.count, args.seed)

    records = [SheetRecord(args.start_seq + i, mac, "", args.device_type) for i, mac in enumerate(macs)]
    pages = build_pdf(records, args.out)
    print(f"Wrote {args.out}  ({len(records)} labels, {COLS}x{ROWS} per page, {pages} page{'s' if pages != 1 else ''})")
    if args.open and sys.platform == "win32":
        os.startfile(args.out)  # noqa: S606
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
