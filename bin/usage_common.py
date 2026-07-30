"""Shared helpers for the statusline renderer, the SessionStart hook, and the
background watcher."""
import hashlib, json, os, re, subprocess, sys
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


def fable_estimate(now, current_resets_at=None, current_seven_day_pct=None):
    """Returns the live weekly % for the per-model pool Anthropic's real
    rate_limits field doesn't break out (default: Fable) -- projected from a
    weekly $ cap derived at the last real calibration against
    claude.ai/settings/usage, against cost-weighted local usage since the
    window started (see tokens-since.py). This is the one number in the
    tool that isn't verified reported Anthropic data, so it always comes
    back with an explicit `stale` flag rather than a bare number -- callers
    must never present it as fact when stale. Returns None if it's never
    been calibrated at all.

    Both tokens-since.py subprocess calls below carry an explicit timeout
    (found live, 2026-07-29): tokens-since.py glob-scans every local
    session transcript, and under real disk contention that scan can run
    long enough to blow past the *caller's* subprocess timeout ten steps up
    the chain (statusline.py's run(), timeout=10) -- which kills the whole
    usage-statusline.py process, not just this fable segment, dropping the
    entire quota line off the bar. A tight bound here (well under that
    outer 10s) means a slow scan degrades to "stale" for this one segment
    instead of taking the rest of the line down with it.

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

    # Advance the window to the real current reset boundary first, before
    # branching on cap state, so every case below reasons about the
    # *current* window rather than a stale one. No browser read needed:
    # prefer rate_limits' own resets_at (ground truth -- the tracked
    # model's pool resets in lockstep with the all-models weekly) when
    # available, else step forward in 7-day increments from the last known
    # boundary.
    if current_resets_at is not None:
        next_reset = datetime.fromtimestamp(current_resets_at, tz=timezone.utc)
    else:
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
        try:
            tokens = json.loads(
                subprocess.check_output(
                    [sys.executable, TOKENS_SINCE, window_start.isoformat()], stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            )
            tracked_now = sum(v for k, v in tokens.items() if tracked_model.lower() in k.lower())
        except Exception:
            return {"tracked_model": tracked_model, "stale": True}
        if not rolled_over and tracked_now > cal.get("tokens_at_cal", 0):
            return {"tracked_model": tracked_model, "stale": True}
        if rolled_over and tracked_now > 0:
            return {"tracked_model": tracked_model, "stale": True}
        return {
            "tracked_model": tracked_model,
            "stale": False,
            "pct": 0 if rolled_over else cal.get("pct", 0),
            "resets_at": int(next_reset.timestamp()),
        }

    try:
        cap_derived_at = datetime.fromisoformat(cap_derived_at_raw)
    except Exception:
        return {"tracked_model": tracked_model, "stale": True}
    if now - cap_derived_at > _cap_max_age():
        return {"tracked_model": tracked_model, "stale": True}

    try:
        tokens = json.loads(
            subprocess.check_output(
                [sys.executable, TOKENS_SINCE, window_start.isoformat()], stderr=subprocess.DEVNULL,
                timeout=5,
            )
        )
    except Exception:
        return {"tracked_model": tracked_model, "stale": True}

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
    }


FABLE_STALE_STATE_PATH = os.path.expanduser("~/.claude/scripts/fable-stale-state.json")
LIVE_CACHE_PATH = os.path.expanduser("~/.claude/scripts/usage-live.json")
FABLE_FORCE_RECAL_PATH = os.path.expanduser("~/.claude/scripts/fable-force-recal.json")


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
    try:
        with open(LIVE_CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:
        return None

    fable = fable_estimate(now, cache.get("seven_day_resets_at"), cache.get("seven_day_pct"))
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
_TITLE_PALETTE = [39, 208, 135, 172, 205, 220, 41, 203]


def _session_color(session_id):
    """Stable color per session_id, not per-render -- a hash keeps one
    terminal window's chip the same color for the session's whole
    lifetime (including after `claude --resume`), which is the actual
    point: telling several concurrent sessions apart at a glance."""
    if not session_id:
        return _TITLE_PALETTE[0]
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
