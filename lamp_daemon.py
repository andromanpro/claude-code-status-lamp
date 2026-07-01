#!/usr/bin/env python3
"""Background arbiter for the Claude Code status lamp.

The hooks write each session's state to the shared file and push the winning
preset — but the lamp is only re-evaluated when a hook fires. A session that
goes quiet with no idle event left the lamp stuck on its last colour (e.g. a
finished-but-not-closed session pinning it blue). This daemon re-evaluates the
shared state on a timer and applies the winning preset, so a stale "working"
session is demoted to green (and the lamp turns off once nothing is live), even
when no hook fires. Immediate transitions (blue on work, amber on a prompt) are
still driven instantly by the hooks; the daemon is the safety net for the
delayed/idle cases and the foundation for per-agent zones later.

All state logic (aggregation, TTL freshness window, WLED apply, locking) is
reused verbatim from lamp_status(.local).py — this file only adds the loop, a
single-instance guard, and a testing entry point.

Config via env:
  LAMP_POLL   seconds between re-evaluations (default 5)
  WLED_IP     lamp address (only used when lamp_status.local.py is absent)
  WLED_DEBUG  print tracebacks instead of failing silently

Usage:
  python lamp_daemon.py            run the loop (headless: use pythonw)
  python lamp_daemon.py --once     one re-evaluation, then exit (apply)
  python lamp_daemon.py --once --dry   compute + print the preset, do not apply
"""
import importlib.util
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_logic():
    """Load the same module the hooks use — prefer the real local config."""
    for name in ("lamp_status.local.py", "lamp_status.py"):
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("lamp_status_logic", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("lamp_status(.local).py not found next to lamp_daemon.py")


L = _load_logic()

POLL = max(1, int(os.environ.get("LAMP_POLL", "5")))
_LOCK = os.path.join(L._TMP, "claude_lamp_daemon.lock")


def _target_preset(now):
    """Read shared state under lock, aggregate, return the preset (0 = off)."""
    fd = L._acquire()
    try:
        sessions = L._load()
        live, agg = L._aggregate(sessions, now)
        if len(live) != len(sessions):  # opportunistic cleanup of TTL-expired
            L._save(live)
    finally:
        L._release(fd)
    return L.STATE_PRESET[agg] if agg else 0


def _single_instance():
    """Return a held lock fd, or None if another daemon already runs."""
    try:
        fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except FileExistsError:
        try:  # reclaim a lock left behind by a hard-killed daemon
            if time.time() - os.path.getmtime(_LOCK) > POLL * 3 + 5:
                os.remove(_LOCK)
                return _single_instance()
        except OSError:
            pass
        return None
    except OSError:
        return None


def _run():
    fd = _single_instance()
    if fd is None:
        return  # another daemon already owns the lamp
    try:
        while True:
            try:
                L._apply(_target_preset(int(time.time())))
                os.utime(_LOCK, None)  # heartbeat for the stale-lock reclaim
            except Exception:
                if os.environ.get("WLED_DEBUG"):
                    import traceback
                    traceback.print_exc()
            time.sleep(POLL)
    finally:
        try:
            os.close(fd)
            os.remove(_LOCK)
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    if "--once" in args:
        preset = _target_preset(int(time.time()))
        print(f"aggregate preset -> {preset}")
        if "--dry" not in args:
            L._apply(preset)
        return
    _run()


if __name__ == "__main__":
    main()
