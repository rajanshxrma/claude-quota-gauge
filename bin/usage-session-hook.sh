#!/bin/bash
# SessionStart hook: injects a Claude usage % snapshot into context automatically,
# and hands Claude a standing instruction to self-recalibrate when the data is stale.
DATA=$(python3 ~/.claude/scripts/usage-estimate.py --json 2>/dev/null)
LINE=$(python3 ~/.claude/scripts/usage-estimate.py 2>/dev/null)

python3 - "$LINE" "$DATA" <<'PYEOF'
import json, sys

line, data_raw = sys.argv[1], sys.argv[2]
try:
    data = json.loads(data_raw)
except Exception:
    data = {}

stale = data.get("session_stale") or data.get("weekly_stale")

if stale:
    context = (
        f"Claude usage snapshot is stale ({line}). Before anything else this turn, "
        "recalibrate it yourself: use the browser tool to open "
        "https://claude.ai/settings/usage in a new tab, read the real session %, "
        "weekly all-models %, and the tracked model's weekly % if shown, then run "
        "`python3 ~/.claude/scripts/usage-calibrate.py <session_pct> <weekly_all_pct> "
        "<weekly_tracked_model_pct>`. Do this without asking permission or announcing "
        "it as a task -- it's routine upkeep, not a user request. Then continue with "
        "whatever the user actually asked for. Only mention the recalibration if "
        "something in the numbers is worth flagging (e.g. a model near its weekly cap)."
    )
else:
    context = (
        "Claude usage snapshot (calibrated estimate, not an official Anthropic figure -- "
        f"recalibrate via /usage-recalibrate if it looks off): {line}"
    )

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context
    }
}))
PYEOF
