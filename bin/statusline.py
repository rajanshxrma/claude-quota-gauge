#!/usr/bin/env python3
"""Combined statusLine, two lines: quota + session-title chip right-aligned
against it, then the workload-gauge segment + resume command right-aligned
against that.

Claude Code allows only one statusLine command, so this wraps rather than
replaces. It reads the Claude payload from stdin ONCE and hands it verbatim
to usage-statusline.py (which needs it for rate_limits/model; since 0.23.0 run
in this same process, see quota_line()), then appends the
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

A third line appears only while this session has a work progress bar set
(bin/work-progress.py -- see work_progress_line() in usage_common.py). The
same line-gap cost is why it isn't there otherwise: a session with no bar
pays one os.path.exists() for it and gets exactly the two lines above. It's
drawn in-process like the ultracode indicator, so it adds no subprocess.
Since 0.23.0 the row is live (clock in seconds, a fill creeping through the
running step, a pulse while the session or its agents are writing); the
payload's transcript_path is what lets the pulse see those writes.

A redraw never waits on the week's transcript scan (0.23.0): the quota line
reads the last finished scan and starts a fresh one detached when it is
older than 20 seconds -- see scan_totals() in usage_common.py. Before that,
the scan ran inside every redraw and hit its 5 second limit, so the status
line took over 5 seconds to draw and Claude Code cancelled most redraws.

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
    fmt_ultracode_styled,
    load_env_file,
    right_align,
    session_title,
    title_chip,
    title_changed_recently,
    title_disambiguation,
    ultracode_readiness,
    ultracode_state,
    work_progress_line,
)

load_env_file()  # the uc cost knobs live in the env file; subprocesses load it themselves

payload = sys.stdin.read()  # read once; the quota line consumes it, everything else doesn't


def start(cmd, stdin_text=None):
    """Starts a piece of the bar without waiting for it (0.23.0)."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return proc, stdin_text
    except Exception:
        return None, None


def finish(started):
    proc, stdin_text = started
    if proc is None:
        return ""
    try:
        out, _ = proc.communicate(input=stdin_text, timeout=10)
        return out.rstrip("\n")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return ""


try:
    parsed = json.loads(payload)
except Exception:
    parsed = {}
session_id = parsed.get("session_id")
transcript_path = parsed.get("transcript_path")

# --no-uc-segment: the ultracode indicator renders on the workload line
# below instead (styled, next to the swap marker -- per Rajan, 2026-08-08),
# so the quota line doesn't carry it twice.
def quota_line(payload_text):
    """usage-statusline.py's line, drawn in this process (0.23.0): it no
    longer waits on anything slow (the transcript count runs detached, see
    scan_totals()), so a separate interpreter only added its start-up time
    to every redraw. Loaded from its file, with its own argv, stdin and
    stdout for the length of the call; any error gives an empty line."""
    import importlib.util
    import io
    saved = sys.argv, sys.stdin, sys.stdout
    try:
        sys.argv = [USAGE, "--no-uc-segment"]
        sys.stdin, sys.stdout = io.StringIO(payload_text), io.StringIO()
        spec = importlib.util.spec_from_file_location("usage_statusline", USAGE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()
        return sys.stdout.getvalue().rstrip("\n")
    except BaseException:
        return ""
    finally:
        sys.argv, sys.stdin, sys.stdout = saved


# The workload segment is its own process; it starts first and draws while
# the quota line is worked out here, so a redraw costs the slower of the two.
wgauge_proc = start([sys.executable, WGAUGE, "--segment"])
usage_line = quota_line(payload)

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

seg = finish(wgauge_proc)

# Ultracode indicator, at the end of the workload segment next to the swap
# marker. The quota subprocess above already wrote this render's fresh cache
# (it runs first), so reading it back here can't be a stale double-read.
try:
    _now = datetime.now(timezone.utc)
    with open(os.path.join(HERE, "usage-live.json")) as _f:
        _cache = json.load(_f)
    uc = fmt_ultracode_styled(ultracode_state(_now), ultracode_readiness(_now, _cache), _now)
except Exception:
    uc = None
if uc:
    seg = f"{seg}  {uc}" if seg else uc

resume = f"\033[2m↳ claude --resume {session_id}\033[0m" if session_id else ""
if seg or resume:
    lines.append(right_align(seg, resume))

# Work progress bar: its own last line, only while this session has one set.
try:
    progress = work_progress_line(session_id, datetime.now(timezone.utc), transcript_path)
except Exception:
    progress = ""
if progress:
    lines.append(progress)

sys.stdout.write("\n".join(lines))
