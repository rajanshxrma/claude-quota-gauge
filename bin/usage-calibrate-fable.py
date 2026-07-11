#!/usr/bin/env python3
"""Calibrates a per-model weekly % (default: Fable) that Claude Code's own
`rate_limits` field doesn't expose -- Anthropic's real backend reports one
aggregate weekly %, not a per-model breakdown, even though
claude.ai/settings/usage itself shows a separate row for models with their
own pool (e.g. Fable).

Absolute-cap model (2026-07-10): rather than remembering this % and scaling
it by a token ratio on every read (the old model -- see CHANGELOG for why
that froze at 0% and needed constant re-anchoring), this derives a weekly
$ cap in the same cost-weighted units tokens-since.py already produces:

    cap = tokens_at_cal / (pct / 100)

Once a cap exists, usage_common.fable_estimate() projects it against live
local usage on every read -- no further calibration needed except to
occasionally re-verify the cap hasn't drifted (see CAP_MAX_AGE), and the
weekly window advances on its own at the real reset boundary with no
browser read needed.

A read of exactly 0% can't derive a cap (division by zero -- there's
nothing used yet to calibrate a denominator against), so a 0% calibration
updates the window/reset bookkeeping but deliberately keeps whatever cap
was already on file, rather than discarding a good cap just because this
particular read happened to land at zero.

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
    # The real, verified aggregate weekly % at the moment of this
    # calibration -- the tripwire in fable_estimate() compares this against
    # the *current* real aggregate on every later read. A big gap between
    # them means account-wide usage has moved in a way the local-token
    # projection may not have seen (e.g. the tracked model used outside this
    # CLI), so that's the signal used to force a re-read rather than trust a
    # stale local-only projection indefinitely.
    seven_day_pct_at_cal = cache.get("seven_day_pct")

    tokens = json.loads(
        subprocess.check_output(
            [sys.executable, os.path.join(SCRIPTS, "tokens-since.py"), window_start.isoformat()]
        )
    )
    tracked_tokens = sum(v for k, v in tokens.items() if TRACK_MODEL.lower() in k.lower())

    now = datetime.now(timezone.utc)
    cal = {
        "calibrated_at": now.isoformat(),
        "tracked_model": TRACK_MODEL,
        "pct": pct,
        "window_start": window_start.isoformat(),
        "next_reset": next_reset.isoformat(),
        "tokens_at_cal": tracked_tokens,
        "seven_day_pct_at_cal": seven_day_pct_at_cal,
    }

    if pct > 0:
        # A real non-zero read gives an actual denominator -- derive
        # (or refresh, if re-verifying) the weekly cap from it.
        cal["cap"] = tracked_tokens / (pct / 100)
        cal["cap_derived_at"] = now.isoformat()
    else:
        # Can't derive a cap from a 0% read -- carry forward whatever cap
        # is already on file (if any) instead of discarding it.
        prior_cap, prior_cap_derived_at = None, None
        if os.path.exists(CAL_PATH):
            try:
                with open(CAL_PATH) as f:
                    prior = json.load(f)
                prior_cap = prior.get("cap")
                prior_cap_derived_at = prior.get("cap_derived_at")
            except Exception:
                pass
        cal["cap"] = prior_cap
        cal["cap_derived_at"] = prior_cap_derived_at

    with open(CAL_PATH, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"Fable calibration written to {CAL_PATH}")
    print(json.dumps(cal, indent=2))


if __name__ == "__main__":
    main()
