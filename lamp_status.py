#!/usr/bin/env python3
"""Claude Code -> WLED status light. Applies a WLED preset by status name.

Called from Claude Code hooks (see examples/settings-hooks.json):

    python lamp_status.py working     # blue  - Claude is working
    python lamp_status.py attention   # amber - needs your input / permission
    python lamp_status.py done        # green - turn finished

Configure the lamp address via the WLED_IP environment variable, or edit
LAMP_IP below. Fail-silent: any network/lamp error is swallowed so Claude
is never blocked.
"""
import os
import sys
import json
import urllib.request

# Your WLED lamp IP. Override with the WLED_IP env var, or edit this line.
LAMP_IP = os.environ.get("WLED_IP", "192.168.1.50")

# status name -> WLED preset number (created by setup.py / presets.json)
PRESETS = {"working": 1, "attention": 2, "done": 3, "off": 0}


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "done").strip().lower()
    preset = PRESETS.get(arg)
    if preset is None:
        preset = int(arg) if arg.lstrip("-").isdigit() else 3
    body = json.dumps({"ps": preset}).encode()
    req = urllib.request.Request(
        f"http://{LAMP_IP}/json/state",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # lamp offline / wrong network: stay silent, never block Claude


if __name__ == "__main__":
    main()
