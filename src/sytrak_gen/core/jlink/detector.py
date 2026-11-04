"""J-Link probe discovery.

USB enumeration uses only OS facilities (cfgmgr32 on Windows, pyusb + bundled
libusb elsewhere), so probes are listed even when no SEGGER software is
installed. When J-Link Commander is available its ShowEmuList output enriches
the list with product names and adds network (IP) probes.
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SEGGER_VID = 0x1366

# e.g. USB\VID_1366&PID_1015\000260123456  (interfaces "...&MI_00\..." don't match)
_INSTANCE_RE = re.compile(
    r"^USB\\VID_(?P<vid>[0-9A-F]{4})&PID_(?P<pid>[0-9A-F]{4})\\(?P<inst>[^\\]+)$", re.I
)
# e.g. J-Link[0]: Connection: USB, Serial number: 260123456, ProductName: J-Link EDU
_EMU_RE = re.compile(
    r"J-Link\[\d+\]:\s*Connection:\s*(?P<conn>\w+),\s*Serial number:\s*(?P<serial>\d+)"
    r"(?:,\s*IP address:\s*(?P<ip>[^,]+))?(?:,\s*ProductName:\s*(?P<product>.+))?",
    re.I,
)


@dataclass
class JLinkProbe:
    serial: str
    product: str = "J-Link"
    connection: str = "USB"
    address: str = ""  # IP address for network probes
    vid: int | None = None
    pid: int | None = None
    source: str = "USB"

    @property
    def key(self) -> str:
        return f"{self.connection.upper()}:{self.serial or self.address}"

    @property
    def is_usb(self) -> bool:
        return self.connection.upper() == "USB"

    @property
    def vid_pid(self) -> str:
        if self.vid is None or self.pid is None:
            return "-"
        return f"{self.vid:04X}:{self.pid:04X}"

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "product": self.product,
            "connection": self.connection,
            "address": self.address,
            "vid_pid": self.vid_pid,
        }


def normalize_serial(raw: str) -> str:
    """J-Link reports its USB serial zero-padded to 12 digits."""
    s = raw.strip()
    return (s.lstrip("0") or s) if s.isdigit() else s


def parse_usb_instance_id(device_id: str) -> JLinkProbe | None:
    m = _INSTANCE_RE.match(device_id.strip())
    if not m or int(m["vid"], 16) != SEGGER_VID:
        return None
    inst = m["inst"]
    # Windows generates an instance id containing '&' when the device has no serial.
    serial = "" if "&" in inst else normalize_serial(inst)
    return JLinkProbe(serial=serial, vid=SEGGER_VID, pid=int(m["pid"], 16), source="USB")


def parse_emu_list(output: str) -> list[JLinkProbe]:
    probes = []
    for line in output.splitlines():
        m = _EMU_RE.search(line)
        if not m:
            continue
        probes.append(
            JLinkProbe(
                serial=m["serial"],
                product=(m["product"] or "J-Link").strip(),
                connection=m["conn"].upper(),
                address=(m["ip"] or "").strip(),
                source="J-Link Commander",
            )
        )
    return probes


# ---------------------------------------------------------------- Windows ---

def _list_windows() -> list[JLinkProbe]:
    import ctypes
    from ctypes import wintypes

    CR_SUCCESS = 0x00
    CR_BUFFER_SMALL = 0x1A
    CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
    CM_GETIDLIST_FILTER_PRESENT = 0x00000100

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class DEVPROPKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.ULONG)]

    cfg = ctypes.WinDLL("cfgmgr32")
    cfg.CM_Get_Device_ID_List_SizeW.argtypes = [ctypes.POINTER(wintypes.ULONG), wintypes.LPCWSTR, wintypes.ULONG]
    cfg.CM_Get_Device_ID_ListW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
    cfg.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
    cfg.CM_Get_DevNode_PropertyW.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(DEVPROPKEY),
        ctypes.POINTER(wintypes.ULONG),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG,
    ]

    # DEVPKEY_Device_BusReportedDeviceDesc -> USB iProduct string, e.g. "J-Link"
    bus_desc_key = DEVPROPKEY(
        GUID(0x540B947E, 0x8B40, 0x45BC, (ctypes.c_ubyte * 8)(0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2)),
        4,
    )

    def product_name(device_id: str) -> str | None:
        devinst = wintypes.DWORD()
        if cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), device_id, 0) != CR_SUCCESS:
            return None
        prop_type = wintypes.ULONG()
        size = wintypes.ULONG(0)
        cfg.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(bus_desc_key), ctypes.byref(prop_type), None, ctypes.byref(size), 0)
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        rc = cfg.CM_Get_DevNode_PropertyW(
            devinst, ctypes.byref(bus_desc_key), ctypes.byref(prop_type), ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(size), 0
        )
        if rc != CR_SUCCESS:
            return None
        return ctypes.wstring_at(buf).strip() or None

    flags = CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
    for _ in range(3):  # list can grow between the size and data calls
        size = wintypes.ULONG(0)
        if cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "USB", flags) != CR_SUCCESS:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        rc = cfg.CM_Get_Device_ID_ListW("USB", buf, size, flags)
        if rc == CR_SUCCESS:
            break
        if rc != CR_BUFFER_SMALL:
            return []
    else:
        return []

    probes = []
    for device_id in buf[: size.value].split("\x00"):
        probe = parse_usb_instance_id(device_id) if device_id else None
        if probe:
            probe.product = product_name(device_id) or probe.product
            probes.append(probe)
    return probes


# ----------------------------------------------------------- Linux / macOS ---

def _list_libusb() -> list[JLinkProbe]:
    try:
        import usb.core
        import usb.util
    except ImportError:
        log.warning("pyusb not installed; USB enumeration unavailable")
        return []
    backend = None
    try:
        import libusb_package

        backend = libusb_package.get_libusb1_backend()
    except Exception:  # fall back to a system libusb
        pass
    probes = []
    for dev in usb.core.find(find_all=True, idVendor=SEGGER_VID, backend=backend) or []:
        serial, product = "", "J-Link"
        try:
            serial = normalize_serial(usb.util.get_string(dev, dev.iSerialNumber) or "")
            product = usb.util.get_string(dev, dev.iProduct) or product
        except Exception:  # no permission to open the device (udev rules)
            pass
        probes.append(JLinkProbe(serial=serial, product=product, vid=dev.idVendor, pid=dev.idProduct, source="USB"))
    return probes


# ------------------------------------------------------------------ public ---

def list_usb_probes() -> list[JLinkProbe]:
    try:
        return _list_windows() if sys.platform == "win32" else _list_libusb()
    except Exception:
        log.exception("USB enumeration failed")
        return []


def detect_probes(jlink_exe: Path | None = None, timeout: float = 15.0) -> list[JLinkProbe]:
    """USB scan, optionally enriched with J-Link Commander's emulator list."""
    probes = list_usb_probes()
    if jlink_exe is None:
        return probes

    from .commander import JLinkCommander

    try:
        emulators = JLinkCommander(jlink_exe).list_emulators(timeout=timeout)
    except Exception:
        log.exception("ShowEmuList failed")
        return probes

    by_serial = {p.serial: p for p in probes if p.serial}
    unmatched_usb = []
    for emu in emulators:
        known = by_serial.get(emu.serial)
        if known:
            known.product = emu.product
            known.source = "USB + Commander"
        elif emu.is_usb:
            unmatched_usb.append(emu)
        else:
            probes.append(emu)
    if unmatched_usb:
        # USB entries without a readable serial are the same probes Commander reported.
        probes = [p for p in probes if p.serial or not p.is_usb] + unmatched_usb
    return probes
