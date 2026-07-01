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
import json
import os
import sys
import time
import urllib.request

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

# Zones: each active agent gets an equal horizontal band, in this order (claude
# on top). Up to _MAX_ZONES agents are shown at once; one agent -> whole matrix.
_ORDER = ["claude", "codex", "openclaw", "opencode"]
_MAX_ZONES = 4
_leds = 0  # cached LED count (queried from the lamp once)


def _provider_states(sessions, now):
    """{provider: highest-priority effective state} across live sessions."""
    groups = {}
    for v in sessions.values():
        prov = v.get("tool") or "claude"
        s = v.get("s")
        if s == "working" and now - v.get("t", 0) > L.WORKING_TTL:
            s = "idle"  # quiet past the window -> its own teal, NOT green:
            #             a long tool call or a pause, not a confirmed "done"
        groups.setdefault(prov, set()).add(s)
    out = {}
    for prov, states in groups.items():
        agg = next((s for s in L.PRIORITY if s in states), None)
        if agg:
            out[prov] = agg
    return out


def _render(now):
    """Reap dead sessions, then return ordered [(provider, state), ...] to show."""
    fd = L._acquire()
    try:
        sessions = L._load()
        reaped = L._reap_dead(sessions, now)
        if len(reaped) != len(sessions):
            L._save(reaped)
    finally:
        L._release(fd)
    ps = _provider_states(reaped, now)
    ordered = [p for p in _ORDER if p in ps] + sorted(p for p in ps if p not in _ORDER)
    return [(p, ps[p]) for p in ordered]


def _led_count():
    global _leds
    if _leds:
        return _leds
    try:
        with L._DIRECT.open(f"http://{L.LAMP_IP}/json/info", timeout=2) as r:
            n = int((json.load(r).get("leds") or {}).get("count") or 0)
            if n:
                _leds = n
                return n
    except Exception:
        pass
    return 256  # sane default until the lamp answers


def _apply_zones(active):
    """Paint one solid-colour horizontal band per active agent (seg[]). True on
    success. One agent -> a single band across the whole matrix; N agents -> N
    equal bands, claude on top.

    The daemon always drives the full seg[] and deactivates the unused segments,
    so every render deterministically owns the whole matrix — a leftover band
    from a previous split can never linger (a WLED *preset* can't do this: ps:N
    paints colour/effect but doesn't reset segment bounds, so it can't clear a
    split). The cost is that solid colours replace any animated preset while the
    daemon is running; run the daemon as the sole renderer (write-only hooks)."""
    if not active:
        payload = {"on": False}
    else:
        n = len(active)
        count = _led_count()
        seg = []
        for i in range(_MAX_ZONES):
            if i < n:
                _prov, state = active[i]
                r, g, b = L.STATE_RGB[state]
                seg.append({"id": i, "start": i * count // n, "stop": (i + 1) * count // n,
                            "on": True, "fx": 0, "bri": L.STATE_BRI[state], "col": [[r, g, b]]})
            else:
                seg.append({"id": i, "stop": 0})  # deactivate unused segments
        payload = {"on": True, "bri": 255, "seg": seg}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://{L.LAMP_IP}/json/state", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        L._DIRECT.open(req, timeout=2)
        return True
    except Exception:
        return False


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
    L._log("daemon started")
    last_sig = None
    last_ok = None
    try:
        while True:
            try:
                active = _render(int(time.time()))
                sig = tuple(active)
                if sig != last_sig or last_ok is not True:  # changed, or retry a failed POST
                    ok = _apply_zones(active)
                    if sig != last_sig or ok != last_ok:    # log real transitions only
                        desc = " | ".join("%s:%s" % (p, s) for p, s in active) or "off"
                        L._log("lamp -> [%s] %s" % (desc, "ok" if ok else "UNREACHABLE (offline / LAN blocked?)"))
                    last_sig, last_ok = sig, ok
                os.utime(_LOCK, None)  # heartbeat for the stale-lock reclaim
            except Exception:
                if os.environ.get("WLED_DEBUG"):
                    import traceback
                    traceback.print_exc()
            time.sleep(POLL)
    finally:
        try:
            L._log("daemon stopped")
            os.close(fd)
            os.remove(_LOCK)
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    if "--once" in args:
        active = _render(int(time.time()))
        desc = " | ".join("%s:%s" % (p, s) for p, s in active) or "off"
        print("zones -> [%s]" % desc)
        if "--dry" not in args:
            print("applied:", _apply_zones(active))
        return
    _run()


if __name__ == "__main__":
    main()
