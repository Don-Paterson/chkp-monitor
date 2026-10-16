# chkp-monitor

Lightweight health dashboard for Check Point lab environments. Designed for ephemeral Skillable labs where you need quick visibility into gateway and management server health without persistent infrastructure.

## What it does

Polls your Check Point management server and gateways at regular intervals and serves a clean, auto-refreshing dashboard showing:

- **CPU / Memory / Disk** usage per gateway
- **Firewall connections and drops** (current and peak)
- **ClusterXL state** (Active/Standby per member)
- **Policy install status and SIC state**
- **Kernel memory** (fw ctl pstat)
- **Interface throughput and errors**

Data is collected via three methods in priority order:
1. **Management API** (A-SMS) — gateway status, SIC, policy, cluster membership
2. **Gaia API** (each gateway) — cpstat, cphaprob, fw ctl pstat via /run-script
3. **SSH fallback** — same commands via Paramiko if Gaia API is unreachable

## Quick start

On a fresh lab A-GUI, open PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/Don-Paterson/chkp-monitor/main/bootstrap.ps1 | iex
```

This will:
1. Install Python 3.12 (silent)
2. Install PowerShell 7 (silent)
3. Install Flask, requests, paramiko via pip
4. Download all app files
5. Create a read-only `monitor-api` admin on A-SMS
6. Create a `monitor-api` user on A-GW-01 and A-GW-02
7. Generate `credentials.json` locally
8. Launch the dashboard at http://localhost:8080

## VSX labs and the CCVS-R8220 topology

What is collected per gateway, in **one** Gaia API `run-script` (or one SSH session) per poll:

| Command | Shown as |
|---|---|
| `cpstat os -f all`, `cpstat os -f multi_cpu` | CPU, memory, disk, **CPU per core** |
| `cpstat fw -f all`, `fw ctl pstat` | connections, drops, kernel memory |
| `fw ctl multik stat` | **CoreXL instances**: CPU, connections, peak |
| `cphaprob stat` | ClusterXL members (if clustered) |
| `vsx stat -l` + a 1-second `top` sample of `fwk<VSID>_*` processes | **Virtual systems**: connections, peak, limit, policy, SIC, CPU |
| `/proc/net/dev` | interface throughput (per second) and errors |

All gateways are polled in parallel. The management API collection now includes VSX
gateways, VSX clusters and virtual systems.

For the CCVS-R8220 lab, edit the IPs in `config.ccvs-r8220.json`, then run on A-GUI:

```powershell
$env:CHKP_CONFIG = 'config.ccvs-r8220.json'
python server.py
```

### Monitoring user on each gateway

The SSH fallback works best when the monitoring user's shell is `/bin/bash` (one plain exec,
no prompt handling). With a clish shell it enters expert mode and needs the expert
password (`"expert_password"` under `"gaia"` in `credentials.json`; defaults to the login password).

### Seeing it from the VMware host

chkp-monitor runs on **A-GUI**, inside the lab, where SSH and HTTPS to the Check Point
machines already work. The host monitor (`vmhost/`) reads chkp-monitor's
`http://<A-GUI>:8080/api/status`, so only the host → A-GUI connection is needed.
Choose one way to provide it:

1. **Second NIC on A-GUI, on a host-only network (recommended).** In Workstation add a NIC
   to A-GUI on VMnet1 (or a new host-only VMnet), give it an address in that subnet with
   **no default gateway**, and allow TCP 8080 in A-GUI's Windows firewall. The host reaches
   A-GUI directly; lab routing, NAT and the VyOS routers are untouched.
2. **Port forward on the VyOS edge.** DNAT TCP 8080 on the VyOS outside (VMware NAT)
   interface to A-GUI:8080. The host is on the VMnet8 subnet, so it can reach the VyOS
   outside address. This changes lab NAT, and the Check Point policy must allow it if a
   gateway sits between VyOS and A-GUI.

Then set `lab_status_url` in `vmhost/config.json`.

## Default topology

Configured for standard Check Point lab topology:

| Device | IP |
|---|---|
| A-SMS | 10.1.1.101 |
| A-GW-01 | 10.1.1.2 |
| A-GW-02 | 10.1.1.3 |

Edit `config.json` to change IPs, thresholds, or polling intervals.

## Files

```
chkp-monitor/
├── bootstrap.ps1          # One-liner lab setup script
├── server.py              # Flask app + background collector threads
├── config.json            # Topology, thresholds, polling config
├── credentials.json       # Created by bootstrap (gitignored)
├── credentials.json.example
├── config.ccvs-r8220.json # VSX lab topology (select with CHKP_CONFIG)
├── collectors/
│   ├── commands.py        # The per-gateway script and all parsers
│   ├── mgmt_api.py        # Management API (gateways, VSX, VS, policy, SIC)
│   ├── gaia_api.py        # Gaia API run-script (one task per gateway per poll)
│   └── ssh_fallback.py    # SSH via Paramiko (same script, one session)
├── tests/                 # Parser tests: python -m unittest discover tests
├── vmhost/                # VMware host monitor (runs on the Windows host, see vmhost/README.md)
├── static/
│   └── dashboard.html     # Single-page dashboard
└── .gitignore
```

## Credentials

The bootstrap uses these defaults:
- **SmartConsole admin**: `cpadmin` / `Chkp!234`
- **Gaia admin**: `admin` / `Chkp!234`
- **Created monitoring user**: `monitor-api` / `M0n!t0r@pi`

Change `$LAB_PASSWORD` and `$MONITOR_PASS` in `bootstrap.ps1` if your lab uses different credentials.

## Thresholds

Edit `config.json` to adjust RAG thresholds:

```json
"thresholds": {
    "cpu_warn": 70,
    "cpu_crit": 90,
    "memory_warn": 75,
    "memory_crit": 90,
    "disk_warn": 80,
    "disk_crit": 95,
    "connections_warn": 20000,
    "connections_crit": 40000,
    "kernel_memory_warn": 75,
    "kernel_memory_crit": 90
}
```

## Requirements

- Windows 10 1809+ (tested on Skillable lab A-GUI)
- Outbound internet access (for bootstrap downloads)
- Administrative rights on A-GUI
- Network access to A-SMS and gateways on ports 443 and 22

## Future enhancements

- In-session CSV/JSON logging for trend analysis
- Skyline integration via Infinity Portal API
- Additional Gaia API native endpoints (NTP, routes, interfaces)
- Alert notifications via desktop toast
