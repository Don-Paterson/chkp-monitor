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

from flask import Flask, jsonify, send_from_directory

from collectors.mgmt_api import MgmtApiCollector
from collectors.gaia_api import GaiaApiCollector
from collectors.ssh_fallback import SshFallbackCollector

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chkp-monitor")

# ---- Load config ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE_DIR, "config.json")) as f:
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


def gaia_collector_loop():
    """Background thread: polls gateways via Gaia API, falls back to SSH."""
    gaia_collector = GaiaApiCollector(CONFIG, CREDENTIALS)
    ssh_collector = SshFallbackCollector(CONFIG, CREDENTIALS)
    interval = CONFIG["dashboard"]["poll_interval_seconds"]

    while True:
        try:
            gaia_data = gaia_collector.collect()
            gw_results = {}
            ssh_used = False

            for gw_name, gw_data in gaia_data.get("gateways", {}).items():
                if gw_data.get("gaia_api_reachable"):
                    gw_data["data_source"] = "gaia_api"
                    gw_results[gw_name] = enrich_gateway_data(gw_data)
                else:
                    # Fallback to SSH
                    logger.warning(f"Gaia API unreachable for {gw_name}, trying SSH fallback")
                    gw_config = next((g for g in CONFIG["gateways"] if g["name"] == gw_name), None)
                    if gw_config:
                        ssh_data = ssh_collector.collect_gateway(gw_config)
                        ssh_data["data_source"] = "ssh_fallback"
                        gw_results[gw_name] = enrich_gateway_data(ssh_data)
                        ssh_used = True
                    else:
                        gw_data["data_source"] = "failed"
                        gw_results[gw_name] = enrich_gateway_data(gw_data)

            with state_lock:
                current_state["gateways"] = gw_results
                current_state["collector_status"]["gaia_api"] = "ok"
                current_state["collector_status"]["ssh_fallback"] = "active" if ssh_used else "idle"
                current_state["last_update"] = datetime.now().isoformat()

            logger.info("Gateway collection complete")
        except Exception as e:
            logger.error(f"Gateway collector error: {e}")
            with state_lock:
                current_state["collector_status"]["gaia_api"] = f"error: {e}"
        time.sleep(interval)


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
    logger.info(f"Dashboard: http://localhost:{CONFIG['dashboard']['port']}")
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
