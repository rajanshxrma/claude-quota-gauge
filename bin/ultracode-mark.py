#!/usr/bin/env python3
"""Flips the gauge's ultracode active-run marker. A session (or you) runs
`ultracode-mark.py on --reason "<task>"` right before launching a
multi-agent Workflow run and `ultracode-mark.py off` when it finishes; the
statusline segment (see fmt_ultracode in usage_common.py) reads the marker
every render, so the bar shows "uc: ON <elapsed>" the whole time a run is
live and goes back to the affordability verdict when it isn't.

The marker carries a TTL (CLAUDE_USAGE_UC_TTL_HOURS, default 4) judged
read-side, so a session that dies mid-run without ever marking off can't
leave the gauge lying forever -- see ultracode_state().

`status` prints the resolved state (active/idle + readiness verdict) as
JSON, for scripts or a quick manual check.
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
    parser.add_argument("--session-id", default="", help="optional owning session id, for later debugging of a stuck marker")
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
            "since": state["since"].isoformat() if state else None,
            "reason": state["reason"] if state else None,
            "readiness": ultracode_readiness(now, cache),
        }, indent=2))


if __name__ == "__main__":
    main()
