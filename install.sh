#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
COMMANDS_DIR="$CLAUDE_DIR/commands"

echo "Installing claude-quota-gauge..."

mkdir -p "$SCRIPTS_DIR" "$COMMANDS_DIR"
cp "$SCRIPT_DIR"/bin/* "$SCRIPTS_DIR/"
chmod +x "$SCRIPTS_DIR"/*.py "$SCRIPTS_DIR"/*.sh
cp "$SCRIPT_DIR"/commands/usage-recalibrate.md "$COMMANDS_DIR/"
echo "  copied scripts to $SCRIPTS_DIR"
echo "  copied /usage-recalibrate to $COMMANDS_DIR"

if command -v ccusage >/dev/null 2>&1 || [ -x "$HOME/.npm-global/bin/ccusage" ]; then
  echo "  found ccusage"
else
  echo ""
  echo "  ccusage not found on PATH. Install it with:"
  echo "    npm install -g ccusage"
  echo "  (the scripts fall back to 'npx ccusage@latest' if it's missing, but that's slower on every run)"
fi

echo ""
read -r -p "Wire the SessionStart hook into ~/.claude/settings.json now? [Y/n] " REPLY
REPLY="${REPLY:-Y}"
if [[ "$REPLY" =~ ^[Yy] ]]; then
  python3 - "$CLAUDE_DIR/settings.json" "$SCRIPTS_DIR/usage-session-hook.sh" <<'PYEOF'
import json, os, sys

settings_path, hook_command = sys.argv[1], sys.argv[2]

settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
    with open(settings_path + ".bak", "w") as f:
        json.dump(settings, f, indent=2)
    print(f"  backed up existing settings to {settings_path}.bak")

def normalize(cmd):
    return os.path.abspath(os.path.expanduser(cmd))

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
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    print(f"  added SessionStart hook to {settings_path}")
PYEOF
else
  echo "  skipped. Add this to ~/.claude/settings.json yourself under hooks.SessionStart:"
  echo '    { "hooks": [ { "type": "command", "command": "'"$SCRIPTS_DIR"'/usage-session-hook.sh", "timeout": 15 } ] }'
fi

echo ""
echo "Done. Next: open Claude Code and run /usage-recalibrate to read your real %"
echo "from claude.ai/settings/usage and calibrate. See README.md for the optional"
echo "background watcher (launchd/) and config (config/usage-calibrator.env.example)."
