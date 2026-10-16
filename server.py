"""
chkp-monitor - Check Point Lab Health Dashboard
Main server: Flask app with background collector threads.
"""
import json
import os
import sys
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, send_from_directory

from collectors.mgmt_api import MgmtApiCollector
from collectors.gaia_api import GaiaApiCollector
from collectors.ssh_fallback import SshFallbackCollector
from collectors import commands

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chkp-monitor")

# ---- Load config ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# CHKP_CONFIG picks another topology file, e.g. config.ccvs-r8220.json
CONFIG_PATH = os.environ.get("CHKP_CONFIG") or os.path.join(BASE_DIR, "config.json")
if not os.path.isabs(CONFIG_PATH):
    CONFIG_PATH = os.path.join(BASE_DIR, CONFIG_PATH)
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

creds_path = os.path.join(BASE_DIR, "credentials.json")
if not os.path.exists(creds_path):
    logger.error("credentials.json not found. Run bootstrap.ps1 first or copy credentials.json.example")
    sys.exit(1)

with open(creds_path) as f:
    CREDENTIALS = json.load(f)

# ---- Shared state ----
state_lock = threading.Lock()
current_state = {
    "mgmt": {},
    "gateways": {},
    "last_update": None,
    "collector_status": {
        "mgmt_api": "starting",
        "gaia_api": "starting",
        "ssh_fallback": "idle",
    },
}

# ---- Thresholds ----
THRESHOLDS = CONFIG.get("thresholds", {})


def compute_rag(value, warn_threshold, crit_threshold, invert=False):
    """
    Compute RAG status: green/amber/red.
    If invert=True, lower values are worse (e.g., free space).
    """
    if value is None:
        return "unknown"
    if invert:
        if value <= crit_threshold:
            return "red"
        elif value <= warn_threshold:
            return "amber"
        return "green"
    else:
        if value >= crit_threshold:
            return "red"
        elif value >= warn_threshold:
            return "amber"
        return "green"


def enrich_gateway_data(gw_data: dict) -> dict:
    """Add RAG statuses to gateway data based on thresholds."""
    os_data = gw_data.get("os", {})
    fw_data = gw_data.get("firewall", {})
    km_data = gw_data.get("kernel_memory", {})

    gw_data["rag"] = {
        "cpu": compute_rag(os_data.get("cpu_percent"), THRESHOLDS.get("cpu_warn", 70), THRESHOLDS.get("cpu_crit", 90)),
        "memory": compute_rag(os_data.get("memory_percent"), THRESHOLDS.get("memory_warn", 75), THRESHOLDS.get("memory_crit", 90)),
        "disk": compute_rag(os_data.get("disk_percent"), THRESHOLDS.get("disk_warn", 80), THRESHOLDS.get("disk_crit", 95)),
        "connections": compute_rag(fw_data.get("connections_current"), THRESHOLDS.get("connections_warn", 20000), THRESHOLDS.get("connections_crit", 40000)),
        "kernel_memory": compute_rag(km_data.get("kernel_memory_used_percent"), THRESHOLDS.get("kernel_memory_warn", 75), THRESHOLDS.get("kernel_memory_crit", 90)),
    }

    # Overall gateway RAG: worst of all individual RAGs
    statuses = [v for v in gw_data["rag"].values() if v != "unknown"]
    if "red" in statuses:
        gw_data["rag"]["overall"] = "red"
    elif "amber" in statuses:
        gw_data["rag"]["overall"] = "amber"
    elif statuses:
        gw_data["rag"]["overall"] = "green"
    else:
        gw_data["rag"]["overall"] = "unknown"

    return gw_data


# ---- Collector threads ----

def mgmt_collector_loop():
    """Background thread: polls management API."""
    collector = MgmtApiCollector(CONFIG, CREDENTIALS)
    interval = CONFIG["dashboard"]["poll_interval_seconds"]

    while True:
        try:
            data = collector.collect()
            with state_lock:
                current_state["mgmt"] = data
                current_state["collector_status"]["mgmt_api"] = "ok"
                current_state["last_update"] = datetime.now().isoformat()
            logger.info("Management API collection complete")
        except Exception as e:
            logger.error(f"Management API collector error: {e}")
            with state_lock:
                current_state["collector_status"]["mgmt_api"] = f"error: {e}"
        time.sleep(interval)


def collect_one_gateway(gw_config: dict, gaia_collector, ssh_collector, previous: dict) -> dict:
    """Gaia API first, SSH fallback second; adds interface rates from the previous poll."""
    name = gw_config["name"]
    data = gaia_collector.collect_gateway(gw_config)
    if data.get("gaia_api_reachable"):
        data["data_source"] = "gaia_api"
    else:
        logger.warning(f"Gaia API unreachable for {name}, trying SSH fallback")
        data = ssh_collector.collect_gateway(gw_config)
        data["data_source"] = "ssh_fallback" if data.get("ssh_reachable") else "failed"
    data["collected_at"] = time.time()
    prev = previous.get(name)
    if prev:
        commands.add_rates(data, prev, data["collected_at"] - prev.get("collected_at", 0))
    return enrich_gateway_data(data)


def gaia_collector_loop():
    """Background thread: polls all gateways in parallel (one combined script each)."""
    gaia_collector = GaiaApiCollector(CONFIG, CREDENTIALS)
    ssh_collector = SshFallbackCollector(CONFIG, CREDENTIALS)
    interval = CONFIG["dashboard"]["poll_interval_seconds"]
    gateways = CONFIG["gateways"]
    pool = ThreadPoolExecutor(max_workers=max(1, min(8, len(gateways))), thread_name_prefix="gw")

    while True:
        started = time.time()
        try:
            with state_lock:
                previous = dict(current_state["gateways"])
            futures = {gw["name"]: pool.submit(collect_one_gateway, gw, gaia_collector, ssh_collector, previous)
                       for gw in gateways}
            gw_results = {}
            for name, fut in futures.items():
                try:
                    gw_results[name] = fut.result()
                except Exception as e:
                    logger.error(f"{name}: collection failed: {e}")
                    gw_results[name] = enrich_gateway_data({"name": name, "data_source": "failed", "error": str(e)})

            sources = {g.get("data_source") for g in gw_results.values()}
            with state_lock:
                current_state["gateways"] = gw_results
                current_state["collector_status"]["gaia_api"] = "ok" if "gaia_api" in sources or not gw_results else "error: no gateway reachable via Gaia API"
                current_state["collector_status"]["ssh_fallback"] = "active" if "ssh_fallback" in sources else "idle"
                current_state["last_update"] = datetime.now().isoformat()
            logger.info(f"Gateway collection complete in {time.time() - started:.1f}s")
        except Exception as e:
            logger.error(f"Gateway collector error: {e}")
            with state_lock:
                current_state["collector_status"]["gaia_api"] = f"error: {e}"
        time.sleep(max(1, interval - (time.time() - started)))


# ---- Flask app ----

app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


@app.route("/api/status")
def api_status():
    """Return full current state as JSON."""
    with state_lock:
        return jsonify(current_state)


@app.route("/api/health")
def api_health():
    """Simple health check for the dashboard itself."""
    return jsonify({"status": "ok", "uptime": time.time()})


def main():
    logger.info("=" * 60)
    logger.info("chkp-monitor starting")
    logger.info(f"Management server: {CONFIG['management']['host']}")
    for gw in CONFIG["gateways"]:
        logger.info(f"Gateway: {gw['name']} ({gw['mgmt_ip']})")
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info(f"Dashboard: http://localhost:{CONFIG['dashboard']['port']}  (JSON for vmhost: /api/status)")
    logger.info("=" * 60)

    # Start collector threads
    mgmt_thread = threading.Thread(target=mgmt_collector_loop, daemon=True, name="mgmt-collector")
    gaia_thread = threading.Thread(target=gaia_collector_loop, daemon=True, name="gaia-collector")
    mgmt_thread.start()
    gaia_thread.start()

    # Start Flask
    app.run(
        host=CONFIG["dashboard"]["host"],
        port=CONFIG["dashboard"]["port"],
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
