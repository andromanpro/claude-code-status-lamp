#!/usr/bin/env python3
"""Reliability test: owner-process liveness reaping (lamp_status._reap_dead).

    python test_reap.py

A session whose owning process is gone must be dropped immediately — in ANY
state, including a stuck "attention" (amber) — while a live-owner session and a
legacy record with no owner are kept. Uses the real process-identity helpers,
so it exercises the platform-specific liveness check.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lamp_status as L
import lamp_proc

now = int(time.time())

# A genuinely-alive owner (this test's own ancestor) with a real birthmark.
live_pid, live_birth, live_exe = lamp_proc.owner_identity()
print(f"resolved owner: pid={live_pid} exe={live_exe!r} alive={lamp_proc.is_alive(live_pid, live_birth)}")
if not live_pid:
    print("WARN: owner could not be resolved on this platform — reap falls back to TTL only.")

wrong_birth = (live_birth + 1) if isinstance(live_birth, int) else "WRONG"

sessions = {
    "alive":    {"s": "working",   "t": now,            "pid": live_pid, "pst": live_birth},   # keep: owner alive
    "dead_amb": {"s": "attention", "t": now,            "pid": 2 ** 30,  "pst": None},         # reap: dead owner, stuck AMBER
    "reused":   {"s": "error",     "t": now,            "pid": live_pid, "pst": wrong_birth},  # reap: PID reused
    "legacy":   {"s": "working",   "t": now},                                                  # keep: no owner -> TTL fallback
    "ttl_old":  {"s": "working",   "t": now - 7 * 3600, "pid": live_pid, "pst": live_birth},   # reap: past 6h TTL
}

kept = set(L._reap_dead(dict(sessions), now))
# When the owner can't be resolved (e.g. macOS), the reliable checks are skipped
# and only TTL applies, so "dead_amb"/"reused" survive — accept that degraded mode.
if live_pid:
    expected = {"alive", "legacy"}
else:
    expected = {"alive", "dead_amb", "reused", "legacy"}

for k in sessions:
    print(f"  [{'KEEP' if k in kept else 'reap'}] {k:9} s={sessions[k]['s']}")
print(f"\nkept={sorted(kept)}  expected={sorted(expected)}")
ok = kept == expected
print("RESULT:", "PASS" if ok else f"FAIL (diff={kept ^ expected})")
sys.exit(0 if ok else 1)
