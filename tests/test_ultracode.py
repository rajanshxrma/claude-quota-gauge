"""Tests for the ultracode gauge (0.15.0): the active-run marker written by
bin/ultracode-mark.py, the affordability verdict, the statusline "uc:"
segment in both output modes, and the SessionStart hook's context line.

Same isolation discipline as test_usage_statusline_json.py: every test runs
the real scripts as subprocesses against a throwaway HOME, never the
machine's live state files.
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

from test_usage_statusline_json import (
    IsolatedHomeTestCase, REPO_ROOT, basic_payload, run_statusline,
)

MARK = os.path.join(REPO_ROOT, "bin", "ultracode-mark.py")
HOOK = os.path.join(REPO_ROOT, "bin", "usage-session-hook.py")


def run_script(script, args=None, home=None, extra_env=None):
    # CLAUDE_CODE_SESSION_ID stripped from the inherited base for the same
    # reason as run_statusline() in test_usage_statusline_json.py: whatever
    # session happens to be running the tests must not silently become the
    # "owner" of a test-written marker. Tests that need a specific id (or
    # none at all) set it explicitly via extra_env.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE_USAGE") and k != "CLAUDE_CODE_SESSION_ID"
    }
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["PYTHONUTF8"] = "1"
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, script] + (args or []),
        capture_output=True, text=True, env=env, timeout=30,
    )


def write_uc_state(home, active=True, since=None, reason="test task", session_id=""):
    path = os.path.join(home, ".claude", "scripts", "ultracode-state.json")
    since = since or datetime.now(timezone.utc)
    with open(path, "w") as f:
        json.dump({
            "active": active, "since": since.isoformat(), "reason": reason,
            "session_id": session_id,
        }, f)
    return path


class ReadinessSegmentTest(IsolatedHomeTestCase):
    def test_ok_when_all_pools_have_headroom(self):
        payload, _ = basic_payload(datetime.now(timezone.utc))
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("| uc: ok", out)

    def test_wait_names_blocked_pool_and_countdown(self):
        # 5h at 90%: headroom 10 < default cost 20 + buffer 3. Week at 34%
        # stays clear, so only "5h" may be named.
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertRegex(out, r"\| uc: wait \d+h \d+m \(5h\)$")

    def test_wait_on_weekly_pool(self):
        payload, _ = basic_payload(datetime.now(timezone.utc), seven_day_pct=95)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertRegex(out, r"\| uc: wait \d+h \d+m \(week\)$")

    def test_cost_knobs_are_env_tunable(self):
        # Same 90% five-hour block, but with the run-cost knob turned down
        # the verdict flips to ok -- proves the env override actually lands.
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        result = run_statusline(payload, home=self.home)
        self.assertIn("uc: wait", result.stdout)
        env_path = os.path.join(self.home, ".claude", "claude-quota-gauge.env")
        with open(env_path, "w") as f:
            f.write("CLAUDE_USAGE_UC_COST_5H=5\nCLAUDE_USAGE_UC_BUFFER=2\n")
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("| uc: ok", out)


class NoUcSegmentFlagTest(IsolatedHomeTestCase):
    def test_flag_strips_segment_but_not_json(self):
        # The combined statusline.py wrapper passes this flag and renders the
        # indicator on the workload line itself; standalone default keeps it.
        payload, _ = basic_payload(datetime.now(timezone.utc))
        out = run_statusline(payload, args=["--no-uc-segment"], home=self.home).stdout.strip()
        self.assertNotIn("uc:", out)
        data = json.loads(run_statusline(
            payload, args=["--json", "--no-uc-segment"], home=self.home).stdout)
        self.assertEqual(data["ultracode"]["readiness"]["verdict"], "ok")


class ActiveMarkerTest(IsolatedHomeTestCase):
    def test_active_marker_wins_over_readiness(self):
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(minutes=42),
                        session_id="session-mine")
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        out = run_statusline(payload, home=self.home,
                              extra_env={"CLAUDE_CODE_SESSION_ID": "session-mine"}).stdout.strip()
        self.assertIn("| uc: ON 42m", out)
        self.assertNotIn("uc: wait", out)

    def test_active_marker_from_other_session_shows_elsewhere(self):
        # A run owned by a different session is real, shown info -- but
        # tagged distinctly and never the loud "uc: ON <n>m" form, so it
        # can't be mistaken for this session's own run.
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(minutes=42),
                        session_id="session-a")
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        out = run_statusline(payload, home=self.home,
                              extra_env={"CLAUDE_CODE_SESSION_ID": "session-b"}).stdout.strip()
        self.assertIn("| uc: ON elsewhere 42m", out)
        self.assertNotRegex(out, r"\| uc: ON 42m$")

    def test_active_marker_with_no_session_id_is_never_mine(self):
        # Markers written before this ownership fix (or by a manual `on`
        # with no CLAUDE_CODE_SESSION_ID in the environment) carry no
        # session id at all -- unclaimable by anyone, so every reader sees
        # "elsewhere" rather than one session lucking into "mine" by
        # matching an empty string against an empty string.
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(minutes=5),
                        session_id="")
        payload, _ = basic_payload(datetime.now(timezone.utc))
        out = run_statusline(payload, home=self.home,
                              extra_env={"CLAUDE_CODE_SESSION_ID": ""}).stdout.strip()
        self.assertIn("| uc: ON elsewhere 5m", out)

    def test_expired_marker_falls_back_to_readiness(self):
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(hours=5),
                        session_id="session-mine")
        payload, _ = basic_payload(datetime.now(timezone.utc))
        out = run_statusline(payload, home=self.home,
                              extra_env={"CLAUDE_CODE_SESSION_ID": "session-mine"}).stdout.strip()
        self.assertIn("| uc: ok", out)
        self.assertNotIn("uc: ON", out)

    def test_mark_on_off_roundtrip(self):
        payload, _ = basic_payload(datetime.now(timezone.utc))
        env = {"CLAUDE_CODE_SESSION_ID": "session-mine"}
        result = run_script(MARK, ["on", "--reason", "big refactor"], home=self.home,
                             extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = run_statusline(payload, home=self.home, extra_env=env).stdout.strip()
        self.assertIn("| uc: ON 0m", out)

        status = json.loads(run_script(MARK, ["status"], home=self.home, extra_env=env).stdout)
        self.assertTrue(status["active"])
        self.assertTrue(status["mine"])
        self.assertEqual(status["reason"], "big refactor")

        # A different session can't clear it without --force...
        other = {"CLAUDE_CODE_SESSION_ID": "session-other"}
        refused = run_script(MARK, ["off"], home=self.home, extra_env=other)
        self.assertNotEqual(refused.returncode, 0)
        status = json.loads(run_script(MARK, ["status"], home=self.home, extra_env=env).stdout)
        self.assertTrue(status["active"])

        # ...but the owning session can, plainly.
        run_script(MARK, ["off"], home=self.home, extra_env=env)
        out = run_statusline(payload, home=self.home, extra_env=env).stdout.strip()
        self.assertNotIn("uc: ON", out)
        status = json.loads(run_script(MARK, ["status"], home=self.home, extra_env=env).stdout)
        self.assertFalse(status["active"])

    def test_off_with_force_clears_other_sessions_marker(self):
        env_a = {"CLAUDE_CODE_SESSION_ID": "session-a"}
        env_b = {"CLAUDE_CODE_SESSION_ID": "session-b"}
        run_script(MARK, ["on", "--reason", "batch job"], home=self.home, extra_env=env_a)
        forced = run_script(MARK, ["off", "--force"], home=self.home, extra_env=env_b)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        status = json.loads(run_script(MARK, ["status"], home=self.home, extra_env=env_a).stdout)
        self.assertFalse(status["active"])


class JsonOutputTest(IsolatedHomeTestCase):
    def test_json_carries_ultracode_block(self):
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        data = json.loads(run_statusline(payload, args=["--json"], home=self.home).stdout)
        uc = data["ultracode"]
        self.assertFalse(uc["active"])
        self.assertEqual(uc["readiness"]["verdict"], "wait")
        self.assertEqual(uc["readiness"]["blockers"], ["5h"])

    def test_json_active(self):
        write_uc_state(self.home)
        payload, _ = basic_payload(datetime.now(timezone.utc))
        data = json.loads(run_statusline(payload, args=["--json"], home=self.home).stdout)
        self.assertTrue(data["ultracode"]["active"])
        self.assertIsNotNone(data["ultracode"]["since"])


class SessionHookTest(IsolatedHomeTestCase):
    def _hook_context(self, extra_env=None):
        result = run_script(HOOK, home=self.home, extra_env=extra_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def _seed_cache(self, five_hour_pct=12):
        # The hook reads the cache the statusline wrote; render once to seed it.
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=five_hour_pct)
        run_statusline(payload, home=self.home)

    def test_hook_is_informational_without_auto_flag(self):
        self._seed_cache()
        ctx = self._hook_context()
        self.assertIn("ultracode budget: ok", ctx)
        self.assertNotIn("Standing auto-mode", ctx)

    def test_hook_carries_directive_with_auto_flag(self):
        self._seed_cache()
        ctx = self._hook_context(extra_env={"CLAUDE_USAGE_UC_AUTO": "1"})
        self.assertIn("Standing auto-mode is also ON", ctx)
        self.assertIn("ultracode-mark.py", ctx)

    def test_hook_budget_gates_auto_mode(self):
        self._seed_cache(five_hour_pct=90)
        ctx = self._hook_context(extra_env={"CLAUDE_USAGE_UC_AUTO": "1"})
        self.assertIn("budget-gated", ctx)
        self.assertIn("do NOT start a Workflow run", ctx)

    def test_hook_surfaces_own_active_run_with_off_instruction(self):
        self._seed_cache()
        write_uc_state(self.home, reason="repo-wide audit", session_id="session-mine")
        ctx = self._hook_context(extra_env={"CLAUDE_CODE_SESSION_ID": "session-mine"})
        self.assertIn("marked ACTIVE", ctx)
        self.assertIn("repo-wide audit", ctx)
        self.assertIn("ultracode-mark.py off", ctx)

    def test_hook_surfaces_other_sessions_run_without_off_instruction(self):
        # The reading session didn't start this run and can't know whether
        # it's finished -- it should hear about it, but never be told to
        # turn it off itself.
        self._seed_cache()
        write_uc_state(self.home, reason="repo-wide audit", session_id="session-a")
        ctx = self._hook_context(extra_env={"CLAUDE_CODE_SESSION_ID": "session-b"})
        self.assertIn("another session", ctx)
        self.assertIn("repo-wide audit", ctx)
        self.assertIn("shouldn't turn it off", ctx)
        self.assertNotIn("If it has finished, run", ctx)


if __name__ == "__main__":
    unittest.main()
