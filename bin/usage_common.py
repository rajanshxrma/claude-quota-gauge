"""Shared helpers for the statusline renderer, the SessionStart hook, and the
background watcher."""
import json, os, re, subprocess, sys
# hashlib and statistics are imported where they are used: together they are
# about half of this module's import time, and every redraw imports it.
from datetime import datetime, timedelta, timezone

FABLE_CAL_PATH = os.path.expanduser("~/.claude/scripts/usage-fable-calibration.json")
TOKENS_SINCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens-since.py")
# Absolute-cap model (replaced the old ratio-scaling model 2026-07-10): a
# calibration derives a weekly $ cap (tokens_at_cal / (pct/100)) instead of
# remembering a % to scale. This also fixed a real bug in the old model:
# calibrating at exactly 0% (pct=0) froze the ratio-scaled estimate at 0%
# forever, since it multiplied by cal["pct"]. The cap model has no such
# freeze -- it projects live local usage against a fixed denominator.
#
# The max-age ceiling used to be 14 days on the (wrong) assumption that the
# cap is what drifts. It isn't the cap that drifts fastest -- it's the *local
# projection's blind spot*: tokens-since.py only sees Claude Code CLI usage,
# never claude.ai web/mobile usage of the tracked model. Whenever a real
# chunk of that model's usage happens off the CLI, the projection quietly
# falls behind (e.g. an 8%->40% real move showed up locally as only
# 8%->16%). A 14-day ceiling let that run for two weeks before ever forcing
# a re-read. Tightened to a matter of hours so a stale projection can't
# coast silently -- overridable via CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS.
#
# Both env-tunable values are read lazily (inside fable_estimate), NOT at
# module import: every consumer script imports this module first and calls
# load_env_file() after, so a module-level os.environ.get() here would bake
# in the default before the config file's overrides ever landed -- making
# the documented knobs silently dead. The pre-existing config vars (e.g.
# CLAUDE_USAGE_ALERT_THRESHOLD in usage-watch.py) already follow this
# read-after-load ordering; these must too.
def _cap_max_age():
    return timedelta(hours=float(os.environ.get("CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS", "12")))


# The other half of the fix: the max-age ceiling alone only catches drift
# once it's had hours to accumulate. This catches it fast -- measured in
# points of *unexplained* aggregate-weekly movement since the last
# calibration (movement beyond what local usage accounts for -- see the
# tripwire block in fable_estimate()). Because locally-explained movement
# is subtracted out first, this can sit tight without false-positiving on
# heavy CLI days.
def _fable_drift_threshold():
    return float(os.environ.get("CLAUDE_USAGE_FABLE_DRIFT_THRESHOLD", "2"))


# Local projections aren't ground truth -- if the cost-weighted local
# estimate blows past a sane ceiling, that's a sign the cap itself has
# drifted from reality (e.g. Anthropic adjusted the limit), not that the
# tracked model's usage is actually >120% of the weekly pool. Report stale
# rather than a number nobody would believe.
PROJECTION_CEILING = 120


def load_env_file(path="~/.claude/claude-quota-gauge.env"):
    """Loads KEY=VALUE overrides -- neither the statusline command nor a
    SessionStart hook nor launchd inherit the shell profile's env vars, so
    this is how personal config reaches these scripts. Never overrides an
    already-set var.

    Falls back to the old ~/.claude/usage-calibrator.env filename (used
    before the config file was renamed to match the project name) if the
    new one isn't present, so existing installs keep working untouched --
    no silent breakage just from upgrading the scripts."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        legacy = os.path.expanduser("~/.claude/usage-calibrator.env")
        if os.path.exists(legacy):
            path = legacy
        else:
            return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def pending_file_path():
    if os.environ.get("CLAUDE_USAGE_PENDING_FILE"):
        return os.path.expanduser(os.environ["CLAUDE_USAGE_PENDING_FILE"])
    for candidate in ("./PENDING.md", "~/.claude/PENDING.md"):
        path = os.path.expanduser(candidate)
        if os.path.exists(path):
            return path
    return None


def pending_tasks_count():
    """Counts '## ' entries in PENDING.md -- each is one open item.
    Headings containing "RESOLVED" (case-insensitive) are kept in the file
    for reference but excluded from the count."""
    path = pending_file_path()
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return sum(
            1 for line in f
            if line.startswith("## ") and "resolved" not in line.lower()
        )


def version_lt(a, b):
    """Compares dotted version strings numerically (e.g. "2.1.9" < "2.1.80").
    Returns False on anything unparseable rather than guessing."""
    try:
        a_parts = [int(x) for x in a.split(".")]
        b_parts = [int(x) for x in b.split(".")]
    except (ValueError, AttributeError):
        return False
    return a_parts < b_parts


# The transcript scan behind fable_estimate() (0.23.0). A redraw never
# waits on it: it reads the last finished scan's totals and, when those are
# older than SCAN_REFRESH_S, starts ONE scan detached (tokens-since.py
# --background, under a lock file holding the scanner's pid and start time;
# a lock older than 2 minutes is taken over). The scan is incremental, so a
# refresh normally reads only what was appended in the last few seconds.
SCAN_REFRESH_S = 20
# The scan's age is shown on the bar only past this. Refreshes are started
# every SCAN_REFRESH_S and an incremental one finishes in well under a
# second, so a scan five minutes old means several refreshes in a row have
# failed to finish -- worth saying. Anything younger is the normal rhythm,
# and at a heavy session's pace a few minutes of lag moves the estimate by
# well under a point, so showing it would only be noise.
SCAN_AGE_SHOWN_S = 300
SCAN_WAIT_TIMEOUT_S = 120


def _scan_scripts_dir():
    return os.path.expanduser("~/.claude/scripts")


def _scan_wait_default():
    return os.environ.get("CLAUDE_USAGE_SCAN_WAIT", "").strip() not in ("", "0")


def _kick_scan(start):
    """Starts one detached background scan unless one is already running.
    The lock is taken here, before the spawn, so two redraws landing at the
    same moment start one scanner, not two. Never raises, never waits."""
    lock = os.path.join(_scan_scripts_dir(), "tokens-since.lock")
    now_ts = datetime.now(timezone.utc).timestamp()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            if now_ts - os.stat(lock).st_mtime < 120:
                return False
            os.unlink(lock)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return False
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": os.getpid(), "started_at": now_ts}))
        subprocess.Popen(
            [sys.executable, TOKENS_SINCE, "--background", "--lock-held", start],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        return True
    except Exception:
        try:
            os.unlink(lock)
        except OSError:
            pass
        return False


def scan_totals(start, now=None, wait=None):
    """(totals, scanned_at) for window `start` from the last finished scan,
    or (None, None) when none has finished yet. With wait (default
    CLAUDE_USAGE_SCAN_WAIT) the scan runs in this call first; without it a
    scan older than SCAN_REFRESH_S is started detached and never waited on."""
    if wait is None:
        wait = _scan_wait_default()
    if wait:
        try:
            totals = json.loads(subprocess.check_output(
                [sys.executable, TOKENS_SINCE, start], stderr=subprocess.DEVNULL,
                timeout=SCAN_WAIT_TIMEOUT_S))
            if isinstance(totals, dict):
                return totals, datetime.now(timezone.utc).timestamp()
        except Exception:
            pass
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    entry = None
    try:
        with open(os.path.join(_scan_scripts_dir(), "tokens-since-totals.json")) as f:
            entry = json.load(f)["starts"][start]
        totals, scanned_at = entry["totals"], float(entry["scanned_at"])
        if not isinstance(totals, dict):
            raise ValueError("totals")
    except Exception:
        totals, scanned_at = None, None
    if not wait and (scanned_at is None or now_ts - scanned_at > SCAN_REFRESH_S):
        _kick_scan(start)
    return totals, scanned_at


def _counting(tracked_model, next_reset):
    """No scan of this window has finished yet: nothing to project from,
    which is not the same as stale (see fable_estimate())."""
    return {"tracked_model": tracked_model, "stale": False, "counting": True,
            "pct": None, "resets_at": int(next_reset.timestamp()), "scanned_at": None}


def fable_estimate(now, current_resets_at=None, current_seven_day_pct=None, wait=None):
    """Returns the live weekly % for the per-model pool Anthropic's real
    rate_limits field doesn't break out (default: Fable) -- projected from a
    weekly $ cap derived at the last real calibration against
    claude.ai/settings/usage, against cost-weighted local usage since the
    window started (see tokens-since.py). This is the one number in the
    tool that isn't verified reported Anthropic data, so it always comes
    back with an explicit `stale` flag rather than a bare number -- callers
    must never present it as fact when stale. Returns None if it's never
    been calibrated at all.

    The local usage comes from the last FINISHED transcript scan, read from
    tokens-since.py's totals file (0.23.0; see scan_totals()). Until then
    the scan ran inside this call with a 5 second limit, and at a real
    week's size it needed longer, so every redraw waited the full 5 seconds
    and then reported "stale" -- and Claude Code, which cancels a redraw
    still running when the next one is due, rarely got to draw at all. Now
    a scan older than SCAN_REFRESH_S is started detached and this call
    returns at once with the last finished one. A late or failed scan is
    not staleness: the estimate keeps projecting from the last finished
    scan, and `scanned_at` says how old it is. Only when no scan of this
    window has ever finished (a fresh install, the first seconds of a new
    week) is there nothing to project from; that comes back as
    `counting: True`, never as `stale`. `wait=True` (or
    CLAUDE_USAGE_SCAN_WAIT=1) counts in this call instead, for callers
    that have time: the background watcher and tests.

    Unlike the 5h/weekly-all numbers (which come free from rate_limits on
    every render), this needs the cap to have been derived at least once
    from a real, non-zero settings-page read. Once that's done, the %
    itself updates live every call as local usage accrues -- no further
    browser reads needed except to occasionally re-verify the cap hasn't
    drifted (see CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS), and the weekly window advances on its own
    at the real reset boundary (analytically, from rate_limits' own
    resets_at when available) -- also no browser read needed.

    Before a cap has ever been derived (only possible via a non-zero real
    read -- see usage-calibrate-fable.py), this does NOT report `stale`:
    the same graceful-fallback principle as the 5h/weekly-all fix in
    v0.3.2 (show the last known real number instead of an alarming
    "unavailable" whenever something honest is already known, rather than
    disappointing a user with an error state that isn't one). A window
    that has since rolled over starts fresh at 0% by definition; otherwise
    the last real read on file (necessarily 0%, since that's the only way
    a cap couldn't be derived) is shown plainly. `stale` is reserved for
    genuine drift risk once a cap *does* exist -- too old to trust, or a
    live projection so far past it that the cap itself is suspect --
    because relaxing those the same way would resurrect the exact
    silent-drift bug (showing 99% when the real number was 0%) v0.4.1 was
    built to catch.

    `current_seven_day_pct` (optional -- the real, free aggregate weekly %
    from the same rate_limits payload/cache the caller already has) is the
    fast half of that same guarantee: the max-age ceiling alone only forces a
    re-read after it's had hours to go stale. Comparing against the
    aggregate catches drift the moment it happens -- if real account-wide
    usage has moved more than CLAUDE_USAGE_FABLE_DRIFT_THRESHOLD points since this
    calibration, something happened that the local-only projection may not
    have seen (e.g. the tracked model used outside this CLI), so report
    stale immediately rather than keep projecting a number that's already
    known to be behind reality."""
    if not os.path.exists(FABLE_CAL_PATH):
        return None
    try:
        with open(FABLE_CAL_PATH) as f:
            cal = json.load(f)
        next_reset = datetime.fromisoformat(cal["next_reset"])
    except Exception:
        return None

    tracked_model = cal["tracked_model"]

    # A calibration whose own next_reset predates its own calibrated_at is
    # internally impossible -- calibration always anchors to the *upcoming*
    # boundary -- and means it was written against a cached resets_at that had
    # already rolled over (see the roll-forward guard in
    # usage-calibrate-fable.py for how that happened live on 2026-09-05). Its
    # window_start, tokens_at_cal and therefore its cap all cover the wrong
    # week, so projecting the current window against that cap produces a
    # confidently wrong number rather than an obviously broken one (a real 81%
    # rendered as 18%). That is precisely what `stale` exists to prevent, so
    # report it and let the auto-recalibration replace the file instead of
    # trusting a cap known to be derived from mismatched inputs. Checked here
    # while next_reset still holds the calibration's own value, before the
    # advance below overwrites it with the live boundary.
    try:
        if datetime.fromisoformat(cal["calibrated_at"]) > next_reset:
            return {"tracked_model": tracked_model, "stale": True}
    except Exception:
        pass

    # Advance the window to the real current reset boundary first, before
    # branching on cap state, so every case below reasons about the
    # *current* window rather than a stale one. No browser read needed:
    # prefer rate_limits' own resets_at (ground truth -- the tracked
    # model's pool resets in lockstep with the all-models weekly) when
    # available, else step forward in 7-day increments from the last known
    # boundary.
    if current_resets_at is not None:
        next_reset = datetime.fromtimestamp(current_resets_at, tz=timezone.utc)
    # Roll a boundary that has already passed forward, whatever its source.
    # rate_limits' own resets_at is ground truth only for as long as the
    # payload carrying it is current, and it isn't always: the shared cache is
    # written by every open session's statusline render, and a long-idle
    # session keeps re-rendering the last payload it ever received (found live
    # 2026-09-06: a session still reporting a weekly boundary of 09-05T09:00Z
    # and a 5h boundary of 09-02T14:10Z, both long past). Taken verbatim, a
    # boundary in the past puts window_start a whole week early and the
    # projection sums an extra week of tokens -- the same 81%/18% flip-flop
    # (depending on which session wrote the cache last) that the roll-forward
    # in usage-calibrate-fable.py exists to stop on the derivation side. A
    # past boundary is never the *current* window's boundary, so both paths
    # advance the same way; when the cached resets_at is fresh (the normal
    # case) this loop is a no-op and nothing changes.
    while now > next_reset:
        next_reset += timedelta(days=7)
    window_start = next_reset - timedelta(days=7)
    rolled_over = window_start.isoformat() != cal.get("window_start")

    cap = cal.get("cap")
    cap_derived_at_raw = cal.get("cap_derived_at")

    if not cap or not cap_derived_at_raw:
        # No cap ever derived -- only possible when every real read so far
        # landed at 0% (or a fresh install). 0% is honest exactly as long
        # as local transcripts still show zero tracked usage this window;
        # the moment any appears there's nothing to project it against, so
        # report stale to trigger the auto-recalibration that derives the
        # cap from a real non-zero read. Without this check, the friendly
        # 0% would sit frozen while real usage climbed -- the same freeze
        # bug this model was built to kill, in friendlier clothes.
        tokens, scanned_at = scan_totals(window_start.isoformat(), now, wait=wait)
        if tokens is None:
            return _counting(tracked_model, next_reset)
        tracked_now = sum(v for k, v in tokens.items() if tracked_model.lower() in k.lower())
        if not rolled_over and tracked_now > cal.get("tokens_at_cal", 0):
            return {"tracked_model": tracked_model, "stale": True}
        if rolled_over and tracked_now > 0:
            return {"tracked_model": tracked_model, "stale": True}
        return {
            "tracked_model": tracked_model,
            "stale": False,
            "pct": 0 if rolled_over else cal.get("pct", 0),
            "resets_at": int(next_reset.timestamp()),
            "scanned_at": scanned_at,
        }

    try:
        cap_derived_at = datetime.fromisoformat(cap_derived_at_raw)
    except Exception:
        return {"tracked_model": tracked_model, "stale": True}
    if now - cap_derived_at > _cap_max_age():
        return {"tracked_model": tracked_model, "stale": True}

    tokens, scanned_at = scan_totals(window_start.isoformat(), now, wait=wait)
    if tokens is None:
        return _counting(tracked_model, next_reset)

    # The drift tripwire, sharpened (v0.8.3): a raw |agg_now - agg_at_cal|
    # threshold conflates two very different things -- aggregate movement
    # from ordinary CLI usage (fully visible to the local projection, proves
    # nothing) and movement from usage somewhere the projection can't see
    # (the entire point). A threshold loose enough to not false-positive on
    # a heavy CLI day was therefore too loose to catch real hidden drift
    # quickly. Fix: subtract the movement local usage already explains, and
    # trip only on what's left. The aggregate cap needed for that conversion
    # is derived at calibration time from the same snapshot
    # (local_total_at_cal / seven_day_pct_at_cal) -- an *underestimate*
    # whenever pre-calibration usage happened off this CLI (real usage >=
    # local usage for the same reported %), which overestimates the
    # explained share and undertrips slightly; the max-age ceiling remains
    # the unconditional backstop for that residual. Old calibration files
    # without the new field fall back to the raw diff at the old looser
    # threshold until one recalibration upgrades them.
    seven_day_pct_at_cal = cal.get("seven_day_pct_at_cal")
    if not rolled_over and current_seven_day_pct is not None and seven_day_pct_at_cal is not None:
        agg_delta = current_seven_day_pct - seven_day_pct_at_cal
        local_total_at_cal = cal.get("local_total_at_cal")
        if local_total_at_cal and seven_day_pct_at_cal > 0:
            agg_cap_est = local_total_at_cal / (seven_day_pct_at_cal / 100)
            local_delta = max(0.0, sum(tokens.values()) - local_total_at_cal)
            explained = 100 * local_delta / agg_cap_est
            # Directional, not abs(): only the aggregate rising *more* than
            # local usage explains signals possible off-CLI use of the tracked
            # model (the real drift this guards). The opposite direction --
            # aggregate lagging what local predicts -- is just the coarse
            # integer % not having ticked up yet after ordinary CLI work, and
            # abs() was tripping stale on that constantly (the |0 - 4.8| = 4.8
            # false positive). The _cap_max_age() backstop still catches slow
            # hidden drift unconditionally, so nothing real slips through here.
            if agg_delta - explained > _fable_drift_threshold():
                return {"tracked_model": tracked_model, "stale": True}
        elif abs(agg_delta) > max(_fable_drift_threshold(), 5):
            return {"tracked_model": tracked_model, "stale": True}

    tracked_now = sum(v for k, v in tokens.items() if tracked_model.lower() in k.lower())
    # Anchor model (2026-09-19, see usage-calibrate-fable.py): inside the
    # window a calibration happened in, the last real reading IS the level
    # and the cap only projects growth since. A rolled-over window has no
    # reading yet, so it projects from zero with the last known slope.
    if (cal.get("model") == "anchor-v2" and not rolled_over
            and cal.get("pct") is not None and cal.get("tokens_at_cal") is not None):
        grown = max(0.0, tracked_now - float(cal["tokens_at_cal"]))
        pct = float(cal["pct"]) + 100 * grown / cap
    else:
        pct = 100 * tracked_now / cap

    if pct > PROJECTION_CEILING:
        # The cap itself has likely drifted from reality (Anthropic changed
        # the limit, or the derivation was off) -- don't present a number
        # nobody would believe.
        return {"tracked_model": tracked_model, "stale": True}

    return {
        "tracked_model": tracked_model,
        "stale": False,
        "pct": min(pct, 150),
        "resets_at": int(next_reset.timestamp()),
        "scanned_at": scanned_at,
    }


FABLE_STALE_STATE_PATH = os.path.expanduser("~/.claude/scripts/fable-stale-state.json")
LIVE_CACHE_PATH = os.path.expanduser("~/.claude/scripts/usage-live.json")
FABLE_FORCE_RECAL_PATH = os.path.expanduser("~/.claude/scripts/fable-force-recal.json")
FABLE_SESSION_USAGE_PATH = os.path.expanduser("~/.claude/scripts/fable-session-usage.json")


def fable_mark_session_used(session_id, now):
    """Records that THIS session has actually dispatched a Fable agent at
    least once. Used to gate the mid-session staleness nudge below so it
    only fires in sessions that are actually working with Fable -- found
    live 2026-08-24: an 11-day maxwell-training monitoring session that
    never once dispatched Fable was repeatedly interrupted by "Fable went
    stale, recalibrate" nudges purely on the blind time/drift schedule,
    forcing a full browser round-trip for a number nothing in that session
    needed. The event-driven path (fable_force_recal_pending, tied to a
    real dispatch) already does the right thing; this fixes the *other*
    path (fable_stale_to_announce) to respect the same principle."""
    if not session_id:
        return
    usage = {}
    if os.path.exists(FABLE_SESSION_USAGE_PATH):
        try:
            with open(FABLE_SESSION_USAGE_PATH) as f:
                usage = json.load(f)
        except Exception:
            usage = {}
    usage[session_id] = now.isoformat()
    # Prune old sessions same as the stale-state file, so this doesn't grow
    # forever.
    cutoff = now - THEME_STATE_MAX_AGE
    usage = {
        sid: ts for sid, ts in usage.items()
        if _safe_parse_iso(ts, now) > cutoff
    }
    os.makedirs(os.path.dirname(FABLE_SESSION_USAGE_PATH), exist_ok=True)
    with open(FABLE_SESSION_USAGE_PATH, "w") as f:
        json.dump(usage, f)


def fable_session_has_used(session_id):
    if not session_id or not os.path.exists(FABLE_SESSION_USAGE_PATH):
        return False
    try:
        with open(FABLE_SESSION_USAGE_PATH) as f:
            usage = json.load(f)
        return session_id in usage
    except Exception:
        return False


def _safe_parse_iso(ts, fallback_now):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return fallback_now


def fable_mark_used(now, context="agent-dispatch"):
    """Ties recalibration to actual Fable usage instead of the blind
    time/drift schedule below: called by fable-agent-posttooluse-hook.py the
    moment an Agent tool call explicitly dispatches model="fable", so the
    very next prompt forces an immediate, silent gauge-calibrate regardless
    of where the max-age/drift clock currently sits. That schedule still
    exists as a backstop for Fable usage this CLI never sees (web/mobile),
    just loosened -- see CLAUDE_USAGE_FABLE_MAX_CAL_AGE_HOURS /
    CLAUDE_USAGE_FABLE_DRIFT_THRESHOLD in the config file."""
    os.makedirs(os.path.dirname(FABLE_FORCE_RECAL_PATH), exist_ok=True)
    with open(FABLE_FORCE_RECAL_PATH, "w") as f:
        json.dump({"marked_at": now.isoformat(), "context": context}, f)


def fable_force_recal_pending():
    if not os.path.exists(FABLE_FORCE_RECAL_PATH):
        return False
    try:
        with open(FABLE_FORCE_RECAL_PATH) as f:
            json.load(f)
        return True
    except Exception:
        return False


def fable_clear_force_recal():
    try:
        os.remove(FABLE_FORCE_RECAL_PATH)
    except FileNotFoundError:
        pass


def _load_fable_stale_state():
    if not os.path.exists(FABLE_STALE_STATE_PATH):
        return {}
    try:
        with open(FABLE_STALE_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_fable_stale_state(state, now):
    # Same pruning shape as the theme-drift state file below -- an abandoned
    # or killed session's row shouldn't accumulate forever.
    cutoff = now - THEME_STATE_MAX_AGE
    pruned = {
        sid: entry for sid, entry in state.items()
        if datetime.fromisoformat(entry.get("last_seen", now.isoformat())) > cutoff
    }
    os.makedirs(os.path.dirname(FABLE_STALE_STATE_PATH), exist_ok=True)
    with open(FABLE_STALE_STATE_PATH, "w") as f:
        json.dump(pruned, f)


def fable_stale_to_announce(session_id, now):
    """Mid-session counterpart to the `SessionStart` staleness nudge in
    usage-session-hook.py: that one only fires once, at a session's launch,
    so a session that runs long enough to cross the staleness threshold
    (max-age or the drift tripwire) partway through would otherwise
    sit stale for the rest of that session with nothing to prompt a fix.
    Called from the UserPromptSubmit hook so it re-checks on every prompt --
    same shape as theme_drift_to_announce() below, for the same reason
    (Claude Code has no way to re-fire SessionStart mid-session).

    Dedups per session against the *calibration's own identity*
    (`calibrated_at`) so a given stale calibration is announced to a given
    session at most once, not spammed every prompt while nothing has
    changed. The moment a fresh calibration lands -- via this hook's own
    nudge succeeding, the SessionStart hook, or a manual /gauge-calibrate --
    the identity changes, so a genuinely new future stale episode re-arms
    cleanly rather than staying suppressed. Returns the tracked model name
    (e.g. "fable") when there's something new to announce, else None."""
    if not session_id or not os.path.exists(LIVE_CACHE_PATH):
        return None
    if not fable_session_has_used(session_id):
        # This session has never dispatched Fable -- the blind time/drift
        # schedule isn't a reason to nag it mid-session. If it later does
        # dispatch Fable, fable_mark_session_used() flips this on and the
        # event-driven path (fable_force_recal_pending) already forces an
        # immediate recalibrate right after that dispatch anyway.
        return None
    try:
        with open(LIVE_CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:
        return None

    fable = fable_estimate(now, cache.get("seven_day_resets_at"), cache.get("seven_day_pct"))
    if fable and fable.get("counting"):
        return None  # nothing known yet either way; leave the dedup state alone
    state = _load_fable_stale_state()

    if not fable or not fable.get("stale"):
        # Genuinely fine right now -- clear any leftover dedup entry so a
        # *future* stale episode (even the same calibration somehow going
        # stale again) isn't suppressed by a marker from a resolved one.
        if session_id in state:
            del state[session_id]
            _save_fable_stale_state(state, now)
        return None

    identity = None
    if os.path.exists(FABLE_CAL_PATH):
        try:
            with open(FABLE_CAL_PATH) as f:
                identity = json.load(f).get("calibrated_at")
        except Exception:
            pass

    entry = state.get(session_id, {})
    entry["last_seen"] = now.isoformat()
    if entry.get("announced_for") == identity:
        state[session_id] = entry
        _save_fable_stale_state(state, now)
        return None

    entry["announced_for"] = identity
    state[session_id] = entry
    _save_fable_stale_state(state, now)
    return fable["tracked_model"]


def fable_stale_elapsed(cache, now):
    """How long the *current* stale episode has run, for the statusline to
    decide whether the calm "refreshes next msg!" label is still honest or
    whether the auto-heal has demonstrably missed its window and a louder
    nudge is owed (see fable_estimate()'s docstring on the CLI-blind-spot
    drift this exists to catch, and _cap_max_age() for the grace window).

    Keyed to the calibration's own identity (calibrated_at), same pattern as
    fable_stale_to_announce() above -- so a fresh calibration always resets
    the clock, even if the estimate immediately goes stale again for some
    other reason (e.g. drift), rather than inheriting a stale-since from a
    now-irrelevant prior episode. Mutates `cache` in place (fable_stale_since
    / fable_stale_identity) so the caller's existing cache-write covers this
    too; caller is responsible for clearing both fields once the estimate is
    healthy again, so a resolved episode doesn't leave a stale clock ticking
    for the next one to inherit by accident."""
    identity = None
    if os.path.exists(FABLE_CAL_PATH):
        try:
            with open(FABLE_CAL_PATH) as f:
                identity = json.load(f).get("calibrated_at")
        except Exception:
            pass

    if cache.get("fable_stale_identity") != identity:
        cache["fable_stale_identity"] = identity
        cache["fable_stale_since"] = now.isoformat()

    try:
        since = datetime.fromisoformat(cache["fable_stale_since"])
    except Exception:
        since = now
        cache["fable_stale_since"] = now.isoformat()

    return now - since


UC_STATE_PATH = os.path.expanduser("~/.claude/scripts/ultracode-state.json")
# Rolling history of real per-run pool deltas, appended to by
# ultracode-mark.py's `off` action -- see ultracode_observed_cost() below for
# how this feeds back into readiness estimates.
UC_HISTORY_PATH = os.path.expanduser("~/.claude/scripts/ultracode-history.json")


def _uc_cost(name, default):
    # Lazy env reads, same rationale as the fable knobs above: consumers
    # import this module before load_env_file() runs, so module-level reads
    # would bake in defaults and leave the documented knobs dead.
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def current_session_id():
    """This process's own Claude Code session id, set by the CLI on every
    subprocess it spawns (hooks, the statusline command, and any shell a
    session's Bash tool runs) -- so it's available identically whether
    ultracode_state() is being read from inside the session that owns a
    marker or from a sibling session's statusline render."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def ultracode_state(now):
    """Reads the shared active-run marker (written by ultracode-mark.py when
    a session starts or finishes an orchestrated Workflow run). The marker
    file itself is machine-wide, not per-session -- one Workflow run anywhere
    spends from the same 5h/weekly quota every session shares -- but each
    reader still needs to know whether *it* is the owner, so callers don't
    address instructions like "run ultracode-mark.py off" at a session that
    didn't start the run. Returns None when idle. An active entry older than
    its TTL counts as expired, not active -- a crashed or killed session must
    never leave the gauge claiming a run is live forever. Expiry is judged
    read-side (nothing is repaired on disk) so every statusline render stays
    write-free."""
    if not os.path.exists(UC_STATE_PATH):
        return None
    try:
        with open(UC_STATE_PATH) as f:
            state = json.load(f)
        if not state.get("active"):
            return None
        since = datetime.fromisoformat(state["since"])
    except Exception:
        return None
    ttl = timedelta(hours=_uc_cost("CLAUDE_USAGE_UC_TTL_HOURS", 4))
    elapsed = now - since
    if elapsed > ttl or elapsed < timedelta(0):
        return None
    marker_session = state.get("session_id") or ""
    return {
        "since": since,
        "elapsed": elapsed,
        "reason": state.get("reason") or "",
        "session_id": marker_session,
        # A blank marker session id (pre-fix markers, or "on" run manually
        # without the env var present) can't be claimed by anyone -- treat
        # it as "not mine" everywhere rather than guessing, so it never
        # falsely lights up as this session's own run.
        "mine": bool(marker_session) and marker_session == current_session_id(),
    }


def ultracode_observed_cost(label, min_samples=3, max_samples=10):
    """Real observed per-run cost for one pool, in percentage points, derived
    from the rolling history ultracode-mark.py's `off` action appends to
    (UC_HISTORY_PATH) -- one entry per completed run, each carrying the
    actual pct delta measured across that run's on/off window for every pool
    that had a clean before/after snapshot (see ultracode-mark.py's `off`
    handler: a pool whose window rolled over mid-run, or whose on-time
    snapshot is missing, is recorded as null there rather than a misleading
    delta, so this never has to guess which numbers are trustworthy).

    Uses the median, not the mean, of up to the most recent `max_samples`
    non-null deltas for this pool -- a single unusually large or unusually
    small run (a one-off giant refactor, or a run cut short) shouldn't swing
    the whole estimate the way it would swing a mean, and real runs vary
    enough in shape that this is genuinely a fat-tailed distribution, not a
    tight bell curve a mean would represent well.

    `label` is matched to the history record's key the same way the pool is
    matched in `ultracode_readiness` below: "5h" -> five_hour_delta, "week"
    -> seven_day_delta, anything else is treated as a tracked-model name and
    matched against each record's own `tracked_model` field before pulling
    `tracked_delta` -- so if the tracked model ever changes (CLAUDE_USAGE_
    TRACK_MODEL edited, or a different model calibrated), history recorded
    under the old model's name is never silently averaged into the new
    model's estimate.

    Requires at least `min_samples` real data points before returning
    anything -- one or two runs is noise, not a trend, and a readiness
    verdict built on noise is worse than one built on the deliberately rough
    static default it falls back to. Returns None (not 0, not the default)
    whenever the history file is missing, unreadable, or thin, so the caller
    can fall back to the env-tunable CLAUDE_USAGE_UC_COST_* default exactly
    as it did before this existed -- a fresh install or a wiped history file
    degrades to the old static behavior, never to an unreliable number
    pretending to be observed."""
    if not os.path.exists(UC_HISTORY_PATH):
        return None
    try:
        with open(UC_HISTORY_PATH) as f:
            history = json.load(f)
    except Exception:
        return None
    if not isinstance(history, list):
        return None

    if label == "5h":
        key = "five_hour_delta"
    elif label == "week":
        key = "seven_day_delta"
    else:
        key = "tracked_delta"

    deltas = []
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        if key == "tracked_delta" and entry.get("tracked_model") != label:
            continue
        val = entry.get(key)
        if val is None:
            continue
        try:
            deltas.append(float(val))
        except (TypeError, ValueError):
            continue
        if len(deltas) >= max_samples:
            break

    if len(deltas) < min_samples:
        return None
    import statistics
    return statistics.median(deltas)


def ultracode_readiness(now, cache):
    """Verdict on whether one typical ultracode (multi-agent Workflow) run
    fits in the quota that's left. Judged per pool against a per-run cost
    estimate in percentage points of that pool, plus a reserve buffer so a
    run never lands exactly on 100%.

    Two ways that cost is derived, preferred in this order:

      1. **Real observed cost** (see `ultracode_observed_cost()` above) --
         the median of this pool's actual measured deltas across the last
         several completed runs. `ultracode-mark.py on` snapshots each
         pool's % and resets_at at the moment a run starts; `off` re-reads
         the cache and records how many points that pool actually moved
         (skipping any pool whose window rolled over mid-run, since the
         delta would then reflect a reset, not the run). This is the real
         thing this machine's runs actually cost, not a guess -- and it
         only gets more accurate as more runs get properly bracketed with
         on/off.
      2. **Static env-tunable default** -- used whenever real history is too
         thin (fewer than `ultracode_observed_cost`'s `min_samples`, default
         3 runs) or missing entirely (fresh install, wiped history file):

           CLAUDE_USAGE_UC_COST_5H       default 20   (5h block points)
           CLAUDE_USAGE_UC_COST_WEEK     default 6    (weekly points)
           CLAUDE_USAGE_UC_COST_TRACKED  default 8    (tracked-model weekly points)

         Deliberately rough -- exists to catch the obvious cases (plenty of
         room vs. clearly about to cap), not to model a specific workflow,
         until enough real runs have accumulated to replace it.

    CLAUDE_USAGE_UC_BUFFER (default 3) is the reserve kept on every pool on
    top of the assumed cost, regardless of which of the two sources above
    supplied it.

    Returns None when no pool data is cached at all, else one of:

      {"verdict": "wait", "blockers": [labels], "until": epoch|None}

        when at least one pool doesn't have enough headroom left for one
        more run plus the buffer -- `until` is the latest reset among
        blocked pools (when every blocked pool reports one), i.e. when the
        answer flips back to ok; or

      {"verdict": "ok", "blockers": [], "until": None,
       "marginal": bool, "margin_notes": [...]}

        when every pool has room. `marginal` / `margin_notes` are an
        *additive* signal layered on top of a still-genuinely-affordable
        "ok" -- never a third verdict value, so every existing
        `readiness["verdict"] == "ok"` check across the codebase keeps
        working unchanged. A pool lands in `margin_notes` for one of two
        reasons (CLAUDE_USAGE_UC_MARGIN / _RESET_SOON / _RESET_SOON_PCT):
        "thin" when the headroom that would remain *after* one more run
        (`(100-pct) - cost`) falls under the margin, with `headroom_after`
        attached; or "reset_soon" when the pool's own reset is imminent AND
        it already carries meaningful usage (an unused pool gains nothing
        from rolling over early, so it's never flagged), with `resets_at`
        attached. A pool that trips both conditions reports only "thin" --
        it's the more directly actionable of the two numbers, and one
        reason per pool keeps the output terse."""
    buffer = _uc_cost("CLAUDE_USAGE_UC_BUFFER", 3)
    pools = []
    if "five_hour_pct" in cache:
        cost = ultracode_observed_cost("5h")
        if cost is None:
            cost = _uc_cost("CLAUDE_USAGE_UC_COST_5H", 20)
        pools.append(("5h", cache["five_hour_pct"], cache.get("five_hour_resets_at"), cost))
    if "seven_day_pct" in cache:
        cost = ultracode_observed_cost("week")
        if cost is None:
            cost = _uc_cost("CLAUDE_USAGE_UC_COST_WEEK", 6)
        pools.append(("week", cache["seven_day_pct"], cache.get("seven_day_resets_at"), cost))
    tracked = cache.get("fable_tracked_model")
    if tracked and "fable_pct" in cache:
        # A slightly-stale tracked % beats ignoring the pool entirely, same
        # trade the watcher's threshold checks already make.
        cost = ultracode_observed_cost(tracked)
        if cost is None:
            cost = _uc_cost("CLAUDE_USAGE_UC_COST_TRACKED", 8)
        pools.append((tracked, cache["fable_pct"], cache.get("fable_resets_at"), cost))
    if not pools:
        return None

    blockers, until, until_known = [], None, True
    for label, pct, resets_at, cost in pools:
        if resets_at is not None and (resets_at - now.timestamp()) <= 0:
            continue  # window already rolled over server-side; not a blocker
        if (100 - pct) < (cost + buffer):
            blockers.append(label)
            if resets_at is None:
                until_known = False
            elif until is None or resets_at > until:
                until = resets_at
    if not blockers:
        margin = _uc_cost("CLAUDE_USAGE_UC_MARGIN", 10)
        reset_soon_s = _uc_cost("CLAUDE_USAGE_UC_RESET_SOON", 600)
        reset_soon_pct = _uc_cost("CLAUDE_USAGE_UC_RESET_SOON_PCT", 15)
        margin_notes = []
        for label, pct, resets_at, cost in pools:
            if resets_at is not None and (resets_at - now.timestamp()) <= 0:
                continue  # rolled over server-side; not a signal
            headroom_after = (100 - pct) - cost
            if headroom_after < margin:
                margin_notes.append({"pool": label, "reason": "thin",
                                      "headroom_after": headroom_after})
            elif resets_at is not None:
                secs_left = resets_at - now.timestamp()
                if 0 < secs_left <= reset_soon_s and pct >= reset_soon_pct:
                    margin_notes.append({"pool": label, "reason": "reset_soon",
                                          "resets_at": resets_at})
        return {"verdict": "ok", "blockers": [], "until": None,
                "marginal": bool(margin_notes), "margin_notes": margin_notes}
    return {"verdict": "wait", "blockers": blockers, "until": until if until_known else None}


def _uc_margin_suffix(margin_notes, now):
    """Terse tag list for statusline-style renders, e.g. '5h thin, week in
    8m'. Empty string when margin_notes is empty."""
    parts = []
    for note in margin_notes:
        if note["reason"] == "thin":
            parts.append(f"{note['pool']} thin")
        else:  # "reset_soon"
            delta = fmt_delta(note["resets_at"], now)
            parts.append(f"{note['pool']} in {delta}" if delta else f"{note['pool']} thin")
    return ", ".join(parts)


def _uc_margin_context_clause(margin_notes, now):
    """Fuller clause for the SessionStart hook sentence, e.g.
    'week only ~6pts left after; 5h resets in 8m anyway'."""
    parts = []
    for note in margin_notes:
        if note["reason"] == "thin":
            parts.append(f"{note['pool']} only ~{round(note['headroom_after'])}pts left after")
        else:  # "reset_soon"
            delta = fmt_delta(note["resets_at"], now)
            parts.append(f"{note['pool']} resets in {delta} anyway" if delta else f"{note['pool']} margin thin")
    return "; ".join(parts)


def fmt_ultracode(state, readiness, now):
    """The statusline segment: the active marker wins (an in-flight run is
    the fact worth showing; affordability of a *second* run is nobody's
    question), else the readiness verdict, else nothing. A marker owned by
    a *different* session is invisible here, same as idle -- each session's
    gauge stays specific to itself; the quota it actually costs still shows
    up organically in the readiness verdict below (real live %), so nothing
    is hidden, just not narrated as a foreign event on this session's own
    line (found live 2026-08-09: an earlier version showed a dim "uc: ON
    elsewhere" here, which Rajan didn't want -- a session's indication
    should read as its own status, not a feed of what other sessions are
    doing)."""
    if state and state["mine"]:
        mins = int(state["elapsed"].total_seconds() // 60)
        return f"uc: ON {mins}m"
    if not readiness:
        return None
    if readiness["verdict"] == "ok":
        suffix = _uc_margin_suffix(readiness.get("margin_notes") or [], now)
        return "uc: ok" + (f" ({suffix})" if suffix else "")
    who = "+".join(readiness["blockers"])
    delta = fmt_delta(readiness["until"], now)
    return f"uc: wait {delta} ({who})" if delta else f"uc: wait ({who})"


# The 256-color ramp approximating the magenta→purple gradient Claude Code's
# own UI paints the "ultracode" keyword with. Rendered per-character, and
# phase-shifted by wall-clock across renders so the statusline version
# shimmers over time the way the CLI's animated keyword does (a statusline
# render is a still frame; the phase shift is what strings the frames into
# the animation).
_UC_GRADIENT = [213, 207, 201, 165, 129, 93, 99, 135]


def fmt_ultracode_styled(state, readiness, now):
    """The workload-line rendering of the ultracode indicator, sitting at the
    end of that line next to the swap marker (placement per Rajan,
    2026-08-08). Active runs get the full Claude-Code-style gradient text,
    bold -- the one genuinely loud state this bar has, for the one state
    that's actually burning quota. Idle states stay dim so they read as
    ambient info, same register as the resume hint. A marker owned by a
    different session is invisible here, same as idle -- each session's
    indicator reflects only its own run (found live 2026-08-09: Rajan
    didn't want a "some other session is active" note on a session that
    isn't itself doing anything)."""
    if state and state["mine"]:
        mins = int(state["elapsed"].total_seconds() // 60)
        text = f"⚡ultracode ON {mins}m"
        phase = int(now.timestamp() // 2) % len(_UC_GRADIENT)
        out = []
        for i, ch in enumerate(text):
            color = _UC_GRADIENT[(i + phase) % len(_UC_GRADIENT)]
            out.append(f"\033[1;38;5;{color}m{ch}")
        return "".join(out) + "\033[0m"
    if not readiness:
        return None
    if readiness["verdict"] == "ok":
        suffix = _uc_margin_suffix(readiness.get("margin_notes") or [], now)
        text = "uc ok" + (f" ({suffix})" if suffix else "")
        return f"\033[2m{text}\033[0m"
    who = "+".join(readiness["blockers"])
    delta = fmt_delta(readiness["until"], now)
    body = f"uc wait {delta} ({who})" if delta else f"uc wait ({who})"
    return f"\033[2m{body}\033[0m"


def ultracode_context(state, readiness, now):
    """The SessionStart-hook sentence. Informational by default; when the
    machine's owner has opted in with CLAUDE_USAGE_UC_AUTO=1 (a standing,
    user-granted authorization recorded in their own config file), it also
    carries the auto-orchestration directive so a session knows it may reach
    for the Workflow tool on its own judgment -- and how to flip the gauge's
    active marker so the bar reflects reality.

    The marker file is machine-wide (one shared quota pool), but a marker
    owned by a *different* session (state["mine"] is False) is treated as
    if idle here, same as the statusline -- this session's own context stays
    specific to itself rather than narrating what another session is doing
    (found live 2026-08-09: an earlier version surfaced an "another session
    has a run" line here, which Rajan didn't want). The real cost of that
    other run still shows up on its own, honestly, in the readiness verdict
    below (live %), so nothing is actually hidden -- just not called out as
    a foreign event on a session that isn't the one spending it. Only the
    owning session ever sees the "turn it off when it's done" instruction,
    since only it has any basis to judge whether the run is actually
    finished."""
    auto = os.environ.get("CLAUDE_USAGE_UC_AUTO", "") == "1"
    mark = "python3 ~/.claude/scripts/ultracode-mark.py"
    if state and state["mine"]:
        mins = int(state["elapsed"].total_seconds() // 60)
        reason = f" (reason: {state['reason']})" if state["reason"] else ""
        return (
            f"ultracode: marked ACTIVE {mins}m ago{reason} -- an orchestrated "
            f"Workflow run is (or was) in flight. If it has finished, run "
            f"`{mark} off` so the gauge stops showing it."
        )
    if not readiness:
        return None
    if readiness["verdict"] == "ok":
        if readiness.get("marginal"):
            clause = _uc_margin_context_clause(readiness["margin_notes"], now)
            base = (f"ultracode budget: ok, but thin: {clause}. Weigh whether this run "
                     "is worth it now vs waiting -- gauge can't see task size, use judgment too.")
        else:
            base = (
                "ultracode budget: ok (one typical multi-agent Workflow run fits "
                "in every pool's remaining quota)."
            )
        line = (
            f"{base} Whenever this session launches "
            "a Workflow run for ANY reason (ultracode keyword, session "
            f"toggle, or explicit ask), run `{mark} on --reason '<short "
            f"task>'` first and `{mark} off` when it finishes -- that's what "
            "makes the statusline gauge show the run as active; the harness "
            "never flips it for you."
        )
        if auto:
            line += (
                " Standing auto-mode is also ON (user-granted, in this "
                "machine's claude-quota-gauge config): when a task clearly "
                "warrants multi-agent orchestration, use the Workflow tool on "
                "your own judgment without waiting for the keyword -- same "
                "marking discipline."
            )
        return line
    who = "+".join(readiness["blockers"])
    delta = fmt_delta(readiness["until"], now)
    when = f" -- clears in {delta}" if delta else ""
    line = f"ultracode budget: tight on {who}{when}"
    if auto:
        line += ". Auto-mode is ON but budget-gated: do NOT start a Workflow run on your own judgment until this clears (explicit user request still overrides)."
    return line


THEME_STATE_PATH = os.path.expanduser("~/.claude/scripts/theme-state.json")
# How long a session's drift-tracking entry survives with no prompts
# touching it -- long enough to outlive a normal Claude Code session, short
# enough that abandoned/killed sessions don't pile up in the state file
# forever.
THEME_STATE_MAX_AGE = timedelta(hours=24)


def theme_watch_enabled():
    """Off by default -- this whole feature is a macOS-only `defaults read`
    dependency, exactly the kind of fragile platform-specific add-on that
    shouldn't be forced on every user of this cross-platform tool. Opt in
    per-machine via CLAUDE_USAGE_THEME_WATCH=1 in
    ~/.claude/claude-quota-gauge.env."""
    return os.environ.get("CLAUDE_USAGE_THEME_WATCH") == "1"


def os_appearance():
    """Returns "dark" / "light" for the current macOS system appearance, or
    None on any non-macOS platform or read failure -- callers must treat
    None as "can't tell, don't report drift" rather than assuming a value.
    There is no supported way for Claude Code itself to expose this (the
    statusLine JSON schema has no theme/appearance field), so this reads
    the same OS-level source of truth a human would check by eye."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    # Exit code is nonzero with empty stdout when the key is simply absent --
    # that's the normal, expected way macOS represents "light mode", not an
    # error. Anything else unreadable stays None (unknown) rather than
    # guessing.
    if out.returncode == 0 and "dark" in out.stdout.lower():
        return "dark"
    if out.returncode != 0 and not out.stdout.strip():
        return "light"
    return None


def _load_theme_state():
    if not os.path.exists(THEME_STATE_PATH):
        return {}
    try:
        with open(THEME_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_theme_state(state, now):
    # Prune entries this stale so a long-lived machine doesn't accumulate one
    # row per session forever.
    cutoff = now - THEME_STATE_MAX_AGE
    pruned = {
        sid: entry for sid, entry in state.items()
        if datetime.fromisoformat(entry.get("last_seen", now.isoformat())) > cutoff
    }
    os.makedirs(os.path.dirname(THEME_STATE_PATH), exist_ok=True)
    with open(THEME_STATE_PATH, "w") as f:
        json.dump(pruned, f)


def _theme_state_entry(session_id, now):
    """Loads (state, entry) for a session, creating a fresh entry baselined
    to the current OS appearance on first sight -- that appearance is what
    this session's theme actually resolved against at launch, since Claude
    Code queries it once at startup and never again (no settings.json
    hot-reload, no statusLine/hook field exposing the live resolved theme).
    Returns (state, entry, current_appearance), any of which may be None if
    theme-watch is off, session_id is missing, or the OS appearance can't be
    read right now."""
    if not theme_watch_enabled() or not session_id:
        return None, None, None
    current = os_appearance()
    if current is None:
        return None, None, None

    state = _load_theme_state()
    now_iso = now.isoformat()
    entry = state.get(session_id)
    if entry is None:
        entry = {"baseline": current, "announced_for": None, "first_seen": now_iso, "last_seen": now_iso}
        state[session_id] = entry
        _save_theme_state(state, now)
    return state, entry, current


def theme_drift_to_announce(session_id, now):
    """Reports whether the OS appearance has diverged from the appearance
    this session's theme actually resolved against at launch, for the
    UserPromptSubmit hook to relay into Claude's context -- entirely a
    background check, never surfaced in the visible statusline bar.

    Dedups so Claude is told about a given drift event exactly once (not
    spammed every prompt), tracked via `announced_for`; re-arms the moment
    the OS flips again, including flipping back and then away once more.
    The underlying baseline itself never moves mid-session -- there's no way
    to observe that `/config theme=auto` actually ran, so nothing here
    assumes it did. It only clears two honest ways: the OS appearance flips
    back to match what the theme actually launched under (genuinely no
    longer stale, so `announced_for` resets too), or the session restarts
    (fresh baseline captured at the new launch). Returns None if
    theme-watch is off, session_id is missing, the OS appearance can't be
    read, or there's nothing new to announce."""
    state, entry, current = _theme_state_entry(session_id, now)
    if entry is None:
        return None

    entry["last_seen"] = now.isoformat()
    baseline = entry["baseline"]

    if baseline == current:
        entry["announced_for"] = None
        state[session_id] = entry
        _save_theme_state(state, now)
        return None

    if entry.get("announced_for") == current:
        state[session_id] = entry
        _save_theme_state(state, now)
        return None

    entry["announced_for"] = current
    state[session_id] = entry
    _save_theme_state(state, now)
    return {"drifted": True, "from": baseline, "to": current}


def resolve_configured_model(cwd):
    """Best-effort lookup of the `model` setting governing this session
    (e.g. "opusplan"), checked project-local first then user-global -- the
    same precedence Claude Code itself uses, minus the CLI-flag/env-var
    layers this script has no visibility into. Returns None if unset
    anywhere, in which case the caller just shows the live model as-is."""
    candidates = []
    if cwd:
        candidates.append(os.path.join(cwd, ".claude", "settings.local.json"))
        candidates.append(os.path.join(cwd, ".claude", "settings.json"))
    candidates.append(os.path.expanduser("~/.claude/settings.json"))
    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        if "model" in data:
            return data["model"]
    return None


def fmt_model(payload):
    """Formats the current session's active model + reasoning effort, e.g.
    'opusplan→Sonnet 5 (high)' or 'Fable 5 (xhigh/fast)'. Pulled straight
    from the statusLine stdin payload (real per-render data, same principle
    as the rate_limits numbers above) rather than inferred from settings
    alone -- opusplan mode alternates the live model between Opus (plan
    phase) and Sonnet (execution), so only the payload's own model.id/
    display_name reflects which one is actually active right now. The
    "opusplan" tag is layered on top from settings so it reads as a mode,
    not just whichever sub-model happens to be live at that instant.

    The prefix only applies when the live model is one opusplan can
    actually produce (Opus or Sonnet). A session-level /model override to
    anything else (e.g. Fable) leaves the settings pin in place but takes
    this session out of opusplan mode entirely -- the payload is the only
    place that override is visible, and without this check the bar renders
    an impossible "opusplan→Fable 5". An override to Opus or Sonnet
    themselves is indistinguishable from opusplan in the payload, so the
    prefix can still show in that narrow case."""
    model_info = payload.get("model") or {}
    display = model_info.get("display_name") or model_info.get("id")
    if not display:
        return None

    cwd = payload.get("cwd") or (payload.get("workspace") or {}).get("current_dir")
    configured = resolve_configured_model(cwd)
    model_key = f"{model_info.get('id') or ''} {display}".lower()
    in_opusplan = configured == "opusplan" and ("opus" in model_key or "sonnet" in model_key)
    label = f"opusplan→{display}" if in_opusplan else display

    tags = []
    effort = (payload.get("effort") or {}).get("level")
    if effort:
        tags.append(effort)
    if payload.get("fast_mode"):
        tags.append("fast")
    if tags:
        label += f" ({'/'.join(tags)})"
    return label


def fmt_delta(epoch_target, now):
    """Formats a countdown to a Unix epoch timestamp, computed fresh against
    `now` every call -- accurate even if the surrounding data was cached
    a few minutes ago, since resets_at is an absolute point in time. Returns
    None once the target has passed -- callers use that to switch to an
    explicit "resetting" state (see fmt_window) instead of a countdown that
    reads "now" forever."""
    if epoch_target is None:
        return None
    secs = int(epoch_target - now.timestamp())
    if secs <= 0:
        return None
    if secs < 120:
        # Sub-2-minute granularity so the last stretch before a reset counts
        # down visibly (e.g. "42s") instead of sitting on "0h 0m" for up to a
        # minute, which reads as a hang rather than an active countdown.
        return f"{secs}s"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h > 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    return f"{h}h {m}m"


def fmt_tokens(n):
    """Compact token count for the bar: 45000 -> '45k', 1250000 -> '1.2M'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def fmt_prompt_cache(pc, now):
    """Formats the session's prompt-cache state from Claude Code's
    `prompt_cache` payload object (v2.1.251+), e.g. 'cache: warm 91% (42m
    left)' or 'cache: cold (~45k to rewarm)'. Returns None when the field is
    absent (older CLI, or before the first API response) or when caching was
    never observed this session (provider/gateway not reporting it) -- a
    segment that can only ever say "off" is noise, not signal.

    Why it earns a slot on the bar: the 5h/weekly numbers say how much pool
    is left, but not how expensive the *next* prompt is. A cold cache means
    the whole prefix gets re-written on the next turn (`recache_tokens_if_
    cold`), which is real quota on a long session -- so "is it still warm,
    and how long for" is the one thing worth knowing before stepping away
    from a session and coming back to it later."""
    if not isinstance(pc, dict) or not pc.get("caching_observed"):
        return None
    if pc.get("warm"):
        bits = ["warm"]
        ratio = pc.get("hit_ratio")
        if isinstance(ratio, (int, float)):
            bits.append(f"{ratio * 100:.0f}%")
        text = "cache: " + " ".join(bits)
        left = fmt_delta(pc.get("expires_at"), now)
        if left:
            text += f" ({left} left)"
        return text
    tok = fmt_tokens(pc.get("recache_tokens_if_cold"))
    if tok and float(pc.get("recache_tokens_if_cold") or 0) > 0:
        return f"cache: cold (~{tok} to rewarm)"
    return "cache: cold"


def fmt_window(label, pct, resets_at, now, cached=False, note=None):
    """Formats one usage row, e.g. '5h: 18% (resets 4h 58m)'. Once resets_at
    has passed, the window has crossed its reset boundary server-side but a
    fresh reading hasn't landed yet (nothing refreshes the % until the next
    real rate_limits payload arrives) -- shown as an explicit 'refreshing...'
    state carrying the last known % instead of a stale countdown stuck at
    "now", which is indistinguishable from the tool having hung. Uses the
    same wording as the cached-state marker below -- both describe a % that
    hasn't caught up yet, and showing two different words for that read as
    inconsistent rather than as two meaningfully different states.

    `note` replaces the default cached-state marker with caller-specific
    wording -- the tracked-model row uses it to say *when* its refresh
    happens ("refreshes next msg") rather than the passive "refreshing…",
    which reads like something worth waiting for when the actual trigger is
    the user's own next message. Passing note implies the cached rendering."""
    if resets_at is not None and (resets_at - now.timestamp()) <= 0:
        return f"{label}: refreshing… (was {pct:.0f}%)"
    resets = fmt_delta(resets_at, now)
    if cached or note:
        marker = note or "refreshing…"
        tail = f" ({marker}), resets {resets}" if resets else f" ({marker})"
    else:
        tail = f" (resets {resets})" if resets else ""
    return f"{label}: {pct:.0f}%{tail}"


RIGHT_ALIGN_MARGIN = 4
# Claude Code's actual renderable row width runs a few columns short of the
# raw COLUMNS value it reports -- confirmed live: padding to exactly COLUMNS
# clipped exactly 4 characters off the trailing session id, so the
# statusline row itself reserves that much width, likely for its own UI
# border/padding (the docs' `padding` setting describes this as "the
# interface's built-in spacing", separate from anything a script controls).
# Set to the measured value rather than a rounder guess, since any less
# reproduces that exact clip and any more is just unused blank space.

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_len(s):
    """Length as it actually renders, ignoring ANSI color escapes -- the
    workload-gauge segment embeds `\\033[...m` codes (see workload-gauge.py's
    sc()) that count as characters to len() but draw zero columns, so any
    right_align() padding against raw len() on colored text comes up short
    and the right-hand cluster lands well short of the terminal edge."""
    return len(_ANSI_RE.sub("", s))


def right_align(left, right):
    """Right-justifies `right` against the live terminal width so it reads as
    its own cluster near the far edge of the bar (e.g. the session id)
    rather than just trailing immediately after the last `|`-joined segment
    on the left. Claude Code sets the COLUMNS env var to the terminal's
    current width before running the statusLine command (v2.1.153+) -- this
    pads between `left` and `right` to fill it, short of the true edge by
    RIGHT_ALIGN_MARGIN (see above). When COLUMNS isn't set this render (found
    live, 2026-07-29: it varies per session/render context -- e.g. a
    background-monitored session's statusLine invocation may not get it the
    same way an actively-focused terminal's does), falls back to
    _tty_columns() -- a direct query of the controlling terminal device,
    same fallback right_align_solo() already uses -- before finally
    dropping to a plain `left | right` join if neither source panned out
    (very old Claude Code, or truly no accessible terminal at all).

    Measures both sides with visible_len() rather than len() so this works
    whether `left` carries ANSI color codes (the workload segment) or not
    (the plain usage line) without the caller needing to know or care."""
    if not right:
        return left
    if not left:
        return right
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = None
    if not columns:
        columns = _tty_columns()
    if columns:
        pad = columns - RIGHT_ALIGN_MARGIN - visible_len(left) - visible_len(right)
        if pad >= 1:
            return left + " " * pad + right
    return f"{left} | {right}"


def _tty_columns():
    """Best-effort real terminal width straight from the controlling
    terminal device, independent of whether Claude Code passes COLUMNS.
    A subprocess normally inherits its parent's controlling terminal even
    when its own stdout is redirected/piped (as it is here -- Claude Code
    captures this script's stdout to parse it, so sys.stdout is never
    itself a tty), which is exactly the gap COLUMNS-only alignment fell
    into: found live (2026-07-29) that COLUMNS isn't reliably set on every
    render. Returns None on any failure (no controlling terminal at all --
    cron, a test harness, etc.), never raises."""
    try:
        fd = os.open(os.ctermid(), os.O_RDONLY)
        try:
            return os.get_terminal_size(fd).columns
        finally:
            os.close(fd)
    except OSError:
        return None


# Root cause, confirmed against Claude Code's actual source (2026-07-29,
# third round -- Fable traced this in the CLI binary, not inferred):
# statusLine output is rendered through an Ink virtual-component tree, not
# passed to a raw terminal. Two unconditional mechanisms sit between this
# script's stdout and the display, neither configurable:
#   (a) `s.stdout.trim().split('\n').flatMap(c => c.trim() || []).join('\n')`
#       -- every line individually .trim()'d. Kills literal leading spaces
#       outright (attempt 1 -- proven live: padding was really in the
#       bytes, rendered flush left anyway).
#   (b) the surviving text is fed through a real stateful ANSI parser
#       (`Xho().feed()`) whose event loop only handles "text" and "link"
#       (OSC-8) event types. A cursor-positioning sequence like `\033[171G`
#       IS recognized as a legitimate control sequence -- and then silently
#       dropped, since nothing consumes its event type. Kills cursor
#       positioning outright (attempt 2 -- proven live: correct column
#       values were computed and emitted, chip still rendered flush left).
# SGR color/style codes (`\033[1m`, `\033[48;5;Nm`, ...) are the only
# non-text things (b) preserves -- confirmed by the chip's background color
# rendering correctly both attempts.
#
# The fix: (a) only strips from a string's own two ends, stopping at the
# first non-whitespace byte -- so a line that starts with a real ESC
# sequence is untouched from character 1 onward, and literal space
# characters *after* that point survive completely intact. This is exactly
# why the OLD shared-line design (gauge segment + padding + resume, one
# string) always worked: real visible content occupied both ends, so the
# padding in the middle was never near either edge .trim() touches. Solo
# lines need the same shape manufactured deliberately: a short anchor
# (itself starting with an ESC byte, so step (a) can't reach past it)
# before the padding, not the padding first.
_SOLO_ANCHOR = "\033[2m·\033[0m"  # dim middle dot -- small, doesn't compete
                                   # visually with the chip/resume content
                                   # it's marking the left edge of


def right_align_solo(text, anchor_width=None):
    """Right-pads a single piece of text for a line with nothing else on
    it (the resume command's row, the title chip's row -- see
    statusline.py) -- prefixed with `_SOLO_ANCHOR` so the padding survives
    Claude Code's per-line trim (see the block comment above `_SOLO_ANCHOR`
    for the full, source-verified reason this specific shape is required).

    Width source, best available first: COLUMNS env var when Claude Code
    sets it this render, else the controlling terminal's real width via
    _tty_columns(), else `anchor_width` -- the widest line this script
    already rendered this call (see statusline.py). COLUMNS/tty is what
    actually reaches the true terminal edge and is confirmed live
    (2026-07-29, third round) to produce correct right-alignment;
    anchor_width alone (tried briefly as primary, round four, reverted)
    only reaches as far as this script's own short lines and visibly lands
    "in the middle" of a wide terminal instead of at the true right edge --
    it stays only as a fallback for the rare render where COLUMNS/tty are
    both unavailable.

    RIGHT_ALIGN_MARGIN (the measured UI-border correction) applies only
    when padding against a real terminal-width number -- irrelevant to the
    anchor_width case, which pads against a line we drew ourselves."""
    if not text:
        return ""
    glyph_w = visible_len(_SOLO_ANCHOR)
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = None
    if not columns:
        columns = _tty_columns()
    if columns:
        pad = columns - RIGHT_ALIGN_MARGIN - visible_len(text) - glyph_w
        if pad >= 1:
            return _SOLO_ANCHOR + " " * pad + text
    if anchor_width:
        pad = anchor_width - visible_len(text) - glyph_w
        if pad >= 1:
            return _SOLO_ANCHOR + " " * pad + text
    return _SOLO_ANCHOR + " " + text


# ---- session title chip ------------------------------------------------
# Claude Code generates a short AI title for each session and re-generates
# it as the task shifts (confirmed live: one session's title moved
# "Resolve app installation and sign-in issues for COBUX" ->
# "testflight-internal-external-switch" -> "cobux-2-0-defect-fixes" over
# its lifetime). It persists every version into the session's own
# transcript JSONL as {"type":"ai-title","aiTitle":"...","sessionId":"..."}
# (or "custom-title"/"customTitle" when set by hand) -- so the current
# title costs a file tail-read, not an LLM call. Claude Code's own UI
# renders this as a chip just above the statusline but only intermittently;
# reproducing it here, on the one surface that renders every single time,
# is what makes it "always show."

TITLE_STATE_PATH = os.path.expanduser("~/.claude/scripts/session-title-state.json")
TITLE_CHANGE_MARKER_WINDOW = timedelta(minutes=5)
_TITLE_TAIL_BYTES = 65536


def _tail_json_records(path, tail_bytes=_TITLE_TAIL_BYTES):
    """Yields parsed JSON objects from the last `tail_bytes` of a JSONL
    file, oldest to newest. Transcripts run to thousands of lines and the
    statusline re-renders roughly every 60s, so this never reads the whole
    file -- it seeks near the end and discards whatever partial line the
    seek landed inside of before parsing forward."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # discard the partial line the seek landed in
            for raw in f:
                try:
                    yield json.loads(raw)
                except Exception:
                    continue
    except Exception:
        return


def _first_prompt_text(path, max_chars=200):
    """Head-scan (not tail) for the session's first real user message --
    the fallback for a session too new to have any title record yet.
    Mirrors Claude Code's own firstPrompt fallback (m7t() in the bundled
    CLI): skip prompts that are themselves wrapped system content rather
    than something the user actually typed."""
    try:
        with open(path, "r", errors="replace") as f:
            for _ in range(200):  # a title/first prompt lands in the first
                                   # few dozen lines of any real session;
                                   # bail rather than scan forever
                line = f.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                content = (d.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = ""
                text = text.strip()
                if text and not text.startswith("<"):
                    return text[:max_chars]
    except Exception:
        pass
    return None


def session_title(transcript_path, session_id):
    """Resolves the same title Claude Code's own UI would show, in the same
    precedence order the CLI itself uses: a hand-set title > the
    AI-generated one > the first user prompt > the bare session id (so a
    brand-new, not-yet-titled session still shows something)."""
    if not transcript_path or not os.path.exists(transcript_path):
        return session_id[:8] if session_id else None

    custom = None
    ai = None
    for rec in _tail_json_records(transcript_path):
        t = rec.get("type")
        if t == "custom-title" and rec.get("customTitle"):
            custom = rec["customTitle"]
        elif t == "ai-title" and rec.get("aiTitle"):
            ai = rec["aiTitle"]

    title = custom or ai
    if not title:
        title = _first_prompt_text(transcript_path)
    if not title and session_id:
        title = session_id[:8]
    return title


def _load_title_state():
    if not os.path.exists(TITLE_STATE_PATH):
        return {}
    try:
        with open(TITLE_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_title_state(state, now):
    # Prune well past the marker's own window so a long-lived machine
    # doesn't accumulate one row per session forever -- same shape as
    # _save_theme_state() above.
    cutoff = now - TITLE_CHANGE_MARKER_WINDOW * 4
    pruned = {}
    for sid, entry in state.items():
        try:
            seen = datetime.fromisoformat(entry.get("changed_at", now.isoformat()))
        except Exception:
            continue
        if seen > cutoff:
            pruned[sid] = entry
    try:
        os.makedirs(os.path.dirname(TITLE_STATE_PATH), exist_ok=True)
        with open(TITLE_STATE_PATH, "w") as f:
            json.dump(pruned, f)
    except Exception:
        pass


def title_changed_recently(session_id, title, now):
    """Records this session's current title and reports whether it shifted
    within TITLE_CHANGE_MARKER_WINDOW -- drives the chip's brief '▸' marker
    (see title_chip()) so a task-shift that happened while Rajan was in
    another window is still visible when he looks back. Any read/write
    failure degrades to "no marker" rather than breaking the bar, matching
    the theme-state helpers' error handling."""
    if not session_id or not title:
        return False
    try:
        state = _load_title_state()
        entry = state.get(session_id)
        if entry and entry.get("title") == title:
            # Same title as last recorded -- only still worth marking if
            # THAT establishment was itself a real change (not the
            # session's first-ever sighting) and it's still inside the
            # window. is_change is stored on the entry itself rather than
            # re-derived here, because "was this a change" and "is this
            # call recent" are different questions -- collapsing them made
            # a second call with an unchanged title read (now - now) < 5min
            # as true and wrongly re-trigger the marker on every repeat
            # call for a title that was never a change to begin with.
            #
            # `last_seen` (added for colliding_sessions(), 2026-07-29) still
            # needs to advance here even though the title itself didn't
            # change -- otherwise a long-idle-but-open session with an
            # unchanged title would look "not live" forever after its first
            # render. Throttled to a write only every 60s+ (not every
            # render) so an open, actively-rendering session doesn't turn
            # this file into a write-on-every-keystroke log.
            last_seen = entry.get("last_seen")
            try:
                stale = not last_seen or (now - datetime.fromisoformat(last_seen)) > timedelta(seconds=60)
            except Exception:
                stale = True
            if stale:
                entry["last_seen"] = now.isoformat()
                state[session_id] = entry
                _save_title_state(state, now)
            if not entry.get("is_change"):
                return False
            changed_at = datetime.fromisoformat(entry["changed_at"])
            return (now - changed_at) < TITLE_CHANGE_MARKER_WINDOW
        # Title differs from what was last recorded, or this session hasn't
        # been recorded yet. Anchor a fresh changed_at either way -- but
        # only report an actual *change* worth marking when there was a
        # prior title to change from; a session's very first sighting isn't
        # a shift a human needs flagged.
        is_change = entry is not None
        state[session_id] = {
            "title": title, "changed_at": now.isoformat(), "is_change": is_change,
            "last_seen": now.isoformat(),
        }
        _save_title_state(state, now)
        return is_change
    except Exception:
        return False


def colliding_sessions(session_id, title, now, live_window=None):
    """Other currently-open sessions whose title exactly matches this one's
    -- the real signal worth acting on (see title_disambiguation() below),
    since a title being short/plain is fine as long as it's unique, and the
    actual problem Rajan hit was several concurrent `/afk` sessions all
    landing on the literal Claude-Code-generated title "AFK pre-flight
    check". "Currently open" is judged by `last_seen` (see
    title_changed_recently()) inside `live_window` -- without that, a
    session closed hours ago whose last title happened to match would
    trigger a collision nobody could see, since it's not actually
    contending for the same visual space anymore."""
    if not session_id or not title:
        return []
    if live_window is None:
        live_window = TITLE_CHANGE_MARKER_WINDOW
    try:
        state = _load_title_state()
    except Exception:
        return []
    out = []
    for sid, entry in state.items():
        if sid == session_id or entry.get("title") != title:
            continue
        try:
            last_seen = datetime.fromisoformat(entry["last_seen"])
        except Exception:
            continue
        if (now - last_seen) < live_window:
            out.append(sid)
    return sorted(out)


TITLE_DISAMBIG_CACHE_PATH = os.path.expanduser("~/.claude/scripts/title-disambig-cache.json")
TITLE_DISAMBIG_MAX_AGE = timedelta(hours=2)


def title_disambiguation(session_id, title):
    """Reads a Fable-generated disambiguation label for this session, if
    one exists, is still fresh, and was generated against the SAME title
    that's colliding right now (see title-collision-prompt-hook.py, which
    writes this cache via title-disambig-write.py). Returns None on any
    miss -- no cache entry, expired past TITLE_DISAMBIG_MAX_AGE, or
    `source_title` mismatch (the raw ai-title moved on since generation,
    so the cached label may no longer describe what's colliding now) --
    which the caller (title_chip()'s caller in statusline.py) treats
    identically to "never generate one": just show the plain title. This
    read never blocks and never calls Fable itself -- generation only ever
    happens from a live Claude Code turn (see the hook), which is the only
    way to spend against the tracked Fable quota rather than pay-per-token
    API credits."""
    if not session_id or not title:
        return None
    try:
        with open(TITLE_DISAMBIG_CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:
        return None
    entry = cache.get(session_id)
    if not entry or entry.get("source_title") != title:
        return None
    try:
        generated_at = datetime.fromisoformat(entry["generated_at"])
    except Exception:
        return None
    if datetime.now(timezone.utc) - generated_at > TITLE_DISAMBIG_MAX_AGE:
        return None
    return entry.get("summary") or None


# NOT gated on sys.stdout.isatty() -- found live (2026-07-29): Claude Code
# always captures a statusLine command's stdout to parse it, so it is NEVER
# a real tty from this script's point of view, in every render, always.
# Gating color on that check (the mistake this comment replaces) meant the
# chip was colorless in every single real render, not just some -- the
# opposite of the intended "usually colored, plain only through a genuine
# non-interactive pipe" behavior. workload-gauge.py's segment() already
# solved this correctly (see its sc() helper and docstring: "ANSI is forced
# on -- statuslines render it even though stdout isn't a TTY here") --
# title_chip() below now follows the same rule: always emit ANSI, no TTY
# check at all, since Claude Code's own renderer is what actually
# interprets these codes, not this process's stdout.

# Eight hand-picked xterm-256 background colors, spread across both hue and
# lightness (not hue alone) so the set stays distinguishable under
# red-green color-vision deficiencies, and all bright enough that black
# chip text reads clearly on every one. Deliberately skips pure red (196):
# this bar already uses red for the swap warning (see workload-gauge.py),
# and a session-identity color shouldn't borrow the "something's wrong"
# association. Also skips teal/cyan (originally 44, DarkTurquoise): Claude
# Code's own native title chip renders in a fixed teal, so a session whose
# hash landed there would look like a literal duplicate of the native chip
# rather than just coincidentally matching text -- swapped for 172
# (a warm goldenrod), a hue bucket nothing else here is close to.
# 135 (MediumPurple1) originally sat here but only cleared WCAG contrast
# 5.90 against black text -- weakest of the eight by a wide margin (the
# next-lowest was 7.05, and 220/gold hits 14.97), confirmed by measuring
# each entry's actual sRGB relative luminance rather than eyeballing it.
# Replaced with 141 (MediumPurple2, one step lighter/bluer), contrast 7.73,
# same hue bucket so distinguishability from the rest of the set holds.
_TITLE_PALETTE = [39, 208, 141, 172, 205, 220, 41, 203]


def _session_color(session_id):
    """Stable color per session_id, not per-render -- a hash keeps one
    terminal window's chip the same color for the session's whole
    lifetime (including after `claude --resume`), which is the actual
    point: telling several concurrent sessions apart at a glance."""
    if not session_id:
        return _TITLE_PALETTE[0]
    import hashlib
    digest = hashlib.sha256(session_id.encode()).digest()
    return _TITLE_PALETTE[digest[0] % len(_TITLE_PALETTE)]


def title_chip(title, session_id, changed=False, columns=None):
    """Builds the session-title chip: a filled block (bold black text on a
    stable per-session background) reproducing the chip Claude Code's own
    UI shows intermittently above the statusline -- rendered here on every
    render instead, since this bar is the one surface that always draws.
    On its own line now (no longer sharing a row with the workload-gauge
    segment -- see statusline.py), so `max_len` no longer needs to leave
    room for that segment; it now sizes to roughly the width of the line
    it's the only thing on. A solid background (not colored foreground
    text) is what keeps it legible in both light and dark terminal themes
    without this script needing to track which one is active.

    `changed` prefixes a small marker (see title_changed_recently()) so a
    task-shift that happened off-screen is still visible when Rajan looks
    back, without a second color or an extra line."""
    if not title:
        return ""
    try:
        cols = columns if columns is not None else int(os.environ.get("COLUMNS", ""))
    except (ValueError, TypeError):
        cols = None
    max_len = 60 if not cols else max(16, min(70, cols - RIGHT_ALIGN_MARGIN))
    marker = "▸ " if changed else ""
    budget = max(1, max_len - len(marker))
    text = title
    if len(text) > budget:
        text = text[: max(1, budget - 1)].rstrip() + "…"
    body = f" {marker}{text} "
    bg = _session_color(session_id)
    return f"\033[1m\033[48;5;{bg}m\033[38;5;16m{body}\033[0m"


# ---- work progress bar ---------------------------------------------------
# One bar per session: bin/work-progress.py writes it (set / step / note /
# eta / clear) and statusline.py draws it as an extra last line while one
# is set. Same split as the ultracode marker above: the CLI owns every write
# and these helpers only read and render, so a statusline render stays
# write-free. State is one small JSON file per session id, so sessions open
# side by side can never see or change each other's bar.

WORK_PROGRESS_DIR = os.path.expanduser("~/.claude/scripts")
_WP_MAX_BYTES = 65536  # a real state file is well under 1 KB
_WP_SESSION_RE = re.compile(r"[^A-Za-z0-9_-]")
_WP_EIGHTHS = "▏▎▍▌▋▊▉"  # sub-cell fill, so a long bar moves smoothly
_WP_MIN_PACE_SPAN = 60  # seconds; steps ticked off right after `set` measure nothing
WP_CREEP_CAP = 0.9  # the creeping fill stops at nine tenths of the running step
WP_PULSE_WINDOW_S = 60  # the pulse moves only if something was written this recently
_WP_PULSE = ("◐◓◑◒", "○")  # frames while work is moving, and the still glyph
_WP_PULSE_ASCII = ("|/-\\", ".")
_WP_CREEP_GLYPH = ("▒", "=")  # a second texture in the same hue, never dimmed


def _env_float(name, default):
    """A numeric knob, read lazily (after load_env_file()) for the same
    reason as _uc_cost(); blank or unparseable falls back to `default`."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def work_progress_path(session_id):
    """The state file for one session's bar, or None without a usable id.
    The id is cut down to [A-Za-z0-9_-] so it can't name a path outside
    WORK_PROGRESS_DIR."""
    safe = _WP_SESSION_RE.sub("", str(session_id or ""))[:80]
    if not safe:
        return None
    return os.path.join(WORK_PROGRESS_DIR, f"work-progress-{safe}.json")


def _wp_time(value):
    """Epoch seconds from a number or an ISO-8601 string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def work_progress_normalize(raw, session_id=None):
    """Validated state from a parsed file, or None when its shape makes no
    sense: wrong types, no start time, or another session's id inside. A
    minimal hand-written file (label, done, total, started_at, updated_at)
    is enough; everything else gets its default. `measured` counts the
    finished steps that have a real finish time, which is what the pace
    is computed from: steps passed in as already done at `set` time count
    toward the percentage but not toward the pace."""
    if not isinstance(raw, dict):
        return None
    owner = raw.get("session")
    if owner and session_id and owner != session_id:
        return None
    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not all(isinstance(s, str) and s for s in steps):
        return None
    if len(set(steps)) != len(steps):
        return None
    finished_raw = raw.get("finished") or {}
    if not isinstance(finished_raw, dict):
        return None
    finished = {name: _wp_time(t) for name, t in finished_raw.items() if name in steps}
    try:
        if steps:
            total, done = len(steps), len(finished)
            measured = sum(1 for t in finished.values() if t is not None)
        else:
            total, done = int(raw.get("total") or 1), int(raw.get("done") or 0)
            base = int(raw.get("done_at_start") or 0)
            measured = max(0, done - max(0, base))
    except (TypeError, ValueError):
        return None
    if total < 1 or done < 0:
        return None
    done = min(done, total)
    measured = min(measured, done)
    started = _wp_time(raw.get("started_at"))
    updated = _wp_time(raw.get("updated_at"))
    started = started if started is not None else updated
    updated = updated if updated is not None else started
    if started is None:
        return None
    last_done = _wp_time(raw.get("last_done_at"))
    if last_done is None and measured:
        last_done = max([t for t in finished.values() if t is not None] or [updated])
    finished_at = _wp_time(raw.get("finished_at"))
    if finished_at is None and done >= total:
        finished_at = last_done if last_done is not None else updated
    quiet_min = raw.get("quiet_min")
    if isinstance(quiet_min, bool) or not isinstance(quiet_min, (int, float)):
        quiet_min = None
    eta_done = raw.get("eta_done")
    if isinstance(eta_done, bool) or not isinstance(eta_done, int):
        eta_done = None
    label, note = raw.get("label"), raw.get("note")
    return {
        "session": owner or session_id or "",
        "label": label if isinstance(label, str) else "",
        "note": note if isinstance(note, str) else "",
        "steps": steps, "finished": finished, "total": total, "done": done,
        "measured": measured, "started_at": started, "updated_at": updated,
        "last_done_at": last_done, "finished_at": finished_at,
        "eta_at": _wp_time(raw.get("eta_at")), "eta_done": eta_done,
        "quiet_min": quiet_min,
    }


def work_progress_load(session_id):
    """This session's bar, or None: no id, no file, a file too big to be
    one, unparseable JSON, or a shape work_progress_normalize() rejects.
    Every failure reads as "no bar", so a broken file never draws a wrong
    one and never breaks the render around it."""
    path = work_progress_path(session_id)
    if not path or not os.path.exists(path):
        return None
    try:
        if os.path.getsize(path) > _WP_MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as f:
            return work_progress_normalize(json.load(f), session_id)
    except Exception:
        return None


def work_progress_view(state, now):
    """Everything the bar shows, worked out in one place for the status
    line, `status` and `status --json`, so the three never disagree.

    Time left blends two figures. The stated estimate (`eta_at`) carries
    what the session knows about the work ahead; the measured pace (time
    per finished step so far) carries what the work has actually cost.
    Each step finished after the estimate was given moves weight from the
    first to the second (weight = steps finished since the estimate /
    steps that were left when it was given), so the figure starts as the
    estimate and ends as the pace -- and a restated estimate starts at
    full weight again, since it already knows the pace up to that point.
    A stated time that has already passed is dropped rather than averaged
    in as zero, and nothing here ever goes negative."""
    now_ts = now.timestamp()
    total, done, measured = state["total"], state["done"], state["measured"]
    started, updated = state["started_at"], state["updated_at"]
    finished = done >= total
    end = (state["finished_at"] or updated) if finished else now_ts
    elapsed = max(0.0, end - started)
    since_update = max(0.0, now_ts - updated)

    stated = pace = pace_left = left = source = None
    if not finished:
        if state["eta_at"] is not None:
            stated = state["eta_at"] - now_ts
        last = state["last_done_at"]
        if measured and last is not None and last - started >= _WP_MIN_PACE_SPAN:
            pace = (last - started) / measured
            into_current = max(0.0, now_ts - last)
            pace_left = max(0.0, pace - into_current) + pace * (total - done - 1)
        base = state["eta_done"]
        base = min(done, base if base is not None else done - measured)
        weight = (done - base) / max(1, total - base)
        if pace_left is not None and stated is not None and stated > 0 and weight > 0:
            left, source = weight * pace_left + (1 - weight) * stated, "blend"
        elif stated is not None and stated > 0:
            left, source = stated, "estimate"
        elif pace_left is not None:
            left, source = pace_left, "pace"
        elif stated is not None:
            left, source = 0.0, "past_estimate"

    quiet_min = state["quiet_min"]
    if quiet_min is None:
        quiet_min = _env_float("CLAUDE_USAGE_PROGRESS_QUIET_MIN", 20)
    quiet_after = max(0.0, quiet_min) * 60
    hidden = None
    if since_update > _env_float("CLAUDE_USAGE_PROGRESS_STALE_HOURS", 8) * 3600:
        hidden = "stale"
    elif finished and now_ts - (state["finished_at"] or updated) > \
            _env_float("CLAUDE_USAGE_PROGRESS_DONE_MIN", 30) * 60:
        hidden = "finished"

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None

    def secs(value):
        return int(round(value)) if value is not None else None

    # The running step (0.23.0): how far into it the work is, against what
    # a step is expected to take -- the measured pace, else the stated
    # estimate as it stood when the step began, shared over the steps left.
    # Capped at WP_CREEP_CAP of one step, so the creeping fill can never
    # reach the next step's mark: only a finished step moves the bar there.
    creep = 0.0
    step_started = expected = None
    if not finished:
        step_started = max(started, state["last_done_at"] or started)
        if pace is not None:
            expected = pace
        elif state["eta_at"] is not None and state["eta_at"] > step_started:
            expected = (state["eta_at"] - step_started) / max(1, total - done)
        if expected:
            creep = min(WP_CREEP_CAP, max(0.0, now_ts - step_started) / expected)

    steps = [{"name": n, "done": n in state["finished"],
              "finished_at": iso(state["finished"].get(n))} for n in state["steps"]]
    return {
        "label": state["label"], "note": state["note"],
        "done": done, "total": total,
        "percent": 100 if finished else min(99, int(100 * done / total)),
        "steps": steps,
        "current_step": next((s["name"] for s in steps if not s["done"]), None),
        "started_at": iso(started), "updated_at": iso(updated),
        "eta_at": iso(state["eta_at"]), "finished_at": iso(state["finished_at"]) if finished else None,
        "elapsed_s": secs(elapsed),
        "left_s": secs(left), "left_source": source,
        "estimate_left_s": secs(max(0.0, stated)) if stated is not None else None,
        "pace_s_per_step": secs(pace), "pace_left_s": secs(pace_left),
        "quiet": (not finished) and quiet_after > 0 and since_update >= quiet_after,
        "quiet_s": secs(since_update), "quiet_after_s": secs(quiet_after),
        "finished": finished,
        "visible": hidden is None, "hidden_reason": hidden,
        "step_started_at": iso(step_started), "step_expected_s": secs(expected),
        "creep": round(creep, 4),
    }


def work_progress_no_color():
    """https://no-color.org: any non-empty NO_COLOR turns colour off."""
    return os.environ.get("NO_COLOR", "") != ""


def work_progress_ascii_default():
    """ASCII glyphs when asked for (CLAUDE_USAGE_PROGRESS_ASCII=1), or when
    stdout's encoding can't carry the block characters at all."""
    if os.environ.get("CLAUDE_USAGE_PROGRESS_ASCII", "").strip() not in ("", "0"):
        return True
    try:
        "█░▏▸✓⚠·…".encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return True
    return False


def _live_columns():
    """COLUMNS when Claude Code sets it this render, else the controlling
    terminal's width, else None -- the same order right_align() uses."""
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = None
    return columns or _tty_columns()


def fmt_span(seconds):
    """Exact duration, e.g. '42m', '1h 5m', '2d 3h'."""
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return "<1m"
    hours, mins = divmod(seconds // 60, 60)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def fmt_span_short(seconds):
    """A past duration for a note, e.g. '7m', '2h', '3d'."""
    seconds = max(0, int(seconds or 0))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fmt_span_approx(seconds):
    """An estimate rounded to what it can honestly claim: the nearest
    minute under 10m, 5m under an hour, 10m beyond."""
    mins = max(0.0, seconds) / 60
    step = 1 if mins < 10 else 5 if mins < 60 else 10
    return fmt_span(max(step, int(round(mins / step)) * step) * 60)


def _wp_trim(text, limit, ellipsis):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - len(ellipsis))].rstrip() + ellipsis


def work_progress_left_text(view):
    source = view["left_source"]
    if source is None:
        return ""
    if source == "past_estimate":
        return "past estimate"
    if view["left_s"] < 90:
        return "finishing"
    return f"~{fmt_span_approx(view['left_s'])} left"


def _wp_bar_cells(width, columns):
    """Bar length in cells: --width, then CLAUDE_USAGE_PROGRESS_WIDTH, then
    a tenth of the terminal (8-20 cells), then 12."""
    for value in (width, _env_float("CLAUDE_USAGE_PROGRESS_WIDTH", 0)):
        try:
            if value and int(value) > 0:
                return max(4, min(60, int(value)))
        except (TypeError, ValueError):
            pass
    return max(8, min(20, columns // 10)) if columns else 12


def work_progress_palette(appearance=None):
    """The row's colours, as SGR codes: (hue, detail, empty, label).

    CLAUDE_USAGE_PROGRESS_COLOR picks the look:
      calm    (default) one hue for the whole row and nothing dimmed, so
              the bar, the numbers and the note read as one quiet line on
              a light terminal and on a dark one alike
      plain   the terminal's own text colour throughout, the bar included
      accent  the 0.22.0 look: a warm bar with dimmed details
      0-255   that 256-colour as the hue, otherwise like calm

    `calm` takes a deeper tone on a light terminal and a lighter one on a
    dark terminal when the appearance is known ("light"/"dark"), and a
    middle tone that holds about 4.6:1 against both white and black when
    it is not."""
    choice = os.environ.get("CLAUDE_USAGE_PROGRESS_COLOR", "calm").strip().lower()
    if choice == "plain":
        return ("", "", "", "1")
    if choice == "accent":
        return ("38;5;173", "2", "2", "1")
    tone = {"light": "62", "dark": "105"}.get(appearance or "", "63")
    if choice.isdigit() and 0 <= int(choice) <= 255:
        tone = choice
    hue = f"38;5;{tone}"
    return (hue, hue, hue, f"1;{hue}")


def work_progress_appearance():
    """"light" or "dark" from CLAUDE_USAGE_PROGRESS_APPEARANCE, else None
    (the middle tone). Never shells out: a status line render must stay
    cheap, and a terminal's own profile can differ from the system's."""
    value = os.environ.get("CLAUDE_USAGE_PROGRESS_APPEARANCE", "").strip().lower()
    return value if value in ("light", "dark") else None


def work_progress_live():
    """The live parts of the row (0.23.0): the clock in seconds, the fill
    creeping inside the running step, and the pulse. On unless
    CLAUDE_USAGE_PROGRESS_LIVE=0, which draws the row exactly as 0.22.1."""
    return os.environ.get("CLAUDE_USAGE_PROGRESS_LIVE", "1").strip() != "0"


def fmt_span_clock(seconds):
    """Elapsed time to the second, e.g. '42s', '37m 05s', '1h 5m 07s'; a
    day or more falls back to fmt_span()."""
    seconds = max(0, int(seconds or 0))
    if seconds >= 86400:
        return fmt_span(seconds)
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {mins}m {secs:02d}s"
    if mins:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def fmt_work_progress(view, width=None, color=True, ascii_only=False, columns=None,
                      live=None, pulse=None):
    """One status-line row for a bar, e.g.
    'release 2.4 ████████▒░░░░░░░ 50% · 2/4 ▸ test ◐ · 42m 05s in · ~40m left · note'
    -- green with '✓ done in 1h 42m' once the last step finishes, and a
    yellow '⚠ quiet 25m' while nothing has updated it past the quiet
    threshold. Sized to the terminal (`columns`, default the live width)
    so it never wraps: the note is trimmed first, then the clock's seconds
    go, then the note is dropped, then the elapsed time goes.

    Live (see work_progress_live()): the clock counts seconds, the fill
    creeps in a second texture of the same hue through the running step
    (view["creep"], never past nine tenths of it; the percentage and the
    count stay counted from finished steps only), and `pulse` -- a glyph
    the caller chose from real activity, see work_progress_pulse() -- sits
    beside the running step's name. With live off none of the three is
    drawn and the row is byte for byte the 0.22.1 row."""
    if columns is None:
        columns = _live_columns()
    if live is None:
        live = work_progress_live()

    def paint(code, text):
        return f"\033[{code}m{text}\033[0m" if color and code and text else text

    hue, detail, empty, label_code = work_progress_palette(work_progress_appearance())
    raw_sep, ell = (" | ", "...") if ascii_only else (" · ", "…")
    sep = paint(detail, raw_sep)
    cells = _wp_bar_cells(width, columns)
    frac = view["done"] / view["total"]
    fill = "32" if view["finished"] else hue
    creep = 0.0
    if live and not view["finished"]:
        creep = min(WP_CREEP_CAP, max(0.0, float(view.get("creep") or 0.0)))
    creep_end = (view["done"] + creep) / view["total"] * cells
    if ascii_only:
        filled = int(frac * cells + 1e-9)
        crept = max(0, int(creep_end + 1e-9) - filled) if creep else 0
        bar = (paint(detail, "[") + paint(fill, "#" * filled)
               + (paint(fill, _WP_CREEP_GLYPH[1] * crept) if crept else "")
               + paint(empty, "-" * (cells - filled - crept)) + paint(detail, "]"))
    else:
        full, part = divmod(int(frac * cells * 8 + 1e-9), 8)
        head = "█" * full + (_WP_EIGHTHS[part - 1] if part else "")
        crept = max(0, int(creep_end + 1e-9) - len(head)) if creep else 0
        bar = (paint(fill, head)
               + (paint(fill, _WP_CREEP_GLYPH[0] * crept) if crept else "")
               + paint(empty, "░" * (cells - len(head) - crept)))
    first = f"{bar} {paint(hue, str(view['percent']) + '%')}"
    label = _wp_trim(view["label"], 40, ell)
    if label:
        first = f"{paint(label_code, label)} {first}"
    count = f"{view['done']}/{view['total']}"
    if view["current_step"]:
        count += f" {'>' if ascii_only else '▸'} {_wp_trim(view['current_step'], 30, ell)}"
        if live and pulse and not view["finished"]:
            count += f" {pulse}"
    budget = columns - RIGHT_ALIGN_MARGIN if columns else None

    def build(clock):
        pieces, elapsed = [first, paint(hue, count)], None
        if view["finished"]:
            check = "" if ascii_only else "✓ "
            pieces.append(paint("32", f"{check}done in {fmt_span(view['elapsed_s'])}"))
        else:
            elapsed = paint(detail, f"{clock(view['elapsed_s'])} in")
            pieces.append(elapsed)
            left = work_progress_left_text(view)
            if left:
                pieces.append(paint(hue, left))
            if view["quiet"]:
                mark = "! " if ascii_only else "⚠ "
                pieces.append(paint("33", f"{mark}quiet {fmt_span(view['quiet_s'])}"))
        kept_elapsed = True
        if budget and elapsed and visible_len(sep.join(pieces)) > budget:
            pieces.remove(elapsed)
            kept_elapsed = False
        line = sep.join(pieces)
        kept_note = not view["note"]
        if view["note"]:
            room = min(80, budget - visible_len(line) - len(raw_sep)) if budget else 80
            if room >= 8:
                line += sep + paint(detail, _wp_trim(view["note"], room, ell))
                kept_note = True
        return line, kept_elapsed and kept_note

    if live and not view["finished"]:
        line, whole = build(fmt_span_clock)
        if whole:
            return line
    return build(fmt_span)[0]


def _wp_recent(path, since):
    try:
        return os.stat(path).st_mtime >= since
    except OSError:
        return False


def _wp_any_recent(folder, since, suffix=None, depth=1):
    """True when `folder` itself, or an entry in it (and one level further
    down with depth=2), was modified at or after `since`. One scandir per
    folder and an early exit: never a walk of the whole projects tree."""
    if not folder or not _wp_recent(folder, 0):
        return False
    if _wp_recent(folder, since):
        return True
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth > 1 and _wp_any_recent(entry.path, since, suffix, depth - 1):
                            return True
                        continue
                    if suffix and not entry.name.endswith(suffix):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime >= since:
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def work_progress_activity(session_id, transcript_path, now_ts):
    """True when the session really wrote something in the last
    WP_PULSE_WINDOW_S seconds: its own transcript, a transcript of one of
    its subagents (<transcript without .jsonl>/subagents/*.jsonl), or a
    file under its tasks folder (~/.claude/tasks/<session id>/). Paths come
    from the status line's own payload; cheapest check first."""
    since = now_ts - WP_PULSE_WINDOW_S
    if transcript_path and _wp_recent(transcript_path, since):
        return True
    safe = _WP_SESSION_RE.sub("", str(session_id or ""))[:80]
    if safe and _wp_any_recent(os.path.join(os.path.expanduser("~/.claude/tasks"), safe), since, depth=2):
        return True
    if transcript_path and transcript_path.endswith(".jsonl"):
        subagents = os.path.join(transcript_path[: -len(".jsonl")], "subagents")
        if _wp_any_recent(subagents, since, suffix=".jsonl"):
            return True
    return False


def work_progress_pulse_path(session_id):
    safe = _WP_SESSION_RE.sub("", str(session_id or ""))[:80]
    return os.path.join(WORK_PROGRESS_DIR, f".work-progress-{safe}.pulse") if safe else None


def work_progress_pulse(session_id, transcript_path, now_ts, ascii_only=False):
    """The glyph beside the running step: the next frame on every redraw
    while work_progress_activity() is true (the frame index is kept in a
    one-number file, so consecutive redraws always differ), else the still
    glyph. Any error gives the still glyph."""
    frames, still = _WP_PULSE_ASCII if ascii_only else _WP_PULSE
    try:
        if not work_progress_activity(session_id, transcript_path, now_ts):
            return still
        path = work_progress_pulse_path(session_id)
        if not path:
            return still
        try:
            with open(path) as f:
                index = (int(f.read().strip() or 0) + 1) % len(frames)
        except (OSError, ValueError):
            index = 0
        with open(path, "w") as f:
            f.write(str(index))
        return frames[index]
    except Exception:
        return still


def work_progress_line(session_id, now, transcript_path=None):
    """statusline.py's progress row for this session, or "" -- no bar set
    (costs one os.path.exists() and nothing more), a hidden bar, or any
    error at all, since a render must never break on this file.
    `transcript_path` (from the status line's payload) lets the pulse see
    the session's own writes; without it only the tasks folder is seen."""
    path = work_progress_path(session_id)
    if not path or not os.path.exists(path):
        return ""
    try:
        state = work_progress_load(session_id)
        if not state:
            return ""
        view = work_progress_view(state, now)
        if not view["visible"]:
            return ""
        ascii_only = work_progress_ascii_default()
        live = work_progress_live()
        pulse = None
        if live and not view["finished"] and view["current_step"]:
            pulse = work_progress_pulse(session_id, transcript_path, now.timestamp(), ascii_only)
        return fmt_work_progress(view, color=not work_progress_no_color(),
                                 ascii_only=ascii_only, live=live, pulse=pulse)
    except Exception:
        return ""
