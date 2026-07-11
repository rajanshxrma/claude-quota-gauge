#!/usr/bin/env python3
"""Background watcher: fires a macOS notification when the last cached
usage % (written by the statusline renderer) crosses a threshold, once per
window (dedup'd against that window's reset timestamp). This reads whatever
was last cached -- it can't refresh the numbers itself, since the real
rate_limits data only arrives while a Claude Code session's statusline is
actively rendering.

Also appends each distinct snapshot to a persistent JSONL history file
(usage-history.jsonl) -- piggybacking on this script's existing 15-min
launchd cadence rather than adding a second scheduled job. Purely additive:
does not change the threshold-notification behavior above.
"""
import datetime, json, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_common import load_env_file, fable_estimate  # noqa: E402

load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
CACHE_PATH = os.path.join(SCRIPTS, "usage-live.json")
FABLE_CAL_PATH = os.path.join(SCRIPTS, "usage-fable-calibration.json")
STATE_PATH = os.path.join(SCRIPTS, "usage-watch-state.json")
HISTORY_PATH = os.path.expanduser("~/.claude/usage-history.jsonl")
THRESHOLD = int(os.environ.get("CLAUDE_USAGE_ALERT_THRESHOLD", "85"))


def notify(title, message):
    if sys.platform == "darwin":
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Glass"']
        )
    else:
        print(f"{title}: {message}")


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def append_history(cache, state):
    """Appends one JSONL row per distinct snapshot (dedup'd on fetched_at
    against the last row this script appended -- tracked in the shared
    state file so a bare restart of this script doesn't need to re-read
    the whole history file to find the last row). Returns True if state
    changed (a new row was appended)."""
    fetched_at = cache.get("fetched_at")
    if not fetched_at or state.get("last_history_fetched_at") == fetched_at:
        return False

    row = dict(cache)
    row["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")

    state["last_history_fetched_at"] = fetched_at
    return True


def main():
    if not os.path.exists(CACHE_PATH):
        return

    with open(CACHE_PATH) as f:
        cache = json.load(f)

    state = load_state()
    changed = append_history(cache, state)

    checks = []
    if "five_hour_pct" in cache:
        checks.append(("five_hour", "5-hour block", cache["five_hour_pct"], cache.get("five_hour_resets_at")))
    if "seven_day_pct" in cache:
        checks.append(("seven_day", "weekly", cache["seven_day_pct"], cache.get("seven_day_resets_at")))
    if "fable_pct" in cache:
        model = cache.get("fable_tracked_model", "fable")
        checks.append((f"{model}_weekly", f"{model} weekly", cache["fable_pct"], cache.get("fable_resets_at")))

    for key, label, pct, resets_at in checks:
        notified_key = f"{key}_notified_for"
        if pct >= THRESHOLD and state.get(notified_key) != resets_at:
            notify("Claude usage", f"{label} at {pct:.0f}% used")
            state[notified_key] = resets_at
            changed = True
        elif pct < THRESHOLD and state.get(notified_key) is not None:
            if state.get(notified_key) != resets_at:
                state[notified_key] = None
                changed = True

    # Separate from the threshold checks above: this catches the estimate
    # itself going stale (e.g. calibration too old, or Anthropic's real
    # number moved without a window rollover) even when no session is open
    # to notice via the statusline. Deduped on the calibration's own
    # calibrated_at so it renotifies once per stale calibration, then again
    # after the next /gauge-calibrate.
    now = datetime.datetime.now(datetime.timezone.utc)
    fable = fable_estimate(now, cache.get("seven_day_resets_at"), cache.get("seven_day_pct"))
    if fable and fable.get("stale"):
        cal_marker = None
        if os.path.exists(FABLE_CAL_PATH):
            try:
                with open(FABLE_CAL_PATH) as f:
                    cal_marker = json.load(f).get("calibrated_at")
            except Exception:
                pass
        if state.get("fable_stale_notified_for") != cal_marker:
            notify(
                "Claude usage",
                f"{fable['tracked_model']} weekly estimate is stale -- run /gauge-calibrate",
            )
            state["fable_stale_notified_for"] = cal_marker
            changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
