"""
Tier 3 - Gaia API Collector (Primary)
Polls each gateway via Gaia REST API /run-script endpoint.
Collects: CPU, memory, disk, connections, drops, ClusterXL state,
kernel memory, interface stats.
"""
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
        self.sessions = {}  # gateway_name -> {sid, last_login}
        self.session_lifetime = 500

    def _login(self, gw: dict) -> str | None:
        """Authenticate to a gateway's Gaia API."""
        name = gw["name"]
        url = f"https://{gw['mgmt_ip']}:{gw['gaia_port']}/gaia_api/login"
        try:
            resp = requests.post(
                url,
                json={"user": self.user, "password": self.password},
                verify=False,
                timeout=15,
            )
            resp.raise_for_status()
            sid = resp.json().get("sid")
            self.sessions[name] = {"sid": sid, "last_login": time.time()}
            logger.info(f"Gaia API login to {name} successful")
            return sid
        except Exception as e:
            logger.error(f"Gaia API login to {name} failed: {e}")
            self.sessions.pop(name, None)
            return None

    def _ensure_session(self, gw: dict) -> str | None:
        """Return a valid session ID for the gateway."""
        name = gw["name"]
        session = self.sessions.get(name)
        if session and (time.time() - session["last_login"]) < self.session_lifetime:
            return session["sid"]
        return self._login(gw)

    def _run_script(self, gw: dict, script: str) -> str | None:
        """Execute a script on the gateway via Gaia API /run-script."""
        sid = self._ensure_session(gw)
        if not sid:
            return None

        url = f"https://{gw['mgmt_ip']}:{gw['gaia_port']}/gaia_api/run-script"
        try:
            resp = requests.post(
                url,
                json={"script": script},
                headers={"X-chkp-sid": sid},
                verify=False,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            output = data.get("tasks", [{}])
            if output:
                task = output[0] if isinstance(output, list) else output
                return task.get("task-details", [{}])[0].get("statusDescription", "")
            return data.get("output", "")
        except Exception as e:
            logger.error(f"run-script on {gw['name']} failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                if e.response.status_code in (401, 403):
                    self.sessions.pop(gw["name"], None)
            return None

    def _run_script_simple(self, gw: dict, script: str) -> str | None:
        """
        Execute a script, handling both task-based and direct response formats.
        The Gaia API may return results in different structures depending on version.
        """
        sid = self._ensure_session(gw)
        if not sid:
            return None

        url = f"https://{gw['mgmt_ip']}:{gw['gaia_port']}/gaia_api/run-script"
        try:
            resp = requests.post(
                url,
                json={"script": script},
                headers={"X-chkp-sid": sid},
                verify=False,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            # Try direct output first
            if "output" in data:
                return data["output"]

            # Task-based response
            tasks = data.get("tasks", [])
            if tasks:
                task = tasks[0] if isinstance(tasks, list) else tasks
                details = task.get("task-details", [])
                if details:
                    detail = details[0] if isinstance(details, list) else details
                    return detail.get("statusDescription", "")

            # Fallback: return raw JSON string for debugging
            logger.warning(f"Unexpected run-script response from {gw['name']}: {list(data.keys())}")
            return str(data)
        except Exception as e:
            logger.error(f"run-script on {gw['name']} failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                if e.response.status_code in (401, 403):
                    self.sessions.pop(gw["name"], None)
            return None

    # ---- Parsers for command outputs ----

    def _parse_cpstat_os(self, output: str) -> dict:
        """Parse cpstat os -f all output for CPU, memory, disk."""
        result = {"cpu_percent": None, "memory_percent": None, "disk_percent": None,
                  "cpu_idle": None, "memory_total_mb": None, "memory_used_mb": None,
                  "disk_total_gb": None, "disk_used_gb": None}
        if not output:
            return result

        # CPU: look for idle percentage
        idle_match = re.search(r"CPU Idle[:\s]+(\d+\.?\d*)%?", output, re.IGNORECASE)
        if idle_match:
            idle = float(idle_match.group(1))
            result["cpu_idle"] = idle
            result["cpu_percent"] = round(100 - idle, 1)

        # Also try "CPU User" + "CPU System" pattern
        if result["cpu_percent"] is None:
            user_match = re.search(r"CPU User[:\s]+(\d+\.?\d*)%?", output, re.IGNORECASE)
            sys_match = re.search(r"CPU System[:\s]+(\d+\.?\d*)%?", output, re.IGNORECASE)
            if user_match and sys_match:
                result["cpu_percent"] = round(float(user_match.group(1)) + float(sys_match.group(1)), 1)

        # Memory: look for total and used/free
        mem_total = re.search(r"Total (?:Real |Physical )?Memory[:\s]+(\d+)", output, re.IGNORECASE)
        mem_used = re.search(r"(?:Active|Used) (?:Real |Physical )?Memory[:\s]+(\d+)", output, re.IGNORECASE)
        mem_free = re.search(r"Free (?:Real |Physical )?Memory[:\s]+(\d+)", output, re.IGNORECASE)

        if mem_total:
            total = int(mem_total.group(1))
            result["memory_total_mb"] = total
            if mem_used:
                used = int(mem_used.group(1))
                result["memory_used_mb"] = used
                result["memory_percent"] = round((used / total) * 100, 1) if total > 0 else 0
            elif mem_free:
                free = int(mem_free.group(1))
                result["memory_used_mb"] = total - free
                result["memory_percent"] = round(((total - free) / total) * 100, 1) if total > 0 else 0

        # Disk: look for partition usage
        disk_match = re.search(r"/\s+\d+\s+\d+\s+\d+\s+(\d+)%", output)
        if disk_match:
            result["disk_percent"] = int(disk_match.group(1))

        # Alternative disk parsing from cpstat output
        disk_cap = re.search(r"Disk Capacity[:\s]+(\d+)", output, re.IGNORECASE)
        disk_used_match = re.search(r"Disk Used[:\s]+(\d+)%?", output, re.IGNORECASE)
        if disk_used_match:
            result["disk_percent"] = int(disk_used_match.group(1))

        return result

    def _parse_cpstat_fw(self, output: str) -> dict:
        """Parse cpstat fw -f all output for connections, packets, drops."""
        result = {"connections_current": None, "connections_peak": None,
                  "packets_total": None, "drops_total": None,
                  "packets_accepted": None, "packets_dropped": None,
                  "packets_logged": None}
        if not output:
            return result

        # Current connections
        conn_match = re.search(r"(?:Num\. )?Connections[:\s]+(\d+)", output, re.IGNORECASE)
        if conn_match:
            result["connections_current"] = int(conn_match.group(1))

        peak_match = re.search(r"Peak[:\s]+(\d+)", output, re.IGNORECASE)
        if peak_match:
            result["connections_peak"] = int(peak_match.group(1))

        # Packets
        accepted = re.search(r"(?:Packets )?Accepted[:\s]+(\d+)", output, re.IGNORECASE)
        dropped = re.search(r"(?:Packets )?Dropped[:\s]+(\d+)", output, re.IGNORECASE)
        logged = re.search(r"(?:Packets )?Logged[:\s]+(\d+)", output, re.IGNORECASE)

        if accepted:
            result["packets_accepted"] = int(accepted.group(1))
        if dropped:
            result["packets_dropped"] = int(dropped.group(1))
            result["drops_total"] = int(dropped.group(1))
        if logged:
            result["packets_logged"] = int(logged.group(1))

        return result

    def _parse_cphaprob(self, output: str) -> dict:
        """Parse cphaprob stat output for ClusterXL state."""
        result = {"cluster_state": "unknown", "members": []}
        if not output:
            return result

        # Look for local and remote member states
        # Typical format:
        # Member_A(local):  Active
        # Member_B(remote): Standby
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

        # Determine overall cluster state from local member
        for member in result["members"]:
            if member["locality"] == "local":
                result["cluster_state"] = member["state"]
                break

        # Fallback: look for simple "active" or "standby" keywords
        if result["cluster_state"] == "unknown":
            if re.search(r"\bActive\b", output, re.IGNORECASE):
                result["cluster_state"] = "Active"
            elif re.search(r"\bStandby\b", output, re.IGNORECASE):
                result["cluster_state"] = "Standby"

        return result

    def _parse_fw_ctl_pstat(self, output: str) -> dict:
        """Parse fw ctl pstat output for kernel memory stats."""
        result = {"kernel_memory_used_percent": None, "kernel_memory_total": None,
                  "kernel_memory_used": None, "kernel_memory_peak": None,
                  "hash_memory_used_percent": None, "conns_memory_used_percent": None}
        if not output:
            return result

        # Kernel memory block
        # Example: Total memory bytes used: 12345678, Total memory bytes available: 87654321
        total_match = re.search(r"Total (?:memory )?bytes? (?:available|allocated)[:\s]+(\d+)", output, re.IGNORECASE)
        used_match = re.search(r"Total (?:memory )?bytes? used[:\s]+(\d+)", output, re.IGNORECASE)
        peak_match = re.search(r"Peak (?:memory )?bytes? used[:\s]+(\d+)", output, re.IGNORECASE)

        if total_match and used_match:
            total = int(total_match.group(1))
            used = int(used_match.group(1))
            result["kernel_memory_total"] = total
            result["kernel_memory_used"] = used
            result["kernel_memory_used_percent"] = round((used / total) * 100, 1) if total > 0 else 0

        if peak_match:
            result["kernel_memory_peak"] = int(peak_match.group(1))

        # Hash memory
        hash_total = re.search(r"Total (?:hash )?memory[:\s]+(\d+)", output, re.IGNORECASE)
        hash_used = re.search(r"(?:hash )?memory used[:\s]+(\d+)", output, re.IGNORECASE)
        if hash_total and hash_used:
            ht = int(hash_total.group(1))
            hu = int(hash_used.group(1))
            result["hash_memory_used_percent"] = round((hu / ht) * 100, 1) if ht > 0 else 0

        return result

    def _parse_interfaces(self, output: str) -> list:
        """Parse interface stats from clish or /proc/net/dev."""
        interfaces = []
        if not output:
            return interfaces

        # Parse /proc/net/dev format
        lines = output.strip().split("\n")
        for line in lines:
            # Skip header lines
            if "Inter-" in line or "face" in line or "|" in line:
                continue

            parts = line.strip().split()
            if len(parts) >= 10 and ":" in parts[0]:
                iface = parts[0].rstrip(":")
                if iface in ("lo",):
                    continue
                interfaces.append({
                    "name": iface,
                    "rx_bytes": int(parts[1]),
                    "rx_packets": int(parts[2]),
                    "rx_errors": int(parts[3]),
                    "rx_drops": int(parts[4]),
                    "tx_bytes": int(parts[9]) if len(parts) > 9 else 0,
                    "tx_packets": int(parts[10]) if len(parts) > 10 else 0,
                    "tx_errors": int(parts[11]) if len(parts) > 11 else 0,
                    "tx_drops": int(parts[12]) if len(parts) > 12 else 0,
                })

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
        os_output = self._run_script_simple(gw, "cpstat os -f all")
        if os_output is not None:
            result["gaia_api_reachable"] = True
            result["os"] = self._parse_cpstat_os(os_output)

        # cpstat fw -f all
        fw_output = self._run_script_simple(gw, "cpstat fw -f all")
        if fw_output is not None:
            result["firewall"] = self._parse_cpstat_fw(fw_output)

        # cphaprob stat
        ha_output = self._run_script_simple(gw, "cphaprob stat")
        if ha_output is not None:
            result["cluster"] = self._parse_cphaprob(ha_output)

        # fw ctl pstat
        pstat_output = self._run_script_simple(gw, "fw ctl pstat")
        if pstat_output is not None:
            result["kernel_memory"] = self._parse_fw_ctl_pstat(pstat_output)

        # Interface stats via /proc/net/dev
        iface_output = self._run_script_simple(gw, "cat /proc/net/dev")
        if iface_output is not None:
            result["interfaces"] = self._parse_interfaces(iface_output)

        return result

    def collect(self) -> dict:
        """Collect from all gateways."""
        result = {"timestamp": time.time(), "gateways": {}}
        for gw in self.gateways:
            result["gateways"][gw["name"]] = self.collect_gateway(gw)
        return result
