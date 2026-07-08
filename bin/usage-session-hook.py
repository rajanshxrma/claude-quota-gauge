#!/usr/bin/env python3
"""SessionStart hook: injects the real usage % into context, read from the
cache the statusline command wrote on its last render. There's nothing to
calibrate here -- the numbers came straight from Claude Code's own
rate_limits data (Anthropic's real backend figures), so this is purely
informational, not an instruction to go fetch anything.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import fmt_delta  # noqa: E402

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")


def main():
    now = datetime.now(timezone.utc)

    if not os.path.exists(CACHE_PATH):
        context = (
            "Claude usage data isn't cached yet. It's written by the statusline "
            "renderer from Claude Code's own rate_limits field (real Anthropic "
            "data, refreshed automatically) -- it'll appear within about a "
            "minute of any Claude Code session being open, no action needed."
        )
    else:
        with open(CACHE_PATH) as f:
            cache = json.load(f)

        parts = []
        if "five_hour_pct" in cache:
            resets = fmt_delta(cache.get("five_hour_resets_at"), now)
            parts.append(f"5h: {cache['five_hour_pct']:.0f}%" + (f" (resets {resets})" if resets else ""))
        if "seven_day_pct" in cache:
            resets = fmt_delta(cache.get("seven_day_resets_at"), now)
            parts.append(f"week: {cache['seven_day_pct']:.0f}%" + (f" (resets {resets})" if resets else ""))

        line = " | ".join(parts) if parts else "no rate limit data cached yet"
        context = f"Claude usage (real, from Claude Code's own rate_limits -- not estimated): {line}"

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }))


if __name__ == "__main__":
    main()
