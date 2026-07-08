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


def fmt_delta(epoch_target, now):
    """Formats a countdown to a Unix epoch timestamp, computed fresh against
    `now` every call -- accurate even if the surrounding data was cached
    a few minutes ago, since resets_at is an absolute point in time."""
    if epoch_target is None:
        return None
    secs = int(epoch_target - now.timestamp())
    if secs <= 0:
        return "now"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h > 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    return f"{h}h {m}m"
