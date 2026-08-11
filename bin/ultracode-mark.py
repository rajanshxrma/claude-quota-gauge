#!/usr/bin/env python3
"""Flips the gauge's ultracode active-run marker. A session (or you) runs
`ultracode-mark.py on --reason "<task>"` right before launching a
multi-agent Workflow run and `ultracode-mark.py off` when it finishes; the
statusline segment (see fmt_ultracode in usage_common.py) reads the marker
every render, so the bar shows "uc: ON <elapsed>" the whole time a run is
live and goes back to the affordability verdict when it isn't.

The marker file is machine-wide -- one shared quota pool, so a run started
in any session is visible to every session's statusline -- but `on` tags it
with the caller's own CLAUDE_CODE_SESSION_ID by default, and every reader
compares that against its own id to decide whether the run is "mine". Only
the owning session gets the loud "uc: ON" treatment and the "run off when
done" instruction; other sessions see a dim "elsewhere" note instead, so
turning off a run neither you nor the calling session actually started
requires --force (see `off` below).

The marker carries a TTL (CLAUDE_USAGE_UC_TTL_HOURS, default 4) judged
read-side, so a session that dies mid-run without ever marking off can't
leave the gauge lying forever -- see ultracode_state().

`status` prints the resolved state (active/idle, ownership, and readiness
verdict) as JSON, for scripts or a quick manual check.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import (  # noqa: E402
    UC_HISTORY_PATH, UC_STATE_PATH, load_env_file, ultracode_readiness, ultracode_state,
)

LIVE_CACHE_PATH = os.path.expanduser("~/.claude/scripts/usage-live.json")

# Max history rows kept in UC_HISTORY_PATH -- a rolling window, not a
# permanent log. ultracode_observed_cost() in usage_common.py only ever
# looks at the last handful (default up to 10) of these anyway, so keeping
# more than this is pure unused disk, not extra signal.
UC_HISTORY_MAX_ROWS = 20


def _read_cache():
    if not os.path.exists(LIVE_CACHE_PATH):
        return {}
    try:
        with open(LIVE_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _record_observed_cost(state, now):
    """Appends one real-cost sample to UC_HISTORY_PATH for the run that's
    ending, so ultracode_observed_cost() in usage_common.py has real data to
    median over instead of always falling back to the static per-pool
    default. Best-effort in every direction -- a missing/partial on-time
    snapshot, a missing/corrupt live cache, or a missing/corrupt history
    file all degrade gracefully (that pool's delta, or the whole record,
    just doesn't get recorded) rather than ever failing the `off` action
    that calls this. Same defensive posture as the rest of this codebase:
    an `off` that fails to flip the marker because of a bookkeeping hiccup
    would be far worse than an `off` that silently skips one history row."""
    try:
        cache = _read_cache()

        def delta_for(pct_key, resets_key, snap_pct_key, snap_resets_key):
            snap_pct = state.get(snap_pct_key)
            snap_resets = state.get(snap_resets_key)
            now_pct = cache.get(pct_key)
            now_resets = cache.get(resets_key)
            if snap_pct is None or now_pct is None:
                return None
            if snap_resets != now_resets:
                # A reset happened mid-run -- the raw delta would reflect the
                # rollover, not what the run actually cost. Unreliable, skip
                # just this pool rather than the whole record.
                return None
            return max(0.0, now_pct - snap_pct)

        tracked_model = state.get("tracked_model_at_on")
        record = {
            "since": state.get("since"),
            "ended_at": now.isoformat(),
            "reason": state.get("reason") or "",
            "five_hour_delta": delta_for(
                "five_hour_pct", "five_hour_resets_at",
                "five_hour_pct_at_on", "five_hour_resets_at_at_on",
            ),
            "seven_day_delta": delta_for(
                "seven_day_pct", "seven_day_resets_at",
                "seven_day_pct_at_on", "seven_day_resets_at_at_on",
            ),
            "tracked_delta": delta_for(
                "fable_pct", "fable_resets_at",
                "tracked_pct_at_on", "tracked_resets_at_at_on",
            ) if tracked_model else None,
            "tracked_model": tracked_model,
        }

        history = []
        if os.path.exists(UC_HISTORY_PATH):
            try:
                with open(UC_HISTORY_PATH) as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    history = loaded
            except Exception:
                history = []
        history.append(record)
        history = history[-UC_HISTORY_MAX_ROWS:]
        os.makedirs(os.path.dirname(UC_HISTORY_PATH), exist_ok=True)
        with open(UC_HISTORY_PATH, "w") as f:
            json.dump(history, f)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("--reason", default="", help="short label for what the run is doing (shown by the SessionStart hook)")
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        help="owning session id; defaults to this process's own CLAUDE_CODE_SESSION_ID "
             "(set by the Claude Code CLI on every subprocess it spawns) so a plain "
             "`on` already tags the marker correctly -- override only for manual testing",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="allow `off` to clear a marker owned by a different session "
             "(normally refused, since this session has no way to know "
             "whether that other run actually finished)",
    )
    args = parser.parse_args()

    load_env_file()
    now = datetime.now(timezone.utc)

    if args.action == "on":
        # Snapshot every pool present in the live cache at the moment the
        # run starts -- `off` diffs against this to record the run's real
        # observed cost (see _record_observed_cost() and
        # ultracode_observed_cost() in usage_common.py). Missing/partial
        # cache just means those keys come back None; that pool's delta
        # simply won't be recordable at `off` time, same defensive style as
        # everywhere else in this file.
        cache = _read_cache()
        os.makedirs(os.path.dirname(UC_STATE_PATH), exist_ok=True)
        with open(UC_STATE_PATH, "w") as f:
            json.dump({
                "active": True,
                "since": now.isoformat(),
                "reason": args.reason,
                "session_id": args.session_id,
                "five_hour_pct_at_on": cache.get("five_hour_pct"),
                "five_hour_resets_at_at_on": cache.get("five_hour_resets_at"),
                "seven_day_pct_at_on": cache.get("seven_day_pct"),
                "seven_day_resets_at_at_on": cache.get("seven_day_resets_at"),
                "tracked_model_at_on": cache.get("fable_tracked_model"),
                "tracked_pct_at_on": cache.get("fable_pct"),
                "tracked_resets_at_at_on": cache.get("fable_resets_at"),
            }, f)
        print(f"ultracode marked ON{' -- ' + args.reason if args.reason else ''}")
    elif args.action == "off":
        # Written as inactive rather than deleted so the file keeps the last
        # run's trace (since/reason) for a quick post-hoc look.
        state = {}
        if os.path.exists(UC_STATE_PATH):
            try:
                with open(UC_STATE_PATH) as f:
                    state = json.load(f)
            except Exception:
                state = {}
        marker_session = state.get("session_id") or ""
        my_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if (state.get("active") and marker_session and marker_session != my_session
                and not args.force):
            print(
                f"refusing: this marker belongs to a different session "
                f"({marker_session}), not this one ({my_session or 'unknown'}) -- "
                f"pass --force if you've confirmed that run actually finished",
                file=sys.stderr,
            )
            sys.exit(1)
        # Only on a successful, non-refused off: record this run's real
        # observed cost before the on-time snapshot fields are (implicitly)
        # superseded by the next `on`.
        _record_observed_cost(state, now)
        state["active"] = False
        state["ended_at"] = now.isoformat()
        with open(UC_STATE_PATH, "w") as f:
            json.dump(state, f)
        print("ultracode marked off")
    else:
        cache = {}
        if os.path.exists(LIVE_CACHE_PATH):
            try:
                with open(LIVE_CACHE_PATH) as f:
                    cache = json.load(f)
            except Exception:
                cache = {}
        state = ultracode_state(now)
        print(json.dumps({
            "active": bool(state),
            "mine": state["mine"] if state else None,
            "since": state["since"].isoformat() if state else None,
            "reason": state["reason"] if state else None,
            "readiness": ultracode_readiness(now, cache),
        }, indent=2))


if __name__ == "__main__":
    main()
