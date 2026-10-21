"""
Live CPView metrics from `cpview -m` (the telemetry feed Skyline exports).

R82 CPView Services only flushes the history DB to disk every few hours,
so the DB gives history and `cpview -m` gives "now". Output looks like:

  {"metrics": [{"metric-id": "system.cpu.utilization", "metric-unit": "1",
                "description": "...", "metric-type": "Gauge",
                "data": [{"cpu": "0", "state": "idle", "type": "OTHER", "value": 100}, ...]},
               ...],
   "timestamp": 1792585117}

Samples are stored with tbl=LIVE_TABLE, col=metric-id and the label as
"key=value,key=value" (sorted keys), so catalogue `live` specs can filter
and regroup them at query time.
"""
import json
import re

LIVE_TABLE = "cpview -m"


def parse_metrics(text: str) -> tuple[int | None, list[dict]]:
    """Returns (timestamp, metrics). Tolerates noise before the JSON object."""
    if not text or "{" not in text:
        return None, []
    try:
        doc = json.loads(text[text.index("{"):])
    except ValueError:
        return None, []
    return doc.get("timestamp"), doc.get("metrics", [])


def encode_labels(point: dict) -> str:
    return ",".join(f"{k}={point[k]}" for k in sorted(point) if k != "value")


def decode_labels(label: str) -> dict:
    out = {}
    for part in label.split(",") if label else []:
        k, _, v = part.partition("=")
        out[k] = v
    return out


def resolve_live(spec: dict | None, metric_ids: list[str]) -> str | None:
    """First discovered metric-id matching the spec's regex list."""
    if not spec:
        return None
    for pat in spec.get("metric", []):
        r = re.compile(pat, re.IGNORECASE)
        for mid in sorted(metric_ids):
            if r.search(mid):
                return mid
    return None


def shape(raw: dict[str, list], spec: dict) -> dict[str, list]:
    """
    Apply a catalogue live spec to stored series {label_string: [[ts, v], ...]}:
      where     - {label_key: regex} filters data points (full match, case-insensitive)
      by        - label keys (regex, full match) that name the output series; others are summed
      transform - "100-" (e.g. idle -> busy) or "used_pct" (used / all states * 100)
    """
    where = {k: re.compile(v, re.IGNORECASE) for k, v in spec.get("where", {}).items()}
    by = [re.compile(k, re.IGNORECASE) for k in spec.get("by", [])]
    transform = spec.get("transform")

    groups: dict[str, dict[int, list]] = {}
    for label, points in raw.items():
        labels = decode_labels(label)
        if any(k not in labels or not r.fullmatch(labels[k]) for k, r in where.items()):
            continue
        key = " / ".join(next((v for k, v in labels.items() if r.fullmatch(k)), "") for r in by)
        state = labels.get("state", "").lower()
        bucket = groups.setdefault(key, {})
        for ts, v in points:
            if v is None:
                continue
            slot = bucket.setdefault(ts, [0.0, 0.0])  # [selected, total]
            slot[1] += v
            if transform != "used_pct" or state == "used":
                slot[0] += v

    out = {}
    for key, bucket in groups.items():
        series = []
        for ts in sorted(bucket):
            sel, total = bucket[ts]
            if transform == "used_pct":
                val = 100.0 * sel / total if total else None
            elif transform == "100-":
                val = 100.0 - sel
            else:
                val = sel
            series.append([ts, val])
        out[key] = series
    return out


def index_metrics(metrics: list[dict], collected: set[str]) -> list[dict]:
    """Compact description of every metric in the last `cpview -m`, for the Schema Explorer."""
    out = []
    for m in metrics:
        data = m.get("data", [])
        keys = sorted({k for d in data for k in d if k != "value"})
        out.append({
            "id": m.get("metric-id"),
            "description": m.get("description"),
            "unit": m.get("metric-unit"),
            "type": m.get("metric-type"),
            "label_keys": keys,
            "points": len(data),
            "collected": m.get("metric-id") in collected,
            "data": data[:200],
        })
    return sorted(out, key=lambda x: x["id"] or "")
