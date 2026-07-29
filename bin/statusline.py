#!/usr/bin/env python3
"""Combined statusLine: the existing claude-quota-gauge line, a compact
workload-gauge segment on a second line with the session-title chip
right-aligned against it, and the session's full resume command on a third
line, right-aligned below that.

Claude Code allows only one statusLine command, so this wraps rather than
replaces. It reads the Claude payload from stdin ONCE and forwards it verbatim
to usage-statusline.py (which needs it for rate_limits/model), then appends the
workload segment. The workload part reads a cache instantly and never samples,
so this wrapper adds no measurable latency to a render -- see
workload-gauge.py's cache plumbing for how freshness is kept without lag.

The session-title chip reproduces the colored title block Claude Code's own
UI shows intermittently above the statusline, but renders it here on every
single render instead -- see session_title()/title_chip() in usage_common.py
for where the title comes from (a tail-read of the transcript Claude Code
already writes, no LLM call) and why the chip stays legible in both light and
dark terminal themes.

The full `claude --resume <uuid>` command (the exact text to resume this
session in a fresh terminal if this one hits a usage limit) used to trail
line 1 as a bare "session: <id>", then moved to trail line 2 -- both times
crowding out something else on an already-dense line. It now gets its own
third line, right-aligned with right_align_solo() so the right-hand edge
still reads as one consistent column top to bottom.

If any piece errors, its line/segment is simply omitted rather than breaking
the whole statusline.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE = os.path.join(HERE, "usage-statusline.py")
WGAUGE = os.path.join(HERE, "workload-gauge.py")

sys.path.insert(0, HERE)
from usage_common import (  # noqa: E402
    right_align,
    right_align_solo,
    session_title,
    title_chip,
    title_changed_recently,
)

payload = sys.stdin.read()  # read once; the quota line consumes it, everything else doesn't


def run(cmd, stdin_text=None):
    try:
        r = subprocess.run(cmd, input=stdin_text, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.rstrip("\n")
    except Exception:
        return ""


try:
    parsed = json.loads(payload)
except Exception:
    parsed = {}
session_id = parsed.get("session_id")
transcript_path = parsed.get("transcript_path")

lines = []
usage_line = run([sys.executable, USAGE], stdin_text=payload)
if usage_line:
    lines.append(usage_line)

seg = run([sys.executable, WGAUGE, "--segment"])

chip = ""
try:
    title = session_title(transcript_path, session_id)
    if title:
        changed = title_changed_recently(session_id, title, datetime.now(timezone.utc))
        chip = title_chip(title, session_id, changed=changed)
except Exception:
    chip = ""

if seg or chip:
    lines.append(right_align(seg, chip))

resume = f"claude --resume {session_id}" if session_id else ""
if resume:
    lines.append(right_align_solo(resume))

sys.stdout.write("\n".join(lines))
