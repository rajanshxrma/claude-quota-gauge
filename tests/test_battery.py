"""Tests for 0.24.0: the battery cell of the workload segment
(bin/workload-gauge.py): reading `pmset -g batt`, the glyph's fill, the
colours, the motion, and the sampler easing while the Mac runs on battery.

Run with:  python3 -m unittest discover -s tests -v

Every power report here is written for the test; none is read from the
machine the tests run on.
"""
import importlib.util
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile("\x1b\\[[0-9;]*m")


def load():
    spec = importlib.util.spec_from_file_location(
        "workload_gauge", os.path.join(REPO_ROOT, "bin", "workload-gauge.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WG = load()


def report(source, detail):
    return f"Now drawing from '{source}'\n -InternalBattery-0 (id=1234567)\t{detail} present: true\n"


class ReadingThePowerReport(unittest.TestCase):
    def test_on_battery_with_an_estimate(self):
        b = WG.battery_status(report("Battery Power", "15%; discharging; 0:26 remaining"))
        self.assertEqual(b, {"pct": 15, "state": "discharging", "minutes": 26, "on_ac": False})

    def test_on_battery_without_an_estimate(self):
        b = WG.battery_status(report("Battery Power", "61%; discharging; (no estimate)"))
        self.assertEqual((b["pct"], b["state"], b["minutes"]), (61, "discharging", None))

    def test_charging(self):
        b = WG.battery_status(report("AC Power", "54%; charging; 1:05 remaining"))
        self.assertEqual((b["pct"], b["state"], b["minutes"], b["on_ac"]), (54, "charging", 65, True))

    def test_finishing_charge_counts_as_charging(self):
        b = WG.battery_status(report("AC Power", "97%; finishing charge; 0:10 remaining"))
        self.assertEqual((b["state"], b["minutes"]), ("charging", 10))

    def test_charged(self):
        b = WG.battery_status(report("AC Power", "100%; charged; 0:00 remaining"))
        self.assertEqual((b["pct"], b["state"], b["minutes"]), (100, "full", None))

    def test_plugged_in_and_held(self):
        b = WG.battery_status(report("AC Power", "80%; AC attached; not charging"))
        self.assertEqual((b["pct"], b["state"], b["minutes"]), (80, "held", None))

    def test_a_mac_without_a_battery(self):
        self.assertIsNone(WG.battery_status("Now drawing from 'AC Power'\n"))

    def test_an_empty_or_unreadable_report(self):
        self.assertIsNone(WG.battery_status(""))
        self.assertIsNone(WG.battery_status(" -InternalBattery-0 (id=1)\tno level here\n"))

    def test_a_state_not_seen_before_follows_the_power_source(self):
        self.assertEqual(WG.battery_status(report("AC Power", "70%; something new; 0:30 remaining"))["state"], "held")
        self.assertEqual(WG.battery_status(report("Battery Power", "70%; something new; 0:30 remaining"))["state"],
                         "discharging")


class TheGlyph(unittest.TestCase):
    def test_the_width_never_changes(self):
        for pct in range(0, 101):
            for rising in range(0, 8):
                self.assertEqual(len(WG.battery_cells(pct, rising=rising)), WG.BATTERY_CELLS, (pct, rising))
                self.assertEqual(len(WG.battery_cells(pct, rising=rising, ascii_only=True)), WG.BATTERY_CELLS)

    def test_empty_full_and_the_smallest_charge(self):
        self.assertEqual(WG.battery_cells(0), " " * WG.BATTERY_CELLS)
        self.assertEqual(WG.battery_cells(100), "█" * WG.BATTERY_CELLS)
        self.assertNotEqual(WG.battery_cells(1), " " * WG.BATTERY_CELLS)

    def test_the_fill_never_shrinks_as_the_level_rises(self):
        order = " " + WG._BATTERY_EIGHTHS + "█"

        def weight(cells):
            return sum(order.index(ch) for ch in cells)
        last = -1
        for pct in range(0, 101):
            now = weight(WG.battery_cells(pct))
            self.assertGreaterEqual(now, last, pct)
            last = now

    def test_a_full_battery_has_no_room_to_rise(self):
        self.assertEqual(WG.battery_cells(100, rising=5), "█" * WG.BATTERY_CELLS)


class TheCell(unittest.TestCase):
    def cell(self, pct, state, minutes=None, **kw):
        return WG.fmt_battery({"pct": pct, "state": state, "minutes": minutes, "on_ac": state != "discharging"}, **kw)

    def setUp(self):
        self.saved = os.environ.pop("NO_COLOR", None)

    def tearDown(self):
        os.environ.pop("NO_COLOR", None)
        if self.saved is not None:
            os.environ["NO_COLOR"] = self.saved

    def test_nothing_is_drawn_without_a_battery(self):
        self.assertEqual(WG.fmt_battery(None), "")

    def test_the_words_of_each_state(self):
        self.assertTrue(self.cell(15, "discharging", 26, now_ts=0, color=False).endswith("15% 0:26"))
        self.assertTrue(self.cell(54, "charging", 65, now_ts=0, color=False).endswith("⚡54% 1:05"))
        self.assertTrue(self.cell(100, "full", now_ts=0, color=False).endswith("100%"))
        self.assertTrue(self.cell(80, "held", now_ts=0, color=False).endswith("80%"))
        self.assertTrue(self.cell(8, "discharging", 9, now_ts=0, color=False).endswith("8% 0:09 ⚠"))

    def test_the_colour_follows_the_level_on_battery(self):
        self.assertIn("\x1b[32m", self.cell(80, "discharging", now_ts=2))
        self.assertIn("\x1b[33m", self.cell(35, "discharging", now_ts=2))
        self.assertIn("31m", self.cell(15, "discharging", now_ts=2))

    def test_low_on_battery_breathes_between_two_reds(self):
        frames = {self.cell(15, "discharging", 26, now_ts=t) for t in (0, 2, 4, 6)}
        self.assertEqual(len(frames), 2)
        self.assertEqual(len({ANSI.sub("", f) for f in frames}), 1)  # the words hold still

    def test_charging_moves_and_the_words_hold_still(self):
        frames = [self.cell(54, "charging", 65, now_ts=t, color=False) for t in range(0, 16, 2)]
        self.assertGreater(len(set(frames)), 1)
        # the words follow the glyph's right wall, its last "▏"
        self.assertEqual(len({f.rsplit("▏", 1)[1] for f in frames}), 1)
        self.assertEqual(len({len(f) for f in frames}), 1)

    def test_full_and_held_sit_still(self):
        for state in ("full", "held"):
            self.assertEqual(len({self.cell(90, state, now_ts=t) for t in range(0, 16, 2)}), 1)

    def test_nothing_moves_when_live_is_off(self):
        self.assertEqual(len({self.cell(54, "charging", 65, now_ts=t, live=False) for t in range(0, 16, 2)}), 1)
        self.assertEqual(len({self.cell(15, "discharging", 26, now_ts=t, live=False) for t in range(0, 16, 2)}), 1)

    def test_no_color_is_honoured(self):
        os.environ["NO_COLOR"] = "1"
        self.assertNotIn("\x1b", self.cell(15, "discharging", 26, now_ts=0))

    def test_ascii_only(self):
        out = self.cell(54, "charging", 65, now_ts=0, color=False, ascii_only=True)
        out.encode("ascii")
        self.assertEqual(out, "[##--]+54% 1:05")


class TheSamplerEasesOnBattery(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(WG.battery_interval(None), WG.WRITE_EVERY)
        self.assertEqual(WG.battery_interval({"pct": 50, "state": "charging"}), WG.WRITE_EVERY)
        self.assertEqual(WG.battery_interval({"pct": 100, "state": "full"}), WG.WRITE_EVERY)
        self.assertGreater(WG.battery_interval({"pct": 60, "state": "discharging"}), WG.WRITE_EVERY)
        self.assertGreater(WG.battery_interval({"pct": 15, "state": "discharging"}),
                           WG.battery_interval({"pct": 60, "state": "discharging"}))

    def test_the_segment_is_never_called_stale_by_the_eased_interval(self):
        slowest = WG.battery_interval({"pct": 5, "state": "discharging"})
        stale_after = WG.STALE_AFTER + max(0.0, slowest - WG.WRITE_EVERY) * 2
        self.assertGreater(stale_after, slowest + 2)  # a sample takes about a second


if __name__ == "__main__":
    unittest.main()
