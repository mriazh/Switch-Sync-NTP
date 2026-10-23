from pathlib import Path
import pytest
from switch_sync.models import Device, Platform
from switch_sync.inventory import (
    load_switch_txt,
    load_xlsx,
    merge_inventory,
    filter_devices,
    username_candidates,
)

FIXTURES = Path(__file__).parent / "fixtures"
SWITCH_TXT = FIXTURES / "switch_sanitized.txt"
EXCEL_PATH = Path(r"C:\Users\adima\OneDrive\Dokumen\Network-Riyadh\Switch-Master.xlsx")


def test_load_switch_txt_count():
    devices = load_switch_txt(SWITCH_TXT)
    assert len(devices) == 104


def test_load_switch_txt_adm1n_and_dirty_rows():
    devices = load_switch_txt(SWITCH_TXT)
    by_ip = {d.ip: d for d in devices}

    # The five adm1n rows must survive
    adm1n_ips = ["172.16.13.210", "172.16.13.211", "172.16.12.241", "172.16.12.163", "172.16.66.5"]
    for ip in adm1n_ips:
        assert ip in by_ip
        assert by_ip[ip].model == "adm1n"
        assert by_ip[ip].platform == Platform.UNKNOWN

    # Spaceless-pipe before IP row (row 90: ESW01-WS1-LGL2B-2530 |172.16.12.212 | Aruba2530 | ***)
    assert "172.16.12.212" in by_ip
    assert by_ip["172.16.12.212"].name == "ESW01-WS1-LGL2B-2530"

    # Spaceless-pipe before password (row 92: SW-WS2-L2A-2530 | 172.16.12.171 | Aruba2530 |***)
    assert "172.16.12.171" in by_ip
    assert by_ip["172.16.12.171"].password == "***"

    # Protected devices flagged
    for prot_ip in ["172.16.13.1", "172.21.255.1", "172.21.255.2", "172.21.255.6"]:
        assert by_ip[prot_ip].protected is True

    # Controllers flagged
    for ctrl_ip in ["172.16.21.5", "172.16.21.6", "172.16.21.7"]:
        assert by_ip[ctrl_ip].is_controller is True
        assert by_ip[ctrl_ip].platform == Platform.ARUBOS_MOBILITY


def test_load_switch_txt_5_fields(tmp_path):
    custom_txt = tmp_path / "custom_switch.txt"
    custom_txt.write_text(
        "SW-TEST | 10.1.1.1 | Aruba2930 | secret | admin_user\n"
        "SW-TEST2| 10.1.1.2|Procurve2520| secret2 \n",
        encoding="utf-8",
    )
    devices = load_switch_txt(custom_txt)
    assert len(devices) == 2
    assert devices[0].username == "admin_user"
    assert devices[0].name == "SW-TEST"
    assert devices[0].ip == "10.1.1.1"
    assert devices[0].model == "Aruba2930"
    assert devices[0].platform == Platform.ARUBOS_S
    assert devices[1].username is None


def test_load_xlsx():
    if not EXCEL_PATH.exists():
        pytest.skip("Switch-Master.xlsx not present")
    rows = load_xlsx(EXCEL_PATH)
    assert len(rows) == 161
    first = rows[0]
    assert "label" in first
    assert "ip" in first
    assert "hostname" in first
    assert "model" in first
    assert "firmware" in first
    assert "vlan_mgmt" in first
    assert "status" in first


def test_merge_inventory():
    devices = load_switch_txt(SWITCH_TXT)
    excel_rows = [
        {
            "label": "CORE DISTRI HG4",
            "ip": "172.16.13.1",
            "hostname": "DSW01-HG4-LT.1",
            "member": "1",
            "model": "Aruba5400",
            "firmware": "KB.16.01.0009",
            "vlan_mgmt": "Testing",
            "status": "success",
        },
        {
            "label": "SW-MD-L1-2530",
            "ip": "172.16.12.163",
            "hostname": "SW-MD-L1-2530",
            "member": "1",
            "model": "Aruba2530",
            "firmware": "YA.15.17.0009",
            "vlan_mgmt": "MGMT",
            "status": "success",
        },
        # Known swap 1
        {
            "label": "ASW02-HG3-L1CF",
            "ip": "172.16.12.124",
            "hostname": "ASW02-HG3-L1CF",
            "member": "1",
            "model": "Aruba2930",
            "firmware": "WC.16.05.0004",
            "vlan_mgmt": "MGMT",
            "status": "success",
        },
        # Known swap 2
        {
            "label": "ASW01-HG3-L1CF",
            "ip": "172.16.12.236",
            "hostname": "ASW01-HG3-L1CF",
            "member": "1",
            "model": "Aruba2930",
            "firmware": "WC.16.05.0004",
            "vlan_mgmt": "MGMT",
            "status": "success",
        },
        # Extra excel device not in switch.txt
        {
            "label": "EXTRA-SW",
            "ip": "10.99.99.99",
            "hostname": "EXTRA-SW",
            "member": "1",
            "model": "Aruba2930",
            "firmware": "WC.16.05.0004",
            "vlan_mgmt": "MGMT",
            "status": "success",
        },
    ]

    merged_switch = merge_inventory(devices, excel_rows, source="switch")
    by_ip = {d.ip: d for d in merged_switch}

    # 172.16.12.163 originally had model=adm1n and platform=UNKNOWN
    # Excel has firmware YA.15.17.0009 -> resolved to ARUBOS_S
    assert by_ip["172.16.12.163"].platform == Platform.ARUBOS_S
    assert by_ip["172.16.12.163"].firmware == "YA.15.17.0009"

    # Hostname conflict note on 172.16.13.1 (switch.txt has CORE DISTRI HG4, excel has DSW01-HG4-LT.1)
    notes_13_1 = by_ip["172.16.13.1"].notes
    assert any("DATA" in n and "WARN" in n for n in notes_13_1)

    # Known swap notes appear on 172.16.12.124 and 172.16.12.236
    notes_124 = by_ip["172.16.12.124"].notes
    assert any("swap" in n.lower() or "DATA" in n for n in notes_124)
    notes_236 = by_ip["172.16.12.236"].notes
    assert any("swap" in n.lower() or "DATA" in n for n in notes_236)

    # Source "both" includes extra device
    merged_both = merge_inventory(devices, excel_rows, source="both")
    assert any(d.ip == "10.99.99.99" for d in merged_both)


def test_filter_devices():
    devices = [
        Device(name="SW-A", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S),
        Device(name="SW-B", ip="10.0.0.2", model="Aruba2530", platform=Platform.ARUBOS_S),
        Device(name="SW-C", ip="10.0.0.3", model="Procurve2520", platform=Platform.PROCURVE_LEGACY),
    ]

    # whitelist (only)
    f_only = filter_devices(devices, only="sw-a,10.0.0.3", exclude=None)
    assert [d.name for d in f_only] == ["SW-A", "SW-C"]

    # blacklist (exclude)
    f_exc = filter_devices(devices, only=None, exclude="10.0.0.2")
    assert [d.name for d in f_exc] == ["SW-A", "SW-C"]

    # both
    f_both = filter_devices(devices, only="sw-a,sw-b", exclude="sw-b")
    assert [d.name for d in f_both] == ["SW-A"]


def test_username_candidates():
    # Order: explicit_user, device.username, MODEL verbatim, MODEL without trailing A, Aruba2930, password
    # 3-item cap, deduplicated
    dev1 = Device(
        name="SW1",
        ip="1.1.1.1",
        model="Aruba2530A",
        platform=Platform.ARUBOS_S,
        password="pwd",
    )
    cands1 = username_candidates(dev1, explicit_user="operator")
    # Candidates: operator, Aruba2530A, Aruba2530 -> cap 3
    assert cands1 == ["operator", "Aruba2530A", "Aruba2530"]

    # Without explicit user, with username in device
    dev2 = Device(
        name="SW2",
        ip="1.1.1.2",
        model="adm1n",
        platform=Platform.UNKNOWN,
        password="pwd",
        username="manager",
    )
    cands2 = username_candidates(dev2, explicit_user=None)
    assert cands2 == ["manager", "adm1n", "Aruba2930"]

    # Fallback to password if fewer candidates
    dev3 = Device(
        name="SW3",
        ip="1.1.1.3",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
        password="secret",
    )
    cands3 = username_candidates(dev3, explicit_user=None)
    # Aruba2930 (model), Aruba2930 (fallback dup), secret (password)
    assert cands3 == ["Aruba2930", "secret"]
