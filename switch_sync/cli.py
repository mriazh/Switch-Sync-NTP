import argparse
import json
from pathlib import Path
import sys
from typing import Any, Optional, Sequence

from switch_sync import inventory, ntp, report, runner, verify
from switch_sync.models import (
    ApplyResult,
    Device,
    NtpResult,
    PlanItem,
    Platform,
    ProbeResult,
    RunOptions,
    Status,
    SyncVerdict,
    Verdict,
)
from switch_sync.parsers import parse_show_sntp
from switch_sync.platforms import config_commands, rollback_commands, should_emit_timesync

DEFAULT_EXCEL_PATH = r"C:\Users\adima\OneDrive\Dokumen\Network-Riyadh\Switch-Master.xlsx"


def _add_global_args(p: argparse.ArgumentParser, is_subcommand: bool = False) -> None:
    suppress_or_default = argparse.SUPPRESS if is_subcommand else None
    p.add_argument(
        "--config",
        default=suppress_or_default if is_subcommand else "config/switch.txt",
        help="Path to switch.txt",
    )
    p.add_argument(
        "--excel",
        default=suppress_or_default if is_subcommand else DEFAULT_EXCEL_PATH,
        help="Path to Switch-Master.xlsx",
    )
    p.add_argument(
        "--source",
        choices=["switch", "excel", "both"],
        default=suppress_or_default if is_subcommand else "switch",
        help="Inventory source to use",
    )
    p.add_argument(
        "--user",
        default=suppress_or_default if is_subcommand else None,
        help="Optional username override",
    )
    p.add_argument(
        "--out-dir",
        default=suppress_or_default if is_subcommand else "out",
        help="Output directory",
    )
    p.add_argument(
        "--timeout",
        type=float if is_subcommand else int,
        default=suppress_or_default if is_subcommand else 20,
        help="Per-device timeout in seconds",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=suppress_or_default if is_subcommand else 3,
        help="Connection retries",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switch_sync",
        description="Switch-Sync-NTP: Fleet NTP audit, configuration, and verification.",
    )
    _add_global_args(parser, is_subcommand=False)

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # Subcommand: preflight
    p_preflight = subparsers.add_parser(
        "preflight",
        help="Validate NTP server reachability and time service",
    )
    p_preflight.add_argument("--server", required=True, help="NTP server IP or hostname")
    p_preflight.add_argument("--timeout", type=float, default=5.0, help="UDP query timeout in seconds")
    p_preflight.add_argument("--json", action="store_true", help="Output raw fields in JSON")
    # Subcommand-level globals (except timeout which is specialized for preflight)
    p_preflight.add_argument("--out-dir", default=argparse.SUPPRESS, help="Output directory")

    # Subcommand: probe
    p_probe = subparsers.add_parser(
        "probe",
        help="Probe fleet or list inventory",
    )
    _add_global_args(p_probe, is_subcommand=True)
    p_probe.add_argument("--list", action="store_true", dest="list_devices", help="Print inventory device list and exit")
    p_probe.add_argument("--workers", type=int, default=6, help="Concurrent probe workers (1-12)")
    p_probe.add_argument("--out", default=None, help="Path for probe results CSV")
    p_probe.add_argument("--only", default=None, help="Filter devices: comma-separated hostnames or IPs")
    p_probe.add_argument("--exclude", default=None, help="Exclude devices: comma-separated hostnames or IPs")

    # Subcommand: plan
    p_plan = subparsers.add_parser(
        "plan",
        help="Generate configuration plan without connecting for writes",
    )
    _add_global_args(p_plan, is_subcommand=True)
    p_plan.add_argument("--server", required=True, help="Target NTP server (comma-separated if multiple)")
    p_plan.add_argument("--tz", type=int, default=420, help="Timezone offset in minutes (default 420)")
    p_plan.add_argument("--tz-name", default=None, help="Timezone name for CX (e.g. Asia/Jakarta)")
    p_plan.add_argument("--cx-vrf", default="default", help="VRF for CX (default: default)")
    p_plan.add_argument("--timesync-mode", choices=["keep", "ntp", "sntp"], default="keep", help="Time sync mode: keep (default), ntp, sntp")
    p_plan.add_argument("--out", default=None, help="Path for plan.json output")
    p_plan.add_argument("--only", default=None, help="Filter devices: comma-separated hostnames or IPs")
    p_plan.add_argument("--exclude", default=None, help="Exclude devices: comma-separated hostnames or IPs")
    p_plan.add_argument("--workers", type=int, default=6, help="Concurrent probe workers (1-12)")

    # Subcommand: apply
    p_apply = subparsers.add_parser(
        "apply",
        help="Apply NTP configuration (dry run by default)",
    )
    _add_global_args(p_apply, is_subcommand=True)
    p_apply.add_argument("--server", required=True, help="Target NTP server (comma-separated if multiple)")
    p_apply.add_argument("--tz", type=int, default=420, help="Timezone offset in minutes (default 420)")
    p_apply.add_argument("--tz-name", default=None, help="Timezone name for CX (e.g. Asia/Jakarta)")
    p_apply.add_argument("--cx-vrf", default="default", help="VRF for CX (default: default)")
    p_apply.add_argument("--timesync-mode", choices=["keep", "ntp", "sntp"], default="keep", help="Time sync mode: keep (default), ntp, sntp")
    p_apply.add_argument("--apply", action="store_true", help="Execute write commands (dry run if omitted)")
    p_apply.add_argument("--allow-protected", action="store_true", help="Allow writes to protected devices")
    p_apply.add_argument("--include-controllers", action="store_true", help="Include mobility controllers")
    p_apply.add_argument("--allow-controller-apply", action="store_true", help="Allow writes to mobility controllers")
    p_apply.add_argument("--out", default=None, help="Path for ntp_result.csv output")
    p_apply.add_argument("--only", default=None, help="Filter devices: comma-separated hostnames or IPs")
    p_apply.add_argument("--exclude", default=None, help="Exclude devices: comma-separated hostnames or IPs")
    p_apply.add_argument("--workers", type=int, default=6, help="Concurrent apply workers (1-12)")

    # Subcommand: verify
    p_verify = subparsers.add_parser(
        "verify",
        help="Verify fleet synchronization and skew",
    )
    _add_global_args(p_verify, is_subcommand=True)
    p_verify.add_argument("--wait", type=int, default=120, help="Seconds to wait before probing devices (default: 120)")
    p_verify.add_argument("--max-skew", type=float, default=120.0, help="Maximum allowed clock skew in seconds (default: 120.0)")
    p_verify.add_argument("--anchor", default="172.16.13.208", help="Hostname or IP of reference device clock (default: 172.16.13.208)")
    p_verify.add_argument("--reference-server", default="172.16.40.5", help="NTP reference server to validate anchor (default: 172.16.40.5)")
    p_verify.add_argument("--anchor-max-skew", type=float, default=120.0, help="Maximum allowed anchor deviation in seconds (default: 120.0)")
    p_verify.add_argument("--out", default=None, help="Path for verify.csv output")
    p_verify.add_argument("--only", default=None, help="Filter devices: comma-separated hostnames or IPs")
    p_verify.add_argument("--exclude", default=None, help="Exclude devices: comma-separated hostnames or IPs")
    p_verify.add_argument("--workers", type=int, default=6, help="Concurrent probe workers (1-12)")

    # Subcommand: report
    p_report = subparsers.add_parser(
        "report",
        help="Generate summary Markdown report",
    )
    _add_global_args(p_report, is_subcommand=True)
    p_report.add_argument("--dir", default=None, help="Directory containing run artifacts to report on")
    p_report.add_argument("--out", default=None, help="Path for report.md output")

    return parser


def load_filtered_inventory(args: argparse.Namespace) -> list[Device]:
    devices = inventory.load_switch_txt(args.config)
    excel_path = Path(args.excel)
    if excel_path.exists():
        excel_rows = inventory.load_xlsx(excel_path)
        devices = inventory.merge_inventory(devices, excel_rows, source=args.source)
    only = getattr(args, "only", None)
    exclude = getattr(args, "exclude", None)
    return inventory.filter_devices(devices, only=only, exclude=exclude)


def handle_preflight(args: argparse.Namespace) -> int:
    res = ntp.query(args.server, timeout=args.timeout)
    if getattr(args, "json", False):
        data = {
            "server": res.server,
            "reachable": res.reachable,
            "valid": res.valid,
            "stratum": res.stratum,
            "root_delay_s": res.root_delay_s,
            "root_dispersion_s": res.root_dispersion_s,
            "offset_s": res.offset_s,
            "mode": res.mode,
            "refid": res.refid,
            "error": res.error,
        }
        print(json.dumps(data, indent=2))
    else:
        print(f"Server: {res.server}")
        print(f"Reachable: {res.reachable}")
        print(f"Valid: {res.valid}")
        print(f"Stratum: {res.stratum}")
        print(f"Root Delay: {res.root_delay_s} s")
        print(f"Root Dispersion: {res.root_dispersion_s} s")
        if res.offset_s is not None:
            print(f"Offset: {res.offset_s} s")
        elif res.error:
            print(f"Offset: unavailable ({res.error})")
        else:
            print("Offset: unavailable")
        print(f"Mode: {res.mode}")
        print(f"RefID: {res.refid}")
        if res.error and res.offset_s is not None:
            print(f"Error: {res.error}")
        elif res.error and not res.valid:
            print(f"Error: {res.error}")
    return 0 if res.valid else 2


def handle_probe(args: argparse.Namespace) -> int:
    devices = load_filtered_inventory(args)
    if args.list_devices:
        for dev in devices:
            ctrl_tag = " [CONTROLLER]" if dev.is_controller else ""
            plat_val = dev.platform.value if hasattr(dev.platform, "value") else str(dev.platform)
            print(f"{dev.name} | {dev.ip} | {dev.model} | {plat_val}{ctrl_tag}")
        return 0

    run_opts = RunOptions(
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
        out_dir=args.out_dir,
        user=args.user,
    )
    probes = runner.probe_devices(devices, run_opts)

    # Write results using report.write_ntp_result_csv
    apply_results: list[ApplyResult] = []
    for p in probes:
        ar = ApplyResult(
            device=p.device,
            status=p.status,
            clock_before=p.clock,
            wrote=False,
            dry_run=True,
            error=p.error,
        )
        setattr(ar, "servers_before", p.servers)
        setattr(ar, "timezone_before", p.timezone)
        setattr(ar, "sync_status", p.sync_status)
        setattr(ar, "stratum", p.stratum)
        apply_results.append(ar)

    out_csv = args.out or str(Path(args.out_dir) / "ntp_result.csv")
    report.write_ntp_result_csv(apply_results, out_csv)

    failed = any(not p.ok for p in probes if not p.device.is_controller)
    return 1 if failed else 0


def build_plan_item(
    dev: Device,
    probe: Optional[ProbeResult],
    target_servers: list[str],
    tz_minutes: int,
    tz_name: Optional[str],
    cx_vrf: Optional[str],
    timesync_mode: str = "keep",
) -> PlanItem:
    servers_b = list(probe.servers) if probe and probe.servers else []
    entries_b = list(probe.entries) if probe and getattr(probe, "entries", None) else []
    tz_b = probe.timezone if probe else None
    rb = rollback_commands(dev.platform, servers_b, tz_b, entries_before=entries_b if entries_b else None)

    if dev.is_controller:
        return PlanItem(
            device=dev,
            status=Status.SKIP_CONTROLLER,
            reason="MANUAL",
            commands=[],
            needs_change=False,
            servers_before=servers_b,
            entries_before=entries_b,
            timezone_before=tz_b,
            rollback=[],
            timesync_mode=timesync_mode,
        )

    if probe is not None:
        if probe.status.value.startswith("SKIP"):
            return PlanItem(
                device=dev,
                status=probe.status,
                reason=probe.error or "Skipped per probe status",
                commands=[],
                needs_change=False,
                servers_before=servers_b,
                entries_before=entries_b,
                timezone_before=tz_b,
                rollback=rb,
                timesync_mode=timesync_mode,
            )
        if not probe.ok:
            return PlanItem(
                device=dev,
                status=probe.status,
                reason=probe.error or "Probe failed",
                commands=[],
                needs_change=False,
                servers_before=servers_b,
                entries_before=entries_b,
                timezone_before=tz_b,
                rollback=rb,
                timesync_mode=timesync_mode,
            )

        servers_match = set(probe.servers) == set(target_servers)
        tz_match = False
        if probe.timezone:
            tz_match = str(tz_minutes) in probe.timezone or (bool(tz_name) and tz_name.lower() in probe.timezone.lower())

        if dev.platform == Platform.ARUBOS_CX:
            sync_satisfied = (probe.sync_status or "").lower() == "synchronized"
            ts_mode_satisfied = True
        else:
            sync_satisfied = True
            if timesync_mode == "keep":
                ts_mode_satisfied = True
            elif timesync_mode == "ntp":
                ts_mode_satisfied = (getattr(probe, "sync_mode", None) or "").strip().upper() == "NTP"
            elif timesync_mode == "sntp":
                ts_mode_satisfied = (getattr(probe, "sync_mode", None) or "").strip().upper() == "SNTP"
            else:
                ts_mode_satisfied = True

        if servers_match and tz_match and sync_satisfied and ts_mode_satisfied:
            return PlanItem(
                device=dev,
                status=Status.OK_ALREADY,
                reason="Configuration and sync indicator satisfied",
                commands=[],
                needs_change=False,
                servers_before=servers_b,
                entries_before=entries_b,
                timezone_before=tz_b,
                rollback=rb,
                timesync_mode=timesync_mode,
            )

        cmds = config_commands(
            dev.platform,
            target_servers,
            tz_minutes,
            tz_name=tz_name,
            cx_vrf=cx_vrf,
            entries_before=entries_b,
            timesync_mode=timesync_mode,
        )
        return PlanItem(
            device=dev,
            status=Status.NEEDS_CHANGE,
            reason="Configuration or sync differs from target",
            commands=cmds,
            needs_change=True,
            servers_before=servers_b,
            entries_before=entries_b,
            timezone_before=tz_b,
            rollback=rb,
            timesync_mode=timesync_mode,
        )

    # Without probe data (offline plan fallback)
    cmds = config_commands(
        dev.platform,
        target_servers,
        tz_minutes,
        tz_name=tz_name,
        cx_vrf=cx_vrf,
        entries_before=[],
        timesync_mode=timesync_mode,
    )
    return PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="Target configuration planned",
        commands=cmds,
        needs_change=True,
        servers_before=[],
        entries_before=[],
        timezone_before=None,
        rollback=[],
        timesync_mode=timesync_mode,
    )


def handle_plan(args: argparse.Namespace) -> int:
    devices = load_filtered_inventory(args)
    target_servers = [s.strip() for s in args.server.split(",") if s.strip()]
    ts_mode = getattr(args, "timesync_mode", "keep")

    run_opts = RunOptions(
        workers=getattr(args, "workers", 6),
        timeout=int(args.timeout),
        retries=args.retries,
        out_dir=args.out_dir,
        user=args.user,
        timesync_mode=ts_mode,
    )
    probes = runner.probe_devices(devices, run_opts)
    probe_by_ip = {p.device.ip: p for p in probes}

    plan: list[PlanItem] = []
    for dev in devices:
        item = build_plan_item(
            dev,
            probe=probe_by_ip.get(dev.ip),
            target_servers=target_servers,
            tz_minutes=args.tz,
            tz_name=args.tz_name,
            cx_vrf=args.cx_vrf,
            timesync_mode=ts_mode,
        )
        plan.append(item)

    out_plan = args.out or str(Path(args.out_dir) / "plan.json")
    report.write_plan_json(plan, out_plan)
    print(f"Plan written to {out_plan} ({len(plan)} devices)")
    return 0


def handle_apply(args: argparse.Namespace) -> int:
    devices = load_filtered_inventory(args)
    target_servers = [s.strip() for s in args.server.split(",") if s.strip()]
    ts_mode = getattr(args, "timesync_mode", "keep")

    run_opts = RunOptions(
        workers=args.workers,
        timeout=int(args.timeout),
        retries=args.retries,
        servers=target_servers,
        tz_minutes=args.tz,
        tz_name=args.tz_name,
        cx_vrf=args.cx_vrf,
        apply=args.apply,
        allow_protected=args.allow_protected,
        include_controllers=args.include_controllers,
        allow_controller_apply=args.allow_controller_apply,
        out_dir=args.out_dir,
        user=args.user,
        dry_run=not args.apply,
        timesync_mode=ts_mode,
    )

    probes = runner.probe_devices(devices, run_opts)
    probe_by_ip = {p.device.ip: p for p in probes}

    plan: list[PlanItem] = []
    for dev in devices:
        item = build_plan_item(
            dev,
            probe=probe_by_ip.get(dev.ip),
            target_servers=target_servers,
            tz_minutes=args.tz,
            tz_name=args.tz_name,
            cx_vrf=args.cx_vrf,
            timesync_mode=ts_mode,
        )
        plan.append(item)

    if not args.apply:
        apply_results: list[ApplyResult] = []
        for item in plan:
            print(f"=== {item.device.name} ({item.device.ip}) [DRY RUN] ===")
            print(f"Time Sync Mode: {item.timesync_mode}")
            if item.status.value.startswith("SKIP"):
                print(f"Status: {item.status.value} - Reason: {item.reason}")
            elif item.status == Status.OK_ALREADY:
                print(f"Status: {item.status.value} - Reason: {item.reason}")
            else:
                for cmd in item.commands:
                    print(f"  {cmd}")
            dry_status = item.status if item.status != Status.OK else Status.NEEDS_CHANGE
            ar = ApplyResult(
                device=item.device,
                status=dry_status,
                commands=item.commands,
                wrote=False,
                dry_run=True,
            )
            setattr(ar, "servers_before", item.servers_before)
            setattr(ar, "timezone_before", item.timezone_before)
            apply_results.append(ar)
        out_csv = args.out or str(Path(args.out_dir) / "ntp_result.csv")
        report.write_ntp_result_csv(apply_results, out_csv)
        return 0

    results = runner.apply_devices(devices, plan, run_opts)
    out_csv = args.out or str(Path(args.out_dir) / "ntp_result.csv")
    report.write_ntp_result_csv(results, out_csv)

    failed = any(r.status in (Status.CONFIG_FAIL, Status.AUTH_FAIL, Status.CONNECT_FAIL) for r in results)
    return 1 if failed else 0


def handle_verify(args: argparse.Namespace) -> int:
    devices = load_filtered_inventory(args)
    anchor_arg = getattr(args, "anchor", "172.16.13.208")
    ref_server_arg = getattr(args, "reference_server", "172.16.40.5")
    anchor_max_skew = getattr(args, "anchor_max_skew", 120.0)
    max_skew = getattr(args, "max_skew", 120.0)

    if anchor_arg:
        if not any(d.name == anchor_arg or d.ip == anchor_arg for d in devices):
            full_devices = inventory.load_switch_txt(args.config)
            excel_path = Path(args.excel)
            if excel_path.exists():
                excel_rows = inventory.load_xlsx(excel_path)
                full_devices = inventory.merge_inventory(full_devices, excel_rows, source=args.source)
            anchor_match = [d for d in full_devices if d.name == anchor_arg or d.ip == anchor_arg]
            if anchor_match:
                devices.extend(anchor_match)

    run_opts = RunOptions(
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
        out_dir=args.out_dir,
        user=args.user,
    )
    kwargs = {}
    if anchor_arg is not None:
        kwargs["anchor"] = anchor_arg
    if ref_server_arg is not None:
        kwargs["reference_server"] = ref_server_arg
    if anchor_max_skew is not None:
        kwargs["anchor_max_skew"] = anchor_max_skew

    try:
        verdicts = verify.verify_devices(
            devices,
            run_opts,
            wait=args.wait,
            max_skew=max_skew,
            **kwargs,
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    poll_intervals = []
    for d in devices:
        pi = getattr(d, "poll_interval", None)
        if pi is not None:
            poll_intervals.append(pi)
    for v in verdicts:
        pi = getattr(v.device, "poll_interval", None)
        if pi is not None:
            poll_intervals.append(pi)

    if poll_intervals:
        max_poll = max(poll_intervals)
        if args.wait < max_poll:
            print(
                f"WARNING: Requested wait ({args.wait}s) is shorter than poll interval ({max_poll}s) of probed devices. "
                f"Devices need up to {max_poll}s before first poll. Wait at least {max_poll}s."
            )
            for v in verdicts:
                pi = getattr(v.device, "poll_interval", None)
                if pi is not None and args.wait < pi and v.verdict != SyncVerdict.SYNC:
                    v.detail = f"Device poll interval not elapsed ({args.wait}s < {pi}s; wait at least {pi}s); {v.detail}".strip("; ")

    out_csv = args.out or str(Path(args.out_dir) / "verify.csv")
    report.write_verify_csv(verdicts, out_csv)

    anchor_v = next(
        (v for v in verdicts if v.device.name == anchor_arg or v.device.ip == anchor_arg),
        None,
    )
    anchor_name = anchor_v.device.name if anchor_v else anchor_arg
    anchor_ip = anchor_v.device.ip if anchor_v else anchor_arg
    anchor_clock = getattr(anchor_v, "clock", None) or "unknown"
    ref_strat = getattr(anchor_v, "reference_stratum", None)
    ref_id = getattr(anchor_v, "reference_refid", None)
    anc_dev = getattr(anchor_v, "anchor_deviation", None)
    skews = [abs(v.skew_s) for v in verdicts if v.skew_s is not None]
    largest_dev = max(skews) if skews else 0.0

    meta = {
        "reference_server": ref_server_arg,
        "reference_stratum": ref_strat,
        "reference_refid": ref_id,
        "anchor": f"{anchor_name} ({anchor_ip})",
        "anchor_tolerance": f"{anchor_max_skew:.1f}s" if isinstance(anchor_max_skew, float) else f"{anchor_max_skew}s",
        "anchor_deviation": f"{anc_dev:.4f}s" if anc_dev is not None else "N/A",
        "threshold": f"{max_skew:.1f}s" if isinstance(max_skew, float) else f"{max_skew}s",
        "largest_deviation": f"{largest_dev:.4f}s",
    }
    meta_path = Path(args.out_dir) / "verify_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if ref_server_arg:
        strat_info = f" (stratum {ref_strat}, refid {ref_id})" if ref_strat is not None else ""
        print(f"Reference Server: {ref_server_arg}{strat_info}")
    if anchor_arg:
        print(f"Anchor: {anchor_name} ({anchor_ip})")
        print(f"Anchor Clock: {anchor_clock}")
        print(f"Anchor Tolerance: {anchor_max_skew}s")
        if anc_dev is not None:
            print(f"Anchor Deviation vs Reference Server: {anc_dev:.4f}s")
        if anchor_v and anchor_v.offset_s is not None:
            ws_offset = -anchor_v.offset_s
            print(f"Workstation Clock vs Anchor: {ws_offset:+.4f}s offset (workstation clock is not trusted reference)")
        else:
            print("Workstation Clock vs Anchor: unavailable (workstation clock is not trusted reference)")

    print(f"Fleet Poll Interval: 720s (threshold: {max_skew}s)")

    all_pass = True
    for v in verdicts:
        plat_val = v.device.platform.value if hasattr(v.device.platform, "value") else str(v.device.platform)
        verd_val = v.verdict.value if hasattr(v.verdict, "value") else str(v.verdict)
        if anchor_arg:
            dev_str = f"deviation: {v.skew_s:.4f}s (threshold: {max_skew}s)" if v.skew_s is not None else "deviation: unknown"
            print(f"{v.device.name} ({v.device.ip}) [{plat_val}]: {verd_val} - {dev_str} - {v.detail}")
        else:
            print(f"{v.device.name} ({v.device.ip}) [{plat_val}]: {verd_val} - {v.detail}")
        if v.verdict != SyncVerdict.SYNC:
            all_pass = False

    ref_desc = f"anchor {anchor_arg}" if anchor_arg else "median"
    fleet_status = "PASS" if all_pass else "FAIL"
    print(f"Fleet Synchronization: {fleet_status} (reference: {ref_desc})")
    return 0 if all_pass else 1


def handle_report(args: argparse.Namespace) -> int:
    out_dir = Path(getattr(args, "dir", None) or getattr(args, "out_dir", "out"))
    out_report = args.out or str(out_dir / "report.md")
    report.generate_report(out_dir, out_report)
    print(out_report)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code != 0 else 0

    handlers = {
        "preflight": handle_preflight,
        "probe": handle_probe,
        "plan": handle_plan,
        "apply": handle_apply,
        "verify": handle_verify,
        "report": handle_report,
    }

    handler = handlers.get(args.subcommand)
    if not handler:
        parser.print_help()
        return 2

    return handler(args)
