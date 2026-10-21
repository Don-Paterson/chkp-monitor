"""
CPView history collector.

Per gateway, one background thread:
  1. Discovers the CPViewDB.dat schema (hourly) and resolves the metric
     catalogue against it.
  2. Every poll, sends ONE script that
       - pulls new rows (Timestamp > cursor) from each collected table
       - runs `cpview -m` for live values (R82 flushes the DB only every few hours)
       - grabs a small system snapshot (version, uptime, /proc, df, policy)
     and stores the results locally. Label columns that reference a lookup
     table (R82: cpview_ref_table) are resolved to names before storing.
First contact backfills `backfill_hours` of history that the gateway already
holds, in chunks of `max_rows_per_table`, polling quickly until caught up.
"""
import os
import re
import time
import logging

from cpview.live import LIVE_TABLE, encode_labels, index_metrics, parse_metrics, resolve_live
from cpview.schema import SCHEMA_SQL, build_schema_full, parse_csv, ref_value_column, resolve_all
from cpview.transport import build_script, sqlite_cmd, split_sections

logger = logging.getLogger("chkp-monitor.cpview.collector")

DEFAULT_DB = "/var/log/CPView_history/CPViewDB.dat"

# Where CPView keeps its history DB: R80.x-R81.x use CPView_history/CPViewDB.dat,
# R82 moved it under CPView Services (cpview -s on|stat).
HISTORY_GLOBS = '/var/log/CPView_history/* /var/log/opt/CPshrd-*/cpview_services/*'

# Finds readable SQLite files (configured path first, then the known history
# locations) and a sqlite3 binary - run-script's PATH can be minimal.
PROBE_SECTIONS = [
    ("p_dbs", f'for f in "$CPV_DB" "$(dirname "$CPV_DB")"/* {HISTORY_GLOBS}; do '
              '[ -f "$f" ] && [ -r "$f" ] && [ "$(head -c 15 "$f" 2>/dev/null)" = "SQLite format 3" ] '
              '&& echo "$(stat -c \'%Y %s\' "$f") $f"; done | sort -u'),
    ("p_sqlite", 'command -v sqlite3 || for b in /usr/bin/sqlite3 /bin/sqlite3 /usr/local/bin/sqlite3 '
                 '"$CPDIR"/bin/sqlite3 /opt/CPshrd-*/bin/sqlite3; do [ -x "$b" ] && echo "$b" && break; done'),
    # Shows each candidate's size and first bytes, so a non-SQLite format is obvious in the error
    ("p_diag", f'echo "user: $(id)"; for f in "$CPV_DB" {HISTORY_GLOBS}; do [ -f "$f" ] && '
               'echo "$f $(stat -c %s "$f")B header=$(head -c 16 "$f" | tr -c "[:print:]" ".")"; done; '
               'cpview -s stat 2>/dev/null | head -3'),
]

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
        self.sqlite_bin = "sqlite3"
        self.max_rows = int(cfg.get("max_rows_per_table", 1500))
        self.backfill = int(float(cfg.get("backfill_hours", 24)) * 3600)
        self.extra_tables = [re.compile(p, re.I) for p in cfg.get("extra_tables", [])]
        self.tables = {}
        self.mapping = {}
        self.collect_tables = []
        self.schema_time = 0
        self.ref_meta = {}   # ref_table -> (key_col, value_col)
        self.refs = {}       # ref_table -> {key: name}
        self.live_enabled = cfg.get("live_metrics", True)
        self.live_extra = [re.compile(p, re.I) for p in cfg.get("live_extra", [])]
        self.live_mapping = {}
        self.live_index = []
        self.live_ts = None
        self.status = {
            "state": "starting", "error": None, "method": None, "last_poll": None,
            "db_present": None, "db_bytes": None, "db_age_seconds": None,
            "tables_total": 0, "tables_collected": 0, "catching_up": False,
            "metrics_mapped": 0, "metrics_total": len(catalogue),
            "live_metrics": 0, "live_age_seconds": None, "db_note": None,
        }

    # ---- schema ----

    def locate_db(self) -> bool:
        """
        Confirm the CPView DB path and sqlite3 binary. Falls back to the newest
        SQLite file in the history dir if the configured file isn't there; if
        nothing usable is found, logs what the gateway does have.
        """
        out = self.transport.run(self.gw, f"CPV_DB={_q(self.db_path)}\n" + build_script(PROBE_SECTIONS))
        if out is None:
            self._fail("gateway unreachable (Gaia API and SSH)")
            return False
        parts = split_sections(out)
        found = []
        for line in parts.get("p_dbs", "").splitlines():
            f = line.split(" ", 2)
            if len(f) == 3 and f[0].isdigit():
                found.append((int(f[0]), int(f[1]), f[2]))
        sqlite_bin = (parts.get("p_sqlite", "").strip().splitlines() or [""])[0]
        diag = " | ".join(l.strip() for l in parts.get("p_diag", "").splitlines() if l.strip())

        if not sqlite_bin:
            self._fail(f"sqlite3 binary not found in run-script environment. Gateway says: {diag}")
            return False
        self.sqlite_bin = sqlite_bin
        if not found:
            self._fail(f"CPView history is not enabled or the DB is missing - on the gateway (expert mode) run 'cpview -s on' (R82) or 'cpview history on' (R81.x), then wait a few minutes; this collector retries every poll. Gateway says: {diag}")
            return False
        paths = [p for _, _, p in found]
        if self.db_path not in paths:
            # Prefer CPView-named files over any other SQLite file in those dirs
            named = [f for f in found if "cpview" in os.path.basename(f[2]).lower()]
            newest = max(named or found)[2]
            logger.warning(f"{self.name}: {self.db_path} not usable, using {newest} (found: {paths})")
            self.db_path = newest
        return True

    def refresh_schema(self) -> bool:
        if not self.locate_db():
            return False
        script = f"CPV_DB={_q(self.db_path)}\n" + build_script([("schema", sqlite_cmd(self.db_path, SCHEMA_SQL, self.sqlite_bin))])
        out = self.transport.run(self.gw, script)
        if out is None:
            self._fail("gateway unreachable (Gaia API and SSH)")
            return False
        try:
            tables, ref_tables = build_schema_full(split_sections(out).get("schema", ""))
        except RuntimeError as e:
            self._fail(f"sqlite3: {e}")
            return False
        if not tables:
            self._fail(f"no CPView tables found in {self.db_path} - is 'cpview history' enabled?")
            return False
        self.tables = tables
        self.ref_meta = {}
        for rt, cols in ref_tables.items():
            keys = {r[1] for t in tables.values() for r in t["refs"].values() if r[0] == rt}
            key = next(iter(keys)) if keys else None
            val = ref_value_column(cols, key) if key else None
            if key and val:
                self.ref_meta[rt] = (key, val)
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
        mapped = self._mapped_count()
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
            sections.append((f"t{i}", sqlite_cmd(self.db_path, sql, self.sqlite_bin)))
        if self.live_enabled:
            sections.append(("live", "cpview -m 2>/dev/null"))
        sections += INFO_SECTIONS

        out = self.transport.run(self.gw, f"CPV_DB={_q(self.db_path)}\n" + build_script(sections))
        if out is None:
            self._fail("gateway unreachable (Gaia API and SSH)")
            return False
        parts = split_sections(out)

        backlog = False
        errors = []
        parsed = {}
        for i, table in enumerate(self.collect_tables):
            try:
                parsed[table] = parse_csv(parts.get(f"t{i}", ""))
            except Exception as e:
                errors.append(f"{table}: {e}")
        try:
            self._fetch_refs(parsed)
        except Exception as e:
            errors.append(f"lookup names: {e}")
        for table, (header, rows) in parsed.items():
            try:
                more = self._ingest(table, header, rows, cursors.get(table))
                backlog = backlog or more
            except Exception as e:
                errors.append(f"{table}: {e}")
        if errors:
            logger.warning(f"{self.name}: {len(errors)} table errors, first: {errors[0]}")

        info = parse_info(parts)
        info["collector"] = {"method": self.transport.last_method.get(self.name)}
        self.store.set_info(self.name, info)
        if self.live_enabled:
            self._ingest_live(parts.get("live", ""), info.get("gateway_time"))

        self.status.update(
            state="ok", error=errors[0] if errors else None,
            method=self.transport.last_method.get(self.name),
            last_poll=time.time(), catching_up=backlog,
            db_present=info.get("db_present"), db_bytes=info.get("db_bytes"),
            db_age_seconds=info.get("db_age_seconds"),
        )
        live_ok = self.live_ts is not None and info.get("gateway_time") and info["gateway_time"] - self.live_ts < 300
        self.status.update(live_age_seconds=(info["gateway_time"] - self.live_ts) if self.live_ts and info.get("gateway_time") else None,
                           metrics_mapped=self._mapped_count(), db_note=None)
        # Lag of the newest stored DB row behind the gateway clock (file mtime is unreliable on R82)
        cur = self.store.get_cursors(self.name)
        db_latest = max((cur[t] for t in self.collect_tables if t in cur), default=None)
        db_lag = (info["gateway_time"] - db_latest) if db_latest and info.get("gateway_time") else None
        self.status["db_lag_seconds"] = db_lag
        stale = None
        if info.get("db_present") is False:
            stale = f"{self.db_path} not found - enable CPView history ('cpview -s on' on R82)"
        elif db_lag is not None and db_lag > 300 and not backlog:
            stale = (f"newest CPView history row is {_ago(db_lag)} old - "
                     "R82 CPView Services flushes the history DB to disk in batches")
        if stale and live_ok:
            # Live cpview -m covers "now"; the DB only supplies older history
            self.status["db_note"] = stale + "; recent data comes from 'cpview -m'"
        elif stale:
            self.status.update(state="warning", error=stale + "; graphs end at the last flush")
        return backlog

    def _mapped_count(self) -> int:
        return sum(1 for m in self.catalogue if self.mapping.get(m["id"]) or self.live_mapping.get(m["id"]))

    # ---- lookup names (R82 cpview_ref_table) ----

    def _fetch_refs(self, parsed: dict):
        """Fetch names for any lookup keys seen in this batch that aren't cached yet."""
        wanted = {}
        for table, (header, rows) in parsed.items():
            refs = self.tables[table].get("refs", {})
            for col, (rt, _) in refs.items():
                if rt not in self.ref_meta or col not in header:
                    continue
                i = header.index(col)
                cache = self.refs.setdefault(rt, {})
                for row in rows:
                    v = row[i] if i < len(row) else ""
                    if v.lstrip("-").isdigit() and int(v) >= 0 and int(v) not in cache:
                        wanted.setdefault(rt, set()).add(int(v))
        if not wanted:
            return
        sections = []
        for rt, keys in wanted.items():
            key, val = self.ref_meta[rt]
            ids = sorted(keys)
            for n in range(0, len(ids), 500):
                chunk = ",".join(str(k) for k in ids[n:n + 500])
                sql = f'SELECT "{key}", "{val}" FROM "{rt}" WHERE "{key}" IN ({chunk})'
                sections.append((f"r{len(sections)}|{rt}", sqlite_cmd(self.db_path, sql, self.sqlite_bin)))
        out = self.transport.run(self.gw, build_script(sections))
        if out is None:
            return
        for sid, text in split_sections(out).items():
            rt = sid.split("|", 1)[1]
            _, rows = parse_csv(text)
            for row in rows:
                if len(row) >= 2 and row[0].lstrip("-").isdigit():
                    self.refs[rt][int(row[0])] = row[1]

    def _label(self, table: str, header: list, row: list, label_i: list) -> str:
        refs = self.tables[table].get("refs", {})
        parts = []
        for i in label_i:
            if i >= len(row) or row[i] in ("", "-1"):
                continue
            v = row[i]
            ref = refs.get(header[i])
            if ref and v.lstrip("-").isdigit():
                v = self.refs.get(ref[0], {}).get(int(v), v)
            parts.append(v)
        return " / ".join(parts)

    # ---- live cpview -m ----

    def _ingest_live(self, text: str, gateway_time: int | None):
        ts, metrics = parse_metrics(text)
        if not metrics:
            return
        ts = int(ts or gateway_time or time.time())
        ids = [m.get("metric-id") for m in metrics if m.get("metric-id")]
        self.live_mapping = {m["id"]: resolve_live(m.get("live"), ids) for m in self.catalogue}
        collect = {v for v in self.live_mapping.values() if v}
        collect |= {i for i in ids if any(p.search(i) for p in self.live_extra)}
        samples = []
        for m in metrics:
            mid = m.get("metric-id")
            if mid not in collect:
                continue
            for point in m.get("data", []):
                try:
                    samples.append((mid, encode_labels(point), ts, float(point.get("value"))))
                except (TypeError, ValueError):
                    continue
        self.store.add_samples(self.name, LIVE_TABLE, samples)
        self.store.set_cursor(self.name, LIVE_TABLE, ts)
        self.live_ts = ts
        self.live_index = index_metrics(metrics, collect)
        self.status["live_metrics"] = len(ids)

    def _ingest(self, table: str, header: list, rows: list, prev_cursor: int | None) -> bool:
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
            label = self._label(table, header, row, label_i)
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


def _ago(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{int(seconds // 60)} min"


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
