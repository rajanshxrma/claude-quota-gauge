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
from usage_common import fmt_tokens, fmt_window, load_env_file, ultracode_context, ultracode_readiness, ultracode_state  # noqa: E402

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")


def read_payload():
    """Claude Code always pipes the hook payload on stdin and closes it, so
    a plain read is right there. But this hook also gets run by hand and by
    tests with nothing on stdin, and a blocking read on an open, writer-less
    pipe hangs forever -- so only read when data is actually waiting, and
    treat anything else as an empty payload (the pre-0.20.0 behavior)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        if not ready:
            return {}
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resume_note(payload):
    """One clause on what resuming this session costs (Claude Code >=
    2.1.251 passes it to SessionStart hooks on a resume): why the prompt
    cache was invalidated and how many tokens get re-cached on the next
    turn. Silent for a fresh session, an older CLI, or a resume whose cache
    is still intact -- "none" is not worth a sentence."""
    if not isinstance(payload, dict):
        return None
    reason_field = payload.get("session_start_reason") or payload.get("source")
    if reason_field != "resume":
        return None
    tokens = payload.get("re_cache_cost_tokens")
    why = payload.get("cache_invalidation_reason")
    if not tokens or why in (None, "none"):
        return None
    note = f"resumed with a cold prompt cache ({why}): ~{fmt_tokens(tokens)} tokens re-cache on the next turn"
    usd = payload.get("re_cache_cost_usd")
    if isinstance(usd, (int, float)) and usd > 0:
        note += f" (~${usd:.2f} at API pricing)"
    return note


def main():
    now = datetime.now(timezone.utc)
    payload = read_payload()

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
        if "spend_limit_pct" in cache:
            parts.append(fmt_window("spend", cache["spend_limit_pct"], cache.get("spend_limit_resets_at"), now))

        line = " | ".join(parts) if parts else "no rate limit data cached yet"
        context = f"Claude usage (real, from Claude Code's own rate_limits -- not estimated): {line}"

        model = cache.get("fable_tracked_model")
        if model and cache.get("fable_stale"):
            context += (
                f" | {model} weekly estimate is stale -- run /gauge-calibrate "
                f"now, silently (write the calibration, don't announce the "
                f"number in chat unless the user explicitly asked about usage)."
            )
        elif model and "fable_pct" in cache:
            context += " | " + fmt_window(f"{model} weekly", cache["fable_pct"], cache.get("fable_resets_at"), now)

        # Ultracode awareness: active-run marker + affordability verdict,
        # and (only when the machine's owner set CLAUDE_USAGE_UC_AUTO=1 in
        # their claude-quota-gauge.env) the standing directive that lets a
        # session start a Workflow run on its own judgment. See
        # ultracode_context() in usage_common.py for the exact wording.
        uc_line = ultracode_context(ultracode_state(now), ultracode_readiness(now, cache), now)
        if uc_line:
            context += " | " + uc_line

    note = resume_note(payload)
    if note:
        context += " | " + note

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }))


if __name__ == "__main__":
    main()
