---
description: Refresh the real Claude usage % from claude.ai/settings/usage and recalibrate local estimates
---

Do this now:

1. Use the browser tool to navigate to `https://claude.ai/settings/usage` (open a new tab; get tab context first if needed).
2. Read the real numbers off the page: "Current session" % used, "Weekly limits" → "All models" % used, and the % used for whichever model `CLAUDE_USAGE_TRACK_MODEL` is set to (default: `fable`) if shown as its own row — note any other per-model rows that appear too.
3. Run: `python3 ~/.claude/scripts/usage-calibrate.py <session_pct> <weekly_all_pct> <weekly_tracked_model_pct>` with the values you just read.
4. Confirm the write succeeded and report the fresh numbers back to the user in plain language, along with anything worth flagging (e.g. a model close to its weekly cap).
