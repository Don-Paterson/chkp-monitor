"""
Windows thread names (SetThreadDescription / GetThreadDescription, Windows 10 1607+).

VMware names its vmware-vmx threads (vmware.log shows threadName="vmx", "vcpu-0", ...),
which lets us show per-vCPU load. Opening another user's or an elevated process's
threads needs PowerShell/Python to run as Administrator; otherwise names are None.
"""
import os

_names: dict[int, str | None] = {}

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.LocalFree.argtypes = [wintypes.HLOCAL]
    _get_desc = getattr(_k32, "GetThreadDescription", None)
    if _get_desc:
        _get_desc.restype = ctypes.c_long  # HRESULT
        _get_desc.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.LPWSTR)]
    THREAD_QUERY_LIMITED_INFORMATION = 0x0800

    def _query(tid: int) -> str | None:
        if not _get_desc:
            return None
        h = _k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
        if not h:
            return None
        try:
            buf = wintypes.LPWSTR()
            if _get_desc(h, ctypes.byref(buf)) < 0:
                return None
            try:
                return buf.value or None
            finally:
                _k32.LocalFree(ctypes.cast(buf, wintypes.HLOCAL))
        finally:
            _k32.CloseHandle(h)
else:
    def _query(tid: int) -> str | None:
        try:  # Linux equivalent, handy for testing
            with open(f"/proc/{tid}/comm") as f:
                return f.read().strip() or None
        except OSError:
            return None


def thread_name(tid: int) -> str | None:
    """Cached per thread id; names are set once at thread start."""
    if tid not in _names:
        _names[tid] = _query(tid)
    return _names[tid]


def forget_except(live_tids: set[int]) -> None:
    for tid in [t for t in _names if t not in live_tids]:
        del _names[tid]
