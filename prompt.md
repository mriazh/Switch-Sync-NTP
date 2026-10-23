# PROMPT — Switch-Sync-NTP

Bangun program Python untuk **menyeragamkan dan menyinkronkan waktu (NTP) seluruh perangkat jaringan GMF Aeroasia**, lalu memverifikasi hasilnya. Semua kode, test, dan output berada di repo ini (`Switch-Sync-NTP`).

---

## 1. Konteks masalah

- Ada ~104 perangkat L2/L3 (switch ArubaOS-S, ArubaOS-CX, ProCurve legacy) + 3 Mobility Controller.
- Jam di perangkat **tidak sinkron** dan tidak ada sumber waktu yang benar-benar dipakai.
- Server NTP yang tercatat di konfigurasi lama, `192.168.240.240`, **mati** (ping timeout dari ASW01, ESW01, UNTA). Artinya perangkat yang "punya konfigurasi SNTP" pun sebenarnya tidak sinkron.
- Verifikasi di 3 switch (`ASW01-MD-DC`, `ESW01-MD-DC`, `SW-MD-L2-UNTA`) menunjukkan:
  - ArubaOS-S: `timesync sntp` + `sntp unicast` + `sntp server priority 1 <ip>` + `time timezone 420`
  - ArubaOS-CX: `ntp server <ip> iburst` + `ntp enable` + `clock timezone <nama_zona>`
  - `show sntp` (ArubaOS-S) / `show ntp status` (CX) dipakai untuk verifikasi.
- Akun read-only (`netops-ro`) **tidak bisa** menulis konfigurasi dan tidak bisa `show running-config`. Program harus memakai kredensial per-perangkat dari `config/switch.txt`.

Tujuan akhir: satu perintah menjalankan **audit → rencana → apply → verifikasi → laporan** untuk seluruh fleet, dengan bukti sebelum/sesudah per perangkat.

---

## 2. Sumber data

### 2.1 `config/switch.txt` (kredensial + daftar perangkat)

Format per baris:

```
<NAMA> | <IP> | <MODEL> | <PASSWORD>
```

104 baris perangkat. Data **kotor**, parser harus tahan terhadap:

- spasi di sekitar `|`, termasuk `NAMA |IP` dan `NAMA | @pass` (baris 90–92, 80)
- baris kosong di akhir file
- nama dengan titik dan strip: `SW-T3.INT-G19-2530`, `ESW01-HG4-A3L2`
- password berbeda per keluarga: `***`, `***`, `***`, `***`, `***`, `***`, `***`, `***`, dan `***`
- username **tidak ada** di file → username default per model, lihat §3.3

**Kolom MODEL adalah sumber utama penentuan platform**, bukan IP atau nama host.

Nilai MODEL yang ada beserta pemetaannya:

| MODEL di file | Platform sebenarnya | Firmware | Keluarga CLI |
|---|---|---|---|
| `Aruba2930` | Aruba 2930F | `WC.16.01.0004` | ArubaOS-S |
| `Aruba2530` | Aruba 2530 | `YA.15.17` / `YA.15.19` / `YA.15.20` / `YB.15.04` | ArubaOS-S |
| `Aruba5400` | Aruba 5400R zl2 | `KB.16.01.0009` | ArubaOS-S (sama seperti 2930F) |
| `Aruba8400` | Aruba 8400 (CX) | `XL.10.04.3050` | ArubaOS-CX |
| `Procurve2530` | ProCurve 2530 | `YA.15.09` | ArubaOS-S |
| `Procurve2520` | ProCurve 2520 | `J.14.04` | ProCurve legacy |
| `Procurve2610` | ProCurve 2610 | `Y.11.xx` | ProCurve legacy |
| `Procurve2620` | ProCurve 2620 | `RA.15.11` | ArubaOS-S |
| `Aruba7200` | Aruba Mobility Controller 7200 (WLC, **bukan switch**) | ArubaOS 8 | ArubaOS (berbeda) |

Catatan penting: `Aruba5400` pada `CORE DISTRI HG4` (`172.16.13.1`) dan `FARM SWITCH` (`172.21.255.6`) adalah **5400R zl2**, bukan 2930M. Firmware `KB.16.01.0009` mengonfirmasi (K-series = 5400R).

### 2.2 `C:\Users\adima\OneDrive\Dokumen\Network-Riyadh\Switch-Master.xlsx` (inventaris, read-only)

Sheet `Switch Data`, header di baris 1, data A2:V162. Kolom yang dipakai:

| Kolom | Nama | Kegunaan |
|---|---|---|
| B | Label | nama alternatif |
| C | IP Address | kunci join dengan `switch.txt` |
| D | Hostname | kunci join utama |
| E | Member | nomor member VSF/stack |
| G | Model | **sering kosong** — jangan diandalkan |
| H | Firmware | penentu platform (lebih akurat dari MODEL) |
| P | VLAN Management | untuk mendeteksi jalur manajemen |
| T | Status | `success` hanya berarti login+read berhasil |

Aturan: 161 baris → **101 perangkat unik** (dedupe per `Hostname`; satu baris = satu member stack). Data ini dipakai sebagai *pelengkap*, bukan pengganti `switch.txt`.

### 2.3 Merge kedua sumber

- Default: `switch.txt` sebagai daftar utama (karena berisi password), Excel untuk melengkapi hostname/firmware.
- Flag `--source switch|excel|both`. Untuk `both`, union per IP; konflik hostname dilaporkan, **jangan diperbaiki otomatis**.
- Sudah diketahui ada label tertukar di Excel: `ASW01-HG3-L1CF` (`172.16.12.124`) dan `ASW02-HG3-L1CF` (`172.16.12.236`) — laporkan sebagai `DATA_WARN`, jangan diubah.

---

## 3. Matriks perintah per platform

Semua perintah dijalankan dari **enable/privileged mode** (`#`), bukan `>`.

### 3.1 Deteksi platform

Urutan prioritas:

1. `MODEL` dari `switch.txt`
2. `Firmware` dari Excel (`XL` → CX, `KB`/`WC`/`YA`/`YB`/`RA` → ArubaOS-S, `J.`/`Y.` → ProCurve legacy)
3. Probe CLI setelah login (lihat §3.4)

### 3.2 Template konfigurasi

**ArubaOS-S** (2930F, 2530, 5400R, 2620):

```
configure
timesync sntp
sntp unicast
no sntp server priority 1
no sntp server priority 2
no sntp server priority 3
sntp server priority 1 <SERVER_1>
sntp server priority 2 <SERVER_2>
time timezone <TZ_MINUTES>
write memory
```

**ArubaOS-CX** (8400):

```
configure terminal
no ntp server <SERVER_1>
no ntp server <SERVER_2>
ntp server <SERVER_1> iburst
ntp server <SERVER_2> iburst
ntp enable
clock timezone <TZ_NAME>
end
write memory
```

Jika manajemen lewat VRF mgmt, tambahkan `vrf mgmt` pada tiap baris `ntp server`. Flag `--cx-vrf mgmt|default`.

**ProCurve legacy** (2520 `J.14.04`, 2610 `Y.11`):

Sintaks SNTP mirip ArubaOS-S, tapi **wajib dideteksi dulu**. Jalankan `show sntp`; jika balasannya `Invalid input`, tandai `UNSUPPORTED` dan lewati (jangan menulis konfigurasi spekulatif). Jika didukung, pakai template ArubaOS-S tanpa baris `timesync` bila `timesync` juga tidak dikenali.

**ArubaOS Mobility Controller** (7200, wajib `--include-controllers`):

Template berbeda dan **harus diverifikasi manual pada satu unit dulu**. Program hanya boleh menjalankan mode `plan` untuk keluarga ini sampai ada konfirmasi sintaks; `apply` diblokir kecuali `--allow-controller-apply`.

### 3.3 Kredensial

- Username per model: `Aruba2930/Aruba2530/Aruba5400/Aruba8400` → `Aruba2930`? **Jangan berasumsi.** Baca dari `config/switch.txt` hanya password; username diambil dari:
  1. flag `--user` (global)
  2. field opsional ke-5 pada baris `switch.txt` jika ada (dukung format masa depan `<NAMA> | <IP> | <MODEL> | <PASS> | <USER>`)
  3. fallback: coba `Aruba2930`, lalu password sendiri sebagai username
- Jangan pernah menulis password ke log, CSV, atau nama file. Redaksi `***` pada semua output.

### 3.4 Probe kemampuan (capability detection)

Sebelum apply, per perangkat jalankan read-only:

| Perintah | Tujuan |
|---|---|
| `show version` | verifikasi firmware/keluarga |
| `show sntp` | ArubaOS-S: cek dukungan + status |
| `show ntp status` | CX: cek dukungan + status |
| `show time` | ArubaOS-S: jam lokal |
| `show clock` | CX: jam lokal |
| `show running-config \| include sntp` | ArubaOS-S: konfigurasi saat ini |
| `show running-config \| include ntp` | CX: konfigurasi saat ini |

Jika `show running-config` ditolak (akun operator), catat `PRIVILEGE_INSUFFICIENT` dan jangan lanjut ke apply untuk perangkat itu.

---

## 4. Arsitektur program

Python 3.11+ (target lingkungan: Python 3.14.3 di Windows, netmiko 4.6.0, openpyxl 3.1.5). Tanpa framework berat; `argparse` + `dataclasses` cukup.

```
Switch-Sync-NTP/
├── prompt.md
├── README.md
├── requirements.txt
├── .gitignore
├── config/
│   └── switch.txt
├── switch_sync/
│   ├── __init__.py
│   ├── cli.py            # subcommand: preflight|probe|plan|apply|verify|report
│   ├── inventory.py      # parse switch.txt + xlsx, merge, dedupe
│   ├── platforms.py      # Platform enum, template perintah, capability rules
│   ├── session.py        # netmiko wrapper: connect, ANSI strip, paging, retry
│   ├── runner.py         # ThreadPoolExecutor, rate limit, dry-run gate
│   ├── parsers.py        # parse show sntp / show ntp status / show clock / show time
│   ├── verify.py         # logika PASS/FAIL + skew matrix antar perangkat
│   ├── report.py         # CSV, JSON, Markdown
│   └── models.py         # dataclass: Device, ProbeResult, ApplyResult, Verdict
├── tests/
│   ├── fixtures/         # output CLI asli (disamarkan) untuk unit test
│   └── test_*.py
└── out/                  # hasil run (gitignored)
```

### 4.1 Wrapper netmiko

- Driver: `terminal_server` untuk semua ArubaOS-S/ProCurve (terbukti bekerja), `aruba_cx` untuk 8400.
- `fast_cli=False` (wajib; CLI ArubaOS-S tidak konsisten).
- Setelah login: kirim `\n`, lalu `no page` (ArubaOS-S) untuk mematikan paging.
- Bersihkan escape ANSI dengan `re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b.")`.
- Deteksi prompt: `#` = enable, `>` = user-exec → kirim `enable` bila perlu.
- Timeout per perangkat (default 20 s), retry 3x dengan backoff 2/4/6 s.
- Satu koneksi per perangkat per fase; selalu `disconnect()` di `finally`.

### 4.2 Runner

- `ThreadPoolExecutor`, default `--workers 6`, maksimum 12.
- Dry-run adalah **default**. Menulis hanya jika `--apply` diberikan.
- Per perangkat, status akhir salah satu dari: `OK`, `OK-ALREADY`, `SKIP-UNSUPPORTED`, `SKIP-PRIVILEGE`, `CONNECT-FAIL`, `AUTH-FAIL`, `CONFIG-FAIL`, `VERIFY-FAIL`, `UNREACHABLE`.
- Circuit breaker: jika 5 kegagalan koneksi berturut-turut, hentikan run dan cetak peringatan (kemungkinan kredensial atau ACL salah).

---

## 5. Alur CLI

```bash
# 0. Cek server NTP dulu (UDP/123), tanpa menyentuh switch
python -m switch_sync preflight --server 192.168.240.215

# 1. Baca inventaris saja
python -m switch_sync probe --list

# 2. Baca kondisi semua perangkat (read-only)
python -m switch_sync probe --workers 6 --out out/probe.csv

# 3. Lihat rencana perintah per perangkat (tanpa menulis)
python -m switch_sync plan --server 192.168.240.215 --tz 420

# 4. Terapkan
python -m switch_sync apply --server 192.168.240.215 --tz 420 --apply

# 5. Verifikasi setelah sinkronisasi (tunggu minimal 120 s)
python -m switch_sync verify --wait 120 --out out/verify.csv

# 6. Laporan gabungan
python -m switch_sync report --out out/report.md
```

Subcommand wajib: `preflight`, `probe`, `plan`, `apply`, `verify`, `report`.

### 5.1 `preflight`

Query NTP ke server via socket UDP/123 (paket NTPv4 48 byte), laporkan: reachable, stratum, root delay, root dispersion, offset terhadap jam host. Exit code non-zero bila server tidak menjawab atau stratum 0/16 (unsynchronized). Ini mencegah fleet diarahkan ke server mati seperti kejadian `192.168.240.240`.

### 5.2 `plan`

Untuk tiap perangkat hasilkan daftar perintah **lengkap** + alasan status:

- `OK-ALREADY` bila `show sntp` / `ntp status` sudah sinkron **dan** server serta timezone sudah sesuai target → tidak ada perintah tulis.
- `NEEDS-CHANGE` bila server berbeda, timezone berbeda, atau status unsynchronized.
- `SKIP-*` sesuai §4.2.

`plan` menulis `out/plan.json`, bukan mengeksekusi.

### 5.3 `apply`

Urutan per perangkat:

1. rekam `show time` / `show clock` (jam sebelum)
2. jalankan template §3.2 baris per baris, jeda 300–400 ms antar baris
3. `write memory` (wajib; konfirmasi balasan tidak mengandung `error`/`invalid`)
4. rekam jam sesudah (belum tentu sinkron — SNTP butuh waktu)

`apply` **tidak** boleh: reload, erase config, mengubah STP, mengubah VLAN, atau menyentuh port.

### 5.4 `verify`

1. tunggu `--wait` detik (default 120)
2. per perangkat: `show sntp` / `show ntp status`
3. klasifikasi: `SYNC` (status synchronized dan stratum 1–15), `UNSYNC`, `NO-SERVER`, `UNREACHABLE`
4. hitung selisih jam perangkat terhadap jam host (`--max-skew 2` detik, default) dan **skew matrix** antar perangkat
5. `PASS` hanya jika seluruh perangkat target `SYNC` dan skew di bawah ambang

---

## 6. Aturan keselamatan (wajib)

- Default **dry-run**. Tidak ada penulisan tanpa `--apply`.
- Idempotent: jalankan dua kali, hasil kedua harus `OK-ALREADY` di semua perangkat.
- Jangan menyimpan kredensial di kode. `config/switch.txt` **masuk `.gitignore`** dan repo belum punya commit — pastikan file itu tidak pernah ter-commit.
- Redaksi password di semua log, exception, traceback, dan nama file.
- `--only` / `--exclude` menerima hostname atau IP, dipisah koma; `--only` dipakai untuk pilot 1–2 perangkat sebelum fleet.
- Pilot wajib: `--only ASW01-MD-DC,ESW01-MD-DC,SW-MD-L2-UNTA` (tiga yang sudah terverifikasi) sebelum menyentuh sisanya.
- Jangan menyentuh `CORE DISTRI HG4` (`172.16.13.1`), `FARM SWITCH` (`172.21.255.6`), `PRIMARY/SECONDARY SWITCH CORE` (`172.21.255.1/.2`), dan controller kecuali flag eksplisit diberikan.
- Semua perangkat disimpan output mentahnya ke `out/raw/<ip>-<phase>.txt` agar bisa diaudit ulang.

---

## 7. Penanganan kasus khusus

| Kasus | Perlakuan |
|---|---|
| VSF/stack (ASW01, ESW01, DSW01) | satu IP = satu konfigurasi; jangan iterasi per member. Catat `member` dari Excel sebagai informasi. |
| `ESW01-ES-LT2` `192.168.238.10`, `SWITCH GSO` `192.168.40.2`, `ESW01-H1-*` `192.168.163.x` | subnet berbeda; mungkin tidak reachable dari host ini. Laporkan `UNREACHABLE`, jangan dianggap gagal konfigurasi. |
| `SW02-UPG-2610` `172.16.61.88` | sebelumnya timeout. Retry lebih panjang, lalu laporkan. |
| `SW02-T3-G53-2530` `172.16.73.5` | auth gagal sebelumnya; model di file `Procurve2520`. Laporkan `AUTH-FAIL`. |
| `SW-CARGO-2530` `172.16.67.10` | timeout sebelumnya (routing lintas site). |
| `SW-MD-L2-UNTA` baris 80 | password field terbaca `***` setelah strip; hati-hati spasi ganda. |
| Timezone | **keputusan terbuka**: `time timezone 420` = UTC+7 (WIB). Jika lokasi site Riyadh, seharusnya `180` (UTC+3). Program hanya meneruskan `--tz`; default `420` agar tidak mengubah perilaku yang ada, dan **cetak peringatan** jika `--tz` berbeda dari nilai yang ditemukan di perangkat. |
| CX 10.04 | `clock timezone` memakai **nama zona** (`Asia/Jakarta`, `Asia/Riyadh`), bukan menit. Sediakan `--tz-name` terpisah. |
| Controller 7200 | CLI ArubaOS 8 berbeda; `apply` diblokir tanpa `--allow-controller-apply`; sintaks harus dikonfirmasi manual di satu unit. |
| 2520 `J.14.04` | dukungan `sntp` belum pasti; probe dulu, `SKIP-UNSUPPORTED` bila tidak ada. |
| `show running-config` ditolak | `SKIP-PRIVILEGE`, sarankan pakai akun manager. |

---

## 8. Output

`out/` (gitignored):

- `plan.json` — rencana per perangkat
- `ntp_result.csv` — kolom: `host, ip, model, platform, status, server_before, server_after, tz_before, tz_after, clock_before, clock_after, sync_status, stratum, offset_s, detail`
- `verify.csv` — hasil `verify` + skew
- `report.md` — ringkasan: total, per platform, PASS/FAIL, daftar perangkat yang perlu tindakan manual, temuan keamanan
- `raw/<ip>-<phase>.txt` — output CLI mentah

---

## 9. Acceptance criteria

- [ ] `python -m switch_sync probe --list` mencetak 104 perangkat dari `switch.txt` tanpa error parsing, dan menandai 3 controller.
- [ ] `plan` mengeluarkan perintah berbeda untuk ArubaOS-S vs CX vs ProCurve legacy vs controller; tidak ada perintah CX yang dikirim ke 2530.
- [ ] Semua unit test lulus; parser diuji terhadap fixture `show sntp`, `show ntp status`, `show clock`, `show time` asli (disamarkan).
- [ ] Dry-run tidak mengubah apa pun: diuji dengan mock transport yang gagal bila ada perintah tulis.
- [ ] `apply` pada 3 switch pilot menghasilkan `SYNC` di `verify`.
- [ ] Menjalankan `apply` dua kali berturut-turut menghasilkan `OK-ALREADY` pada run kedua.
- [ ] Password tidak muncul di `out/`, log, maupun `git status`.
- [ ] `preflight --server 192.168.240.240` gagal dengan pesan jelas (server mati), `--server 192.168.240.215` memberi hasil terukur.

---

## 10. Non-goals

- Bukan tool untuk mengubah STP, loop-protect, storm-control, VLAN, atau firmware.
- Bukan dashboard web. Output cukup CSV/Markdown.
- Bukan pengganti AirWave. Program ini CLI-first, satu kali jalan, bisa diulang.
- Tidak melakukan `reload`, `erase`, atau `copy` config antar perangkat.

---

## 11. Yang harus dikonfirmasi ke manusia sebelum `apply` ke seluruh fleet

1. **Server NTP final.** Kandidat: `192.168.240.215` (hidup, 1 ms) dan `172.16.21.7` (Domain Controller). Harus lolos `preflight`.
2. **Timezone site**: `420` (UTC+7) atau `180` (UTC+3)?
3. **Akun**: apakah semua perangkat punya akun manager yang bisa `write memory`? Akun read-only tidak cukup.
4. **Izin menyentuh core 8400 dan controller** atau dikerjakan terpisah.

Tulis pertanyaan ini di `README.md` sebagai bagian "Open decisions" agar tidak hilang.
