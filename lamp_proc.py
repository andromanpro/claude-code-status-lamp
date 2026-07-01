#!/usr/bin/env python3
"""Process-identity helpers for reliable session liveness (stdlib only).

Sessions are keyed by session_id, but a session that closes without a clean
SessionEnd (hard-closed window, crash, kill) would otherwise linger until a
6-hour TTL. These helpers let the *writer* (which runs as a descendant of the
Claude Code session process) record the owning process — its PID plus a
start-time "birthmark" that survives PID reuse — and let the *daemon* reap a
session the instant that process is gone, in any state (incl. a stuck amber).

Best-effort and fail-silent: if the owner can't be determined (unsupported
platform, access denied), owner_identity() returns (None, None, None) and the
caller falls back to the time-based heuristic. Nothing here ever raises.

    owner_identity() -> (pid, birthmark, exe)   # call from the hook/writer
    is_alive(pid, birthmark) -> bool            # call from the daemon/reaper
"""
import os
import sys

# Ancestors to walk *past* when looking for the session-owning process. The
# hook is python, launched via py.exe / a shell, so the real owner is the first
# ancestor that isn't one of these launchers.
_WRAPPERS = {
    "python.exe", "pythonw.exe", "py.exe", "python", "python3", "pythonw",
    "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe",
    "sh", "bash", "dash", "zsh", "fish",
    "sh.exe", "bash.exe", "dash.exe", "zsh.exe", "wsl.exe",
    "npm", "npm.cmd", "npx", "npx.cmd", "node.cmd",
}
_MAX_WALK = 15


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID = wintypes.HANDLE(-1).value

    class _PE32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME)]

    def _snapshot():
        """pid -> (ppid, exe_name_lower) for every process. {} on failure."""
        procs = {}
        h = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not h or h == _INVALID:
            return procs
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            if not _k32.Process32FirstW(h, ctypes.byref(e)):
                return procs
            while True:
                procs[int(e.th32ProcessID)] = (
                    int(e.th32ParentProcessID), (e.szExeFile or "").lower())
                if not _k32.Process32NextW(h, ctypes.byref(e)):
                    break
        finally:
            _k32.CloseHandle(h)
        return procs

    def _creation(pid):
        """Process creation time as an int birthmark, or None."""
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            c, x, k, u = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x),
                                        ctypes.byref(k), ctypes.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            _k32.CloseHandle(h)

    def owner_identity():
        try:
            procs = _snapshot()
            pid = os.getppid()
            seen = set()
            for _ in range(_MAX_WALK):
                if pid in seen or pid not in procs:
                    break
                seen.add(pid)
                ppid, name = procs[pid]
                if name not in _WRAPPERS:
                    return pid, _creation(pid), name
                pid = ppid
        except Exception:
            pass
        return None, None, None

    def is_alive(pid, birthmark):
        try:
            if not pid:
                return False
            h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False  # no such process
            try:
                if birthmark is None:
                    return True
                c, x, k, u = (wintypes.FILETIME() for _ in range(4))
                if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x),
                                            ctypes.byref(k), ctypes.byref(u)):
                    return True
                now = (c.dwHighDateTime << 32) | c.dwLowDateTime
                return now == birthmark  # different start time => PID reused
            finally:
                _k32.CloseHandle(h)
        except Exception:
            return True  # never reap on an internal error


else:  # POSIX: full support via /proc (Linux); degraded elsewhere (macOS)
    def _read(path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ""

    def _proc(pid):
        """(ppid, comm_lower, starttime) from /proc/<pid>/stat, or (None,'',None)."""
        stat = _read(f"/proc/{pid}/stat")
        if not stat:
            return None, "", None
        try:
            rp = stat.rfind(")")          # comm is parenthesised, may hold spaces
            comm = stat[stat.find("(") + 1:rp].lower()
            rest = stat[rp + 2:].split()
            return int(rest[1]), comm, int(rest[19])  # ppid (f4), starttime (f22)
        except (ValueError, IndexError):
            return None, "", None

    def _boot_id():
        return _read("/proc/sys/kernel/random/boot_id").strip()

    def owner_identity():
        try:
            pid = os.getppid()
            seen = set()
            for _ in range(_MAX_WALK):
                if pid in seen:
                    break
                seen.add(pid)
                ppid, comm, start = _proc(pid)
                if ppid is None:
                    break  # no /proc (e.g. macOS) -> degrade to heuristic
                if comm not in _WRAPPERS:
                    return pid, f"{start}:{_boot_id()}", comm
                pid = ppid
        except Exception:
            pass
        return None, None, None

    def is_alive(pid, birthmark):
        try:
            if not pid:
                return False
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # exists, not ours
            if birthmark is None:
                return True
            _, _, start = _proc(int(pid))
            if start is None:
                return True  # can't verify birthmark -> don't reap
            return f"{start}:{_boot_id()}" == birthmark
        except Exception:
            return True


if __name__ == "__main__":  # quick self-check
    pid, birth, exe = owner_identity()
    print(f"owner_identity -> pid={pid} exe={exe!r} birthmark={birth}")
    print(f"is_alive(owner) -> {is_alive(pid, birth)}")
    print(f"is_alive(2**30 fake) -> {is_alive(2**30, None)}")
    print(f"is_alive(owner, wrong birthmark) -> {is_alive(pid, (birth or 0) + 1 if isinstance(birth, int) else 'x')}")
