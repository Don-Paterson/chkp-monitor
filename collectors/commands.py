"""
The commands chkp-monitor runs on each gateway, and their parsers.

All commands go into ONE bash script per gateway per poll (one Gaia API run-script task
or one SSH exec), separated by "@@@ <section>" marker lines, instead of one task or
connection per command.

Parsers are matched to R81.20/R82 output. Sections that do not apply (vsx on a
non-VSX gateway, cphaprob on a non-cluster) simply parse to empty results.
"""
import re

SECTIONS = [
    ("os", "cpstat os -f all"),
    ("multi_cpu", "cpstat os -f multi_cpu"),
    ("fw", "cpstat fw -f all"),
    ("ha", "cphaprob stat"),
    ("pstat", "fw ctl pstat"),
    ("multik", "fw ctl multik stat"),
    ("netdev", "cat /proc/net/dev"),
    # VSX only: per-VS details, then per-VS fwk process CPU from a 1-second top sample
    ("vsx", "vsx stat -l"),
    ("fwk", "top -b -n 2 -d 1 | awk '/^top -/{n++} n==2' | grep -E 'PID|fwk'"),
]
MARK = "@@@ "


def build_script() -> str:
    lines = ["#!/bin/bash", "source /etc/profile.d/CP.sh 2>/dev/null"]
    for name, cmd in SECTIONS:
        lines.append(f"echo '{MARK}{name}'")
        lines.append(f"{cmd} 2>&1")
    lines.append(f"echo '{MARK}end'")
    return "\n".join(lines) + "\n"


def split_sections(output: str) -> dict[str, str]:
    out, cur, buf = {}, None, []
    for line in (output or "").splitlines():
        if line.startswith(MARK):
            if cur:
                out[cur] = "\n".join(buf)
            cur, buf = line[len(MARK):].strip(), []
        elif cur:
            buf.append(line)
    if cur and cur != "end":
        out[cur] = "\n".join(buf)
    return out


def _grab_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text or "")
    return int(m.group(1)) if m else None


# ---- cpstat os -------------------------------------------------------------

def parse_cpstat_os(output: str) -> dict:
    r = {
        "cpu_percent": _grab_int(r"CPU Usage \(%\):\s+(\d+)", output),
        "cpu_user": _grab_int(r"CPU User Time \(%\):\s+(\d+)", output),
        "cpu_system": _grab_int(r"CPU System Time \(%\):\s+(\d+)", output),
        "cpu_idle": _grab_int(r"CPU Idle Time \(%\):\s+(\d+)", output),
        "cpus_number": _grab_int(r"CPUs Number:\s+(\d+)", output),
        "memory_percent": None, "memory_total_bytes": None, "memory_active_bytes": None, "memory_free_bytes": None,
        "disk_percent": None, "disk_free_percent": None,
        "disk_total_bytes": _grab_int(r"Disk Total Space \(Bytes\):\s+(\d+)", output),
        "disk_free_bytes": _grab_int(r"Disk Total Free Space \(Bytes\):\s+(\d+)", output),
        "version": None,
    }
    m = re.search(r"SVN Foundation Version String:\s+(\S+)", output or "")
    r["version"] = m.group(1) if m else None

    total = _grab_int(r"Total Real Memory \(Bytes\):\s+(\d+)", output)
    active = _grab_int(r"Active Real Memory \(Bytes\):\s+(\d+)", output)
    free = _grab_int(r"Free Real Memory \(Bytes\):\s+(\d+)", output)
    if total:
        r["memory_total_bytes"] = total
        if active is not None:
            r["memory_active_bytes"] = active
            r["memory_percent"] = round(active / total * 100, 1)
        elif free is not None:
            r["memory_free_bytes"] = free
            r["memory_percent"] = round((total - free) / total * 100, 1)

    disk_free = _grab_int(r"Disk Free Space \(%\):\s+(\d+)", output)
    if disk_free is not None:
        r["disk_free_percent"] = disk_free
        r["disk_percent"] = 100 - disk_free
    return r


def parse_interfaces_from_cpstat(output: str) -> list:
    """Rows of the 'Interface configuration table' in cpstat os -f all."""
    interfaces, in_table = [], False
    for line in (output or "").splitlines():
        if "Interface configuration table" in line:
            in_table = True
            continue
        if in_table and ("Routing table" in line or "Processors load" in line or "Partitions space" in line):
            break
        if not in_table or not line.startswith("|") or "Name" in line or "---" in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 5 and parts[0] != "lo":
            try:
                interfaces.append({"name": parts[0], "address": parts[1], "mask": parts[2],
                                   "mtu": int(parts[3]), "state": int(parts[4]),
                                   "mac": parts[5] if len(parts) > 5 else ""})
            except (ValueError, IndexError):
                continue
    return interfaces


def parse_multi_cpu(output: str) -> list:
    """cpstat os -f multi_cpu rows: |CPU|User|System|Idle|Usage|Run queue|Interrupts/s|"""
    cpus = []
    for m in re.finditer(r"^\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", output or "", re.MULTILINE):
        cpus.append({"cpu": int(m.group(1)), "user": int(m.group(2)), "system": int(m.group(3)),
                     "idle": int(m.group(4)), "usage": int(m.group(5))})
    return cpus


# ---- cpstat fw -------------------------------------------------------------

def parse_cpstat_fw(output: str) -> dict:
    r = {
        "connections_current": _grab_int(r"Num\. connections:\s+(\d+)", output),
        "connections_peak": _grab_int(r"Peak num\. connections:\s+(\d+)", output),
        "connections_limit": _grab_int(r"Connections capacity limit:\s+(\d+)", output),
        "packets_accepted": _grab_int(r"Total accepted packets:\s+(\d+)", output),
        "packets_dropped": None, "packets_rejected": None, "packets_logged": None, "drops_total": None,
        "per_interface": [],
        "kmem_bytes_used": _grab_int(r"kmem - bytes used:\s+(\d+)", output),
        "kmem_bytes_peak": _grab_int(r"kmem - bytes peak:\s+(\d+)", output),
    }
    # Totals row of the interface table: |    |   | 71127|15355|     0|19139|
    totals = re.search(r"\|\s+\|\s+\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", output or "")
    if totals:
        r["packets_accepted"] = r["packets_accepted"] or int(totals.group(1))
        r["drops_total"] = r["packets_dropped"] = int(totals.group(2))
        r["packets_rejected"] = int(totals.group(3))
        r["packets_logged"] = int(totals.group(4))
    for m in re.finditer(r"\|(\w[\w.]*)\s*\|(in|out)\s*\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", output or ""):
        r["per_interface"].append({"name": m.group(1), "direction": m.group(2), "accept": int(m.group(3)),
                                   "drop": int(m.group(4)), "reject": int(m.group(5)), "log": int(m.group(6))})
    return r


# ---- cphaprob / fw ctl pstat / multik -------------------------------------

def parse_cphaprob(output: str) -> dict:
    result = {"cluster_state": "unknown", "members": []}
    if not output or "not configured" in output.lower():
        return result
    for m in re.finditer(r"(\S+?)\s*\((local|remote)\)\s*[:\-]\s*(Active|Standby|Down|Ready|Initializing|Backup)",
                         output, re.IGNORECASE):
        result["members"].append({"name": m.group(1), "locality": m.group(2).lower(), "state": m.group(3)})
    # R81+ table: "1 (local)  10.1.1.2  100%  ACTIVE  A-GW-01"
    if not result["members"]:
        for m in re.finditer(r"^\s*\d+\s+(\(local\))?\s+([\d.]+)\s+[\d.]+%\s+(\w+(?:\(!\))?)\s+(\S+)",
                             output, re.MULTILINE):
            result["members"].append({"name": m.group(4), "ip": m.group(2),
                                      "locality": "local" if m.group(1) else "remote",
                                      "state": m.group(3).title()})
    local = next((x for x in result["members"] if x["locality"] == "local"), None)
    if local:
        result["cluster_state"] = local["state"]
    return result


def parse_fw_ctl_pstat(output: str) -> dict:
    r = {
        "physical_memory_pct": None, "physical_memory_used_mb": None, "physical_memory_total_mb": None,
        "kernel_memory_used_percent": None, "kernel_memory_used_mb": None, "kernel_memory_total_mb": None,
        "virtual_memory_pct": None, "virtual_memory_used_mb": None, "virtual_memory_total_mb": None,
        "kernel_memory_total": None, "kernel_memory_used": None, "kernel_memory_peak": None,
        "connections_concurrent": None, "connections_peak": None, "watermark_status": None,
    }
    if not output:
        return r
    for key, label in (("physical_memory", r"Physical memory"), ("kernel_memory", r"Kernel\s+memory"),
                       ("virtual_memory", r"Virtual\s+memory")):
        m = re.search(label + r" used:\s+(\d+)%\s+\((\d+)\s+MB\s+out of\s+(\d+)\s+MB\)(?:\s+-\s+(\w+\s+\w+))?", output)
        if m:
            pct_key = "kernel_memory_used_percent" if key == "kernel_memory" else f"{key}_pct"
            r[pct_key] = int(m.group(1))
            r[f"{key}_used_mb"] = int(m.group(2))
            r[f"{key}_total_mb"] = int(m.group(3))
            if key == "kernel_memory" and m.group(4):
                r["watermark_status"] = m.group(4).strip()
    m = re.search(r"Total memory\s+bytes\s+used:\s+(\d+)\s+peak:\s+(\d+)", output)
    if m:
        r["kernel_memory_used"], r["kernel_memory_peak"] = int(m.group(1)), int(m.group(2))
        if r["kernel_memory_total_mb"]:
            r["kernel_memory_total"] = r["kernel_memory_total_mb"] * 1024 * 1024
    m = re.search(r"(\d+)\s+concurrent,\s+(\d+)\s+peak concurrent", output)
    if m:
        r["connections_concurrent"], r["connections_peak"] = int(m.group(1)), int(m.group(2))
    return r


def parse_multik(output: str) -> list:
    """fw ctl multik stat:  ID | Active | CPU | Connections | Peak  (one row per CoreXL FW instance)"""
    cores = []
    for m in re.finditer(r"^\s*(\d+)\s*\|\s*(\w+)\s*\|\s*([\d,\s]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)", output or "", re.MULTILINE):
        cores.append({"id": int(m.group(1)), "active": m.group(2).lower() == "yes",
                      "cpu": m.group(3).replace(" ", ""), "connections": int(m.group(4)), "peak": int(m.group(5))})
    return cores


def parse_proc_net_dev(output: str) -> list:
    interfaces = []
    for line in (output or "").splitlines():
        if ":" not in line or "|" in line:
            continue
        name, _, rest = line.partition(":")
        parts = rest.split()
        name = name.strip()
        if name == "lo" or len(parts) < 16:
            continue
        try:
            interfaces.append({"name": name,
                               "rx_bytes": int(parts[0]), "rx_packets": int(parts[1]),
                               "rx_errors": int(parts[2]), "rx_drops": int(parts[3]),
                               "tx_bytes": int(parts[8]), "tx_packets": int(parts[9]),
                               "tx_errors": int(parts[10]), "tx_drops": int(parts[11])})
        except ValueError:
            continue
    return interfaces


# ---- VSX -------------------------------------------------------------------

def parse_vsx_stat(output: str) -> dict:
    """
    vsx stat -l: one block per virtual device, e.g.
        VSID:            1
        Type:            Virtual System
        Name:            VS-Corp
        Security Policy: Corp_Policy
        Installed at:    12Oct2026 10:00:00
        SIC Status:      Trust
        Connections number: 12
        Connections peak:   40
        Connections limit:  14900
    """
    result = {"is_vsx": False, "systems": []}
    if not output or not re.search(r"^\s*VSID:", output, re.MULTILINE):
        return result
    result["is_vsx"] = True
    for block in re.split(r"(?m)^(?=\s*VSID:)", output):
        kv = {}
        for line in block.splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip():
                kv[k.strip().lower()] = v.strip()
        if "vsid" not in kv:
            continue

        def as_int(key):
            try:
                return int(kv.get(key, "").split()[0])
            except (ValueError, IndexError):
                return None

        result["systems"].append({
            "id": as_int("vsid"),
            "name": kv.get("name", ""),
            "type": kv.get("type", ""),
            "policy": kv.get("security policy") or kv.get("access control policy") or "",
            "installed_at": kv.get("installed at", ""),
            "sic": kv.get("sic status", ""),
            "connections": as_int("connections number"),
            "peak": as_int("connections peak"),
            "limit": as_int("connections limit"),
            "cpu_pct": None,
        })
    return result


def parse_fwk_top(output: str) -> dict[int, float]:
    """VS id -> %CPU summed over that VS's fwk processes (fwk<VSID>_<n>), from a top sample."""
    per_vs: dict[int, float] = {}
    header = None
    for line in (output or "").splitlines():
        cols = line.split()
        if "PID" in cols and "%CPU" in cols:
            header = cols
            continue
        if not header or len(cols) < len(header):
            continue
        cmd = cols[-1]
        m = re.match(r"fwk(\d+)", cmd)
        if not m:
            continue
        try:
            cpu = float(cols[header.index("%CPU")])
        except (ValueError, IndexError):
            continue
        vs = int(m.group(1))
        per_vs[vs] = per_vs.get(vs, 0.0) + cpu
    return per_vs


# ---- Everything for one gateway -------------------------------------------

def parse_all(output: str) -> dict:
    s = split_sections(output)
    vsx = parse_vsx_stat(s.get("vsx", ""))
    if vsx["is_vsx"]:
        fwk = parse_fwk_top(s.get("fwk", ""))
        for v in vsx["systems"]:
            if v["id"] in fwk:
                v["cpu_pct"] = round(fwk[v["id"]], 1)
    interfaces = parse_interfaces_from_cpstat(s.get("os", ""))
    traffic = {t["name"]: t for t in parse_proc_net_dev(s.get("netdev", ""))}
    for iface in interfaces:
        iface.update(traffic.pop(iface["name"], {}))
    # interfaces cpstat does not list (e.g. VSX wrp/ links) still have counters
    interfaces += [dict(t, address="", mask="", state=None) for t in traffic.values()]
    return {
        "sections_seen": sorted(s),
        "os": parse_cpstat_os(s.get("os", "")),
        "cpus": parse_multi_cpu(s.get("multi_cpu", "")),
        "firewall": parse_cpstat_fw(s.get("fw", "")),
        "cluster": parse_cphaprob(s.get("ha", "")),
        "kernel_memory": parse_fw_ctl_pstat(s.get("pstat", "")),
        "cores": parse_multik(s.get("multik", "")),
        "interfaces": interfaces,
        "vsx": vsx,
    }


def add_rates(cur: dict, prev: dict | None, dt: float) -> None:
    """Turn cumulative interface byte counters into bytes/sec using the previous poll."""
    if not prev or dt <= 0:
        return
    old = {i["name"]: i for i in prev.get("interfaces", [])}
    for i in cur.get("interfaces", []):
        o = old.get(i["name"])
        if o and "rx_bytes" in i and "rx_bytes" in o:
            i["rx_bps"] = max(0, i["rx_bytes"] - o["rx_bytes"]) / dt
            i["tx_bps"] = max(0, i["tx_bytes"] - o["tx_bytes"]) / dt
