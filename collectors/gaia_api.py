"""
Gaia API Collector (primary per-gateway source)
Polls each gateway via the Gaia REST API.
Flow: ONE run-script per gateway per poll (commands.build_script) -> show-task ->
decode base64 output -> commands.parse_all.
"""
import base64
import time
import logging
import requests
import urllib3

from collectors import commands

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("chkp-monitor.gaia_api")


class GaiaApiCollector:
    def __init__(self, config: dict, credentials: dict):
        self.gateways = config["gateways"]
        self.script = commands.build_script()
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

        # Small delay before first show-task to let the task register
        time.sleep(1)

        for attempt in range(15):
            task_result = self._show_task(gw, task_id)
            if task_result is None:
                # show-task failed (could be object_not_found), retry after delay
                time.sleep(2)
                continue

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

    def _show_task(self, gw: dict, task_id: str) -> dict | None:
        """Call show-task, returning None on object_not_found (so caller can retry)."""
        sid = self._ensure_session(gw)
        if not sid:
            return None
        url = f"https://{gw['mgmt_ip']}:{gw['gaia_port']}/gaia_api/show-task"
        try:
            resp = requests.post(
                url,
                json={"task-id": task_id},
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
                # object_not_found means task isn't registered yet — retry
                if "object_not_found" in body or "doesn't exist" in body:
                    logger.debug(f"Task {task_id} not found yet on {gw['name']}, will retry")
                    return None
            logger.error(f"show-task on {gw['name']} failed: {e} | Body: {body}")
            return None
        except Exception as e:
            logger.error(f"show-task on {gw['name']} failed: {e}")
            return None

    def collect_gateway(self, gw: dict) -> dict:
        """Run the combined script on one gateway and parse every section."""
        result = {"name": gw["name"], "ip": gw["mgmt_ip"], "gaia_api_reachable": False}
        output = self._run_script(gw, self.script)
        if output is not None:
            result["gaia_api_reachable"] = True
            result.update(commands.parse_all(output))
        return result
