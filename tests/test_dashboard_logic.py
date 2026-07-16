"""Tests for docs/dashboard-logic.js - the pure, dependency-free helpers
behind the "Today"/"Tomorrow" forecast cards. These execute the actual JS
file with Node (present on GitHub Actions ubuntu-latest runners by
default - no new CI dependency) rather than re-implementing the logic in
Python, so a regression in the real browser-facing code is what actually
gets caught. No network calls; Node only evaluates the local file plus a
short inline test script.

Skips cleanly (does not fail) if `node` isn't on PATH, since this repo's
core Python pipeline must stay runnable without Node - see CLAUDE.md."""

import json
import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_LOGIC_PATH = os.path.join(REPO_ROOT, "docs", "dashboard-logic.js")
NODE = shutil.which("node")


def run_node(js_expression):
    """Loads dashboard-logic.js as `m`, evaluates js_expression (must be a
    JSON-serializable value or a JS expression producing one), and returns
    the parsed Python value. Raises AssertionError with stderr on failure."""
    script = f"""
    const m = require({json.dumps(DASHBOARD_LOGIC_PATH)});
    const result = ({js_expression});
    process.stdout.write(JSON.stringify(result));
    """
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


@unittest.skipUnless(NODE, "node not available on PATH")
class ZurichDateHelperTests(unittest.TestCase):
    def test_today_in_cest_summer(self):
        # 2026-07-16T10:00Z = 12:00 CEST (UTC+2) - same calendar day.
        got = run_node("m.zurichTodayDateString(new Date('2026-07-16T10:00:00Z'))")
        self.assertEqual(got, "2026-07-16")

    def test_tomorrow_in_cest_summer(self):
        got = run_node("m.zurichTomorrowDateString(new Date('2026-07-16T10:00:00Z'))")
        self.assertEqual(got, "2026-07-17")

    def test_midnight_boundary_in_cet_winter_rolls_to_next_day(self):
        # 2026-01-15T23:30Z = 00:30 CET (UTC+1) the NEXT calendar day - if
        # this used UTC's date directly it would wrongly say 2026-01-15.
        got = run_node("m.zurichTodayDateString(new Date('2026-01-15T23:30:00Z'))")
        self.assertEqual(got, "2026-01-16")

    def test_midnight_boundary_in_cest_summer_rolls_to_next_day(self):
        # 2026-07-15T22:30Z = 00:30 CEST (UTC+2) the next day.
        got = run_node("m.zurichTodayDateString(new Date('2026-07-15T22:30:00Z'))")
        self.assertEqual(got, "2026-07-16")

    def test_viewer_timezone_does_not_matter(self):
        # zurichDateParts hardcodes timeZone: 'Europe/Zurich' - the test
        # process's own TZ env var must have zero effect on the result.
        script = f"""
        process.env.TZ = 'Pacific/Auckland';
        const m = require({json.dumps(DASHBOARD_LOGIC_PATH)});
        process.stdout.write(JSON.stringify(m.zurichTodayDateString(new Date('2026-07-16T10:00:00Z'))));
        """
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), "2026-07-16")


@unittest.skipUnless(NODE, "node not available on PATH")
class GroupForecastByWindowTests(unittest.TestCase):
    def test_groups_by_calendar_date_and_filters_hour_window(self):
        forecast = [
            {"target_time": "2026-07-16T12:00", "probability": 0.1},   # outside 14-18
            {"target_time": "2026-07-16T14:00", "probability": 0.5},
            {"target_time": "2026-07-16T19:00", "probability": 0.9},   # outside 14-18
            {"target_time": "2026-07-17T15:00", "probability": 0.6},
        ]
        got = run_node(f"m.groupForecastByWindow({json.dumps(forecast)}, 14, 18)")
        self.assertEqual(set(got.keys()), {"2026-07-16", "2026-07-17"})
        self.assertEqual(len(got["2026-07-16"]), 1)
        self.assertEqual(got["2026-07-16"][0]["target_time"], "2026-07-16T14:00")
        self.assertEqual(len(got["2026-07-17"]), 1)

    def test_sorted_chronologically_within_a_date(self):
        forecast = [
            {"target_time": "2026-07-16T17:00", "probability": 0.3},
            {"target_time": "2026-07-16T14:00", "probability": 0.5},
            {"target_time": "2026-07-16T16:00", "probability": 0.4},
        ]
        got = run_node(f"m.groupForecastByWindow({json.dumps(forecast)}, 14, 18)")
        hours = [row["target_time"][11:13] for row in got["2026-07-16"]]
        self.assertEqual(hours, ["14", "16", "17"])


@unittest.skipUnless(NODE, "node not available on PATH")
class GetForecastByDayTests(unittest.TestCase):
    """The core bug fix: today and tomorrow must both be derivable
    simultaneously, and today must never be inferred from "the earliest
    date with data"."""

    def test_today_and_tomorrow_both_present_with_hours_left(self):
        # "now" is 2026-07-16T10:00 local (08:00Z, CEST) - well before any
        # of today's or tomorrow's 14-18h cards.
        forecast = [
            {"target_time": "2026-07-16T14:00", "probability": 0.5},
            {"target_time": "2026-07-16T18:00", "probability": 0.6},
            {"target_time": "2026-07-17T14:00", "probability": 0.7},
            {"target_time": "2026-07-17T18:00", "probability": 0.8},
        ]
        got = run_node(
            f"m.getForecastByDay({json.dumps(forecast)}, new Date('2026-07-16T08:00:00Z'))"
        )
        self.assertEqual(got["todayStr"], "2026-07-16")
        self.assertEqual(got["tomorrowStr"], "2026-07-17")
        self.assertEqual(len(got["todayRemaining"]), 2)
        self.assertEqual(len(got["tomorrowCards"]), 2)

    def test_tomorrow_not_hidden_when_today_has_one_hour_left(self):
        """The exact bug being fixed: previously getTodayCards() picked the
        earliest date in the list as "today" and only ever showed that
        date - so if today still had a 18:00 forecast logged, tomorrow's
        cards were completely invisible no matter how many there were."""
        forecast = [
            {"target_time": "2026-07-16T18:00", "probability": 0.9},   # today, still upcoming
            {"target_time": "2026-07-17T14:00", "probability": 0.5},
            {"target_time": "2026-07-17T15:00", "probability": 0.6},
            {"target_time": "2026-07-17T16:00", "probability": 0.7},
            {"target_time": "2026-07-17T17:00", "probability": 0.8},
            {"target_time": "2026-07-17T18:00", "probability": 0.9},
        ]
        # "now" is 17:30 local on the 16th - the 18:00 slot hasn't started yet.
        got = run_node(
            f"m.getForecastByDay({json.dumps(forecast)}, new Date('2026-07-16T15:30:00Z'))"
        )
        self.assertEqual(len(got["todayRemaining"]), 1)
        self.assertEqual(got["todayRemaining"][0]["target_time"], "2026-07-16T18:00")
        self.assertEqual(len(got["tomorrowCards"]), 5)

    def test_today_remaining_excludes_hours_already_passed(self):
        forecast = [
            {"target_time": "2026-07-16T14:00", "probability": 0.5},
            {"target_time": "2026-07-16T15:00", "probability": 0.6},
            {"target_time": "2026-07-16T18:00", "probability": 0.9},
        ]
        # "now" is 16:30 CEST (14:30Z) - 14:00 and 15:00 have already passed.
        got = run_node(
            f"m.getForecastByDay({json.dumps(forecast)}, new Date('2026-07-16T14:30:00Z'))"
        )
        remaining_hours = sorted(row["target_time"][11:13] for row in got["todayRemaining"])
        self.assertEqual(remaining_hours, ["18"])
        # todayAllCards must still include the passed hours - callers use it
        # to distinguish "day complete" from "nothing was ever logged".
        self.assertEqual(len(got["todayAllCards"]), 3)

    def test_today_empty_but_tomorrow_populated(self):
        forecast = [
            {"target_time": "2026-07-17T14:00", "probability": 0.5},
            {"target_time": "2026-07-17T15:00", "probability": 0.6},
        ]
        got = run_node(
            f"m.getForecastByDay({json.dumps(forecast)}, new Date('2026-07-16T08:00:00Z'))"
        )
        self.assertEqual(got["todayAllCards"], [])
        self.assertEqual(got["todayRemaining"], [])
        self.assertEqual(len(got["tomorrowCards"]), 2)

    def test_no_tomorrow_forecast(self):
        forecast = [
            {"target_time": "2026-07-16T14:00", "probability": 0.5},
        ]
        got = run_node(
            f"m.getForecastByDay({json.dumps(forecast)}, new Date('2026-07-16T08:00:00Z'))"
        )
        self.assertEqual(len(got["todayRemaining"]), 1)
        self.assertEqual(got["tomorrowCards"], [])

    def test_empty_forecast_list(self):
        got = run_node("m.getForecastByDay([], new Date('2026-07-16T08:00:00Z'))")
        self.assertEqual(got["todayAllCards"], [])
        self.assertEqual(got["todayRemaining"], [])
        self.assertEqual(got["tomorrowCards"], [])


@unittest.skipUnless(NODE, "node not available on PATH")
class SelectBestCardTests(unittest.TestCase):
    def test_picks_highest_probability(self):
        cards = [
            {"target_time": "2026-07-16T14:00", "probability": 0.5},
            {"target_time": "2026-07-16T15:00", "probability": 0.9},
            {"target_time": "2026-07-16T16:00", "probability": 0.3},
        ]
        got = run_node(f"m.selectBestCard({json.dumps(cards)})")
        self.assertEqual(got["target_time"], "2026-07-16T15:00")

    def test_empty_list_is_null(self):
        got = run_node("m.selectBestCard([])")
        self.assertIsNone(got)


class DashboardLogicSyntaxTests(unittest.TestCase):
    def test_file_is_valid_javascript(self):
        if NODE is None:
            self.skipTest("node not available on PATH")
        proc = subprocess.run([NODE, "--check", DASHBOARD_LOGIC_PATH], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
