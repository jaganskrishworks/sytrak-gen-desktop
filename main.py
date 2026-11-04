"""SyTrak Gen launcher (also the PyInstaller entry point)."""
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sytrak_gen.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
