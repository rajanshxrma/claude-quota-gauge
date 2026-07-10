#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
COMMANDS_DIR="$CLAUDE_DIR/commands"

echo "Installing claude-quota-gauge..."

mkdir -p "$SCRIPTS_DIR" "$COMMANDS_DIR"
cp "$SCRIPT_DIR"/bin/*.py "$SCRIPTS_DIR/"
chmod +x "$SCRIPTS_DIR"/*.py
cp "$SCRIPT_DIR"/commands/pending.md "$COMMANDS_DIR/"
cp "$SCRIPT_DIR"/commands/gauge-cali-fable.md "$COMMANDS_DIR/"
echo "  copied scripts to $SCRIPTS_DIR"
echo "  copied /pending and /gauge-cali-fable to $COMMANDS_DIR"

echo ""
read -r -p "Wire the statusline and hooks into ~/.claude/settings.json now? [Y/n] " REPLY
REPLY="${REPLY:-Y}"
if [[ "$REPLY" =~ ^[Yy] ]]; then
  python3 - "$CLAUDE_DIR/settings.json" \
    "python3 $SCRIPTS_DIR/usage-statusline.py" \
    "python3 $SCRIPTS_DIR/usage-session-hook.py" \
    "python3 $SCRIPTS_DIR/theme-watch-prompt-hook.py" <<'PYEOF'
import json, os, sys

settings_path, statusline_command, hook_command, prompt_hook_command = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
    with open(settings_path + ".bak", "w") as f:
        json.dump(settings, f, indent=2)
    print(f"  backed up existing settings to {settings_path}.bak")


def normalize(cmd):
    return os.path.abspath(os.path.expanduser(cmd.split(" ", 1)[-1]))


existing_statusline = settings.get("statusLine", {}).get("command")
if not existing_statusline:
    settings["statusLine"] = {"type": "command", "command": statusline_command, "refreshInterval": 60}
    print(f"  set statusLine to {statusline_command}")
elif normalize(existing_statusline) == normalize(statusline_command):
    print("  statusLine already points here, left settings.json unchanged")
else:
    print(f"  statusLine already set to something else ({existing_statusline!r}) -- left it alone.")
    print(f"    Add this yourself if you want ours: {{\"type\": \"command\", \"command\": \"{statusline_command}\", \"refreshInterval\": 60}}")

hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])

already_present = any(
    h.get("command") and normalize(h["command"]) == normalize(hook_command)
    for group in session_start
    for h in group.get("hooks", [])
)

if already_present:
    print("  SessionStart hook already present, left settings.json unchanged")
else:
    session_start.append({"hooks": [{"type": "command", "command": hook_command, "timeout": 15}]})
    print(f"  added SessionStart hook: {hook_command}")

user_prompt_submit = hooks.setdefault("UserPromptSubmit", [])

prompt_already_present = any(
    h.get("command") and normalize(h["command"]) == normalize(prompt_hook_command)
    for group in user_prompt_submit
    for h in group.get("hooks", [])
)

if prompt_already_present:
    print("  UserPromptSubmit hook already present, left settings.json unchanged")
else:
    user_prompt_submit.append({"hooks": [{"type": "command", "command": prompt_hook_command, "timeout": 5}]})
    print(f"  added UserPromptSubmit hook: {prompt_hook_command}")
    print("  (no-ops unless CLAUDE_USAGE_THEME_WATCH=1 is set -- see config/usage-calibrator.env.example)")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
PYEOF
else
  echo "  skipped. Add these to ~/.claude/settings.json yourself:"
  echo '    "statusLine": { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/usage-statusline.py", "refreshInterval": 60 }'
  echo '    "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/usage-session-hook.py", "timeout": 15 } ] } ], "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/theme-watch-prompt-hook.py", "timeout": 5 } ] } ] }'
fi

echo ""
echo "Done. Open Claude Code -- the statusline shows your real 5h/weekly % as"
echo "soon as it first renders (usually within a few seconds), read straight"
echo "from Claude Code's own rate_limits data. No calibration step, nothing to"
echo "run by hand for those two numbers. If your account has a separate"
echo "weekly pool for one model (e.g. Fable) that rate_limits doesn't break"
echo "out, run /gauge-cali-fable once to add a clearly-labeled estimate for"
echo "it too. See README.md for the optional background watcher (launchd/)"
echo "and config (config/usage-calibrator.env.example)."
