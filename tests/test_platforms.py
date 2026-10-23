import pytest
from switch_sync.models import Platform
from switch_sync.platforms import (
    MODEL_PLATFORM,
    platform_from_model,
    platform_from_firmware,
    resolve_platform,
    netmiko_device_type,
    probe_commands,
    clock_command,
    timezone_command,
    config_commands,
    backup_commands,
    rollback_commands,
    should_emit_timesync,
)


def test_model_platform_mapping():
    assert platform_from_model("Aruba2930") == Platform.ARUBOS_S
    assert platform_from_model("aruba2930") == Platform.ARUBOS_S
    assert platform_from_model("Aruba2530") == Platform.ARUBOS_S
    assert platform_from_model("Aruba2530A") == Platform.ARUBOS_S
    assert platform_from_model("Aruba5400") == Platform.ARUBOS_S
    assert platform_from_model("Procurve2530") == Platform.ARUBOS_S
    assert platform_from_model("Procurve2620") == Platform.ARUBOS_S
    assert platform_from_model("Procurve2520") == Platform.PROCURVE_LEGACY
    assert platform_from_model("Procurve2610") == Platform.PROCURVE_LEGACY
    assert platform_from_model("aruba8400") == Platform.ARUBOS_CX
    assert platform_from_model("Aruba8400") == Platform.ARUBOS_CX
    assert platform_from_model("Aruba7200") == Platform.ARUBOS_MOBILITY
    assert platform_from_model("adm1n") == Platform.UNKNOWN
    assert platform_from_model("unknown_box") == Platform.UNKNOWN


def test_firmware_fallback():
    assert platform_from_firmware("XL.10.04.3050") == Platform.ARUBOS_CX
    assert platform_from_firmware("KB.16.09.0001") == Platform.ARUBOS_S
    assert platform_from_firmware("WC.16.05.0004") == Platform.ARUBOS_S
    assert platform_from_firmware("YA.15.17.0009") == Platform.ARUBOS_S
    assert platform_from_firmware("YB.16.01.0001") == Platform.ARUBOS_S
    assert platform_from_firmware("RA.16.01.0001") == Platform.ARUBOS_S
    assert platform_from_firmware("J.14.01") == Platform.PROCURVE_LEGACY
    assert platform_from_firmware("Y.11.01") == Platform.PROCURVE_LEGACY
    assert platform_from_firmware("unknown") == Platform.UNKNOWN

    assert resolve_platform("Aruba2930", "WC.16.05.0004") == Platform.ARUBOS_S
    assert resolve_platform("adm1n", "YA.15.17.0009") == Platform.ARUBOS_S
    assert resolve_platform("adm1n", "XL.10.04.3050") == Platform.ARUBOS_CX
    assert resolve_platform("adm1n", None) == Platform.UNKNOWN


def test_netmiko_device_type():
    assert netmiko_device_type(Platform.ARUBOS_S) == "aruba_os"
    assert netmiko_device_type(Platform.ARUBOS_MOBILITY) == "aruba_os"
    assert netmiko_device_type(Platform.PROCURVE_LEGACY) == "aruba_procurve"
    assert netmiko_device_type(Platform.ARUBOS_CX) == "aruba_aoscx"


def test_read_commands():
    aos_cmds = probe_commands(Platform.ARUBOS_S)
    assert "show version" in aos_cmds
    assert "show sntp" in aos_cmds
    assert "show system" in aos_cmds
    assert "show running-config | include sntp" in aos_cmds

    cx_cmds = probe_commands(Platform.ARUBOS_CX)
    assert "show version" in cx_cmds
    assert "show ntp status" in cx_cmds
    assert "show clock" in cx_cmds
    assert "show running-config | include ntp" in cx_cmds

    assert clock_command(Platform.ARUBOS_S) == "show time"
    assert clock_command(Platform.PROCURVE_LEGACY) == "show time"
    assert clock_command(Platform.ARUBOS_CX) == "show clock"

    assert timezone_command(Platform.ARUBOS_S) == "show system"
    assert timezone_command(Platform.PROCURVE_LEGACY) == "show system"
    assert timezone_command(Platform.ARUBOS_CX) == "show clock"


def test_config_commands_templates():
    servers = ["192.168.240.240", "192.168.240.215"]
    
    # ARUBOS_S without timesync keyword
    cmds_s = config_commands(Platform.ARUBOS_S, servers, tz_minutes=420, timesync_supported=False)
    assert "configure" in cmds_s
    assert "timesync sntp" not in cmds_s
    assert "sntp unicast" in cmds_s
    assert "sntp server priority 1 192.168.240.240" in cmds_s
    assert "sntp server priority 2 192.168.240.215" in cmds_s
    assert "time timezone 420" in cmds_s
    assert "write memory" in cmds_s

    # ProCurve with timesync_supported=True
    cmds_legacy = config_commands(Platform.PROCURVE_LEGACY, servers, tz_minutes=420, timesync_supported=True)
    assert "timesync sntp" in cmds_legacy

    # CX template differs from ArubaOS-S
    cmds_cx = config_commands(Platform.ARUBOS_CX, servers, tz_minutes=420, tz_name="Asia/Jakarta")
    assert "configure terminal" in cmds_cx
    assert "no ntp server 192.168.240.240" in cmds_cx
    assert "ntp server 192.168.240.240 iburst" in cmds_cx
    assert "ntp enable" in cmds_cx
    assert "clock timezone Asia/Jakarta" in cmds_cx
    assert "end" in cmds_cx
    assert "write memory" in cmds_cx
    assert "sntp unicast" not in cmds_cx

    # CX with cx_vrf="mgmt"
    cmds_cx_vrf = config_commands(Platform.ARUBOS_CX, servers, tz_minutes=420, tz_name="Asia/Jakarta", cx_vrf="mgmt")
    assert any("ntp server 192.168.240.240" in c and "vrf mgmt" in c for c in cmds_cx_vrf)
    assert any("no ntp server 192.168.240.240" in c and "vrf mgmt" in c for c in cmds_cx_vrf)

    # Controller yields no write commands
    cmds_ctrl = config_commands(Platform.ARUBOS_MOBILITY, servers, tz_minutes=420)
    assert cmds_ctrl == []


def test_backup_commands():
    s_cmds = backup_commands(Platform.ARUBOS_S)
    assert s_cmds == [("show config", "startup"), ("show running-config", "running")]

    leg_cmds = backup_commands(Platform.PROCURVE_LEGACY)
    assert leg_cmds == [("show config", "startup"), ("show running-config", "running")]

    cx_cmds = backup_commands(Platform.ARUBOS_CX)
    assert cx_cmds == [("show startup-config", "startup"), ("show running-config", "running")]

    mob_cmds = backup_commands(Platform.ARUBOS_MOBILITY)
    assert mob_cmds == []

    unk_cmds = backup_commands(Platform.UNKNOWN)
    assert unk_cmds == []


def test_rollback_commands():
    # ArubaOS-S / ProCurve legacy with old server and timezone
    rb_s = rollback_commands(
        Platform.ARUBOS_S,
        servers_before=["192.168.240.240"],
        timezone_before="420",
    )
    assert rb_s == [
        "no sntp server priority 1",
        "sntp server priority 1 192.168.240.240",
        "time timezone 420",
        "write memory",
    ]

    # ArubaOS-S without old server
    rb_s_no_srv = rollback_commands(
        Platform.ARUBOS_S,
        servers_before=[],
        timezone_before="420",
    )
    assert rb_s_no_srv == [
        "no sntp server priority 1",
        "time timezone 420",
        "write memory",
    ]

    # ArubaOS-CX with old server and timezone
    rb_cx = rollback_commands(
        Platform.ARUBOS_CX,
        servers_before=["192.168.240.240"],
        timezone_before="Asia/Jakarta",
    )
    assert rb_cx == [
        "configure terminal",
        "no ntp server 192.168.240.240",
        "ntp server 192.168.240.240 iburst",
        "clock timezone Asia/Jakarta",
        "end",
        "write memory",
    ]

    # ArubaOS-CX without old server
    rb_cx_no_srv = rollback_commands(
        Platform.ARUBOS_CX,
        servers_before=[],
        timezone_before="Asia/Jakarta",
    )
    assert rb_cx_no_srv == [
        "configure terminal",
        "clock timezone Asia/Jakarta",
        "end",
        "write memory",
    ]

    # Mobility Controller yields empty rollback
    rb_ctrl = rollback_commands(
        Platform.ARUBOS_MOBILITY,
        servers_before=["192.168.240.240"],
        timezone_before="420",
    )
    assert rb_ctrl == []


def test_config_commands_removes_existing_entries_with_priority_and_address():
    servers = ["172.16.40.5"]
    entries_before = [(1, "192.168.240.240"), (2, "10.0.0.1")]

    cmds = config_commands(
        Platform.ARUBOS_S,
        servers,
        tz_minutes=420,
        entries_before=entries_before,
    )

    # Must contain exact priority and address removal forms
    assert "no sntp server priority 1 192.168.240.240" in cmds
    assert "no sntp server priority 2 10.0.0.1" in cmds

    # Must NEVER contain bare priority or bare address removals
    assert "no sntp server priority 1" not in cmds
    assert "no sntp server priority 2" not in cmds
    assert "no sntp server 192.168.240.240" not in cmds
    assert "no sntp server 10.0.0.1" not in cmds

    # Removals must come BEFORE adding new targets
    idx_rem1 = cmds.index("no sntp server priority 1 192.168.240.240")
    idx_add1 = cmds.index("sntp server priority 1 172.16.40.5")
    assert idx_rem1 < idx_add1


def test_should_emit_timesync():
    # Reports TIMEP/SNTP -> True
    assert should_emit_timesync("TIMEP/SNTP", False) is True
    assert should_emit_timesync("TIMEP/SNTP", True) is True

    # Reports TIME/SNTP -> True
    assert should_emit_timesync("TIME/SNTP", False) is True

    # Reports exactly SNTP without config keyword -> False
    assert should_emit_timesync("SNTP", False) is False

    # Reports exactly SNTP with config keyword -> True (idempotent re-apply)
    assert should_emit_timesync("SNTP", True) is True

    # Mode not reported (None)
    assert should_emit_timesync(None, True) is True
    assert should_emit_timesync(None, False) is False


def test_config_commands_timesync_mode_options():
    servers = ["192.168.240.240"]
    # keep emits no timesync line
    cmds_keep = config_commands(Platform.ARUBOS_S, servers, tz_minutes=420, timesync_mode="keep")
    assert "timesync sntp" not in cmds_keep
    assert "timesync ntp" not in cmds_keep

    # ntp emits timesync ntp
    cmds_ntp = config_commands(Platform.ARUBOS_S, servers, tz_minutes=420, timesync_mode="ntp")
    assert "timesync ntp" in cmds_ntp
    assert "timesync sntp" not in cmds_ntp

    # sntp emits timesync sntp
    cmds_sntp = config_commands(Platform.ARUBOS_S, servers, tz_minutes=420, timesync_mode="sntp")
    assert "timesync sntp" in cmds_sntp
    assert "timesync ntp" not in cmds_sntp


