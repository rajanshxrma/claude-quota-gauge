#!/usr/bin/env python3
"""PostToolUse hook on the Agent tool: whenever a dispatched agent explicitly
set model="fable", marks Fable as freshly used so the very next prompt
forces an immediate, silent gauge-calibrate -- tying the refresh to actual
Fable usage instead of the blind time/drift schedule in usage_common.py.
This fires right after the agent call returns, which is exactly the "end of
a Fable task" moment worth re-reading the settings page for.

Never blocks or slows the turn: any unexpected payload shape is swallowed
and the hook exits quietly with no output.
"""
import sys
import os
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import fable_mark_used  # noqa: E402


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    tool_input = payload.get("tool_input") or {}
    if tool_input.get("model") != "fable":
        return

    try:
        fable_mark_used(datetime.now(timezone.utc))
    except Exception:
        pass


if __name__ == "__main__":
    main()
