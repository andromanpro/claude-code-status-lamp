#!/usr/bin/env python3
"""Claude Code -> WLED status light. Applies a WLED preset by status name.

Called from Claude Code hooks (see examples/settings-hooks.json):

    python lamp_status.py working     # blue  - Claude is working
    python lamp_status.py attention   # amber - needs your input / permission
    python lamp_status.py done        # green - idle, waiting for you
    python lamp_status.py error       # red   - a turn failed
    python lamp_status.py off         # lamp off (e.g. on session end)

Configure the lamp address via the WLED_IP environment variable, or edit
LAMP_IP below.

Fail-silent: any network/lamp error is swallowed so Claude is never blocked.
Set WLED_DEBUG=1 to print the traceback when debugging why nothing happens.

De-dup: the last sent preset is cached in the temp dir; an identical repeat
(e.g. PreToolUse firing "working" on every tool call) is skipped, so the lamp
isn't spammed and the meaningful transitions stay responsive.
"""
import os
import sys
import json
import tempfile
import urllib.request

# Your WLED lamp IP. Override with the WLED_IP env var, or edit this line.
LAMP_IP = os.environ.get("WLED_IP", "192.168.1.50")

# status name -> WLED preset number (created by init_presets.py / presets.json)
PRESETS = {"working": 1, "attention": 2, "done": 3, "error": 5, "off": 0}

_STATE_FILE = os.path.join(tempfile.gettempdir(), "claude_lamp_last")


def _resolve(arg):
    preset = PRESETS.get(arg)
    if preset is None:
        try:
            preset = int(arg)          # bare integer preset id
        except ValueError:
            preset = 3                 # unknown -> done
    return preset if preset >= 0 else 3


def main():
    try:
        arg = (sys.argv[1] if len(sys.argv) > 1 else "done").strip().lower()
        preset = _resolve(arg)

        # de-dup: skip if this is the same preset we last sent
        try:
            with open(_STATE_FILE, "r") as f:
                if f.read().strip() == str(preset):
                    return
        except OSError:
            pass

        body = json.dumps({"ps": preset}).encode()
        req = urllib.request.Request(
            f"http://{LAMP_IP}/json/state",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)

        try:
            with open(_STATE_FILE, "w") as f:
                f.write(str(preset))
        except OSError:
            pass
    except Exception:
        if os.environ.get("WLED_DEBUG"):
            import traceback
            traceback.print_exc()
        # otherwise stay silent: lamp offline / wrong network must never block Claude


if __name__ == "__main__":
    main()
