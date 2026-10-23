import re
import time
from collections.abc import Sequence
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoBaseException, ReadTimeout
from switch_sync.models import Device, Platform
from switch_sync.platforms import netmiko_device_type


class ConfigRejected(Exception):
    pass


class SessionError(Exception):
    pass


class DeviceSession:
    def __init__(
        self,
        device: Device,
        username: str,
        password: str,
        timeout: int = 20,
        retries: int = 3,
        backoff: Sequence[int] = (2, 4, 6),
    ):
        self.device = device
        self.username = username
        self.password = password
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._conn = None
        self._prompt = ""
        self.last_unattributed = False

    def __enter__(self) -> "DeviceSession":
        if self._conn is None:
            self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _connect(self) -> None:
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                self._conn = ConnectHandler(
                    device_type=netmiko_device_type(self.device.platform),
                    host=self.device.ip,
                    username=self.username,
                    password=self.password,
                    fast_cli=False,
                    conn_timeout=self.timeout,
                    auth_timeout=self.timeout,
                    banner_timeout=self.timeout,
                    read_timeout_override=30,
                )
                self._conn.write_channel("\n")
                self._prompt = self._conn.find_prompt()
                if self.device.platform in (Platform.ARUBOS_S, Platform.PROCURVE_LEGACY):
                    self._conn.write_channel("no page\n")
                return
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, ReadTimeout):
                    break
                if attempt < self.retries:
                    delay = (
                        self.backoff[attempt]
                        if attempt < len(self.backoff)
                        else (self.backoff[-1] if self.backoff else 0)
                    )
                    if delay > 0:
                        time.sleep(delay)
                else:
                    break

        if last_exc:
            msg = f"{self.device.ip}: {type(last_exc).__name__}"
            try:
                raise type(last_exc)(msg) from None
            except Exception:
                raise SessionError(msg) from None

    def _read_until_prompt(self, command: str = "", timeout: float | None = None) -> str:
        if timeout is None:
            timeout = self.timeout
        prompt = self._prompt or self._conn.find_prompt()
        prompt_base = prompt.rstrip("#>").strip()

        cmd_clean = command.strip()
        cmd_pattern = None
        if cmd_clean:
            cmd_pattern = re.compile(
                rf"(?:^|\r?\n)(?:[ \t]*|[^\r\n]*?[#>][ \t]*){re.escape(cmd_clean)}[ \t]*(?:\r?\n|$)"
            )

        start_time = time.time()
        buf = ""

        while time.time() - start_time < timeout:
            chunk = self._conn.read_channel()
            if chunk:
                buf += chunk
                cleaned = self._clean_raw(buf)
                if cmd_pattern:
                    matches = list(cmd_pattern.finditer(cleaned))
                    if matches:
                        after_echo = cleaned[matches[-1].end():].rstrip()
                        last_line = after_echo.splitlines()[-1] if after_echo else ""
                        if after_echo.endswith(prompt) or (
                            last_line.endswith(("#", ">"))
                            and (not prompt_base or prompt_base in last_line)
                        ):
                            break
                else:
                    cleaned_probe = cleaned.rstrip()
                    last_line = cleaned_probe.splitlines()[-1] if cleaned_probe else ""
                    if cleaned_probe.endswith(prompt) or (
                        last_line.endswith(("#", ">"))
                        and (not prompt_base or prompt_base in last_line)
                    ):
                        break
            time.sleep(0.05)

        return buf

    def send(self, command: str) -> str:
        if self._conn is None:
            self._connect()

        self._conn.write_channel(command + "\n")

        prompt = self._prompt or self._conn.find_prompt()
        buf = self._read_until_prompt(command=command)

        return self._format_output(buf, command, prompt)

    def _clean_raw(self, text: str) -> str:
        clean = text.replace("\x00", "")
        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", clean)
        clean = re.sub(r"\x1b\([a-zA-Z]", "", clean)
        clean = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", clean)
        return clean

    def _format_output(self, text: str, command: str, prompt: str) -> str:
        clean = self._clean_raw(text).rstrip()

        # Step 4 & 6: search for echoed command
        cmd_clean = command.strip()
        last_idx = -1
        if cmd_clean:
            pattern = re.compile(
                rf"(?:^|\r?\n)(?:[ \t]*|[^\r\n]*?[#>][ \t]*){re.escape(cmd_clean)}[ \t]*(?:\r?\n|$)"
            )
            matches = list(pattern.finditer(clean))
            if matches:
                last_match = matches[-1]
                last_idx = last_match.end()

        if last_idx != -1:
            clean = clean[last_idx:]
            self.last_unattributed = False
        else:
            self.last_unattributed = True

        # Step 5: remove trailing prompt
        clean = clean.rstrip()
        if clean.endswith(prompt):
            clean = clean[: -len(prompt)].rstrip()
        else:
            last_line = clean.splitlines()[-1] if clean else ""
            if last_line.endswith(("#", ">")):
                clean = clean[: -len(last_line)].rstrip()

        return "\n".join(clean.splitlines()).strip()

    def send_lines(self, lines: list[str], delay: float = 0.35) -> str:
        collected: list[str] = []
        for line in lines:
            out = self.send(line)
            collected.append(out)
            self._check_rejected(out)
            if delay > 0:
                time.sleep(delay)
        return "\n".join(collected)

    def _check_rejected(self, output: str) -> None:
        if not output:
            return
        lower = output.lower()
        for pattern in ("invalid input", "% invalid", "error", "syntax error"):
            if pattern in lower:
                safe_out = output.strip()
                if self.password and self.password in safe_out:
                    safe_out = safe_out.replace(self.password, "******")
                raise ConfigRejected(f"{self.device.ip}: Config rejected: {safe_out}")

    def enable(self) -> None:
        if self._conn is None:
            self._connect()
        prompt = self._conn.find_prompt()
        if prompt.endswith("#"):
            self._prompt = prompt
            return
        if prompt.endswith(">"):
            old_prompt = self._prompt or prompt
            self._prompt = "#"
            try:
                self.send("enable")
            finally:
                new_prompt = self._conn.find_prompt()
                if new_prompt.endswith("#"):
                    self._prompt = new_prompt
                else:
                    self._prompt = old_prompt.rstrip(">") + "#"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.disconnect()
            except Exception:
                pass
            finally:
                self._conn = None


def connect(device: Device, username: str, password: str, **kw) -> DeviceSession:
    session = DeviceSession(device, username, password, **kw)
    session._connect()
    return session
