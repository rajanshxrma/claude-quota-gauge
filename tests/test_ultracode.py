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


def write_live_cache(home, **kwargs):
    """Writes bin/usage-statusline.py's cache format (usage-live.json)
    directly -- this is the file ultracode-mark.py's `on`/`off` snapshot
    against, so tests exercising the observed-cost history don't need to
    round-trip through a full statusline render just to seed it."""
    path = os.path.join(home, ".claude", "scripts", "usage-live.json")
    with open(path, "w") as f:
        json.dump(kwargs, f)
    return path


def write_uc_history(home, entries):
    """Writes bin/usage_common.py's UC_HISTORY_PATH format directly, so
    tests proving ultracode_readiness() picks up observed cost don't need
    to run a real on/off cycle several times over just to build up samples."""
    path = os.path.join(home, ".claude", "scripts", "ultracode-history.json")
    with open(path, "w") as f:
        json.dump(entries, f)
    return path


def read_uc_history(home):
    path = os.path.join(home, ".claude", "scripts", "ultracode-history.json")
    with open(path) as f:
        return json.load(f)


def payload_with_resets(now, five_hour_pct, five_hour_resets_in_s,
                         seven_day_pct, seven_day_resets_in_s):
    """Like basic_payload(), but lets the two pools carry independent
    resets_at offsets -- needed to test the "reset_soon" margin signal on
    one pool without also tripping it on the other."""
    return {
        "model": {"id": "fable-5", "display_name": "Fable 5"},
        "rate_limits": {
            "five_hour": {"used_percentage": five_hour_pct,
                           "resets_at": int(now.timestamp()) + five_hour_resets_in_s},
            "seven_day": {"used_percentage": seven_day_pct,
                          "resets_at": int(now.timestamp()) + seven_day_resets_in_s},
        },
        "version": "2.1.90",
    }


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

    def test_active_marker_from_other_session_is_invisible_here(self):
        # A run owned by a different session must not appear on this
        # session's gauge at all -- each session's indicator stays specific
        # to itself, falling through to the plain readiness verdict exactly
        # as if no marker existed.
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(minutes=42),
                        session_id="session-a")
        payload, _ = basic_payload(datetime.now(timezone.utc), five_hour_pct=90)
        out = run_statusline(payload, home=self.home,
                              extra_env={"CLAUDE_CODE_SESSION_ID": "session-b"}).stdout.strip()
        self.assertNotIn("uc: ON", out)
        self.assertRegex(out, r"\| uc: wait \d+h \d+m \(5h\)$")

    def test_active_marker_with_no_session_id_is_never_mine(self):
        # Markers written before the ownership fix (or by a manual `on`
        # with no CLAUDE_CODE_SESSION_ID in the environment) carry no
        # session id at all -- unclaimable by anyone, so every reader falls
        # through to its own readiness verdict rather than one session
        # lucking into "mine" by matching an empty string against an empty
        # string.
        write_uc_state(self.home, since=datetime.now(timezone.utc) - timedelta(minutes=5),
                        session_id="")
        payload, _ = basic_payload(datetime.now(timezone.utc))
        out = run_statusline(payload, home=self.home,
                              extra_env={"CLAUDE_CODE_SESSION_ID": ""}).stdout.strip()
        self.assertNotIn("uc: ON", out)
        self.assertIn("| uc: ok", out)

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


class ObservedCostHistoryTest(IsolatedHomeTestCase):
    """Coverage for 0.19.0: `on`/`off` snapshotting real pool deltas into
    UC_HISTORY_PATH (see _record_observed_cost() in ultracode-mark.py)."""

    def test_on_off_roundtrip_records_a_sane_delta(self):
        now = datetime.now(timezone.utc)
        far_5h = int(now.timestamp()) + 5 * 3600
        far_week = int(now.timestamp()) + 6 * 24 * 3600
        write_live_cache(self.home, five_hour_pct=10, five_hour_resets_at=far_5h,
                          seven_day_pct=20, seven_day_resets_at=far_week)
        env = {"CLAUDE_CODE_SESSION_ID": "session-mine"}

        on = run_script(MARK, ["on", "--reason", "roundtrip test"], home=self.home, extra_env=env)
        self.assertEqual(on.returncode, 0, on.stderr)

        # Same resets_at (no rollover) but the 5h pool moved 5pts; week held
        # steady -- a real run's shape.
        write_live_cache(self.home, five_hour_pct=15, five_hour_resets_at=far_5h,
                          seven_day_pct=20, seven_day_resets_at=far_week)
        off = run_script(MARK, ["off"], home=self.home, extra_env=env)
        self.assertEqual(off.returncode, 0, off.stderr)

        history = read_uc_history(self.home)
        self.assertEqual(len(history), 1)
        record = history[0]
        self.assertAlmostEqual(record["five_hour_delta"], 5, places=6)
        self.assertAlmostEqual(record["seven_day_delta"], 0, places=6)
        self.assertEqual(record["reason"], "roundtrip test")

    def test_rollover_mid_run_nulls_that_pools_delta_without_failing_the_record(self):
        now = datetime.now(timezone.utc)
        far_5h = int(now.timestamp()) + 5 * 3600
        far_week = int(now.timestamp()) + 6 * 24 * 3600
        write_live_cache(self.home, five_hour_pct=10, five_hour_resets_at=far_5h,
                          seven_day_pct=20, seven_day_resets_at=far_week)
        env = {"CLAUDE_CODE_SESSION_ID": "session-mine"}

        on = run_script(MARK, ["on", "--reason", "rollover test"], home=self.home, extra_env=env)
        self.assertEqual(on.returncode, 0, on.stderr)

        # The 5h window rolled over mid-run: resets_at jumped to a new future
        # epoch and pct dropped low, exactly what a real reset looks like.
        # Recording the raw delta (now_pct - snap_pct, likely negative or
        # nonsensical) would be a bogus number -- this pool's delta must
        # come back null instead. The week pool is untouched, so its delta
        # must still record normally.
        rolled_5h = far_5h + 5 * 3600
        write_live_cache(self.home, five_hour_pct=2, five_hour_resets_at=rolled_5h,
                          seven_day_pct=25, seven_day_resets_at=far_week)
        off = run_script(MARK, ["off"], home=self.home, extra_env=env)
        self.assertEqual(off.returncode, 0, off.stderr)

        record = read_uc_history(self.home)[0]
        self.assertIsNone(record["five_hour_delta"])
        self.assertAlmostEqual(record["seven_day_delta"], 5, places=6)

    def test_history_caps_at_twenty_rows_dropping_oldest(self):
        # 25 pre-existing rows, tagged so the oldest 5 are identifiable.
        entries = [{"since": f"row-{i}", "ended_at": None, "reason": "",
                    "five_hour_delta": 1.0, "seven_day_delta": None,
                    "tracked_delta": None, "tracked_model": None}
                   for i in range(25)]
        write_uc_history(self.home, entries)
        now = datetime.now(timezone.utc)
        far_5h = int(now.timestamp()) + 5 * 3600
        write_live_cache(self.home, five_hour_pct=10, five_hour_resets_at=far_5h)
        env = {"CLAUDE_CODE_SESSION_ID": "session-mine"}
        run_script(MARK, ["on"], home=self.home, extra_env=env)
        write_live_cache(self.home, five_hour_pct=12, five_hour_resets_at=far_5h)
        run_script(MARK, ["off"], home=self.home, extra_env=env)

        history = read_uc_history(self.home)
        self.assertEqual(len(history), 20)
        # The 6 oldest of the original 25 ("row-0" .. "row-5") must be gone;
        # the newest original rows plus the just-appended one survive.
        self.assertNotIn("row-0", [h["since"] for h in history])
        self.assertNotIn("row-4", [h["since"] for h in history])
        self.assertIn("row-24", [h["since"] for h in history])


class ObservedCostReadinessTest(IsolatedHomeTestCase):
    """Coverage for 0.19.0: ultracode_readiness() preferring the real
    observed-cost median over the static default once enough history
    exists (see ultracode_observed_cost() in usage_common.py)."""

    def test_threshold_literally_flips_the_verdict(self):
        # 5h at 90%: against the static default cost (20) + buffer (3) this
        # blocks, exactly like test_wait_names_blocked_pool_and_countdown.
        now = datetime.now(timezone.utc)
        payload, _ = basic_payload(now, five_hour_pct=90)

        # Two samples -- below min_samples=3, so still falls back to the
        # static default and still blocks.
        write_uc_history(self.home, [
            {"since": "a", "ended_at": None, "reason": "", "five_hour_delta": 2.0,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
            {"since": "b", "ended_at": None, "reason": "", "five_hour_delta": 2.0,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
        ])
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("uc: wait", out)

        # A third sample crosses min_samples=3 -- the median observed cost
        # (2pts) plus buffer (3) easily fits in the 10pts of headroom left,
        # so the verdict flips to ok even though nothing about the pool's
        # own % or the env knobs changed.
        write_uc_history(self.home, [
            {"since": "a", "ended_at": None, "reason": "", "five_hour_delta": 2.0,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
            {"since": "b", "ended_at": None, "reason": "", "five_hour_delta": 2.0,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
            {"since": "c", "ended_at": None, "reason": "", "five_hour_delta": 2.0,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
        ])
        data = json.loads(run_statusline(payload, args=["--json"], home=self.home).stdout)
        self.assertEqual(data["ultracode"]["readiness"]["verdict"], "ok")
        self.assertEqual(data["ultracode"]["readiness"]["blockers"], [])

    def test_null_deltas_in_history_are_skipped_not_averaged_in(self):
        # A history full of nothing but rolled-over (null) samples must
        # never be mistaken for 3 real samples -- guards the min_samples
        # count against being satisfied by Nones (which would otherwise
        # blow up statistics.median on an empty list).
        now = datetime.now(timezone.utc)
        payload, _ = basic_payload(now, five_hour_pct=90)
        write_uc_history(self.home, [
            {"since": "a", "ended_at": None, "reason": "", "five_hour_delta": None,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
            {"since": "b", "ended_at": None, "reason": "", "five_hour_delta": None,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
            {"since": "c", "ended_at": None, "reason": "", "five_hour_delta": None,
             "seven_day_delta": None, "tracked_delta": None, "tracked_model": None},
        ])
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("uc: wait", out)


class MarginalSignalTest(IsolatedHomeTestCase):
    """Coverage for 0.19.0: the additive marginal/margin_notes fields on an
    "ok" verdict, and their rendering in the statusline segment (see
    _uc_margin_suffix() in usage_common.py)."""

    def test_thin_and_reset_soon_render_on_the_statusline_segment(self):
        now = datetime.now(timezone.utc)
        # 5h at 75%: static cost 20, buffer 3 -> not blocked (25 >= 23), but
        # headroom_after = (100-75)-20 = 5, under the default 10pt margin ->
        # "thin". Week at 50%, resetting in 5 minutes (< default 600s
        # reset_soon window) with >=15pts already used -> "reset_soon".
        payload = payload_with_resets(now, five_hour_pct=75, five_hour_resets_in_s=5 * 3600,
                                       seven_day_pct=50, seven_day_resets_in_s=300)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertRegex(out, r"\| uc: ok \(5h thin, week in \d+h \d+m\)$")

        data = json.loads(run_statusline(payload, args=["--json"], home=self.home).stdout)
        readiness = data["ultracode"]["readiness"]
        self.assertEqual(readiness["verdict"], "ok")
        self.assertTrue(readiness["marginal"])
        reasons = {n["pool"]: n["reason"] for n in readiness["margin_notes"]}
        self.assertEqual(reasons["5h"], "thin")
        self.assertEqual(reasons["week"], "reset_soon")

    def test_plenty_of_headroom_is_not_marginal(self):
        now = datetime.now(timezone.utc)
        payload, _ = basic_payload(now, five_hour_pct=10, seven_day_pct=10)
        out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertTrue(out.endswith("| uc: ok"))
        data = json.loads(run_statusline(payload, args=["--json"], home=self.home).stdout)
        readiness = data["ultracode"]["readiness"]
        self.assertFalse(readiness["marginal"])
        self.assertEqual(readiness["margin_notes"], [])


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

    def test_hook_is_silent_about_other_sessions_run(self):
        # A run owned by a different session must not surface here at all
        # -- this session's context stays specific to itself, falling
        # through to the plain budget verdict exactly as if idle.
        self._seed_cache()
        write_uc_state(self.home, reason="repo-wide audit", session_id="session-a")
        ctx = self._hook_context(extra_env={"CLAUDE_CODE_SESSION_ID": "session-b"})
        self.assertNotIn("another session", ctx)
        self.assertNotIn("repo-wide audit", ctx)
        self.assertNotIn("marked ACTIVE", ctx)
        self.assertIn("ultracode budget: ok", ctx)


if __name__ == "__main__":
    unittest.main()
