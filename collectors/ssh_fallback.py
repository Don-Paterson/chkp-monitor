"""
SSH fallback collector (Paramiko), used when a gateway's Gaia API is unreachable.
Runs the same combined script as the Gaia API collector, over ONE connection per poll.

Works best when the monitoring user's shell is /bin/bash (then a plain exec is used).
If the user lands in clish, it enters expert mode on an interactive shell and reads
until the script's end marker instead of sleeping and hoping.
"""
import time
import shlex
import logging

from collectors import commands

logger = logging.getLogger("chkp-monitor.ssh_fallback")

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    logger.warning("Paramiko not installed - SSH fallback disabled")

END = commands.MARK + "end"


class SshFallbackCollector:
    def __init__(self, config: dict, credentials: dict):
        self.user = credentials["gaia"]["user"]
        self.password = credentials["gaia"]["password"]
        self.expert_password = credentials["gaia"].get("expert_password", self.password)
        self.script = commands.build_script()
        self.timeout = config.get("ssh", {}).get("command_timeout_seconds", 60)

    def _connect(self, gw: dict):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=gw["mgmt_ip"], port=gw.get("ssh_port", 22), username=self.user,
                       password=self.password, timeout=15, look_for_keys=False, allow_agent=False)
        return client

    def _exec_bash(self, client) -> str | None:
        """Plain exec; returns None if the login shell is clish rather than bash."""
        try:
            # keep stdin referenced: paramiko sends EOF when the stdin file object is collected
            stdin, stdout, _stderr = client.exec_command("bash -s", timeout=self.timeout)
            stdin.write(self.script)
            stdin.flush()
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
        except Exception as e:  # clish may refuse or close the exec channel
            logger.debug(f"bash exec not available: {e}")
            return None
        return out if END in out else None

    def _exec_expert(self, client) -> str | None:
        chan = client.invoke_shell(width=250, height=1000)
        buf = ""

        def read_until(token: str, seconds: float) -> bool:
            nonlocal buf
            deadline = time.time() + seconds
            while time.time() < deadline:
                if chan.recv_ready():
                    buf += chan.recv(65535).decode("utf-8", errors="replace")
                    if token in buf:
                        return True
                else:
                    time.sleep(0.1)
            return token in buf

        read_until(">", 10)
        chan.send("expert\n")
        if read_until("assword", 5):
            chan.send(self.expert_password + "\n")
        read_until("#", 10)
        buf = ""
        # Send the script as one line so the shell does not echo marker lines back as input
        chan.send("bash -c " + shlex.quote(self.script) + "\n")
        ok = read_until("\n" + END, self.timeout)
        chan.close()
        return buf if ok else None

    def collect_gateway(self, gw: dict) -> dict:
        result = {"name": gw["name"], "ip": gw["mgmt_ip"], "ssh_reachable": False}
        if not PARAMIKO_AVAILABLE:
            result["error"] = "paramiko not installed"
            return result
        client = None
        try:
            client = self._connect(gw)
            output = self._exec_bash(client) or self._exec_expert(client)
            if output:
                result["ssh_reachable"] = True
                result.update(commands.parse_all(output))
            else:
                result["error"] = "no output (check the user's shell / expert password)"
        except Exception as e:
            logger.error(f"SSH to {gw['name']} ({gw['mgmt_ip']}) failed: {e}")
            result["error"] = str(e)
        finally:
            if client:
                client.close()
        return result
