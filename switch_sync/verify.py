from datetime import datetime, timezone
import re
import statistics
import time
from typing import Optional

from switch_sync import ntp, runner
from switch_sync.models import (
    Device,
    NtpResult,
    Platform,
    ProbeResult,
    RunOptions,
    Status,
    SyncVerdict,
    Verdict,
)

MONTHS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_clock(clock_str: Optional[str]) -> Optional[float]:
    if not clock_str:
        return None
    m = re.search(
        r"([A-Za-z]{3})\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})(?:\s+([A-Za-z0-9/+-]+))?\s+(\d{4})",
        clock_str.strip(),
    )
    if not m:
        return None
    mon_name = m.group(2).lower()
    if mon_name not in MONTHS:
        return None
    month = MONTHS[mon_name]
    day = int(m.group(3))
    hour = int(m.group(4))
    minute = int(m.group(5))
    second = int(m.group(6))
    year = int(m.group(8))
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.timestamp()


def classify(
    probe: ProbeResult,
    target_servers: list[str],
    max_skew: float,
    device_clocks: dict[str, float],
    reference_clock: Optional[float] = None,
) -> Verdict:
    if not probe.clock and not probe.sync_status and not probe.servers:
        return Verdict(
            device=probe.device,
            verdict=SyncVerdict.UNREACHABLE,
            stratum=probe.stratum,
            servers=list(probe.servers),
            detail="Unreachable: no clock, sync status, or servers detected",
        )

    if not probe.servers:
        return Verdict(
            device=probe.device,
            verdict=SyncVerdict.NO_SERVER,
            stratum=probe.stratum,
            servers=[],
            detail="No NTP server configured",
        )

    fleet_ref: Optional[float] = reference_clock
    if fleet_ref is None and device_clocks:
        fleet_ref = float(statistics.median(device_clocks.values()))

    dev_clock = device_clocks.get(probe.device.ip)
    if dev_clock is None and probe.clock:
        dev_clock = parse_clock(probe.clock)

    clock_skew: Optional[float] = None
    if dev_clock is not None and fleet_ref is not None:
        clock_skew = abs(dev_clock - fleet_ref)

    if probe.device.platform == Platform.ARUBOS_CX:
        is_sync = (probe.sync_status or "").lower() == "synchronized"
        valid_strat = probe.stratum is not None and 1 <= probe.stratum <= 15
        skew_ok = clock_skew is None or clock_skew <= max_skew

        if not is_sync:
            return Verdict(
                device=probe.device,
                verdict=SyncVerdict.UNSYNC,
                stratum=probe.stratum,
                servers=list(probe.servers),
                detail=f"CX status: {probe.sync_status or 'unsynchronized'}",
            )
        if not valid_strat:
            return Verdict(
                device=probe.device,
                verdict=SyncVerdict.UNSYNC,
                stratum=probe.stratum,
                servers=list(probe.servers),
                detail=f"CX invalid stratum: {probe.stratum}",
            )
        if not skew_ok:
            return Verdict(
                device=probe.device,
                verdict=SyncVerdict.UNSYNC,
                stratum=probe.stratum,
                servers=list(probe.servers),
                detail=f"CX clock skew {clock_skew:.2f}s exceeds max {max_skew}s",
            )
        return Verdict(
            device=probe.device,
            verdict=SyncVerdict.SYNC,
            stratum=probe.stratum,
            servers=list(probe.servers),
            detail="Synchronized",
        )

    # ArubaOS-S / PROCURVE_LEGACY
    servers_match = set(probe.servers) == set(target_servers) if target_servers else True
    if not servers_match:
        return Verdict(
            device=probe.device,
            verdict=SyncVerdict.UNSYNC,
            stratum=None,
            servers=list(probe.servers),
            detail=f"Configured servers {probe.servers} differ from target {target_servers}",
        )

    if clock_skew is not None and clock_skew > max_skew:
        return Verdict(
            device=probe.device,
            verdict=SyncVerdict.UNSYNC,
            stratum=None,
            servers=list(probe.servers),
            detail=f"Clock skew {clock_skew:.2f}s exceeds max {max_skew}s",
        )

    return Verdict(
        device=probe.device,
        verdict=SyncVerdict.SYNC,
        stratum=None,
        servers=list(probe.servers),
        detail="Synchronized",
    )


def skew_matrix(verdicts: list[Verdict]) -> dict[str, dict[str, float]]:
    matrix: dict[str, dict[str, float]] = {}
    valid = [v for v in verdicts if v.offset_s is not None]
    for v1 in valid:
        ip1 = v1.device.ip
        matrix[ip1] = {}
        for v2 in valid:
            if v1.device.ip != v2.device.ip:
                matrix[ip1][v2.device.ip] = round(abs(v1.offset_s - v2.offset_s), 4)
    return matrix


def verify_devices(
    devices: list[Device],
    options: RunOptions,
    wait: int = 120,
    max_skew: float = 120.0,
    anchor: Optional[str] = "172.16.13.208",
    reference_server: Optional[str] = "172.16.40.5",
    anchor_max_skew: float = 120.0,
) -> list[Verdict]:
    anchor_dev: Optional[Device] = None
    if anchor is not None:
        anchor_clean = anchor.strip()
        for d in devices:
            if d.name == anchor_clean or d.ip == anchor_clean:
                anchor_dev = d
                break
        if anchor_dev is None:
            for d in devices:
                if d.name.lower() == anchor_clean.lower():
                    anchor_dev = d
                    break
        if anchor_dev is None:
            raise ValueError(f"Anchor '{anchor}' not found in inventory")

    if wait > 0:
        time.sleep(wait)

    probes = runner.probe_devices(devices, options)

    anchor_epoch: Optional[float] = None
    ref_res: Optional[NtpResult] = None
    anchor_deviation: Optional[float] = None

    if anchor_dev is not None:
        anchor_probe: Optional[ProbeResult] = None
        for p in probes:
            if p.device.ip == anchor_dev.ip:
                anchor_probe = p
                break
        if anchor_probe is None or not anchor_probe.ok or anchor_probe.status != Status.OK:
            err = (
                anchor_probe.error
                if (anchor_probe and anchor_probe.error)
                else (anchor_probe.status.value if anchor_probe else "device probe missing")
            )
            raise RuntimeError(f"Anchor '{anchor}' ({anchor_dev.ip}) is unreachable: {err}")

        anchor_epoch = parse_clock(anchor_probe.clock) if anchor_probe.clock else None
        if anchor_epoch is None:
            raise RuntimeError(f"Anchor '{anchor}' ({anchor_dev.ip}) has no readable clock")

        if reference_server is not None:
            ref_res = ntp.query(reference_server)
            if not ref_res.reachable or ref_res.reference_time is None:
                err = ref_res.error or "no reference timestamp returned"
                raise RuntimeError(f"Reference server '{reference_server}' unreachable or invalid: {err}")

            anchor_deviation = abs(anchor_epoch - ref_res.reference_time)
            if anchor_deviation > anchor_max_skew:
                raise RuntimeError(
                    f"Anchor '{anchor_dev.name}' ({anchor_dev.ip}) clock deviates from reference server "
                    f"'{reference_server}' by {anchor_deviation:.4f}s (tolerance {anchor_max_skew}s); "
                    f"refusing to grade fleet"
                )

    device_clocks: dict[str, float] = {}
    for p in probes:
        if p.clock:
            t = parse_clock(p.clock)
            if t is not None:
                device_clocks[p.device.ip] = t

    host_clock = time.time()
    verdicts: list[Verdict] = []
    ref_clock = anchor_epoch if anchor is not None else None
    for p in probes:
        v = classify(p, options.servers, max_skew, device_clocks, reference_clock=ref_clock)
        v.clock = p.clock
        if reference_server is not None:
            v.reference_server = reference_server
            if ref_res is not None:
                v.reference_stratum = ref_res.stratum
                v.reference_refid = ref_res.refid
        if anchor_max_skew is not None:
            v.anchor_max_skew = anchor_max_skew

        if p.device.ip in device_clocks:
            dev_epoch = device_clocks[p.device.ip]
            v.offset_s = round(dev_epoch - host_clock, 4)
            if anchor is not None and anchor_epoch is not None and anchor_dev is not None:
                if p.device.ip == anchor_dev.ip:
                    v.skew_s = 0.0
                    if anchor_deviation is not None:
                        v.anchor_deviation = anchor_deviation
                        ref_info = (
                            f" (stratum {ref_res.stratum}, refid {ref_res.refid})"
                            if ref_res and ref_res.stratum is not None
                            else ""
                        )
                        v.detail = (
                            f"Anchor verified against {reference_server}{ref_info}: "
                            f"deviation {anchor_deviation:.4f}s (tolerance {anchor_max_skew:.1f}s)"
                        )
                else:
                    v.skew_s = round(abs(dev_epoch - anchor_epoch), 4)
            else:
                other_diffs = [
                    abs(dev_epoch - other_epoch)
                    for other_ip, other_epoch in device_clocks.items()
                    if other_ip != p.device.ip
                ]
                v.skew_s = round(max(other_diffs), 4) if other_diffs else 0.0
        verdicts.append(v)

    return verdicts
