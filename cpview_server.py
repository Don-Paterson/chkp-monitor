"""
chkp-monitor CPView edition - Skyline-style time-series dashboard built on
the CPView history database (/var/log/CPView_history/CPViewDB.dat).

No agent, OpenTelemetry collector, Prometheus or Grafana: gateways are
polled over the Gaia API (SSH fallback), CPView rows are stored in a local
SQLite time-series DB, and served to static/cpview.html.

    python cpview_server.py            # lab: uses config.json + credentials.json
    python cpview_server.py --demo     # synthetic gateways, no lab needed
"""
import argparse
import json
import os
import sys
import time
import logging
import threading

from flask import Flask, jsonify, redirect, request, send_from_directory

from cpview.collector import CpviewCollector, DEFAULT_DB
from cpview.schema import parse_csv
from cpview.store import Store
from cpview.transport import GatewayTransport, LocalTransport, build_script, sqlite_cmd, split_sections

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chkp-monitor.cpview")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHARTJS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"

DEFAULTS = {
    "port": 8081,
    "poll_interval_seconds": 60,
    "schema_refresh_seconds": 3600,
    "backfill_hours": 24,
    "retention_days": 7,
    "max_rows_per_table": 1500,
    "db_path": DEFAULT_DB,
    "store_path": "data/cpview_store.db",
    "extra_tables": [],
    "collect_all_tables": False,
    "include_management": False,
    "ssh_fallback": True,
}

with open(os.path.join(BASE_DIR, "config.json")) as f:
    CONFIG = json.load(f)
CPV = {**DEFAULTS, **CONFIG.get("cpview", {})}
THRESHOLDS = CONFIG.get("thresholds", {})

with open(os.path.join(BASE_DIR, "cpview_metrics.json")) as f:
    CATALOGUE = json.load(f)["metrics"]
CATALOGUE_BY_ID = {m["id"]: m for m in CATALOGUE}

collectors: dict[str, CpviewCollector] = {}
store: Store | None = None
transport = None


class DemoTransport:
    """LocalTransport plus per-gateway env vars consumed by the demo stubs."""

    def __init__(self, bin_dir: str):
        self.local = LocalTransport(extra_path=bin_dir)
        self.last_method = self.local.last_method

    def run(self, gw: dict, script: str) -> str | None:
        from cpview.demo import demo_script_prefix
        return self.local.run(gw, demo_script_prefix(gw) + script)


# ---- helpers ----

def pick_step(range_s: int) -> int:
    """~300 points per chart, never finer than CPView's 60s sampling."""
    return max(60, (range_s // 300) // 60 * 60)


def window(host: str) -> tuple[int, int, int]:
    """
    Time window anchored on the newest sample from the gateway rather than
    the local clock - lab VMs frequently drift by minutes or hours.
    """
    range_s = int(request.args.get("range", 3600))
    end = request.args.get("end", type=int) or store.latest_ts(host) or int(time.time())
    return end - range_s, end, pick_step(range_s)


def get_collector(host: str) -> CpviewCollector | None:
    return collectors.get(host)


# ---- Flask ----

app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    return send_from_directory("static", "cpview.html")


@app.route("/vendor/chart.js")
def chartjs():
    """Local copy (downloaded by bootstrap.ps1) if present, else the CDN."""
    if os.path.exists(os.path.join(BASE_DIR, "static", "vendor", "chart.umd.min.js")):
        return send_from_directory(os.path.join("static", "vendor"), "chart.umd.min.js")
    return redirect(CHARTJS_CDN)


@app.route("/api/cpview/config")
def api_config():
    return jsonify({
        "thresholds": THRESHOLDS,
        "poll_interval_seconds": CPV["poll_interval_seconds"],
        "catalogue": [{k: m[k] for k in ("id", "title", "unit")} for m in CATALOGUE],
        "hosts": list(collectors),
    })


@app.route("/api/cpview/hosts")
def api_hosts():
    out = []
    for name, c in collectors.items():
        out.append({
            "name": name,
            "ip": c.gw.get("mgmt_ip"),
            "status": c.status,
            "info": store.get_info(name),
            "latest_ts": store.latest_ts(name),
        })
    return jsonify({"hosts": out, "store": store.stats(), "now": time.time()})


@app.route("/api/cpview/host/<name>")
def api_host(name):
    c = get_collector(name)
    if not c:
        return jsonify({"error": "unknown host"}), 404
    return jsonify({
        "name": name,
        "status": c.status,
        "info": store.get_info(name),
        "mapping": c.mapping,
        "latest_ts": store.latest_ts(name),
    })


@app.route("/api/cpview/metric")
def api_metric():
    """Catalogue metric for one host. ?host=&id=&range=seconds"""
    host, mid = request.args.get("host"), request.args.get("id")
    c, metric = get_collector(host), CATALOGUE_BY_ID.get(mid)
    if not c or not metric:
        return jsonify({"error": "unknown host or metric"}), 404
    mapping = c.mapping.get(mid)
    if not mapping:
        return jsonify({"id": mid, "title": metric["title"], "unit": metric["unit"], "mapped": False, "series": []})
    start, end, step = window(host)
    data = store.series(host, mapping["table"], mapping["column"], start, end, step)
    if metric.get("agg") == "avg" and len(data) > 1:
        data = {"avg": _average(data)}
    return jsonify({
        "id": mid, "title": metric["title"], "unit": metric["unit"], "mapped": True,
        "table": mapping["table"], "column": mapping["column"],
        "start": start, "end": end, "step": step,
        "series": [{"label": k or metric["title"], "points": v} for k, v in sorted(data.items())],
    })


def _average(data: dict) -> list:
    buckets = {}
    for points in data.values():
        for ts, v in points:
            buckets.setdefault(ts, []).append(v)
    return [[ts, sum(vs) / len(vs)] for ts, vs in sorted(buckets.items())]


@app.route("/api/cpview/series")
def api_series():
    """Any stored (table, column) - used by the Schema Explorer."""
    host, tbl, col = request.args.get("host"), request.args.get("table"), request.args.get("column")
    if not get_collector(host):
        return jsonify({"error": "unknown host"}), 404
    start, end, step = window(host)
    data = store.series(host, tbl, col, start, end, step)
    return jsonify({"table": tbl, "column": col, "start": start, "end": end, "step": step,
                    "series": [{"label": k or col, "points": v} for k, v in sorted(data.items())]})


@app.route("/api/cpview/schema")
def api_schema():
    host = request.args.get("host")
    c = get_collector(host)
    if not c:
        return jsonify({"error": "unknown host"}), 404
    mapped_tables = {}
    for mid, m in c.mapping.items():
        if m:
            mapped_tables.setdefault(m["table"], []).append({"id": mid, "column": m["column"]})
    tables = [{
        "name": t,
        "ts": info["ts"],
        "labels": info["labels"],
        "values": info["values"],
        "types": info["types"],
        "collected": t in c.collect_tables,
        "mapped": mapped_tables.get(t, []),
    } for t, info in sorted(c.tables.items())]
    unmapped = [m["id"] for m in CATALOGUE if not c.mapping.get(m["id"])]
    return jsonify({"host": host, "db_path": c.db_path, "tables": tables, "unmapped": unmapped})


@app.route("/api/cpview/query")
def api_query():
    """
    Live read of the last N minutes of one table straight from the gateway's
    CPViewDB.dat - lets you inspect tables that are not being collected.
    """
    host, tbl = request.args.get("host"), request.args.get("table")
    minutes = min(int(request.args.get("minutes", 30)), 24 * 60)
    c = get_collector(host)
    if not c or tbl not in c.tables:
        return jsonify({"error": "unknown host or table"}), 404
    ts = c.tables[tbl]["ts"]
    sql = (f'SELECT * FROM "{tbl}" WHERE "{ts}" > (SELECT MAX("{ts}") FROM "{tbl}") - {minutes * 60} '
           f'ORDER BY "{ts}" DESC LIMIT 1000')
    out = transport.run(c.gw, build_script([("q", sqlite_cmd(c.db_path, sql, c.sqlite_bin))]))
    if out is None:
        return jsonify({"error": "gateway unreachable"}), 502
    try:
        header, rows = parse_csv(split_sections(out).get("q", ""))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"table": tbl, "ts": ts, "labels": c.tables[tbl]["labels"],
                    "header": header, "rows": rows[::-1]})


# ---- startup ----

def prune_loop():
    while True:
        try:
            store.prune(float(CPV["retention_days"]))
        except Exception as e:
            logger.error(f"Prune failed: {e}")
        time.sleep(3600)


def main():
    global store, transport
    parser = argparse.ArgumentParser(description="chkp-monitor CPView edition")
    parser.add_argument("--demo", action="store_true", help="run against synthetic gateways (no lab needed)")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    if args.demo:
        from cpview.demo import setup_demo, start_generator
        gateways, bin_dir = setup_demo(BASE_DIR, backfill_hours=float(CPV["backfill_hours"]))
        transport = DemoTransport(bin_dir)
        store_path = os.path.join(BASE_DIR, "data", "cpview_demo_store.db")
        if os.path.exists(store_path):
            os.remove(store_path)
        start_generator(gateways)
    else:
        creds_path = os.path.join(BASE_DIR, "credentials.json")
        if not os.path.exists(creds_path):
            logger.error("credentials.json not found. Run bootstrap.ps1 first or copy credentials.json.example "
                         "(or try: python cpview_server.py --demo)")
            sys.exit(1)
        with open(creds_path) as f:
            credentials = json.load(f)
        gateways = list(CPV.get("gateways") or CONFIG["gateways"])
        if CPV["include_management"]:
            m = CONFIG["management"]
            gateways.append({"name": m.get("name", m["host"]), "mgmt_ip": m["host"], "gaia_port": 443, "ssh_port": 22})
        transport = GatewayTransport({**CONFIG, "gateways": gateways, "cpview": CPV}, credentials)
        store_path = os.path.join(BASE_DIR, CPV["store_path"])

    store = Store(store_path)
    port = args.port or CPV["port"]

    logger.info("=" * 60)
    logger.info(f"chkp-monitor CPView edition starting{' (DEMO)' if args.demo else ''}")
    for gw in gateways:
        logger.info(f"Gateway: {gw['name']} ({gw['mgmt_ip']}) db={gw.get('cpview_db', CPV['db_path'])}")
    logger.info(f"Store: {store_path}  retention={CPV['retention_days']}d  backfill={CPV['backfill_hours']}h")
    logger.info(f"Dashboard: http://localhost:{port}")
    logger.info("=" * 60)

    for gw in gateways:
        c = CpviewCollector(gw, transport, store, CATALOGUE, CPV)
        collectors[gw["name"]] = c
        threading.Thread(
            target=c.run_forever,
            args=(int(CPV["poll_interval_seconds"]), int(CPV["schema_refresh_seconds"])),
            daemon=True, name=f"cpview-{gw['name']}",
        ).start()
    threading.Thread(target=prune_loop, daemon=True, name="cpview-prune").start()

    app.run(host=CONFIG["dashboard"].get("host", "0.0.0.0"), port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
