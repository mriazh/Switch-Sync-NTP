import socket
import struct
from unittest.mock import patch
import pytest

from switch_sync.ntp import query, build_request, parse_reply
from switch_sync.models import NtpResult


def _build_synthetic_packet(
    mode=4,
    stratum=2,
    poll=6,
    precision=-20,
    root_delay=65536,  # 1.0 in 16.16 fixed-point
    root_dispersion=32768,  # 0.5 in 16.16 fixed-point
    refid=b"LOCL",
    ref_ts=3900000000,
    orig_ts=3900000010,
    recv_ts=3900000020,
    trans_ts=3900000022,
) -> bytes:
    # byte 0: LI=0 (0), VN=4 (4 << 3 = 32), MODE (mode & 7)
    b0 = (0 << 6) | (4 << 3) | (mode & 0x07)
    header = struct.pack("!BBBb", b0, stratum, poll, precision)
    fixed = struct.pack("!ii", root_delay, root_dispersion)
    refid_bytes = refid.ljust(4, b"\x00")[:4]
    timestamps = struct.pack("!IIII", ref_ts, orig_ts, recv_ts, trans_ts)
    padding = b"\x00" * 16
    return header + fixed + refid_bytes + timestamps + padding


def test_ntp_query_valid_server():
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        root_delay=65536,
        root_dispersion=32768,
        refid=b"LOCL",
        ref_ts=3900000000,
        orig_ts=3900000010,
        recv_ts=3900000020,
        trans_ts=3900000022,
    )

    # Hand-computed values:
    # root_delay_s = 65536 / 65536.0 = 1.0
    # root_dispersion_s = 32768 / 65536.0 = 0.5
    # t_send = 1691011200.0 -> t1 = 1691011200.0 + 2208988800 = 3900000000.0
    # t_recv = 1691011204.0 -> t4 = 1691011204.0 + 2208988800 = 3900000004.0
    # recv_ts = 3900000020, trans_ts = 3900000022
    # offset = ((3900000020 - 3900000000) + (3900000022 - 3900000004)) / 2.0
    #        = (20.0 + 18.0) / 2.0 = 19.0
    with patch("socket.socket") as mock_sock_cls, patch("time.time") as mock_time:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("10.0.0.1", 123))
        mock_time.side_effect = [1691011200.0, 1691011204.0]

        res = query("10.0.0.1", timeout=2.0)

        assert isinstance(res, NtpResult)
        assert res.server == "10.0.0.1"
        assert res.reachable is True
        assert res.valid is True
        assert res.stratum == 2
        assert res.mode == 4
        assert res.refid == "LOCL"
        assert res.root_delay_s == 1.0
        assert res.root_dispersion_s == 0.5
        assert res.offset_s == 19.0
        assert res.error is None


def test_ntp_query_stratum_0():
    packet = _build_synthetic_packet(
        mode=4,
        stratum=0,
        refid=b"INIT",
        trans_ts=0,
    )
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("10.0.0.2", 123))

        res = query("10.0.0.2", timeout=2.0)

        assert res.reachable is True
        assert res.valid is False
        assert res.stratum == 0
        assert res.mode == 4
        assert res.refid == "INIT"


def test_ntp_query_mode_not_4():
    packet = _build_synthetic_packet(
        mode=3,  # Client mode instead of server
        stratum=2,
    )
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("10.0.0.3", 123))

        res = query("10.0.0.3", timeout=2.0)

        assert res.reachable is True
        assert res.valid is False
        assert res.mode == 3


def test_ntp_query_zero_transmit_timestamp():
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        trans_ts=0,
    )
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("10.0.0.4", 123))

        res = query("10.0.0.4", timeout=2.0)

        assert res.reachable is True
        assert res.valid is False
        assert res.stratum == 2
        assert res.mode == 4
        assert res.offset_s is None
        assert res.error is not None
        assert "without timestamps" in res.error.lower()


def test_ntp_query_timeout():
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.side_effect = socket.timeout("timed out")

        res = query("10.0.0.5", timeout=1.0)

        assert res.reachable is False
        assert res.valid is False
        assert res.stratum is None
        assert res.offset_s is None
        assert res.error is not None
        assert "timed out" in res.error.lower() or "timeout" in res.error.lower()


def test_ntp_query_malformed_packet():
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (b"\x1b\x00\x00", ("10.0.0.6", 123))

        res = query("10.0.0.6", timeout=1.0)

        assert res.reachable is True
        assert res.valid is False
        assert res.error is not None


def test_ntp_build_request():
    req = build_request("172.16.40.5")
    assert len(req) == 48
    assert req[0] == 0x1B
    assert req[1] == 0
    assert req[2] == 6
    assert req[3] == 0xE9
    assert req[4:40] == b"\x00" * 36
    # Transmit timestamp at offset 40 is non-zero
    sec, frac = struct.unpack("!II", req[40:48])
    assert sec != 0


def test_ntp_query_real_measured_servers():
    # 172.16.40.5 answers MODE 4, stratum 4, refid 162.159.200.1
    refid_ip = socket.inet_aton("162.159.200.1")
    pkt_40 = _build_synthetic_packet(
        mode=4,
        stratum=4,
        refid=refid_ip,
        trans_ts=3900000022,
    )
    with patch("socket.socket") as mock_sock_cls, patch("time.time") as mock_time:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (pkt_40, ("172.16.40.5", 123))
        mock_time.side_effect = [1691011200.0, 1691011204.0]

        res = query("172.16.40.5", timeout=2.0)
        assert res.reachable is True
        assert res.valid is True
        assert res.stratum == 4
        assert res.mode == 4
        assert res.refid == "162.159.200.1"
        assert res.offset_s is not None

    # 192.168.240.240 answers MODE 4, stratum 2, refid 86.77.84.80
    refid_vmtp = socket.inet_aton("86.77.84.80")
    pkt_240 = _build_synthetic_packet(
        mode=4,
        stratum=2,
        refid=refid_vmtp,
        trans_ts=3900000022,
    )
    with patch("socket.socket") as mock_sock_cls, patch("time.time") as mock_time:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (pkt_240, ("192.168.240.240", 123))
        mock_time.side_effect = [1691011200.0, 1691011204.0]

        res = query("192.168.240.240", timeout=2.0)
        assert res.reachable is True
        assert res.valid is True
        assert res.stratum == 2
        assert res.mode == 4
        assert res.refid == "86.77.84.80"
        assert res.offset_s is not None

    # 192.168.240.215 answers MODE 4 with stratum 0, refid INIT
    pkt_215 = _build_synthetic_packet(
        mode=4,
        stratum=0,
        refid=b"INIT",
        trans_ts=0,
    )
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (pkt_215, ("192.168.240.215", 123))

        res = query("192.168.240.215", timeout=2.0)
        assert res.reachable is True
        assert res.valid is False
        assert res.stratum == 0
        assert res.mode == 4
        assert res.refid == "INIT"
        assert res.offset_s is None
        assert "without timestamps" in res.error.lower()


def test_ntp_query_unix_epoch_transmit_timestamp():
    # Appliance answers with UNIX-style epoch timestamp (e.g. 704782336), not NTP timestamp range
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        refid=b"VMTP",
        ref_ts=4001575679,
        orig_ts=4001575600,
        recv_ts=4001575627,
        trans_ts=704782336,
    )
    with patch("socket.socket") as mock_sock_cls, patch("time.time") as mock_time:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("192.168.240.240", 123))
        mock_time.side_effect = [1792586827.0, 1792586827.05]

        res = query("192.168.240.240", timeout=2.0)
        assert res.reachable is True
        assert res.valid is True
        assert res.stratum == 2
        assert res.mode == 4
        assert res.offset_s is None
        assert res.error is not None
        assert "inconsistent timestamps" in res.error.lower()


def test_ntp_query_offset_larger_than_one_day():
    # Timestamps in NTP range but offset > 86400s (e.g. 100,000s)
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        refid=b"VMTP",
        ref_ts=3900000000,
        orig_ts=3900000000,
        recv_ts=3900100000,
        trans_ts=3900100000,
    )
    with patch("socket.socket") as mock_sock_cls, patch("time.time") as mock_time:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("192.168.240.240", 123))
        mock_time.side_effect = [1691011200.0, 1691011200.05]

        res = query("192.168.240.240", timeout=2.0)
        assert res.reachable is True
        assert res.valid is True
        assert res.stratum == 2
        assert res.mode == 4
        assert res.offset_s is None
        assert res.error is not None
        assert "inconsistent timestamps" in res.error.lower()


def test_ntp_query_zero_receive_timestamp():
    # recv_ts = 0
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        refid=b"LOCL",
        recv_ts=0,
        trans_ts=3900000022,
    )
    with patch("socket.socket") as mock_sock_cls, patch("time.time") as mock_time:
        mock_sock = mock_sock_cls.return_value
        mock_sock.recvfrom.return_value = (packet, ("10.0.0.7", 123))
        mock_time.side_effect = [1691011200.0, 1691011204.0]

        res = query("10.0.0.7", timeout=2.0)
        assert res.reachable is True
        assert res.valid is True
        assert res.stratum == 2
        assert res.mode == 4
        assert res.offset_s is None
        assert res.error is not None
        assert "without timestamps" in res.error.lower() or "inconsistent timestamps" in res.error.lower()


def test_ntp_reference_time_set():
    # 3900000000 NTP seconds = 3900000000 - 2208988800 = 1691011200.0 POSIX epoch
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        ref_ts=3900000000,
        trans_ts=3900000022,
    )
    res = parse_reply(packet, 1691011200.0, 1691011204.0, server="10.0.0.1")
    assert res.reference_time == 1691011200.0
    assert res.clock_skew is not None


def test_ntp_reference_time_zero_yields_none():
    packet = _build_synthetic_packet(
        mode=4,
        stratum=2,
        ref_ts=0,
        trans_ts=3900000022,
    )
    res = parse_reply(packet, 1691011200.0, 1691011204.0, server="10.0.0.1")
    assert res.reference_time is None


