#!/usr/bin/env python3
"""Background watcher: fires a macOS notification when usage crosses a
threshold, once per window (dedup'd against the window's reset timestamp
so it doesn't repeat until that window actually resets)."""
import json, os, subprocess, sys


def _load_env_file(path="~/.claude/usage-calibrator.env"):
    """Loads KEY=VALUE overrides -- launchd doesn't inherit shell profile
    env vars, so this is how personal config reaches the watcher."""
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


_load_env_file()

SCRIPTS = os.path.expanduser("~/.claude/scripts")
STATE_PATH = os.path.join(SCRIPTS, "usage-watch-state.json")
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


def main():
    out = subprocess.check_output(
        [sys.executable, os.path.join(SCRIPTS, "usage-estimate.py"), "--json"]
    )
    data = json.loads(out)
    state = load_state()
    changed = False

    checks = []
    if not data.get("session_stale") and "session_pct" in data:
        checks.append(("session", "5-hour block", data["session_pct"], data.get("session_resets_at")))
    if not data.get("weekly_stale"):
        if "weekly_all_pct" in data:
            checks.append(("weekly_all", "weekly (all models)", data["weekly_all_pct"], data.get("weekly_resets_at")))
        if "weekly_tracked_model_pct" in data:
            model_name = data.get("weekly_tracked_model", "tracked model")
            checks.append(("weekly_tracked", f"weekly {model_name}", data["weekly_tracked_model_pct"], data.get("weekly_resets_at")))

    for key, label, pct, resets_at in checks:
        notified_key = f"{key}_notified_for"
        if pct >= THRESHOLD and state.get(notified_key) != resets_at:
            notify("Claude usage", f"{label} at {pct:.0f}% used")
            state[notified_key] = resets_at
            changed = True
        elif pct < THRESHOLD and state.get(notified_key) is not None:
            # window clearly reset / usage dropped -- clear so a future crossing re-alerts
            if state.get(notified_key) != resets_at:
                state[notified_key] = None
                changed = True

    # --- recalibration-due alerts (fire even with no Claude session open) ---
    if data.get("session_stale") and "session_new_block_start" in data:
        notified_key = "session_recal_notified_for"
        marker = data["session_new_block_start"]
        if state.get(notified_key) != marker:
            notify("Claude usage", "New 5h block started -- open Claude Code to recalibrate")
            state[notified_key] = marker
            changed = True

    if data.get("weekly_stale") and "weekly_stale_since" in data:
        notified_key = "weekly_recal_notified_for"
        marker = data["weekly_stale_since"]
        if state.get(notified_key) != marker:
            notify("Claude usage", "Weekly usage window rolled over -- open Claude Code to recalibrate")
            state[notified_key] = marker
            changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
