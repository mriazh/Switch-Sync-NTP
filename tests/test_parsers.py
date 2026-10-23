from pathlib import Path
import pytest
from switch_sync.models import Platform
from switch_sync.parsers import (
    strip_ansi,
    parse_show_version,
    parse_show_sntp,
    parse_show_ntp_status,
    parse_show_time,
    parse_show_clock,
    parse_show_system,
    parse_running_config,
    is_invalid_input,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    path = FIXTURES / name
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def test_parse_show_version():
    text_246 = read_fixture("172.16.12.246-show_version.txt")
    res_246 = parse_show_version(text_246)
    assert res_246["firmware"] == "WC.16.05.0004"
    assert res_246["platform_hint"] == Platform.ARUBOS_S

    text_cx = read_fixture("172.21.255.1-show_version.txt")
    res_cx = parse_show_version(text_cx)
    assert res_cx["firmware"] == "XL.10.04.3050"
    assert res_cx["platform_hint"] == Platform.ARUBOS_CX

    text_legacy = read_fixture("172.16.61.5-show_version.txt")
    res_legacy = parse_show_version(text_legacy)
    assert res_legacy["firmware"] == "J.14.01"
    assert res_legacy["platform_hint"] == Platform.PROCURVE_LEGACY


def test_parse_show_sntp():
    # 2930 standard
    res_246 = parse_show_sntp(read_fixture("172.16.12.246-show_sntp.txt"))
    assert res_246["servers"] == ["192.168.240.240"]
    assert res_246["entries"] == [(1, "192.168.240.240")]
    assert res_246["poll_interval"] == 720
    assert res_246["mode"] == "Unicast"
    assert res_246["sync_mode"] == "SNTP"
    assert res_246["supported"] is True
    assert res_246["timezone"] is None

    # 2530 DHCP mode (no servers)
    res_216 = parse_show_sntp(read_fixture("172.16.12.216-show_sntp.txt"))
    assert res_216["servers"] == []
    assert res_216["entries"] == []
    assert res_216["poll_interval"] == 720
    assert res_216["mode"] == "DHCP"
    assert res_216["sync_mode"] == "TIMEP/SNTP"
    assert res_216["supported"] is True

    # 5400 with extra OOBM column
    res_5400 = parse_show_sntp(read_fixture("172.16.13.1-show_sntp.txt"))
    assert res_5400["servers"] == ["192.168.240.240"]
    assert res_5400["entries"] == [(1, "192.168.240.240")]
    assert res_5400["poll_interval"] == 720
    assert res_5400["mode"] == "Unicast"
    assert res_5400["sync_mode"] == "SNTP"
    assert res_5400["supported"] is True

    # ProCurve 2520 legacy
    res_legacy = parse_show_sntp(read_fixture("172.16.61.5-show_sntp.txt"))
    assert res_legacy["servers"] == ["192.168.240.240"]
    assert res_legacy["entries"] == [(1, "192.168.240.240")]
    assert res_legacy["poll_interval"] == 720
    assert res_legacy["mode"] == "Unicast"
    assert res_legacy["sync_mode"] == "Sntp"
    assert res_legacy["supported"] is True


def test_parse_show_ntp_status():
    res_cx = parse_show_ntp_status(read_fixture("172.21.255.1-show_ntp_status.txt"))
    assert res_cx["servers"] == []
    assert res_cx["sync_status"] == "unsynchronized"
    assert res_cx["stratum"] is None
    assert res_cx["clock"] == "Tue Oct 20 10:01:19 WIB 2026"
    assert res_cx["enabled"] is True
    assert "default" in res_cx["vrf"].lower()


def test_parse_show_time():
    # 2 spaces day padding
    t_163 = parse_show_time(read_fixture("172.16.12.163-show_time.txt"))
    assert t_163 == "Wed Feb  7 10:22:28 1990"

    t_246 = parse_show_time(read_fixture("172.16.12.246-show_time.txt"))
    assert t_246 == "Tue Jan  2 01:20:24 1990"

    t_5400 = parse_show_time(read_fixture("172.16.13.1-show_time.txt"))
    assert t_5400 == "Tue Oct 20 10:04:50 2026"

    assert parse_show_time("Garbage output") is None


def test_parse_show_clock():
    res_cx = parse_show_clock(read_fixture("172.21.255.1-show_clock.txt"))
    assert res_cx["clock"] == "Tue Oct 20 10:01:25 WIB 2026"
    assert res_cx["timezone"] == "Asia/Jakarta"


def test_parse_show_system():
    res_246 = parse_show_system(read_fixture("172.16.12.246-show_system.txt"))
    assert res_246["timezone"] == "420"
    assert res_246["firmware"] == "WC.16.05.0004"


def test_parse_running_config():
    res_246 = parse_running_config(read_fixture("172.16.12.246-show_running_config_include_sntp.txt"))
    assert res_246["servers"] == ["192.168.240.240"]
    assert res_246["entries"] == [(1, "192.168.240.240")]
    assert res_246["timesync_supported"] is True

    res_216 = parse_running_config(read_fixture("172.16.12.216-show_running_config_include_sntp.txt"))
    assert res_216["servers"] == []
    assert res_216["entries"] == []
    assert res_216["timesync_supported"] is False

    res_cx = parse_running_config(read_fixture("172.21.255.1-show_running_config_include_ntp.txt"))
    assert res_cx["servers"] == []
    assert res_cx["entries"] == []


def test_invalid_input_and_safe_parsing():
    assert is_invalid_input("Invalid input: show foo") is True
    assert is_invalid_input("% Command incomplete.") is True
    assert is_invalid_input("Invalid") is True
    assert is_invalid_input("Incomplete input: 1") is True
    assert is_invalid_input("SNTP Server Address or Priority already configured.") is True
    assert is_invalid_input("show version") is False

    # Never raise on garbage
    assert parse_show_version("garbage") == {"firmware": None, "platform_hint": None}
    assert parse_show_sntp("garbage") == {
        "servers": [],
        "entries": [],
        "mode": None,
        "sync_mode": None,
        "poll_interval": None,
        "supported": False,
        "timezone": None,
    }
    assert parse_show_ntp_status("garbage") == {
        "servers": [],
        "sync_status": None,
        "stratum": None,
        "clock": None,
        "enabled": None,
        "vrf": None,
    }
    assert parse_show_time("garbage") is None
    assert parse_show_clock("garbage") == {"clock": None, "timezone": None}
    assert parse_show_system("garbage") == {"timezone": None, "firmware": None}
    assert parse_running_config("garbage") == {
        "servers": [],
        "entries": [],
        "timezone": None,
        "timesync_supported": False,
    }


def test_parse_show_sntp_and_running_config_entries():
    # 5400 variant with OOBM column
    text_5400 = (
        " SNTP Configuration and Status\n"
        "  Time Sync Mode      : SNTP\n"
        "  SNTP Mode           : Unicast\n"
        "  Poll Interval (sec) : 720\n"
        "  Priority SNTP Server Address                     Version OOBM Key-id\n"
        "  -------- --------------------------------------- ------- ---- ----------\n"
        "  1        192.168.240.240                         3       No   0\n"
        "  2        10.0.0.1                                3       Yes  0\n"
    )
    res_5400 = parse_show_sntp(text_5400)
    assert res_5400["entries"] == [(1, "192.168.240.240"), (2, "10.0.0.1")]
    assert res_5400["servers"] == ["192.168.240.240", "10.0.0.1"]
    assert res_5400["poll_interval"] == 720

    # ProCurve variant
    text_procurve = (
        " SNTP Configuration and Status\n"
        "  Time Sync Mode      : SNTP\n"
        "  Priority SNTP Server Address                      Version Key-id\n"
        "  -------- ---------------------------------------  ------- ----------\n"
        "  1        192.168.240.240                          3       0\n"
        "  2        172.16.40.5                              3       0\n"
    )
    res_pc = parse_show_sntp(text_procurve)
    assert res_pc["entries"] == [(1, "192.168.240.240"), (2, "172.16.40.5")]
    assert res_pc["servers"] == ["192.168.240.240", "172.16.40.5"]

    # Running config lines
    rc_text = (
        "timesync sntp\n"
        "sntp unicast\n"
        "sntp server priority 1 192.168.240.240\n"
        "sntp server priority 2 10.0.0.1\n"
    )
    res_rc = parse_running_config(rc_text)
    assert res_rc["entries"] == [(1, "192.168.240.240"), (2, "10.0.0.1")]
    assert res_rc["servers"] == ["192.168.240.240", "10.0.0.1"]
    assert res_rc["timesync_supported"] is True
