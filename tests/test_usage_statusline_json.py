"""Tests for the --json flag on bin/usage-statusline.py.

Run with:  python3 -m unittest discover -s tests -v
(no third-party deps -- stdlib unittest only, matching the rest of this
project's zero-dependency scripts.)

Every test drives the real script as a subprocess with an isolated HOME (a
fresh tempdir per test, standing in for ~/.claude/scripts,
~/.claude/projects, etc.) so nothing here ever reads or writes Rajan's real
usage-live.json, fable calibration, or PENDING.md -- see
feedback-isolate-tests-from-live-state-files.md for why that separation is
non-negotiable.

Coverage:
  - default (no-flag) text output is byte-shape unchanged
  - --json emits valid JSON whose numbers match the same-input text output
  - --json field values are correct against a known/mocked calibrated
    tracked-model state (a fake fable calibration + one fake transcript
    entry with a hand-computed expected cost)
  - the fully-dark fallback (no rate_limits, no cache) emits valid JSON with
    the correct `unavailable` reason (outdated_cli vs. no_data)
  - the tracked-model stale sub-states (within-grace / past-grace, each with
    and without a cached %) report the correct `stale_past_grace` /
    `stale_elapsed_hours` / `pct` combination
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "bin", "usage-statusline.py")


def run_statusline(payload, args=None, home=None):
    """Runs the real script as a subprocess against an isolated HOME.

    Strips any CLAUDE_USAGE_* vars inherited from the real shell/env file so
    a developer's actual ~/.claude/claude-quota-gauge.env can never leak
    into a test run (load_env_file() only sets vars that aren't already
    set, so an inherited real value would otherwise silently win).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_USAGE")}
    env["HOME"] = home
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    cmd = [sys.executable, SCRIPT] + (args or [])
    return subprocess.run(
        cmd, input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=home, timeout=15,
    )


class IsolatedHomeTestCase(unittest.TestCase):
    """Base class: gives every test its own throwaway HOME so scripts that
    read/write ~/.claude/scripts/*.json or ~/.claude/projects/**/*.jsonl
    never touch the real machine state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        os.makedirs(os.path.join(self.home, ".claude", "scripts"), exist_ok=True)
        os.makedirs(os.path.join(self.home, ".claude", "projects"), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()


def basic_payload(now, five_hour_pct=12, seven_day_pct=34, hours_to_reset=5):
    resets_at = int((now + timedelta(hours=hours_to_reset)).timestamp())
    return {
        "model": {"id": "fable-5", "display_name": "Fable 5"},
        "rate_limits": {
            "five_hour": {"used_percentage": five_hour_pct, "resets_at": resets_at},
            "seven_day": {"used_percentage": seven_day_pct, "resets_at": resets_at},
        },
        "version": "2.1.90",
    }, resets_at


class DefaultTextOutputUnchangedTest(IsolatedHomeTestCase):
    """--json must be purely additive -- the pre-existing default behavior
    (no flag) is not allowed to change shape or content."""

    def test_default_output_shape_and_values(self):
        now = datetime.now(timezone.utc)
        payload, resets_at = basic_payload(now)
        result = run_statusline(payload, home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.strip()
        self.assertRegex(
            out,
            r"^Fable 5 \| 5h: 12% \(resets \d+h \d+m\) \| week: 34% \(resets \d+h \d+m\)$",
        )

    def test_default_output_is_not_json(self):
        now = datetime.now(timezone.utc)
        payload, _ = basic_payload(now)
        out = run_statusline(payload, home=self.home).stdout.strip()
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


class JsonFlagBasicTest(IsolatedHomeTestCase):
    """The common/healthy-path case: rate_limits present, no fable
    calibration on file yet (tracked_model is honestly null, not guessed)."""

    def test_json_is_valid_and_matches_text_output(self):
        now = datetime.now(timezone.utc)
        payload, resets_at = basic_payload(now)

        text_out = run_statusline(payload, home=self.home).stdout.strip()
        json_result = run_statusline(payload, args=["--json"], home=self.home)
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        json_out = json_result.stdout.strip()

        data = json.loads(json_out)  # fails the test if this isn't valid JSON

        self.assertEqual(data["model"], "Fable 5")
        self.assertEqual(data["five_hour"], {"pct": 12, "resets_at": resets_at, "cached": False})
        self.assertEqual(data["seven_day"], {"pct": 34, "resets_at": resets_at, "cached": False})
        self.assertIsNone(data["tracked_model"])
        self.assertIsNone(data["pending_tasks"])
        self.assertIn("generated_at", data)

        # Same underlying numbers, never allowed to disagree between the two
        # output modes for the same input.
        self.assertIn("5h: 12%", text_out)
        self.assertIn("week: 34%", text_out)

    def test_json_flag_does_not_alter_default_output(self):
        """Sanity check on the flag gate itself: invoking with --json must
        never fall back to accidentally also printing the text line
        alongside it -- exactly one output format per invocation, and it
        must be the JSON one when the flag is given."""
        now = datetime.now(timezone.utc)
        payload, _ = basic_payload(now)
        json_out = run_statusline(payload, args=["--json"], home=self.home).stdout.strip()
        self.assertTrue(json_out.startswith("{"))
        self.assertNotIn(" | ", json_out)  # the bar's segment separator never appears in JSON


class JsonFlagKnownCalibratedStateTest(IsolatedHomeTestCase):
    """Field values against a known/mocked quota state: a real fable
    calibration file plus one fake local transcript entry with a
    hand-computed expected cost, so the resulting pct isn't just "whatever
    the code produces" but a value independently verified here."""

    def _write_calibration(self, now, cap, seven_day_pct_at_cal):
        next_reset = now + timedelta(days=3)
        window_start = next_reset - timedelta(days=7)
        cal = {
            "tracked_model": "fable",
            "next_reset": next_reset.isoformat(),
            "window_start": window_start.isoformat(),
            "cap": cap,
            "cap_derived_at": (now - timedelta(hours=1)).isoformat(),
            "tokens_at_cal": 0,
            "pct": 0,
            "seven_day_pct_at_cal": seven_day_pct_at_cal,
            "local_total_at_cal": 0,
            "calibrated_at": (now - timedelta(hours=1)).isoformat(),
        }
        cal_path = os.path.join(self.home, ".claude", "scripts", "usage-fable-calibration.json")
        with open(cal_path, "w") as f:
            json.dump(cal, f)
        return int(next_reset.timestamp())

    def _write_transcript(self, now, model, input_tokens, output_tokens):
        proj_dir = os.path.join(self.home, ".claude", "projects", "testproj")
        os.makedirs(proj_dir, exist_ok=True)
        entry = {
            "timestamp": now.isoformat(),
            "message": {
                "model": model,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        }
        with open(os.path.join(proj_dir, "session.jsonl"), "w") as f:
            f.write(json.dumps(entry) + "\n")

    def test_tracked_model_pct_matches_hand_computed_cost(self):
        now = datetime.now(timezone.utc)
        cap = 100  # arbitrary unit -- see usage_common.fable_estimate()'s cap model
        seven_day_pct = 34
        next_reset_epoch = self._write_calibration(now, cap, seven_day_pct)
        # claude-fable-5 pricing (see tokens-since.py PRICING): $10/1M input,
        # $50/1M output. 1,000,000 input tokens, 0 output -> exactly $10.00 of
        # cost -> 10.00 / cap(100) * 100 = 10.0%.
        self._write_transcript(now, "claude-fable-5", input_tokens=1_000_000, output_tokens=0)
        expected_pct = 10.0

        payload = {
            "model": {"id": "fable-5", "display_name": "Fable 5"},
            "rate_limits": {
                "five_hour": {"used_percentage": 12, "resets_at": next_reset_epoch},
                "seven_day": {"used_percentage": seven_day_pct, "resets_at": next_reset_epoch},
            },
        }
        result = run_statusline(payload, args=["--json"], home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())

        self.assertIsNotNone(data["tracked_model"])
        self.assertEqual(data["tracked_model"]["name"], "fable")
        self.assertFalse(data["tracked_model"]["stale"])
        self.assertAlmostEqual(data["tracked_model"]["pct"], expected_pct, places=6)
        self.assertEqual(data["tracked_model"]["resets_at"], next_reset_epoch)
        # Not stale -> both stale-only fields are null, not 0/False.
        self.assertIsNone(data["tracked_model"]["stale_elapsed_hours"])
        self.assertIsNone(data["tracked_model"]["stale_past_grace"])

        # The text line must report the exact same number, just formatted.
        text_out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("fable: 10%", text_out)


class JsonFlagStaleSubStatesTest(IsolatedHomeTestCase):
    """The four stale sub-states the bar text renders as distinct human
    copy -- within-grace vs. past-grace, each with and without a cached %
    to fall back on -- fully reconstructed from `stale_past_grace` +
    `stale_elapsed_hours` + `pct` rather than left as a bare `stale: bool`."""

    GRACE_HOURS = 12  # matches usage_common._cap_max_age()'s default

    def _write_stale_calibration(self, now, calibrated_at):
        """A calibration whose cap_derived_at is old enough to make
        fable_estimate() report stale=True via the max-age path."""
        cal = {
            "tracked_model": "fable",
            "next_reset": (now + timedelta(days=3)).isoformat(),
            "window_start": (now + timedelta(days=3) - timedelta(days=7)).isoformat(),
            "cap": 100,
            "cap_derived_at": calibrated_at.isoformat(),
            "tokens_at_cal": 0,
            "pct": 0,
            "calibrated_at": calibrated_at.isoformat(),
        }
        cal_path = os.path.join(self.home, ".claude", "scripts", "usage-fable-calibration.json")
        with open(cal_path, "w") as f:
            json.dump(cal, f)

    def _write_live_cache(self, calibrated_at, stale_since, fable_pct=None):
        """Pre-seeds the cache fable_stale_elapsed() reads, so the test
        controls elapsed time directly instead of racing a real clock.
        `fable_stale_identity` must match the calibration's calibrated_at
        exactly, or fable_stale_elapsed() resets the clock to "just now"."""
        cache = {
            "fable_stale_identity": calibrated_at.isoformat(),
            "fable_stale_since": stale_since.isoformat(),
        }
        if fable_pct is not None:
            cache["fable_pct"] = fable_pct
            cache["fable_resets_at"] = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
        cache_path = os.path.join(self.home, ".claude", "scripts", "usage-live.json")
        with open(cache_path, "w") as f:
            json.dump(cache, f)

    def _run(self, elapsed_hours, fable_pct):
        now = datetime.now(timezone.utc)
        calibrated_at = now - timedelta(hours=self.GRACE_HOURS + 5)  # old enough to be stale
        self._write_stale_calibration(now, calibrated_at)
        self._write_live_cache(calibrated_at, now - timedelta(hours=elapsed_hours), fable_pct=fable_pct)
        payload, _ = basic_payload(now)
        result = run_statusline(payload, args=["--json"], home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())["tracked_model"]

    def test_past_grace_with_cached_pct(self):
        data = self._run(elapsed_hours=self.GRACE_HOURS + 2, fable_pct=42)
        self.assertTrue(data["stale"])
        self.assertTrue(data["stale_past_grace"])
        self.assertAlmostEqual(data["stale_elapsed_hours"], self.GRACE_HOURS + 2, delta=0.1)
        self.assertEqual(data["pct"], 42)

    def test_past_grace_no_cached_pct(self):
        data = self._run(elapsed_hours=self.GRACE_HOURS + 2, fable_pct=None)
        self.assertTrue(data["stale"])
        self.assertTrue(data["stale_past_grace"])
        self.assertAlmostEqual(data["stale_elapsed_hours"], self.GRACE_HOURS + 2, delta=0.1)
        self.assertIsNone(data["pct"])

    def test_within_grace_with_cached_pct(self):
        data = self._run(elapsed_hours=2, fable_pct=42)
        self.assertTrue(data["stale"])
        self.assertFalse(data["stale_past_grace"])
        self.assertAlmostEqual(data["stale_elapsed_hours"], 2, delta=0.1)
        self.assertEqual(data["pct"], 42)

    def test_within_grace_no_cached_pct(self):
        data = self._run(elapsed_hours=2, fable_pct=None)
        self.assertTrue(data["stale"])
        self.assertFalse(data["stale_past_grace"])
        self.assertAlmostEqual(data["stale_elapsed_hours"], 2, delta=0.1)
        self.assertIsNone(data["pct"])


class JsonFlagUnavailableStateTest(IsolatedHomeTestCase):
    """The fully-dark case (no rate_limits, no prior cache): --json must
    emit valid JSON with a `data["unavailable"]` dict that names *why*,
    mirroring the bar text's own two-variant distinction (old CLI vs. no
    data yet) instead of a bare boolean a caller can't act on."""

    def test_outdated_cli_reason(self):
        payload = {"version": "2.1.9"}  # below MIN_VERSION, no rate_limits
        text_out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("usage: unavailable (Claude Code 2.1.9 < 2.1.80)", text_out)

        json_result = run_statusline(payload, args=["--json"], home=self.home)
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        data = json.loads(json_result.stdout.strip())
        self.assertIsNone(data["five_hour"])
        self.assertIsNone(data["seven_day"])
        self.assertEqual(data["unavailable"], {
            "reason": "outdated_cli", "cli_version": "2.1.9", "min_version": "2.1.80",
        })

    def test_no_data_reason(self):
        payload = {}  # no version, no rate_limits, no prior cache -- fresh install
        text_out = run_statusline(payload, home=self.home).stdout.strip()
        self.assertIn("usage: unavailable", text_out)
        self.assertNotIn("Claude Code", text_out)  # must not blame a version it never saw

        json_result = run_statusline(payload, args=["--json"], home=self.home)
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        data = json.loads(json_result.stdout.strip())
        self.assertEqual(data["unavailable"], {
            "reason": "no_data", "cli_version": None, "min_version": None,
        })


if __name__ == "__main__":
    unittest.main()
