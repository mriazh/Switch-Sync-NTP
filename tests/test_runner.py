import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from switch_sync.models import (
    Device,
    Platform,
    Status,
    ProbeResult,
    PlanItem,
    ApplyResult,
    RunOptions,
)
from switch_sync.runner import (
    run_parallel,
    CircuitBreaker,
    PROTECTED_IPS,
    probe_devices,
    apply_devices,
)


class FakeSession:
    def __init__(self, device, username, password, **kwargs):
        self.device = device
        self.username = username
        self.password = password
        self.sent_commands = []
        self.sent_lines_list = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def send(self, command: str) -> str:
        self.sent_commands.append(command)
        # Any forbidden write attempt in probe or read path raises error
        if any(w in command for w in ("write memory", "sntp server", "ntp server")):
            raise RuntimeError(f"Forbidden command sent: {command}")

        if "show version" in command:
            if self.device.platform == Platform.ARUBOS_CX:
                return "Version : XL.10.04.3050"
            return "Image stamp: /ws/swbuild01/build\n WC.16.05.0004"
        if "show config" in command or "show startup-config" in command:
            return "startup-config content\nhostname test"
        if "show sntp" in command:
            return (
                "SNTP Configuration\n"
                "  Time Sync Mode: Sntp\n"
                "  SNTP Mode : Unicast\n"
                "  Priority SNTP Server Address\n"
                "  -------- -------------------\n"
                "  1        192.168.240.240\n"
            )
        if "show system" in command:
            return "Time Zone : 420\nSoftware revision : WC.16.05.0004"
        if "show running-config" in command:
            return "sntp server priority 1 192.168.240.240\ntime timezone 420"
        if "show ntp status" in command:
            return (
                "NTP : Enabled\n"
                "System time : Tue Oct 20 09:58:41 WIB 2026\n"
                "Synchronized with an NTP server.\n"
                "Stratum : 2\n"
            )
        if "show clock" in command:
            return (
                "System is configured for timezone : Asia/Jakarta\n"
                "Tue Oct 20 09:58:41 WIB 2026\n"
            )
        if "show time" in command:
            return "Tue Oct 20 10:02:18 2026"
        return ""

    def send_lines(self, lines: list[str], delay: float = 0.35) -> str:
        self.sent_lines_list.extend(lines)
        return "\n".join(lines)

    def close(self):
        self.closed = True


def test_circuit_breaker():
    cb = CircuitBreaker(threshold=5)
    assert not cb.tripped
    for _ in range(4):
        assert cb.record(False)
        assert not cb.tripped
    # 5th failure trips it
    assert not cb.record(False)
    assert cb.tripped

    # Reset
    assert cb.record(True)
    assert not cb.tripped


def test_run_parallel_workers_clamping():
    items = list(range(10))
    res = run_parallel(items, lambda x: x * 2, workers=100)
    assert res == [x * 2 for x in items]

    res_min = run_parallel(items, lambda x: x * 2, workers=0)
    assert res_min == [x * 2 for x in items]


def test_run_parallel_order_and_callback():
    items = [5, 4, 3, 2, 1]
    completed = []

    def cb(val):
        completed.append(val)

    res = run_parallel(items, lambda x: x + 10, workers=4, on_result=cb)
    assert res == [15, 14, 13, 12, 11]
    assert sorted(completed) == [11, 12, 13, 14, 15]


def test_protected_ips_constant():
    expected = {"172.16.13.1", "172.21.255.1", "172.21.255.2", "172.21.255.6"}
    assert set(PROTECTED_IPS) == expected


def test_probe_controller_excluded(monkeypatch):
    dev = Device(
        name="CTRL-1",
        ip="172.16.21.5",
        model="Aruba7200",
        platform=Platform.ARUBOS_MOBILITY,
        is_controller=True,
    )
    connect_mock = MagicMock()
    monkeypatch.setattr("switch_sync.session.connect", connect_mock)

    opts = RunOptions(include_controllers=False)
    results = probe_devices([dev], opts)

    assert len(results) == 1
    assert results[0].status == Status.SKIP_CONTROLLER
    assert not results[0].ok
    connect_mock.assert_not_called()


def test_probe_unknown_platform(monkeypatch):
    dev = Device(
        name="UNK-1",
        ip="172.16.99.1",
        model="GenericSwitch",
        platform=Platform.UNKNOWN,
    )
    connect_mock = MagicMock()
    monkeypatch.setattr("switch_sync.session.connect", connect_mock)

    opts = RunOptions()
    results = probe_devices([dev], opts)

    assert len(results) == 1
    assert results[0].status == Status.SKIP_UNKNOWN_PLATFORM
    connect_mock.assert_not_called()


def test_probe_circuit_breaker_trips_after_5_failures(monkeypatch):
    devices = [
        Device(name=f"SW-{i}", ip=f"10.0.0.{i}", model="Aruba2930", platform=Platform.ARUBOS_S)
        for i in range(1, 11)
    ]
    call_count = 0

    def mock_connect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise NetmikoTimeoutException("Host unreachable")

    monkeypatch.setattr("switch_sync.session.connect", mock_connect)

    opts = RunOptions(workers=1)
    results = probe_devices(devices, opts)

    assert len(results) == 10
    # Exactly 5 connection attempts before circuit breaker stopped the fleet
    assert call_count == 5
    for r in results:
        assert r.status == Status.UNREACHABLE


def test_probe_auth_fail(monkeypatch):
    dev = Device(name="SW-AUTH", ip="10.1.1.1", model="Aruba2930", platform=Platform.ARUBOS_S)

    def mock_connect(*args, **kwargs):
        raise NetmikoAuthenticationException("Auth failed")

    monkeypatch.setattr("switch_sync.session.connect", mock_connect)

    opts = RunOptions(workers=1)
    results = probe_devices([dev], opts)

    assert len(results) == 1
    assert results[0].status == Status.AUTH_FAIL
    assert not results[0].ok


def test_probe_privilege_gate(monkeypatch, tmp_path):
    dev = Device(name="SW-PRIV", ip="10.1.1.2", model="Aruba2930", platform=Platform.ARUBOS_S)

    class PrivFakeSession(FakeSession):
        def send(self, command: str) -> str:
            if "show running-config" in command:
                return "Invalid input: show running-config"
            return super().send(command)

    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: PrivFakeSession(*a, **k))

    opts = RunOptions(out_dir=str(tmp_path), workers=1)
    results = probe_devices([dev], opts)

    assert len(results) == 1
    assert results[0].status == Status.SKIP_PRIVILEGE
    assert not results[0].privilege_ok


def test_probe_platform_refinement(monkeypatch, tmp_path):
    # Device model says Aruba2930 (AOS-S), but firmware reports XL.10.04.3050 (AOS-CX)
    dev = Device(name="SW-REFINE", ip="10.1.1.3", model="Aruba2930", platform=Platform.ARUBOS_S)

    class CXFwSession(FakeSession):
        def send(self, command: str) -> str:
            if "show version" in command:
                return "Version : XL.10.04.3050"
            return super().send(command)

    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: CXFwSession(*a, **k))

    opts = RunOptions(out_dir=str(tmp_path), workers=1)
    results = probe_devices([dev], opts)

    assert len(results) == 1
    # Device itself is never modified
    assert dev.platform == Platform.ARUBOS_S
    # Detected platform reflects the firmware
    assert results[0].platform_detected == Platform.ARUBOS_CX


def test_probe_raw_file_saved_without_ansi(monkeypatch, tmp_path):
    dev = Device(name="SW-RAW", ip="10.1.1.4", model="Aruba2930", platform=Platform.ARUBOS_S)

    class AnsiSession(FakeSession):
        def send(self, command: str) -> str:
            if "show version" in command:
                return "\x1b[31mImage stamp:\x1b[0m WC.16.05.0004"
            return super().send(command)

    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: AnsiSession(*a, **k))

    opts = RunOptions(out_dir=str(tmp_path), workers=1)
    probe_devices([dev], opts)

    raw_file = tmp_path / "raw" / "10.1.1.4-probe.txt"
    assert raw_file.exists()
    content = raw_file.read_text(encoding="utf-8")
    assert "\x1b[" not in content
    assert "WC.16.05.0004" in content


def test_apply_dry_run_gate(monkeypatch):
    dev = Device(name="SW-DRY", ip="10.2.2.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update required",
        commands=["sntp server priority 1 192.168.240.240", "write memory"],
        needs_change=True,
    )
    connect_mock = MagicMock()
    monkeypatch.setattr("switch_sync.session.connect", connect_mock)

    opts = RunOptions(apply=False, dry_run=True, servers=["192.168.240.240"])
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    res = results[0]
    assert res.dry_run is True
    assert res.wrote is False
    assert res.status == Status.NEEDS_CHANGE
    assert "write memory" in res.commands[-1]
    # No connection made during dry-run
    connect_mock.assert_not_called()


def test_apply_protected_device_blocked(monkeypatch):
    dev = Device(
        name="CORE-1",
        ip="172.16.13.1",
        model="Aruba5400",
        platform=Platform.ARUBOS_S,
        protected=True,
    )
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["write memory"],
        needs_change=True,
    )
    connect_mock = MagicMock()
    monkeypatch.setattr("switch_sync.session.connect", connect_mock)

    opts = RunOptions(apply=True, allow_protected=False)
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    assert results[0].status == Status.SKIP_PROTECTED
    assert results[0].wrote is False
    connect_mock.assert_not_called()


def test_apply_controller_blocked(monkeypatch):
    dev = Device(
        name="CTRL-1",
        ip="172.16.21.5",
        model="Aruba7200",
        platform=Platform.ARUBOS_MOBILITY,
        is_controller=True,
    )
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["write memory"],
        needs_change=True,
    )
    connect_mock = MagicMock()
    monkeypatch.setattr("switch_sync.session.connect", connect_mock)

    opts = RunOptions(apply=True, allow_controller_apply=False)
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    assert results[0].status == Status.SKIP_CONTROLLER
    assert results[0].wrote is False
    connect_mock.assert_not_called()


def test_apply_real_write(monkeypatch, tmp_path):
    dev = Device(name="SW-APPLY", ip="10.3.3.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["configure", "sntp unicast", "write memory"],
        needs_change=True,
    )

    created_session = None

    def fake_connect(*a, **k):
        nonlocal created_session
        created_session = FakeSession(*a, **k)
        return created_session

    monkeypatch.setattr("switch_sync.session.connect", fake_connect)

    opts = RunOptions(apply=True, out_dir=str(tmp_path), servers=["192.168.240.240"])
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    res = results[0]
    assert res.status == Status.OK
    assert res.wrote is True
    assert res.clock_before is not None
    assert res.clock_after is not None
    assert created_session.sent_lines_list == ["configure", "sntp unicast", "write memory"]

    apply_file = tmp_path / "raw" / "10.3.3.1-apply.txt"
    assert apply_file.exists()

    startup_file = tmp_path / "backup" / "SW-APPLY-startup.txt"
    running_file = tmp_path / "backup" / "SW-APPLY-running.txt"
    assert startup_file.exists()
    assert running_file.exists()


def test_backup_before_write_enforced(monkeypatch, tmp_path):
    dev = Device(name="SW-GUARD", ip="10.3.3.2", model="Aruba2930", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["configure", "write memory"],
        needs_change=True,
    )

    class GuardedSession(FakeSession):
        def send_lines(self, lines: list[str], delay: float = 0.35) -> str:
            running_bk = tmp_path / "backup" / f"{self.device.name}-running.txt"
            startup_bk = tmp_path / "backup" / f"{self.device.name}-startup.txt"
            if not running_bk.exists() or not startup_bk.exists():
                raise RuntimeError("Write attempted before backup exists!")
            return super().send_lines(lines, delay)

    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: GuardedSession(*a, **k))

    opts = RunOptions(apply=True, out_dir=str(tmp_path), servers=["192.168.240.240"])
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    assert results[0].status == Status.OK
    assert results[0].wrote is True


def test_running_config_backup_denied_skips_privilege(monkeypatch, tmp_path):
    dev = Device(name="SW-NOPRIV", ip="10.3.3.3", model="Aruba2930", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["configure", "write memory"],
        needs_change=True,
    )

    class DeniedSession(FakeSession):
        def send(self, command: str) -> str:
            if "show running-config" in command:
                return "% Invalid input: running-config"
            return super().send(command)

        def send_lines(self, lines: list[str], delay: float = 0.35) -> str:
            raise RuntimeError("send_lines should not be called when backup denied!")

    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: DeniedSession(*a, **k))

    opts = RunOptions(apply=True, out_dir=str(tmp_path), servers=["192.168.240.240"])
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    res = results[0]
    assert res.status == Status.SKIP_PRIVILEGE
    assert res.wrote is False
    assert "running-config" in res.error.lower() or "rejected" in res.error.lower()


def test_backup_order_startup_first_running_second(monkeypatch, tmp_path):
    dev = Device(name="SW-ORDER", ip="10.3.3.4", model="Aruba2930", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["configure", "write memory"],
        needs_change=True,
    )

    created_sess = None

    def fake_connect(*a, **k):
        nonlocal created_sess
        created_sess = FakeSession(*a, **k)
        return created_sess

    monkeypatch.setattr("switch_sync.session.connect", fake_connect)

    opts = RunOptions(apply=True, out_dir=str(tmp_path), servers=["192.168.240.240"])
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    cmds = created_sess.sent_commands
    startup_idx = -1
    running_idx = -1
    for idx, c in enumerate(cmds):
        if "show config" in c:
            startup_idx = idx
        elif "show running-config" in c:
            running_idx = idx

    assert startup_idx != -1 and running_idx != -1
    assert startup_idx < running_idx


@pytest.mark.parametrize(
    "cmd,err_output",
    [
        ("no sntp server priority 1", "Incomplete input: 1"),
        ("no sntp server 192.168.240.240", "Invalid input: 192.168.240.240"),
        ("sntp server priority 1 192.168.240.240", "SNTP Server Address or Priority already configured."),
    ],
)
def test_apply_rejected_commands_result_in_config_fail(monkeypatch, tmp_path, cmd, err_output):
    from switch_sync.session import ConfigRejected
    dev = Device(name="SW-REJECT", ip="10.4.4.1", model="Aruba2530", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=["configure", cmd, "write memory"],
        needs_change=True,
    )

    class RejectSession(FakeSession):
        def send_lines(self, lines: list[str], delay: float = 0.35) -> str:
            # Raise ConfigRejected naming the rejected line
            raise ConfigRejected(f"{self.device.ip}: Config rejected on line '{cmd}': {err_output}")

    monkeypatch.setattr("switch_sync.session.connect", lambda *a, **k: RejectSession(*a, **k))

    opts = RunOptions(apply=True, out_dir=str(tmp_path), servers=["192.168.240.240"])
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    res = results[0]
    assert res.status == Status.CONFIG_FAIL
    assert res.status != Status.OK
    assert res.wrote is False
    assert cmd in res.error
    assert err_output in res.error


def test_dry_run_never_reports_ok(monkeypatch, tmp_path):
    dev = Device(name="SW-DRY", ip="10.4.4.2", model="Aruba2530", platform=Platform.ARUBOS_S)
    # Even if plan item has status Status.OK
    plan_item = PlanItem(
        device=dev,
        status=Status.OK,
        reason="Test",
        commands=["configure", "write memory"],
        needs_change=True,
    )

    opts = RunOptions(apply=False, dry_run=True, out_dir=str(tmp_path))
    results = apply_devices([dev], [plan_item], opts)

    assert len(results) == 1
    assert results[0].status != Status.OK
    assert results[0].dry_run is True
    assert results[0].wrote is False


def test_apply_timesync_mode_commands_fallback():
    dev = Device(name="SW-TS", ip="10.5.5.1", model="Aruba2930", platform=Platform.ARUBOS_S)
    plan_item = PlanItem(
        device=dev,
        status=Status.NEEDS_CHANGE,
        reason="NTP update",
        commands=[],  # Fallback generation in _apply_single_item
        needs_change=True,
    )
    # Default keep
    opts_keep = RunOptions(apply=False, dry_run=True, servers=["192.168.240.240"], timesync_mode="keep")
    res_keep = apply_devices([dev], [plan_item], opts_keep)[0]
    assert "timesync sntp" not in res_keep.commands
    assert "timesync ntp" not in res_keep.commands

    # ntp
    opts_ntp = RunOptions(apply=False, dry_run=True, servers=["192.168.240.240"], timesync_mode="ntp")
    res_ntp = apply_devices([dev], [plan_item], opts_ntp)[0]
    assert "timesync ntp" in res_ntp.commands

    # sntp
    opts_sntp = RunOptions(apply=False, dry_run=True, servers=["192.168.240.240"], timesync_mode="sntp")
    res_sntp = apply_devices([dev], [plan_item], opts_sntp)[0]
    assert "timesync sntp" in res_sntp.commands


