#!/usr/bin/env python3
"""Background arbiter + renderer for the Claude Code status lamp.

The hooks only *record* each session's state (write-only mode); this daemon
re-reads the shared state on a timer, reaps sessions whose owning process died,
demotes quiet "working" sessions to "idle", and paints the lamp:

  0 live sessions   -> off
  1 live session    -> solid colour across the whole matrix (its state)
  2+ live sessions  -> LAVA: one floating drop per session, drop colours =
                       session states (attention/error always win a slot)

Lava is rendered two ways at once (hybrid):

  * base state — WLED's native 2D "Blobs" effect, set with one JSON POST per
    state change. Fully autonomous: keeps animating if this daemon dies, the
    PC sleeps, or the LAN path breaks. Its one flaw: it moves drops in whole
    pixels, which reads as stepping on a 16x16 matrix.
  * smooth overlay — while the daemon runs it also streams subpixel metaball
    frames (lamp_lava.py) over WLED realtime UDP on top of that base. WLED
    falls back to the native effect ~2s after the stream stops, automatically.
    Set LAMP_STREAM=0 to disable streaming and keep the native effect only.

Requires WLED's 2D matrix layout to be configured once (Settings -> LED -> 2D,
or /json/cfg hw.led.matrix + reboot); without it the native effect falls back
to a 1D smear but nothing breaks.

All state logic (aggregation, TTLs, process reaping, locking, colours) is
reused from lamp_status(.local).py — this file adds the loop, the renderer, a
single-instance guard, and a testing entry point.

Config via env:
  LAMP_POLL    seconds between state re-evaluations (default 5)
  LAMP_STREAM  0 to disable the smooth realtime overlay (default on)
  WLED_IP      lamp address (only used when lamp_status.local.py is absent)
  WLED_DEBUG   print tracebacks instead of failing silently

Usage:
  python lamp_daemon.py            run the loop (headless: use pythonw)
  python lamp_daemon.py --once     one re-evaluation, then exit (apply base)
  python lamp_daemon.py --once --dry   compute + print the view, do not apply
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module(names):
    for name in names:
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


L = _load_module(("lamp_status.local.py", "lamp_status.py"))
if L is None:
    raise RuntimeError("lamp_status(.local).py not found next to lamp_daemon.py")
LV = _load_module(("lamp_lava.py",))  # smooth-stream renderer; optional

POLL = max(1, int(os.environ.get("LAMP_POLL", "5")))
_LOCK = os.path.join(L._TMP, "claude_lamp_daemon.lock")

# Native-Blobs base look: very slow drift, heavy blur, long trail. This is the
# *fallback* picture (shown when the smooth stream isn't running), so it should
# read calm even with its whole-pixel stepping.
_LAVA = {"sx": 8, "c1": 235, "c2": 55, "pal": 5}
_MAX_BLOBS = 8      # WLED 2D Blobs renders at most 8 drops
_MAX_SEGS = 4       # deactivate this many segment slots we may have used
_STREAM_FPS = 15

_geom = None        # cached (width, height, is2d)
_fx_blobs = None    # cached effect id for "Blobs" (121 in stock builds)


def _http(path, payload=None, timeout=3):
    url = f"http://{L.LAMP_IP}{path}"
    if payload is None:
        return json.load(L._DIRECT.open(url, timeout=timeout))
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    return L._DIRECT.open(req, timeout=timeout)


def _matrix_geom():
    """(w, h, is2d). With a 2D layout, segment start/stop are COLUMNS and
    startY/stopY rows; without one they're plain LED indexes."""
    global _geom
    if _geom:
        return _geom
    try:
        leds = _http("/json/info").get("leds") or {}
        m = leds.get("matrix") or {}
        w, h = int(m.get("w") or 0), int(m.get("h") or 0)
        if w > 1 and h > 1:
            _geom = (w, h, True)
        else:
            _geom = (int(leds.get("count") or 256), 1, False)
        return _geom
    except Exception:
        return (256, 1, False)  # don't cache a failure


def _blobs_fx():
    """Resolve the "Blobs" effect id by name (stable across builds)."""
    global _fx_blobs
    if _fx_blobs:
        return _fx_blobs
    try:
        eff = _http("/json/eff")
        _fx_blobs = eff.index("Blobs")
    except Exception:
        _fx_blobs = 121  # stock id, fallback only
    return _fx_blobs


def _session_states(sessions, now):
    """One (session_id, tool, effective_state) per live session — a drop each."""
    out = []
    for sid, v in sessions.items():
        s = v.get("s")
        if s == "working" and now - v.get("t", 0) > L.WORKING_TTL:
            s = "idle"  # quiet past the window -> teal, not a false green
        out.append((sid, v.get("tool") or "claude", s))
    return out


def _render(now):
    """Reap dead sessions, then return the per-session view to show."""
    fd = L._acquire()
    try:
        sessions = L._load()
        reaped = L._reap_dead(sessions, now)
        if len(reaped) != len(sessions):
            L._save(reaped)
    finally:
        L._release(fd)
    return _session_states(reaped, now)


def _full_seg(extra):
    """A segment covering the whole matrix, 2D-aware."""
    w, h, is2d = _matrix_geom()
    seg = {"id": 0, "start": 0, "stop": w, "on": True}
    if is2d:
        seg["startY"], seg["stopY"] = 0, h
    seg.update(extra)
    return seg


def _lava_slots(states):
    """3 palette slots from the states present: every distinct state (priority
    first) gets a slot; the extra slot goes to the most numerous state."""
    cnt = Counter(states)
    distinct = [s for s in L.PRIORITY if s in cnt]
    if len(distinct) >= 3:
        return distinct[:3]
    if len(distinct) == 2:
        major = max(distinct, key=lambda s: cnt[s])
        return [distinct[0], distinct[1], major]
    return distinct * 3


def _apply_view(active):
    """Set the lamp's BASE state for the live sessions. True on success.

    Always drives the full seg[] with explicit bounds and deactivates the
    unused slots, so every render deterministically owns the whole matrix
    (a WLED preset can't do this — ps:N doesn't reset segment bounds)."""
    states = [s for _sid, _t, s in active]
    if not active:
        payload = {"on": False}
    elif len(active) == 1:
        r, g, b = L.STATE_RGB[states[0]]
        seg = [_full_seg({"fx": 0, "bri": L.STATE_BRI[states[0]], "col": [[r, g, b]]})]
        seg += [{"id": i, "stop": 0} for i in range(1, _MAX_SEGS)]
        payload = {"on": True, "bri": 255, "seg": seg}
    else:
        blobs = min(len(active), _MAX_BLOBS)
        ix = min(255, max(0, round((blobs - 1) * 255 / (_MAX_BLOBS - 1))))
        col = [list(L.STATE_RGB[s]) for s in _lava_slots(states)]
        seg = [_full_seg({"fx": _blobs_fx(), "ix": ix,
                          "bri": max(L.STATE_BRI[s] for s in states),
                          "col": col, **_LAVA})]
        seg += [{"id": i, "stop": 0} for i in range(1, _MAX_SEGS)]
        payload = {"on": True, "bri": 255, "seg": seg}
    try:
        _http("/json/state", payload, timeout=2)
        return True
    except Exception:
        return False


def _describe(active):
    if not active:
        return "off"
    if len(active) == 1:
        return "%s:%s" % (active[0][1], active[0][2])
    cnt = Counter(s for _sid, _t, s in active)
    parts = " ".join("%sx%d" % (s, cnt[s]) for s in L.PRIORITY if s in cnt)
    return "lava x%d: %s" % (len(active), parts)  # ASCII on purpose (cp1251 consoles)


def _signature(active):
    if not active:
        return ("off",)
    if len(active) == 1:
        return ("solid", active[0][1], active[0][2])
    return ("lava", min(len(active), _MAX_BLOBS),
            tuple(sorted(Counter(s for _sid, _t, s in active).items())))


def _stream_on():
    return (LV is not None
            and os.environ.get("LAMP_STREAM", "1") not in ("0", "false", "no"))


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
    last_poll = 0.0
    active = []
    field = None  # smooth-stream metaball field, alive only in lava mode
    try:
        while True:
            now = time.time()
            if now - last_poll >= POLL:
                last_poll = now
                try:
                    active = _render(int(now))
                    sig = _signature(active)
                    if sig != last_sig or last_ok is not True:  # changed, or retry a failed POST
                        ok = _apply_view(active)                # base state (native fallback)
                        if sig != last_sig or ok != last_ok:    # log real transitions only
                            L._log("lamp -> [%s] %s" % (_describe(active),
                                   "ok" if ok else "UNREACHABLE (offline / LAN blocked?)"))
                        last_sig, last_ok = sig, ok
                    os.utime(_LOCK, None)  # heartbeat for the stale-lock reclaim
                except Exception:
                    if os.environ.get("WLED_DEBUG"):
                        import traceback
                        traceback.print_exc()
            if len(active) >= 2 and _stream_on():
                # smooth overlay: subpixel metaballs over realtime UDP; WLED
                # reverts to the native base ~2s after the stream stops
                try:
                    if field is None:
                        field = LV.Field(calm=True)
                    field.sync([(sid, L.STATE_RGB[s]) for sid, _t, s in active])
                    field.step()
                    LV.stream(field.render())
                except Exception:
                    if os.environ.get("WLED_DEBUG"):
                        import traceback
                        traceback.print_exc()
                time.sleep(1.0 / _STREAM_FPS)
            else:
                field = None
                time.sleep(min(POLL, 1))
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
        print("view -> [%s]" % _describe(active))
        if "--dry" not in args:
            print("applied:", _apply_view(active))
        return
    _run()


if __name__ == "__main__":
    main()
