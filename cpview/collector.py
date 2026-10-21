"""
CPView history collector.

Per gateway, one background thread:
  1. Discovers the CPViewDB.dat schema (hourly) and resolves the metric
     catalogue against it.
  2. Every poll, sends ONE script that
       - pulls new rows (Timestamp > cursor) from each collected table
       - grabs a small system snapshot (version, uptime, /proc, df, policy)
     and stores the results locally.
First contact backfills `backfill_hours` of history that the gateway already
holds, in chunks of `max_rows_per_table`, polling quickly until caught up.
"""
import re
import time
import logging

from cpview.schema import SCHEMA_SQL, build_schema, parse_csv, resolve_all
from cpview.transport import build_script, sqlite_cmd, split_sections

logger = logging.getLogger("chkp-monitor.cpview.collector")

DEFAULT_DB = "/var/log/CPView_history/CPViewDB.dat"

INFO_SECTIONS = [
    ("i_now", "date +%s"),
    ("i_db", "stat -c '%s %Y' \"$CPV_DB\" 2>/dev/null || echo missing"),
    ("i_release", "cat /etc/cp-release 2>/dev/null"),
    ("i_fwver", "fw ver 2>/dev/null; fw ver -k 2>/dev/null"),
    ("i_arch", "uname -m"),
    ("i_hostname", "hostname"),
    ("i_uptime", "cat /proc/uptime"),
    ("i_cpus", "grep -c ^processor /proc/cpuinfo"),
    ("i_meminfo", "cat /proc/meminfo"),
    ("i_df", "df -P -k"),
    ("i_stat", "head -1 /proc/stat; sleep 1; head -1 /proc/stat"),
    ("i_policy", "cpstat fw -f policy 2>/dev/null"),
    ("i_hw", "dmidecode -s system-product-name 2>/dev/null || cat /sys/class/dmi/id/product_name 2>/dev/null"),
    ("i_role", "for f in FwIsFirewallModule FwIsFirewallMgmt FwIsLogServer FwIsVSX; do echo \"$f=$(cpprod_util $f 2>/dev/null)\"; done"),
    ("i_ha", "cphaprob stat 2>/dev/null | head -30"),
]


class CpviewCollector:
    def __init__(self, gw: dict, transport, store, catalogue: list[dict], cfg: dict):
        self.gw = gw
        self.name = gw["name"]
        self.transport = transport
        self.store = store
        self.catalogue = catalogue
        self.cfg = cfg
        self.db_path = gw.get("cpview_db", cfg.get("db_path", DEFAULT_DB))
        self.max_rows = int(cfg.get("max_rows_per_table", 1500))
        self.backfill = int(float(cfg.get("backfill_hours", 24)) * 3600)
        self.extra_tables = [re.compile(p, re.I) for p in cfg.get("extra_tables", [])]
        self.tables = {}
        self.mapping = {}
        self.collect_tables = []
        self.schema_time = 0
        self.status = {
            "state": "starting", "error": None, "method": None, "last_poll": None,
            "db_present": None, "db_bytes": None, "db_age_seconds": None,
            "tables_total": 0, "tables_collected": 0, "catching_up": False,
            "metrics_mapped": 0, "metrics_total": len(catalogue),
        }

    # ---- schema ----

    def refresh_schema(self) -> bool:
        script = f"CPV_DB={_q(self.db_path)}\n" + build_script([("schema", sqlite_cmd(self.db_path, SCHEMA_SQL))])
        out = self.transport.run(self.gw, script)
        if out is None:
            self._fail("gateway unreachable (Gaia API and SSH)")
            return False
        try:
            tables = build_schema(split_sections(out).get("schema", ""))
        except RuntimeError as e:
            self._fail(f"sqlite3: {e}")
            return False
        if not tables:
            self._fail(f"no CPView tables found in {self.db_path} - is 'cpview history' enabled?")
            return False
        self.tables = tables
        self.mapping = resolve_all(self.catalogue, tables)
        wanted = {m["table"] for m in self.mapping.values() if m}
        wanted |= {t for t in tables if any(p.search(t) for p in self.extra_tables)}
        if self.cfg.get("collect_all_tables"):
            wanted = set(tables)
        self.collect_tables = sorted(wanted)
        self.schema_time = time.time()
        self.store.set_schema(self.name, {
            "tables": tables, "mapping": self.mapping, "collected": self.collect_tables,
        })
        mapped = sum(1 for m in self.mapping.values() if m)
        self.status.update(tables_total=len(tables), tables_collected=len(self.collect_tables),
                           metrics_mapped=mapped)
        logger.info(f"{self.name}: {len(tables)} CPView tables, {mapped}/{len(self.catalogue)} metrics mapped, "
                    f"collecting {len(self.collect_tables)} tables")
        return True

    # ---- polling ----

    def poll(self) -> bool:
        """One collection cycle. Returns True if more backlog is waiting."""
        cursors = self.store.get_cursors(self.name)
        sections = []
        for i, table in enumerate(self.collect_tables):
            ts = self.tables[table]["ts"]
            if table in cursors:
                where = f'"{ts}" > {int(cursors[table])}'
            else:
                where = f'"{ts}" > (SELECT MAX("{ts}") FROM "{table}") - {self.backfill}'
            sql = f'SELECT * FROM "{table}" WHERE {where} ORDER BY "{ts}" LIMIT {self.max_rows}'
            sections.append((f"t{i}", sqlite_cmd(self.db_path, sql)))
        sections += INFO_SECTIONS

        out = self.transport.run(self.gw, f"CPV_DB={_q(self.db_path)}\n" + build_script(sections))
        if out is None:
            self._fail("gateway unreachable (Gaia API and SSH)")
            return False
        parts = split_sections(out)

        backlog = False
        errors = []
        for i, table in enumerate(self.collect_tables):
            try:
                more = self._ingest(table, parts.get(f"t{i}", ""), cursors.get(table))
                backlog = backlog or more
            except Exception as e:
                errors.append(f"{table}: {e}")
        if errors:
            logger.warning(f"{self.name}: {len(errors)} table errors, first: {errors[0]}")

        info = parse_info(parts)
        info["collector"] = {"method": self.transport.last_method.get(self.name)}
        self.store.set_info(self.name, info)

        self.status.update(
            state="ok", error=errors[0] if errors else None,
            method=self.transport.last_method.get(self.name),
            last_poll=time.time(), catching_up=backlog,
            db_present=info.get("db_present"), db_bytes=info.get("db_bytes"),
            db_age_seconds=info.get("db_age_seconds"),
        )
        if info.get("db_present") is False:
            self.status.update(state="warning", error=f"{self.db_path} not found - run 'cpview history on'")
        elif (info.get("db_age_seconds") or 0) > 300:
            self.status.update(state="warning", error="CPView history DB not updated for >5 min - check 'cpview history'")
        return backlog

    def _ingest(self, table: str, text: str, prev_cursor: int | None) -> bool:
        header, rows = parse_csv(text)
        if not rows:
            return False
        info = self.tables[table]
        idx = {c: i for i, c in enumerate(header)}
        ts_i = idx.get(info["ts"])
        if ts_i is None:
            raise RuntimeError(f"timestamp column {info['ts']} missing from result")
        label_i = [idx[c] for c in info["labels"] if c in idx]
        value_i = [(c, idx[c]) for c in info["values"] if c in idx]

        samples = []
        max_ts = prev_cursor or 0
        for row in rows:
            try:
                ts = int(float(row[ts_i]))
            except (ValueError, IndexError):
                continue
            max_ts = max(max_ts, ts)
            label = " / ".join(row[i] for i in label_i if i < len(row) and row[i] != "")
            for col, i in value_i:
                if i >= len(row) or row[i] == "":
                    continue
                try:
                    samples.append((col, label, ts, float(row[i])))
                except ValueError:
                    continue
        self.store.add_samples(self.name, table, samples)

        hit_limit = len(rows) >= self.max_rows
        cursor = max_ts
        if hit_limit and max_ts - 1 > (prev_cursor or 0):
            # LIMIT may have split rows sharing the last timestamp - re-read it
            cursor = max_ts - 1
        self.store.set_cursor(self.name, table, cursor)
        return hit_limit

    # ---- loop ----

    def run_forever(self, interval: int, schema_refresh: int = 3600):
        while True:
            more = False
            try:
                if not self.tables or time.time() - self.schema_time > schema_refresh:
                    self.refresh_schema()
                if self.tables:
                    more = self.poll()
            except Exception as e:
                logger.exception(f"{self.name}: collector error")
                self._fail(str(e))
            time.sleep(2 if more else interval)

    def _fail(self, msg: str):
        logger.error(f"{self.name}: {msg}")
        self.status.update(state="error", error=msg, last_poll=time.time())


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ---- system snapshot parsing ----

def parse_info(parts: dict) -> dict:
    info = {}

    def num(text, default=None):
        try:
            return int(text.strip().split()[0])
        except (ValueError, IndexError, AttributeError):
            return default

    now = num(parts.get("i_now", ""))
    info["gateway_time"] = now

    db = parts.get("i_db", "").strip()
    if db and db != "missing":
        size, mtime = (db.split() + [None, None])[:2]
        info["db_present"] = True
        info["db_bytes"] = num(size)
        info["db_age_seconds"] = (now - num(mtime)) if now and num(mtime) else None
    elif db == "missing":
        info["db_present"] = False

    release = parts.get("i_release", "").strip()
    m = re.search(r"\b(R\d+(?:\.\d+)*)", release)
    fwver = parts.get("i_fwver", "")
    info["os_version"] = m.group(1) if m else (re.search(r"\b(R\d+(?:\.\d+)*)", fwver) or [None, None])[1]
    info["release"] = release or None
    kb = re.search(r"kernel:.*?Build\s+(\d+)", fwver) or re.search(r"Build\s+(\d+)", fwver)
    info["fw_build"] = kb.group(1) if kb else None
    arch = parts.get("i_arch", "").strip()
    info["os_edition"] = "Gaia 64Bit" if "64" in arch else ("Gaia 32Bit" if arch else None)
    info["hostname"] = parts.get("i_hostname", "").strip() or None

    up = parts.get("i_uptime", "").split()
    info["uptime_seconds"] = float(up[0]) if up else None
    info["cpu_cores"] = num(parts.get("i_cpus", ""))
    info["hardware"] = parts.get("i_hw", "").strip().splitlines()[0] if parts.get("i_hw", "").strip() else None

    mem = {}
    for line in parts.get("i_meminfo", "").splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            mem[k.strip()] = num(v, 0) * 1024
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable", (mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)))
    info["ram_total_bytes"] = total
    info["ram_used_bytes"] = (total - avail) if total else None
    info["swap_total_bytes"] = mem.get("SwapTotal")
    swap_used = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) if "SwapTotal" in mem else None
    info["swap_used_bytes"] = swap_used

    fs = {}
    for line in parts.get("i_df", "").splitlines()[1:]:
        f = line.split()
        if len(f) >= 6 and f[1].isdigit():
            fs[f[5]] = {"total_bytes": int(f[1]) * 1024, "used_bytes": int(f[2]) * 1024,
                        "percent": float(f[4].rstrip("%")) if f[4].rstrip("%").isdigit() else None}
    info["filesystems"] = fs

    stat = [l.split() for l in parts.get("i_stat", "").splitlines() if l.startswith("cpu ")]
    cpu_busy = None
    if len(stat) == 2:
        a = [int(x) for x in stat[0][1:]]
        b = [int(x) for x in stat[1][1:]]
        d = [y - x for x, y in zip(a, b)]
        idle = d[3] + (d[4] if len(d) > 4 else 0)
        cpu_busy = round(100 * (sum(d) - idle) / sum(d), 2) if sum(d) > 0 else None

    pol = parts.get("i_policy", "")
    pm = re.search(r"Policy name:\s*(.+)", pol)
    pt = re.search(r"Install time:\s*(.+)", pol, re.IGNORECASE)
    info["policy_name"] = pm.group(1).strip() if pm else None
    info["policy_install_time"] = pt.group(1).strip() if pt else None

    roles = dict(l.split("=", 1) for l in parts.get("i_role", "").splitlines() if "=" in l)
    is_ = lambda k: roles.get(k, "").strip() == "1"
    if is_("FwIsVSX"):
        info["role"] = "Check Point VSX Gateway"
    elif is_("FwIsFirewallModule") and is_("FwIsFirewallMgmt"):
        info["role"] = "Check Point Standalone"
    elif is_("FwIsFirewallModule"):
        info["role"] = "Check Point Security Gateway"
    elif is_("FwIsFirewallMgmt"):
        info["role"] = "Check Point Management Server"
    elif is_("FwIsLogServer"):
        info["role"] = "Check Point Log Server"
    else:
        info["role"] = None

    ha = parts.get("i_ha", "")
    hm = re.search(r"\(local\)\s+\S+\s+\S+\s+(ACTIVE|STANDBY|DOWN|READY|INIT\w*)", ha, re.I)
    info["cluster_state"] = hm.group(1).upper() if hm else None

    def pct(used, total):
        return round(100 * used / total, 2) if used is not None and total else None

    logs_fs = fs.get("/var/log") or fs.get("/var/log/") or fs.get("/")
    info["gauges"] = {
        "cpu_busy": cpu_busy,
        "ram_used": pct(info["ram_used_bytes"], total),
        "swap_used": pct(swap_used, info["swap_total_bytes"]) if info["swap_total_bytes"] else 0.0,
        "root_fs_used": fs.get("/", {}).get("percent"),
        "logs_fs_used": logs_fs.get("percent") if logs_fs else None,
    }
    info["root_fs_total_bytes"] = fs.get("/", {}).get("total_bytes")
    return info
