# PENDING

Parked issues and open questions — one `## ` heading per item, newest on top.
Written with enough detail that a cold session (yours next week, or a
different model entirely) can pick one up without re-deriving context.

`usage-estimate.py` counts the `## ` headings below and surfaces it as
`pending: N` in your statusline. A heading with "RESOLVED" in it (any case)
still lives here for reference but is excluded from the count — close things
out by editing the title, not by deleting the section.

## Recalibration drifts if a session runs entirely on one non-default model

Noticed the weekly estimate reads a little high after a session that's
almost all Opus, almost all cache reads. Cost-weighting should already
account for this — worth a follow-up recalibration to confirm it's just
normal between-calibration drift and not a pricing-table gap.

## RESOLVED: statusline showed stale % after a Claude Code update

Turned out to be an unrelated ccusage cache; `ccusage --clear-cache` fixed
it. Keeping this here as a note in case it recurs.
