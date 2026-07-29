#!/usr/bin/env python3
"""Combined statusLine, two lines: quota + session-title chip right-aligned
against it, then the workload-gauge segment + resume command right-aligned
against that.

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

When that title collides with another currently-open session's title (e.g.
several `/afk` sessions all landing on Claude Code's own generic "AFK
pre-flight check"), the chip prefers a cached Fable-generated disambiguation
label instead, if one's fresh (title_disambiguation() -- see
title-collision-prompt-hook.py, the UserPromptSubmit hook that actually
triggers generation from a live turn, since spending against the tracked
Fable quota requires an Agent dispatch, not something this render-path
script can or should do itself). A cache miss (nothing generated yet, or
stale) falls back to the plain title exactly as before -- this lookup is a
local file read, so it can't add latency or block on Fable either way.

Down to two lines now (2026-07-29, per Rajan directly): resume moved off its
own solo line onto the gauge line, via the same right_align() the chip
already uses against the quota line. Two things drove this, both visible in
screenshots he sent: the visual gap between chip and resume when they sat on
separate lines, and a lone dim anchor dot appearing to float on its own row
before Claude Code's own "bypass permissions" chrome line beneath the bar --
Claude Code's statusline renderer (an Ink Box with a fixed `gap` prop between
children, found in the CLI's own source) inserts real vertical space between
every line this script emits, independent of what text is in those lines --
so the only lever available here is emitting fewer lines. Two lines, each
built from real content + `right_align()`, both needs no _SOLO_ANCHOR at all
(that dot only existed to keep a *solo* line's padding from being stripped
by Claude Code's per-line trim -- see usage_common.py -- and neither line
here is solo anymore).

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
    session_title,
    title_chip,
    title_changed_recently,
    title_disambiguation,
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

usage_line = run([sys.executable, USAGE], stdin_text=payload)

chip = ""
try:
    title = session_title(transcript_path, session_id)
    if title:
        changed = title_changed_recently(session_id, title, datetime.now(timezone.utc))
        display_title = title_disambiguation(session_id, title) or title
        chip = title_chip(display_title, session_id, changed=changed)
except Exception:
    chip = ""

lines = []
if usage_line or chip:
    lines.append(right_align(usage_line, chip))

seg = run([sys.executable, WGAUGE, "--segment"])
resume = f"\033[2m↳ claude --resume {session_id}\033[0m" if session_id else ""
if seg or resume:
    lines.append(right_align(seg, resume))

sys.stdout.write("\n".join(lines))
