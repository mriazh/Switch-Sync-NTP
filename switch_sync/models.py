from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    ARUBOS_S = "arubos-s"
    ARUBOS_CX = "arubos-cx"
    PROCURVE_LEGACY = "procurve-legacy"
    ARUBOS_MOBILITY = "arubos-mobility"
    UNKNOWN = "unknown"


class Status(str, Enum):
    OK = "OK"
    OK_ALREADY = "OK-ALREADY"
    NEEDS_CHANGE = "NEEDS-CHANGE"
    SKIP_UNSUPPORTED = "SKIP-UNSUPPORTED"
    SKIP_PRIVILEGE = "SKIP-PRIVILEGE"
    SKIP_UNKNOWN_PLATFORM = "SKIP-UNKNOWN-PLATFORM"
    SKIP_PROTECTED = "SKIP-PROTECTED"
    SKIP_CONTROLLER = "SKIP-CONTROLLER"
    CONNECT_FAIL = "CONNECT-FAIL"
    AUTH_FAIL = "AUTH-FAIL"
    CONFIG_FAIL = "CONFIG-FAIL"
    VERIFY_FAIL = "VERIFY-FAIL"
    UNREACHABLE = "UNREACHABLE"
    DATA_WARN = "DATA-WARN"


class SyncVerdict(str, Enum):
    SYNC = "SYNC"
    UNSYNC = "UNSYNC"
    NO_SERVER = "NO-SERVER"
    UNREACHABLE = "UNREACHABLE"


@dataclass
class Device:
    name: str
    ip: str
    model: str
    platform: Platform
    password: str = ""
    username: Optional[str] = None
    firmware: Optional[str] = None
    excel_hostname: Optional[str] = None
    label: Optional[str] = None
    member: Optional[str] = None
    vlan_mgmt: Optional[str] = None
    is_controller: bool = False
    protected: bool = False
    notes: list[str] = field(default_factory=list)
    poll_interval: Optional[int] = None
    sync_mode: Optional[str] = None


@dataclass
class ProbeResult:
    device: Device
    ok: bool = False
    error: Optional[str] = None
    status: Status = Status.CONNECT_FAIL
    raw: dict[str, str] = field(default_factory=dict)
    firmware: Optional[str] = None
    sync_status: Optional[str] = None
    stratum: Optional[int] = None
    servers: list[str] = field(default_factory=list)
    entries: list[tuple[int, str]] = field(default_factory=list)
    timezone: Optional[str] = None
    clock: Optional[str] = None
    sntp_supported: Optional[bool] = None
    privilege_ok: bool = False
    platform_detected: Optional[Platform] = None
    poll_interval: Optional[int] = None
    sync_mode: Optional[str] = None


@dataclass
class PlanItem:
    device: Device
    status: Status
    reason: str
    commands: list[str] = field(default_factory=list)
    needs_change: bool = False
    servers_before: list[str] = field(default_factory=list)
    entries_before: list[tuple[int, str]] = field(default_factory=list)
    timezone_before: Optional[str] = None
    rollback: list[str] = field(default_factory=list)
    timesync_mode: str = "keep"


@dataclass
class ApplyResult:
    device: Device
    status: Status
    commands: list[str] = field(default_factory=list)
    clock_before: Optional[str] = None
    clock_after: Optional[str] = None
    wrote: bool = False
    dry_run: bool = False
    output: str = ""
    error: Optional[str] = None


@dataclass
class Verdict:
    device: Device
    verdict: SyncVerdict
    stratum: Optional[int] = None
    offset_s: Optional[float] = None
    skew_s: Optional[float] = None
    servers: list[str] = field(default_factory=list)
    detail: str = ""
    poll_interval: Optional[int] = None
    clock: Optional[str] = None
    reference_server: Optional[str] = None
    reference_stratum: Optional[int] = None
    reference_refid: Optional[str] = None
    anchor_deviation: Optional[float] = None
    anchor_max_skew: Optional[float] = None


@dataclass
class NtpResult:
    server: str
    reachable: bool
    valid: bool
    stratum: Optional[int]
    root_delay_s: Optional[float]
    root_dispersion_s: Optional[float]
    offset_s: Optional[float]
    mode: Optional[int]
    refid: Optional[str]
    error: Optional[str] = None
    reference_time: Optional[float] = None
    clock_skew: Optional[float] = None


@dataclass
class RunOptions:
    workers: int = 6
    timeout: int = 20
    retries: int = 3
    servers: list[str] = field(default_factory=list)
    tz_minutes: int = 420
    tz_name: Optional[str] = "Asia/Jakarta"
    cx_vrf: Optional[str] = None
    apply: bool = False
    allow_protected: bool = False
    allow_controller_apply: bool = False
    include_controllers: bool = False
    out_dir: str = "out"
    user: Optional[str] = None
    dry_run: bool = True
    timesync_mode: str = "keep"
