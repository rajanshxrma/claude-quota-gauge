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

    now = datetime.now(timezone.utc)
    next_reset = datetime.fromtimestamp(resets_at, tz=timezone.utc)
    # The cached resets_at is only ever as fresh as the last statusline
    # render, so it can name a boundary that has already passed -- found live
    # 2026-09-05: a calibration at 23:07Z anchored to a resets_at of 09:00Z
    # that same morning, i.e. a window that had already rolled over. Taken at
    # face value that puts window_start a full week early, so tokens_at_cal
    # sums an extra week of usage and the cap derived from it comes out
    # inflated by that same ratio; fable_estimate() then measures the *current*
    # window's usage against that inflated cap and under-reports badly (a real
    # 81% read rendered as 18%). Step forward in 7-day increments to the real
    # upcoming boundary -- the same way fable_estimate() advances a window of
    # its own when it has no live resets_at to lean on.
    while now >= next_reset:
        next_reset += timedelta(days=7)
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
    prior_pct, prior_tokens = None, None
    if os.path.exists(CAL_PATH):
        try:
            with open(CAL_PATH) as f:
                prior = json.load(f)
            prior_cap = prior.get("cap")
            prior_cap_derived_at = prior.get("cap_derived_at")
            prior_window_start = prior.get("window_start")
            prior_pct = prior.get("pct")
            prior_tokens = prior.get("tokens_at_cal")
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
        # Guard against blending in a stale carried-forward cap: the 0%/
        # zero-tracked-tokens branch below can carry an OLD window's cap
        # forward while still stamping the CURRENT window_start onto the
        # file (so window/reset bookkeeping stays current even when the cap
        # itself couldn't be re-derived that time). That makes
        # `prior_window_start == window_start` true even though no real
        # calibration happened in this window -- found live 2026-08-24: a
        # 430-cap carried forward from the prior week got blended 70% into
        # a fresh 14%-based ~17 raw_cap, producing an inflated ~220 cap that
        # then made fable_estimate() (tracked_now/cap*100) under-report 14%
        # as 1.1%. Fix: only trust prior_cap for blending if it was
        # *actually derived* inside the current window, not merely labeled
        # with it -- check prior_cap_derived_at falls in [window_start,
        # next_reset), not just the window_start string match.
        prior_cap_valid_for_blend = False
        if prior_cap and prior_cap_derived_at:
            try:
                prior_derived_dt = datetime.fromisoformat(prior_cap_derived_at)
                prior_cap_valid_for_blend = (
                    prior_window_start == window_start.isoformat()
                    and window_start <= prior_derived_dt < next_reset
                )
            except Exception:
                prior_cap_valid_for_blend = False
        # ANCHOR MODEL (2026-09-19). The old model showed
        # `tracked_now / cap` and blended each new cap 70/30 with the prior
        # one, so a fresh, true reading barely moved the display: told 80%,
        # it kept showing 88-97% (raw cap 744, blended cap 651). His words:
        # "whenever I talk about the gauge being wrong the new updated
        # number to be displayed and the calculation model behind our gauge
        # fixed." A calibration is ground truth, so it now sets the LEVEL
        # exactly (see usage_common.fable_estimate: pct_at_cal + growth
        # since), and `cap` only sets the SLOPE -- units of local usage per
        # 100% -- for what accrues afterwards. The slope prefers two real
        # readings in this window (what the pool actually charged between
        # them) over one reading divided by everything since the window
        # opened, because local accounting misses off-CLI use and weights
        # cache reads differently from the backend, which is why the raw
        # cap wanders from read to read. Smoothing the slope is safe now:
        # the level no longer depends on it.
        slope = raw_cap
        if (prior_cap_valid_for_blend and prior_pct is not None and prior_tokens is not None
                and pct - float(prior_pct) >= 5 and tracked_tokens > float(prior_tokens)):
            two_point = (tracked_tokens - float(prior_tokens)) / ((pct - float(prior_pct)) / 100)
            two_point = max(0.5 * raw_cap, min(2.0 * raw_cap, two_point))
            slope = 0.5 * two_point + 0.5 * raw_cap
        cal["cap"] = slope
        cal["cap_derived_at"] = now.isoformat()
        cal["model"] = "anchor-v2"
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
    # The display reads usage-live.json, which only refreshes on the next
    # statusline render -- so a correction used to leave the old number on
    # screen. Write the true reading through now.
    try:
        cache["fable_pct"] = pct
        cache["fable_stale"] = False
        cache["fable_tracked_model"] = TRACK_MODEL
        cache["fable_resets_at"] = int(next_reset.timestamp())
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass
    print(f"Fable calibration written to {CAL_PATH}")
    print(json.dumps(cal, indent=2))


if __name__ == "__main__":
    main()
