#!/usr/bin/env python3
"""SessionStart hook: injects the usage % into context, read from the cache
the statusline command wrote on its last render. The 5h/weekly numbers came
straight from Claude Code's own rate_limits data (Anthropic's real backend
figures) -- purely informational, nothing to fetch. The optional per-model
line (e.g. fable), if present, is flagged as stale (with the recalibration
command named) when it needs a fresh read of claude.ai/settings/usage --
reading that page has no side effects, so recalibrate immediately when
stale rather than waiting to be asked.
"""
import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import fmt_window  # noqa: E402

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
            parts.append(fmt_window("5h", cache["five_hour_pct"], cache.get("five_hour_resets_at"), now))
        if "seven_day_pct" in cache:
            parts.append(fmt_window("week", cache["seven_day_pct"], cache.get("seven_day_resets_at"), now))

        line = " | ".join(parts) if parts else "no rate limit data cached yet"
        context = f"Claude usage (real, from Claude Code's own rate_limits -- not estimated): {line}"

        model = cache.get("fable_tracked_model")
        if model and cache.get("fable_stale"):
            context += (
                f" | {model} weekly estimate is stale -- read the real % off "
                f"claude.ai/settings/usage and run `python3 ~/.claude/scripts/"
                f"usage-calibrate-fable.py <pct>` now, no need to ask first "
                f"(viewing that page has no side effects)."
            )
        elif model and "fable_pct" in cache:
            context += " | " + fmt_window(f"{model} weekly", cache["fable_pct"], cache.get("fable_resets_at"), now)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }))


if __name__ == "__main__":
    main()
