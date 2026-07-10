"""Shared helpers for the statusline renderer, the SessionStart hook, and the
background watcher."""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

FABLE_CAL_PATH = os.path.expanduser("~/.claude/scripts/usage-fable-calibration.json")
TOKENS_SINCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens-since.py")
# Absolute-cap model (replaced the old ratio-scaling model 2026-07-10): a
# calibration derives a weekly $ cap (tokens_at_cal / (pct/100)) instead of
# remembering a % to scale. The cap is slow-moving -- Anthropic's weekly
# limit rarely changes -- so it only needs occasional re-verification, not
# a tight hours-long ceiling. This also fixed a real bug in the old model:
# calibrating at exactly 0% (pct=0) froze the ratio-scaled estimate at 0%
# forever, since it multiplied by cal["pct"]. The cap model has no such
# freeze -- it projects live local usage against a fixed denominator.
CAP_MAX_AGE = timedelta(days=14)
# Local projections aren't ground truth -- if the cost-weighted local
# estimate blows past a sane ceiling, that's a sign the cap itself has
# drifted from reality (e.g. Anthropic adjusted the limit), not that Fable
# usage is actually >120% of the weekly pool. Report stale rather than a
# number nobody would believe.
PROJECTION_CEILING = 120


def load_env_file(path="~/.claude/usage-calibrator.env"):
    """Loads KEY=VALUE overrides -- neither the statusline command nor a
    SessionStart hook nor launchd inherit the shell profile's env vars, so
    this is how personal config reaches these scripts. Never overrides an
    already-set var."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
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


def fable_estimate(now, current_resets_at=None):
    """Returns the live weekly % for the per-model pool Anthropic's real
    rate_limits field doesn't break out (default: Fable) -- projected from a
    weekly $ cap derived at the last real calibration against
    claude.ai/settings/usage, against cost-weighted local usage since the
    window started (see tokens-since.py). This is the one number in the
    tool that isn't verified reported Anthropic data, so it always comes
    back with an explicit `stale` flag rather than a bare number -- callers
    must never present it as fact when stale. Returns None if it's never
    been calibrated at all.

    Unlike the 5h/weekly-all numbers (which come free from rate_limits on
    every render), this needs the cap to have been derived at least once
    from a real, non-zero settings-page read. Once that's done, the %
    itself updates live every call as local usage accrues -- no further
    browser reads needed except to occasionally re-verify the cap hasn't
    drifted (see CAP_MAX_AGE), and the weekly window advances on its own
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
    built to catch."""
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
                    [sys.executable, TOKENS_SINCE, window_start.isoformat()], stderr=subprocess.DEVNULL
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
    if now - cap_derived_at > CAP_MAX_AGE:
        return {"tracked_model": tracked_model, "stale": True}

    try:
        tokens = json.loads(
            subprocess.check_output(
                [sys.executable, TOKENS_SINCE, window_start.isoformat()], stderr=subprocess.DEVNULL
            )
        )
    except Exception:
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


def fmt_window(label, pct, resets_at, now, cached=False):
    """Formats one usage row, e.g. '5h: 18% (resets 4h 58m)'. Once resets_at
    has passed, the window has crossed its reset boundary server-side but a
    fresh reading hasn't landed yet (nothing refreshes the % until the next
    real rate_limits payload arrives) -- shown as an explicit 'resetting...'
    state carrying the last known % instead of a stale countdown stuck at
    "now", which is indistinguishable from the tool having hung."""
    if resets_at is not None and (resets_at - now.timestamp()) <= 0:
        return f"{label}: resetting... (was {pct:.0f}%)"
    resets = fmt_delta(resets_at, now)
    if cached:
        tail = f" (cached, auto-updates shortly), resets {resets}" if resets else " (cached, auto-updates shortly)"
    else:
        tail = f" (resets {resets})" if resets else ""
    return f"{label}: {pct:.0f}%{tail}"
