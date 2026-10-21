"""
Demo mode: synthetic CPViewDB.dat files plus stub Check Point commands, so
the full pipeline (shell script -> sqlite3 -> parse -> store -> dashboard)
can be exercised on any machine with bash and the sqlite3 CLI.

The table/column names below are modelled on real CPView history tables but
are NOT guaranteed to match any particular Gaia build - DEMO-GW-02 uses
deliberately different names to exercise schema discovery.

DEMO-GW-03 imitates R82 as seen on a lab gateway: real R82 DDL for the
memory and interface tables (labels referencing cpview_ref_table), a
history file that stops 3 hours ago (CPView Services flushes in batches)
and a `cpview -m` stub producing the telemetry JSON for live values.
"""
import math
import os
import random
import sqlite3
import stat
import threading
import time

VARIANTS = {
    "A": {
        "cpu": ("UM_STAT_UM_CPU_UM_CPU_ORDERED_TABLE", "name_of_cpu",
                ["cpu_usage", "cpu_user", "cpu_system", "cpu_idle", "cpu_iowait", "cpu_interrupts"]),
        "mem": ("UM_STAT_UM_MEMORY", ["real_total", "real_used", "real_free", "swap_total", "swap_used", "swap_free"]),
        "disk": ("UM_STAT_UM_DISK_UM_PARTITIONS_TABLE", "partition_name", ["total", "used", "free", "used_percent"]),
        "net": ("UM_STAT_UM_NETWORK_UM_INTERFACES_TABLE", "interface_name",
                ["rx_bits_per_sec", "tx_bits_per_sec", "rx_packets_per_sec", "tx_packets_per_sec", "rx_drops", "tx_drops"]),
        "fw": ("fw_counters", ["concurrent_conns", "tcp_conns", "udp_conns", "other_conns", "conn_rate",
                               "accepted_pkts", "dropped_pkts", "rejected_pkts"]),
        "corexl": ("UM_STAT_UM_COREXL_UM_INSTANCES_TABLE", "instance_name", ["cpu_usage", "connections", "peak_connections"]),
    },
    "B": {
        "cpu": ("UM_CPU_TABLE", "cpu_name", ["usage", "user", "system", "idle", "io_wait", "interrupts"]),
        "mem": ("MEMORY_STATS", ["total_bytes", "used_bytes", "free_bytes", "swap_total", "swap_used", "swap_free"]),
        "disk": ("HDD_PARTITIONS", "mount", ["total", "used", "free", "usage_percent"]),
        "net": ("NETWORK_IFACE_TRAFFIC", "iface", ["rx_bps", "tx_bps", "rx_pps", "tx_pps", "rx_dropped", "tx_dropped"]),
        "fw": ("FW_STATS", ["num_connections", "tcp_connections", "udp_connections", "icmp_connections",
                            "new_conn_rate", "accepted", "dropped", "rejected"]),
        "corexl": ("COREXL_FW_INSTANCES", "instance", ["cpu", "connections", "peak"]),
    },
}

DEMO_GATEWAYS = [
    {"name": "DEMO-GW-01", "variant": "A", "cores": 4, "ifaces": ["eth0", "eth1", "eth2", "eth3"],
     "ram": 8 * 2**30, "policy": "Standard", "hw": "VMware Virtual Platform", "ha": "ACTIVE"},
    {"name": "DEMO-GW-02", "variant": "B", "cores": 2, "ifaces": ["eth0", "eth1", "eth2"],
     "ram": 6 * 2**30, "policy": "Standard", "hw": "Check Point 3100", "ha": "STANDBY"},
    {"name": "DEMO-GW-03", "variant": "R82", "cores": 8, "ifaces": ["eth0", "eth1", "eth2", "eth3"],
     "ram": 10 * 2**30, "policy": "Alpha-Standard", "hw": "Virtual Machine", "ha": "ACTIVE", "db_lag_hours": 3},
]

R82_DDL = """
CREATE TABLE cpview_ref_table (seq INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE UM_STAT_UM_MEMORY (Timestamp INTEGER,real_total INTEGER,real_used INTEGER,swap_used INTEGER,real_free INTEGER,swap_free INTEGER,swap_total INTEGER);
CREATE INDEX UM_STAT_UM_MEMORY_Timestamp_Index ON UM_STAT_UM_MEMORY (Timestamp);
CREATE TABLE UM_STAT_UM_CPU_UM_CPU_ORDERED_TABLE (Timestamp INTEGER,name_of_cpu INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,cpu_usage INTEGER,cpu_user INTEGER,cpu_system INTEGER,cpu_io_wait INTEGER,cpu_interrupts INTEGER);
CREATE TABLE UM_STAT_UM_HW_UM_IF_TABLE (Timestamp INTEGER,name INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,device INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,state INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,type INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,port INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,driver INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,speed INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,ipv4 INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,ipv6 INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,firmware INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,mac  TEXT);
CREATE TABLE UM_STAT_UM_HW_UM_IF_RX_STATISTICS_TABLE (Timestamp INTEGER,if_name INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,device INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,if_rx_packets INTEGER,if_rx_packets_peak INTEGER,if_rx_packets_throughput INTEGER,if_rx_bits INTEGER,if_rx_bits_peak INTEGER,if_rx_bits_throughput INTEGER);
CREATE TABLE UM_STAT_UM_HW_UM_IF_TX_STATISTICS_TABLE (Timestamp INTEGER,if_name INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,device INTEGER REFERENCES cpview_ref_table(seq) DEFAULT -1,if_tx_packets INTEGER,if_tx_packets_peak INTEGER,if_tx_packets_throughput INTEGER,if_tx_bits INTEGER,if_tx_bits_peak INTEGER,if_tx_bits_throughput INTEGER);
"""


def _ref(conn, value: str) -> int:
    row = conn.execute("SELECT seq FROM cpview_ref_table WHERE value = ?", (value,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO cpview_ref_table (value) VALUES (?)", (value,)).lastrowid


def _write_r82(conn, gw, ts, rnd):
    load = max(0.02, 0.1 + 0.05 * math.sin(2 * math.pi * (ts % 86400) / 86400) + rnd.gauss(0, 0.02))
    used = int(gw["ram"] * 0.6)
    conn.execute("INSERT INTO UM_STAT_UM_MEMORY VALUES (?,?,?,?,?,?,?)",
                 (ts, gw["ram"], used, 48603136, gw["ram"] - used, 16661499904, 16710103040))
    for i in range(gw["cores"]):
        u = int(100 * load + rnd.gauss(0, 2))
        conn.execute("INSERT INTO UM_STAT_UM_CPU_UM_CPU_ORDERED_TABLE VALUES (?,?,?,?,?,?,?)",
                     (ts, _ref(conn, str(i)), u, u // 2, u // 2, 0, 1345))
    dev = _ref(conn, "21")
    for n, iface in enumerate(gw["ifaces"]):
        bits = int(max(0, 400000 / (n + 1) + rnd.gauss(0, 20000)))
        for d in ("RX", "TX"):
            conn.execute(f"INSERT INTO UM_STAT_UM_HW_UM_IF_{d}_STATISTICS_TABLE VALUES (?,?,?,?,?,?,?,?,?)",
                         (ts, _ref(conn, iface), dev, ts // 10, 7761, bits // 1200, ts * 8, 53552264, bits))


def _create(conn, v):
    cpu_t, cpu_l, cpu_c = v["cpu"]
    mem_t, mem_c = v["mem"]
    disk_t, disk_l, disk_c = v["disk"]
    net_t, net_l, net_c = v["net"]
    fw_t, fw_c = v["fw"]
    cx_t, cx_l, cx_c = v["corexl"]
    ints = lambda cols: ", ".join(f"{c} INTEGER" for c in cols)
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {cpu_t} (Timestamp INTEGER, {cpu_l} TEXT, {ints(cpu_c)});
        CREATE TABLE IF NOT EXISTS {mem_t} (Timestamp INTEGER, {ints(mem_c)});
        CREATE TABLE IF NOT EXISTS {disk_t} (Timestamp INTEGER, {disk_l} TEXT, {ints(disk_c)});
        CREATE TABLE IF NOT EXISTS {net_t} (Timestamp INTEGER, {net_l} TEXT, {ints(net_c)});
        CREATE TABLE IF NOT EXISTS {fw_t} (Timestamp INTEGER, {ints(fw_c)});
        CREATE TABLE IF NOT EXISTS {cx_t} (Timestamp INTEGER, {cx_l} TEXT, {ints(cx_c)});
        CREATE TABLE IF NOT EXISTS UM_STAT_UM_HW_UM_SENSORS (Timestamp INTEGER, sensor_name TEXT, value INTEGER, status TEXT);
    """)


def _write_sample(conn, v, gw, ts, rnd):
    day = math.sin(2 * math.pi * (ts % 86400) / 86400)
    load = max(0.02, 0.15 + 0.1 * day + rnd.gauss(0, 0.03) + (0.5 if rnd.random() < 0.01 else 0))

    cpu_t, cpu_l, _ = v["cpu"]
    for i in range(gw["cores"]):
        usage = min(100, max(0, int(100 * load * (1.3 if i == 0 else 1) + rnd.gauss(0, 3))))
        user, system, iowait = int(usage * 0.4), int(usage * 0.5), int(usage * 0.1)
        conn.execute(f"INSERT INTO {cpu_t} VALUES (?,?,?,?,?,?,?,?)",
                     (ts, f"CPU{i}", usage, user, system, 100 - usage, iowait, int(8000 + 6000 * load + rnd.gauss(0, 500))))

    mem_t, _ = v["mem"]
    used = int(gw["ram"] * (0.42 + 0.03 * day + rnd.gauss(0, 0.005)))
    conn.execute(f"INSERT INTO {mem_t} VALUES (?,?,?,?,?,?,?)",
                 (ts, gw["ram"], used, gw["ram"] - used, 8 * 2**30, 0, 8 * 2**30))

    disk_t, _, _ = v["disk"]
    for mount, total, frac in (("/", 34 * 2**30, 0.31), ("/var/log", 60 * 2**30, 0.058), ("/boot", 2**28, 0.28)):
        u = int(total * frac)
        conn.execute(f"INSERT INTO {disk_t} VALUES (?,?,?,?,?,?)", (ts, mount, total, u, total - u, int(frac * 100)))

    net_t, _, _ = v["net"]
    for n, iface in enumerate(gw["ifaces"]):
        scale = 1.0 / (n + 1)
        rx = int(max(0, (2e6 + 8e6 * load) * scale + rnd.gauss(0, 3e5)))
        tx = int(max(0, rx * (0.9 + rnd.random() * 0.2)))
        conn.execute(f"INSERT INTO {net_t} VALUES (?,?,?,?,?,?,?,?)",
                     (ts, iface, rx, tx, rx // 6000, tx // 6000, rnd.choice([0] * 20 + [1, 3]), 0))

    fw_t, _ = v["fw"]
    conns = int(200 + 150 * load + rnd.gauss(0, 15))
    conn.execute(f"INSERT INTO {fw_t} VALUES (?,?,?,?,?,?,?,?,?)",
                 (ts, conns, int(conns * 0.7), int(conns * 0.25), int(conns * 0.05),
                  int(20 + 40 * load), int(1500 * load * 10), int(rnd.random() * 15), int(rnd.random() * 3)))

    cx_t, _, _ = v["corexl"]
    for i in range(max(1, gw["cores"] - 1)):
        conn.execute(f"INSERT INTO {cx_t} VALUES (?,?,?,?,?)",
                     (ts, f"fw_{i}", int(100 * load * 0.8 + rnd.gauss(0, 2)), conns // max(1, gw["cores"] - 1), conns))

    for sensor, val in (("CPU Temp", 48), ("Fan 1", 4200)):
        conn.execute("INSERT INTO UM_STAT_UM_HW_UM_SENSORS VALUES (?,?,?,?)", (ts, sensor, int(val + rnd.gauss(0, 2)), "OK"))


CPVIEW_M = r"""
import json, os, random, time
cores, ram = int(os.environ.get("DEMO_CORES", 4)), int(os.environ.get("DEMO_RAM", 2**33))
r = random.Random()
util = []
for c in range(cores):
    busy = r.uniform(2, 12)
    t = "OTHER" if c < 2 else "CoreXL_FW"
    util += [{"cpu": str(c), "state": "idle", "type": t, "value": round(100 - busy)},
             {"cpu": str(c), "state": "system", "type": t, "value": round(busy / 2)},
             {"cpu": str(c), "state": "user", "type": t, "value": round(busy / 2)},
             {"cpu": str(c), "state": "io wait", "type": t, "value": 0}]
used = int(ram * r.uniform(0.6, 0.65))
def m(mid, data, unit="1", typ="Gauge"):
    return {"data": data, "description": mid, "metric-id": mid, "metric-type": typ, "metric-unit": unit, "metric-value-type": "double"}
print(json.dumps({"metrics": [
    m("system.cpu.count", [{"value": cores}], "{cpu}"),
    m("system.cpu.utilization", util),
    m("system.cpu.interrupts", [{"cpu": str(c), "type": "OTHER", "value": 1345} for c in range(cores)], "{interrupts}"),
    m("system.memory.limit", [{"value": ram}], "By"),
    m("system.memory.usage", [{"state": "used", "value": used}, {"state": "free", "value": ram - used}], "By", "AsyncUpDownCounter"),
    m("system.paging.usage", [{"state": "free", "value": 16661499904}, {"state": "used", "value": 48603136}], "By"),
    m("system.filesystem.usage", [{"mountpoint": mp, "state": s, "value": v} for mp, u, f in
        (("/boot", 40204288, 240303104), ("/", 12498526208, 94823476224), ("/var/log", 9721565184, 97600437248))
        for s, v in (("used", u), ("free", f))], "By"),
    m("system.traffic.dropped.rate", [{"component": "FW", "type": "General Cluster", "value": 2},
        {"component": "FW", "type": "Tcp Out Of State", "value": r.randint(0, 3)},
        {"component": "FW", "type": "Rulebase", "value": 1}], "{drops}"),
], "timestamp": int(time.time())}))
"""

STUBS = {
    "cpview": """#!/bin/bash
[ "$1" = "-m" ] && [ "$DEMO_LIVE" = "1" ] && exec python3 "$(dirname "$0")/cpview_m.py"
exit 0
""",
    "fw": """#!/bin/bash
if [ "$2" = "-k" ]; then echo "This is Check Point's software version R82 - Build 012"; echo "kernel: R82 - Build 998002151"; else echo "This is Check Point's software version R82 - Build 012"; fi
""",
    "cpstat": """#!/bin/bash
echo ""; echo "Product name:         Firewall"; echo "Policy name:          $DEMO_POLICY"; echo "Policy install time:  Wed Nov 12 08:51:43 2025"
echo "Install time:         Wed Nov 12 08:51:43 2025"
""",
    "cpprod_util": """#!/bin/bash
case "$1" in FwIsFirewallModule) echo 1;; *) echo 0;; esac
""",
    "dmidecode": """#!/bin/bash
echo "$DEMO_HW"
""",
    "cphaprob": """#!/bin/bash
echo "Cluster Mode:   High Availability (Active Up) with IGMP Membership"
echo ""
echo "ID         Unique Address  Assigned Load   State          Name"
echo "1 (local)  10.1.1.2        100%            $DEMO_HA         $DEMO_NAME"
""",
}


def setup_demo(base_dir: str, backfill_hours: float = 24) -> list[dict]:
    """Create demo DBs + stub bin dir. Returns gateway config entries."""
    demo_dir = os.path.join(base_dir, "data", "demo")
    bin_dir = os.path.join(demo_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, "cpview_m.py"), "w") as f:
        f.write(CPVIEW_M)
    for name, body in STUBS.items():
        p = os.path.join(bin_dir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    gateways = []
    now = int(time.time()) // 60 * 60
    for gw in DEMO_GATEWAYS:
        db = os.path.join(demo_dir, gw["name"], "CPViewDB.dat")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        if os.path.exists(db):
            os.remove(db)
        conn = sqlite3.connect(db)
        rnd = random.Random(gw["name"])
        last = now - int(gw.get("db_lag_hours", 0) * 3600)
        if gw["variant"] == "R82":
            conn.executescript(R82_DDL)
            for ts in range(now - int(backfill_hours * 3600), last + 1, 60):
                _write_r82(conn, gw, ts, rnd)
        else:
            v = VARIANTS[gw["variant"]]
            _create(conn, v)
            for ts in range(now - int(backfill_hours * 3600), last + 1, 60):
                _write_sample(conn, v, gw, ts, rnd)
        conn.commit()
        conn.close()
        gateways.append({"name": gw["name"], "mgmt_ip": "127.0.0.1", "cpview_db": db, "_demo": gw})
    return gateways, bin_dir


def demo_script_prefix(gw_entry: dict) -> str:
    d = gw_entry["_demo"]
    live = 1 if d["variant"] == "R82" else 0
    return (f"export DEMO_POLICY='{d['policy']}' DEMO_HW='{d['hw']}' DEMO_HA='{d['ha']}' DEMO_NAME='{d['name']}' "
            f"DEMO_LIVE={live} DEMO_CORES={d['cores']} DEMO_RAM={d['ram']}\n")


def start_generator(gateways: list[dict], interval: int = 60):
    """Append one sample per gateway every `interval` seconds (CPView writes once a minute)."""
    def loop():
        rnds = {g["name"]: random.Random(g["name"] + "live") for g in gateways}
        while True:
            time.sleep(interval)
            ts = int(time.time())
            for g in gateways:
                d = g["_demo"]
                if d.get("db_lag_hours"):
                    continue  # R82-style: history file is only flushed in batches
                conn = sqlite3.connect(g["cpview_db"], timeout=10)
                _write_sample(conn, VARIANTS[d["variant"]], d, ts, rnds[g["name"]])
                conn.commit()
                conn.close()
    threading.Thread(target=loop, daemon=True, name="demo-generator").start()
