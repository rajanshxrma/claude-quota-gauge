"""Shared helpers for the statusline renderer, the SessionStart hook, and the
background watcher."""
import json, os, subprocess, sys
from datetime import datetime

FABLE_CAL_PATH = os.path.expanduser("~/.claude/scripts/usage-fable-calibration.json")
TOKENS_SINCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens-since.py")


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
    """Returns the estimated weekly % for the per-model pool Anthropic's real
    rate_limits field doesn't break out (default: Fable) -- scaled from the
    last manual calibration against claude.ai/settings/usage. This is the
    one number in the tool that isn't verified reported Anthropic data, so
    it always comes back with an explicit `stale` flag rather than a bare
    number -- callers must never present it as fact when stale. Returns
    None if it's never been calibrated at all."""
    if not os.path.exists(FABLE_CAL_PATH):
        return None
    try:
        with open(FABLE_CAL_PATH) as f:
            cal = json.load(f)
        next_reset = datetime.fromisoformat(cal["next_reset"])
    except Exception:
        return None

    tracked_model = cal["tracked_model"]
    stale = now > next_reset
    if current_resets_at is not None and int(next_reset.timestamp()) != int(current_resets_at):
        stale = True

    if stale:
        return {"tracked_model": tracked_model, "stale": True}

    try:
        tokens = json.loads(
            subprocess.check_output(
                [sys.executable, TOKENS_SINCE, cal["window_start"]], stderr=subprocess.DEVNULL
            )
        )
    except Exception:
        return {"tracked_model": tracked_model, "stale": True}

    tracked_now = sum(v for k, v in tokens.items() if tracked_model.lower() in k.lower())
    pct = cal["pct"] * (tracked_now / cal["tokens_at_cal"]) if cal.get("tokens_at_cal") else cal["pct"]
    pct = min(pct, 150)
    return {
        "tracked_model": tracked_model,
        "stale": False,
        "pct": pct,
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
    not just whichever sub-model happens to be live at that instant."""
    model_info = payload.get("model") or {}
    display = model_info.get("display_name") or model_info.get("id")
    if not display:
        return None

    cwd = payload.get("cwd") or (payload.get("workspace") or {}).get("current_dir")
    configured = resolve_configured_model(cwd)
    label = f"opusplan→{display}" if configured == "opusplan" else display

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
