"""
vmhost - VMware Workstation host monitor (runs on the Windows host, not in the lab).

Every few seconds it samples:
  - each running VM (vmware-vmx.exe): host CPU, per-vCPU thread load, RAM, disk I/O,
    configured vCPUs/RAM/networks (.vmx), Native/WHP mode and recent warnings (vmware.log)
  - the host: per-logical-CPU load, memory, per-adapter network (incl. VMnetX), disk I/O
  - the top non-VM processes
and serves a local dashboard at http://127.0.0.1:8090 with history kept in SQLite.

Optionally it pulls chkp-monitor's /api/status (running on A-GUI inside the lab) and
shows the Check Point view of each gateway next to its VM.

Run:  python vmhost.py            (elevated PowerShell gives per-vCPU names + VM names faster)
"""
import json
import os
import re
import sys
import time
import sqlite3
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import psutil

import vmware_meta
import winthreads

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("vmhost")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "listen": "127.0.0.1",
    "port": 8090,
    "sample_seconds": 2,
    "vm_roots": [],
    "vmrun_path": None,
    "process_names": ["vmware-vmx.exe"],
    "history_db": "history.sqlite",
    "history_days": 7,
    "top_other": 8,
    "lab_status_url": None,
    "lab_poll_seconds": 30,
}
VCPU_RE = re.compile(r"vcpu-?(\d+)", re.IGNORECASE)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.environ.get("VMHOST_CONFIG") or os.path.join(BASE_DIR, "config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


# ---- History ---------------------------------------------------------------

class History:
    """One row per series per sample. series = 'host' or a VM display name."""

    def __init__(self, path: str, keep_days: int):
        self.path = path
        self.keep_days = keep_days
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS samples(
                ts REAL, series TEXT, cpu REAL, mem REAL, rd REAL, wr REAL)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_series_ts ON samples(series, ts)")
            self.db.commit()
        self.last_prune = 0.0

    def add(self, ts: float, rows: list[tuple]):
        with self.lock:
            self.db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?)",
                                [(ts, *r) for r in rows])
            if ts - self.last_prune > 3600:
                self.db.execute("DELETE FROM samples WHERE ts < ?", (ts - self.keep_days * 86400,))
                self.last_prune = ts
            self.db.commit()

    def query(self, series: str, minutes: float, points: int) -> list[dict]:
        since = time.time() - minutes * 60
        bucket = max(1.0, minutes * 60 / max(points, 1))
        with self.lock:
            rows = self.db.execute(
                """SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG(cpu), MAX(cpu), AVG(mem), AVG(rd), AVG(wr)
                   FROM samples WHERE series = ? AND ts >= ? GROUP BY t ORDER BY t""",
                (bucket, bucket, series, since)).fetchall()
        return [{"t": r[0], "cpu": r[1], "cpu_max": r[2], "mem": r[3], "rd": r[4], "wr": r[5]} for r in rows]

    def series(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT series FROM samples")]


# ---- Collector -------------------------------------------------------------

class VmTracker:
    """Per-vmware-vmx state carried between samples."""

    def __init__(self, proc: psutil.Process):
        self.proc = proc
        self.vmx: str | None = None
        self.info: dict = {}
        self.log: dict = {}
        self.next_resolve = 0.0
        self.next_log = 0.0
        self.prev_io = None
        self.prev_threads: dict[int, float] = {}
        self.peak_cpu = 0.0


class Collector:
    def __init__(self, cfg: dict, history: History):
        self.cfg = cfg
        self.history = history
        self.names = {n.lower() for n in cfg["process_names"]}
        self.ncpu = psutil.cpu_count() or 1
        self.vmrun = vmware_meta.find_vmrun(cfg.get("vmrun_path"))
        self.vms: dict[int, VmTracker] = {}
        self.prev_cpu: dict[int, tuple[float, float]] = {}   # pid -> (create_time, cpu seconds)
        self.prev_net = None
        self.prev_disk = None
        self.pid_map: dict[int, str] = {}
        self.next_pid_map = 0.0
        self.last_t = None
        self.lock = threading.Lock()
        self.state: dict = {"vms": [], "host": {}, "others": [], "lab": None, "notes": []}
        psutil.cpu_percent(percpu=True)  # prime
        logger.info(f"vmrun: {self.vmrun or 'not found'}; VM roots: {cfg['vm_roots'] or 'none'}")

    # -- VM identity -----------------------------------------------------

    def _resolve(self, pid: int, vm: VmTracker, now: float):
        if vm.vmx or now < vm.next_resolve:
            return
        vm.next_resolve = now + 15
        try:
            vm.vmx = vmware_meta.vmx_from_cmdline(vm.proc.cmdline())
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except psutil.NoSuchProcess:
            return
        if not vm.vmx:
            if pid not in self.pid_map and now >= self.next_pid_map:
                self.next_pid_map = now + 15
                self.pid_map = vmware_meta.build_pid_map(self.vmrun, self.cfg["vm_roots"])
            vm.vmx = self.pid_map.get(pid)
        if vm.vmx:
            vm.info = vmware_meta.parse_vmx(vm.vmx)

    # -- One sample ------------------------------------------------------

    def sample(self):
        t_wall = time.time()
        now = time.monotonic()
        dt = (now - self.last_t) if self.last_t else 0.0
        self.last_t = now
        notes = []

        percpu = psutil.cpu_percent(percpu=True)
        mem = psutil.virtual_memory()

        # Host network adapters (VMware's appear as "VMware Network Adapter VMnetX")
        nets = []
        cur_net = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        if self.prev_net and dt > 0:
            for nic, c in cur_net.items():
                p = self.prev_net.get(nic)
                if not p:
                    continue
                up = stats[nic].isup if nic in stats else None
                rx = max(0, c.bytes_recv - p.bytes_recv) / dt
                tx = max(0, c.bytes_sent - p.bytes_sent) / dt
                if up or rx or tx:
                    m = re.search(r"VMnet\d+", nic, re.IGNORECASE)
                    nets.append({"name": nic, "vmnet": m.group(0) if m else None, "up": up,
                                 "rx_bps": rx, "tx_bps": tx,
                                 "errors": c.errin + c.errout, "drops": c.dropin + c.dropout})
        self.prev_net = cur_net

        disks = []
        try:
            cur_disk = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            cur_disk = {}
        if self.prev_disk and dt > 0:
            for d, c in cur_disk.items():
                p = self.prev_disk.get(d)
                if p and not re.match(r"(loop|ram|zram|dm-)", d):
                    disks.append({"name": d, "rd_bps": max(0, c.read_bytes - p.read_bytes) / dt,
                                  "wr_bps": max(0, c.write_bytes - p.write_bytes) / dt,
                                  "busy_pct": min(100.0, max(0, getattr(c, "busy_time", 0) - getattr(p, "busy_time", 0)) / dt / 10)
                                  if hasattr(c, "busy_time") else None})
        self.prev_disk = cur_disk

        # Processes: CPU for everyone (for "top others"), full detail for VMs
        vm_rows, others, live = [], [], set()
        for proc in psutil.process_iter(["name"]):
            pid = proc.pid
            if pid == 0:
                continue
            name = proc.info.get("name") or ""
            try:
                ct = proc.create_time()
                cpu_s = sum(proc.cpu_times()[:2])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            live.add(pid)
            prev = self.prev_cpu.get(pid)
            self.prev_cpu[pid] = (ct, cpu_s)
            fresh = prev is None or prev[0] != ct or dt <= 0
            cores = 0.0 if fresh else max(0.0, cpu_s - prev[1]) / dt

            if name.lower() in self.names:
                vm = self.vms.get(pid)
                if vm is None or vm.proc is not proc:
                    vm = self.vms[pid] = VmTracker(proc)
                row = self._vm_row(pid, vm, cores, dt, now, fresh)
                if row:
                    vm_rows.append(row)
            elif not fresh and cores > 0:
                try:
                    rss = proc.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    rss = None
                others.append({"pid": pid, "name": name, "cpu_pct": cores / self.ncpu * 100, "rss": rss})

        for pid in [p for p in self.prev_cpu if p not in live]:
            del self.prev_cpu[pid]
        for pid in [p for p in self.vms if p not in live]:
            del self.vms[pid]
        winthreads.forget_except({tid for v in self.vms.values() for tid in v.prev_threads})

        others.sort(key=lambda r: r["cpu_pct"], reverse=True)
        vm_rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
        if vm_rows and not any(r["vcpus_live"] for r in vm_rows) and os.name == "nt":
            notes.append("Per-vCPU load needs the monitor to run elevated (Run as Administrator).")
        if any(r["vmx"] is None for r in vm_rows):
            notes.append("Some VMs are unnamed: add their parent folder to vm_roots in config.json, or run elevated.")

        host = {
            "ncpu": self.ncpu,
            "cpu_pct": sum(percpu) / len(percpu) if percpu else 0,
            "percpu": percpu,
            "mem_total": mem.total, "mem_used": mem.total - mem.available, "mem_pct": mem.percent,
            "nets": sorted(nets, key=lambda n: (n["vmnet"] is None, n["name"])),
            "disks": disks,
        }

        if dt > 0:
            rows = [("host", host["cpu_pct"], host["mem_used"],
                     sum(d["rd_bps"] for d in disks), sum(d["wr_bps"] for d in disks))]
            rows += [(r["name"], r["cpu_pct"], r["rss"], r["rd_bps"], r["wr_bps"]) for r in vm_rows]
            self.history.add(t_wall, rows)

        with self.lock:
            self.state.update({"ts": t_wall, "interval": self.cfg["sample_seconds"], "host": host,
                               "vms": vm_rows if dt > 0 else [],
                               "others": others[: self.cfg["top_other"]], "notes": notes,
                               "vmrun": self.vmrun})

    def _vm_row(self, pid: int, vm: VmTracker, cores: float, dt: float, now: float, fresh: bool) -> dict | None:
        p = vm.proc
        self._resolve(pid, vm, now)
        if vm.vmx and now >= vm.next_log:
            vm.next_log = now + 60
            vm.log = vmware_meta.parse_log(vm.vmx)
        try:
            mi = p.memory_info()
            rss, private = mi.rss, getattr(mi, "private", None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            rss = private = None

        rd = wr = 0.0
        try:
            io = p.io_counters()
            if vm.prev_io and dt > 0:
                rd = max(0, io.read_bytes - vm.prev_io.read_bytes) / dt
                wr = max(0, io.write_bytes - vm.prev_io.write_bytes) / dt
            vm.prev_io = io
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        # Threads: vCPU threads are named "vcpu-N"; others are VMware's own (MKS, SVGA, disk I/O...)
        vcpus, busy_other = [], []
        try:
            threads = p.threads()
            cur = {}
            for th in threads:
                total = th.user_time + th.system_time
                cur[th.id] = total
                prev = vm.prev_threads.get(th.id)
                if prev is None or dt <= 0:
                    continue
                pct = max(0.0, total - prev) / dt * 100   # % of one core
                tname = winthreads.thread_name(th.id)
                m = VCPU_RE.search(tname or "")
                if m:
                    vcpus.append({"index": int(m.group(1)), "pct": min(pct, 100.0)})
                elif pct >= 2:
                    busy_other.append({"name": tname or f"tid {th.id}", "pct": min(pct, 100.0)})
            vm.prev_threads = cur
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        vcpus.sort(key=lambda v: v["index"])
        busy_other.sort(key=lambda v: v["pct"], reverse=True)

        cpu_pct = cores / self.ncpu * 100
        if not fresh:
            vm.peak_cpu = max(vm.peak_cpu, cpu_pct)
        info = vm.info or {}
        return {
            "pid": pid,
            "name": info.get("display_name") or f"vmware-vmx ({pid})",
            "vmx": vm.vmx,
            "cpu_pct": cpu_pct,
            "cores": cores,
            "peak_pct": vm.peak_cpu,
            "rss": rss, "private": private,
            "rd_bps": rd, "wr_bps": wr,
            "vcpus_live": vcpus,
            "busy_threads": busy_other[:4],
            "config": {k: info.get(k) for k in ("vcpus", "cores_per_socket", "memory_mb", "guest_os",
                                                 "hw_version", "nics", "disks")},
            "mode": vm.log.get("mode"), "monitor": vm.log.get("monitor"), "vbs": vm.log.get("vbs"),
            "problems": vm.log.get("problems", []),
        }

    def run(self):
        while True:
            start = time.monotonic()
            try:
                self.sample()
            except Exception:
                logger.exception("sample failed")
            time.sleep(max(0.2, self.cfg["sample_seconds"] - (time.monotonic() - start)))

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state, default=str))


def lab_poller(cfg: dict, collector: Collector):
    """Pull chkp-monitor's /api/status (inside the lab) so its view sits next to the VMs."""
    url = cfg["lab_status_url"]
    while True:
        lab = {"url": url, "fetched": time.time()}
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                lab["data"] = json.load(r)
        except Exception as e:
            lab["error"] = str(e)
        with collector.lock:
            collector.state["lab"] = lab
        time.sleep(cfg["lab_poll_seconds"])


# ---- HTTP ------------------------------------------------------------------

def make_handler(collector: Collector, history: History):
    static_dir = os.path.join(BASE_DIR, "static")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                with open(os.path.join(static_dir, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._json(collector.snapshot())
            elif u.path == "/api/history":
                series = q.get("series", ["host"])[0]
                minutes = min(float(q.get("minutes", ["30"])[0]), 7 * 1440)
                points = min(int(q.get("points", ["240"])[0]), 2000)
                self._json({"series": series, "minutes": minutes, "rows": history.query(series, minutes, points)})
            elif u.path == "/api/series":
                self._json(history.series())
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def main():
    cfg = load_config()
    db_path = cfg["history_db"]
    if not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    history = History(db_path, cfg["history_days"])
    collector = Collector(cfg, history)
    threading.Thread(target=collector.run, daemon=True, name="collector").start()
    if cfg.get("lab_status_url"):
        threading.Thread(target=lab_poller, args=(cfg, collector), daemon=True, name="lab").start()

    server = ThreadingHTTPServer((cfg["listen"], cfg["port"]), make_handler(collector, history))
    logger.info(f"Dashboard: http://{cfg['listen']}:{cfg['port']}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
