# Pairing session notes: `--json` output flag

Draft scaffolding for a real pair-programming session with Rohit. Not
committed, not pushed -- lives as uncommitted changes on the local branch
`pairing/json-output-flag`. The actual commit (and the co-authored PR) should
happen live, together, so the "Pair Extraordinaire" achievement reflects real
collaboration.

## What this is

`bin/usage-statusline.py` is the statusLine command Claude Code feeds a JSON
payload to on every render (real `rate_limits` from Anthropic, plus a
locally-projected weekly % for a per-model pool `rate_limits` doesn't expose
-- see `fable_estimate()` in `bin/usage_common.py`). Today it only ever prints
a human-readable bar string. The point of this feature: let other
scripts/tools consume the same numbers as JSON instead of scraping the
terminal string.

## What's scaffolded (done, working, tested)

- `--json` flag on `bin/usage-statusline.py`, gated so the **default (no
  flag) text output is byte-for-byte unchanged** -- verified by
  `tests/test_usage_statusline_json.py::DefaultTextOutputUnchangedTest`.
- A `data` dict built alongside the existing `parts` list inside `main()`,
  one entry per bar segment, populated in the exact same branches that
  already compute each value -- no new computation, just capturing what was
  already being calculated before it got formatted into text.
- Working, tested JSON output for the common/healthy paths:
  - `model` -- the same display string the bar leads with (e.g. `"Fable 5
    (high)"`), or `null`.
  - `five_hour` / `seven_day` -- `{"pct": <number>, "resets_at": <unix
    epoch or null>, "cached": <bool>}`, or `null` if that window has never
    been seen. `cached: true` mirrors the bar's "(refreshing…)" fallback
    state (using the last-known cached value while a real `rate_limits`
    payload hasn't landed yet this render).
  - `tracked_model` -- `{"name": "fable", "stale": <bool>, "pct": <number
    or null>, "resets_at": <epoch or null>}` for the healthy
    (non-stale) case. **This shape is a first cut, not final for the stale
    sub-states -- see below.**
  - `pending_tasks` -- same integer (or `null`) as the `pending: N` segment.
  - `generated_at` -- ISO8601 timestamp of this render, JSON-only (the text
    bar has no equivalent since it's always "now" by definition).
- `tests/test_usage_statusline_json.py` -- stdlib `unittest`, no new deps.
  Every test runs the real script as a subprocess against a fresh, isolated
  `HOME` tempdir (never touches your real `~/.claude/scripts/usage-live.json`
  or fable calibration). Covers:
  - default text output unchanged (regex-matched, tolerant of the live
    countdown value changing run to run)
  - `--json` produces valid JSON whose numbers match the same-input text
    output
  - a **known/mocked quota state**: a fake fable calibration file + one fake
    local transcript entry with a hand-computed expected cost (1,000,000
    `claude-fable-5` input tokens against a cap of 100 → exactly 10.0%),
    asserting the JSON's `tracked_model.pct` against that independently
    computed number, not just "whatever the code happens to produce"
  - the fully-dark case (no `rate_limits`, no cache) still emits valid JSON
    rather than crashing

Run them with:

```
cd ~/Desktop/claude-quota-gauge
python3 -m unittest discover -s tests -v
```

All 6 pass as of this writing.

## What's left for the live session (real work, not busywork)

1. **The stale tracked-model JSON contract.** The bar text renders *four*
   distinct stale sub-states for the tracked-model row (see the comments
   around `fable_estimate()` in `bin/usage_common.py` and the `if
   fable["stale"]:` block in `bin/usage-statusline.py`):
   - never calibrated at all
   - stale but within the auto-heal grace window (bar shows "refreshes next
     msg!")
   - stale past the grace window, with a cached % to show (bar shows an
     elapsed-hours call to action, e.g. "stale 14h — /gauge-calibrate")
   - stale with no cached % at all
   Right now `data["tracked_model"]` collapses all of this to a bare
   `stale: bool` + last-known `pct`/`resets_at`. Is that enough for a
   script consuming this JSON, or should it also expose the elapsed-hours /
   grace-window state so a caller can build its own UX instead of parroting
   the bar's copy? There's a `# TODO(pairing session)` comment marking the
   exact spot in `bin/usage-statusline.py` (search for that string). This is
   a genuine design call, not a mechanical one -- good first task to pair on.
2. **The fully-unavailable JSON contract.** When there's no `rate_limits`
   *and* no cache at all, the bar shows one of two different text messages
   (plain "usage: unavailable" vs. blaming an old CLI version when the
   payload confirms it). The JSON currently just sets
   `data["unavailable"] = True` as a placeholder and leaves `five_hour`/
   `seven_day` as `null` -- should the JSON surface *why* (old CLI vs. no
   data yet vs. free-tier account), the way the text does? Also has a `#
   TODO(pairing session)` marker in the code.
3. **Tests for whichever contract gets picked in (1) and (2).** The current
   test suite deliberately does NOT assert on the stale-substate shape or
   the `unavailable` placeholder shape (see the docstring on
   `JsonFlagUnavailableStateTest` in the test file) -- those tests should be
   written once the design is settled, together.
4. **README docs.** `README.md` doesn't mention `--json` at all yet. Once
   the two open contracts above are settled, it needs a short section
   (probably near the existing statusline description) plus a sample output
   block.
5. **CHANGELOG entry.** Following the existing `Keep a Changelog` format at
   the top of `CHANGELOG.md` -- holding off until the feature is actually
   finished/committed together, per this project's usual practice of one
   real entry per shipped change.
6. **Optional stretch, only if there's time and appetite:** a tiny example
   consumer script (e.g. something that reads `--json` output on a cron and
   only alerts when `tracked_model.pct` crosses a threshold) would be a nice
   concrete proof the flag is actually useful for "other tools/scripts," not
   just a format change nobody uses.

## Co-authorship trailer

Once the two of you land the real commit together, use:

```
Co-authored-by: Rohit <111145290+rohitN04@users.noreply.github.com>
```

(confirmed via `gh api users/rohitN04 --jq .id` -- exact format GitHub
requires for the co-author to count toward contribution/achievement credit:
numeric ID first, then `+`, then the GitHub username,
`@users.noreply.github.com`.)

Rohit was invited as a push-access collaborator on this repo (2026-07-21) --
he needs to accept the invite at
https://github.com/rajanshxrma/claude-quota-gauge/invitations before pushing.

## Files touched by this scaffolding

- `bin/usage-statusline.py` -- the `--json` flag + `data` dict (modified)
- `tests/test_usage_statusline_json.py` -- new test file
- `PAIRING_SESSION_NOTES.md` -- this file (not committed)

Currently sitting as uncommitted changes on local branch
`pairing/json-output-flag` (not pushed, no remote touched).
