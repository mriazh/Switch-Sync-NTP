import socket
import struct
import time
from typing import Optional
from switch_sync.models import NtpResult

NTP_EPOCH_OFFSET = 2208988800

KNOWN_REFIDS = {
    "LOCL", "INIT", "STEP", "DENY", "GPS", "PPS",
    "ACTS", "PTB", "USNO", "CHU", "WWV", "WWVB", "MSF", "GOES",
}


def build_request(server: str = "", t: Optional[float] = None) -> bytes:
    if t is None:
        t = time.time()
    req = bytearray(48)
    req[0] = 0x1B
    req[1] = 0
    req[2] = 6
    req[3] = 0xE9
    sec = int(t + NTP_EPOCH_OFFSET)
    frac = int((t + NTP_EPOCH_OFFSET - sec) * (1 << 32))
    req[40:48] = struct.pack("!II", sec, frac)
    return bytes(req)


def parse_reply(data: bytes, t_send: float, t_recv: float, server: str = "") -> NtpResult:
    if len(data) < 48:
        return NtpResult(
            server=server,
            reachable=True,
            valid=False,
            stratum=None,
            root_delay_s=None,
            root_dispersion_s=None,
            offset_s=None,
            mode=None,
            refid=None,
            error="Malformed reply: packet too short",
        )

    b0 = data[0]
    mode = b0 & 0x07
    stratum = data[1]
    poll = data[2]
    precision = struct.unpack("!b", data[3:4])[0]

    root_delay_raw = struct.unpack("!i", data[4:8])[0]
    root_delay_s = root_delay_raw / 65536.0

    root_disp_raw = struct.unpack("!i", data[8:12])[0]
    root_dispersion_s = root_disp_raw / 65536.0

    refid_bytes = data[12:16]
    try:
        candidate = refid_bytes.decode("ascii").rstrip("\x00")
        if candidate in KNOWN_REFIDS or (candidate.isprintable() and len(candidate) > 0 and stratum <= 1):
            refid = candidate
        else:
            refid = socket.inet_ntoa(refid_bytes) if stratum > 1 else (candidate if candidate.isprintable() else refid_bytes.hex())
    except Exception:
        refid = socket.inet_ntoa(refid_bytes) if stratum > 1 else refid_bytes.hex()

    ref_ts = struct.unpack("!I", data[16:20])[0]
    orig_ts = struct.unpack("!I", data[20:24])[0]
    recv_ts = struct.unpack("!I", data[24:28])[0]
    trans_ts = struct.unpack("!I", data[28:32])[0]

    if ref_ts != 0:
        reference_time = float(ref_ts - NTP_EPOCH_OFFSET) if ref_ts >= NTP_EPOCH_OFFSET else float(ref_ts)
    else:
        reference_time = None

    valid = bool(mode == 4 and (1 <= stratum <= 15) and (trans_ts != 0))

    if trans_ts == 0 or recv_ts == 0:
        offset_s = None
        error_msg = "Server answered without timestamps"
    elif trans_ts < NTP_EPOCH_OFFSET:
        offset_s = None
        error_msg = "Server answered with inconsistent timestamps"
    else:
        t1 = t_send + NTP_EPOCH_OFFSET
        t2 = float(recv_ts)
        t3 = float(trans_ts)
        t4 = t_recv + NTP_EPOCH_OFFSET
        calc_offset = ((t2 - t1) + (t3 - t4)) / 2.0
        if abs(calc_offset) > 86400.0:
            offset_s = None
            error_msg = "Server answered with inconsistent timestamps"
        else:
            offset_s = calc_offset
            error_msg = None

    clock_skew = offset_s if offset_s is not None else None

    return NtpResult(
        server=server,
        reachable=True,
        valid=valid,
        stratum=stratum,
        root_delay_s=root_delay_s,
        root_dispersion_s=root_dispersion_s,
        offset_s=offset_s,
        mode=mode,
        refid=refid,
        error=error_msg,
        reference_time=reference_time,
        clock_skew=clock_skew,
    )


def query(server: str, timeout: float = 5.0) -> NtpResult:
    t_send = time.time()
    req = build_request(server=server, t=t_send)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    try:
        sock.sendto(req, (server, 123))
        data, _ = sock.recvfrom(1024)
        t_recv = time.time()
    except socket.timeout:
        return NtpResult(
            server=server,
            reachable=False,
            valid=False,
            stratum=None,
            root_delay_s=None,
            root_dispersion_s=None,
            offset_s=None,
            mode=None,
            refid=None,
            error="Connection timed out",
        )
    except Exception as exc:
        return NtpResult(
            server=server,
            reachable=False,
            valid=False,
            stratum=None,
            root_delay_s=None,
            root_dispersion_s=None,
            offset_s=None,
            mode=None,
            refid=None,
            error=f"{type(exc).__name__}",
        )
    finally:
        sock.close()

    return parse_reply(data, t_send, t_recv, server=server)
