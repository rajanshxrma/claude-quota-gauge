# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- README demo swapped from a static SVG to an animated recording of a real
  session — `/pending` parking an item, and the next session's snapshot
  picking up the new count.
- Renamed `/usage-recalibrate` to `/gauge-cali`.

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
