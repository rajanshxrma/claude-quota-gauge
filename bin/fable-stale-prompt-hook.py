#!/usr/bin/env python3
"""UserPromptSubmit hook: catches the tracked-model (Fable) weekly estimate
going stale *mid-session*, not just at the next session's launch. The
SessionStart hook (usage-session-hook.py) only fires once, at a session's
own start -- a long-running session that crosses the staleness threshold
(CAP_MAX_AGE or FABLE_DRIFT_THRESHOLD) partway through would otherwise sit
stale for the rest of that session with nothing prompting a fix. This
re-checks on every prompt so the very next message after it trips gets the
same "recalibrate now, no need to ask" nudge -- dedup'd per session against
the calibration's own identity (see fable_stale_to_announce() in
usage_common.py) so it's surfaced once per distinct stale episode, not
spammed every message. Same shape as theme-watch-prompt-hook.py, which
solves the identical "mid-session, not just at launch" problem for the
(unrelated) UI-theme-drift case.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import fable_stale_to_announce  # noqa: E402


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session_id = payload.get("session_id")
    now = datetime.now(timezone.utc)

    model = fable_stale_to_announce(session_id, now)
    if not model:
        return

    context = (
        f"{model} weekly estimate just went stale mid-session -- run "
        f"/gauge-calibrate now, no need to ask first (viewing that settings "
        f"page has no side effects)."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }))


if __name__ == "__main__":
    main()
