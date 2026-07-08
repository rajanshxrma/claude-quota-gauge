---
description: Refresh the per-model (e.g. Fable) weekly % estimate from claude.ai/settings/usage -- the one number rate_limits doesn't expose
---

Do this now, without waiting to be asked -- viewing a settings page has no side effects:

1. Use the browser tool to navigate to `https://claude.ai/settings/usage` (open a new tab; get tab context first if needed).
2. Read the weekly % for whichever model `CLAUDE_USAGE_TRACK_MODEL` is set to (default: `fable`) -- it appears as its own row under "Weekly limits" when the account has a separate pool for that model.
3. Run: `python3 ~/.claude/scripts/usage-calibrate-fable.py <pct>` with the value you just read.
4. Confirm the write succeeded and report the fresh % back to the user in plain language, clearly labeling it as an estimate (not the same kind of verified number as the 5h/weekly-all figures).
