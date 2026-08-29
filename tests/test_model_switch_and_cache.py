"""Tests for the v0.20.0 additions: the PreModelSwitch/PostModelSwitch hook
(bin/model-switch-hook.py), the `prompt_cache` and `rate_limits.spend_limit`
segments in bin/usage-statusline.py, and the resume re-cache note in
bin/usage-session-hook.py.

Run with:  python3 -m unittest discover -s tests -v

Same isolation rule as the rest of the suite: every script runs as a
subprocess against a throwaway HOME (and USERPROFILE, for Windows), with
every CLAUDE_USAGE_* var stripped, so nothing here can read or write the
real ~/.claude/scripts state.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO_ROOT, "bin")
STATUSLINE = os.path.join(BIN, "usage-statusline.py")
SWITCH_HOOK = os.path.join(BIN, "model-switch-hook.py")
SESSION_HOOK = os.path.join(BIN, "usage-session-hook.py")


def run_script(script, payload, home, args=None, extra_env=None):
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
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", env=env, cwd=home, timeout=15,
    )


class IsolatedHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.scripts = os.path.join(self.home, ".claude", "scripts")
        os.makedirs(self.scripts, exist_ok=True)
        os.makedirs(os.path.join(self.home, ".claude", "projects"), exist_ok=True)
        self.now = datetime.now(timezone.utc)
        self.resets_at = int((self.now + timedelta(hours=5)).timestamp())

    def tearDown(self):
        self._tmp.cleanup()

    def write_cache(self, **extra):
        cache = {
            "five_hour_pct": 50, "five_hour_resets_at": self.resets_at,
            "seven_day_pct": 18, "seven_day_resets_at": self.resets_at,
        }
        cache.update(extra)
        with open(os.path.join(self.scripts, "usage-live.json"), "w") as f:
            json.dump(cache, f)

    def payload(self, **extra):
        p = {
            "model": {"id": "fable-5", "display_name": "Fable 5"},
            "rate_limits": {
                "five_hour": {"used_percentage": 12, "resets_at": self.resets_at},
                "seven_day": {"used_percentage": 34, "resets_at": self.resets_at},
            },
            "version": "2.1.251",
        }
        p.update(extra)
        return p


class PromptCacheSegmentTest(IsolatedHome):
    def test_warm_cache_renders_ratio_and_time_left(self):
        pc = {"warm": True, "caching_observed": True, "ttl": "1h",
              "expires_at": int((self.now + timedelta(minutes=42)).timestamp()),
              "hit_ratio": 0.91, "requests": 14, "misses": 2,
              "recache_tokens_if_cold": 45000}
        r = run_script(STATUSLINE, self.payload(prompt_cache=pc), self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"cache: warm 91% \(\d+h \d+m left\)")
        data = json.loads(run_script(STATUSLINE, self.payload(prompt_cache=pc), self.home, ["--json"]).stdout)
        self.assertTrue(data["prompt_cache"]["warm"])
        self.assertEqual(data["prompt_cache"]["hit_ratio"], 0.91)
        self.assertEqual(data["prompt_cache"]["recache_tokens_if_cold"], 45000)

    def test_cold_cache_names_rewarm_cost(self):
        pc = {"warm": False, "caching_observed": True, "ttl": "5m", "expires_at": None,
              "hit_ratio": 0.5, "recache_tokens_if_cold": 45000}
        r = run_script(STATUSLINE, self.payload(prompt_cache=pc), self.home)
        self.assertIn("cache: cold (~45k to rewarm)", r.stdout)

    def test_no_caching_observed_is_silent(self):
        pc = {"warm": False, "caching_observed": False}
        r = run_script(STATUSLINE, self.payload(prompt_cache=pc), self.home)
        self.assertNotIn("cache:", r.stdout)
        data = json.loads(run_script(STATUSLINE, self.payload(prompt_cache=pc), self.home, ["--json"]).stdout)
        self.assertIsNone(data["prompt_cache"])

    def test_absent_field_is_silent_and_never_cached(self):
        r = run_script(STATUSLINE, self.payload(), self.home)
        self.assertNotIn("cache:", r.stdout)
        with open(os.path.join(self.scripts, "usage-live.json")) as f:
            cache = json.load(f)
        self.assertFalse(any(k.startswith("prompt_cache") for k in cache))


class SpendLimitSegmentTest(IsolatedHome):
    def test_spend_limit_renders_and_caches(self):
        p = self.payload()
        p["rate_limits"]["spend_limit"] = {"used_percentage": 62.8, "resets_at": self.resets_at}
        r = run_script(STATUSLINE, p, self.home)
        self.assertRegex(r.stdout, r"spend: 63% \(resets \d+h \d+m\)")
        with open(os.path.join(self.scripts, "usage-live.json")) as f:
            cache = json.load(f)
        self.assertEqual(cache["spend_limit_pct"], 62.8)
        data = json.loads(run_script(STATUSLINE, p, self.home, ["--json"]).stdout)
        self.assertEqual(data["spend_limit"]["pct"], 62.8)

    def test_over_limit_percentage_shown_as_is(self):
        p = self.payload()
        p["rate_limits"]["spend_limit"] = {"used_percentage": 112, "resets_at": self.resets_at}
        r = run_script(STATUSLINE, p, self.home)
        self.assertIn("spend: 112%", r.stdout)

    def test_absent_window_clears_stale_cache(self):
        self.write_cache(spend_limit_pct=40, spend_limit_resets_at=self.resets_at)
        r = run_script(STATUSLINE, self.payload(), self.home)
        self.assertNotIn("spend:", r.stdout)
        with open(os.path.join(self.scripts, "usage-live.json")) as f:
            cache = json.load(f)
        self.assertNotIn("spend_limit_pct", cache)

    def test_cached_fallback_when_rate_limits_missing(self):
        self.write_cache(spend_limit_pct=40, spend_limit_resets_at=self.resets_at)
        p = self.payload()
        del p["rate_limits"]
        r = run_script(STATUSLINE, p, self.home)
        self.assertIn("spend: 40% (refreshing…)", r.stdout)


class ModelSwitchHookTest(IsolatedHome):
    def switch(self, event, to_model="claude-fable-5", from_model="claude-opus-5", extra_env=None):
        payload = {"hook_event_name": event, "session_id": "sess-1",
                   "from_model": from_model, "to_model": to_model, "requested_by": "user"}
        return run_script(SWITCH_HOOK, payload, self.home, extra_env=extra_env)

    def test_pre_switch_annotates_with_live_usage(self):
        self.write_cache(fable_pct=24, fable_resets_at=self.resets_at, fable_tracked_model="fable")
        r = self.switch("PreModelSwitch")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn("5h: 50%", out["systemMessage"])
        self.assertIn("fable weekly: 24%", out["systemMessage"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_pre_switch_off_tracked_model_omits_its_estimate(self):
        self.write_cache(fable_pct=24, fable_resets_at=self.resets_at)
        r = self.switch("PreModelSwitch", to_model="claude-sonnet-5", from_model="claude-fable-5")
        out = json.loads(r.stdout)
        self.assertIn("week: 18%", out["systemMessage"])
        self.assertNotIn("fable weekly", out["systemMessage"])

    def test_pre_switch_blocks_only_when_threshold_set_and_reached(self):
        self.write_cache(fable_pct=96, fable_resets_at=self.resets_at)
        allowed = json.loads(self.switch("PreModelSwitch").stdout)
        self.assertNotIn("permissionDecision", allowed["hookSpecificOutput"])
        denied = json.loads(self.switch("PreModelSwitch", extra_env={"CLAUDE_USAGE_SWITCH_BLOCK_PCT": "95"}).stdout)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("96%", denied["hookSpecificOutput"]["permissionDecisionReason"])
        under = json.loads(self.switch("PreModelSwitch", extra_env={"CLAUDE_USAGE_SWITCH_BLOCK_PCT": "97"}).stdout)
        self.assertNotIn("permissionDecision", under["hookSpecificOutput"])

    def test_pre_switch_with_no_cache_is_silent_and_allows(self):
        r = self.switch("PreModelSwitch")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertNotIn("systemMessage", out)
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_post_switch_onto_tracked_marks_used_and_session(self):
        self.write_cache()
        r = self.switch("PostModelSwitch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        with open(os.path.join(self.scripts, "fable-force-recal.json")) as f:
            self.assertEqual(json.load(f)["context"], "model-switch")
        with open(os.path.join(self.scripts, "fable-session-usage.json")) as f:
            self.assertIn("sess-1", json.load(f))

    def test_post_switch_off_tracked_marks_used_but_not_session(self):
        self.write_cache()
        self.switch("PostModelSwitch", to_model="claude-sonnet-5", from_model="claude-fable-5")
        self.assertTrue(os.path.exists(os.path.join(self.scripts, "fable-force-recal.json")))
        self.assertFalse(os.path.exists(os.path.join(self.scripts, "fable-session-usage.json")))

    def test_post_switch_unrelated_models_touch_nothing(self):
        self.write_cache()
        self.switch("PostModelSwitch", to_model="claude-sonnet-5", from_model="claude-opus-5")
        self.assertFalse(os.path.exists(os.path.join(self.scripts, "fable-force-recal.json")))

    def test_garbage_stdin_exits_clean(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_USAGE")}
        env.update({"HOME": self.home, "USERPROFILE": self.home})
        r = subprocess.run([sys.executable, SWITCH_HOOK], input="not json", capture_output=True,
                           text=True, env=env, cwd=self.home, timeout=15)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class SessionHookResumeNoteTest(IsolatedHome):
    def context(self, payload):
        r = run_script(SESSION_HOOK, payload, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_resume_with_cold_cache_is_named(self):
        self.write_cache()
        ctx = self.context({"hook_event_name": "SessionStart", "source": "resume",
                            "cache_invalidation_reason": "model_switch",
                            "re_cache_cost_tokens": 125000, "re_cache_cost_usd": 0.31})
        self.assertIn("resumed with a cold prompt cache (model_switch)", ctx)
        self.assertIn("~125k tokens", ctx)
        self.assertIn("$0.31", ctx)

    def test_resume_with_intact_cache_says_nothing(self):
        self.write_cache()
        ctx = self.context({"hook_event_name": "SessionStart", "source": "resume",
                            "cache_invalidation_reason": "none", "re_cache_cost_tokens": 0})
        self.assertNotIn("resumed", ctx)

    def test_fresh_start_unchanged_and_spend_shown_when_cached(self):
        self.write_cache(spend_limit_pct=40, spend_limit_resets_at=self.resets_at)
        ctx = self.context({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertNotIn("resumed", ctx)
        self.assertIn("spend: 40%", ctx)


if __name__ == "__main__":
    unittest.main()
