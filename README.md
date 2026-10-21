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
│   ├── dashboard.html     # Single-page dashboard
│   └── cpview.html        # CPView time-series dashboard
├── cpview_server.py       # CPView edition: Flask app + per-gateway collectors
├── cpview_metrics.json    # Logical metric -> CPView table/column regexes
├── cpview/
│   ├── transport.py       # Gaia API run-script / SSH / local script runner
│   ├── schema.py          # CPViewDB.dat schema discovery + metric resolution
│   ├── collector.py       # Incremental CPView pull + system snapshot
│   ├── store.py           # Local SQLite time-series store
│   └── demo.py            # Synthetic gateways for --demo
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

## CPView edition (Skyline-style time series)

`cpview_server.py` is a second dashboard (http://localhost:8081) that gives you Skyline-style graphs **without** the OpenTelemetry agent, collector, Prometheus or Grafana. It reads the CPView history database that every Gaia gateway already keeps:

```
/var/log/CPView_history/CPViewDB.dat   (SQLite, one sample per minute, ~30 days)
```

| Skyline | chkp-monitor CPView edition |
|---|---|
| OpenTelemetry CPView agent on the gateway | nothing installed - `sqlite3` queries via Gaia API `run-script` (SSH fallback) |
| OTel collector → Prometheus remote-write | incremental pull of rows newer than the last poll |
| Prometheus TSDB | local SQLite store (`data/cpview_store.db`) |
| Grafana dashboards | `static/cpview.html` - Single Machine, Machines Overview, Schema Explorer |
| 15s resolution, history starts when enabled | 60s resolution, **backfills history the gateway already holds** |

How it works:

1. **Schema discovery** - CPView table/column names change between versions and Jumbo takes, so nothing is hard-coded. The gateway's `CREATE TABLE` statements are fetched and replayed into an in-memory SQLite DB to get exact column lists.
2. **Metric catalogue** - `cpview_metrics.json` maps logical metrics (CPU per core, RAM, interface throughput, connections, drops, CoreXL...) to CPView tables/columns using ordered regex candidates. Unmatched metrics show a banner pointing to the Schema Explorer.
3. **Collection** - each poll sends ONE script per gateway: `SELECT * FROM <table> WHERE Timestamp > <cursor>` for every mapped table, plus a small system snapshot (`fw ver`, `/proc/meminfo`, `df`, `cpstat fw -f policy`, `cphaprob stat`) for the overview tiles and gauges. First contact backfills `backfill_hours` of history in chunks.
4. **Schema Explorer** - browse every CPView table, see which catalogue metric uses which column, chart any stored column, or read any table live from the gateway (last N minutes).

Run it:

```powershell
python cpview_server.py           # uses config.json + credentials.json (bootstrap starts it for you)
python cpview_server.py --demo    # two synthetic gateways, no lab needed (needs bash + sqlite3 CLI)
```

Settings live in the `cpview` block of `config.json`:

| Key | Default | Meaning |
|---|---|---|
| `poll_interval_seconds` | 60 | CPView writes once a minute, so faster polling gains nothing |
| `backfill_hours` | 24 | history pulled from the gateway on first contact |
| `retention_days` | 7 | local store pruning (~14 MB per gateway per day) |
| `max_rows_per_table` | 1500 | rows per table per poll (keeps run-script output small) |
| `extra_tables` | `[]` | regexes of extra CPView tables to store even if no metric uses them |
| `collect_all_tables` | false | store every CPView table |
| `include_management` | false | also poll the management server (needs a Gaia API user there) |

**First run on a new version:** open the Schema Explorer tab. If a panel says "No CPView table/column matched", find the right table there and add its name/column as a regex to that metric in `cpview_metrics.json`, then restart `cpview_server.py`. The demo DB schema is modelled on CPView but is synthetic; validate the mappings against your R81.20/R82 gateways.

Prerequisites on the gateway: CPView history enabled (default; bootstrap checks for the DB file and runs `cpview history on` if it is missing) and the Gaia API user from bootstrap (uid 0 / adminRole, which can read `/var/log/CPView_history`).

## Future enhancements

- In-session CSV/JSON logging for trend analysis
- Skyline integration via Infinity Portal API
- CPView edition: per-VS context for VSX, alert rules on catalogue metrics
- Additional Gaia API native endpoints (NTP, routes, interfaces)
- Alert notifications via desktop toast
