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
        # All-models local cost this window, paired with the aggregate %
        # above: together they let the drift tripwire in fable_estimate()
        # estimate the aggregate pool's cap and subtract locally-explained
        # aggregate movement, so only *unexplained* movement trips staleness.
        "local_total_at_cal": sum(tokens.values()),
    }

    prior_cap, prior_cap_derived_at, prior_window_start = None, None, None
    if os.path.exists(CAL_PATH):
        try:
            with open(CAL_PATH) as f:
                prior = json.load(f)
            prior_cap = prior.get("cap")
            prior_cap_derived_at = prior.get("cap_derived_at")
            prior_window_start = prior.get("window_start")
        except Exception:
            pass

    if pct > 0 and tracked_tokens > 0:
        # A real non-zero read *and* a nonzero local denominator -- derive a
        # fresh raw cap from this sample. The true weekly cap is a fixed
        # constant (the plan's real budget); each calibration is just a
        # noisy independent estimate of it, so blend with the prior
        # same-window cap (EMA, 70% prior / 30% new) instead of overwriting
        # outright -- a single noisy sample otherwise swings the live %
        # wildly. Found live (2026-08-06): recalibrating immediately after a
        # Fable subagent dispatch (see fable-agent-posttooluse-hook.py)
        # counts the fresh local tokens before claude.ai's server-side %
        # has caught up to reflect that same dispatch, so the raw sample
        # systematically overshoots right after a dispatch -- a 150->239
        # cap swing (59%) inside 17 minutes was observed with three
        # concurrent local sessions running Fable-backed skills. Smoothing
        # doesn't fix the lag itself, but stops one overshoot from being
        # trusted outright; it converges back over a few calibrations if
        # the true cap really did change (e.g. a plan upgrade), and resets
        # to trusting the raw sample once the window rolls over (a new
        # week has nothing prior to blend against).
        raw_cap = tracked_tokens / (pct / 100)
        if prior_cap and prior_window_start == window_start.isoformat():
            cal["cap"] = 0.7 * prior_cap + 0.3 * raw_cap
        else:
            cal["cap"] = raw_cap
        cal["cap_derived_at"] = now.isoformat()
    else:
        # Can't derive a trustworthy cap here -- either a 0% read (no
        # numerator), or a nonzero real % with zero locally-tracked tokens
        # this window (found live, 2026-07-30: happens whenever this week's
        # real Fable usage ran entirely off-CLI -- web/mobile, or background
        # routines before local tracking picked anything up). Dividing by a
        # zero tracked_tokens in that second case would silently produce a
        # cap of exactly 0.0, which fable_estimate() then either treats as
        # "never calibrated" (0.0 is falsy) or, if tracked_tokens ticks up
        # to even 1 unit right after, projects straight through 85%/95%
        # toward the 120% ceiling off a single trivial local ping -- the
        # false "hit 95%" alert this was chasing. Carry forward whatever cap
        # is already on file (if any) instead of writing a degenerate one.
        cal["cap"] = prior_cap
        cal["cap_derived_at"] = prior_cap_derived_at

    with open(CAL_PATH, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"Fable calibration written to {CAL_PATH}")
    print(json.dumps(cal, indent=2))


if __name__ == "__main__":
    main()
