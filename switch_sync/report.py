import csv
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Optional, Union

from switch_sync.models import ApplyResult, PlanItem, Verdict


def _ensure_path(path: Union[str, Path]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_plan_json(plan: list[PlanItem], path: Union[str, Path]) -> None:
    p = _ensure_path(path)
    data = []
    for item in plan:
        dev = item.device
        dev_dict = {
            "name": dev.name,
            "ip": dev.ip,
            "model": dev.model,
            "platform": dev.platform.value if hasattr(dev.platform, "value") else str(dev.platform),
            "firmware": dev.firmware,
            "excel_hostname": dev.excel_hostname,
            "label": dev.label,
            "member": dev.member,
            "vlan_mgmt": dev.vlan_mgmt,
            "is_controller": dev.is_controller,
            "protected": dev.protected,
            "notes": list(dev.notes),
        }
        data.append({
            "device": dev_dict,
            "status": item.status.value if hasattr(item.status, "value") else str(item.status),
            "reason": item.reason,
            "commands": list(item.commands),
            "needs_change": item.needs_change,
            "servers_before": list(item.servers_before),
            "timezone_before": item.timezone_before,
            "rollback": list(item.rollback),
            "timesync_mode": getattr(item, "timesync_mode", "keep"),
        })
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def write_ntp_result_csv(results: list[ApplyResult], path: Union[str, Path]) -> None:
    p = _ensure_path(path)
    header = [
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

    def _fmt(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, (list, tuple)):
            return ";".join(str(x) for x in val)
        return str(val)

    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in results:
            dev = r.device
            plat = dev.platform.value if hasattr(dev.platform, "value") else str(dev.platform)
            stat = r.status.value if hasattr(r.status, "value") else str(r.status)

            srv_before = getattr(r, "server_before", None) or getattr(r, "servers_before", "")
            srv_after = getattr(r, "server_after", None) or getattr(r, "servers_after", "")
            tz_before = getattr(r, "tz_before", None) or getattr(r, "timezone_before", "")
            tz_after = getattr(r, "tz_after", None) or getattr(r, "timezone_after", "")
            sync_stat = getattr(r, "sync_status", "")
            strat = getattr(r, "stratum", "")
            offset = getattr(r, "offset_s", "")
            detail = r.error or getattr(r, "detail", "") or ""

            writer.writerow([
                dev.name,
                dev.ip,
                dev.model,
                plat,
                stat,
                _fmt(srv_before),
                _fmt(srv_after),
                _fmt(tz_before),
                _fmt(tz_after),
                _fmt(r.clock_before),
                _fmt(r.clock_after),
                _fmt(sync_stat),
                _fmt(strat),
                _fmt(offset),
                _fmt(detail),
            ])


def write_verify_csv(verdicts: list[Verdict], path: Union[str, Path]) -> None:
    p = _ensure_path(path)
    header = [
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

    def _fmt(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, (list, tuple)):
            return ";".join(str(x) for x in val)
        return str(val)

    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for v in verdicts:
            dev = v.device
            plat = dev.platform.value if hasattr(dev.platform, "value") else str(dev.platform)
            verd = v.verdict.value if hasattr(v.verdict, "value") else str(v.verdict)
            writer.writerow([
                dev.name,
                dev.ip,
                dev.model,
                plat,
                verd,
                _fmt(v.stratum),
                _fmt(v.offset_s),
                _fmt(v.skew_s),
                _fmt(v.servers),
                _fmt(v.detail),
            ])


def find_artifacts(out_dir: Union[str, Path]) -> dict[str, Optional[tuple[Path, str]]]:
    d = Path(out_dir)
    families = {
        "Plan": ["plan.json", "plan*.json"],
        "Apply": ["ntp_result.csv", "ntp_result*.csv"],
        "Verify": ["verify.csv", "verify*.csv"],
        "Probe": ["probe.csv", "probe*.csv"],
    }
    found: dict[str, Optional[tuple[Path, str]]] = {}
    for fam, patterns in families.items():
        candidates: list[Path] = []
        for pat in patterns:
            for p in d.glob(pat):
                if p.is_file() and p.name != "report.md":
                    candidates.append(p)
        if not candidates:
            found[fam] = None
        else:
            candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
            chosen = candidates[0]
            mtime_dt = datetime.fromtimestamp(chosen.stat().st_mtime)
            mtime_str = mtime_dt.strftime("%Y-%m-%d %H:%M:%S")
            found[fam] = (chosen, mtime_str)
    return found


def build_summary_from_artifacts(out_dir: Union[str, Path]) -> dict[str, Any]:
    d = Path(out_dir)
    found = find_artifacts(d)

    if all(v is None for v in found.values()):
        return {
            "no_artifacts": True,
            "total": 0,
            "pass": False,
            "overall_status": "UNKNOWN",
            "artifacts": {fam: None for fam in ["Plan", "Apply", "Verify", "Probe"]},
            "status_counts": {},
            "platforms": {},
            "reference": "N/A (no artifacts found)",
            "largest_deviation": "N/A",
            "threshold": "2.0s",
            "manual_action": [],
            "security_findings": [
                "No credentials were written to any output file.",
                "Devices with missing configuration backup: None",
                "Skipped protected devices: None",
                "Skipped controllers: None",
            ],
            "missing_backups": [],
            "skipped_protected": [],
            "skipped_controllers": [],
            "passwords_to_scrub": [],
        }

    devices: dict[str, dict[str, Any]] = {}
    passwords_to_scrub: set[str] = set()

    def get_entry(host: str, ip: str, model: str = "", platform: str = "", is_controller: bool = False, protected: bool = False):
        key = ip or host
        if not key:
            return None
        if key not in devices:
            devices[key] = {
                "host": host,
                "ip": ip,
                "model": model,
                "platform": platform or "unknown",
                "is_controller": is_controller,
                "protected": protected,
                "plan_status": None,
                "plan_reason": None,
                "apply_status": None,
                "apply_detail": None,
                "verify_verdict": None,
                "verify_detail": None,
                "verify_skew": None,
                "probe_status": None,
                "probe_detail": None,
                "has_backup": False,
            }
        entry = devices[key]
        if host and not entry["host"]:
            entry["host"] = host
        if ip and not entry["ip"]:
            entry["ip"] = ip
        if model and not entry["model"]:
            entry["model"] = model
        if platform and entry["platform"] in ("unknown", ""):
            entry["platform"] = platform
        if is_controller:
            entry["is_controller"] = True
        if protected:
            entry["protected"] = True
        return entry

    # 1. Plan
    if found["Plan"]:
        plan_file = found["Plan"][0]
        try:
            with open(plan_file, "r", encoding="utf-8") as f:
                plan_data = json.load(f)
            for item in plan_data:
                dev = item.get("device", {})
                entry = get_entry(
                    host=dev.get("name", ""),
                    ip=dev.get("ip", ""),
                    model=dev.get("model", ""),
                    platform=dev.get("platform", ""),
                    is_controller=dev.get("is_controller", False),
                    protected=dev.get("protected", False),
                )
                if entry:
                    entry["plan_status"] = item.get("status")
                    entry["plan_reason"] = item.get("reason", "")
                if "password" in dev and dev["password"]:
                    passwords_to_scrub.add(str(dev["password"]))
        except Exception:
            pass

    # 2. Apply
    if found["Apply"]:
        apply_file = found["Apply"][0]
        try:
            with open(apply_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entry = get_entry(
                        host=row.get("host", ""),
                        ip=row.get("ip", ""),
                        model=row.get("model", ""),
                        platform=row.get("platform", ""),
                    )
                    if entry:
                        entry["apply_status"] = row.get("status")
                        entry["apply_detail"] = row.get("detail", "")
        except Exception:
            pass

    # 3. Verify
    if found["Verify"]:
        verify_file = found["Verify"][0]
        try:
            with open(verify_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entry = get_entry(
                        host=row.get("host", ""),
                        ip=row.get("ip", ""),
                        model=row.get("model", ""),
                        platform=row.get("platform", ""),
                    )
                    if entry:
                        entry["verify_verdict"] = row.get("verdict")
                        entry["verify_detail"] = row.get("detail", "")
                        skew_raw = row.get("skew_s")
                        if skew_raw:
                            try:
                                entry["verify_skew"] = float(skew_raw)
                            except ValueError:
                                pass
        except Exception:
            pass

    # 4. Probe
    if found["Probe"]:
        probe_file = found["Probe"][0]
        try:
            with open(probe_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entry = get_entry(
                        host=row.get("host", ""),
                        ip=row.get("ip", ""),
                        model=row.get("model", ""),
                        platform=row.get("platform", ""),
                    )
                    if entry:
                        entry["probe_status"] = row.get("status")
                        entry["probe_detail"] = row.get("detail", "")
        except Exception:
            pass

    # Check configuration backups
    backup_dir = d / "backup"
    for entry in devices.values():
        h = entry["host"]
        if h and backup_dir.is_dir():
            b_running = backup_dir / f"{h}-running.txt"
            if b_running.is_file() and b_running.stat().st_size > 0:
                entry["has_backup"] = True

    MANUAL_ACTION_STATUSES = {
        "UNREACHABLE",
        "AUTH-FAIL",
        "CONNECT-FAIL",
        "CONFIG-FAIL",
        "VERIFY-FAIL",
        "UNSYNC",
        "SKIP-UNSUPPORTED",
        "SKIP-PRIVILEGE",
        "SKIP-PROTECTED",
        "SKIP-CONTROLLER",
        "NO-SERVER",
    }

    manual_actions: list[str] = []
    missing_backups: list[str] = []
    skipped_controllers: list[str] = []
    skipped_protected: list[str] = []

    PROTECTED_IPS = {"172.16.13.1", "172.21.255.1", "172.21.255.2", "172.21.255.6"}

    for entry in devices.values():
        h = entry["host"]
        ip = entry["ip"]

        # Check manual action
        manual_status = None
        manual_reason = None
        if entry["verify_verdict"] in MANUAL_ACTION_STATUSES:
            manual_status = entry["verify_verdict"]
            manual_reason = entry["verify_detail"] or "Verification failed"
        elif entry["apply_status"] in MANUAL_ACTION_STATUSES:
            manual_status = entry["apply_status"]
            manual_reason = entry["apply_detail"] or "Apply failed"
        elif entry["plan_status"] in MANUAL_ACTION_STATUSES:
            manual_status = entry["plan_status"]
            manual_reason = entry["plan_reason"] or "Plan skipped"
        elif entry["probe_status"] in MANUAL_ACTION_STATUSES:
            manual_status = entry["probe_status"]
            manual_reason = entry["probe_detail"] or "Probe failed"

        if manual_status:
            entry["effective_status"] = manual_status
            entry["manual_action"] = True
            entry["manual_reason"] = manual_reason
            manual_actions.append(f"{h} ({ip}) - Status: {manual_status} - Reason: {manual_reason}")
        else:
            entry["manual_action"] = False
            if entry["verify_verdict"]:
                entry["effective_status"] = entry["verify_verdict"]
            elif entry["apply_status"]:
                entry["effective_status"] = entry["apply_status"]
            elif entry["plan_status"]:
                entry["effective_status"] = entry["plan_status"]
            elif entry["probe_status"]:
                entry["effective_status"] = entry["probe_status"]
            else:
                entry["effective_status"] = "UNKNOWN"

        # Check backup requirement
        if entry["apply_status"] is not None or entry["plan_status"] == "SKIP-PRIVILEGE":
            if not entry["has_backup"]:
                missing_backups.append(f"{h} ({ip})")

        # Check skipped controllers & protected
        is_ctrl = entry["is_controller"] or entry["model"] == "Aruba7200" or entry["plan_status"] == "SKIP-CONTROLLER" or "controller" in h.lower()
        is_prot = entry["protected"] or ip in PROTECTED_IPS or entry["plan_status"] == "SKIP-PROTECTED"

        if is_ctrl and (entry["plan_status"] == "SKIP-CONTROLLER" or entry["apply_status"] in (None, "SKIP-CONTROLLER")):
            skipped_controllers.append(f"{h} ({ip})")
        elif is_prot and (entry["plan_status"] == "SKIP-PROTECTED" or (entry["apply_status"] is None and entry["verify_verdict"] != "SYNC")):
            skipped_protected.append(f"{h} ({ip})")

    # Overall status
    if found["Verify"] is None:
        overall_status = "UNKNOWN"
        is_pass = False
    else:
        all_sync = True
        for entry in devices.values():
            if entry["verify_verdict"] and entry["verify_verdict"] != "SYNC":
                all_sync = False
                break
            if entry["apply_status"] in ("CONFIG-FAIL", "AUTH-FAIL", "CONNECT-FAIL"):
                all_sync = False
                break
        overall_status = "PASS" if all_sync else "FAIL"
        is_pass = all_sync

    # Clock reference and largest deviation
    reference = "N/A"
    largest_deviation = "N/A"
    threshold = "120.0s"
    reference_server = None
    reference_stratum = None
    reference_refid = None
    anchor_dev_name = None
    anchor_tolerance = None
    anchor_deviation = None

    meta_file = d / "verify_meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            reference_server = meta.get("reference_server")
            reference_stratum = meta.get("reference_stratum")
            reference_refid = meta.get("reference_refid")
            anchor_dev_name = meta.get("anchor")
            anchor_tolerance = meta.get("anchor_tolerance")
            anchor_deviation = meta.get("anchor_deviation")
            if "threshold" in meta:
                threshold = meta["threshold"]
            if "largest_deviation" in meta:
                largest_deviation = meta["largest_deviation"]
        except Exception:
            pass

    if found["Verify"] is not None:
        anchor_cand = None
        skews: list[float] = []
        for entry in devices.values():
            if entry["verify_skew"] is not None:
                skews.append(abs(entry["verify_skew"]))
                if entry["verify_skew"] == 0.0 and entry["verify_verdict"] == "SYNC":
                    anchor_cand = f"{entry['host']} ({entry['ip']})"
            det = entry["verify_detail"] or ""
            m_anc = re.search(
                r"Anchor verified against\s+([^\s\(]+)(?:\s+\(stratum\s+(\d+),\s+refid\s+([^\)]+)\))?:\s+deviation\s+([0-9\.]+)s\s+\(tolerance\s+([0-9\.]+)s\)",
                det,
                re.IGNORECASE,
            )
            if m_anc:
                if not reference_server:
                    reference_server = m_anc.group(1)
                if reference_stratum is None and m_anc.group(2):
                    reference_stratum = int(m_anc.group(2))
                if not reference_refid and m_anc.group(3):
                    reference_refid = m_anc.group(3)
                if not anchor_deviation and m_anc.group(4):
                    anchor_deviation = f"{float(m_anc.group(4)):.4f}s"
                if not anchor_tolerance and m_anc.group(5):
                    anchor_tolerance = f"{float(m_anc.group(5)):.1f}s"

            m = re.search(r"(?:max|threshold)\s+([0-9]+(?:\.[0-9]+)?)s", det, re.IGNORECASE)
            if m and (not meta_file.exists()):
                threshold = f"{m.group(1)}s"

        if anchor_dev_name:
            reference = anchor_dev_name
        elif anchor_cand:
            reference = anchor_cand
        else:
            reference = "Fleet median"

        if skews and (not meta_file.exists()):
            max_s = max(skews)
            largest_deviation = f"{max_s:.1f}s" if max_s.is_integer() else f"{max_s}s"

    status_counts: dict[str, int] = {}
    platforms: dict[str, int] = {}
    for entry in devices.values():
        st = entry["effective_status"]
        status_counts[st] = status_counts.get(st, 0) + 1
        plat = entry["platform"] or "unknown"
        platforms[plat] = platforms.get(plat, 0) + 1

    artifacts_dict: dict[str, Optional[dict[str, str]]] = {}
    for fam, val in found.items():
        if val is not None:
            artifacts_dict[fam] = {"name": val[0].name, "mtime": val[1]}
        else:
            artifacts_dict[fam] = None

    sec_findings = [
        "No credential was written to any output file.",
        f"Devices with missing configuration backup: {', '.join(missing_backups) if missing_backups else 'None'}",
        f"Skipped protected devices: {', '.join(skipped_protected) if skipped_protected else 'None'}",
        f"Skipped controllers: {', '.join(skipped_controllers) if skipped_controllers else 'None'}",
    ]

    return {
        "overall_status": overall_status,
        "pass": is_pass,
        "total": len(devices),
        "status_counts": status_counts,
        "platforms": platforms,
        "reference": reference,
        "reference_server": reference_server,
        "reference_stratum": reference_stratum,
        "reference_refid": reference_refid,
        "anchor_tolerance": anchor_tolerance,
        "anchor_deviation": anchor_deviation,
        "largest_deviation": largest_deviation,
        "threshold": threshold,
        "manual_action": manual_actions,
        "security_findings": sec_findings,
        "missing_backups": missing_backups,
        "skipped_protected": skipped_protected,
        "skipped_controllers": skipped_controllers,
        "artifacts": artifacts_dict,
        "passwords_to_scrub": list(passwords_to_scrub),
    }


def write_report_md(summary: dict[str, Any], path: Union[str, Path]) -> None:
    p = _ensure_path(path)
    status_label = summary.get("overall_status")
    if not status_label:
        is_pass = summary.get("pass")
        if is_pass is True:
            status_label = "PASS"
        elif is_pass is False:
            status_label = "FAIL"
        else:
            status_label = "UNKNOWN"

    lines = [
        "# Switch NTP Synchronization Report",
        "",
        f"**Overall Status:** {status_label}",
        "",
    ]

    artifacts = summary.get("artifacts")
    if artifacts and isinstance(artifacts, dict):
        lines.append("## Run Artifacts")
        for fam in ["Plan", "Apply", "Verify", "Probe"]:
            info = artifacts.get(fam)
            if info and isinstance(info, dict) and info.get("name"):
                mtime_str = info.get("mtime", "")
                lines.append(f"- {fam}: {info['name']} (modified: {mtime_str})")
            elif info and isinstance(info, str):
                lines.append(f"- {fam}: {info}")
            else:
                lines.append(f"- {fam}: Missing")
        lines.append("")

    lines.append("## Summary Totals")
    if summary.get("no_artifacts"):
        lines.append("No run artifacts found in the specified directory.")
    else:
        lines.append(f"- Total Devices: {summary.get('total', 0)}")

    status_counts = summary.get("status_counts")
    if status_counts and isinstance(status_counts, dict):
        lines.append("")
        lines.append("### Per-Status Breakdown")
        for st, count in sorted(status_counts.items()):
            lines.append(f"- {st}: {count}")

    lines.append("")
    lines.append("### Per-Platform Breakdown")
    platforms = summary.get("platforms", {})
    if platforms and isinstance(platforms, dict):
        if all(isinstance(v, int) for v in platforms.values()):
            for plat_name, count in sorted(platforms.items()):
                lines.append(f"- {plat_name}: {count}")
        else:
            lines.append("| Platform | Total | OK | Failed |")
            lines.append("|---|---|---|---|")
            for plat_name, stats in platforms.items():
                if isinstance(stats, dict):
                    tot = stats.get("total", 0)
                    ok = stats.get("ok", 0)
                    fail = stats.get("fail", 0)
                    lines.append(f"| {plat_name} | {tot} | {ok} | {fail} |")
                else:
                    lines.append(f"| {plat_name} | {stats} | - | - |")
    else:
        lines.append("No platform breakdown available.")

    ref = summary.get("reference")
    largest_dev = summary.get("largest_deviation")
    threshold = summary.get("threshold", "120.0s")
    ref_srv = summary.get("reference_server")
    ref_strat = summary.get("reference_stratum")
    ref_refid = summary.get("reference_refid")
    anc_tol = summary.get("anchor_tolerance")
    anc_dev = summary.get("anchor_deviation")

    if ref is not None or largest_dev is not None or ref_srv is not None:
        lines.append("")
        lines.append("## Verification & Clock Comparison")
        if ref_srv:
            strat_info = f" (stratum {ref_strat}, refid {ref_refid})" if ref_strat is not None else ""
            lines.append(f"- Reference Server: {ref_srv}{strat_info}")
        lines.append(f"- Reference Clock: {ref or 'N/A'}")
        if anc_tol is not None:
            lines.append(f"- Anchor Tolerance: {anc_tol}")
        if anc_dev is not None:
            lines.append(f"- Anchor Deviation vs Reference Server: {anc_dev}")
        lines.append(f"- Fleet Threshold: {threshold}")
        lines.append(f"- Largest Deviation: {largest_dev or 'N/A'} (threshold: {threshold})")

    lines.append("")
    lines.append("## Devices Needing Manual Action")
    manual_actions = summary.get("manual_action", [])
    if manual_actions and isinstance(manual_actions, list):
        for act in manual_actions:
            lines.append(f"- {act}")
    else:
        lines.append("None.")

    lines.append("")
    lines.append("## Security Findings")
    findings = summary.get("security_findings") or summary.get("findings")
    if findings and isinstance(findings, list):
        for f in findings:
            lines.append(f"- {f}")
    else:
        lines.append("- No credentials were written to any output file.")
        mb = summary.get("missing_backups", [])
        lines.append(f"- Devices with missing configuration backup: {', '.join(mb) if mb else 'None'}")
        sp = summary.get("skipped_protected", [])
        lines.append(f"- Skipped protected devices: {', '.join(sp) if sp else 'None'}")
        sc = summary.get("skipped_controllers", [])
        lines.append(f"- Skipped controllers: {', '.join(sc) if sc else 'None'}")

    text = "\n".join(lines) + "\n"

    # Scrub passwords without breaking device names containing 'password'
    passwords_to_scrub = summary.get("passwords_to_scrub", [])
    if isinstance(passwords_to_scrub, (list, set)):
        for pw in passwords_to_scrub:
            if isinstance(pw, str) and pw and len(pw) >= 4:
                if pw.lower() in ("password", "passwd", "admin", "unknown", "none"):
                    continue
                text = text.replace(pw, "[REDACTED]")

    for k, v in summary.items():
        if "pass" in k.lower() and k not in ("pass", "overall_status", "passwords_to_scrub"):
            if isinstance(v, str) and v and len(v) >= 4:
                if v.lower() not in ("password", "passwd", "admin", "unknown", "none"):
                    text = text.replace(v, "[REDACTED]")

    p.write_text(text, encoding="utf-8")


def generate_report(out_dir: Union[str, Path], out_path: Optional[Union[str, Path]] = None) -> Path:
    d = Path(out_dir)
    target = Path(out_path) if out_path else d / "report.md"
    summary = build_summary_from_artifacts(d)
    write_report_md(summary, target)
    return target


def build_report_text(out_dir: Union[str, Path]) -> str:
    target = generate_report(out_dir)
    return target.read_text(encoding="utf-8")

