"""
CPView transport - runs shell scripts on a gateway and returns their output.

Primary path is the Gaia API /run-script flow already used by chkp-monitor
(GaiaApiCollector._run_script). SSH (Paramiko exec_command, then the legacy
interactive expert-mode helper) is used when the Gaia API is unreachable.

Every query is wrapped in @@CPV-BEGIN / @@CPV-END markers so that banner,
prompt and echo noise from SSH sessions is ignored by split_sections().
"""
import os
import shlex
import logging
import subprocess

from collectors.gaia_api import GaiaApiCollector
from collectors.ssh_fallback import SshFallbackCollector, PARAMIKO_AVAILABLE

logger = logging.getLogger("chkp-monitor.cpview.transport")

BEGIN = "@@CPV-BEGIN"
END = "@@CPV-END"


def build_script(sections: list[tuple[str, str]]) -> str:
    """Join (section_id, shell_command) pairs into one marker-delimited script."""
    lines = []
    for sid, cmd in sections:
        lines.append(f"echo '{BEGIN} {sid}'")
        lines.append(f"{{ {cmd} ; }} 2>&1")
        lines.append(f"echo '{END} {sid}'")
    return "\n".join(lines) + "\n"


def sqlite_cmd(db_path: str, sql: str) -> str:
    """
    Shell command that runs one read-only query and prints CSV with a header row.
    The -r guard stops sqlite3 from creating an empty DB if the path is wrong.
    """
    db = shlex.quote(db_path)
    return (f"if [ -r {db} ]; then sqlite3 -csv -header {db} {shlex.quote(sql)}; "
            f"else echo 'Error: {db_path} not readable'; fi")


def split_sections(output: str) -> dict[str, str]:
    """Extract marker-delimited sections from script output."""
    sections = {}
    current = None
    buf = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(BEGIN + " "):
            current = stripped[len(BEGIN) + 1:]
            buf = []
            continue
        if current is not None and stripped == f"{END} {current}":
            sections[current] = "\n".join(buf)
            current = None
            continue
        if current is not None:
            buf.append(line.rstrip("\r"))
    return sections


class GatewayTransport:
    """Gaia API first, SSH second. Remembers which one worked last per gateway."""

    def __init__(self, config: dict, credentials: dict):
        self.gaia = GaiaApiCollector(config, credentials)
        self.ssh = SshFallbackCollector(config, credentials)
        self.user = credentials["gaia"]["user"]
        self.password = credentials["gaia"]["password"]
        self.ssh_enabled = config.get("cpview", {}).get("ssh_fallback", True)
        self.last_method = {}

    def run(self, gw: dict, script: str) -> str | None:
        out = self.gaia._run_script(gw, script)
        if out is not None:
            self.last_method[gw["name"]] = "gaia_api"
            return out
        if not self.ssh_enabled:
            return None
        logger.warning(f"Gaia API run-script failed for {gw['name']}, trying SSH")
        out = self._ssh_exec(gw, script)
        if out is None:
            out = self.ssh._ssh_command(gw, script)
        if out is not None:
            self.last_method[gw["name"]] = "ssh"
        return out

    def _ssh_exec(self, gw: dict, script: str) -> str | None:
        """Non-interactive exec - works when the user's shell is /bin/bash."""
        if not PARAMIKO_AVAILABLE:
            return None
        import paramiko
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
            _, stdout, _ = client.exec_command("bash -s", timeout=60)
            stdout.channel.sendall(script.encode())
            stdout.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            # Clish shells reject 'bash -s' - let the caller try expert mode instead
            return out if BEGIN in out else None
        except Exception as e:
            logger.debug(f"SSH exec to {gw['name']} failed: {e}")
            return None
        finally:
            client.close()


class LocalTransport:
    """
    Runs scripts with the local bash. Used by --demo mode and for testing
    against a CPViewDB.dat copied off a gateway (cpview history export).
    """

    def __init__(self, extra_path: str | None = None):
        self.env = dict(os.environ)
        if extra_path:
            self.env["PATH"] = extra_path + os.pathsep + self.env.get("PATH", "")
        self.last_method = {}

    def run(self, gw: dict, script: str) -> str | None:
        try:
            proc = subprocess.run(
                ["bash", "-s"], input=script, capture_output=True,
                text=True, timeout=60, env=self.env,
            )
            self.last_method[gw["name"]] = "local"
            return proc.stdout
        except Exception as e:
            logger.error(f"Local script failed for {gw['name']}: {e}")
            return None
