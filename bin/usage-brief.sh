#!/bin/bash
# Summarizes Claude Code usage (current 5h block + this week) for injection into session context.
# Data source: local session transcripts under ~/.claude/projects, parsed by ccusage.
# NOTE: Anthropic does not publish exact Max-plan quota numbers, so any "remaining" figure
# below is a rough estimate based on this machine's own historical usage, not an official cap.

if [ -x "$HOME/.npm-global/bin/ccusage" ]; then
  CCUSAGE="$HOME/.npm-global/bin/ccusage"
else
  CCUSAGE=$(command -v ccusage || echo "npx ccusage@latest")
fi

BLOCK_JSON=$("$CCUSAGE" blocks --active --json 2>/dev/null)
WEEK_JSON=$("$CCUSAGE" weekly --json 2>/dev/null)

python3 - "$BLOCK_JSON" "$WEEK_JSON" <<'PYEOF'
import json, sys, datetime

block_raw, week_raw = sys.argv[1], sys.argv[2]

lines = []

try:
    blocks = json.loads(block_raw).get("blocks", [])
except Exception:
    blocks = []

if blocks:
    b = blocks[0]
    models = ", ".join(m.replace("claude-", "") for m in b.get("models", []))
    cost = b.get("costUSD", 0)
    tokens = b.get("totalTokens", 0)
    remaining_min = b.get("projection", {}).get("remainingMinutes")
    end_time = b.get("endTime", "")
    lines.append(
        f"Current 5h block: ${cost:.2f}, {tokens:,} tokens, models used: {models or 'none'}"
        + (f", ~{remaining_min}min left in window (ends {end_time})" if remaining_min is not None else "")
    )
else:
    lines.append("Current 5h block: no active session block detected.")

try:
    week = json.loads(week_raw)
    weekly_list = week.get("weekly", [])
except Exception:
    weekly_list = []

if weekly_list:
    w = weekly_list[-1]
    total_cost = w.get("totalCost", 0)
    total_tokens = w.get("totalTokens", 0)
    per_model = w.get("modelBreakdowns", [])
    breakdown = ", ".join(
        f"{m.get('modelName','?').replace('claude-','')}: ${m.get('cost',0):.2f}"
        for m in per_model
    )
    lines.append(
        f"This week (since {w.get('period','?')}): ${total_cost:.2f} total, {total_tokens:,} tokens"
        + (f" ({breakdown})" if breakdown else "")
    )

lines.append(
    "(Estimates from local ccusage log parsing — Anthropic doesn't expose an official remaining-quota API for Max plans.)"
)

print("Claude usage snapshot — " + "; ".join(lines))
PYEOF
