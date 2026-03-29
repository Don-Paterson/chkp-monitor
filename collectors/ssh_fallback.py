"""
Tier 2 - SSH Fallback Collector
Uses Paramiko to SSH to gateways when the Gaia API is unreachable.
Runs the same commands and uses the same parsers as the Gaia API collector.
"""
import time
import logging

logger = logging.getLogger("chkp-monitor.ssh_fallback")

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    logger.warning("Paramiko not installed - SSH fallback disabled")


class SshFallbackCollector:
    def __init__(self, config: dict, credentials: dict):
        self.gateways = config["gateways"]
        self.user = credentials["gaia"]["user"]
        self.password = credentials["gaia"]["password"]
        # Borrow parsers from Gaia API collector
        from collectors.gaia_api import GaiaApiCollector
        self._parser = GaiaApiCollector(config, credentials)

    def _ssh_command(self, gw: dict, command: str) -> str | None:
        """Execute a command via SSH and return the output."""
        if not PARAMIKO_AVAILABLE:
            return None

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=gw["mgmt_ip"],
                port=gw.get("ssh_port", 22),
                username=self.user,
                password=self.password,
                timeout=15,
                look_for_keys=False,
                allow_agent=False,
            )
            # Use expert mode for Check Point commands
            # Open a shell channel to handle the expert mode prompt
            channel = client.invoke_shell()
            time.sleep(1)

            # Enter expert mode
            channel.send("expert\n")
            time.sleep(1)

            # Send password for expert mode (same as login password on most lab setups)
            channel.send(f"{self.password}\n")
            time.sleep(1)

            # Clear any buffer
            if channel.recv_ready():
                channel.recv(65535)

            # Send the actual command
            channel.send(f"{command}\n")
            time.sleep(3)

            # Collect output
            output = ""
            while channel.recv_ready():
                output += channel.recv(65535).decode("utf-8", errors="replace")

            channel.close()
            return output

        except Exception as e:
            logger.error(f"SSH to {gw['name']} ({gw['mgmt_ip']}) failed: {e}")
            return None
        finally:
            client.close()

    def collect_gateway(self, gw: dict) -> dict:
        """Collect all data from a single gateway via SSH."""
        name = gw["name"]
        result = {
            "name": name,
            "ip": gw["mgmt_ip"],
            "ssh_reachable": False,
            "os": {},
            "firewall": {},
            "cluster": {},
            "kernel_memory": {},
            "interfaces": [],
        }

        # cpstat os
        os_output = self._ssh_command(gw, "cpstat os -f all")
        if os_output is not None:
            result["ssh_reachable"] = True
            result["os"] = self._parser._parse_cpstat_os(os_output)

        # cpstat fw
        fw_output = self._ssh_command(gw, "cpstat fw -f all")
        if fw_output is not None:
            result["firewall"] = self._parser._parse_cpstat_fw(fw_output)

        # cphaprob stat
        ha_output = self._ssh_command(gw, "cphaprob stat")
        if ha_output is not None:
            result["cluster"] = self._parser._parse_cphaprob(ha_output)

        # fw ctl pstat
        pstat_output = self._ssh_command(gw, "fw ctl pstat")
        if pstat_output is not None:
            result["kernel_memory"] = self._parser._parse_fw_ctl_pstat(pstat_output)

        # Interface stats
        iface_output = self._ssh_command(gw, "cat /proc/net/dev")
        if iface_output is not None:
            result["interfaces"] = self._parser._parse_interfaces(iface_output)

        return result

    def collect(self) -> dict:
        """Collect from all gateways via SSH."""
        result = {"timestamp": time.time(), "gateways": {}}
        for gw in self.gateways:
            result["gateways"][gw["name"]] = self.collect_gateway(gw)
        return result
