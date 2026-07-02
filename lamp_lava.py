#!/usr/bin/env python3
"""Lava-lamp renderer for the WLED matrix — floating metaball "drops", one per
live session, coloured by that session's state, streamed as realtime UDP (DRGB).

Realtime UDP (port 21324, protocol DRGB) lets the PC be the renderer and push a
full 16x16 frame ~15 fps, instead of the slow HTTP-preset path. WLED drops back
to its normal state ~`timeout` seconds after the last packet, so the stream must
be kept alive while lava mode is active.

The daemon streams this as a smooth overlay ON TOP of the native 2D "Blobs"
effect it sets as the base state (see lamp_daemon.py): the native effect moves
drops in whole pixels (stepping on a 16x16 matrix), this renderer moves a
continuous field (subpixel-smooth), and WLED auto-falls back to the native base
~2s after the stream stops — so the lamp stays autonomous when the PC isn't.

Standalone check (fake drops, to verify look + serpentine orientation on the
real lamp):
    python lamp_lava.py --demo [seconds]     # calm drift, 4 state colours
    python lamp_lava.py --orient             # bottom row red, left column green

The Field class keeps per-key metaball physics: sync() adds/removes/retints
drops from any [(key, rgb)] list, step() advances, render() rasterises a frame.
Colours come from the shared STATE_RGB so the look matches the daemon's.
"""
import importlib.util
import math
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_logic():
    for name in ("lamp_status.local.py", "lamp_status.py"):
        p = os.path.join(_HERE, name)
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("lamp_status_logic", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("lamp_status(.local).py not found next to lamp_lava.py")


L = _load_logic()

W = H = 16
LEDS = W * H
PORT = 21324
MASTER = 0.95   # global scale
BLOB_R = 1.9    # metaball radius — small = distinct drops, big = a soft wash
_LO, _SPAN = 0.9, 1.3  # brightness threshold window (higher LO = crisper edges)

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def xy_to_idx(x, y):
    """Matrix (x, y) -> physical LED index. Serpentine, bottom-left origin,
    even rows run left->right (WLED indexes the raw strip; 2D isn't configured).
    y = 0 is the BOTTOM row. Flip here if the demo looks torn/mirrored."""
    return y * W + (x if (y % 2 == 0) else (W - 1 - x))


def stream(frame, timeout=2):
    """frame: LEDS x (r, g, b). Send one WLED DRGB realtime packet (direct UDP)."""
    buf = bytearray((2, timeout))  # 2 = DRGB, then per-LED RGB from index 0
    for r, g, b in frame:
        buf.append(r & 255)
        buf.append(g & 255)
        buf.append(b & 255)
    _sock.sendto(bytes(buf), (L.LAMP_IP, PORT))


class Field:
    """Persistent metaball physics keyed by session id, rendered to a frame."""

    def __init__(self, calm=True):
        self.blobs = {}                 # key -> {x, y, vx, vy, r, col}
        self.spd = 0.9 if calm else 2.1
        self._seed = 1234567

    def _rand(self):                    # deterministic LCG (no Math.random deps)
        self._seed = (self._seed * 1103515245 + 12345) & 0x7FFFFFFF
        return self._seed / 0x7FFFFFFF

    def sync(self, items):
        """items: [(key, (r, g, b)), ...] — add new drops, drop gone, retint."""
        seen = set()
        for key, col in items:
            seen.add(key)
            b = self.blobs.get(key)
            if b is None:
                ang = self._rand() * 6.2832
                self.blobs[key] = {
                    "x": 2 + self._rand() * (W - 4),
                    "y": 2 + self._rand() * (H - 4),
                    "vx": math.cos(ang) * 0.02 * self.spd,
                    "vy": math.sin(ang) * 0.02 * self.spd,
                    "r": BLOB_R,
                    "col": [int(col[0]), int(col[1]), int(col[2])],
                }
            else:
                tc = [int(col[0]), int(col[1]), int(col[2])]
                for i in range(3):      # ease toward the new state colour
                    b["col"][i] += (tc[i] - b["col"][i]) * 0.25
        for key in list(self.blobs):
            if key not in seen:
                del self.blobs[key]

    def step(self):
        for b in self.blobs.values():
            b["x"] += b["vx"]
            b["y"] += b["vy"]
            b["vy"] += (0.0007 if b["y"] < H / 2 else -0.0007) * self.spd  # slow bob
            # let centres reach the very edge rows/cols so drops get "cut" by them
            if b["x"] < 0.5:
                b["x"] = 0.5; b["vx"] = abs(b["vx"])
            if b["x"] > W - 0.5:
                b["x"] = W - 0.5; b["vx"] = -abs(b["vx"])
            if b["y"] < 0.5:
                b["y"] = 0.5; b["vy"] = abs(b["vy"])
            if b["y"] > H - 0.5:
                b["y"] = H - 0.5; b["vy"] = -abs(b["vy"])

    def render(self):
        frame = [(0, 0, 0)] * LEDS
        blobs = list(self.blobs.values())
        if not blobs:
            return frame
        for y in range(H):
            for x in range(W):
                f = r = g = bl = 0.0
                for b in blobs:
                    dx = x - b["x"]
                    dy = y - b["y"]
                    w = (b["r"] * b["r"]) / (dx * dx + dy * dy + 0.7)
                    f += w
                    r += w * b["col"][0]
                    g += w * b["col"][1]
                    bl += w * b["col"][2]
                r /= f; g /= f; bl /= f
                t = (f - _LO) / _SPAN
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                bri = (t * t * (3 - 2 * t)) * MASTER   # smoothstep -> crisp drop edges
                frame[xy_to_idx(x, y)] = (int(r * bri), int(g * bri), int(bl * bri))
        return frame


def _demo(seconds, calm=True):
    fld = Field(calm=calm)
    fld.sync([
        ("working", L.STATE_RGB["working"]),
        ("attention", L.STATE_RGB["attention"]),
        ("idle", L.STATE_RGB["idle"]),
        ("done", L.STATE_RGB["done"]),
    ])
    t0 = time.time()
    frames = 0
    while time.time() - t0 < seconds:
        fld.step()
        stream(fld.render())
        frames += 1
        time.sleep(1 / 15.0)
    print(f"streamed {frames} frames in {seconds}s -> letting realtime time out")


def _orient():
    """Static frame: bottom row (y=0) red, left column (x=0) green -> the
    bottom-left corner reads yellow. Tells us origin + row direction."""
    frame = [(0, 0, 0)] * LEDS
    for x in range(W):
        frame[xy_to_idx(x, 0)] = (120, 0, 0)      # bottom row -> red
    for y in range(H):
        gy = frame[xy_to_idx(0, y)]
        frame[xy_to_idx(0, y)] = (gy[0], 120, 0)  # left column -> green (corner yellow)
    for _ in range(40):
        stream(frame, timeout=3)
        time.sleep(0.1)
    print("orient frame sent (bottom=red, left=green, corner=yellow)")


def main():
    args = sys.argv[1:]
    if "--orient" in args:
        _orient()
        return
    if "--demo" in args:
        secs = 20
        for a in args:
            if a.isdigit():
                secs = int(a)
        _demo(secs, calm="--lively" not in args)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
