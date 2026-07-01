#!/usr/bin/env python3
"""Claude Code -> WLED status light, with multi-session arbitration.

Called from Claude Code hooks (see examples/settings-hooks.json):

    python lamp_status.py working     # blue  - this session is working
    python lamp_status.py attention   # amber - this session needs you
    python lamp_status.py done        # green - this session is idle
    python lamp_status.py error       # red   - this session's turn failed
    python lamp_status.py off         # this session ended

Multiple Claude Code sessions can share one lamp. Each session's state is
tracked by its session_id (read from the hook's stdin JSON) in a shared file in
the temp dir, and the lamp shows the highest-priority state across all live
sessions:

    attention (amber)  >  error (red)  >  working (blue)  >  done (green)

So "attention" from any session latches the lamp amber until that session is no
longer blocked, even if another session is busy or idle. With a single session
it behaves like a plain one-state indicator. When the last session ends (off),
the lamp turns off.

Configure the lamp via the WLED_IP env var, or edit LAMP_IP below.
Fail-silent: any error is swallowed so Claude is never blocked. Set WLED_DEBUG=1
to print tracebacks. A bare integer arg ("python lamp_status.py 4") applies that
preset directly, bypassing the arbiter (manual override).
"""
import os
import sys
import json
import time
import tempfile
import urllib.request

try:
    import lamp_proc  # process-identity helpers for reliable session liveness
except Exception:  # keep working even if the helper is missing
    lamp_proc = None

LAMP_IP = os.environ.get("WLED_IP", "192.168.1.50")

# session state -> WLED preset number
STATE_PRESET = {"attention": 2, "error": 5, "working": 1, "done": 3}
# state -> RGB + brightness, for the daemon's per-agent zone rendering (seg[])
STATE_RGB = {"working": (0, 80, 255), "attention": (255, 150, 0), "done": (0, 255, 40), "error": (255, 0, 0)}
STATE_BRI = {"working": 110, "attention": 200, "done": 150, "error": 200}
# aggregation priority, highest first
PRIORITY = ["attention", "error", "working", "done"]
TTL = 6 * 3600  # forget a session not updated in 6h (crash safety net)
# A "working" session quiet longer than this counts as idle ("done"): it
# finished or was killed without a clean idle_prompt / SessionEnd. Active work
# refreshes "working" on every tool call, so this only demotes genuinely quiet
# sessions and stops a stuck/zombie session from pinning the lamp blue.
WORKING_TTL = 180

_TMP = tempfile.gettempdir()
_SESSIONS = os.path.join(_TMP, "claude_lamp_sessions.json")
_LAST = os.path.join(_TMP, "claude_lamp_last")
_LOCK = os.path.join(_TMP, "claude_lamp.lock")
_LOG = os.path.join(_TMP, "claude_lamp.log")
_LOG_MAX = 256 * 1024  # bytes; the log rotates to .1 once it grows past this


def _log(msg):
    """Append a timestamped line to the debug log. Set LAMP_LOG=0 to disable."""
    if os.environ.get("LAMP_LOG", "1") in ("0", "false", "no"):
        return
    try:
        if os.path.getsize(_LOG) > _LOG_MAX:
            os.replace(_LOG, _LOG + ".1")
    except OSError:
        pass
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _acquire():
    """Tiny cross-platform spin lock; best-effort (returns None after 2s)."""
    start = time.time()
    while True:
        try:
            return os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_LOCK) > 5:  # stale lock
                    os.remove(_LOCK)
                    continue
            except OSError:
                pass
            if time.time() - start > 2:
                return None
            time.sleep(0.05)


def _release(fd):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(_LOCK)
    except OSError:
        pass


def _load():
    try:
        with open(_SESSIONS) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(sessions):
    try:
        tmp = _SESSIONS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sessions, f)
        os.replace(tmp, _SESSIONS)
    except OSError:
        pass


def _session_id():
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.buffer.read()
            if raw:
                return json.loads(raw.decode("utf-8-sig", "replace")).get("session_id") or "default"
    except Exception:
        pass
    return "default"


def _tool():
    """Which agent this hook is for (--tool claude|codex|...); default claude."""
    a = sys.argv
    if "--tool" in a:
        i = a.index("--tool")
        if i + 1 < len(a):
            return (a[i + 1] or "claude").strip().lower() or "claude"
    return "claude"


# Talk to the lamp on the LAN directly — never through a proxy. A system
# HTTP/SOCKS proxy (a VPN or anti-censorship tool) that can't reach a LAN
# address would otherwise make this POST hang and block the hook.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _apply(preset):
    """POST the preset (0 = off), de-duping repeats. Returns True on success,
    False if the lamp couldn't be reached (offline, or LAN blocked by a VPN)."""
    try:
        with open(_LAST) as f:
            if f.read().strip() == str(preset):
                return True
    except OSError:
        pass
    payload = {"on": False} if preset == 0 else {"on": True, "ps": preset}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{LAMP_IP}/json/state",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        _DIRECT.open(req, timeout=2)
    except Exception:
        return False
    try:
        with open(_LAST, "w") as f:
            f.write(str(preset))
    except OSError:
        pass
    return True


def _aggregate(sessions, now):
    """Highest-priority preset across live sessions (None -> off). Pure/testable.

    Drops sessions past TTL, and demotes a stale "working" session to "done" so a
    finished-but-not-closed (or hard-killed) session no longer forces the lamp
    blue. Returns (live_sessions, aggregate_state_or_None).
    """
    live = {k: v for k, v in sessions.items() if now - v.get("t", 0) <= TTL}

    def _effective(v):
        s = v.get("s")
        if s == "working" and now - v.get("t", 0) > WORKING_TTL:
            return "done"
        return s

    states = {_effective(v) for v in live.values()}
    agg = next((s for s in PRIORITY if s in states), None)
    return live, agg


def _reap_dead(sessions, now):
    """Drop sessions whose owning process is gone, plus past-TTL leftovers.

    The owning-process check (via lamp_proc) is the reliable path: a session
    that closed without a clean SessionEnd — hard-closed window, crash, kill —
    is removed the instant its process dies, in ANY state (incl. a stuck amber),
    not after a 6h heuristic. Records with no resolved owner (or a platform
    where it couldn't be determined) fall back to the TTL safety net.
    """
    live = {}
    for k, v in sessions.items():
        if now - v.get("t", 0) > TTL:
            _log(f"reap {k[:8]} (TTL) was {v.get('s')}")
            continue
        pid = v.get("pid")
        if pid and lamp_proc is not None and not lamp_proc.is_alive(pid, v.get("pst")):
            _log(f"reap {k[:8]} (owner {pid} gone) was {v.get('s')}")
            continue
        live[k] = v
    return live


def main():
    try:
        arg = (sys.argv[1] if len(sys.argv) > 1 else "done").strip().lower()

        # manual override: a bare integer preset bypasses the arbiter
        if arg not in STATE_PRESET and arg != "off":
            try:
                _apply(max(0, int(arg)))
            except ValueError:
                _apply(STATE_PRESET["done"])
            return

        sid = _session_id()
        now = int(time.time())
        fd = _acquire()
        try:
            sessions = _load()
            if arg == "off":
                sessions.pop(sid, None)
            else:
                rec = {"s": arg, "t": now, "tool": _tool()}
                prev = sessions.get(sid) or {}
                if prev.get("pid"):  # resolve the owning process once per session
                    rec["pid"], rec["pst"] = prev.get("pid"), prev.get("pst")
                elif lamp_proc is not None:
                    opid, ostart, _oexe = lamp_proc.owner_identity()
                    if opid:
                        rec["pid"], rec["pst"] = opid, ostart
                sessions[sid] = rec
            sessions = _reap_dead(sessions, now)
            live, agg = _aggregate(sessions, now)
            _save(live)
        finally:
            _release(fd)

        # Write-only mode (--write-only or LAMP_WRITE_ONLY): record the state but
        # leave applying to the background daemon. Used by the Codex hooks so a
        # tool call never waits on the lamp's network (e.g. LAN blocked by a VPN).
        if "--write-only" in sys.argv or os.environ.get("LAMP_WRITE_ONLY"):
            return
        _apply(STATE_PRESET[agg] if agg else 0)  # no live sessions -> off
    except Exception:
        if os.environ.get("WLED_DEBUG"):
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
