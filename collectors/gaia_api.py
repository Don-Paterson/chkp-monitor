"""
Tier 3 - Gaia API Collector (Primary)
Polls each gateway via Gaia REST API.
Flow: run-script -> get task-id -> show-task -> decode base64 output -> parse.
Parsers matched to actual R82 output formats.
"""
import base64
import re
import time
import logging
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("chkp-monitor.gaia_api")


class GaiaApiCollector:
    def __init__(self, config: dict, credentials: dict):
        self.gateways = config["gateways"]
        self.user = credentials["gaia"]["user"]
        self.password = credentials["gaia"]["password"]
        self.sessions = {}
        self.session_lifetime = 500

    def _login(self, gw: dict) -> str | None:
        """Login to Gaia API. Tries configured port first, then alternate port.
        Port 443 is default; shifts to 4434 when APCL/URLF blades are enabled."""
        name = gw["name"]
        ports_to_try = [gw["gaia_port"]]
        alt_port = 4434 if gw["gaia_port"] == 443 else 443
        ports_to_try.append(alt_port)

        for port in ports_to_try:
            url = f"https://{gw['mgmt_ip']}:{port}/gaia_api/login"
            try:
                resp = requests.post(
                    url,
                    json={"user": self.user, "password": self.password},
                    verify=False,
                    timeout=10,
                )
                resp.raise_for_status()
                sid = resp.json().get("sid")
                self.sessions[name] = {"sid": sid, "last_login": time.time()}
                # Remember the working port for future calls
                if port != gw["gaia_port"]:
                    logger.info(f"Gaia API on {name} detected on port {port} (not {gw['gaia_port']})")
                    gw["gaia_port"] = port
                logger.info(f"Gaia API login to {name} successful (port {port})")
                return sid
            except requests.exceptions.ConnectionError:
                logger.debug(f"Gaia API on {name} port {port} - connection refused, trying next")
                continue
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.debug(f"Gaia API on {name} port {port} - 404, trying next")
                    continue
                logger.error(f"Gaia API login to {name} failed on port {port}: {e}")
                return None
            except Exception as e:
                logger.error(f"Gaia API login to {name} failed on port {port}: {e}")
                return None

        logger.error(f"Gaia API login to {name} failed on all ports ({ports_to_try})")
        self.sessions.pop(name, None)
        return None

    def _ensure_session(self, gw: dict) -> str | None:
        name = gw["name"]
        session = self.sessions.get(name)
        if session and (time.time() - session["last_login"]) < self.session_lifetime:
            return session["sid"]
        return self._login(gw)

    def _api_post(self, gw: dict, endpoint: str, payload: dict) -> dict | None:
        sid = self._ensure_session(gw)
        if not sid:
            return None
        url = f"https://{gw['mgmt_ip']}:{gw['gaia_port']}/gaia_api/{endpoint}"
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"X-chkp-sid": sid},
                verify=False,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            body = ""
            if e.response is not None:
                try:
                    body = e.response.text[:300]
                except Exception:
                    pass
                if e.response.status_code in (401, 403):
                    self.sessions.pop(gw["name"], None)
            logger.error(f"Gaia API {endpoint} on {gw['name']} failed: {e} | Body: {body}")
            return None
        except Exception as e:
            logger.error(f"Gaia API {endpoint} on {gw['name']} failed: {e}")
            return None

    def _run_script(self, gw: dict, script: str) -> str | None:
        """Execute a script via Gaia API async flow: run-script -> show-task -> decode."""
        result = self._api_post(gw, "run-script", {"script": script})
        if not result:
            return None

        task_id = result.get("task-id")
        if not task_id:
            logger.error(f"No task-id from run-script on {gw['name']}")
            return None

        for attempt in range(15):
            task_result = self._api_post(gw, "show-task", {"task-id": task_id})
            if not task_result:
                return None

            tasks = task_result.get("tasks", [])
            if not tasks:
                time.sleep(2)
                continue

            task = tasks[0]
            progress = task.get("progress-description", "")
            status = task.get("status", "")

            if progress == "succeeded" or status == "succeeded":
                details = task.get("task-details", [])
                if details:
                    detail = details[0] if isinstance(details, list) else details
                    b64_output = detail.get("output", "")
                    if b64_output:
                        try:
                            return base64.b64decode(b64_output).decode("utf-8", errors="replace")
                        except Exception as e:
                            logger.error(f"Base64 decode failed on {gw['name']}: {e}")
                return None
            elif progress in ("failed", "partially succeeded") or status == "failed":
                logger.error(f"Script failed on {gw['name']}: {progress}")
                return None
            time.sleep(2)

        logger.error(f"Script timed out on {gw['name']} (task-id: {task_id})")
        return None

    # ---- Parsers matched to actual R82 output ----

    def _parse_cpstat_os(self, output: str) -> dict:
        """
        Parse cpstat os -f all. Actual R82 fields:
          CPU Usage (%):  2
          CPU User Time (%):  1
          CPU Idle Time (%):  99
          Total Real Memory (Bytes):  8053116928
          Active Real Memory (Bytes):  3513761792
          Free Real Memory (Bytes):  4539355136
          Disk Free Space (%):  90
          Disk Total Space (Bytes):  107321753600
        """
        r = {
            "cpu_percent": None, "cpu_user": None, "cpu_system": None, "cpu_idle": None,
            "memory_percent": None, "memory_total_bytes": None, "memory_active_bytes": None,
            "memory_free_bytes": None,
            "disk_percent": None, "disk_free_percent": None, "disk_total_bytes": None,
            "disk_free_bytes": None,
            "cpus_number": None, "version": None,
        }
        if not output:
            return r

        def grab(pattern):
            m = re.search(pattern, output)
            return m.group(1) if m else None

        r["version"] = grab(r"SVN Foundation Version String:\s+(\S+)")
        r["cpu_percent"] = int(v) if (v := grab(r"CPU Usage \(%\):\s+(\d+)")) else None
        r["cpu_user"] = int(v) if (v := grab(r"CPU User Time \(%\):\s+(\d+)")) else None
        r["cpu_system"] = int(v) if (v := grab(r"CPU System Time \(%\):\s+(\d+)")) else None
        r["cpu_idle"] = int(v) if (v := grab(r"CPU Idle Time \(%\):\s+(\d+)")) else None
        r["cpus_number"] = int(v) if (v := grab(r"CPUs Number:\s+(\d+)")) else None

        total_str = grab(r"Total Real Memory \(Bytes\):\s+(\d+)")
        active_str = grab(r"Active Real Memory \(Bytes\):\s+(\d+)")
        free_str = grab(r"Free Real Memory \(Bytes\):\s+(\d+)")

        if total_str:
            total = int(total_str)
            r["memory_total_bytes"] = total
            if active_str:
                active = int(active_str)
                r["memory_active_bytes"] = active
                r["memory_percent"] = round((active / total) * 100, 1) if total > 0 else 0
            elif free_str:
                free = int(free_str)
                r["memory_free_bytes"] = free
                r["memory_percent"] = round(((total - free) / total) * 100, 1) if total > 0 else 0

        disk_free_pct = grab(r"Disk Free Space \(%\):\s+(\d+)")
        if disk_free_pct:
            r["disk_free_percent"] = int(disk_free_pct)
            r["disk_percent"] = 100 - int(disk_free_pct)

        r["disk_total_bytes"] = int(v) if (v := grab(r"Disk Total Space \(Bytes\):\s+(\d+)")) else None
        r["disk_free_bytes"] = int(v) if (v := grab(r"Disk Total Free Space \(Bytes\):\s+(\d+)")) else None

        return r

    def _parse_cpstat_fw(self, output: str) -> dict:
        """
        Parse cpstat fw -f all. Actual R82 fields:
          Num. connections:  26
          Peak num. connections:  581
          Connections capacity limit:  0
          Total accepted packets:  2435257
          Interface table with per-interface Accept/Drop/Reject/Log
          Totals row:  |    |   | 71127|15355|     0|19139|
          kmem - bytes used:  503247099
          kmem - bytes peak:  533816308
        """
        r = {
            "connections_current": None, "connections_peak": None,
            "connections_limit": None,
            "packets_accepted": None, "packets_dropped": None,
            "packets_rejected": None, "packets_logged": None,
            "drops_total": None,
            "per_interface": [],
            "kmem_bytes_used": None, "kmem_bytes_peak": None,
        }
        if not output:
            return r

        def grab(pattern):
            m = re.search(pattern, output)
            return m.group(1) if m else None

        r["connections_current"] = int(v) if (v := grab(r"Num\. connections:\s+(\d+)")) else None
        r["connections_peak"] = int(v) if (v := grab(r"Peak num\. connections:\s+(\d+)")) else None
        r["connections_limit"] = int(v) if (v := grab(r"Connections capacity limit:\s+(\d+)")) else None
        r["packets_accepted"] = int(v) if (v := grab(r"Total accepted packets:\s+(\d+)")) else None

        # Totals row from interface table: |    |   | 71127|15355|     0|19139|
        totals = re.search(r"\|\s+\|\s+\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", output)
        if totals:
            r["packets_accepted"] = r["packets_accepted"] or int(totals.group(1))
            r["drops_total"] = int(totals.group(2))
            r["packets_dropped"] = int(totals.group(2))
            r["packets_rejected"] = int(totals.group(3))
            r["packets_logged"] = int(totals.group(4))

        # Per-interface stats from the first interface table
        iface_pattern = re.finditer(
            r"\|(\w+)\s*\|(in|out)\s*\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|",
            output
        )
        for m in iface_pattern:
            r["per_interface"].append({
                "name": m.group(1),
                "direction": m.group(2),
                "accept": int(m.group(3)),
                "drop": int(m.group(4)),
                "reject": int(m.group(5)),
                "log": int(m.group(6)),
            })

        # Kernel memory from cpstat fw output
        r["kmem_bytes_used"] = int(v) if (v := grab(r"kmem - bytes used:\s+(\d+)")) else None
        r["kmem_bytes_peak"] = int(v) if (v := grab(r"kmem - bytes peak:\s+(\d+)")) else None

        return r

    def _parse_cphaprob(self, output: str) -> dict:
        """Parse cphaprob stat output for ClusterXL state."""
        result = {"cluster_state": "unknown", "members": []}
        if not output:
            return result

        member_pattern = re.finditer(
            r"(\S+?)\s*\((local|remote)\)\s*[:\-]\s*(Active|Standby|Down|Ready|Initializing)",
            output, re.IGNORECASE
        )
        for m in member_pattern:
            result["members"].append({
                "name": m.group(1),
                "locality": m.group(2).lower(),
                "state": m.group(3),
            })

        for member in result["members"]:
            if member["locality"] == "local":
                result["cluster_state"] = member["state"]
                break

        if result["cluster_state"] == "unknown":
            if re.search(r"\bActive\b", output):
                result["cluster_state"] = "Active"
            elif re.search(r"\bStandby\b", output):
                result["cluster_state"] = "Standby"

        return result

    def _parse_fw_ctl_pstat(self, output: str) -> dict:
        """
        Parse fw ctl pstat. Actual R82 format:
          Physical memory used:  26% (1718 MB out of 6528 MB) - below watermark
          Kernel   memory used:   6% (402 MB out of 6528 MB) - below watermark
          Virtual  memory used:  20% (1315 MB out of 6528 MB) - below watermark
          Total memory  bytes  used: 503250451   peak: 533816308
          Concurrent Connections: 27 (Unlimited)
          27 concurrent, 581 peak concurrent
        """
        r = {
            "physical_memory_pct": None, "physical_memory_used_mb": None, "physical_memory_total_mb": None,
            "kernel_memory_used_percent": None, "kernel_memory_used_mb": None, "kernel_memory_total_mb": None,
            "virtual_memory_pct": None, "virtual_memory_used_mb": None, "virtual_memory_total_mb": None,
            "kernel_memory_total": None, "kernel_memory_used": None, "kernel_memory_peak": None,
            "connections_concurrent": None, "connections_peak": None,
            "watermark_status": None,
        }
        if not output:
            return r

        # Physical memory used:  26% (1718 MB out of 6528 MB) - below watermark
        phys = re.search(r"Physical memory used:\s+(\d+)%\s+\((\d+)\s+MB\s+out of\s+(\d+)\s+MB\)\s+-\s+(\w+\s+\w+)", output)
        if phys:
            r["physical_memory_pct"] = int(phys.group(1))
            r["physical_memory_used_mb"] = int(phys.group(2))
            r["physical_memory_total_mb"] = int(phys.group(3))

        # Kernel memory used:   6% (402 MB out of 6528 MB) - below watermark
        kern = re.search(r"Kernel\s+memory used:\s+(\d+)%\s+\((\d+)\s+MB\s+out of\s+(\d+)\s+MB\)\s+-\s+(\w+\s+\w+)", output)
        if kern:
            r["kernel_memory_used_percent"] = int(kern.group(1))
            r["kernel_memory_used_mb"] = int(kern.group(2))
            r["kernel_memory_total_mb"] = int(kern.group(3))
            r["watermark_status"] = kern.group(4).strip()

        # Virtual memory used:  20% (1315 MB out of 6528 MB)
        virt = re.search(r"Virtual\s+memory used:\s+(\d+)%\s+\((\d+)\s+MB\s+out of\s+(\d+)\s+MB\)", output)
        if virt:
            r["virtual_memory_pct"] = int(virt.group(1))
            r["virtual_memory_used_mb"] = int(virt.group(2))
            r["virtual_memory_total_mb"] = int(virt.group(3))

        # Total memory bytes used: 503250451   peak: 533816308
        raw_mem = re.search(r"Total memory\s+bytes\s+used:\s+(\d+)\s+peak:\s+(\d+)", output)
        if raw_mem:
            r["kernel_memory_used"] = int(raw_mem.group(1))
            r["kernel_memory_peak"] = int(raw_mem.group(2))
            # Use kernel summary total if available
            if r["kernel_memory_total_mb"]:
                r["kernel_memory_total"] = r["kernel_memory_total_mb"] * 1024 * 1024

        # Connections: 27 concurrent, 581 peak concurrent
        conns = re.search(r"(\d+)\s+concurrent,\s+(\d+)\s+peak concurrent", output)
        if conns:
            r["connections_concurrent"] = int(conns.group(1))
            r["connections_peak"] = int(conns.group(2))

        return r

    def _parse_interfaces_from_cpstat(self, output: str) -> list:
        """
        Parse interface config table from cpstat os -f all.
        ONLY parse rows from the Interface configuration table, not Partitions or Processors.
        """
        interfaces = []
        if not output:
            return interfaces

        # Extract only the interface configuration table section
        in_iface_table = False
        for line in output.split("\n"):
            if "Interface configuration table" in line:
                in_iface_table = True
                continue
            if in_iface_table and ("Routing table" in line or "Processors load" in line or "Partitions space" in line):
                break
            if not in_iface_table:
                continue
            if not line.startswith("|") or "Name" in line or "---" in line:
                continue

            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 5:
                name = parts[0]
                if name in ("lo",):
                    continue
                try:
                    interfaces.append({
                        "name": name,
                        "address": parts[1],
                        "mask": parts[2],
                        "mtu": int(parts[3]),
                        "state": int(parts[4]),
                        "mac": parts[5] if len(parts) > 5 else "",
                    })
                except (ValueError, IndexError):
                    continue

        return interfaces

    def _parse_proc_net_dev(self, output: str) -> list:
        """Parse /proc/net/dev for traffic counters."""
        interfaces = []
        if not output:
            return interfaces

        for line in output.strip().split("\n"):
            if "Inter-" in line or "face" in line or "|" in line:
                continue
            parts = line.strip().split()
            if len(parts) >= 10 and ":" in parts[0]:
                iface = parts[0].rstrip(":")
                if iface in ("lo",):
                    continue
                try:
                    interfaces.append({
                        "name": iface,
                        "rx_bytes": int(parts[1]),
                        "rx_packets": int(parts[2]),
                        "rx_errors": int(parts[3]),
                        "rx_drops": int(parts[4]),
                        "tx_bytes": int(parts[9]),
                        "tx_packets": int(parts[10]),
                        "tx_errors": int(parts[11]),
                        "tx_drops": int(parts[12]),
                    })
                except (ValueError, IndexError):
                    continue

        return interfaces

    def collect_gateway(self, gw: dict) -> dict:
        """Collect all Tier 3 data from a single gateway."""
        name = gw["name"]
        result = {
            "name": name,
            "ip": gw["mgmt_ip"],
            "gaia_api_reachable": False,
            "os": {},
            "firewall": {},
            "cluster": {},
            "kernel_memory": {},
            "interfaces": [],
        }

        # cpstat os -f all
        os_output = self._run_script(gw, "cpstat os -f all")
        if os_output is not None:
            result["gaia_api_reachable"] = True
            result["os"] = self._parse_cpstat_os(os_output)
            result["interfaces"] = self._parse_interfaces_from_cpstat(os_output)

        # cpstat fw -f all
        fw_output = self._run_script(gw, "cpstat fw -f all")
        if fw_output is not None:
            result["firewall"] = self._parse_cpstat_fw(fw_output)

        # cphaprob stat
        ha_output = self._run_script(gw, "cphaprob stat")
        if ha_output is not None:
            result["cluster"] = self._parse_cphaprob(ha_output)

        # fw ctl pstat
        pstat_output = self._run_script(gw, "fw ctl pstat")
        if pstat_output is not None:
            result["kernel_memory"] = self._parse_fw_ctl_pstat(pstat_output)

        # /proc/net/dev for traffic counters
        dev_output = self._run_script(gw, "cat /proc/net/dev")
        if dev_output is not None:
            traffic = self._parse_proc_net_dev(dev_output)
            traffic_map = {t["name"]: t for t in traffic}
            for iface in result["interfaces"]:
                if iface["name"] in traffic_map:
                    iface.update(traffic_map[iface["name"]])

        return result

    def collect(self) -> dict:
        """Collect from all gateways."""
        result = {"timestamp": time.time(), "gateways": {}}
        for gw in self.gateways:
            result["gateways"][gw["name"]] = self.collect_gateway(gw)
        return result
