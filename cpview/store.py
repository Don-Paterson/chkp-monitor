"""
Local time-series store for CPView samples (SQLite).

Plays the role Prometheus plays in Skyline: holds every numeric CPView value
as (host, table, column, label, ts) -> value, plus per-host info snapshots
and the discovered schema. One connection per call keeps it thread-safe for
the collector threads and Flask workers.
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    host  TEXT NOT NULL,
    tbl   TEXT NOT NULL,
    col   TEXT NOT NULL,
    label TEXT NOT NULL,
    ts    INTEGER NOT NULL,
    value REAL,
    PRIMARY KEY (host, tbl, col, label, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS cursors (
    host TEXT NOT NULL, tbl TEXT NOT NULL, last_ts INTEGER NOT NULL,
    PRIMARY KEY (host, tbl)
);
CREATE TABLE IF NOT EXISTS host_info (
    host TEXT PRIMARY KEY, info TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS host_schema (
    host TEXT PRIMARY KEY, schema TEXT, updated REAL
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.write_lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---- writes ----

    def add_samples(self, host: str, tbl: str, rows: list[tuple[str, str, int, float]]):
        """rows: (col, label, ts, value)"""
        if not rows:
            return
        with self.write_lock, self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO samples (host, tbl, col, label, ts, value) VALUES (?, ?, ?, ?, ?, ?)",
                [(host, tbl, col, label, ts, val) for col, label, ts, val in rows],
            )

    def set_cursor(self, host: str, tbl: str, last_ts: int):
        with self.write_lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO cursors (host, tbl, last_ts) VALUES (?, ?, ?)", (host, tbl, last_ts))

    def get_cursors(self, host: str) -> dict[str, int]:
        with self._conn() as c:
            return dict(c.execute("SELECT tbl, last_ts FROM cursors WHERE host = ?", (host,)).fetchall())

    def set_info(self, host: str, info: dict):
        with self.write_lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO host_info (host, info, updated) VALUES (?, ?, ?)",
                      (host, json.dumps(info), time.time()))

    def set_schema(self, host: str, schema: dict):
        with self.write_lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO host_schema (host, schema, updated) VALUES (?, ?, ?)",
                      (host, json.dumps(schema), time.time()))

    def prune(self, retention_days: float):
        cutoff = int(time.time() - retention_days * 86400)
        with self.write_lock, self._conn() as c:
            c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))

    # ---- reads ----

    def get_info(self, host: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT info FROM host_info WHERE host = ?", (host,)).fetchone()
        return json.loads(row[0]) if row else {}

    def get_schema(self, host: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT schema FROM host_schema WHERE host = ?", (host,)).fetchone()
        return json.loads(row[0]) if row else {}

    def latest_ts(self, host: str) -> int | None:
        with self._conn() as c:
            row = c.execute("SELECT MAX(last_ts) FROM cursors WHERE host = ?", (host,)).fetchone()
        return row[0] if row and row[0] else None

    def series(self, host: str, tbl: str, col: str, start: int, end: int, step: int) -> dict[str, list]:
        """Bucketed averages per label: {label: [[ts, value], ...]}"""
        step = max(1, int(step))
        with self._conn() as c:
            rows = c.execute(
                """SELECT label, (ts / ?) * ? AS bucket, AVG(value)
                   FROM samples
                   WHERE host = ? AND tbl = ? AND col = ? AND ts BETWEEN ? AND ?
                   GROUP BY label, bucket ORDER BY label, bucket""",
                (step, step, host, tbl, col, start, end),
            ).fetchall()
        out = {}
        for label, bucket, value in rows:
            out.setdefault(label, []).append([bucket, value])
        return out

    def last_values(self, host: str, tbl: str, col: str) -> dict[str, tuple[int, float]]:
        """Most recent value per label."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT s.label, s.ts, s.value FROM samples s
                   JOIN (SELECT label, MAX(ts) AS mts FROM samples
                         WHERE host = ? AND tbl = ? AND col = ? GROUP BY label) m
                     ON s.label = m.label AND s.ts = m.mts
                   WHERE s.host = ? AND s.tbl = ? AND s.col = ?""",
                (host, tbl, col, host, tbl, col),
            ).fetchall()
        return {label: (ts, value) for label, ts, value in rows}

    def stats(self) -> dict:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"samples": n, "db_bytes": size}
