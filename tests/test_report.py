import csv
import json
from pathlib import Path
import pytest

from switch_sync.models import (
    ApplyResult,
    Device,
    PlanItem,
    Platform,
    Status,
    SyncVerdict,
    Verdict,
)
from switch_sync.report import (
    write_ntp_result_csv,
    write_plan_json,
    write_report_md,
    write_verify_csv,
)


def test_write_plan_json_round_trip_and_no_password(tmp_path):
    secret_pass = "SUPER_SECRET_P@SSW0RD_123"
    dev = Device(
        name="SW-TEST",
        ip="10.0.0.1",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
        password=secret_pass,
    )
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="Update NTP server",
        commands=["sntp unicast", "write memory"],
        needs_change=True,
        servers_before=["192.168.1.1"],
        timezone_before="420",
    )

    out_file = tmp_path / "plan.json"
    write_plan_json([plan_item], out_file)

    raw_text = out_file.read_text(encoding="utf-8")
    assert secret_pass not in raw_text

    data = json.loads(raw_text)
    assert len(data) == 1
    item = data[0]
    assert item["device"]["name"] == "SW-TEST"
    assert item["device"]["ip"] == "10.0.0.1"
    assert "password" not in item["device"]
    assert item["status"] == "NEEDS-CHANGE"
    assert item["needs_change"] is True
    assert item["commands"] == ["sntp unicast", "write memory"]
    assert item["servers_before"] == ["192.168.1.1"]
    assert item["timezone_before"] == "420"


def test_write_ntp_result_csv_exact_headers_and_no_password(tmp_path):
    secret_pass = "TOP_SECRET_PASSWORD"
    dev = Device(
        name="ASW01-MD-DC",
        ip="172.16.12.246",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
        password=secret_pass,
    )
    res = ApplyResult(
        device=dev,
        status=Status.OK,
        commands=["sntp unicast", "write memory"],
        clock_before="Tue Oct 20 10:02:18 2026",
        clock_after="Tue Oct 20 10:02:20 2026",
        wrote=True,
    )
    # Extra metadata for CSV columns
    res.server_before = "192.168.240.240"
    res.server_after = "192.168.240.240"
    res.tz_before = "420"
    res.tz_after = "420"
    res.sync_status = "synchronized"
    res.stratum = 2
    res.offset_s = 0.12
    res.detail = "Success"

    out_file = tmp_path / "ntp_result.csv"
    write_ntp_result_csv([res], out_file)

    raw_text = out_file.read_text(encoding="utf-8")
    assert secret_pass not in raw_text

    with open(out_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        row = next(reader)

    expected_header = [
        "host",
        "ip",
        "model",
        "platform",
        "status",
        "server_before",
        "server_after",
        "tz_before",
        "tz_after",
        "clock_before",
        "clock_after",
        "sync_status",
        "stratum",
        "offset_s",
        "detail",
    ]
    assert header == expected_header
    assert row[0] == "ASW01-MD-DC"
    assert row[1] == "172.16.12.246"
    assert row[2] == "Aruba2930"
    assert row[3] == "arubos-s"
    assert row[4] == "OK"
    assert row[9] == "Tue Oct 20 10:02:18 2026"
    assert row[10] == "Tue Oct 20 10:02:20 2026"


def test_write_verify_csv_exact_headers_and_no_password(tmp_path):
    secret_pass = "ANOTHER_SECRET_PASSWORD"
    dev = Device(
        name="CX01",
        ip="172.21.255.1",
        model="aruba8400",
        platform=Platform.ARUBOS_CX,
        password=secret_pass,
    )
    v = Verdict(
        device=dev,
        verdict=SyncVerdict.SYNC,
        stratum=2,
        offset_s=0.05,
        skew_s=0.1,
        servers=["192.168.240.240"],
        detail="Synchronized",
    )

    out_file = tmp_path / "verify.csv"
    write_verify_csv([v], out_file)

    raw_text = out_file.read_text(encoding="utf-8")
    assert secret_pass not in raw_text

    with open(out_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        row = next(reader)

    expected_header = [
        "host",
        "ip",
        "model",
        "platform",
        "verdict",
        "stratum",
        "offset_s",
        "skew_s",
        "servers",
        "detail",
    ]
    assert header == expected_header
    assert row[0] == "CX01"
    assert row[1] == "172.21.255.1"
    assert row[2] == "aruba8400"
    assert row[3] == "arubos-cx"
    assert row[4] == "SYNC"
    assert row[5] == "2"
    assert row[6] == "0.05"
    assert row[7] == "0.1"
    assert row[8] == "192.168.240.240"
    assert row[9] == "Synchronized"


def test_write_report_md_and_no_password(tmp_path):
    secret_pass = "HIDDEN_SECRET_PASS"
    summary = {
        "pass": True,
        "total": 10,
        "platforms": {
            "arubos-s": {"total": 8, "ok": 8, "fail": 0},
            "arubos-cx": {"total": 2, "ok": 2, "fail": 0},
        },
        "manual_action": ["172.16.21.5 (Controller bypassed)"],
        "findings": ["All accessible switches synchronized within 1s."],
        "leaked_pass": secret_pass,
    }

    out_file = tmp_path / "report.md"
    write_report_md(summary, out_file)

    raw_text = out_file.read_text(encoding="utf-8")
    assert secret_pass not in raw_text
    assert "PASS" in raw_text
    assert "arubos-s" in raw_text
    assert "arubos-cx" in raw_text
    assert "172.16.21.5" in raw_text
    assert "All accessible switches synchronized" in raw_text


def test_report_from_pilot_artifacts_in_temp_dir(tmp_path):
    from switch_sync.report import build_summary_from_artifacts, generate_report

    # Populate temporary out directory with the 4 pilot artifacts
    plan_data = [
        {
            "device": {"name": "ASW01-MD-DC", "ip": "172.16.12.246", "model": "Aruba2930", "platform": "arubos-s"},
            "status": "OK-ALREADY",
            "reason": "Configuration and sync indicator satisfied",
            "commands": [],
            "needs_change": False,
        },
        {
            "device": {"name": "ESW01-MD-DC", "ip": "172.16.12.213", "model": "Aruba2930", "platform": "arubos-s"},
            "status": "OK-ALREADY",
            "reason": "Configuration and sync indicator satisfied",
            "commands": [],
            "needs_change": False,
        },
        {
            "device": {"name": "SW-MD-L2-UNTA", "ip": "172.16.12.216", "model": "Aruba2530", "platform": "arubos-s"},
            "status": "NEEDS-CHANGE",
            "reason": "Configuration or sync differs from target",
            "commands": ["sntp unicast"],
            "needs_change": True,
        },
    ]
    (tmp_path / "plan_pilot.json").write_text(json.dumps(plan_data), encoding="utf-8")

    (tmp_path / "ntp_result.csv").write_text(
        "host,ip,model,platform,status,server_before,server_after,tz_before,tz_after,clock_before,clock_after,sync_status,stratum,offset_s,detail\n"
        "SW-MD-L2-UNTA,172.16.12.216,Aruba2530,arubos-s,OK,,,,,\"clock1\",\"clock2\",,,,\n",
        encoding="utf-8",
    )

    (tmp_path / "verify_unta.csv").write_text(
        "host,ip,model,platform,verdict,stratum,offset_s,skew_s,servers,detail\n"
        "SW-MD-L2-UNTA,172.16.12.216,Aruba2530,arubos-s,UNSYNC,,-1161215858.46,1161241388.0,192.168.240.240,Clock skew 1161241388.00s exceeds max 2.0s\n"
        "CORE DISTRI HG4,172.16.13.1,Aruba5400,arubos-s,SYNC,,25529.53,0.0,192.168.240.240,Synchronized\n",
        encoding="utf-8",
    )

    (tmp_path / "probe_pilot.csv").write_text(
        "host,ip,model,platform,status,server_before,server_after,tz_before,tz_after,clock_before,clock_after,sync_status,stratum,offset_s,detail\n"
        "ASW01-MD-DC,172.16.12.246,Aruba2930,arubos-s,OK,192.168.240.240,,,,,,,,,\n"
        "ESW01-MD-DC,172.16.12.213,Aruba2930,arubos-s,OK,192.168.240.240,,,,,,,,,\n"
        "SW-MD-L2-UNTA,172.16.12.216,Aruba2530,arubos-s,OK,,,,,,,,,,,\n",
        encoding="utf-8",
    )

    # Backup directory with UNTA backup
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "SW-MD-L2-UNTA-running.txt").write_text("running-config text", encoding="utf-8")

    out_report = tmp_path / "report.md"
    generate_report(tmp_path, out_report)
    assert out_report.exists()
    content = out_report.read_text(encoding="utf-8")

    # 1. Total device count = 4
    assert "Total Devices: 4" in content

    # 2. Per-platform breakdown
    assert "arubos-s" in content
    assert "arubos-s: 4" in content or "| arubos-s |" in content

    # 3. Per-status breakdown
    assert "OK-ALREADY: 2" in content
    assert "SYNC: 1" in content
    assert "UNSYNC: 1" in content

    # 4. Overall status FAIL (due to UNSYNC)
    assert "**Overall Status:** FAIL" in content

    # 5. Reference clock and largest deviation
    assert "CORE DISTRI HG4" in content
    assert "172.16.13.1" in content
    assert "1161241388" in content
    assert "2.0s" in content

    # 6. Manual action list
    assert "SW-MD-L2-UNTA" in content
    assert "172.16.12.216" in content
    assert "UNSYNC" in content
    assert "exceeds max 2.0s" in content

    # 7. Security findings
    assert "No credential" in content or "no credential" in content
    assert "backup" in content.lower()

    # 8. Artifacts used with names and times
    assert "plan_pilot.json" in content
    assert "ntp_result.csv" in content
    assert "verify_unta.csv" in content
    assert "probe_pilot.csv" in content
    assert "modified:" in content


def test_report_with_no_verify_artifact_status_unknown(tmp_path):
    from switch_sync.report import generate_report

    plan_data = [
        {
            "device": {"name": "DEV1", "ip": "1.1.1.1", "model": "Aruba2930", "platform": "arubos-s"},
            "status": "OK-ALREADY",
            "reason": "OK",
        }
    ]
    (tmp_path / "plan_pilot.json").write_text(json.dumps(plan_data), encoding="utf-8")

    out_report = tmp_path / "report.md"
    generate_report(tmp_path, out_report)
    content = out_report.read_text(encoding="utf-8")

    assert "**Overall Status:** UNKNOWN" in content
    assert "Verify: Missing" in content or "Missing" in content


def test_report_with_no_artifacts_says_so_instead_of_zero_devices(tmp_path):
    from switch_sync.report import generate_report

    out_report = tmp_path / "report.md"
    generate_report(tmp_path, out_report)
    content = out_report.read_text(encoding="utf-8")

    assert "**Overall Status:** UNKNOWN" in content
    assert "Total Devices: 0" not in content
    assert "No run artifacts found" in content or "No artifacts found" in content


def test_report_no_password_when_device_name_contains_password(tmp_path):
    from switch_sync.report import generate_report

    plan_data = [
        {
            "device": {
                "name": "SW-PASSWORD-01",
                "ip": "10.0.0.99",
                "model": "Aruba2930",
                "platform": "arubos-s",
                "password": "SUPER_SECRET_VALUE",
            },
            "status": "UNREACHABLE",
            "reason": "Connection timed out",
        }
    ]
    (tmp_path / "plan_pilot.json").write_text(json.dumps(plan_data), encoding="utf-8")

    out_report = tmp_path / "report.md"
    generate_report(tmp_path, out_report)
    content = out_report.read_text(encoding="utf-8")

    # The word "password" in device name must be preserved
    assert "SW-PASSWORD-01" in content
    # The actual password secret must NOT be in the report
    assert "SUPER_SECRET_VALUE" not in content


def test_report_names_reference_server_anchor_deviation_and_threshold(tmp_path):
    from switch_sync.report import generate_report

    meta = {
        "reference_server": "172.16.40.5",
        "reference_stratum": 4,
        "reference_refid": "162.159.200.1",
        "anchor": "ASW01-HG4-2C (172.16.13.208)",
        "anchor_tolerance": "120.0s",
        "anchor_deviation": "9.3000s",
        "threshold": "120.0s",
        "largest_deviation": "49.0000s",
    }
    (tmp_path / "verify_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    (tmp_path / "verify.csv").write_text(
        "host,ip,model,platform,verdict,stratum,offset_s,skew_s,servers,detail\n"
        "ASW01-HG4-2C,172.16.13.208,Aruba2930,arubos-s,SYNC,2,0.01,0.0,192.168.240.240,Synchronized\n"
        "SW-02,172.16.12.10,Aruba2930,arubos-s,SYNC,2,0.02,49.0,192.168.240.240,Synchronized\n",
        encoding="utf-8",
    )

    out_report = tmp_path / "report.md"
    generate_report(tmp_path, out_report)
    content = out_report.read_text(encoding="utf-8")

    assert "Reference Server: 172.16.40.5" in content
    assert "stratum 4" in content
    assert "refid 162.159.200.1" in content
    assert "ASW01-HG4-2C (172.16.13.208)" in content
    assert "Anchor Tolerance: 120.0s" in content
    assert "Anchor Deviation vs Reference Server: 9.3000s" in content
    assert "Fleet Threshold: 120.0s" in content
    assert "49.0000s" in content

