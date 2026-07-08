#!/usr/bin/env python3
"""Calibrates a per-model weekly % estimate (default: Fable) that Claude
Code's own `rate_limits` field doesn't expose -- Anthropic's real backend
reports one aggregate weekly %, not a per-model breakdown, even though
claude.ai/settings/usage itself shows a separate row for models with their
own pool (e.g. Fable). This is the one number this tool shows that isn't a
verified reported figure -- it's a local estimate scaled from token deltas,
so the statusline always labels it "(est.)" and reports staleness rather
than ever presenting it as fact.

Anchors the weekly window to Claude Code's real reported reset time (cached
by the last statusline render in usage-live.json) instead of a guessed
day/hour/timezone -- no separate reset config to get wrong.

Usage: usage-calibrate-fable.py <weekly_pct_from_claude_ai_settings_usage>
"""
import sys, os, json, subprocess
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import load_env_file  # noqa: E402

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")
CAL_PATH = os.path.join(SCRIPTS, "usage-fable-calibration.json")
TRACK_MODEL = os.environ.get("CLAUDE_USAGE_TRACK_MODEL", "fable")


def main():
    pct = float(sys.argv[1])

    if not os.path.exists(CACHE_PATH):
        print(
            "No cached rate_limits yet -- open a Claude Code session first so "
            "the statusline renders at least once, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(CACHE_PATH) as f:
        cache = json.load(f)

    resets_at = cache.get("seven_day_resets_at")
    if not resets_at:
        print(
            "No weekly resets_at cached yet -- open a Claude Code session "
            "first so the statusline renders at least once, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    next_reset = datetime.fromtimestamp(resets_at, tz=timezone.utc)
    window_start = next_reset - timedelta(days=7)

    tokens = json.loads(
        subprocess.check_output(
            [sys.executable, os.path.join(SCRIPTS, "tokens-since.py"), window_start.isoformat()]
        )
    )
    tracked_tokens = sum(v for k, v in tokens.items() if TRACK_MODEL.lower() in k.lower())

    cal = {
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "tracked_model": TRACK_MODEL,
        "pct": pct,
        "window_start": window_start.isoformat(),
        "next_reset": next_reset.isoformat(),
        "tokens_at_cal": tracked_tokens,
    }
    with open(CAL_PATH, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"Fable calibration written to {CAL_PATH}")
    print(json.dumps(cal, indent=2))


if __name__ == "__main__":
    main()
