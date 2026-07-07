#!/bin/bash
# SessionStart hook: injects a Claude usage % snapshot into context automatically.
LINE=$(python3 ~/.claude/scripts/usage-estimate.py 2>/dev/null)

python3 - "$LINE" <<'PYEOF'
import json, sys
line = sys.argv[1]
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
