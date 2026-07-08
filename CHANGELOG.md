# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-07-08

### Added
- Brought back an optional, clearly-labeled weekly estimate for one model
  (default: Fable) that Anthropic's real `rate_limits` field doesn't break
  out — 0.2.0 dropped this entirely rather than let it look like real data.
  It's the same cost-weighted local estimate the tool used to run on for
  everything, now scoped to just this one gap and anchored to Claude Code's
  real reported reset time (`usage-calibrate-fable.py`, `tokens-since.py`
  restored, `fable_estimate()` in `usage_common.py`). Always shown as
  `(est.)` in the statusline, reported as stale rather than silently wrong
  once the weekly window rolls over, and flagged as an estimate everywhere
  it's relayed (SessionStart context, watcher notifications). New
  `/gauge-cali-fable` command and `CLAUDE_USAGE_TRACK_MODEL` config var.

## [0.2.1] - 2026-07-08

### Fixed
- The "unavailable" statusline message asserted `needs Claude Code >=2.1.80`
  whenever `rate_limits` was missing from the payload, even when the running
  version was current — an empty `rate_limits` can also mean no API response
  has landed yet this session, not an outdated CLI. It now reads the
  payload's own `version` field and only names the version as the cause when
  that's actually verified; otherwise it reports "unavailable" with no
  unverified blame attached.

## [0.2.0] - 2026-07-07

### Changed
- **Replaced the entire cost-weighted local estimate and manual calibration
  mechanism** with Claude Code's own `rate_limits` field, which the
  statusline command has been fed on stdin since Claude Code v2.1.80. Every
  number shown is now the real Anthropic-reported percentage, not an
  estimate — no browser scraping, no stored credentials, no anchor to drift
  between calibrations.
- `SessionStart` hook now just relays the last real numbers the statusline
  cached, instead of self-recalibrating via the browser tool — there's
  nothing left to calibrate.
- README demo swapped from a static SVG to an animated recording of a real
  session — `/pending` parking an item, and the next session's snapshot
  picking up the new count.

### Removed
- `/usage-recalibrate` (and its later rename, `/gauge-cali`) — recalibration
  no longer exists as a concept.
- The `ccusage` dependency, `tokens-since.py`, `usage-calibrate.py`, and
  `usage-calibration.json` — the whole cost-weighting/anchor pipeline.
- The per-model tracked weekly % (e.g. "week fable: 94%") — Anthropic
  doesn't expose that breakdown through `rate_limits` or anywhere else, so
  it's gone rather than kept as a guess. Only the two real, verifiable
  numbers (5-hour, weekly all-models) are shown.
- `CLAUDE_USAGE_TRACK_MODEL`, `CLAUDE_USAGE_RESET_DAY`, `CLAUDE_USAGE_RESET_HOUR`,
  `CLAUDE_USAGE_RESET_TZ`, `CCUSAGE` config variables — no longer meaningful
  now that reset times come from `rate_limits` directly.

## [0.1.0] - 2026-07-07

### Added
- Cost-weighted local estimator for Claude Max quota usage — reads session
  transcripts, weights by real per-model dollar cost, anchors to the real %
  from `claude.ai/settings/usage`, and scales linearly between calibrations.
- `SessionStart` hook that injects the current estimate into every new
  Claude Code session automatically.
- `/usage-recalibrate` command to refresh the anchor via the browser tool.
- `/pending` command to append parked items to `PENDING.md` from inside a
  session, following the newest-on-top convention.
- Optional `launchd` background watcher that fires a macOS notification when
  a tracked % crosses a configurable threshold.
- The `PENDING.md` convention: parked issues surfaced as a `pending: N`
  count in the statusline.
- `SessionStart` hook now self-recalibrates instead of just telling the user
  to run `/usage-recalibrate` — when the session or weekly window has gone
  stale, it drives the browser and recalibrates on its own before doing
  anything else that turn.
