#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
COMMANDS_DIR="$CLAUDE_DIR/commands"
ENV_PATH="$CLAUDE_DIR/claude-quota-gauge.env"
MIN_VERSION="2.1.80"

echo "Installing claude-quota-gauge..."

# claude --version preflight -- rate_limits (the two real numbers this tool
# reads) doesn't exist before 2.1.80. The statusline itself handles an old
# version gracefully at runtime (prints "usage: unavailable (Claude Code
# X < 2.1.80)" instead of guessing), but flagging it here too means a friend
# installing this for the first time learns the real requirement up front
# instead of wondering why the bar looks incomplete.
if command -v claude >/dev/null 2>&1; then
  CLAUDE_VERSION="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [[ -n "$CLAUDE_VERSION" ]]; then
    OLDEST="$(printf '%s\n%s\n' "$CLAUDE_VERSION" "$MIN_VERSION" | sort -V | head -1)"
    if [[ "$OLDEST" == "$CLAUDE_VERSION" && "$CLAUDE_VERSION" != "$MIN_VERSION" ]]; then
      echo "  warning: Claude Code $CLAUDE_VERSION is older than $MIN_VERSION -- the two"
      echo "  real usage numbers (rate_limits) won't be available until you upgrade."
      echo "  The statusline will say so plainly rather than guess; everything else"
      echo "  here still installs fine."
    fi
  fi
else
  echo "  note: couldn't find \`claude\` on PATH to check its version -- skipping the"
  echo "  Claude Code $MIN_VERSION+ requirement check."
fi

mkdir -p "$SCRIPTS_DIR" "$COMMANDS_DIR"
cp "$SCRIPT_DIR"/bin/*.py "$SCRIPTS_DIR/"
chmod +x "$SCRIPTS_DIR"/*.py
cp "$SCRIPT_DIR"/commands/pending.md "$COMMANDS_DIR/"
cp "$SCRIPT_DIR"/commands/gauge-calibrate.md "$COMMANDS_DIR/"
echo "  copied scripts to $SCRIPTS_DIR"
echo "  copied /pending and /gauge-calibrate to $COMMANDS_DIR"

# Scaffold the config file so every optional variable is discoverable in one
# place -- every value stays commented out (i.e. still defaulted) until you
# choose to change it. Never overwrites an existing config.
if [[ -f "$ENV_PATH" ]]; then
  echo "  config already exists at $ENV_PATH, left it alone"
elif [[ -f "$CLAUDE_DIR/usage-calibrator.env" ]]; then
  echo "  found existing config at the old path (~/.claude/usage-calibrator.env)"
  echo "    -- still works (scripts fall back to it), left it as-is"
else
  cp "$SCRIPT_DIR/config/claude-quota-gauge.env.example" "$ENV_PATH"
  echo "  copied config/claude-quota-gauge.env.example to $ENV_PATH"
fi

echo ""
read -r -p "Wire the statusline and hooks into ~/.claude/settings.json now? [Y/n] " REPLY
REPLY="${REPLY:-Y}"
if [[ "$REPLY" =~ ^[Yy] ]]; then
  python3 - "$CLAUDE_DIR/settings.json" \
    "python3 $SCRIPTS_DIR/usage-statusline.py" \
    "python3 $SCRIPTS_DIR/usage-session-hook.py" \
    "python3 $SCRIPTS_DIR/theme-watch-prompt-hook.py" \
    "python3 $SCRIPTS_DIR/fable-stale-prompt-hook.py" <<'PYEOF'
import json, os, sys

settings_path, statusline_command, hook_command, prompt_hook_command, fable_prompt_hook_command = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

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
    print("  (no-ops unless CLAUDE_USAGE_THEME_WATCH=1 is set -- see config/claude-quota-gauge.env.example)")

fable_prompt_already_present = any(
    h.get("command") and normalize(h["command"]) == normalize(fable_prompt_hook_command)
    for group in user_prompt_submit
    for h in group.get("hooks", [])
)

if fable_prompt_already_present:
    print("  fable-stale UserPromptSubmit hook already present, left settings.json unchanged")
else:
    user_prompt_submit.append({"hooks": [{"type": "command", "command": fable_prompt_hook_command, "timeout": 5}]})
    print(f"  added UserPromptSubmit hook: {fable_prompt_hook_command}")
    print("  (no-ops unless the tracked-model weekly estimate is stale mid-session)")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
PYEOF
else
  echo "  skipped. Add these to ~/.claude/settings.json yourself:"
  echo '    "statusLine": { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/usage-statusline.py", "refreshInterval": 60 }'
  echo '    "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/usage-session-hook.py", "timeout": 15 } ] } ], "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/theme-watch-prompt-hook.py", "timeout": 5 } ] } ], { "hooks": [ { "type": "command", "command": "python3 '"$SCRIPTS_DIR"'/fable-stale-prompt-hook.py", "timeout": 5 } ] } ] }'
fi

# --- Optional: pending tracking -------------------------------------------
# Off by default, by design -- the `pending: N` segment only ever appears
# once a PENDING.md file actually exists somewhere the statusline looks.
# Explained in full here (not just linked in README) so a first-time
# installer sees exactly what it is before deciding, rather than either
# never discovering it or getting it forced on unasked.
echo ""
echo "One more optional feature: pending tracking."
echo ""
echo "  The statusline can show a running 'pending: N' count -- parked issues"
echo "  or open questions, one '## ' heading per item in a PENDING.md file"
echo "  (a sibling convention to CLAUDE.md/AGENTS.md). Headings with RESOLVED"
echo "  in the title stay in the file for reference but don't count. It's a"
echo "  standing, ambient reminder in your bar that something's still open --"
echo "  a /pending <what's parked> command (already installed) adds one from"
echo "  inside any session."
echo ""
echo "  Off by default: nothing shows until a PENDING.md exists somewhere"
echo "  claude-quota-gauge looks (./PENDING.md in your cwd, then"
echo "  ~/.claude/PENDING.md, or wherever CLAUDE_USAGE_PENDING_FILE points)."
echo ""
read -r -p "Create a starter ~/.claude/PENDING.md now so pending: N shows up? [y/N] " PENDING_REPLY
PENDING_REPLY="${PENDING_REPLY:-N}"
if [[ "$PENDING_REPLY" =~ ^[Yy] ]]; then
  PENDING_PATH="$CLAUDE_DIR/PENDING.md"
  if [[ -f "$PENDING_PATH" ]]; then
    echo "  $PENDING_PATH already exists, left it alone"
  else
    cp "$SCRIPT_DIR/examples/PENDING.md" "$PENDING_PATH" 2>/dev/null || cat > "$PENDING_PATH" <<'MDEOF'
# PENDING

Parked issues and open questions -- one `## ` heading per item, newest on
top. Written with enough detail that a cold session (yours next week, or a
different model entirely) can pick one up without re-deriving context.

A heading with "RESOLVED" in it (any case) still lives here for reference
but is excluded from the `pending: N` count -- close things out by editing
the title, not by deleting the section.
MDEOF
    echo "  created $PENDING_PATH -- pending: 0 will show in your statusline"
    echo "  (once the settings wiring above is applied). Add an item any time"
    echo "  with /pending <what's parked>."
  fi
else
  echo "  skipped -- pending stays off, exactly as designed. Enable it later"
  echo "  any time by creating ~/.claude/PENDING.md (see examples/PENDING.md"
  echo "  for the shape), or a ./PENDING.md in a specific project, or by"
  echo "  pointing CLAUDE_USAGE_PENDING_FILE at a file of your choice in"
  echo "  $ENV_PATH."
fi

echo ""
echo "======================================================================"
echo "Done. Here's what to expect:"
echo ""
echo "  Right away, once Claude Code renders a statusline (usually within a"
echo "  few seconds of opening a session): your real 5h and weekly % --"
echo "  Anthropic's own numbers via rate_limits, no calibration step, nothing"
echo "  to run by hand. If Claude Code is older than $MIN_VERSION, the bar"
echo "  says so plainly there instead of showing a number."
echo ""
echo "  Optional, opt-in separately:"
echo "    - a separate weekly % for one model (e.g. Fable), if your account"
echo "      has its own pool for it that rate_limits doesn't break out --"
echo "      run /gauge-calibrate once to turn it on"
echo "    - the 'pending: N' count -- set up above, or any time later"
echo "    - a background launchd watcher for threshold notifications, and"
echo "      macOS UI-theme-drift detection (CLAUDE_USAGE_THEME_WATCH=1)"
echo ""
echo "  All optional config lives in one place: $ENV_PATH"
echo "  Full details on every feature: README.md"
echo "======================================================================"
