#!/usr/bin/env python3
"""Create the Claude status presets on your WLED lamp (one-time setup).

    python init_presets.py <WLED_IP>
    python init_presets.py 192.168.1.50

Creates / OVERWRITES these preset slots on the target lamp:
    1 = working   (blue)
    2 = attention (amber)
    3 = done      (green)
    5 = error     (red)

⚠️  This overwrites slots 1, 2, 3 and 5. If you already use those for room
lighting, change the numbers in PRESETS below first.

Uses WLED's JSON API: set a deterministic solid color, psave it, then read
/presets.json back to confirm it actually saved (the slow ESP8266 sometimes
needs a couple of tries). Alternatively just restore presets.json in the WLED
UI (Config -> Security & Backup -> Restore presets).
"""
import sys
import json
import time
import urllib.request

PRESETS = [
    (1, "Claude-working",   (0, 80, 255), 110),
    (2, "Claude-attention", (255, 150, 0), 200),
    (3, "Claude-done",      (0, 255, 40), 150),
    (5, "Claude-error",     (255, 0, 0),  200),
]


# Talk to the lamp on the LAN directly — never through a system HTTP/SOCKS proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(ip, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{ip}/json/state",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _DIRECT.open(req, timeout=5).read()


def get_json(ip, path):
    with _DIRECT.open(f"http://{ip}{path}", timeout=5) as r:
        return json.loads(r.read())


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python init_presets.py <WLED_IP>   e.g. python init_presets.py 192.168.1.50")
    ip = sys.argv[1]

    # connectivity + "is this actually WLED?" pre-check, so we fail loudly not silently
    try:
        info = get_json(ip, "/json/info")
    except Exception as e:
        sys.exit(f"Could not reach WLED at {ip}: {e}\n"
                 f"Check the IP, and that the lamp is powered and on this subnet.")
    if "ver" not in info:
        print(f"Warning: {ip} responded but does not look like WLED — continuing anyway.")

    count = int((info.get("leds") or {}).get("count") or 0)  # full-strip length

    print(f"Writing presets to WLED at {ip} (overwrites slots 1, 2, 3, 5)...")
    all_ok = True
    for preset, name, (r, g, b), bri in PRESETS:
        # One solid segment spanning the whole strip, with any leftover extra
        # segments deactivated — so applying a preset always paints the full
        # matrix, never a slice (e.g. if a segment layout was left behind).
        seg0 = {"id": 0, "start": 0, "on": True, "fx": 0, "sx": 128, "ix": 128,
                "pal": 0, "col": [[r, g, b], [0, 0, 0], [0, 0, 0]]}
        if count:
            seg0["stop"] = count
        post(ip, {"on": True, "bri": bri,
                  "seg": [seg0, {"id": 1, "stop": 0}, {"id": 2, "stop": 0}, {"id": 3, "stop": 0}]})
        time.sleep(0.3)
        post(ip, {"psave": preset, "n": name})

        ok = False
        for _ in range(5):
            time.sleep(0.4)
            try:
                if str(preset) in get_json(ip, "/presets.json"):
                    ok = True
                    break
            except Exception:
                pass
        all_ok = all_ok and ok
        print(f"  preset {preset} '{name}' = RGB({r},{g},{b})  {'OK' if ok else 'NOT SAVED — re-run?'}")

    if all_ok:
        print("Done. Test with:  python lamp_status.py working   (then attention / done / error)")
    else:
        sys.exit("Some presets did not save. Re-run, or restore presets.json in the WLED UI.")


if __name__ == "__main__":
    main()
