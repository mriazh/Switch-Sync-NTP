import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from switch_sync import cli
from switch_sync.models import (
    Device,
    NtpResult,
    PlanItem,
    Platform,
    ProbeResult,
    Status,
)


class ReadOnlySession:
    def __init__(self, device, username, password, **kwargs):
        self.device = device
        self.username = username
        self.password = password
        self.sent_commands = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send(self, command: str) -> str:
        self.sent_commands.append(command)
        if any(w in command for w in ("write memory", "sntp server", "ntp server", "configure")):
            raise RuntimeError(f"Forbidden write in read-only session: {command}")
        if "show version" in command:
            if self.device.platform == Platform.ARUBOS_CX:
                return "Version : XL.10.04.3050"
            return "Image stamp: /ws/swbuild01/build\n WC.16.05.0004"
        if "show sntp" in command:
            return (
                "SNTP Configuration\n"
                "  Time Sync Mode: Sntp\n"
                "  SNTP Mode : Unicast\n"
                "  Priority SNTP Server Address\n"
                "  1        192.168.240.240\n"
            )
        if "show system" in command:
            return "Time Zone : 420\nSoftware revision : WC.16.05.0004"
        if "show running-config" in command:
            return "sntp server priority 1 192.168.240.240\ntime timezone 420"
        if "show ntp status" in command:
            return "NTP : Enabled\nSystem time : Tue Oct 20 09:58:41 WIB 2026\nSynchronized with an NTP server.\nStratum : 2\n"
        if "show clock" in command:
            return "System is configured for timezone : Asia/Jakarta\nTue Oct 20 09:58:41 WIB 2026\n"
        if "show time" in command:
            return "Tue Oct 20 10:02:18 2026"
        return ""

    def send_lines(self, lines: list[str], delay: float = 0.35) -> str:
        raise RuntimeError("send_lines must not be called in read-only mode!")

    def close(self):
        pass


def test_cli_probe_list(capsys):
    ret = cli.main(["probe", "--list", "--config", "tests/fixtures/switch_sanitized.txt"])
    assert ret == 0
    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.out.strip().splitlines() if line.strip()]
    assert len(lines) == 104

    controllers = [line for line in lines if "controller" in line.lower() or "aruba7200" in line.lower()]
    assert len(controllers) == 3
    for name in ("DOMAIN CONTROLLER", "PRIMARY CONTROLLER", "SECONDARY CONTROLLER"):
        assert any(name in line for line in controllers)


def test_cli_preflight_valid(monkeypatch, capsys):
    res = NtpResult(
        server="192.168.240.240",
        reachable=True,
        valid=True,
        stratum=2,
        root_delay_s=0.012,
        root_dispersion_s=0.005,
        offset_s=0.001,
        mode=4,
        refid="VMTP",
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda s, timeout=5.0: res)
    ret = cli.main(["preflight", "--server", "192.168.240.240"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "reachable" in captured.out.lower()
    assert "stratum" in captured.out.lower()


def test_cli_preflight_invalid(monkeypatch, capsys):
    res = NtpResult(
        server="192.168.240.215",
        reachable=True,
        valid=False,
        stratum=0,
        root_delay_s=0.0,
        root_dispersion_s=0.0,
        offset_s=None,
        mode=4,
        refid="INIT",
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda s, timeout=5.0: res)
    ret = cli.main(["preflight", "--server", "192.168.240.215"])
    assert ret == 2


def test_cli_preflight_offset_unavailable(monkeypatch, capsys):
    res = NtpResult(
        server="192.168.240.240",
        reachable=True,
        valid=True,
        stratum=2,
        root_delay_s=0.0,
        root_dispersion_s=0.025,
        offset_s=None,
        mode=4,
        refid="86.77.84.80",
        error="Server answered with inconsistent timestamps",
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda s, timeout=5.0: res)
    ret = cli.main(["preflight", "--server", "192.168.240.240"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "offset: unavailable (server answered with inconsistent timestamps)" in captured.out.lower()



def test_cli_plan_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: ReadOnlySession(*a, **k))

    out_file = tmp_path / "plan.json"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "plan",
        "--server", "192.168.240.240",
        "--out", str(out_file),
    ])
    assert ret == 0
    assert out_file.exists()

    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 104
    for item in data:
        assert "rollback" in item
        assert "servers_before" in item
        assert "timezone_before" in item
        if item["device"]["name"] in ("PRIMARY CONTROLLER", "SECONDARY CONTROLLER", "DOMAIN CONTROLLER"):
            assert item["status"] == "SKIP-CONTROLLER"
            assert len(item["commands"]) == 0


def test_cli_apply_dry_run_no_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: ReadOnlySession(*a, **k))

    out_csv = tmp_path / "ntp_result.csv"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "apply",
        "--server", "192.168.240.215",
        "--only", "ASW01-ES-L1LW",
        "--out", str(out_csv),
    ])
    assert ret == 0
    assert out_csv.exists()
    captured = capsys.readouterr()
    assert "configure" in captured.out.lower()


def test_plan_item_needs_change_carries_servers_tz_rollback():
    dev = Device(name="SW-TEST", ip="10.1.1.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="420",
        raw={"show running-config | include sntp": "sntp server priority 1 192.168.240.240"},
    )
    item = cli.build_plan_item(
        dev,
        probe=probe,
        target_servers=["192.168.240.215"],
        tz_minutes=420,
        tz_name="Asia/Jakarta",
        cx_vrf="default",
    )
    assert item.status == Status.NEEDS_CHANGE
    assert item.needs_change is True
    assert item.servers_before == ["192.168.240.240"]
    assert item.timezone_before == "420"
    assert item.rollback == [
        "no sntp server priority 1",
        "sntp server priority 1 192.168.240.240",
        "time timezone 420",
        "write memory",
    ]


def test_ok_already_arubos_s():
    dev = Device(name="SW-2930", ip="10.1.1.2", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="420",
        sync_status=None,  # ArubaOS-S has no sync status
    )
    item = cli.build_plan_item(
        dev,
        probe=probe,
        target_servers=["192.168.240.240"],
        tz_minutes=420,
        tz_name=None,
        cx_vrf="default",
    )
    assert item.status == Status.OK_ALREADY
    assert item.needs_change is False
    assert item.commands == []
    assert item.servers_before == ["192.168.240.240"]
    assert item.timezone_before == "420"
    assert len(item.rollback) > 0


def test_ok_already_arubos_cx_synchronized():
    dev = Device(name="SW-8400", ip="10.1.1.3", model="aruba8400", platform=Platform.ARUBOS_CX)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="Asia/Jakarta",
        sync_status="synchronized",
    )
    item = cli.build_plan_item(
        dev,
        probe=probe,
        target_servers=["192.168.240.240"],
        tz_minutes=420,
        tz_name="Asia/Jakarta",
        cx_vrf="default",
    )
    assert item.status == Status.OK_ALREADY
    assert item.needs_change is False


def test_needs_change_arubos_cx_unsynchronized():
    dev = Device(name="SW-8400", ip="10.1.1.3", model="aruba8400", platform=Platform.ARUBOS_CX)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="Asia/Jakarta",
        sync_status="unsynchronized",
    )
    item = cli.build_plan_item(
        dev,
        probe=probe,
        target_servers=["192.168.240.240"],
        tz_minutes=420,
        tz_name="Asia/Jakarta",
        cx_vrf="default",
    )
    assert item.status == Status.NEEDS_CHANGE
    assert item.needs_change is True
    assert item.servers_before == ["192.168.240.240"]
    assert item.timezone_before == "Asia/Jakarta"
    assert "configure terminal" in item.rollback


def test_timesync_sntp_detection():
    dev = Device(name="SW-2520", ip="10.1.1.5", model="Procurve2520", platform=Platform.PROCURVE_LEGACY)
    # Probe with timesync in running-config
    probe_with_ts = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="420",
        raw={"show running-config | include sntp": "timesync sntp\nsntp unicast"},
    )
    item_ts = cli.build_plan_item(
        dev,
        probe=probe_with_ts,
        target_servers=["192.168.240.215"],
        tz_minutes=420,
        tz_name=None,
        cx_vrf=None,
        timesync_mode="sntp",
    )
    assert "timesync sntp" in item_ts.commands

    # Probe without timesync
    probe_no_ts = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="420",
        raw={"show running-config | include sntp": "sntp unicast"},
    )
    item_no_ts = cli.build_plan_item(
        dev,
        probe=probe_no_ts,
        target_servers=["192.168.240.215"],
        tz_minutes=420,
        tz_name=None,
        cx_vrf=None,
        timesync_mode="keep",
    )
    assert "timesync sntp" not in item_no_ts.commands


def test_plan_json_contains_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: ReadOnlySession(*a, **k))

    out_file = tmp_path / "plan.json"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "plan",
        "--server", "192.168.240.215",
        "--only", "ASW01-ES-L1LW",
        "--out", str(out_file),
    ])
    assert ret == 0
    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    item = data[0]
    assert "rollback" in item
    assert "no sntp server priority 1 192.168.240.240" in item["rollback"]



def test_cli_verify_pass(tmp_path, monkeypatch, capsys):
    from switch_sync.models import Device, Platform, SyncVerdict, Verdict

    dev = Device(
        name="ASW01-ES-L1LW",
        ip="172.16.12.127",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
    )
    mock_verdicts = [
        Verdict(
            device=dev,
            verdict=SyncVerdict.SYNC,
            stratum=2,
            offset_s=0.005,
            skew_s=0.01,
            servers=["192.168.240.240"],
            detail="Synchronized",
        )
    ]
    monkeypatch.setattr("switch_sync.verify.verify_devices", lambda *args, **kwargs: mock_verdicts)

    out_csv = tmp_path / "verify.csv"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "0",
        "--only", "ASW01-ES-L1LW",
        "--out", str(out_csv),
    ])
    assert ret == 0
    assert out_csv.exists()
    captured = capsys.readouterr()
    assert "Fleet Synchronization: PASS" in captured.out


def test_cli_verify_fail(tmp_path, monkeypatch, capsys):
    from switch_sync.models import Device, Platform, SyncVerdict, Verdict

    dev = Device(
        name="ASW01-ES-L1LW",
        ip="172.16.12.127",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
    )
    mock_verdicts = [
        Verdict(
            device=dev,
            verdict=SyncVerdict.UNSYNC,
            stratum=None,
            offset_s=10.5,
            skew_s=5.0,
            servers=["192.168.240.240"],
            detail="Clock skew exceeds max",
        )
    ]
    monkeypatch.setattr("switch_sync.verify.verify_devices", lambda *args, **kwargs: mock_verdicts)

    out_csv = tmp_path / "verify.csv"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "0",
        "--only", "ASW01-ES-L1LW",
        "--out", str(out_csv),
    ])
    assert ret == 1
    assert out_csv.exists()
    captured = capsys.readouterr()
    assert "Fleet Synchronization: FAIL" in captured.out


def test_cli_verify_with_anchor_pass(tmp_path, monkeypatch, capsys):
    from switch_sync.models import Device, Platform, SyncVerdict, Verdict

    dev_anchor = Device(
        name="CORE-5400",
        ip="172.16.13.1",
        model="Aruba5400",
        platform=Platform.ARUBOS_S,
    )
    dev_sw = Device(
        name="ASW01-ES-L1LW",
        ip="172.16.12.127",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
    )
    v_anchor = Verdict(
        device=dev_anchor,
        verdict=SyncVerdict.SYNC,
        stratum=2,
        offset_s=0.010,
        skew_s=0.0,
        servers=["192.168.240.240"],
        detail="Synchronized",
    )
    v_anchor.clock = "Tue Oct 20 10:02:18 2026"
    v_sw = Verdict(
        device=dev_sw,
        verdict=SyncVerdict.SYNC,
        stratum=None,
        offset_s=0.015,
        skew_s=0.005,
        servers=["192.168.240.240"],
        detail="Synchronized",
    )
    v_sw.clock = "Tue Oct 20 10:02:18 2026"

    monkeypatch.setattr(
        "switch_sync.verify.verify_devices",
        lambda *args, **kwargs: [v_anchor, v_sw],
    )

    out_csv = tmp_path / "verify.csv"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "0",
        "--anchor", "172.16.13.1",
        "--out", str(out_csv),
    ])
    assert ret == 0
    assert out_csv.exists()
    captured = capsys.readouterr()
    assert "Anchor: CORE-5400 (172.16.13.1)" in captured.out
    assert "Anchor Clock: Tue Oct 20 10:02:18 2026" in captured.out
    assert "Workstation Clock vs Anchor:" in captured.out
    assert "deviation: 0.0000s" in captured.out
    assert "Fleet Synchronization: PASS (reference: anchor 172.16.13.1)" in captured.out


def test_cli_verify_anchor_not_in_inventory_fails(tmp_path, capsys):
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "0",
        "--anchor", "NON_EXISTENT_ANCHOR",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "not found in inventory" in captured.out


def test_cli_verify_anchor_unreachable_fails(tmp_path, monkeypatch, capsys):
    def fake_verify(*args, **kwargs):
        raise RuntimeError("Anchor '172.16.13.1' is unreachable: Connection timed out")

    monkeypatch.setattr("switch_sync.verify.verify_devices", fake_verify)

    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "0",
        "--anchor", "172.16.13.1",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "is unreachable" in captured.out


def test_cli_report(tmp_path, capsys):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps([
        {
            "device": {"name": "DEV1", "ip": "1.1.1.1", "model": "Aruba2930", "platform": "arubos-s"},
            "status": "OK-ALREADY",
            "reason": "Satisfied",
            "commands": [],
            "needs_change": False,
            "servers_before": ["192.168.240.240"],
            "timezone_before": "420",
        }
    ]), encoding="utf-8")

    verify_file = tmp_path / "verify.csv"
    verify_file.write_text("host,ip,model,platform,verdict,stratum,offset_s,skew_s,servers,detail\nDEV1,1.1.1.1,Aruba2930,arubos-s,SYNC,2,0.01,0.01,192.168.240.240,Synchronized\n", encoding="utf-8")

    out_md = tmp_path / "report.md"
    ret = cli.main([
        "--out-dir", str(tmp_path),
        "report",
        "--out", str(out_md),
    ])
    assert ret == 0
    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")
    assert "**Overall Status:** PASS" in content
    captured = capsys.readouterr()
    assert str(out_md) in captured.out


def test_cli_report_with_dir_option(tmp_path, capsys):
    verify_file = tmp_path / "verify_test.csv"
    verify_file.write_text("host,ip,model,platform,verdict,stratum,offset_s,skew_s,servers,detail\nDEV1,1.1.1.1,Aruba2930,arubos-s,SYNC,2,0.01,0.01,192.168.240.240,Synchronized\n", encoding="utf-8")

    # Call report subcommand passing --dir
    ret = cli.main([
        "report",
        "--dir", str(tmp_path),
    ])
    assert ret == 0
    expected_out = tmp_path / "report.md"
    assert expected_out.exists()
    content = expected_out.read_text(encoding="utf-8")
    assert "**Overall Status:** PASS" in content
    assert "verify_test.csv" in content
    captured = capsys.readouterr()
    assert str(expected_out) in captured.out


def test_timesync_sntp_mode_based_emission():
    dev = Device(name="SW-2530", ip="10.1.1.6", model="Aruba2530", platform=Platform.ARUBOS_S)

    # 1. Device reports TIMEP/SNTP (not SNTP), default timesync-mode (keep) -> MUST NOT emit timesync line
    probe_timep = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=[],
        timezone="420",
        sync_mode="TIMEP/SNTP",
        raw={
            "show sntp": "Time Sync Mode : TIMEP/SNTP\nSNTP Mode : DHCP",
            "show running-config | include sntp": "",
        },
    )
    item_timep_default = cli.build_plan_item(dev, probe=probe_timep, target_servers=["172.16.40.5"], tz_minutes=420, tz_name=None, cx_vrf=None, timesync_mode="keep")
    assert "timesync sntp" not in item_timep_default.commands
    assert "timesync ntp" not in item_timep_default.commands

    # 2. When --timesync-mode ntp is requested -> MUST emit timesync ntp
    item_timep_ntp = cli.build_plan_item(dev, probe=probe_timep, target_servers=["172.16.40.5"], tz_minutes=420, tz_name=None, cx_vrf=None, timesync_mode="ntp")
    assert "timesync ntp" in item_timep_ntp.commands

    # 3. When --timesync-mode sntp is requested -> MUST emit timesync sntp
    item_timep_sntp = cli.build_plan_item(dev, probe=probe_timep, target_servers=["172.16.40.5"], tz_minutes=420, tz_name=None, cx_vrf=None, timesync_mode="sntp")
    assert "timesync sntp" in item_timep_sntp.commands

    # 4. Device reports SNTP, no timesync in running config, default keep -> no timesync line
    probe_sntp = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        servers=["192.168.240.240"],
        timezone="420",
        sync_mode="SNTP",
        raw={
            "show sntp": "Time Sync Mode : SNTP\nSNTP Mode : Unicast",
            "show running-config | include sntp": "sntp unicast\nsntp server priority 1 192.168.240.240",
        },
    )
    item_sntp = cli.build_plan_item(dev, probe=probe_sntp, target_servers=["172.16.40.5"], tz_minutes=420, tz_name=None, cx_vrf=None, timesync_mode="keep")
    assert "timesync sntp" not in item_sntp.commands
    assert "timesync ntp" not in item_sntp.commands


def test_cli_verify_warns_when_wait_shorter_than_poll_interval(tmp_path, monkeypatch, capsys):
    from switch_sync.models import Device, Platform, SyncVerdict, Verdict

    dev = Device(
        name="ASW01-ES-L1LW",
        ip="172.16.12.127",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
        poll_interval=720,
    )
    mock_verdicts = [
        Verdict(
            device=dev,
            verdict=SyncVerdict.UNSYNC,
            stratum=None,
            offset_s=10.0,
            skew_s=5.0,
            servers=["192.168.240.240"],
            detail="Unsynchronized",
        )
    ]
    monkeypatch.setattr("switch_sync.verify.verify_devices", lambda *args, **kwargs: mock_verdicts)

    out_csv = tmp_path / "verify.csv"
    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "120",
        "--only", "ASW01-ES-L1LW",
        "--out", str(out_csv),
    ])
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "720" in captured.out
    assert "Wait at least 720s" in captured.out
    assert "poll interval not elapsed" in mock_verdicts[0].detail.lower()


def test_cli_verify_broken_anchor_exits_nonzero(tmp_path, monkeypatch, capsys):
    def fake_verify(*args, **kwargs):
        raise RuntimeError("Anchor '172.16.13.1' (172.16.13.1) clock deviates from reference server '172.16.40.5' by 342.0000s (tolerance 120.0s); refusing to grade fleet")

    monkeypatch.setattr("switch_sync.verify.verify_devices", fake_verify)

    ret = cli.main([
        "--config", "tests/fixtures/switch_sanitized.txt",
        "--out-dir", str(tmp_path),
        "verify",
        "--wait", "0",
        "--anchor", "172.16.13.1",
        "--reference-server", "172.16.40.5",
    ])
    assert ret == 1
    captured = capsys.readouterr()
    assert "172.16.13.1" in captured.out
    assert "342.0000s" in captured.out
    assert "172.16.40.5" in captured.out
    assert "refusing to grade fleet" in captured.out


