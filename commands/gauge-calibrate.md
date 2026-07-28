---
description: Refresh the tracked model's (default Fable) weekly % estimate from claude.ai/settings/usage -- the one number rate_limits doesn't expose
---

Do this now, without waiting to be asked -- viewing a settings page has no side effects:

1. Use the browser tool to navigate to `https://claude.ai/settings/usage` (open a new tab; get tab context first if needed).
2. Read the weekly % for whichever model `CLAUDE_USAGE_TRACK_MODEL` is set to (default: `fable`) -- it appears as its own row under "Weekly limits" when the account has a separate pool for that model.
3. Run: `python3 ~/.claude/scripts/usage-calibrate-fable.py <pct>` with the value you just read.
4. Close the browser tab. Confirm the write succeeded.
5. Only announce the fresh % in chat if this run was explicitly user-initiated -- they typed `/gauge-calibrate` themselves, or asked a question about usage in this conversation. If this run was instead triggered silently by a SessionStart/UserPromptSubmit hook's `additionalContext` (i.e. nothing the user said prompted it), just write the calibration and say nothing about it -- the whole point of the event-driven trigger is that this stops being something the user has to sit through. When you do announce it, label it clearly as an estimate (not the same kind of verified number as the 5h/weekly-all figures).
