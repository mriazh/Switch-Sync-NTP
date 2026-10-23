# Switch-Sync-NTP

Fleet NTP audit, configuration, verification, and reporting operating manual for ArubaOS-S, ArubaOS-CX, ProCurve legacy switches, and Aruba Mobility Controllers.

## Scope

- Audits, plans, applies, and verifies NTP and timezone configuration across 104 fleet network devices against an absolute clock anchor.
- Captures per-device running and startup configuration backups before any write and provides rollback commands in plan outputs.
- Does not configure or alter STP, loop-protect, storm-control, VLANs, routing, or switch firmware.
- Does not reboot switches, erase configurations, transfer configs between devices, or distribute time using non-NTP protocols.

## Requirements and Installation

Exact versions in use:
- Python 3.14.3
- netmiko 4.6.0
- openpyxl 3.1.5
- pytest

Installation:
```sh
pip install -r requirements.txt
```

## Command Reference

### Global Options
- `--config <path>`: Inventory path (default: `config/switch.txt`).
- `--excel <path>`: Master spreadsheet path (default: `C:\Users\adima\OneDrive\Dokumen\Network-Riyadh\Switch-Master.xlsx`).
- `--source {switch,excel,both}`: Inventory data source (default: `switch`).
- `--user <username>`: Explicit username override across connection attempts.
- `--out-dir <dir>`: Base output directory for artifacts (default: `out`).
- `--timeout <seconds>`: Per-device SSH connection and command timeout (default: `20`).
- `--retries <count>`: Number of connection retry attempts (default: `3`).

### Subcommands

#### `preflight`
Queries an NTP server over UDP/123 and verifies reachability, stratum, mode, and timestamps before pointing fleet devices at it.
```sh
python -m switch_sync preflight --server <IP> [--timeout 5.0] [--json]
```

#### `probe`
Audits fleet inventory or connects read-only to query firmware, clock, SNTP/NTP status, and running configuration without making any changes.
```sh
python -m switch_sync probe [--list] [--workers 6] [--out out/probe.csv] [--only ...] [--exclude ...]
```
- `--list`: Prints parsed inventory devices and flags controllers without connecting.
- `--workers <1-12>`: Concurrent worker threads (default: 6, max: 12).
- `--only <hosts|IPs>`: Filter targets by comma-separated hostnames or IP addresses.
- `--exclude <hosts|IPs>`: Exclude comma-separated hostnames or IP addresses.

#### `plan`
Generates target NTP and timezone configuration commands per platform and emits rollback commands without modifying devices.
```sh
python -m switch_sync plan --server <IP> --tz 420 [--tz-name Asia/Jakarta] [--cx-vrf default] [--workers 6] [--out out/plan.json] [--only ...] [--exclude ...]
```
- `--server <IP>`: Authoritative target NTP server IP.
- `--tz <minutes>`: Timezone offset in minutes (default: 420 for UTC+7).
- `--tz-name <name>`: Timezone name for ArubaOS-CX (e.g. `Asia/Jakarta`).
- `--cx-vrf <vrf>`: VRF for ArubaOS-CX NTP commands (default: `default`).
- `--workers <1-12>`: Concurrent probe workers.
- `--out <path>`: Output path for plan JSON.

#### `apply`
Applies planned NTP configuration. Defaults to dry-run mode, printing commands that would be executed. Live writes require `--apply`.
```sh
python -m switch_sync apply --server <IP> --tz 420 [--apply] [--allow-protected] [--include-controllers] [--allow-controller-apply] [--workers 6] [--out out/ntp_result.csv] [--only ...] [--exclude ...]
```
- `--apply`: Enables live write execution (without this flag, dry-run only).
- `--allow-protected`: Grants explicit permission to configure protected core switches.
- `--include-controllers`: Includes Aruba Mobility Controllers in evaluation.
- `--allow-controller-apply`: Allows configuration writes to controllers (plan-only by default).
- `--workers <1-12>`: Concurrent apply workers.
- `--out <path>`: Output path for results CSV.

#### `verify`
Audits clock synchronization against an absolute anchor or median, evaluates deviation thresholds, and writes verification verdicts.
```sh
python -m switch_sync verify [--anchor 172.16.13.1] [--wait 120] [--max-skew 2.0] [--workers 6] [--out out/verify.csv] [--only ...] [--exclude ...]
```
- `--anchor <IP|host>`: Authoritative reference clock switch (e.g. `172.16.13.1`).
- `--wait <seconds>`: Delay before polling devices to allow poll cycles to elapse (default: 120).
- `--max-skew <seconds>`: Maximum allowable clock deviation in seconds (default: 2.0).
- `--workers <1-12>`: Concurrent verify workers.
- `--out <path>`: Output path for verify CSV.

#### `report`
Scans the output directory for the latest run artifacts by family, calculates totals, and generates a Markdown report.
```sh
python -m switch_sync report [--dir out] [--out out/report.md]
```
- `--dir <dir>` / `--out-dir <dir>`: Directory containing run artifacts (default: `out`).
- `--out <path>`: Markdown report output path (default: `<dir>/report.md`).

## Safety Rules

- **Dry-run default**: `apply` performs zero configuration writes unless `--apply` is explicitly passed.
- **Mandatory configuration backup**: Prior to sending any configuration lines, `apply` captures `show config`/`show startup-config` and `show running-config` into `out/backup/<host>-startup.txt` and `out/backup/<host>-running.txt`.
- **Privilege guard (`SKIP-PRIVILEGE`)**: If `show running-config` cannot be read or returns empty, the device is marked `SKIP-PRIVILEGE` and write commands are blocked.
- **Protected cores guard (`SKIP-PROTECTED`)**: Core switches (`172.16.13.1`, `172.21.255.1`, `172.21.255.2`, `172.21.255.6`) are blocked from writes unless `--allow-protected` is passed.
- **Controllers guard (`SKIP-CONTROLLER`)**: Aruba Mobility Controllers (`172.16.21.5`, `172.16.21.6`, `172.16.21.7`) are blocked from writes unless `--include-controllers` and `--allow-controller-apply` are passed.
- **Zero credential leakage**: Passwords and secrets never appear in output files, console logs, exception messages, file names, or version control.

## Platform Syntax and Measured Fleet Behavior

- **Working removal syntax**: `no sntp server priority N IP` is the only syntax accepted by ArubaOS-S / ProCurve.
- **Rejected removal forms**:
  - `no sntp server priority N` (without IP) is rejected with `Incomplete input: 1`.
  - `no sntp server IP` (without priority) is rejected.
- **Idempotency collision**: Adding an NTP server that is already configured without removing it first is rejected with `SNTP Server Address or Priority already configured.`.
- **Time sync mode switch**: `timesync sntp` switches devices out of `TIMEP/SNTP` into `SNTP`.
- **Fleet poll interval**: The fleet poll interval is 720 seconds (12 minutes). After configuration is applied, devices require up to 12 minutes before sending their first SNTP request.

## Pilot Procedure

The pilot must be executed one device at a time using `--workers 1`, with verification performed after each device:

1. Validate the NTP server reachability and time service:
   ```sh
   python -m switch_sync preflight --server 192.168.240.240
   ```
2. Run read-only capability probe on the target pilot switch:
   ```sh
   python -m switch_sync probe --only SW-MD-L2-UNTA --workers 1
   ```
3. Generate configuration plan and rollback commands:
   ```sh
   python -m switch_sync plan --server 192.168.240.240 --tz 420 --only SW-MD-L2-UNTA --workers 1
   ```
4. Perform dry-run apply to review generated CLI commands:
   ```sh
   python -m switch_sync apply --server 192.168.240.240 --tz 420 --only SW-MD-L2-UNTA --workers 1
   ```
5. Apply configuration with live write and automatic backup:
   ```sh
   python -m switch_sync apply --server 192.168.240.240 --tz 420 --only SW-MD-L2-UNTA --workers 1 --apply
   ```
6. Wait at least 720 seconds (the measured fleet poll interval).
7. Verify clock synchronization against the anchor:
   ```sh
   python -m switch_sync verify --anchor 172.16.13.1 --wait 720 --max-skew 2.0 --only SW-MD-L2-UNTA --workers 1
   ```
8. Generate summary report from run artifacts:
   ```sh
   python -m switch_sync report --dir out
   ```

## Verification Method and Absolute Anchor

- **Absolute anchor**: The Aruba 5400R switch at `172.16.13.1` (`CORE DISTRI HG4`) serves as the fleet reference clock. It has an accurate 2026 clock synchronized to `192.168.240.240` from its own segment.
- **Threshold**: Maximum allowed clock deviation is 2.0 seconds (`--max-skew 2.0`).
- **Fleet median limitation**: The fleet median is not a reliable reference. If an entire access segment is unsynchronized and reads 1990, the median deviation across those switches would falsely report 0.0 seconds skew.
- **Workstation clock limitation**: The engineering workstation clock is unsynchronized (+14.85s to -25529s offset against NTP/anchor) and is not trusted.

## Open Decisions

1. **Authoritative NTP source**: Confirmation of target server IP and network team approval for routing/ACL rules.
2. **Timezone**: Default target is 420 (UTC+7 / WIB, matching probed switches); 180 (UTC+3 / Riyadh) remains an alternate option.
3. **Write accounts**: Verification that administrator accounts have privilege to execute `write memory` across all 104 switches.
4. **Core devices and controllers**: Formal approval to apply changes to core switches (`172.16.13.1`, `172.21.255.1/2/6`) and mobility controllers (`172.16.21.5/6/7`).

## NTP Source Blocker and Survey

Measured NTP query survey from the engineering workstation and switches:

| Host | Answers UDP/123 | Stratum | RefID | Reachable from 172.16.12.0/24 | Usable as Time Source |
|---|---|---|---|---|---|
| `192.168.240.240` | Yes | 2 | `86.77.84.80` | No (ICMP timed out from switches) | Yes (healthy source, but unreachable from access VLAN) |
| `172.16.40.5` | Yes | 4 | `162.159.200.1` | No (ICMP timed out; SNTP unfulfilled) | Yes (healthy upstream, but unreachable from access VLAN) |
| `192.168.240.215` | Yes | 0 | `INIT` | Yes (2 ms) | No (stratum 0, unsynchronized) |
| `172.16.12.8` | Yes | 0 | `INIT` | Yes (Local) | No (stratum 0, unsynchronized) |
| `172.16.12.9` | Yes | 0 | `INIT` | Yes (Local) | No (stratum 0, unsynchronized) |
| `172.16.12.120` | Yes | 0 | `INIT` | Yes (Local) | No (stratum 0, unsynchronized) |
| `172.16.21.5` | Yes | 0 | `INIT` | Yes (2 ms) | No (stratum 0, unsynchronized) |
| `172.16.21.7` (DC) | No | – | – | Yes (2 ms) | No (does not answer UDP/123) |
| `172.16.13.1` | No | – | – | Yes (2 ms) | No (NTP client only, connection reset) |

- **Access VLAN limitation**: `172.16.12.0/24` contains no synchronized NTP source and cannot route to any external synchronized source.
- **Decision taken**: Open firewall ACL for UDP/123 to `192.168.240.240`.
- **Source behavior**: `172.16.40.5` answers with inconsistent timestamps so offset cannot be computed from its reply.

## ACL Scope Table for Ticket

Management subnets containing the 104 fleet devices that require UDP/123 access to `192.168.240.240`:

| Subnet | Device Count |
|---|---|
| `172.16.12.0/24` | 58 |
| `172.16.13.0/24` | 24 |
| `172.16.21.0/24` | 3 |
| `172.16.57.0/24` | 2 |
| `172.16.59.0/24` | 1 |
| `172.16.61.0/24` | 2 |
| `172.16.62.0/24` | 1 |
| `172.16.66.0/24` | 1 |
| `172.16.67.0/24` | 1 |
| `172.16.69.0/24` | 1 |
| `172.16.73.0/24` | 2 |
| `172.16.74.0/24` | 1 |
| `172.21.255.0/24` | 3 |
| `192.168.40.0/24` | 1 |
| `192.168.163.0/24` | 2 |
| `192.168.238.0/24` | 1 |
| **Total** | **104** |

The ACL rule must permit UDP port 123 outbound from these source subnets to `192.168.240.240`. The probe command output determines which subnets hold unsynchronized devices.

## Troubleshooting

- **Raw CLI captures**: Stored under `out/raw/<ip>-<phase>.txt`.
- **Configuration backups**: Stored under `out/backup/<host>-running.txt` and `out/backup/<host>-startup.txt`.
- **Status codes and remediation**:
  - `UNREACHABLE`: TCP 22 connection timed out or network route unreachable. Check physical link, VLAN tagging, gateway routing, and workstation VPN connectivity.
  - `AUTH-FAIL`: SSH authentication rejected after trying MODEL-based usernames and fallback candidates. Verify device credentials, TACACS+ availability, or credentials in `config/switch.txt`.
  - `CONFIG-FAIL`: CLI command syntax error or rejected configuration command. Inspect raw outputs in `out/raw/` for exact error messages and verify platform-specific syntax.
  - `SKIP-PRIVILEGE`: Switch refused privilege escalation or running-config backup could not be captured. Ensure user account has manager privileges to execute `show running-config` and `write memory`.

