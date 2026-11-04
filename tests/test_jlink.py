from pathlib import Path

import pytest

from sytrak_gen.core.jlink.commander import JLinkCommander, TargetConfig
from sytrak_gen.core.jlink.detector import JLinkProbe, normalize_serial, parse_emu_list, parse_usb_instance_id
from sytrak_gen.core.jlink.mac import MacReadError, mac_commands, parse_mac


def test_parse_usb_instance_id_jlink():
    p = parse_usb_instance_id(r"USB\VID_1366&PID_0105\000069730362")
    assert p is not None
    assert (p.serial, p.vid, p.pid) == ("69730362", 0x1366, 0x0105)


def test_parse_usb_instance_id_ignores_other_vendors_and_interfaces():
    assert parse_usb_instance_id(r"USB\VID_8087&PID_0033\5&2bb0e0f&0&10") is None
    assert parse_usb_instance_id(r"USB\VID_1366&PID_1015&MI_00\6&88f1cce&0&0000") is None


def test_normalize_serial():
    assert normalize_serial("000260123456") == "260123456"
    assert normalize_serial("ABC") == "ABC"


def test_parse_emu_list():
    out = (
        "J-Link[0]: Connection: USB, Serial number: 69730362, ProductName: J-Link CE, Nickname: V9\n"
        "J-Link[1]: Connection: IP, Serial number: 801000001, IP address: 192.168.1.50, ProductName: J-Link PRO\n"
    )
    probes = parse_emu_list(out)
    assert [p.serial for p in probes] == ["69730362", "801000001"]
    assert probes[0].is_usb and probes[1].address == "192.168.1.50"


def test_build_args_with_target():
    args = JLinkCommander(Path("JLink.exe")).build_args(
        Path("s.jlink"), serial="69730362", target=TargetConfig("nRF52833_xxAA", "SWD", "4000"))
    assert args[args.index("-USB") + 1] == "69730362"
    assert args[args.index("-Device") + 1] == "nRF52833_xxAA"
    assert args[-2:] == ["-CommandFile", "s.jlink"]


def test_probe_key_distinguishes_usb_and_ip():
    assert JLinkProbe(serial="1").key != JLinkProbe(serial="1", connection="IP", address="x").key


def test_parse_mac_nrf_ficr_real_output():
    # Captured from the nRF52833 on the bench.
    out = ["J-Link>mem32 0x100000A4, 2", "100000A4 = A0ED7C7C CC5FA9EB "]
    assert parse_mac(out, "nrf_ficr") == "E9:EB:A0:ED:7C:7C"
    assert mac_commands("nrf_ficr") == ["mem32 0x100000A4, 2"]


def test_parse_mac_memory_source():
    out = ["20000000 = 11 22 33 44 55 66"]
    assert parse_mac(out, "memory") == "11:22:33:44:55:66"
    assert parse_mac(out, "memory", reverse=True) == "66:55:44:33:22:11"


def test_parse_mac_errors():
    with pytest.raises(MacReadError):
        parse_mac(["Cannot connect to target."], "nrf_ficr")
    with pytest.raises(MacReadError):
        parse_mac(["20000000 = FF FF FF FF FF FF"], "memory")
