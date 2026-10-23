from pathlib import Path
from typing import Optional
import openpyxl
from switch_sync.models import Device, Platform
from switch_sync.platforms import platform_from_model, resolve_platform

PROTECTED_IPS = {"172.16.13.1", "172.21.255.1", "172.21.255.2", "172.21.255.6"}


def load_switch_txt(path: str | Path) -> list[Device]:
    p = Path(path)
    devices: list[Device] = []
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [field.strip() for field in line.split("|")]
            if len(parts) < 4:
                continue
            name = parts[0]
            ip = parts[1]
            model = parts[2]
            password = parts[3]
            username = parts[4] if len(parts) >= 5 and parts[4] else None

            platform = platform_from_model(model)
            is_controller = platform == Platform.ARUBOS_MOBILITY or model.strip().lower() == "aruba7200"
            protected = ip in PROTECTED_IPS

            devices.append(
                Device(
                    name=name,
                    ip=ip,
                    model=model,
                    platform=platform,
                    password=password,
                    username=username,
                    is_controller=is_controller,
                    protected=protected,
                )
            )
    return devices


def load_xlsx(path: str | Path) -> list[dict]:
    p = Path(path)
    wb = openpyxl.load_workbook(p, read_only=True)
    ws = wb["Switch Data"]
    rows: list[dict] = []
    # Sheet layout: header row 1, data A2:V162
    # Columns (0-indexed in values_only):
    # 1: Label (B), 2: IP (C), 3: Hostname (D), 4: Member (E),
    # 6: Model (G), 7: Firmware (H), 15: VLAN Management (P), 19: Status (T)
    for row in ws.iter_rows(min_row=2, max_row=162, values_only=True):
        if not any(row):
            continue
        label = str(row[1]).strip() if row[1] is not None else None
        ip = str(row[2]).strip() if row[2] is not None else None
        hostname = str(row[3]).strip() if row[3] is not None else None
        member = str(row[4]).strip() if row[4] is not None else None
        model = str(row[6]).strip() if row[6] is not None else None
        firmware = str(row[7]).strip() if row[7] is not None else None
        vlan_mgmt = str(row[15]).strip() if row[15] is not None else None
        status = str(row[19]).strip() if row[19] is not None else None

        rows.append({
            "label": label,
            "ip": ip,
            "hostname": hostname,
            "member": member,
            "model": model,
            "firmware": firmware,
            "vlan_mgmt": vlan_mgmt,
            "status": status,
        })
    wb.close()
    return rows


def _apply_excel_enrichment(dev: Device, row: dict) -> None:
    dev.excel_hostname = row.get("hostname")
    dev.label = row.get("label")
    dev.member = row.get("member")
    dev.vlan_mgmt = row.get("vlan_mgmt")
    if row.get("firmware"):
        dev.firmware = row.get("firmware")
    if dev.platform == Platform.UNKNOWN and dev.firmware:
        dev.platform = resolve_platform(dev.model, dev.firmware)

    excel_h = row.get("hostname")
    if excel_h and excel_h.strip().lower() != dev.name.strip().lower():
        if dev.ip in ("172.16.12.124", "172.16.12.236"):
            dev.notes.append(
                f"DATA-WARN: known label swap at {dev.ip}: switch.txt '{dev.name}' vs excel '{excel_h}'"
            )
        else:
            dev.notes.append(
                f"DATA-WARN: hostname conflict at {dev.ip}: switch.txt '{dev.name}' vs excel '{excel_h}'"
            )


def merge_inventory(
    devices: list[Device],
    excel_rows: list[dict],
    source: str = "switch",
) -> list[Device]:
    excel_by_ip: dict[str, dict] = {}
    for r in excel_rows:
        ip = r.get("ip")
        if ip and ip not in excel_by_ip:
            excel_by_ip[ip] = r

    if source == "switch":
        for dev in devices:
            if dev.ip in excel_by_ip:
                _apply_excel_enrichment(dev, excel_by_ip[dev.ip])
        return devices

    if source == "excel":
        built: list[Device] = []
        dev_by_ip = {d.ip: d for d in devices}
        for ip, r in excel_by_ip.items():
            name = r.get("hostname") or r.get("label") or ip
            model = r.get("model") or ""
            firmware = r.get("firmware")
            platform = resolve_platform(model, firmware)
            is_controller = platform == Platform.ARUBOS_MOBILITY or model.strip().lower() == "aruba7200"
            protected = ip in PROTECTED_IPS

            d = Device(
                name=name,
                ip=ip,
                model=model,
                platform=platform,
                firmware=firmware,
                excel_hostname=r.get("hostname"),
                label=r.get("label"),
                member=r.get("member"),
                vlan_mgmt=r.get("vlan_mgmt"),
                is_controller=is_controller,
                protected=protected,
            )
            if ip in dev_by_ip:
                orig = dev_by_ip[ip]
                d.password = orig.password
                d.username = orig.username
                if orig.name.strip().lower() != name.strip().lower():
                    if ip in ("172.16.12.124", "172.16.12.236"):
                        d.notes.append(
                            f"DATA-WARN: known label swap at {ip}: switch.txt '{orig.name}' vs excel '{name}'"
                        )
                    else:
                        d.notes.append(
                            f"DATA-WARN: hostname conflict at {ip}: switch.txt '{orig.name}' vs excel '{name}'"
                        )
            built.append(d)
        return built

    # source == "both"
    merged = [d for d in devices]
    for dev in merged:
        if dev.ip in excel_by_ip:
            _apply_excel_enrichment(dev, excel_by_ip[dev.ip])

    seen_ips = {d.ip for d in merged}
    for ip, r in excel_by_ip.items():
        if ip not in seen_ips:
            name = r.get("hostname") or r.get("label") or ip
            model = r.get("model") or ""
            firmware = r.get("firmware")
            platform = resolve_platform(model, firmware)
            is_controller = platform == Platform.ARUBOS_MOBILITY or model.strip().lower() == "aruba7200"
            protected = ip in PROTECTED_IPS
            merged.append(
                Device(
                    name=name,
                    ip=ip,
                    model=model,
                    platform=platform,
                    firmware=firmware,
                    excel_hostname=r.get("hostname"),
                    label=r.get("label"),
                    member=r.get("member"),
                    vlan_mgmt=r.get("vlan_mgmt"),
                    is_controller=is_controller,
                    protected=protected,
                )
            )
            seen_ips.add(ip)
    return merged


def filter_devices(
    devices: list[Device],
    only: Optional[str],
    exclude: Optional[str],
) -> list[Device]:
    res = devices
    if only:
        only_tokens = {t.strip().lower() for t in only.split(",") if t.strip()}
        res = [d for d in res if d.name.lower() in only_tokens or d.ip.lower() in only_tokens]
    if exclude:
        exclude_tokens = {t.strip().lower() for t in exclude.split(",") if t.strip()}
        res = [d for d in res if d.name.lower() not in exclude_tokens and d.ip.lower() not in exclude_tokens]
    return res


def username_candidates(device: Device, explicit_user: Optional[str]) -> list[str]:
    cands: list[str] = []
    if explicit_user and explicit_user.strip():
        cands.append(explicit_user.strip())
    if device.username and device.username.strip():
        cands.append(device.username.strip())
    if device.model and device.model.strip():
        cands.append(device.model.strip())
        m = device.model.strip()
        if len(m) > 1 and m.endswith(("A", "a")):
            cands.append(m[:-1])
    cands.append("Aruba2930")
    if device.password and device.password.strip():
        cands.append(device.password.strip())

    seen: set[str] = set()
    result: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
            if len(result) == 3:
                break
    return result
