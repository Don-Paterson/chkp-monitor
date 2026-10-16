"""
Tier 1 - Check Point Management API Collector
Polls A-SMS for gateway/cluster status, SIC state, and policy info.
"""
import json
import time
import logging
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("chkp-monitor.mgmt_api")


class MgmtApiCollector:
    def __init__(self, config: dict, credentials: dict):
        self.host = config["management"]["host"]
        self.port = config["management"]["port"]
        self.domain = config["management"].get("domain", "")
        self.name = config["management"].get("name", "Management")
        self.user = credentials["management"]["user"]
        self.password = credentials["management"]["password"]
        self.base_url = f"https://{self.host}:{self.port}/web_api"
        self.sid = None
        self.last_login = 0
        self.session_lifetime = 500  # re-login before 600s default timeout

    def _login(self) -> bool:
        """Authenticate to the management API and obtain a session ID."""
        try:
            payload = {
                "user": self.user,
                "password": self.password,
            }
            if self.domain:
                payload["domain"] = self.domain

            resp = requests.post(
                f"{self.base_url}/login",
                json=payload,
                verify=False,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self.sid = data.get("sid")
            self.last_login = time.time()
            logger.info("Management API login successful")
            return True
        except Exception as e:
            logger.error(f"Management API login failed: {e}")
            self.sid = None
            return False

    def _ensure_session(self) -> bool:
        """Ensure we have a valid session, re-login if needed."""
        if self.sid and (time.time() - self.last_login) < self.session_lifetime:
            return True
        return self._login()

    def _api_call(self, command: str, payload: dict = None) -> dict | None:
        """Make an authenticated API call."""
        if not self._ensure_session():
            return None

        try:
            resp = requests.post(
                f"{self.base_url}/{command}",
                json=payload or {},
                headers={"X-chkp-sid": self.sid},
                verify=False,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API call '{command}' failed: {e}")
            # Invalidate session on auth errors
            if hasattr(e, "response") and e.response is not None:
                if e.response.status_code in (401, 403):
                    self.sid = None
            return None

    def _logout(self):
        """Discard the session (best-effort)."""
        if self.sid:
            try:
                self._api_call("logout", {})
            except Exception:
                pass
            self.sid = None

    def collect(self) -> dict:
        """
        Collect all Tier 1 data from the management server.
        Returns a dict with gateway statuses and cluster info.
        """
        result = {
            "timestamp": time.time(),
            "management": {
                "host": self.host,
                "name": self.name,
                "reachable": False,
                "status": "unknown",
            },
            "gateways": {},
            "cluster": {},
        }

        # -- Gateway and server status --
        gw_data = self._api_call("show-gateways-and-servers", {"details-level": "full"})
        if gw_data:
            result["management"]["reachable"] = True
            result["management"]["status"] = "ok"

            for obj in gw_data.get("objects", []):
                obj_type = obj.get("type", "")
                name = obj.get("name", "unknown")
                # Gateways, clusters, VSX gateways/clusters and their Virtual Systems.
                # The management server itself is "checkpoint-host"; members are listed under clusters.
                if obj_type in ("checkpoint-host", "cluster-member") or "Member" in obj_type:
                    continue

                policy = obj.get("policy") if isinstance(obj.get("policy"), dict) else {}
                install = policy.get("access-policy-installation-date") or policy.get("access-policy-install-time") or {}
                gw_info = {
                    "name": name,
                    "type": obj_type,
                    "is_vsx": "vsx" in obj_type.lower(),
                    "is_vs": obj_type.lower().startswith(("cpmivs", "virtual-system")) and "vsx" not in obj_type.lower(),
                    "policy": {
                        "name": policy.get("access-policy-name", "N/A"),
                        "installed": policy.get("access-policy-installed", False),
                        "install_time": (install.get("posix", 0) / 1000 if install.get("posix", 0) > 1e11
                                         else install.get("posix", 0)) if isinstance(install, dict) else 0,
                        "threat_policy": policy.get("threat-policy-name", ""),
                    },
                    "sic": {"status": "unknown"},
                    "version": obj.get("version", "unknown"),
                    "ipv4": obj.get("ipv4-address", ""),
                }

                sic_state = obj.get("sic-state", "")
                if not sic_state and isinstance(obj.get("connection"), dict):
                    sic_state = obj["connection"].get("sic-state", "")
                gw_info["sic"]["status"] = sic_state or obj.get("sic-status", "") or "unknown"

                result["gateways"][name] = gw_info

        # -- Cluster-specific info --
        # Try to get cluster members via show-simple-cluster for each cluster object
        for gw_name, gw_info in result["gateways"].items():
            if gw_info["type"] in ("simple-cluster", "CpmiGatewayCluster"):
                cluster_data = self._api_call("show-simple-cluster", {"name": gw_name, "details-level": "full"})
                if cluster_data:
                    members = []
                    for member in cluster_data.get("cluster-members", []):
                        members.append({
                            "name": member.get("name", "unknown"),
                            "ip": member.get("ip-address", ""),
                            "sic_status": member.get("sic-state", "unknown"),
                        })
                    result["cluster"][gw_name] = {
                        "members": members,
                        "ha_mode": cluster_data.get("cluster-mode", "unknown"),
                    }

        return result
