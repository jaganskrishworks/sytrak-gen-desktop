"""Read the device MAC over J-Link from the command line - nothing is written to the Google Sheet.

    python tools/read_mac.py                        # first J-Link found
    python tools/read_mac.py --list                 # list connected J-Links
    python tools/read_mac.py --serial 69730362      # a specific J-Link
    python tools/read_mac.py --raw                  # also print the J-Link output
    python tools/read_mac.py --loop                 # read again on every Enter (several devices)

Runs SEGGER JLink.exe (JLinkExe on Linux) with "mem32 0x100000A4, 2" (nRF52 FICR DEVICEADDR),
the same command and MAC calculation as the app (core/jlink/mac.py), plus "mem32 0x10000060, 2"
(FICR DEVICEID, the 64-bit unique chip ID - same as "nrfutil device read --address 0x10000060 --bytes 8").
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sytrak_gen.core.jlink import JLinkCommander, MacReadError, TargetConfig, find_jlink_exe, list_usb_probes  # noqa: E402
from sytrak_gen.core.jlink.mac import mac_commands, parse_mac  # noqa: E402
from sytrak_gen.core.qr import compact_mac  # noqa: E402


DEVICEID = 0x10000060
_ID_LINE = re.compile(rf"{DEVICEID:08X}\s*=\s*([0-9A-F]{{8}})\s+([0-9A-F]{{8}})", re.I)


def device_id(lines: list[str]) -> str:
    """FICR DEVICEID as one 64-bit hex value: DEVICEID[1] (high word) + DEVICEID[0] (low word)."""
    for line in lines:
        m = _ID_LINE.search(line)
        if m:
            return (m.group(2) + m.group(1)).upper()
    raise MacReadError("No DEVICEID data in J-Link output")


def memory_bytes(device_id_hex: str) -> str:
    """Bytes in memory order (little-endian words), as nrfutil prints them with --width 8."""
    value = int(device_id_hex, 16)
    return " ".join(f"{b:02X}" for b in value.to_bytes(8, "little"))


def read_once(commander: JLinkCommander, probe, target: TargetConfig, raw: bool, timeout: float) -> tuple[str, str]:
    # The MAC command goes first: parse_mac reads the first two memory words of the output.
    commands = mac_commands("nrf_ficr") + [f"mem32 0x{DEVICEID:08X}, 2"]
    result = commander.run(commands, probe=probe, target=target, timeout=timeout)
    if raw:
        print("\n".join(f"  | {line}" for line in result.output))
    if result.timed_out:
        raise MacReadError("J-Link timed out")
    if not result.ok:
        reason = next((ln.strip() for ln in reversed(result.output)
                       if any(w in ln.lower() for w in ("error", "cannot", "could not", "failed"))), "")
        raise MacReadError(reason or f"J-Link exited with code {result.returncode}")
    return parse_mac(result.output, "nrf_ficr"), device_id(result.output)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list connected J-Links and exit")
    ap.add_argument("--serial", help="J-Link serial number (default: the first one found)")
    ap.add_argument("--device", default="nRF52833_xxAA")
    ap.add_argument("--speed", default="4000")
    ap.add_argument("--exe", help="path to JLink.exe / JLinkExe (default: auto-detect)")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--raw", action="store_true", help="print the J-Link output too")
    ap.add_argument("--loop", action="store_true", help="read again each time Enter is pressed (q to quit)")
    args = ap.parse_args()

    probes = list_usb_probes()
    if args.list:
        if not probes:
            print("No J-Link connected")
        for p in probes:
            print(f"{p.product}  S/N {p.serial or '?'}")
        return 0

    exe = find_jlink_exe(args.exe)
    if exe is None:
        print("SEGGER J-Link software not found - install it or pass --exe <path to JLink.exe>")
        return 1
    if args.serial:
        probe = next((p for p in probes if p.serial == args.serial), None)
        if probe is None:
            print(f"J-Link S/N {args.serial} not connected (found: {', '.join(p.serial for p in probes) or 'none'})")
            return 1
    elif probes:
        probe = probes[0]
    else:
        print("No J-Link connected")
        return 1

    print(f"J-Link S/N {probe.serial}  ·  {args.device}  ·  {exe}")
    commander = JLinkCommander(exe)
    target = TargetConfig(args.device, "SWD", args.speed)
    while True:
        try:
            mac, dev_id = read_once(commander, probe, target, args.raw, args.timeout)
            print(f"MAC        {mac}   ->   {compact_mac(mac)}")
            print(f"DEVICE ID  {dev_id}   (bytes in memory: {memory_bytes(dev_id)})")
            status = 0
        except MacReadError as exc:
            print(f"FAILED: {exc}")
            status = 2
        if not args.loop:
            return status
        if input("Enter = read next device, q = quit: ").strip().lower() == "q":
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
