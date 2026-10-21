"""
CPView history DB schema discovery.

CPViewDB.dat table and column names differ between Gaia versions and Jumbo
takes, so nothing is hard-coded. The gateway's CREATE TABLE statements are
fetched once and replayed into an in-memory SQLite DB, where PRAGMA
table_info gives the exact column list. Logical metrics from
cpview_metrics.json are then resolved against that schema with regexes.
"""
import csv
import io
import re
import sqlite3
import logging

logger = logging.getLogger("chkp-monitor.cpview.schema")

SCHEMA_SQL = "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"

# Declared types that hold labels (CPU name, interface name...) rather than values
TEXT_TYPES = re.compile(r"CHAR|TEXT|CLOB|STRING", re.IGNORECASE)
# Untyped/integer columns that are still identifiers, not measurements
LABEL_NAMES = re.compile(r"(^|_)(name|names|id|index|instance|iface|interface|partition|mount|vs|vsid)$", re.IGNORECASE)
TS_NAMES = [
    re.compile(r"^timestamp$", re.I),
    re.compile(r"^(time|ts|time_stamp|sample_time|epoch)$", re.I),
    re.compile(r"(time_?stamp|^time|time$|^ts_|_ts$)", re.I),
]


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse sqlite3 -csv -header output. Returns (header, rows)."""
    if not text or not text.strip():
        return [], []
    if text.lstrip().startswith(("Error", "Parse error", "Runtime error")):
        raise RuntimeError(text.strip().splitlines()[0])
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if r]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def build_schema(schema_csv: str) -> dict:
    """
    Turn the output of SCHEMA_SQL into:
      {table: {"columns": [...], "ts": col, "labels": [...], "values": [...]}}
    """
    _, rows = parse_csv(schema_csv)
    mem = sqlite3.connect(":memory:")
    tables = {}
    for row in rows:
        if len(row) < 2:
            continue
        name, ddl = row[0], row[1]
        if name.startswith("sqlite_"):
            continue
        try:
            mem.execute(ddl)
        except sqlite3.Error as e:
            logger.debug(f"Could not replay DDL for {name}: {e}")
            continue
        cols = mem.execute(f'PRAGMA table_info("{name}")').fetchall()
        columns = [(c[1], c[2] or "") for c in cols]
        ts = _find_ts(columns)
        if not ts:
            continue
        labels, values = [], []
        for col, ctype in columns:
            if col == ts:
                continue
            if TEXT_TYPES.search(ctype) or LABEL_NAMES.search(col):
                labels.append(col)
            else:
                values.append(col)
        tables[name] = {
            "columns": [c for c, _ in columns],
            "types": {c: t for c, t in columns},
            "ts": ts,
            "labels": labels,
            "values": values,
        }
    mem.close()
    return tables


def _find_ts(columns: list[tuple[str, str]]) -> str | None:
    for pattern in TS_NAMES:
        for col, _ in columns:
            if pattern.search(col):
                return col
    return None


def resolve_metric(metric: dict, tables: dict) -> dict | None:
    """
    Find the first (table, column) pair matching a catalogue entry.
    Table and column candidates are tried in order, so put the most
    specific regex first in cpview_metrics.json.
    """
    for t_pat in metric.get("table", []):
        t_re = re.compile(t_pat, re.IGNORECASE)
        candidates = sorted(t for t in tables if t_re.search(t))
        for c_pat in metric.get("column", []):
            c_re = re.compile(c_pat, re.IGNORECASE)
            for table in candidates:
                info = tables[table]
                for col in info["values"]:
                    if c_re.search(col):
                        return {"table": table, "column": col, "labels": info["labels"]}
    return None


def resolve_all(catalogue: list[dict], tables: dict) -> dict:
    return {m["id"]: resolve_metric(m, tables) for m in catalogue}
