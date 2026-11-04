"""Read the device MAC address over J-Link."""
from __future__ import annotations

import re
from typing import Iterable

from .commander import JLinkCommander, TargetConfig
from .detector import JLinkProbe

MAC_SOURCES = {
    "nrf_ficr": "nRF52 FICR DEVICEADDR (BLE static address)",
    "memory": "Custom memory address (6 raw bytes)",
}
NRF_DEVICEADDR = 0x100000A4

# e.g. "100000A4 = A0ED7C7C CC5FA9EB"  or  "20000000 = 01 02 03 04 05 06"
_MEM_LINE = re.compile(r"^\s*[0-9A-F]{8}\s*=\s*((?:[0-9A-F]{2,8}\s*)+)$", re.I)
_FAIL_LINE = re.compile(r"error|cannot|could not|failed", re.I)


class MacReadError(Exception):
    pass


def mac_commands(source: str, address: str = "") -> list[str]:
    if source == "nrf_ficr":
        return [f"mem32 0x{NRF_DEVICEADDR:08X}, 2"]
    return [f"mem8 {address}, 6"]


def format_mac(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in data)


def _memory_words(lines: Iterable[str]) -> list[str]:
    words: list[str] = []
    for line in lines:
        m = _MEM_LINE.match(line)
        if m:
            words += m.group(1).split()
    return words


def parse_mac(lines: Iterable[str], source: str, reverse: bool = False) -> str:
    words = _memory_words(lines)
    if source == "nrf_ficr":
        if len(words) < 2:
            raise MacReadError("No DEVICEADDR data in J-Link output")
        low, high = int(words[0], 16), int(words[1], 16)
        # BLE static random address: DEVICEADDR with the two MSBs set.
        value = (((high & 0xFFFF) | 0xC000) << 32) | low
        return format_mac(value.to_bytes(6, "big"))

    if len(words) < 6:
        raise MacReadError("Could not read 6 bytes from the configured address")
    data = bytes(int(w, 16) for w in words[:6])
    if data in (b"\xff" * 6, b"\x00" * 6):
        raise MacReadError("MAC area is blank (not programmed)")
    return format_mac(data[::-1] if reverse else data)


def read_mac(
    commander: JLinkCommander,
    probe: JLinkProbe,
    target: TargetConfig,
    source: str,
    address: str = "",
    reverse: bool = False,
    timeout: float = 30,
) -> str:
    result = commander.run(mac_commands(source, address), probe=probe, target=target, timeout=timeout)
    if result.timed_out:
        raise MacReadError("J-Link timed out")
    if not result.ok:
        reason = next((ln.strip() for ln in reversed(result.output) if _FAIL_LINE.search(ln)), "")
        raise MacReadError(reason or f"J-Link exited with code {result.returncode}")
    return parse_mac(result.output, source, reverse)
