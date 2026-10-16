"""
VMware Workstation metadata: resolve vmware-vmx PIDs to VMs and read their
.vmx configuration and vmware.log.

PID -> VM resolution, in order:
  1. The process command line (last argument is the .vmx path). Windows hides
     this from a non-elevated user for vmware-vmx, so:
  2. vmrun list + each VM folder's vmware.log, whose first line records the pid:
       "... Log for VMware Workstation pid=29148 version=26.0.0 ..."
     Only logs still held open by a running VM are trusted.
"""
import os
import re
import glob
import shutil
import logging
import subprocess

logger = logging.getLogger("vmhost.meta")

PID_RE = re.compile(r"\bpid=(\d+)")
MONITOR_RE = re.compile(r"Monitor Mode:\s*(\S+)")
VBS_RE = re.compile(r"VBS is set to\s*(\S+)")
# Workstation 25+: "-WARNING" / "-ERROR"; older builds: "Wa(03)" / "Er(02)" / "vmx| W115:"
PROBLEM_RE = re.compile(r"\s-(WARNING|ERROR)\s|\s(Wa|Er)\(\d+\)|\|\s[WE]\d+:")
VMX_LINE_RE = re.compile(r'^\s*([\w.:-]+)\s*=\s*"(.*)"\s*$')

# VMware's fixed virtual networks; custom ones use ethernetN.vnet = "VMnetX"
CONNECTION_TYPE_VNET = {"bridged": "VMnet0", "hostonly": "VMnet1", "nat": "VMnet8"}


def find_vmrun(configured: str | None = None) -> str | None:
    candidates = [configured, shutil.which("vmrun")]
    if os.name == "nt":
        try:
            import winreg
            for key in (r"SOFTWARE\WOW6432Node\VMware, Inc.\VMware Workstation",
                        r"SOFTWARE\VMware, Inc.\VMware Workstation"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                        candidates.append(os.path.join(winreg.QueryValueEx(k, "InstallPath")[0], "vmrun.exe"))
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                candidates.append(os.path.join(os.environ[env], "VMware", "VMware Workstation", "vmrun.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def vmrun_list(vmrun: str | None) -> list[str]:
    if not vmrun:
        return []
    try:
        out = subprocess.run([vmrun, "list"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception as e:
        logger.debug(f"vmrun list failed: {e}")
        return []
    return [line.strip() for line in out.splitlines() if line.strip().lower().endswith(".vmx")]


def vmx_from_cmdline(cmdline: list[str]) -> str | None:
    for arg in reversed(cmdline or []):
        if arg.lower().endswith(".vmx"):
            return arg
    return None


def file_in_use(path: str) -> bool:
    """True if another process holds the file open for writing (Windows sharing semantics)."""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if h == wintypes.HANDLE(-1).value:
        return ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION
    k32.CloseHandle(h)
    return False


def read_head(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_tail(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def build_pid_map(vmrun: str | None, vm_roots: list[str]) -> dict[int, str]:
    """pid -> .vmx path for every running VM found via vmrun list or under vm_roots."""
    dirs: dict[str, str | None] = {}
    for vmx in vmrun_list(vmrun):
        dirs[os.path.normcase(os.path.dirname(vmx))] = vmx
    for root in vm_roots:
        for log in glob.glob(os.path.join(root, "**", "vmware.log"), recursive=True):
            dirs.setdefault(os.path.normcase(os.path.dirname(log)), None)

    pid_map = {}
    for d, vmx in dirs.items():
        log = os.path.join(d, "vmware.log")
        if not os.path.isfile(log) or not file_in_use(log):
            continue
        m = PID_RE.search(read_head(log, 4096))
        if not m:
            continue
        if not vmx:
            vmx = next(iter(glob.glob(os.path.join(d, "*.vmx"))), None)
        if vmx:
            pid_map[int(m.group(1))] = vmx
    return pid_map


def parse_vmx(path: str) -> dict:
    """Read the parts of a .vmx that describe what the VM was given."""
    kv = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = VMX_LINE_RE.match(line)
                if m:
                    kv[m.group(1).lower()] = m.group(2)
    except OSError as e:
        return {"error": str(e)}

    nics = []
    for i in range(10):
        p = f"ethernet{i}"
        if kv.get(f"{p}.present", "").upper() != "TRUE":
            continue
        ctype = kv.get(f"{p}.connectiontype", "bridged").lower()
        if ctype == "pvn":
            network = "LAN segment"
        elif ctype == "custom":
            network = kv.get(f"{p}.vnet", "custom")
        else:
            network = CONNECTION_TYPE_VNET.get(ctype, ctype)
        nics.append({"id": p, "network": network, "type": ctype,
                     "connected": kv.get(f"{p}.startconnected", "TRUE").upper() == "TRUE"})

    disks = [v for k, v in kv.items()
             if re.match(r"(scsi|sata|nvme|ide)\d+:\d+\.filename$", k) and v.lower().endswith(".vmdk")]

    def as_int(key, default=None):
        try:
            return int(kv[key])
        except (KeyError, ValueError):
            return default

    return {
        "display_name": kv.get("displayname") or os.path.splitext(os.path.basename(path))[0],
        "guest_os": kv.get("guestos", ""),
        "hw_version": as_int("virtualhw.version"),
        "vcpus": as_int("numvcpus", 1),
        "cores_per_socket": as_int("cpuid.corespersocket", 1),
        "memory_mb": as_int("memsize"),
        "nics": nics,
        "disks": len(disks),
        "vmx": path,
    }


def parse_log(vmx_path: str, head_bytes: int = 1_048_576, tail_bytes: int = 262_144,
              max_problems: int = 15) -> dict:
    """Execution mode (from the start of vmware.log) and recent warnings/errors (from its end)."""
    log = os.path.join(os.path.dirname(vmx_path), "vmware.log")
    if not os.path.isfile(log):
        return {"mode": "no log", "monitor": None, "vbs": None, "problems": []}
    head = read_head(log, head_bytes)
    mon = MONITOR_RE.search(head)
    vbs = VBS_RE.search(head)
    monitor = mon.group(1) if mon else None
    mode = {"ULM": "WHP/Hyper-V", "CPL0": "Native"}.get(monitor or "", "unknown")
    problems = [line.strip()[:300] for line in read_tail(log, tail_bytes).splitlines()
                if PROBLEM_RE.search(line)]
    return {"mode": mode, "monitor": monitor, "vbs": vbs.group(1) if vbs else None,
            "problems": problems[-max_problems:]}
