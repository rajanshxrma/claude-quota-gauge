#!/usr/bin/env python3
"""Combined statusLine: the existing claude-quota-gauge line, plus a compact
workload-gauge segment on a second line -- which also carries this session's
full resume command, right-aligned.

Claude Code allows only one statusLine command, so this wraps rather than
replaces. It reads the Claude payload from stdin ONCE and forwards it verbatim
to usage-statusline.py (which needs it for rate_limits/model), then appends the
workload segment. The workload part reads a cache instantly and never samples,
so this wrapper adds no measurable latency to a render -- see
workload-gauge.py's cache plumbing for how freshness is kept without lag.

The full `claude --resume <uuid>` command (the exact text to resume this
session in a fresh terminal if this one hits a usage limit) used to trail
line 1 as a bare "session: <id>". The whole command reads much longer than
that, and line 1 is already the dense one (model + three usage windows), so
it's right-aligned onto line 2 instead -- the workload segment rarely runs
long enough to crowd it out. Uses the same right_align() as line 1 used to,
which measures visible width (ANSI-stripped) so it lines up correctly against
the workload segment's colored text -- see visible_len() in usage_common.py.

If either child errors, its line is simply omitted rather than breaking the
whole statusline.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE = os.path.join(HERE, "usage-statusline.py")
WGAUGE = os.path.join(HERE, "workload-gauge.py")

sys.path.insert(0, HERE)
from usage_common import right_align  # noqa: E402

payload = sys.stdin.read()  # read once; the quota line consumes it, segment doesn't


def run(cmd, stdin_text=None):
    try:
        r = subprocess.run(cmd, input=stdin_text, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.rstrip("\n")
    except Exception:
        return ""


lines = []
usage_line = run([sys.executable, USAGE], stdin_text=payload)
if usage_line:
    lines.append(usage_line)

seg = run([sys.executable, WGAUGE, "--segment"])
if seg:
    try:
        session_id = json.loads(payload).get("session_id")
    except Exception:
        session_id = None
    resume = f"claude --resume {session_id}" if session_id else ""
    lines.append(right_align(seg, resume))

sys.stdout.write("\n".join(lines))
