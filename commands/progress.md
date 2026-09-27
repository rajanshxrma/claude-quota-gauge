---
description: Show this session's work progress bar, or set one up for the task in hand
---

The bar belongs to this session: `~/.claude/scripts/work-progress.py` picks the session up on its own, so there is nothing to pass. Do this now:

1. If `$ARGUMENTS` is empty, run `python3 ~/.claude/scripts/work-progress.py status` and show its output as-is.
2. If `$ARGUMENTS` is `clear`, run `python3 ~/.claude/scripts/work-progress.py clear` and confirm.
3. Otherwise `$ARGUMENTS` describes the work to track. Set a bar up for it:
   - a short label naming the work, under 30 characters (e.g. `release 2.4`);
   - 3 to 8 named steps in the order they'll happen, a word or two each (e.g. `build,review,test,ship`) -- the steps `$ARGUMENTS` gives, when it gives any;
   - an honest estimate for the whole task (e.g. `1h30m`) -- the one `$ARGUMENTS` gives, when it gives one.

   Run `python3 ~/.claude/scripts/work-progress.py set "<label>" --steps "<a,b,c>" --eta <estimate>` and show the line it prints.
4. From then on, keep the bar true while you work: `python3 ~/.claude/scripts/work-progress.py step <name>` in the same turn a step finishes, `note "<what's happening>"` when the current step changes character, `eta <new estimate>` when the estimate stops being right, and `clear` once the work is delivered. The time left is an estimate: give your honest best guess, never padded or trimmed to look good.
