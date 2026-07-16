"""Offline structural checks against docs/index.html: the cache-bypass
dashboard_data.json fetch, and that dashboard-logic.js is actually wired
up before the main inline script runs. Plain-text checks (no HTML parser
dependency), consistent with tests/test_workflow.py's approach."""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML_PATH = os.path.join(REPO_ROOT, "docs", "index.html")


def _read():
    with open(INDEX_HTML_PATH) as f:
        return f.read()


class CacheBypassFetchTests(unittest.TestCase):
    def setUp(self):
        self.html = _read()

    def test_fetch_call_bypasses_cache(self):
        # Must include a cache-busting query string AND cache: 'no-store' -
        # either alone can still be served stale by an intermediate cache
        # or the browser's disk cache in some configurations. Grab a fixed
        # window of source after "fetch(" rather than trying to regex-match
        # balanced parens (the call itself contains nested parens, e.g.
        # Date.now()).
        fetch_pos = self.html.index("fetch(")
        call = self.html[fetch_pos:fetch_pos + 150]
        self.assertIn("dashboard_data.json", call)
        self.assertIn("Date.now()", call, "fetch must include a cache-busting query param")
        self.assertIn("no-store", call, "fetch must set cache: 'no-store'")

    def test_stale_plain_fetch_is_not_present(self):
        # The exact old, cacheable call must be gone, not just supplemented.
        self.assertNotIn("fetch('./dashboard_data.json')", self.html)
        self.assertNotIn('fetch("./dashboard_data.json")', self.html)

    def test_fetch_failure_still_falls_back(self):
        # The try/catch around the fetch, and the __FALLBACK__ assignment,
        # must both still be present - a cache-busting change must not have
        # dropped the offline/broken-CDN graceful-degradation path.
        self.assertIn("__FALLBACK__", self.html)
        fetch_pos = self.html.index("dashboard_data.json?v=")
        try_pos = self.html.rindex("try{", 0, fetch_pos)
        catch_pos = self.html.index("catch(e)", fetch_pos)
        self.assertLess(try_pos, fetch_pos)
        self.assertLess(fetch_pos, catch_pos)


class DashboardLogicScriptWiringTests(unittest.TestCase):
    def setUp(self):
        self.html = _read()

    def test_dashboard_logic_script_tag_present(self):
        self.assertIn('<script src="./dashboard-logic.js"></script>', self.html)

    def test_dashboard_logic_loads_before_main_inline_script(self):
        logic_pos = self.html.index('<script src="./dashboard-logic.js"></script>')
        main_script_pos = self.html.index("const FEATURES = {")
        self.assertLess(logic_pos, main_script_pos)

    def test_no_duplicate_getforecastbyday_defined_inline(self):
        # getForecastByDay must come from dashboard-logic.js only - a
        # locally re-declared copy would silently shadow it and drift.
        self.assertNotIn("function getForecastByDay(d)", self.html)


class TodayTomorrowMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = _read()

    def test_today_and_tomorrow_grids_both_present(self):
        self.assertIn('id="todayGrid"', self.html)
        self.assertIn('id="tomorrowGrid"', self.html)

    def test_today_and_tomorrow_summaries_both_present(self):
        self.assertIn('id="todaySummaryGrid"', self.html)
        self.assertIn('id="tomorrowSummaryGrid"', self.html)
        self.assertIn('id="todayRecommendation"', self.html)
        self.assertIn('id="tomorrowRecommendation"', self.html)


if __name__ == "__main__":
    unittest.main()
