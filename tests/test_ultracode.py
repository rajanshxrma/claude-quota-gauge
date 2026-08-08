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
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_USAGE")}
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


def write_uc_state(home, active=True, since=None, reason="test task"):
    path = os.path.join(home, ".claude", "scripts", "ultracode-state.json")
    since = since or datetime.now(timezone.utc)
    with open(path, "w") as f:
        json.dump({"active": active, "since": since.isoformat(), "reason": reason}, f)
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
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(minutes=42))
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("| uc: ON 42m", out)
        self.assertNotIn("uc: wait", out)

    def test_expired_marker_falls_back_to_readiness(self):
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(hours=5))
        payload, _ = basic_payload(datetime.now(timezone.utc))
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("| uc: ok", out)
        self.assertNotIn("uc: ON", out)

    def test_mark_on_off_roundtrip(self):
        payload, _ = basic_payload(datetime.now(timezone.utc))
        result = run_script(MARK, ["on", "--reason", "big refactor"], home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("| uc: ON 0m", out)

        status = json.loads(run_script(MARK, ["status"], home=self.home).stdout)
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "big refactor")

        run_script(MARK, ["off"], home=self.home)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertNotIn("uc: ON", out)
        status = json.loads(run_script(MARK, ["status"], home=self.home).stdout)
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

    def test_hook_surfaces_active_run(self):
        self._seed_cache()
        write_uc_state(self.home, reason="repo-wide audit")
        ctx = self._hook_context()
        self.assertIn("marked ACTIVE", ctx)
        self.assertIn("repo-wide audit", ctx)


if __name__ == "__main__":
    unittest.main()
