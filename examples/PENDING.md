# PENDING

Parked issues and open questions — one `## ` heading per item, newest on top.
Written with enough detail that a cold session (yours next week, or a
different model entirely) can pick one up without re-deriving context.

`usage-statusline.py` counts the `## ` headings below and surfaces it as
`pending: N` in your statusline. A heading with "RESOLVED" in it (any case)
still lives here for reference but is excluded from the count — close things
out by editing the title, not by deleting the section.

## RESOLVED: recalibration drifted if a session ran entirely on one non-default model

Was a real gap in the old cost-weighted local estimator — the whole
estimate-and-calibrate mechanism was replaced in 0.2.0 by Claude Code's own
`rate_limits` field, so there's no calibration left to drift.

## RESOLVED: statusline showed stale % after a Claude Code update

Turned out to be an unrelated ccusage cache; `ccusage --clear-cache` fixed
it. Keeping this here as a note in case it recurs.
