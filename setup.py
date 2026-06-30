#!/usr/bin/env python3
"""One-time setup: create the three Claude status presets on your WLED lamp.

Usage:
    python setup.py <WLED_IP>
    python setup.py 192.168.1.50

Creates:
    preset 1 = working   (blue)
    preset 2 = attention (amber)
    preset 3 = done      (green)

This uses WLED's JSON API: set a Solid color, then save it as a preset.
(The legacy /win?PL= command and "save a cycle command" do not work reliably
across WLED builds, so we drive presets explicitly.)
"""
import sys
import json
import time
import urllib.request

PRESETS = [
    (1, "Claude-working",   (0, 80, 255), 160),
    (2, "Claude-attention", (255, 150, 0), 180),
    (3, "Claude-done",      (0, 255, 40), 160),
]


def post(ip, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{ip}/json/state",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def main():
    if len(sys.argv) < 2:
        print("Usage: python setup.py <WLED_IP>   e.g. python setup.py 192.168.1.50")
        sys.exit(1)
    ip = sys.argv[1]
    for preset, name, (r, g, b), bri in PRESETS:
        post(ip, {"on": True, "bri": bri, "seg": [{"id": 0, "fx": 0, "col": [[r, g, b]]}]})
        time.sleep(0.4)
        post(ip, {"psave": preset, "n": name})
        time.sleep(0.4)
        print(f"  preset {preset} '{name}' = RGB({r}, {g}, {b})")
    print("Done. Test with:  python lamp_status.py working   (then attention / done)")


if __name__ == "__main__":
    main()
