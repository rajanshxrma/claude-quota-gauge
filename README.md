# claude-quota-gauge

Your real Claude Max quota %, in your terminal, straight from Claude Code
itself — no scraping, no stored credentials, no estimating for the two
numbers Anthropic actually reports.

![the statusline in a terminal: model + effort, the real 5h / weekly / Fable %, a pending count and the session id, with the workload gauge line (compute / io / ram → verdict) rendering beneath it](docs/demo.gif)

Since Claude Code v2.1.80, the statusline command is fed a `rate_limits`
field on stdin — the exact 5-hour and weekly used-percentage Anthropic's own
backend reports, refreshed automatically every render. This tool reads that
field directly, caches it, and surfaces it two ways: in your statusline, and
injected into every new session's context automatically. Nothing here reads
your browser, touches an API key, or approximates anything — every number
shown is the same one `claude.ai/settings/usage` would show you, because
it's the same data, straight from Claude Code.

The bar carries a second line too: a **workload gauge** that tells an
I/O-bound session apart from a compute-bound one, so you know at a glance
whether to stack jobs in parallel or run them one at a time. It reads the
same signals Activity Monitor does — CPU, GPU, RAM/swap — with no `sudo`
prompt, and it's kept fresh by a background sampler without ever slowing a
render. See [Workload gauge](#workload-gauge-io-bound-vs-compute-bound) for
how to read it.

If your account also has a separate weekly pool for one model (Fable, on
Claude Max) that `rate_limits` doesn't break out, there's an optional
add-on for that too — see [Optional: per-model weekly tracking](#optional-per-model-weekly-tracking-eg-fable).
It's the one number in this tool that isn't straight from Anthropic's
backend; it's calibrated by hand against the real settings page once to
derive a weekly cap, then projected live from local usage against that cap.
Unlike the two numbers above it, it's an estimate with a real, known blind
spot (it can't see that model's usage outside this CLI) — it leans hard
toward reporting itself stale rather than showing a confident wrong number;
see the section below before relying on it.

![version](https://img.shields.io/badge/version-0.9.5-informational)
![MIT license](https://img.shields.io/badge/license-MIT-blue)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)

## Requirements

Claude Code **v2.1.80 or newer** (check with `claude --version`) — that's
the release where `rate_limits` was added to the statusline payload.
`install.sh` checks this for you and warns if you're on an older version;
either way, the statusline itself will say so plainly rather than guess.

## Quickstart

```bash
git clone https://github.com/rajanshxrma/claude-quota-gauge && cd claude-quota-gauge
./install.sh
```

The installer checks your Claude Code version, wires the statusline and
hooks into `~/.claude/settings.json` (asking first, and backing up your
existing settings), and walks you through one optional feature —
[pending tracking](#the-pendingmd-convention) — explaining what it is
before asking whether to turn it on (off by default either way). That's it —
no calibration step, nothing to read off a settings page by hand. Open
Claude Code and the statusline shows your real 5h/weekly % as soon as it
first renders, usually within a few seconds. If your account also has its
own weekly pool for one model (e.g. Fable), see
[Optional: per-model weekly tracking](#optional-per-model-weekly-tracking-eg-fable)
for a one-time opt-in step to track that too.

---

## How it works

1. **Claude Code hands the real numbers to the statusline command.** Every
   render, it feeds the statusline command a JSON payload on stdin that
   includes `rate_limits.five_hour.used_percentage` and
   `rate_limits.seven_day.used_percentage` — Anthropic's own backend
   figures, not a local approximation. The command is `statusline.py`, a
   thin wrapper that forwards that payload to `usage-statusline.py` (the
   quota line) and appends the [workload gauge](#workload-gauge-io-bound-vs-compute-bound)
   line beneath it.
2. **The script prints the statusline and caches those numbers to disk**
   (`~/.claude/scripts/usage-live.json`), so other things — the
   `SessionStart` hook, the background watcher — can read the latest known
   real values without needing that stdin payload themselves.
3. **A `SessionStart` hook injects the cached numbers into every new
   session's context** automatically. Nothing to run, nothing to trigger —
   it's just there.
4. **An optional `launchd` watcher** re-checks the cache periodically and
   fires a macOS notification when either number crosses a threshold.

```mermaid
flowchart LR
    A["Claude Code<br/>(rate_limits on stdin)"] --> B[usage-statusline.py]
    B -->|renders| C[statusline text]
    B -->|caches| D[(usage-live.json)]
    D --> E[SessionStart hook]
    D --> F[usage-watch.py]
    F -->|crosses threshold| G[macOS notification]
```

## Model + effort in the bar

Every statusline render also leads with the current session's own active
model and reasoning effort — e.g. `opusplan→Sonnet 5 (high)` or `Fable 5
(xhigh/fast)` — read straight from the same stdin payload (`model.id`/
`display_name`, `effort.level`, `fast_mode`). If your `model` setting is
`opusplan`, that mode alternates the live model between Opus (plan phase)
and Sonnet (execution); the bar tags it as `opusplan→` on top of whichever
sub-model is actually live at that render, so the mode stays visible even
as the underlying model flips. The tag only appears when the live model is
one opusplan can actually produce (Opus or Sonnet) — a session-level
`/model` override to anything else takes that session out of opusplan mode,
and the bar shows the override bare rather than an impossible combination
like `opusplan→Fable 5`.

Claude Code invokes the statusline command separately per open session, so
if you have several terminals/tabs open on different models or effort
levels, each one's bar reflects only its own session — nothing to
configure to keep them straight.

## Resuming a session from the bar

The bar also trails with this session's own full `session_id` (the same
stdin payload carries it), e.g. `session: 71bb780d-80a5-46c3-9cfa-bf3a0e0fa4bc`.
If a long-running session gets close to a usage limit, copy that ID and run
`claude --resume <id>` in a fresh terminal window to pick it back up —
`--resume` needs the whole UUID, so a shortened prefix won't work.

## Workload gauge (I/O-bound vs compute-bound)

The second line of the bar answers one question: is this machine *waiting* or
*calculating* right now? That's the line between work you can stack in
parallel for free and work you have to run one job at a time.

![the workload gauge full-screen: compute/io/ram gauges, the serialize-vs-parallelize verdict, a CPU/GPU/RAM breakdown, and the top processes eating the machine](docs/workload-demo.gif)

- **I/O-bound** — the bottleneck is waiting on something external (network, a
  download, disk, an API, CI, you typing). The chips sit idle, so overlapping
  many such jobs is basically free. Glyph `⇄`, verdict `parallelize`.
- **Compute-bound** — a chip (CPU or GPU) is pegged near 100% doing math
  (training, local inference, video encode, a big compile). A second job just
  splits the same 100%, so these run best one at a time. Glyph `⚙`, verdict
  `serialize·CPU` or `serialize·GPU` naming which chip is the wall.

A sample line, annotated:

```
⚙  compute 99%  io 41%  ram 76%  → serialize·GPU  ⚠swap
│      │           │        │          │            │
│      │           │        │          │            └─ RAM has become the bottleneck (see below)
│      │           │        │          └─ the verdict + which chip is the wall
│      │           │        └─ ram gauge — memory pressure (used %)
│      │           └─ io gauge — how much data is flowing
│      └─ compute gauge — how pegged the chips are
└─ class glyph — this is what tells you the kind of workload
```

The same elements as a reference:

| Element | Meaning |
| --- | --- |
| `⚙` | Class glyph — `⚙` compute-bound · `⇄` I/O-bound · `·` idle · `◐` mixed/not saturated |
| `compute 99%` | **Compute gauge** — the dominant chip peg, `max(CPU busy, GPU util)`. How hard the chips are actually working. |
| `io 41%` | **I/O gauge** — disk + network throughput saturation (a soft curve; 40 MB/s reads ~50%). |
| `ram 76%` | **RAM gauge** — memory pressure as used % (`100 − free`), so it reads the same direction as the other two: high = tight. |
| `→ serialize·GPU` | The verdict and the advice that follows. `·GPU`/`·CPU` names the pegged chip. |
| `⚠swap` | RAM has become the real bottleneck — you're in swap (>1 GB) or under 20% free. Trumps the chip verdict: don't stack big models. This marker fires on top of the `ram` gauge only once memory is actually the problem. |

**The number colors are intensity, not class** — green under 50%, yellow 50–79%,
red 80%+. So `compute 99%` in red means the chips are slammed; `io 41%` in green
means I/O is quiet. The glyph and verdict are what tell you *which kind* of
workload it is.

These are **two independent gauges, not a split that sums to 100** — "how
pegged are the chips" and "how much data is flowing" are genuinely separate
questions, and macOS doesn't expose a true `iowait%`, so faking one number
would be confidently wrong. The verdict is simply whichever gauge dominates.
You won't see `idle` unless *both* are low at once; a busy machine is always
one of the other three.

**How it stays fresh without lagging the bar.** A sample takes about a
second — far too slow to run on every render. So the render never samples: it
reads a cache file instantly. A small background writer (`workload-gauge.py
--watch-cache`, auto-spawned) re-samples every ~3 seconds to keep that cache
current, and self-exits after 90 seconds with no session watching, so nothing
runs when you're not using Claude Code. If the writer ever dies and the cache
goes truly stale, the line shows `⚠ stale` rather than a confident old number.

**GPU with no `sudo`.** Live GPU utilization comes from `ioreg` (the
IOAccelerator `Device Utilization %`), deliberately not `powermetrics`, so the
gauge never prompts for a password.

Run it full-screen any time for the detailed view — both gauges, the verdict,
CPU/GPU/RAM breakdown, and the top processes eating the machine:

```bash
python3 ~/.claude/scripts/workload-gauge.py          # one reading
python3 ~/.claude/scripts/workload-gauge.py --watch   # live, refreshes each second
```

## Configuration

`install.sh` copies `config/claude-quota-gauge.env.example` to
`~/.claude/claude-quota-gauge.env` for you (skipped if either that file or
the pre-0.7.0 `~/.claude/usage-calibrator.env` already exists — the old
name still works, it's just no longer the default). Uncomment what you
need — it's loaded automatically, including by the statusline command, the
`SessionStart` hook, and `launchd`, none of which see your shell profile.

| Variable | Default | What it does |
|---|---|---|
| `CLAUDE_USAGE_PENDING_FILE` | `./PENDING.md`, then `~/.claude/PENDING.md` | See the PENDING.md convention below |
| `CLAUDE_USAGE_TRACK_MODEL` | `fable` | Which model gets the optional calibrated weekly tracking — see below |
| `CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS` | `12` | Hours a calibration can go unverified before the tracked-model estimate reports stale — see below |
| `CLAUDE_USAGE_FABLE_DRIFT_THRESHOLD` | `2` | Points of *unexplained* weekly-all-models movement (beyond what local usage accounts for) before the estimate reports stale immediately — see below |
| `CLAUDE_USAGE_ALERT_THRESHOLD` | `85` | % that triggers a desktop notification |
| `CLAUDE_USAGE_THEME_WATCH` | unset (off) | macOS-only: flags UI theme staleness in the background — see below |

## The PENDING.md convention

A sibling to `CLAUDE.md`/`AGENTS.md`: a plain markdown file of parked issues,
one `## ` heading per item, written with enough detail that a cold session
can pick one up without re-deriving context. `usage-statusline.py` counts the
headings (excluding ones with "RESOLVED" in the title) and surfaces it as
`pending: N` in your statusline — a standing, ambient reminder that
something's still open. See `examples/PENDING.md` for the shape.

![parking a thought with /pending mid-session: the statusline's pending count goes from 5 to 6 on the very next render](docs/pending-demo.gif)

Off by default, on purpose — `pending: N` only appears once a `PENDING.md`
actually exists. `install.sh` walks you through this explicitly (explains
it in full, then asks) rather than either hiding it in the README or
turning it on unasked; decline and it stays off, exactly as designed.

Run `/pending <what's parked>` to add one from inside a Claude Code session —
it finds the right file (same resolution order as above), creates it from
the template if it doesn't exist yet, and inserts your item as a new
newest-on-top `## ` heading without touching anything already there.

## Optional: per-model weekly tracking (e.g. Fable)

`rate_limits` only exposes two real numbers: 5-hour and weekly-all-models.
If your account has its own separate weekly pool for one model — Fable, on
the Claude Max plan, has its own row on `claude.ai/settings/usage` distinct
from the shared pool — there's no API for that figure. This add-on fills
that one gap with a local, cost-weighted projection: one real read off the
settings page derives a weekly $ cap (`bin/usage-calibrate-fable.py`), and
`bin/tokens-since.py` then projects live local usage against that fixed cap
on every render — no browser automation running in the background.

**Know its real limitation:** the projection only sees usage of the tracked
model through *this* Claude Code CLI. It's blind to that model used via
claude.ai web, mobile, or another machine — if a meaningful share of your
usage happens there, the projected % quietly falls behind the real one
between calibrations (this is exactly what a real 8%→40% week looked like
locally: only 8%→16%). It is not a substitute for the real number on the
settings page, only the closest available estimate between checks of it.

Because of that, it leans hard toward flagging itself stale rather than
showing a confident wrong number: it reports stale the moment the cap
hasn't been re-verified in `CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS` (default
12h), *or* the moment the real weekly-all-models % (free on every render)
has moved more than `CLAUDE_USAGE_FABLE_DRIFT_THRESHOLD` points (default 2)
beyond what local usage since the last calibration accounts for. That
"beyond what local usage accounts for" matters: aggregate movement from
ordinary CLI usage is fully visible to the projection and proves nothing,
so it's subtracted out (using an aggregate-pool cap estimated at
calibration time from the same snapshot) — what's left is precisely the
"usage happened somewhere this projection can't see" signal, which is why
the threshold can sit at 2 points without false alarms on heavy CLI days.

In the bar, staleness looks calm, not alarming: the last known % stays up,
marked `(refreshes next msg!)` — because recalibration is Claude's job now
(see the hooks below), not a chore the bar needs to assign you. The marker
names its trigger on purpose: the refresh happens automatically as part of
your next message to any session, so there's nothing to sit and wait for —
just keep working. The explicit `stale, run /gauge-calibrate` text appears
only when there's no last known number to show at all. Likewise, before a cap has ever been derived (e.g.
right after install), it shows the honest number (`0%`, since that's the
only way a cap couldn't be derived yet) rather than an error state — same
principle as the 5-hour/weekly-all numbers never showing a scary
`unavailable` when a real cached number is already known.

The weekly window itself resets on its own with no browser read needed —
it advances to the real reset boundary Anthropic reports (the same one the
weekly-all-models number uses), so the projection naturally zeroes out
right after a rollover.

You'll rarely need to run the recalibration by hand: whenever it's stale,
the `SessionStart` hook tells Claude to recalibrate immediately at the start
of your next session, and a second hook (`UserPromptSubmit`,
`bin/fable-stale-prompt-hook.py`) catches the case where staleness trips
*mid-session* instead — the very next message after that gets the same
"recalibrate now, no need to ask" nudge, so a long-running session
self-heals without waiting for a new one. Both are dedup'd per session
against the calibration's own identity, so you're told about a given stale
episode once, not on every single message. Setup, one time (or to trigger
it yourself):

```bash
/gauge-calibrate
```

This drives the browser to `claude.ai/settings/usage`, reads the real %
for whatever `CLAUDE_USAGE_TRACK_MODEL` is set to (default `fable`), and
derives the cap from it. A 0% reading can't derive a cap (nothing used yet
to calibrate against), so it keeps whatever cap is already on file rather
than discarding it.

## Optional: UI theme staleness (macOS only)

Claude Code's light/dark theme resolves once when a session launches and is
never hot-reloaded — if your OS appearance flips while a session sits open
(sleep/wake, a scheduled Dark Mode switch, or you just change it), that
session's colors silently go stale with no visible signal, and no external
process can re-apply the theme for you. The only supported mid-session fix
is running `/config theme=auto` yourself.

This add-on can't change that — nothing can — but it can catch the drift.
Set `CLAUDE_USAGE_THEME_WATCH=1` and a `UserPromptSubmit` hook
(`bin/theme-watch-prompt-hook.py`, wired by `install.sh`) checks the real OS
appearance against what the session's theme actually launched under, and
tells Claude in the background — never in the visible statusline bar — the
first time it notices a mismatch, so it can flag it to you unprompted. It
re-arms if the OS flips again, and stays quiet once the appearance matches
what the session launched under (or the session restarts) — there's no way
to see whether `/config theme=auto` actually ran, so it never assumes a fix
happened it can't verify.

## Optional: background watcher

`launchd/com.example.claude-usage-watch.plist.example` runs `usage-watch.py`
every 15 minutes and fires a native macOS notification when either number in
the cache crosses your threshold. It can only act on what's already cached,
though — the real numbers only arrive while a Claude Code session's
statusline is actively rendering, so if you go hours without opening Claude
Code, the watcher is alerting on the last real reading it has, not a live
one. It never estimates a number to fill the gap; it just tells you, always,
exactly what Claude Code last reported.

```bash
sed "s|__HOME__|$HOME|g; s|__PYTHON3__|$(command -v python3)|g" \
  launchd/com.example.claude-usage-watch.plist.example > ~/Library/LaunchAgents/com.claude-usage-watch.plist
launchctl load ~/Library/LaunchAgents/com.claude-usage-watch.plist
```

Not wired up by `install.sh` — the paths are machine-specific, so this is
opt-in and one command.

## Why I built this

I'm on the Max plan and kept getting surprised by the weekly cap mid-session
with zero warning. For a while this ran entirely on a cost-weighted local
estimate, calibrated by hand against `claude.ai/settings/usage` — it
worked, but it was an approximation with a manual step for every number.
Once Claude Code started handing the real percentage straight to the
statusline command, there was no reason to keep estimating the 5-hour and
weekly-all-models figures: those now show the exact same number the
settings page does, automatically, with nothing to calibrate. The old
estimate mechanism still earns its keep for the one number that has no
real API at all — a separate model's weekly pool (Fable) — but it's opt-in.

## What's not included, on purpose

- **A per-model weekly breakdown from Anthropic's own API** — `rate_limits`
  doesn't expose one, and there's no other API for it. "5-hour" and "weekly
  (all models)" are the only numbers that come straight from Anthropic's
  backend. An optional, opt-in calculation for one model is available (see
  above) for accounts with their own separate pool.
- **Email/push delivery** — the watcher uses stock macOS `osascript` only.
  No SMTP, no third-party notification service, nothing account-specific.
- **Stored credentials of any kind** — nothing here logs into `claude.ai`,
  stores a session cookie, or holds an API key. The only data source is the
  stdin payload Claude Code itself already sends to the statusline command
  you configured.
- **`launchd` auto-registration** — a template is provided, but `install.sh`
  won't load it for you; the Python interpreter path and your username are
  yours to fill in, one `sed` command, above.
- **Linux/Windows, today** — the scripts are plain Python and would run
  anywhere; only the notifier (`osascript`) and installer's hook-wiring
  assume macOS + `~/.claude/`. Not shipped, not tested.
- **Support for Claude Code < v2.1.80** — `rate_limits` doesn't exist on
  older versions. The statusline says so plainly rather than guessing.

## License

MIT — see [LICENSE](LICENSE).
