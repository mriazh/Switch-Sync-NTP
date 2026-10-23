import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading
import time
from typing import Any, Optional

from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from switch_sync.inventory import username_candidates
from switch_sync.models import (
    ApplyResult,
    Device,
    PlanItem,
    Platform,
    ProbeResult,
    RunOptions,
    Status,
)
from switch_sync.parsers import (
    is_invalid_input,
    parse_running_config,
    parse_show_clock,
    parse_show_ntp_status,
    parse_show_sntp,
    parse_show_system,
    parse_show_time,
    parse_show_version,
    strip_ansi,
)
from switch_sync.platforms import (
    backup_commands,
    clock_command,
    config_commands,
    platform_from_firmware,
    probe_commands,
)
from switch_sync import session
from switch_sync.session import ConfigRejected, SessionError

REJECT_PATTERNS = (
    "incomplete input",
    "invalid input",
    "% invalid",
    "% command incomplete",
    "error",
    "syntax error",
    "already configured",
)


def _patched_check_rejected(self, output: str) -> None:
    if not output:
        return
    lower = output.lower()
    for pattern in REJECT_PATTERNS:
        if pattern in lower:
            safe_out = output.strip()
            if getattr(self, "password", None) and self.password in safe_out:
                safe_out = safe_out.replace(self.password, "******")
            cur_line = getattr(self, "_current_line", "")
            line_str = f"on line '{cur_line}': " if cur_line else ""
            raise ConfigRejected(f"{self.device.ip}: Config rejected {line_str}{safe_out}".strip())


def _patched_send_lines(self, lines: list[str], delay: float = 0.35) -> str:
    collected: list[str] = []
    for line in lines:
        self._current_line = line
        out = self.send(line)
        collected.append(out)
        self._check_rejected(out)
        if delay > 0:
            time.sleep(delay)
    return "\n".join(collected)


session.DeviceSession._check_rejected = _patched_check_rejected
session.DeviceSession.send_lines = _patched_send_lines

PROTECTED_IPS: frozenset[str] = frozenset({
    "172.16.13.1",
    "172.21.255.1",
    "172.21.255.2",
    "172.21.255.6",
})


class CircuitBreaker:
    def __init__(self, threshold: int = 5) -> None:
        self.threshold = threshold
        self._failures = 0
        self._lock = threading.Lock()

    def record(self, ok: bool) -> bool:
        with self._lock:
            if ok:
                self._failures = 0
                return True
            self._failures += 1
            return self._failures < self.threshold

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._failures >= self.threshold


def run_parallel(
    items: Sequence[Any],
    fn: Callable[[Any], Any],
    workers: int = 6,
    on_result: Optional[Callable[[Any], None]] = None,
) -> list[Any]:
    if not items:
        return []
    worker_count = max(1, min(12, int(workers)))
    results: list[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_idx = {
            executor.submit(fn, item): idx for idx, item in enumerate(items)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            res = future.result()
            results[idx] = res
            if on_result:
                on_result(res)
    return results


def _probe_single_device(
    device: Device,
    options: RunOptions,
    breaker: CircuitBreaker,
) -> ProbeResult:
    if device.is_controller and not options.include_controllers:
        return ProbeResult(
            device=device,
            ok=False,
            status=Status.SKIP_CONTROLLER,
            error="Controller excluded",
        )

    if device.platform == Platform.UNKNOWN:
        return ProbeResult(
            device=device,
            ok=False,
            status=Status.SKIP_UNKNOWN_PLATFORM,
            error="Unknown platform",
        )

    if breaker.tripped:
        return ProbeResult(
            device=device,
            ok=False,
            status=Status.UNREACHABLE,
            error="Circuit breaker tripped",
        )

    candidates = username_candidates(device, options.user)[:3]
    session_obj = None
    last_err: Optional[Exception] = None
    auth_failed = False
    unreachable = False

    for user in candidates:
        if breaker.tripped:
            return ProbeResult(
                device=device,
                ok=False,
                status=Status.UNREACHABLE,
                error="Circuit breaker tripped",
            )
        try:
            session_obj = session.connect(
                device,
                username=user,
                password=device.password,
                timeout=options.timeout,
                retries=options.retries,
            )
            break
        except NetmikoAuthenticationException as exc:
            last_err = exc
            auth_failed = True
        except (NetmikoTimeoutException, SessionError, TimeoutError, OSError) as exc:
            last_err = exc
            unreachable = True
            break
        except Exception as exc:
            last_err = exc
            unreachable = True
            break

    if session_obj is None:
        breaker.record(False)
        status = Status.AUTH_FAIL if auth_failed and not unreachable else Status.UNREACHABLE
        return ProbeResult(
            device=device,
            ok=False,
            status=status,
            error=str(last_err) if last_err else "Connection failed",
        )

    breaker.record(True)
    probe_res = ProbeResult(device=device, ok=True, status=Status.OK)

    try:
        cmds = list(probe_commands(device.platform))
        clk_cmd = clock_command(device.platform)
        if clk_cmd not in cmds:
            cmds.append(clk_cmd)

        raw_map: dict[str, str] = {}
        for cmd in cmds:
            raw_map[cmd] = session_obj.send(cmd)
        probe_res.raw = raw_map

        # Save copy to out/raw/<ip>-probe.txt
        out_dir = Path(options.out_dir) / "raw"
        out_dir.mkdir(parents=True, exist_ok=True)
        probe_path = out_dir / f"{device.ip}-probe.txt"
        file_lines = []
        for cmd, out in raw_map.items():
            file_lines.append(f"=== {cmd} ===\n{strip_ansi(out)}\n")
        probe_path.write_text("\n".join(file_lines), encoding="utf-8")

        # Refine platform
        ver_out = raw_map.get("show version", "")
        ver_data = parse_show_version(ver_out)
        probe_res.firmware = ver_data.get("firmware")
        plat_detected = platform_from_firmware(probe_res.firmware) if probe_res.firmware else None
        if plat_detected and plat_detected != Platform.UNKNOWN:
            probe_res.platform_detected = plat_detected

        # Parse platform-specific clocks, timezones, and servers
        if device.platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
            time_out = raw_map.get("show time", "")
            probe_res.clock = parse_show_time(time_out)

            sys_data = parse_show_system(raw_map.get("show system", ""))
            probe_res.timezone = sys_data.get("timezone")

            sntp_data = parse_show_sntp(raw_map.get("show sntp", ""))
            probe_res.sntp_supported = sntp_data.get("supported")
            probe_res.servers = sntp_data.get("servers", [])
            probe_res.entries = sntp_data.get("entries", [])
            probe_res.poll_interval = sntp_data.get("poll_interval")
            probe_res.sync_mode = sntp_data.get("sync_mode")
            if probe_res.poll_interval is not None:
                device.poll_interval = probe_res.poll_interval
            if probe_res.sync_mode is not None:
                device.sync_mode = probe_res.sync_mode

            if not probe_res.servers or not probe_res.entries:
                rc_data = parse_running_config(raw_map.get("show running-config | include sntp", ""))
                if not probe_res.servers:
                    probe_res.servers = rc_data.get("servers", [])
                if not probe_res.entries:
                    probe_res.entries = rc_data.get("entries", [])
        elif device.platform == Platform.ARUBOS_CX:
            clk_data = parse_show_clock(raw_map.get("show clock", ""))
            probe_res.clock = clk_data.get("clock")
            probe_res.timezone = clk_data.get("timezone")

            ntp_data = parse_show_ntp_status(raw_map.get("show ntp status", ""))
            probe_res.sync_status = ntp_data.get("sync_status")
            probe_res.stratum = ntp_data.get("stratum")
            if not probe_res.clock:
                probe_res.clock = ntp_data.get("clock")

            rc_data = parse_running_config(raw_map.get("show running-config | include ntp", ""))
            probe_res.servers = rc_data.get("servers", []) or ntp_data.get("servers", [])

        # Privilege gate
        rc_out = raw_map.get("show running-config | include sntp", "") or raw_map.get("show running-config | include ntp", "")
        sntp_out = raw_map.get("show sntp", "")
        sntp_works = bool(sntp_out.strip() and not is_invalid_input(sntp_out))
        rc_failed = not rc_out.strip() or is_invalid_input(rc_out)

        if sntp_works and rc_failed:
            probe_res.privilege_ok = False
            probe_res.status = Status.SKIP_PRIVILEGE
            probe_res.ok = False
        else:
            probe_res.privilege_ok = True
            probe_res.status = Status.OK
            probe_res.ok = True

    finally:
        session_obj.close()

    return probe_res


def probe_devices(devices: list[Device], options: RunOptions) -> list[ProbeResult]:
    breaker = CircuitBreaker(threshold=5)
    return run_parallel(
        devices,
        lambda d: _probe_single_device(d, options, breaker),
        workers=options.workers,
    )


def _apply_single_item(
    item: PlanItem,
    options: RunOptions,
) -> ApplyResult:
    dev = item.device

    if item.status == Status.OK_ALREADY or item.status.value.startswith("SKIP"):
        return ApplyResult(
            device=dev,
            status=item.status,
            commands=list(item.commands),
            wrote=False,
            dry_run=not options.apply,
        )

    if (dev.protected or dev.ip in PROTECTED_IPS) and not options.allow_protected:
        return ApplyResult(
            device=dev,
            status=Status.SKIP_PROTECTED,
            commands=list(item.commands),
            wrote=False,
            dry_run=not options.apply,
        )

    if dev.is_controller and not options.allow_controller_apply:
        return ApplyResult(
            device=dev,
            status=Status.SKIP_CONTROLLER,
            commands=list(item.commands),
            wrote=False,
            dry_run=not options.apply,
        )

    mode_to_use = getattr(options, "timesync_mode", getattr(item, "timesync_mode", "keep"))
    cmds = item.commands or config_commands(
        dev.platform,
        options.servers,
        options.tz_minutes,
        tz_name=options.tz_name,
        cx_vrf=options.cx_vrf,
        entries_before=getattr(item, "entries_before", []),
        timesync_mode=mode_to_use,
    )

    if not options.apply:
        dry_status = item.status if item.status not in (Status.OK, Status.NEEDS_CHANGE) else Status.NEEDS_CHANGE
        if item.status == Status.OK_ALREADY:
            dry_status = Status.OK_ALREADY
        res = ApplyResult(
            device=dev,
            status=dry_status,
            commands=cmds,
            wrote=False,
            dry_run=True,
        )
        setattr(res, "reason", getattr(item, "reason", ""))
        return res

    candidates = username_candidates(dev, options.user)[:3]
    session_obj = None
    last_err: Optional[Exception] = None

    for user in candidates:
        try:
            session_obj = session.connect(
                dev,
                username=user,
                password=dev.password,
                timeout=options.timeout,
                retries=options.retries,
            )
            break
        except Exception as exc:
            last_err = exc

    if session_obj is None:
        status = Status.AUTH_FAIL if isinstance(last_err, NetmikoAuthenticationException) else Status.CONNECT_FAIL
        return ApplyResult(
            device=dev,
            status=status,
            commands=cmds,
            wrote=False,
            error=str(last_err) if last_err else "Connection failed",
        )

    clock_before: Optional[str] = None
    clock_after: Optional[str] = None
    output_text = ""
    error_msg: Optional[str] = None
    status = Status.OK
    wrote = False

    try:
        # Mandatory backup before write per section 7.1
        backup_dir = Path(options.out_dir) / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        running_backup_file = backup_dir / f"{dev.name}-running.txt"

        bk_cmds = backup_commands(dev.platform)
        for cmd, suffix in bk_cmds:
            bk_out = session_obj.send(cmd)
            target_file = backup_dir / f"{dev.name}-{suffix}.txt"
            target_file.write_text(strip_ansi(bk_out), encoding="utf-8")

            if suffix == "running":
                cleaned_bk = bk_out.strip()
                if not cleaned_bk or is_invalid_input(bk_out) or any(
                    err in cleaned_bk.lower() for err in ("privilege", "denied", "authorization failed", "not authorized")
                ):
                    return ApplyResult(
                        device=dev,
                        status=Status.SKIP_PRIVILEGE,
                        commands=cmds,
                        wrote=False,
                        dry_run=False,
                        error=f"Running-config backup empty or rejected: {cleaned_bk or 'empty output'}",
                    )

        if not running_backup_file.exists() or running_backup_file.stat().st_size == 0:
            return ApplyResult(
                device=dev,
                status=Status.SKIP_PRIVILEGE,
                commands=cmds,
                wrote=False,
                dry_run=False,
                error="Running-config backup missing or empty",
            )

        clk_cmd = clock_command(dev.platform)
        clock_before = session_obj.send(clk_cmd)

        try:
            output_text = session_obj.send_lines(cmds, delay=0.35)
            if output_text:
                lower_out = output_text.lower()
                for pattern in REJECT_PATTERNS:
                    if pattern in lower_out:
                        failed_line = ""
                        for c in cmds:
                            if c.lower() in lower_out:
                                failed_line = c
                                break
                        if not failed_line:
                            failed_line = cmds[0] if cmds else ""
                        safe_out = output_text.strip()
                        raise ConfigRejected(f"{dev.ip}: Config rejected on line '{failed_line}': {safe_out}")
            wrote = True
        except ConfigRejected as exc:
            status = Status.CONFIG_FAIL
            error_msg = str(exc)
            wrote = False

        clock_after = session_obj.send(clk_cmd)

        out_dir = Path(options.out_dir) / "raw"
        out_dir.mkdir(parents=True, exist_ok=True)
        apply_path = out_dir / f"{dev.ip}-apply.txt"
        apply_path.write_text(strip_ansi(output_text), encoding="utf-8")

    finally:
        session_obj.close()

    res = ApplyResult(
        device=dev,
        status=status,
        commands=cmds,
        clock_before=clock_before,
        clock_after=clock_after,
        wrote=wrote,
        dry_run=False,
        output=output_text,
        error=error_msg,
    )
    if error_msg:
        setattr(res, "reason", error_msg)
        setattr(res, "detail", error_msg)
    return res


def apply_devices(
    devices: list[Device],
    plan: list[PlanItem],
    options: RunOptions,
) -> list[ApplyResult]:
    return run_parallel(
        plan,
        lambda item: _apply_single_item(item, options),
        workers=options.workers,
    )
