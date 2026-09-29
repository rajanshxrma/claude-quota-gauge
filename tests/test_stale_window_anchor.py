"""Regression tests for the stale-window-anchor bug found live 2026-09-06.

A weekly boundary that has already passed was being taken verbatim as the
*current* window's boundary in two places, which put `window_start` a full
week early and made everything downstream reason about the wrong seven days:

  1. `bin/usage-calibrate-fable.py` anchored its window (and therefore
     `tokens_at_cal`, and therefore the derived cap) to whatever
     `seven_day_resets_at` happened to be sitting in the shared
     `usage-live.json`. That cache is written by every open session's
     statusline render, and Claude Code re-renders on the refresh interval
     with whatever rate_limits a session last received -- so a long-idle
     session keeps writing a boundary from days ago. Calibrating against one
     summed an extra week of usage into the numerator and inflated the cap by
     that same ratio: a verified 81% read came back out of the gauge as 18%.
  2. `fable_estimate()` in `bin/usage_common.py` had the mirror-image half --
     it trusted a caller-supplied `resets_at` unconditionally, so the very
     same stale boundary made it project against a week-and-a-bit of tokens.
     Which of the two sessions wrote the cache last decided whether the bar
     showed 81% or 18%.

The fixes are three, one per test class below: roll a past boundary forward
wherever it comes from (both sites), never let a render regress the shared
cache to an older window, and treat a calibration whose own `next_reset`
predates its own `calibrated_at` as stale rather than trusting a cap that is
known to have been derived from mismatched inputs.

Run with:  python3 -m unittest discover -s tests -v

Same isolation rule as the rest of the suite: every script runs as a
subprocess against a throwaway HOME with every CLAUDE_USAGE_* var stripped,
so nothing here can read or write the real ~/.claude/scripts state.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO_ROOT, "bin")
STATUSLINE = os.path.join(BIN, "usage-statusline.py")
CALIBRATE = os.path.join(BIN, "usage-calibrate-fable.py")

WEEK = timedelta(days=7)


def run_script(script, home, args=None, payload=None):
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE_USAGE") and k != "CLAUDE_CODE_SESSION_ID"
    }
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["PYTHONUTF8"] = "1"
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    # Count in the call (0.23.0): a redraw otherwise reads the last finished
    # scan and starts a new one detached, and these tests pin the arithmetic
    # of the estimate, not the scheduling (tests/test_live_progress.py does).
    env["CLAUDE_USAGE_SCAN_WAIT"] = "1"
    return subprocess.run(
        [sys.executable, script] + (args or []),
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=home, timeout=30,
    )


class IsolatedHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.scripts = os.path.join(self.home, ".claude", "scripts")
        os.makedirs(self.scripts, exist_ok=True)
        os.makedirs(os.path.join(self.home, ".claude", "projects"), exist_ok=True)
        # usage-calibrate-fable.py shells out to tokens-since.py from the
        # *installed* scripts dir (~/.claude/scripts), not from alongside
        # itself the way usage_common.py does -- so the isolated HOME has to
        # look like a real install for it, exactly as install.sh makes one.
        shutil.copy2(os.path.join(BIN, "tokens-since.py"), self.scripts)
        # Whole seconds: every boundary below makes a round trip through an
        # integer epoch in usage-live.json, so sub-second precision would only
        # show up as spurious assertion noise.
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.cache_path = os.path.join(self.scripts, "usage-live.json")
        self.cal_path = os.path.join(self.scripts, "usage-fable-calibration.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write_cache(self, **fields):
        with open(self.cache_path, "w") as f:
            json.dump(fields, f)

    def read_cache(self):
        with open(self.cache_path) as f:
            return json.load(f)

    def read_cal(self):
        with open(self.cal_path) as f:
            return json.load(f)

    def write_transcript(self, name, when, model="claude-fable-5",
                         input_tokens=1_000_000, output_tokens=0):
        """One transcript entry at `when`. claude-fable-5 is priced at $10/1M
        input (see bin/tokens-since.py), so 1,000,000 input tokens is exactly
        $10.00 of cost-weighted usage -- a round number the assertions below
        can be written against by hand."""
        proj = os.path.join(self.home, ".claude", "projects", name)
        os.makedirs(proj, exist_ok=True)
        entry = {
            "timestamp": when.isoformat(),
            "message": {
                "model": model,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        }
        with open(os.path.join(proj, "session.jsonl"), "w") as f:
            f.write(json.dumps(entry) + "\n")


class CacheWindowRegressionGuardTest(IsolatedHome):
    """`regresses()` in bin/usage-statusline.py: a render whose payload
    describes a strictly older window must not overwrite the shared cache."""

    def payload(self, five_hour_resets, seven_day_resets, five_pct, seven_pct):
        return {
            "model": {"id": "fable-5", "display_name": "Fable 5"},
            "rate_limits": {
                "five_hour": {"used_percentage": five_pct,
                              "resets_at": int(five_hour_resets.timestamp())},
                "seven_day": {"used_percentage": seven_pct,
                              "resets_at": int(seven_day_resets.timestamp())},
            },
        }

    def test_idle_sessions_older_window_does_not_overwrite_fresh_numbers(self):
        current = self.now + timedelta(days=6)
        stale = current - WEEK
        self.write_cache(
            five_hour_pct=10, five_hour_resets_at=int((self.now + timedelta(hours=2)).timestamp()),
            seven_day_pct=57, seven_day_resets_at=int(current.timestamp()),
        )
        r = run_script(STATUSLINE, self.home,
                       payload=self.payload(self.now - timedelta(days=3), stale, 38, 16))
        self.assertEqual(r.returncode, 0, r.stderr)

        cache = self.read_cache()
        self.assertEqual(cache["seven_day_pct"], 57)
        self.assertEqual(cache["seven_day_resets_at"], int(current.timestamp()))
        self.assertEqual(cache["five_hour_pct"], 10)

    def test_newer_window_render_does_update_cache(self):
        current = self.now + timedelta(days=6)
        self.write_cache(seven_day_pct=16, seven_day_resets_at=int((current - WEEK).timestamp()))
        r = run_script(STATUSLINE, self.home,
                       payload=self.payload(self.now + timedelta(hours=2), current, 10, 57))
        self.assertEqual(r.returncode, 0, r.stderr)

        cache = self.read_cache()
        self.assertEqual(cache["seven_day_pct"], 57)
        self.assertEqual(cache["seven_day_resets_at"], int(current.timestamp()))

    def test_same_window_is_still_newest_wins_even_downward(self):
        """Deliberately not a high-water mark: a mid-window limit boost can
        legitimately move a used_percentage down, so an equal window always
        takes the newest reading rather than the largest one."""
        current = self.now + timedelta(days=6)
        self.write_cache(seven_day_pct=57, seven_day_resets_at=int(current.timestamp()))
        r = run_script(STATUSLINE, self.home,
                       payload=self.payload(self.now + timedelta(hours=2), current, 10, 38))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_cache()["seven_day_pct"], 38)

    def test_render_with_no_cache_at_all_still_writes(self):
        current = self.now + timedelta(days=6)
        r = run_script(STATUSLINE, self.home,
                       payload=self.payload(self.now + timedelta(hours=2), current, 10, 57))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_cache()["seven_day_resets_at"], int(current.timestamp()))


class CalibratorWindowRollForwardTest(IsolatedHome):
    """bin/usage-calibrate-fable.py must never anchor a calibration to a
    boundary that has already passed."""

    def test_past_cached_boundary_rolls_forward_to_the_upcoming_one(self):
        past_boundary = self.now - timedelta(hours=2)
        self.write_cache(seven_day_pct=57, seven_day_resets_at=int(past_boundary.timestamp()))

        r = run_script(CALIBRATE, self.home, args=["50"])
        self.assertEqual(r.returncode, 0, r.stderr)

        cal = self.read_cal()
        next_reset = datetime.fromisoformat(cal["next_reset"])
        window_start = datetime.fromisoformat(cal["window_start"])
        calibrated_at = datetime.fromisoformat(cal["calibrated_at"])

        self.assertGreater(next_reset, self.now, "anchored to a boundary already in the past")
        # The exact shape of the live bug: a calibration stamped with a reset
        # that had already happened before it was written.
        self.assertGreater(next_reset, calibrated_at)
        self.assertEqual(next_reset, past_boundary + WEEK)
        self.assertEqual(window_start, next_reset - WEEK)

    def test_cap_is_derived_from_the_current_window_only(self):
        """The bug's actual damage: with window_start a week early,
        tokens_at_cal picked up the *previous* window's usage too and the cap
        came out inflated by that ratio -- which is what turned a real 81%
        into a displayed 18%."""
        past_boundary = self.now - timedelta(hours=2)
        self.write_cache(seven_day_pct=57, seven_day_resets_at=int(past_boundary.timestamp()))
        # $10 inside the current window, $10 in the window before it.
        self.write_transcript("current", self.now - timedelta(hours=1))
        self.write_transcript("previous", self.now - timedelta(days=5))

        r = run_script(CALIBRATE, self.home, args=["50"])
        self.assertEqual(r.returncode, 0, r.stderr)

        cal = self.read_cal()
        # Only the in-window $10 counts -> cap = 10.00 / 0.50 = 20.00.
        # Anchored a week early both entries would count: cap 40.00, and every
        # later read would report half the true percentage.
        self.assertAlmostEqual(cal["tokens_at_cal"], 10.0, places=6)
        self.assertAlmostEqual(cal["cap"], 20.0, places=6)


class FableEstimateWindowTest(IsolatedHome):
    """fable_estimate() in bin/usage_common.py, driven through the real
    statusline exactly as Claude Code invokes it."""

    def write_cal(self, window_start, next_reset, calibrated_at, cap=100,
                  seven_day_pct_at_cal=57):
        with open(self.cal_path, "w") as f:
            json.dump({
                "calibrated_at": calibrated_at.isoformat(),
                "tracked_model": "fable",
                "pct": 50,
                "window_start": window_start.isoformat(),
                "next_reset": next_reset.isoformat(),
                "tokens_at_cal": 0,
                "seven_day_pct_at_cal": seven_day_pct_at_cal,
                "local_total_at_cal": 0,
                "cap": cap,
                "cap_derived_at": calibrated_at.isoformat(),
            }, f)

    def payload(self, seven_day_resets, seven_pct=57):
        return {
            "model": {"id": "fable-5", "display_name": "Fable 5"},
            "rate_limits": {
                "five_hour": {"used_percentage": 10,
                              "resets_at": int((self.now + timedelta(hours=2)).timestamp())},
                "seven_day": {"used_percentage": seven_pct,
                              "resets_at": int(seven_day_resets.timestamp())},
            },
        }

    def tracked(self, payload):
        r = run_script(STATUSLINE, self.home, args=["--json"], payload=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())["tracked_model"]

    def test_past_boundary_is_rolled_forward_not_taken_verbatim(self):
        past_boundary = self.now - timedelta(hours=2)
        self.write_cal(window_start=past_boundary, next_reset=past_boundary + WEEK,
                       calibrated_at=self.now - timedelta(hours=1))
        # $10 this window, $10 the window before -- only the first may count.
        self.write_transcript("current", self.now - timedelta(hours=1))
        self.write_transcript("previous", self.now - timedelta(days=5))

        tracked = self.tracked(self.payload(past_boundary))
        self.assertFalse(tracked["stale"], "should project cleanly, not report stale")
        self.assertGreater(tracked["resets_at"], self.now.timestamp(),
                           "reported a reset boundary that has already passed")
        self.assertEqual(tracked["resets_at"], int((past_boundary + WEEK).timestamp()))
        # cap 100 against $10 of in-window usage. A week-early window_start
        # would sum both entries and report 20%.
        self.assertAlmostEqual(tracked["pct"], 10.0, places=6)

    def test_calibration_stamped_with_an_already_passed_reset_reports_stale(self):
        """The exact corrupt file found on disk 2026-09-05: next_reset
        (09-05T09:00Z) sitting *before* calibrated_at (09-05T23:07Z). Its cap
        was derived over the wrong seven days, so the honest answer is
        `stale`, not a confidently wrong percentage."""
        past_boundary = self.now - timedelta(hours=14)
        self.write_cal(window_start=past_boundary - WEEK, next_reset=past_boundary,
                       calibrated_at=self.now - timedelta(hours=1))
        self.write_transcript("current", self.now - timedelta(minutes=30))

        tracked = self.tracked(self.payload(self.now + timedelta(days=6)))
        self.assertTrue(tracked["stale"])

    def test_healthy_calibration_is_untouched(self):
        """The fix must be a no-op on the normal case: a current calibration
        against a live, future boundary still projects exactly as before."""
        next_reset = self.now + timedelta(days=6)
        self.write_cal(window_start=next_reset - WEEK, next_reset=next_reset,
                       calibrated_at=self.now - timedelta(hours=1))
        self.write_transcript("current", self.now - timedelta(hours=1))

        tracked = self.tracked(self.payload(next_reset))
        self.assertFalse(tracked["stale"])
        self.assertEqual(tracked["resets_at"], int(next_reset.timestamp()))
        self.assertAlmostEqual(tracked["pct"], 10.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
