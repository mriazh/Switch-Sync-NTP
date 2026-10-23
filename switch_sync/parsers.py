import re
from typing import Optional
from switch_sync.models import Platform
from switch_sync.platforms import platform_from_firmware


def strip_ansi(text: str) -> str:
    if not text:
        return ""
    # Strip null bytes and control chars
    clean = text.replace("\x00", "")
    # Strip ANSI escape sequences
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", clean)
    clean = re.sub(r"\x1b\([a-zA-Z]", "", clean)
    return clean


def is_invalid_input(output: str) -> bool:
    if not output:
        return False
    lower = output.lower()
    return (
        "invalid input" in lower
        or "% command incomplete" in lower
        or "invalid" in lower
        or "% invalid" in lower
        or "syntax error" in lower
        or "incomplete input" in lower
        or "already configured" in lower
    )


def parse_show_version(output: str) -> dict:
    res = {"firmware": None, "platform_hint": None}
    if not output or is_invalid_input(output):
        return res
    text = strip_ansi(output)

    # Check CX format: Version : XL.10.04.3050
    m_cx = re.search(r"^\s*Version\s*:\s*([A-Za-z0-9_.-]+)", text, re.MULTILINE)
    if m_cx:
        fw = m_cx.group(1).strip()
        res["firmware"] = fw
        res["platform_hint"] = platform_from_firmware(fw)
        return res

    # Check AOS-S / ProCurve format: version under Image stamp
    m_img = re.search(r"Image stamp:.*?\n(?:\s*[^\n]+\n)?\s*([A-Z]{1,3}\.\d{2}\.[\w\.]+)", text, re.DOTALL)
    if m_img:
        fw = m_img.group(1).strip()
        res["firmware"] = fw
        res["platform_hint"] = platform_from_firmware(fw)
        return res

    # Fallback for standard version code
    m_code = re.search(r"\b([A-Z]{1,3}\.\d{2}\.[\w\.]+)\b", text)
    if m_code:
        fw = m_code.group(1).strip()
        res["firmware"] = fw
        res["platform_hint"] = platform_from_firmware(fw)
        return res

    return res


def parse_show_sntp(output: str) -> dict:
    res = {
        "servers": [],
        "entries": [],
        "mode": None,
        "sync_mode": None,
        "poll_interval": None,
        "supported": False,
        "timezone": None,
    }
    if not output or is_invalid_input(output):
        return res
    text = strip_ansi(output)
    if "sntp" not in text.lower():
        return res

    res["supported"] = True

    m_mode = re.search(r"SNTP Mode\s*(?:\[\d+\])?\s*:\s*(\S+)", text, re.IGNORECASE)
    if m_mode:
        res["mode"] = m_mode.group(1).strip()

    m_sync = re.search(r"Time Sync Mode\s*:\s*(\S+)", text, re.IGNORECASE)
    if m_sync:
        res["sync_mode"] = m_sync.group(1).strip()

    m_poll = re.search(r"Poll Interval\s*(?:\(sec\))?\s*(?:\[\d+\])?\s*:\s*(\d+)", text, re.IGNORECASE)
    if m_poll:
        res["poll_interval"] = int(m_poll.group(1))

    # Servers from priority table:
    # Priority SNTP Server Address ...
    # 1        192.168.240.240
    servers = []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        m_srv = re.match(r"^(\d+)\s+([0-9a-zA-Z.:_-]+)", line)
        if m_srv:
            prio = int(m_srv.group(1))
            srv = m_srv.group(2).strip()
            if srv not in servers:
                servers.append(srv)
            if (prio, srv) not in entries:
                entries.append((prio, srv))

    res["servers"] = servers
    res["entries"] = entries
    return res


def parse_show_ntp_status(output: str) -> dict:
    res = {
        "servers": [],
        "sync_status": None,
        "stratum": None,
        "clock": None,
        "enabled": None,
        "vrf": None,
    }
    if not output or is_invalid_input(output):
        return res
    text = strip_ansi(output)
    if "ntp" not in text.lower():
        return res

    m_enabled = re.search(r"NTP\s*:\s*(Enabled|Disabled)", text, re.IGNORECASE)
    if m_enabled:
        res["enabled"] = m_enabled.group(1).lower() == "enabled"

    m_vrf = re.search(r"NTP Server Connections\s*:\s*(?:Using the\s+)?([^\n\r]+)", text, re.IGNORECASE)
    if m_vrf:
        res["vrf"] = m_vrf.group(1).strip()

    m_time = re.search(r"System time\s*:\s*([^\n\r]+)", text, re.IGNORECASE)
    if m_time:
        res["clock"] = m_time.group(1).strip()

    lower = text.lower()
    if "not synchronized" in lower or "unsynchronized" in lower:
        res["sync_status"] = "unsynchronized"
    elif "synchronized" in lower:
        res["sync_status"] = "synchronized"

    m_strat = re.search(r"Stratum\s*:\s*(\d+)", text, re.IGNORECASE)
    if m_strat:
        res["stratum"] = int(m_strat.group(1))

    # Look for servers if any
    servers = []
    for m in re.finditer(r"NTP Server\s*:\s*([0-9a-zA-Z.:_-]+)", text, re.IGNORECASE):
        s = m.group(1).strip()
        if s not in servers:
            servers.append(s)
    res["servers"] = servers

    return res


def parse_show_time(output: str) -> Optional[str]:
    if not output or is_invalid_input(output):
        return None
    text = strip_ansi(output)
    m = re.search(r"\b([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\b", text)
    if m:
        return m.group(1)
    return None


def parse_show_clock(output: str) -> dict:
    res = {"clock": None, "timezone": None}
    if not output or is_invalid_input(output):
        return res
    text = strip_ansi(output)

    m_tz = re.search(r"System is configured for timezone\s*:\s*([^\n\r]+)", text, re.IGNORECASE)
    if m_tz:
        res["timezone"] = m_tz.group(1).strip()

    m_clock = re.search(
        r"\b([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\s+[A-Za-z0-9/+-]+)?\s+\d{4})\b",
        text,
    )
    if m_clock:
        res["clock"] = m_clock.group(1).strip()

    return res


def parse_show_system(output: str) -> dict:
    res = {"timezone": None, "firmware": None}
    if not output or is_invalid_input(output):
        return res
    text = strip_ansi(output)

    m_tz = re.search(r"Time Zone\s*:\s*(-?\d+)", text, re.IGNORECASE)
    if m_tz:
        res["timezone"] = m_tz.group(1).strip()

    m_fw = re.search(r"Software revision\s*:\s*([A-Za-z0-9_.-]+)", text, re.IGNORECASE)
    if m_fw:
        res["firmware"] = m_fw.group(1).strip()

    return res


def parse_running_config(output: str) -> dict:
    res = {"servers": [], "entries": [], "timezone": None, "timesync_supported": False}
    if not output or is_invalid_input(output):
        return res
    text = strip_ansi(output)

    if "timesync sntp" in text.lower():
        res["timesync_supported"] = True

    servers = []
    entries = []
    for m in re.finditer(r"sntp server priority (\d+)\s+([0-9a-zA-Z.:_-]+)", text, re.IGNORECASE):
        prio = int(m.group(1))
        s = m.group(2).strip()
        if s not in servers:
            servers.append(s)
        if (prio, s) not in entries:
            entries.append((prio, s))

    for m in re.finditer(r"(?<!s)ntp server\s+([0-9a-zA-Z.:_-]+)", text, re.IGNORECASE):
        s = m.group(1).strip()
        if s.lower() not in ("enable", "unicast", "priority") and s not in servers:
            servers.append(s)

    res["servers"] = servers
    res["entries"] = entries

    m_tz = re.search(r"(?:time|clock)\s+timezone\s+([^\n\r]+)", text, re.IGNORECASE)
    if m_tz:
        res["timezone"] = m_tz.group(1).strip()

    return res
