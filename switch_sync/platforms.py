from typing import Optional
from switch_sync.models import Platform

MODEL_PLATFORM: dict[str, Platform] = {
    "aruba2930": Platform.ARUBOS_S,
    "aruba2530": Platform.ARUBOS_S,
    "aruba2530a": Platform.ARUBOS_S,
    "aruba5400": Platform.ARUBOS_S,
    "procurve2530": Platform.ARUBOS_S,
    "procurve2620": Platform.ARUBOS_S,
    "procurve2520": Platform.PROCURVE_LEGACY,
    "procurve2610": Platform.PROCURVE_LEGACY,
    "aruba8400": Platform.ARUBOS_CX,
    "aruba7200": Platform.ARUBOS_MOBILITY,
}


def platform_from_model(model: str) -> Platform:
    if not model:
        return Platform.UNKNOWN
    cleaned = model.strip().lower()
    return MODEL_PLATFORM.get(cleaned, Platform.UNKNOWN)


def platform_from_firmware(firmware: str) -> Platform:
    if not firmware:
        return Platform.UNKNOWN
    fw = firmware.strip().upper()
    if fw.startswith("XL."):
        return Platform.ARUBOS_CX
    if any(fw.startswith(p) for p in ("KB.", "WC.", "YA.", "YB.", "RA.")):
        return Platform.ARUBOS_S
    if any(fw.startswith(p) for p in ("J.", "Y.")):
        return Platform.PROCURVE_LEGACY
    return Platform.UNKNOWN


def resolve_platform(model: str, firmware: Optional[str]) -> Platform:
    plat = platform_from_model(model)
    if plat != Platform.UNKNOWN:
        return plat
    if firmware:
        return platform_from_firmware(firmware)
    return Platform.UNKNOWN


def netmiko_device_type(platform: Platform) -> str:
    if platform in (Platform.ARUBOS_S, Platform.ARUBOS_MOBILITY):
        return "aruba_os"
    if platform == Platform.PROCURVE_LEGACY:
        return "aruba_procurve"
    if platform == Platform.ARUBOS_CX:
        return "aruba_aoscx"
    return "aruba_os"


def probe_commands(platform: Platform) -> list[str]:
    if platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
        return [
            "show version",
            "show sntp",
            "show system",
            "show running-config | include sntp",
        ]
    if platform == Platform.ARUBOS_CX:
        return [
            "show version",
            "show ntp status",
            "show clock",
            "show running-config | include ntp",
        ]
    if platform == Platform.ARUBOS_MOBILITY:
        return ["show version", "show clock"]
    return ["show version"]


def clock_command(platform: Platform) -> str:
    if platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
        return "show time"
    return "show clock"


def timezone_command(platform: Platform) -> str:
    if platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
        return "show system"
    return "show clock"


def should_emit_timesync(sync_mode: Optional[str], has_timesync_in_config: bool) -> bool:
    if sync_mode is not None:
        if sync_mode.strip() != "SNTP":
            return True
        return has_timesync_in_config
    return has_timesync_in_config


def config_commands(
    platform: Platform,
    servers: list[str],
    tz_minutes: int,
    tz_name: Optional[str] = "Asia/Jakarta",
    cx_vrf: Optional[str] = None,
    timesync_supported: bool = False,
    entries_before: Optional[list[tuple[int, str]]] = None,
    timesync_mode: Optional[str] = None,
) -> list[str]:
    if platform == Platform.ARUBOS_MOBILITY:
        return []

    if platform == Platform.ARUBOS_CX:
        vrf_suffix = f" vrf {cx_vrf}" if cx_vrf and cx_vrf.lower() != "default" else ""
        tz = tz_name or "Asia/Jakarta"
        lines = ["configure terminal"]
        for s in servers:
            lines.append(f"no ntp server {s}{vrf_suffix}")
        for s in servers:
            lines.append(f"ntp server {s} iburst{vrf_suffix}")
        lines.append("ntp enable")
        lines.append(f"clock timezone {tz}")
        lines.append("end")
        lines.append("write memory")
        return lines

    lines = ["configure"]
    if timesync_mode == "sntp" or (timesync_mode is None and timesync_supported):
        lines.append("timesync sntp")
    elif timesync_mode == "ntp":
        lines.append("timesync ntp")
    lines.append("sntp unicast")
    if entries_before:
        for prio, srv in entries_before:
            lines.append(f"no sntp server priority {prio} {srv}")
    for idx, s in enumerate(servers[:3], 1):
        lines.append(f"sntp server priority {idx} {s}")
    lines.append(f"time timezone {tz_minutes}")
    lines.append("write memory")
    return lines


def backup_commands(platform: Platform) -> list[tuple[str, str]]:
    if platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
        return [
            ("show config", "startup"),
            ("show running-config", "running"),
        ]
    if platform == Platform.ARUBOS_CX:
        return [
            ("show startup-config", "startup"),
            ("show running-config", "running"),
        ]
    return []


def rollback_commands(
    platform: Platform,
    servers_before: list[str],
    timezone_before: Optional[str] = None,
    entries_before: Optional[list[tuple[int, str]]] = None,
) -> list[str]:
    if platform == Platform.ARUBOS_MOBILITY:
        return []

    old_server = servers_before[0] if servers_before else None

    if platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
        if entries_before is not None:
            lines = [f"no sntp server priority {p} {s}" for p, s in entries_before]
        else:
            lines = ["no sntp server priority 1"]
        if old_server:
            lines.append(f"sntp server priority 1 {old_server}")
        if timezone_before:
            lines.append(f"time timezone {timezone_before}")
        lines.append("write memory")
        return lines

    if platform == Platform.ARUBOS_CX:
        lines = ["configure terminal"]
        if old_server:
            lines.append(f"no ntp server {old_server}")
            lines.append(f"ntp server {old_server} iburst")
        if timezone_before:
            lines.append(f"clock timezone {timezone_before}")
        lines.append("end")
        lines.append("write memory")
        return lines

    return []
