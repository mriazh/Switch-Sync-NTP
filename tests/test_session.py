from collections.abc import Sequence
from unittest.mock import MagicMock, patch
import pytest

from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from switch_sync.models import Device, Platform
from switch_sync.session import DeviceSession, ConfigRejected, connect


class FakeChannel:
    def __init__(self, prompt: str = "ASW01-MD-DC#"):
        self.prompt = prompt
        self.written: list[str] = []
        self._read_queue: list[str] = []
        self.disconnected = False

    def write_channel(self, text: str) -> None:
        self.written.append(text)

    def read_channel(self) -> str:
        if self._read_queue:
            return self._read_queue.pop(0)
        return ""

    def find_prompt(self) -> str:
        return self.prompt

    def disconnect(self) -> None:
        self.disconnected = True

    def queue_response(self, text: str) -> None:
        self._read_queue.append(text)


@pytest.fixture
def sample_device():
    return Device(
        name="ASW01-MD-DC",
        ip="172.16.12.246",
        model="Aruba2930",
        platform=Platform.ARUBOS_S,
        password="SUPER_SECRET_P@SSW0RD",
    )


def test_ansi_escape_stripping(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    # Output with ANSI color escapes and control codes
    raw = "show time\r\n\x1b[2K\x1b[32mTue Oct 20 10:02:18 2026\x1b[0m\r\nASW01-MD-DC#"
    fake.queue_response(raw)

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show time")

    assert out == "Tue Oct 20 10:02:18 2026"
    assert "\x1b" not in out


def test_nul_byte_stripping(sample_device, monkeypatch):
    fake = FakeChannel(prompt="SW01-UPG-2520#")
    # Output containing NUL bytes between lines as seen on ProCurve 2520
    raw = "show sntp\r\ntimesync sntp\r\n\x00sntp unicast\r\nSW01-UPG-2520#"
    fake.queue_response(raw)

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Procurve2520", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show sntp")

    assert "\x00" not in out
    assert "timesync sntp\nsntp unicast" in out


def test_echoed_command_and_prompt_removal(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    raw = "show clock\r\nTue Oct 20 10:02:18 2026\r\nASW01-MD-DC#"
    fake.queue_response(raw)

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show clock")

    assert out == "Tue Oct 20 10:02:18 2026"
    assert "show clock" not in out
    assert "ASW01-MD-DC#" not in out


def test_prompt_detection_multichunk(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    # Feed data in multiple chunks across read_channel calls
    fake.queue_response("show time\r\nTue Oct 20")
    fake.queue_response(" 10:02:18 2026\r\n")
    fake.queue_response("ASW01-MD-DC#")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show time")

    assert out == "Tue Oct 20 10:02:18 2026"


def test_enable_path_for_prompt_ending_in_gt(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC>")
    # Response to 'enable' changes prompt to '#'
    fake.queue_response("enable\r\nASW01-MD-DC#")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        session.enable()
        fake.prompt = "ASW01-MD-DC#"

    assert any("enable" in cmd for cmd in fake.written)


def test_enable_noop_when_already_privileged(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        written_before = list(fake.written)
        session.enable()

    # No enable command sent since prompt already ended with #
    assert "enable\n" not in fake.written[len(written_before):]


def test_send_lines_config_rejected(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    fake.queue_response("configure\r\nASW01-MD-DC(config)#")
    fake.queue_response("sntp bad_command\r\nInvalid input: bad_command\r\nASW01-MD-DC(config)#")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        with pytest.raises(ConfigRejected) as exc_info:
            session.send_lines(["configure", "sntp bad_command"])

        assert "Invalid input" in str(exc_info.value) or "rejected" in str(exc_info.value).lower()


def test_retry_count_on_connection_error(sample_device, monkeypatch):
    call_count = 0

    def mock_connect(**kw):
        nonlocal call_count
        call_count += 1
        raise NetmikoTimeoutException("Connection timed out")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", mock_connect)
    monkeypatch.setattr("time.sleep", lambda s: None)

    with pytest.raises(Exception):
        session = DeviceSession(
            sample_device,
            "Aruba2930",
            "SUPER_SECRET_P@SSW0RD",
            timeout=5,
            retries=3,
            backoff=(0, 0, 0),
        )
        with session:
            pass

    # 1 initial try + 3 retries = 4 attempts
    assert call_count == 4


def test_password_never_in_exception(sample_device, monkeypatch):
    secret_pass = "SUPER_SECRET_P@SSW0RD"

    def mock_connect(**kw):
        raise NetmikoAuthenticationException(
            f"Authentication failed for user Aruba2930 with password {secret_pass}"
        )

    monkeypatch.setattr("switch_sync.session.ConnectHandler", mock_connect)
    monkeypatch.setattr("time.sleep", lambda s: None)

    session = DeviceSession(
        sample_device,
        "Aruba2930",
        secret_pass,
        timeout=5,
        retries=1,
        backoff=(0,),
    )

    with pytest.raises(Exception) as exc_info:
        with session:
            pass

    err_msg = str(exc_info.value)
    assert secret_pass not in err_msg
    assert sample_device.ip in err_msg
    assert "NetmikoAuthenticationException" in err_msg


class DefectTransport:
    """Fake netmiko transport that reproduces the defect where leftover output of the
    previous command is delivered on the next command write."""
    def __init__(self, leftover: str = "old sntp output\r\n", prompt: str = "ASW01-MD-DC#"):
        self.leftover = leftover
        self.prompt = prompt
        self.written: list[str] = []
        self._queue: list[str] = []
        self.disconnected = False

    def write_channel(self, text: str) -> None:
        self.written.append(text)
        cmd = text.strip()
        if cmd and cmd != "no page":
            self._queue.append(f"{self.leftover}{self.prompt}\r\n{cmd}\r\nTime Zone : 420\r\n{self.prompt}")

    def read_channel(self) -> str:
        if self._queue:
            return self._queue.pop(0)
        return ""

    def find_prompt(self) -> str:
        return self.prompt

    def disconnect(self) -> None:
        self.disconnected = True


def test_channel_leak_previous_command_output(sample_device, monkeypatch):
    fake = DefectTransport(leftover="old sntp output\r\n", prompt="ASW01-MD-DC#")
    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show system")

    assert "old sntp output" not in out
    assert out == "Time Zone : 420"
    assert session.last_unattributed is False


def test_prompt_before_echo_does_not_end_read(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    # First chunk is just a stale prompt from earlier
    fake.queue_response("stale prompt\r\nASW01-MD-DC#\r\n")
    # Second chunk contains the actual echo and result
    fake.queue_response("show clock\r\nTue Oct 20 10:02:18 2026\r\nASW01-MD-DC#")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show clock")

    assert out == "Tue Oct 20 10:02:18 2026"
    assert "stale prompt" not in out
    assert session.last_unattributed is False


def test_command_n_never_contains_command_n_minus_1(sample_device, monkeypatch):
    class BackToBackTransport(FakeChannel):
        def write_channel(self, text: str) -> None:
            self.written.append(text)
            cmd = text.strip()
            if cmd == "show version":
                self._read_queue.append("show version\r\nImage: WC.16.05.0004\r\nASW01-MD-DC#")
            elif cmd == "show sntp":
                self._read_queue.append("WC.16.05.0004\r\nASW01-MD-DC#\r\nshow sntp\r\nSNTP Mode : Unicast\r\nASW01-MD-DC#")

    fake = BackToBackTransport(prompt="ASW01-MD-DC#")
    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out1 = session.send("show version")
        out2 = session.send("show sntp")

    assert "WC.16.05.0004" in out1
    assert "SNTP Mode : Unicast" in out2
    assert "WC.16.05.0004" not in out2
    assert "show version" not in out2
    assert session.last_unattributed is False


def test_command_not_echoed_unattributed(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    # Device outputs result directly without echoing the command line
    fake.queue_response("Direct output without echo\r\nASW01-MD-DC#")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD", timeout=0.1) as session:
        out = session.send("show system")

    assert out == "Direct output without echo"
    assert session.last_unattributed is True


def test_no_page_leak_into_first_command(sample_device, monkeypatch):
    fake = FakeChannel(prompt="ASW01-MD-DC#")
    # Emulate connect where 'no page' produces output sitting in channel
    fake.queue_response("no page output in channel\r\nASW01-MD-DC#\r\nshow time\r\nTue Oct 20 10:02:18 2026\r\nASW01-MD-DC#")

    monkeypatch.setattr("switch_sync.session.ConnectHandler", lambda **kw: fake)

    with connect(sample_device, "Aruba2930", "SUPER_SECRET_P@SSW0RD") as session:
        out = session.send("show time")

    assert "no page" not in out
    assert out == "Tue Oct 20 10:02:18 2026"
    assert session.last_unattributed is False

