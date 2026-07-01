#!/usr/bin/env python3
"""Claude Code Stop hook: flag the lamp amber when the turn ended on a question.

Claude Code has no distinct "the assistant asked you a question" event — a
question and a finished/idle turn both surface only as the ~60s idle_prompt,
which the lamp maps to green ("done"). So "please answer me" and "all done"
look identical, and a question never turns the lamp amber.

This hook closes that gap. On Stop it reads the last assistant message from the
transcript and, if it looks like a question (ends with "?", or used the
AskUserQuestion tool), records this session as "attention" (amber) so the lamp
says "this session needs you". The amber latches until you reply (UserPromptSubmit
-> working) or the session's process exits (the daemon reaps it).

If the turn did NOT end on a question it does nothing — green/idle keep coming
from idle_prompt and the freshness window, so a plain turn-end (including one
that just launched a background subagent) never gets a false amber or green from
here. The question test is deliberately dumb (a trailing "?"), which is enough.

Wire as a Stop hook (async), alongside any others:
  py -3.14 /path/to/lamp_ask_detect.py

Fail-silent: any error is swallowed so Claude is never blocked. WLED_DEBUG=1
prints tracebacks.
"""
import json
import os
import subprocess
import sys

_TAIL = 512 * 1024  # only the transcript's tail holds the last message


def _lamp_script(here):
    """The lamp writer next to this hook (prefer the real local config)."""
    for name in ("lamp_status.local.py", "lamp_status.py"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    return None


def _last_assistant(transcript_path):
    """(text, tool_names) of the last assistant message in the JSONL transcript."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "", []
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)  # a partial first line just fails and is skipped
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            return content, []
        texts, tools = [], []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(b.get("text") or "")
                elif b.get("type") == "tool_use":
                    tools.append(b.get("name") or "")
        return "\n".join(texts), tools
    return "", []


_TRIM = ")]}>»\"'*`~ \t\r\n"  # trailing closers/markdown to look past a final "?"


def _is_question(text, tools):
    if any(t == "AskUserQuestion" for t in tools):
        return True
    if text.rstrip().rstrip(_TRIM).endswith("?"):
        return True
    for line in reversed(text.splitlines()):  # last non-empty line ends with "?"
        line = line.rstrip(_TRIM)
        if line:
            return line.endswith("?")
    return False


def main():
    try:
        raw = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8-sig", "replace")) if raw else {}
        tpath = data.get("transcript_path")
        if not tpath:
            return
        text, tools = _last_assistant(tpath)
        if not _is_question(text, tools):
            return  # not a question -> leave done/idle to idle_prompt / the window
        lamp = _lamp_script(os.path.dirname(os.path.abspath(__file__)))
        if not lamp:
            return
        subprocess.run(
            [sys.executable, lamp, "attention", "--write-only", "--tool", "claude"],
            input=json.dumps({"session_id": data.get("session_id") or "default"}).encode("utf-8"),
            timeout=4,
        )
    except Exception:
        if os.environ.get("WLED_DEBUG"):
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
