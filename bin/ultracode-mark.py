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
    UC_STATE_PATH, load_env_file, ultracode_readiness, ultracode_state,
)

LIVE_CACHE_PATH = os.path.expanduser("~/.claude/scripts/usage-live.json")


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
        os.makedirs(os.path.dirname(UC_STATE_PATH), exist_ok=True)
        with open(UC_STATE_PATH, "w") as f:
            json.dump({
                "active": True,
                "since": now.isoformat(),
                "reason": args.reason,
                "session_id": args.session_id,
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
