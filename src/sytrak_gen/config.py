"""User settings, persisted as JSON in the per-user data folder."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path

from .paths import user_data_dir

log = logging.getLogger(__name__)


@dataclass
class JLinkSettings:
    exe_path: str = ""  # empty -> auto-detect
    device: str = "nRF52833_xxAA"
    interface: str = "SWD"
    speed: str = "4000"
    mac_source: str = "nrf_ficr"  # see core.jlink.mac.MAC_SOURCES
    mac_address: str = "0x100000A4"  # used when mac_source == "memory"
    mac_reverse: bool = False  # memory bytes stored LSB first
    command_timeout_s: int = 30
    auto_scan_interval_ms: int = 2000
    last_serial: str = ""


@dataclass
class QrSettings:
    seq_start: int = 1
    device_types: str = "UWB-K4W"  # comma separated choices
    device_type: str = ""  # last chosen type (must be chosen before Generate)

    @property
    def device_type_list(self) -> list[str]:
        return [t.strip() for t in self.device_types.split(",") if t.strip()]


@dataclass
class SheetSettings:
    url: str = "https://docs.google.com/spreadsheets/d/1yH55rsyUhYIXG6Krr0JinLy5dPnLPdUcjEJ9hi1j-qo/edit?gid=0#gid=0"
    key_file: str = ""  # empty -> secrets/*.json next to the app, or %APPDATA%/SyTrakGen/google_service_account.json
    tab: str = ""  # selected sheet tab; empty -> the tab in the URL (gid) or the first tab


@dataclass
class StationSettings:
    station_id: str = ""  # empty -> derived from the PC name
    name: str = ""  # shown to mobiles; empty -> PC name
    broker_port: int = 1883
    discovery_port: int = 47474
    password: str = ""  # empty -> open broker; else user "sytrak" + this password


def default_station_id() -> str:
    host = re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname()).upper() or "PC"
    return f"SG-{host}"[:32]


def _coerce(default, value):
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, str):
        return str(value)
    return value


def _from_dict(cls, data):
    obj = cls()
    if not isinstance(data, dict):
        return obj
    for f in fields(cls):
        if f.name not in data or f.default is MISSING:
            continue
        try:
            setattr(obj, f.name, _coerce(f.default, data[f.name]))
        except (TypeError, ValueError):
            log.warning("Ignoring invalid setting %s.%s=%r", cls.__name__, f.name, data[f.name])
    return obj


@dataclass
class Settings:
    jlink: JLinkSettings = field(default_factory=JLinkSettings)
    qr: QrSettings = field(default_factory=QrSettings)
    station: StationSettings = field(default_factory=StationSettings)
    sheet: SheetSettings = field(default_factory=SheetSettings)

    @staticmethod
    def path() -> Path:
        return user_data_dir() / "settings.json"

    @property
    def station_id(self) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "", self.station.station_id) or default_station_id()

    @property
    def station_name(self) -> str:
        return self.station.name.strip() or socket.gethostname()

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            log.exception("Could not read settings, using defaults")
            return cls()
        return cls(
            jlink=_from_dict(JLinkSettings, data.get("jlink")),
            qr=_from_dict(QrSettings, data.get("qr")),
            station=_from_dict(StationSettings, data.get("station")),
            sheet=_from_dict(SheetSettings, data.get("sheet")),
        )

    def save(self) -> None:
        path = self.path()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            log.exception("Could not save settings")
