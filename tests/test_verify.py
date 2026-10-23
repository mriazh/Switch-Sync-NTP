from unittest.mock import MagicMock
import pytest

from switch_sync.models import (
    Device,
    Platform,
    ProbeResult,
    RunOptions,
    Status,
    SyncVerdict,
    Verdict,
)
from switch_sync.verify import (
    classify,
    parse_clock,
    skew_matrix,
    verify_devices,
)


def test_parse_clock_formats():
    # Format 1: ArubaOS-S / standard
    t1 = parse_clock("Tue Oct 20 10:02:18 2026")
    assert t1 is not None

    # Format 2: ProCurve / legacy with double space
    t2 = parse_clock("Wed Feb  7 10:19:23 1990")
    assert t2 is not None
    assert t2 < t1

    # Format 3: CX format with timezone token WIB
    t3 = parse_clock("Tue Oct 20 09:58:41 WIB 2026")
    assert t3 is not None
    # Exactly 217 seconds before t1 (10:02:18 - 09:58:41 = 3m 37s = 217s)
    assert abs((t1 - t3) - 217.0) < 1e-3

    # Invalid string
    assert parse_clock("Not a valid clock") is None
    assert parse_clock(None) is None


def test_classify_unreachable():
    dev = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(device=dev, ok=False, status=Status.UNREACHABLE)
    v = classify(probe, target_servers=["192.168.240.240"], max_skew=2.0, device_clocks={})
    assert v.verdict == SyncVerdict.UNREACHABLE


def test_classify_no_server_arubos_s():
    dev = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        servers=[],
    )
    v = classify(probe, target_servers=["192.168.240.240"], max_skew=2.0, device_clocks={"10.0.0.1": 1000.0})
    assert v.verdict == SyncVerdict.NO_SERVER


def test_classify_no_server_cx():
    dev = Device(name="CX1", ip="10.0.0.2", model="aruba8400", platform=Platform.ARUBOS_CX)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        sync_status="unsynchronized",
        servers=[],
    )
    v = classify(probe, target_servers=["192.168.240.240"], max_skew=2.0, device_clocks={"10.0.0.2": 1000.0})
    assert v.verdict == SyncVerdict.NO_SERVER


def test_classify_unsync_arubos_s_wrong_server():
    dev = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        servers=["192.168.1.1"],
    )
    clocks = {"10.0.0.1": parse_clock("Tue Oct 20 10:02:18 2026")}
    v = classify(probe, target_servers=["192.168.240.240"], max_skew=2.0, device_clocks=clocks)
    assert v.verdict == SyncVerdict.UNSYNC


def test_classify_unsync_arubos_s_high_skew():
    dev = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:25 2026",  # 7 seconds ahead of median
        servers=["192.168.240.240"],
    )
    # Fleet median is 10:02:18
    t_med = parse_clock("Tue Oct 20 10:02:18 2026")
    clocks = {
        "10.0.0.1": parse_clock("Tue Oct 20 10:02:25 2026"),
        "10.0.0.2": t_med,
        "10.0.0.3": t_med,
    }
    v = classify(probe, target_servers=["192.168.240.240"], max_skew=2.0, device_clocks=clocks)
    assert v.verdict == SyncVerdict.UNSYNC


def test_classify_sync_arubos_s():
    dev = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    t = parse_clock("Tue Oct 20 10:02:18 2026")
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        servers=["192.168.240.240"],
    )
    clocks = {"10.0.0.1": t, "10.0.0.2": t}
    v = classify(probe, target_servers=["192.168.240.240"], max_skew=2.0, device_clocks=clocks)
    assert v.verdict == SyncVerdict.SYNC
    assert v.stratum is None  # AOS-S has no stratum


def test_classify_cx_stratum_and_sync_rules():
    dev = Device(name="CX1", ip="10.0.0.2", model="aruba8400", platform=Platform.ARUBOS_CX)
    t = parse_clock("Tue Oct 20 10:02:18 2026")
    clocks = {"10.0.0.2": t}

    # Stratum 2, synchronized -> SYNC
    probe_ok = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        sync_status="synchronized",
        stratum=2,
        servers=["192.168.240.240"],
    )
    v_ok = classify(probe_ok, ["192.168.240.240"], 2.0, clocks)
    assert v_ok.verdict == SyncVerdict.SYNC
    assert v_ok.stratum == 2

    # Stratum 0 -> UNSYNC
    probe_strat0 = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        sync_status="synchronized",
        stratum=0,
        servers=["192.168.240.240"],
    )
    v_strat0 = classify(probe_strat0, ["192.168.240.240"], 2.0, clocks)
    assert v_strat0.verdict == SyncVerdict.UNSYNC

    # Stratum 16 -> UNSYNC
    probe_strat16 = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        sync_status="synchronized",
        stratum=16,
        servers=["192.168.240.240"],
    )
    v_strat16 = classify(probe_strat16, ["192.168.240.240"], 2.0, clocks)
    assert v_strat16.verdict == SyncVerdict.UNSYNC

    # sync_status unsynchronized -> UNSYNC
    probe_unsync = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        sync_status="unsynchronized",
        stratum=2,
        servers=["192.168.240.240"],
    )
    v_unsync = classify(probe_unsync, ["192.168.240.240"], 2.0, clocks)
    assert v_unsync.verdict == SyncVerdict.UNSYNC


def test_fleet_median_ignores_outlier():
    # Three switches at 2026, one outlier at 1990
    d_outlier = Device(name="SW-OLD", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    d_good1 = Device(name="SW-OK1", ip="10.0.0.2", model="Aruba2930", platform=Platform.ARUBOS_S)
    d_good2 = Device(name="SW-OK2", ip="10.0.0.3", model="Aruba2930", platform=Platform.ARUBOS_S)
    d_good3 = Device(name="SW-OK3", ip="10.0.0.4", model="Aruba2930", platform=Platform.ARUBOS_S)

    t_1990 = parse_clock("Wed Feb  7 10:19:23 1990")
    t_2026 = parse_clock("Tue Oct 20 10:02:18 2026")

    clocks = {
        "10.0.0.1": t_1990,
        "10.0.0.2": t_2026,
        "10.0.0.3": t_2026 + 0.5,
        "10.0.0.4": t_2026 - 0.5,
    }

    # Outlier classified as UNSYNC due to skew against median
    p_outlier = ProbeResult(
        device=d_outlier,
        ok=True,
        status=Status.OK,
        clock="Wed Feb  7 10:19:23 1990",
        servers=["192.168.240.240"],
    )
    v_outlier = classify(p_outlier, ["192.168.240.240"], 2.0, clocks)
    assert v_outlier.verdict == SyncVerdict.UNSYNC

    # Good switches classified as SYNC
    p_good = ProbeResult(
        device=d_good1,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        servers=["192.168.240.240"],
    )
    v_good = classify(p_good, ["192.168.240.240"], 2.0, clocks)
    assert v_good.verdict == SyncVerdict.SYNC


def test_skew_matrix():
    d1 = Device(name="SW1", ip="10.0.0.1", model="M", platform=Platform.ARUBOS_S)
    d2 = Device(name="SW2", ip="10.0.0.2", model="M", platform=Platform.ARUBOS_S)
    d3 = Device(name="SW3", ip="10.0.0.3", model="M", platform=Platform.ARUBOS_S)

    v1 = Verdict(device=d1, verdict=SyncVerdict.SYNC, offset_s=10.0)
    v2 = Verdict(device=d2, verdict=SyncVerdict.SYNC, offset_s=12.5)
    v3 = Verdict(device=d3, verdict=SyncVerdict.UNREACHABLE, offset_s=None)

    mat = skew_matrix([v1, v2, v3])

    assert "10.0.0.1" in mat
    assert "10.0.0.2" in mat
    assert "10.0.0.3" not in mat  # No clock

    assert mat["10.0.0.1"]["10.0.0.2"] == 2.5
    assert mat["10.0.0.2"]["10.0.0.1"] == 2.5


def test_verify_devices(monkeypatch):
    d1 = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    d2 = Device(name="SW2", ip="10.0.0.2", model="Aruba2930", platform=Platform.ARUBOS_S)

    probes = [
        ProbeResult(
            device=d1,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:18 2026",
            servers=["192.168.240.240"],
        ),
        ProbeResult(
            device=d2,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:19 2026",
            servers=["192.168.240.240"],
        ),
    ]

    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)

    opts = RunOptions(servers=["192.168.240.240"])
    verdicts = verify_devices([d1, d2], opts, wait=0, max_skew=2.0, anchor=None, reference_server=None)

    assert len(verdicts) == 2
    assert verdicts[0].verdict == SyncVerdict.SYNC
    assert verdicts[1].verdict == SyncVerdict.SYNC
    assert verdicts[0].skew_s == 1.0
    assert verdicts[1].skew_s == 1.0
    assert verdicts[0].offset_s is not None


def test_classify_with_reference_clock():
    dev = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    probe = ProbeResult(
        device=dev,
        ok=True,
        status=Status.OK,
        clock="Tue Oct 20 10:02:18 2026",
        servers=["192.168.240.240"],
    )
    t = parse_clock("Tue Oct 20 10:02:18 2026")
    # Fleet median matches dev, but reference_clock deviates by 5 seconds
    clocks = {"10.0.0.1": t, "10.0.0.2": t}
    ref_clock = t + 5.0
    v = classify(probe, ["192.168.240.240"], 2.0, clocks, reference_clock=ref_clock)
    assert v.verdict == SyncVerdict.UNSYNC

    # Reference clock within 1 second
    ref_ok = t + 1.0
    v_ok = classify(probe, ["192.168.240.240"], 2.0, clocks, reference_clock=ref_ok)
    assert v_ok.verdict == SyncVerdict.SYNC


def test_verify_devices_with_anchor_sync_and_skew(monkeypatch):
    anchor = Device(name="CORE5400", ip="172.16.13.1", model="Aruba5400", platform=Platform.ARUBOS_S)
    sw = Device(name="SW1", ip="172.16.12.1", model="Aruba2930", platform=Platform.ARUBOS_S)

    probes = [
        ProbeResult(
            device=anchor,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:18 2026",
            servers=["192.168.240.240"],
        ),
        ProbeResult(
            device=sw,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:19 2026",
            servers=["192.168.240.240"],
        ),
    ]
    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)
    t_anchor = parse_clock("Tue Oct 20 10:02:18 2026")
    from switch_sync.models import NtpResult
    mock_ntp = NtpResult(
        server="172.16.40.5",
        reachable=True,
        valid=True,
        stratum=4,
        root_delay_s=0.01,
        root_dispersion_s=0.01,
        offset_s=0.0,
        mode=4,
        refid="162.159.200.1",
        reference_time=t_anchor - 1.0,  # 1.0s deviation, within 120s tolerance
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda srv: mock_ntp)

    opts = RunOptions(servers=["192.168.240.240"])
    verdicts = verify_devices([anchor, sw], opts, wait=0, max_skew=2.0, anchor="172.16.13.1")

    assert len(verdicts) == 2
    # Anchor device has skew_s 0
    assert verdicts[0].device.ip == "172.16.13.1"
    assert verdicts[0].skew_s == 0.0
    assert verdicts[0].verdict == SyncVerdict.SYNC

    # Device sw has skew_s 1.0 against anchor
    assert verdicts[1].device.ip == "172.16.12.1"
    assert verdicts[1].skew_s == 1.0
    assert verdicts[1].verdict == SyncVerdict.SYNC


def test_verify_devices_with_anchor_detects_fleet_wide_wrong_clock(monkeypatch):
    anchor = Device(name="CORE5400", ip="172.16.13.1", model="Aruba5400", platform=Platform.ARUBOS_S)
    sw1 = Device(name="SW1", ip="172.16.12.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    sw2 = Device(name="SW2", ip="172.16.12.2", model="Aruba2930", platform=Platform.ARUBOS_S)

    probes = [
        ProbeResult(
            device=anchor,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:18 2026",
            servers=["192.168.240.240"],
        ),
        ProbeResult(
            device=sw1,
            ok=True,
            status=Status.OK,
            clock="Wed Feb  7 10:19:23 1990",
            servers=["192.168.240.240"],
        ),
        ProbeResult(
            device=sw2,
            ok=True,
            status=Status.OK,
            clock="Wed Feb  7 10:19:23 1990",
            servers=["192.168.240.240"],
        ),
    ]
    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)
    t_anchor = parse_clock("Tue Oct 20 10:02:18 2026")
    from switch_sync.models import NtpResult
    mock_ntp = NtpResult(
        server="172.16.40.5",
        reachable=True,
        valid=True,
        stratum=4,
        root_delay_s=0.01,
        root_dispersion_s=0.01,
        offset_s=0.0,
        mode=4,
        refid="162.159.200.1",
        reference_time=t_anchor - 1.0,
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda srv: mock_ntp)

    opts = RunOptions(servers=["192.168.240.240"])
    verdicts = verify_devices([anchor, sw1, sw2], opts, wait=0, max_skew=2.0, anchor="CORE5400")

    assert verdicts[0].verdict == SyncVerdict.SYNC
    assert verdicts[0].skew_s == 0.0

    assert verdicts[1].verdict == SyncVerdict.UNSYNC
    assert verdicts[1].skew_s > 1000000

    assert verdicts[2].verdict == SyncVerdict.UNSYNC
    assert verdicts[2].skew_s > 1000000


def test_verify_devices_anchor_not_in_inventory():
    d1 = Device(name="SW1", ip="10.0.0.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    opts = RunOptions(servers=["192.168.240.240"])
    with pytest.raises(ValueError, match="not found in inventory"):
        verify_devices([d1], opts, wait=0, anchor="172.16.13.1")


def test_verify_devices_anchor_unreachable(monkeypatch):
    anchor = Device(name="CORE5400", ip="172.16.13.1", model="Aruba5400", platform=Platform.ARUBOS_S)
    probes = [
        ProbeResult(
            device=anchor,
            ok=False,
            status=Status.UNREACHABLE,
            error="Connection timed out",
        )
    ]
    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)
    opts = RunOptions(servers=["192.168.240.240"])
    with pytest.raises(RuntimeError, match="unreachable"):
        verify_devices([anchor], opts, wait=0, anchor="CORE5400")


def test_verify_devices_anchor_no_readable_clock(monkeypatch):
    anchor = Device(name="CORE5400", ip="172.16.13.1", model="Aruba5400", platform=Platform.ARUBOS_S)
    probes = [
        ProbeResult(
            device=anchor,
            ok=True,
            status=Status.OK,
            clock=None,
            servers=["192.168.240.240"],
        )
    ]
    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)
    opts = RunOptions(servers=["192.168.240.240"])
    with pytest.raises(RuntimeError, match="no readable clock"):
        verify_devices([anchor], opts, wait=0, anchor="CORE5400")


def test_verify_anchor_exceeds_tolerance_fails_loudly(monkeypatch):
    from switch_sync.models import NtpResult

    anchor = Device(name="CORE5400", ip="172.16.13.1", model="Aruba5400", platform=Platform.ARUBOS_S)
    sw = Device(name="SW1", ip="172.16.12.1", model="Aruba2930", platform=Platform.ARUBOS_S)

    probes = [
        ProbeResult(
            device=anchor,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:18 2026",
            servers=["192.168.240.240"],
        ),
        ProbeResult(
            device=sw,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:18 2026",
            servers=["192.168.240.240"],
        ),
    ]
    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)

    t_anchor = parse_clock("Tue Oct 20 10:02:18 2026")
    # True time from reference server is 342s away (anchor is 342s fast)
    mock_ntp = NtpResult(
        server="172.16.40.5",
        reachable=True,
        valid=True,
        stratum=4,
        root_delay_s=0.01,
        root_dispersion_s=0.01,
        offset_s=0.0,
        mode=4,
        refid="162.159.200.1",
        reference_time=t_anchor - 342.0,
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda srv: mock_ntp)

    opts = RunOptions(servers=["192.168.240.240"])
    with pytest.raises(RuntimeError) as exc_info:
        verify_devices([anchor, sw], opts, wait=0, anchor="CORE5400", reference_server="172.16.40.5", anchor_max_skew=120.0)

    msg = str(exc_info.value)
    # Must be loud, name the anchor, the measured deviation, and the reference server
    assert "CORE5400" in msg or "172.16.13.1" in msg
    assert "342.0000s" in msg or "342" in msg
    assert "172.16.40.5" in msg
    assert "refusing to grade fleet" in msg


def test_verify_anchor_within_tolerance_grades_fleet(monkeypatch):
    from switch_sync.models import NtpResult

    anchor = Device(name="ASW01-HG4-2C", ip="172.16.13.208", model="Aruba2930", platform=Platform.ARUBOS_S)
    sw = Device(name="SW1", ip="172.16.12.1", model="Aruba2930", platform=Platform.ARUBOS_S)

    probes = [
        ProbeResult(
            device=anchor,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:18 2026",
            servers=["192.168.240.240"],
        ),
        ProbeResult(
            device=sw,
            ok=True,
            status=Status.OK,
            clock="Tue Oct 20 10:02:20 2026",
            servers=["192.168.240.240"],
        ),
    ]
    monkeypatch.setattr("switch_sync.runner.probe_devices", lambda devs, opts: probes)

    t_anchor = parse_clock("Tue Oct 20 10:02:18 2026")
    # True time from reference server is 9.3s away (within 120s tolerance)
    mock_ntp = NtpResult(
        server="172.16.40.5",
        reachable=True,
        valid=True,
        stratum=4,
        root_delay_s=0.01,
        root_dispersion_s=0.01,
        offset_s=0.0,
        mode=4,
        refid="162.159.200.1",
        reference_time=t_anchor + 9.3,
    )
    monkeypatch.setattr("switch_sync.ntp.query", lambda srv: mock_ntp)

    opts = RunOptions(servers=["192.168.240.240"])
    verdicts = verify_devices([anchor, sw], opts, wait=0, anchor="172.16.13.208", reference_server="172.16.40.5", anchor_max_skew=120.0)

    assert len(verdicts) == 2
    assert verdicts[0].verdict == SyncVerdict.SYNC
    assert verdicts[0].skew_s == 0.0
    assert verdicts[1].verdict == SyncVerdict.SYNC
    assert verdicts[1].skew_s == 2.0


def test_verify_defaults_anchor_and_threshold():
    import inspect
    sig = inspect.signature(verify_devices)
    assert sig.parameters["anchor"].default == "172.16.13.208"
    assert sig.parameters["reference_server"].default == "172.16.40.5"
    assert sig.parameters["max_skew"].default == 120.0
    assert sig.parameters["anchor_max_skew"].default == 120.0


