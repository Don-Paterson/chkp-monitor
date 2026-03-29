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
├── collectors/
│   ├── mgmt_api.py        # Tier 1: Management API
│   ├── gaia_api.py        # Tier 3: Gaia API /run-script
│   └── ssh_fallback.py    # Tier 2: SSH via Paramiko
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
