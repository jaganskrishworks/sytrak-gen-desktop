"""LAN discovery so mobiles can find SyTrak Gen stations.

Every couple of seconds the station broadcasts a JSON beacon on UDP port
47474. A mobile can also send ``SYTRAK_DISCOVER`` to that port (broadcast)
and every station answers with its beacon directly. The mobile uses the
packet's source IP plus ``mqtt_port`` to connect.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable

log = logging.getLogger(__name__)

DISCOVER_REQUEST = b"SYTRAK_DISCOVER"


def local_ipv4_addresses() -> list[str]:
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    primary = primary_ipv4()
    if primary:
        addrs.add(primary)
    return sorted(a for a in addrs if not a.startswith("127.") and not a.startswith("169.254."))


def primary_ipv4() -> str:
    """IP of the interface holding the default route (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


class DiscoveryBeacon:
    def __init__(self, port: int, info: Callable[[], dict], interval: float = 2.0):
        self.port = port
        self.info = info
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="discovery")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(self.interval + 1)
        self._thread = None

    def _targets(self) -> list[str]:
        targets = {"255.255.255.255"}
        for ip in local_ipv4_addresses():
            targets.add(ip.rsplit(".", 1)[0] + ".255")  # assume /24 subnets
        return sorted(targets)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.port))
        except OSError:
            log.warning("Discovery port %d busy - beacons only, no replies", self.port)
        sock.settimeout(self.interval)
        try:
            while not self._stop.is_set():
                beacon = json.dumps(self.info()).encode()
                for target in self._targets():
                    try:
                        sock.sendto(beacon, (target, self.port))
                    except OSError:
                        pass
                try:
                    data, addr = sock.recvfrom(2048)
                    if data.startswith(DISCOVER_REQUEST):
                        sock.sendto(beacon, addr)
                except (socket.timeout, OSError):
                    pass
        finally:
            sock.close()
