# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows
[Semantic Versioning](https://semver.org/).

## [0.8.1] - 2026-07-11

### Added
- **Mid-session self-heal for tracked-model staleness.** The `SessionStart`
  auto-recalibration nudge (shipped in v0.3.0) only fires once, at a
  session's own launch — a long-running session that crosses the v0.8.0
  staleness threshold (`CAP_MAX_AGE` or the drift tripwire) partway through
  had no way to self-heal until a new session happened to start. New
  `UserPromptSubmit` hook `bin/fable-stale-prompt-hook.py` re-checks on
  every prompt, so the very next message after it goes stale mid-session
  triggers the same "recalibrate now, no need to ask" nudge — same shape as
  the existing UI-theme-drift detector, which solves the identical
  "mid-session, not just at launch" problem for that unrelated staleness.
  New `fable_stale_to_announce()` in `usage_common.py` dedups per session
  against the calibration's own identity (mirroring
  `theme_drift_to_announce()`), so a given stale episode is announced once,
  not spammed every message, and re-arms cleanly once a fresh calibration
  lands. Backed by a new small state file, `fable-stale-state.json`, pruned
  the same way as the existing `theme-state.json`.

## [0.8.0] - 2026-07-11

### Fixed
- **The tracked-model (Fable) weekly estimate could be badly wrong while
  reporting itself perfectly healthy.** It showed 16% used when the real
  `claude.ai/settings/usage` page read 40% — a 24-point silent miss. Root
  cause: the estimate has always been projected entirely from local Claude
  Code transcript tokens (`tokens-since.py`), which is structurally blind to
  that model's usage through claude.ai web, mobile, or another machine. It
  tracked closely early on only because virtually all of that usage
  happened to be CLI-only at the time; as usage elsewhere grew, drift was
  inevitable and had no way to surface itself — `CAP_MAX_AGE` was 14 days,
  and nothing compared the projection against any other real signal in
  between.
- Two independent fixes, both in `fable_estimate()`: `CAP_MAX_AGE` tightened
  from 14 days to `CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS` (default 12h), and
  a new fast tripwire — `usage-calibrate-fable.py` now also records the
  real, free aggregate weekly % (`seven_day_pct_at_cal`) at calibration
  time; every later read compares it against the current real aggregate and
  reports stale immediately if it's moved more than
  `CLAUDE_USAGE_FABLE_DRIFT_THRESHOLD` points (default 5) — a fast signal
  that account-wide usage moved in a way the local-only projection may not
  have caught. The existing SessionStart auto-recalibration nudge
  (`usage-session-hook.py`, shipped since v0.3.0) already tells the next
  session to recalibrate immediately whenever the estimate is stale — this
  fix is what makes that mechanism actually fire when it should.

### Changed
- Renamed `/gauge-cali-fable` → `/gauge-calibrate`. The tool already
  generalizes over `CLAUDE_USAGE_TRACK_MODEL`; Fable is just today's only
  model with a hidden separate weekly pool, so the command name was
  needlessly narrow. Internal identifiers (`usage-calibrate-fable.py`,
  `fable_estimate()`, `fable_*` cache keys) are unchanged — only the
  command file and user-facing strings moved.

## [0.7.0] - 2026-07-10

### Fixed
- **The statusline could show a blank usage gap with zero explanation on a
  fresh install.** The `usage: unavailable` fallback (including the
  helpful `Claude Code X < 2.1.80` message) was gated on `if not parts`,
  but the model segment added in 0.4.0 is always present — so that
  condition could never be true. Result: a machine with no `rate_limits`
  yet and no prior cache (every fresh install's first render, and every
  render on Claude Code < 2.1.80) showed only the model and the
  right-aligned session id, with nothing in between and no indication why.
  The fallback now tracks its own segments independently of the model
  segment, so it's reachable again.

### Added
- `install.sh` now checks `claude --version` and warns if it's older than
  2.1.80, so the real requirement is learned at install time rather than
  inferred from an incomplete bar.
- `install.sh` scaffolds `~/.claude/claude-quota-gauge.env` from the
  example file if no config exists yet, so every optional variable is
  discoverable in one place immediately after install.
- `install.sh` now explains the `pending: N` feature in full and asks
  whether to set up a starter `PENDING.md` — still off by default, but no
  longer silently invisible to a first-time installer who never reads the
  README section for it.
- Clearer post-install summary: states plainly what shows up immediately
  (the two real numbers), what's optional and how to turn each on (Fable
  tracking, pending, the background watcher, theme-drift detection), and
  where the config lives.

### Changed
- The optional config file is renamed from `usage-calibrator.env` to
  `claude-quota-gauge.env` (matching the project name) — both the example
  in `config/` and the default path `load_env_file()` reads. Fully
  backward compatible: if the new file doesn't exist, `load_env_file()`
  falls back to the old filename automatically, so upgrading the scripts
  alone never breaks an existing setup.

## [0.6.0] - 2026-07-10

### Added
- The statusline now trails with the current session's own full
  `session_id`, read from the same stdin payload as the model/effort
  label. Copying it lets a session running up against a usage limit be
  resumed in a fresh terminal window with `claude --resume <id>` — the
  full UUID, not a shortened prefix, since that's the only form
  `--resume` accepts.
- Optional (`CLAUDE_USAGE_THEME_WATCH=1`, macOS only) background detection
  of UI theme staleness: Claude Code's theme resolves once at session
  launch and is never hot-reloaded, so a long-open session silently drifts
  from the OS light/dark appearance with `/config theme=auto` as the only
  fix. A new `UserPromptSubmit` hook (`bin/theme-watch-prompt-hook.py`,
  wired by `install.sh`) flags a real drift to Claude in the background —
  never in the visible statusline bar — the first time it's noticed, so
  it can be surfaced unprompted instead of sitting silently stale.

### Changed
- The cached-fallback tail (shown briefly at session start, before the
  first real `rate_limits` payload lands) now reads `(refreshing…)`
  instead of `(cached, auto-updates shortly)` — plainer wording, and
  short enough on both the 5h and weekly rows that the new `session_id`
  segment no longer gets truncated off the end of the bar.

## [0.5.1] - 2026-07-10

### Fixed
- The `opusplan→` model tag no longer renders impossible combinations like
  `opusplan→Fable 5`. The tag was applied whenever the `model` setting on
  disk said `opusplan`, without checking whether the live model was one
  opusplan can actually produce — so a session-level `/model` override to
  any other model still got tagged. The prefix now only appears when the
  payload's live model is Opus or Sonnet, the two sub-models opusplan
  alternates between. (An override to Opus or Sonnet themselves is
  indistinguishable from opusplan in the payload, so the tag can still
  show in that narrow case.)

## [0.5.0] - 2026-07-10

### Changed
- Fable (or whatever `CLAUDE_USAGE_TRACK_MODEL` is set to) weekly tracking
  switched from a ratio-scaling model to an absolute weekly-cap model. The
  old model remembered a calibrated `%` and re-scaled it on every read by
  the ratio of local token deltas (`pct = cal_pct * tokens_now /
  tokens_at_cal`) — accurate right after a calibration, but drifting
  between them with no ground truth, and needing a tight staleness ceiling
  to bound how far that drift could go unnoticed. The new model derives a
  weekly `$` cap once from a real, non-zero read (`cap = tokens_at_cal /
  (pct/100)`), then projects live local usage against that fixed cap on
  every statusline render — the number moves in near-real-time as usage
  accrues, the same way the two real `rate_limits` numbers do, without
  needing a fresh calibration to move at all.
- The weekly window now advances itself at the real reset boundary with no
  browser read needed: it prefers `rate_limits.seven_day.resets_at`
  (ground truth — the tracked model's pool resets in lockstep with the
  all-models weekly) when available, else steps forward in 7-day
  increments from the last known boundary. The projection naturally reads
  ~0% right after a rollover instead of requiring a fresh calibration to
  notice one happened.
- Staleness ceiling relaxed from a hard 6 hours (`FABLE_MAX_CAL_AGE`) to a
  ~14-day cap-re-verification window (`CAP_MAX_AGE`), since the cap itself
  is slow-moving (Anthropic's weekly limit rarely changes) unlike the old
  model's moving anchor, which needed frequent re-anchoring to stay
  trustworthy. Added an explicit projection ceiling (~120%): if local usage
  projects past that against the cap, it's reported stale rather than a
  number nobody would believe, since that's a sign the cap itself has
  drifted from reality.
- A calibration read of exactly 0% can't derive a cap (nothing used yet to
  calibrate a denominator against) — it now updates the window/reset
  bookkeeping but deliberately keeps whatever cap is already on file,
  instead of discarding a good cap just because one particular read landed
  at zero.
- Before any cap has ever been derived (fresh install, or every read so
  far has landed at 0%), the statusline no longer shows an alarming
  `fable: stale, run /gauge-cali-fable` message. It shows the honest
  number instead — `0%`, since that's the only way a cap couldn't be
  derived — the same graceful-fallback principle the 5-hour/weekly-all
  numbers already use (v0.3.2: fall back to a known real number instead of
  an "unavailable" message whenever one is available). But only for as
  long as `0%` stays true: the moment local transcripts show tracked-model
  usage beyond what was on file at that 0% read, there's nothing to
  project it against, so it reports stale — which triggers the standard
  auto-recalibration, and that first non-zero read derives the cap and
  makes the number fully live from then on. Without that guard, the
  friendly `0%` would sit frozen while real usage climbed — the same
  freeze bug this release kills, in friendlier clothes. `stale` otherwise
  remains reserved for genuine drift risk once a cap *does* exist: too old
  to trust, or a live projection so far past it that the cap itself looks
  wrong. Relaxing those the same way would have resurrected the exact
  silent-drift bug (showing 99% when the real number was 0%) v0.4.1 was
  built to catch, so they still report staleness plainly.

### Added
- `usage-watch.py` now appends each distinct cached snapshot to a
  persistent history log (`~/.claude/usage-history.jsonl`), piggybacking on
  its existing 15-minute launchd cadence rather than adding a second
  scheduled job.
- `usage-watch.py` now also fires a native notification when the tracked
  model's estimate itself goes stale (not just threshold crossings),
  deduped per calibration so it renotifies once per stale state and again
  after the next recalibration — this closes a real gap where a stale
  estimate could sit silently for hours with no session open to notice via
  the statusline.

### Fixed
- Calibrating at exactly 0% used to freeze the Fable estimate at 0% for
  the rest of the week: the old ratio model multiplied by the calibrated
  `%`, so a `0` anchor could never project any growth as usage
  accrued — the opposite of what a live estimate should do. The absolute-
  cap model has no such freeze; it isn't built on `pct` as a live
  multiplier, only as provenance for how the cap was derived.
- The calibration JSON gains `cap` / `cap_derived_at` fields; `pct` is now
  kept purely as a record of the last real read, not used as a live
  multiplier. If anything downstream reads the old ratio-model assumption
  (that `pct` alone determines the live number), that's why it changed.

## [0.4.0] - 2026-07-09

### Added
- The statusline now leads with the current session's own active model and
  reasoning effort (e.g. `opusplan→Sonnet 5 (high)`, `Fable 5 (xhigh/fast)`),
  read from the `model`, `effort`, and `fast_mode` fields Claude Code already
  includes in the statusLine stdin payload. New `fmt_model()` /
  `resolve_configured_model()` helpers in `usage_common.py`. Since Claude
  Code invokes the statusline command separately per open session, this
  works out of the box across multiple concurrent terminals/tabs each on a
  different model or effort level, with no extra bookkeeping.

## [0.3.3] - 2026-07-08

### Fixed
- The countdown to a window's reset had two blind spots that both read as
  "is this hung?": (1) the last minute before a reset showed a frozen
  `0h 0m` instead of a visibly ticking countdown, and (2) the moment a
  window's `resets_at` passed — whether from live `rate_limits` or the
  0.3.2 cached fallback — nothing distinguished "actively resetting right
  now" from a stale number sitting there indefinitely. Sub-2-minute
  countdowns now show seconds (`resets 42s`), and any window past its
  reset boundary shows an explicit `resetting... (was 18%)` state instead
  of a countdown stuck on `now`. Applies to the statusline, the
  SessionStart context line, and the per-model weekly row alike (new
  shared `fmt_window()` helper in `usage_common.py`).

## [0.3.2] - 2026-07-08

### Fixed
- The statusline showed a bare `usage: unavailable` on renders where
  `rate_limits` was momentarily empty — typically the very first render of
  a new session, before Claude Code's first API turn populates it — even
  though the last real numbers were sitting right there in the cache file.
  Now falls back to the last cached `5h`/`week` percentages (labeled
  `(cached)`) whenever `rate_limits` is empty but a prior cache exists;
  only falls through to `unavailable` when there's truly no cache yet
  (e.g. a fresh install's very first run).

## [0.3.1] - 2026-07-08

### Changed
- Dropped the `(est.)` label 0.3.0 attached to the per-model weekly figure
  everywhere it appeared (statusline, SessionStart context, watcher
  notifications) — rajan's explicit call, based on this figure tracking
  real numbers closely (his stated tolerance: within ~1%) across the time
  it ran as the tool's only mechanism, before `rate_limits` existed. It's
  now shown the same way as the two real numbers. The staleness check is
  unchanged and still reports itself explicitly (`fable: stale, run
  /gauge-cali-fable`) rather than showing a number scaled against an
  already-ended window — that's a genuine failure mode, not a confidence
  question, so it still speaks up.

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
