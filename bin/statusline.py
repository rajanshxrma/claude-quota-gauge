#!/usr/bin/env python3
"""Combined statusLine: the existing claude-quota-gauge line, plus a compact
workload-gauge segment on a second line.

Claude Code allows only one statusLine command, so this wraps rather than
replaces. It reads the Claude payload from stdin ONCE and forwards it verbatim
to usage-statusline.py (which needs it for rate_limits/model), then appends the
workload segment. The workload part reads a cache instantly and never samples,
so this wrapper adds no measurable latency to a render -- see
workload-gauge.py's cache plumbing for how freshness is kept without lag.

If either child errors, its line is simply omitted rather than breaking the
whole statusline.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE = os.path.join(HERE, "usage-statusline.py")
WGAUGE = os.path.join(HERE, "workload-gauge.py")

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
    lines.append(seg)

sys.stdout.write("\n".join(lines))
