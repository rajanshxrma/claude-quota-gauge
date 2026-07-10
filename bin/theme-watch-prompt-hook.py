#!/usr/bin/env python3
"""UserPromptSubmit hook: entirely a background check, never surfaced in the
visible statusline bar. Claude Code's UI theme resolves once at session
launch and is never hot-reloaded from settings.json or re-applied by any
external signal -- the only supported mid-session fix is running
`/config theme=auto` yourself. Since SessionStart context is injected only
once, this hook re-checks on every prompt (opt-in, macOS-only -- see
CLAUDE_USAGE_THEME_WATCH in usage_common.py) so Claude notices a drift
shortly after it happens and can proactively flag it, instead of the
mismatch sitting silently unnoticed for the rest of the session.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import theme_drift_to_announce  # noqa: E402


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session_id = payload.get("session_id")
    now = datetime.now(timezone.utc)

    drift = theme_drift_to_announce(session_id, now)
    if not drift:
        return

    context = (
        f"OS appearance changed to {drift['to']} since this session launched "
        f"(was {drift['from']} at launch, and Claude Code's theme only resolves "
        f"once at startup -- it will not follow this on its own). Mention that "
        f"the terminal colors are stale and `/config theme=auto` re-syncs "
        f"immediately, no restart needed."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }))


if __name__ == "__main__":
    main()
