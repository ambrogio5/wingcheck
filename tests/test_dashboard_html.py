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


class RetroDashboardSkinTests(unittest.TestCase):
    def setUp(self):
        self.html = _read()
        css_path = os.path.join(REPO_ROOT, "docs", "retro-dashboard.css")
        with open(css_path) as f:
            self.css = f.read()

    def test_three_skins_selectable_with_classic_default(self):
        # All three skin buttons are present and independently selectable.
        self.assertIn('id="skinClassic"', self.html)
        self.assertIn('id="skinTech"', self.html)
        self.assertIn('id="skinRetro"', self.html)
        # CLASSIC is the default when nothing is stored (and for any
        # unrecognized value); only tech/retro set a data-skin attribute.
        self.assertIn("(stored === 'tech' || stored === 'retro') ? stored : 'classic'", self.html)
        self.assertNotIn("stored === 'tech' ? 'retro'", self.html)  # no forced migration
        self.assertIn('data-skin="retro"', self.css)

    def test_approved_semantic_tokens_are_centralized(self):
        for token in (
            "--surface-0:#050705", "--surface-1:#090c09",
            "--amber:#ff9e18", "--gold:#d4af61",
            "--phosphor:#9edb82", "--signal-red:#f04428",
        ):
            self.assertIn(token, self.css)

    def test_real_dashboard_sections_are_mapped_to_panel_grid(self):
        for panel_class in (
            "panel-forecast", "panel-summary", "panel-water", "panel-live",
            "panel-health", "panel-reference", "panel-viz", "panel-eval",
            "panel-ablation", "panel-tech",
        ):
            self.assertIn(panel_class, self.html)
        self.assertIn('id="dashboardMain"', self.html)

    def test_prototype_art_and_fake_metro_telemetry_are_not_shipped(self):
        self.assertNotIn("design-reference.png", self.html)
        for placeholder in ("Civic Center", "Central Line", "Motor temp", "Brake press"):
            self.assertNotIn(placeholder, self.html)

    def test_accessibility_and_reduced_motion_hooks_exist(self):
        self.assertIn('class="skip-link"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn("prefers-reduced-motion:reduce", self.css)
        self.assertIn(":focus-visible", self.css)

    def test_all_charts_have_accessible_names(self):
        for chart_id in ("vizDonut", "vizPolar", "vizRadar", "monthlyChart", "timelineChart"):
            marker = f'id="{chart_id}"'
            start = self.html.index(marker)
            canvas = self.html[start:self.html.index(">", start)]
            self.assertIn('role="img"', canvas)
            self.assertIn('aria-label=', canvas)


class RetroInstrumentClusterTests(unittest.TestCase):
    """The retro skin's operator-console hero: a real-data instrument
    cluster (dial, animated area map, route, systems, next window, day
    profile). Every value is driven from dashboard_data.json - no
    fabricated metro telemetry, no design-reference artwork."""

    def setUp(self):
        self.html = _read()
        css_path = os.path.join(REPO_ROOT, "docs", "retro-dashboard.css")
        with open(css_path) as f:
            self.css = f.read()

    def test_cluster_markup_present(self):
        for marker in (
            'id="retroCluster"', 'class="rc-panel rc-route"', 'id="rcDial"',
            'id="rcMapSvg"', 'id="rcStations"', 'id="rcSysList"',
            'id="rcNextHour"', 'id="rcSpark"', 'id="rcTimeline"',
        ):
            self.assertIn(marker, self.html, marker)

    def test_cluster_is_hidden_off_retro_and_shown_on_retro(self):
        # base stylesheet hides it; the retro skin turns it back on.
        self.assertIn(".retro-cluster{display:none;}", self.html.replace(" ", ""))
        self.assertRegex(
            self.css.replace("\n", " "),
            r'html\[data-skin="retro"\]\s*\.retro-cluster\s*\{[^}]*display:grid',
        )

    def test_render_function_defined_and_wired(self):
        self.assertIn("function renderRetroCluster(d){", self.html)
        # runs on load in the init render loop...
        self.assertIn("renderStatus, renderRetroCluster,", self.html)
        # ...and again on skin switch so animations restart.
        self.assertIn("renderRetroCluster(window.__DASH_DATA)", self.html)

    def test_map_has_real_places_and_animated_flow(self):
        # Real geography of the Maloja wind - not the prototype metro line.
        for place in ("MALOJA PASS", "SILVAPLANA", "SAMEDAN", "CORVATSCH"):
            self.assertIn(place, self.html, place)
        # animated wind streamlines + the guide path the particles ride
        self.assertIn('class="rc-stream"', self.html)
        self.assertIn('id="rcFlowPath"', self.html)
        self.assertIn("@keyframes rcStream", self.css)
        # flow speed is a CSS variable the renderer scales to the forecast wind
        self.assertIn("animation:rcStream var(--flow", self.css)
        self.assertIn("--flow", self.html)  # renderer sets it

    def test_dial_and_readouts_come_from_real_fields(self):
        # gauge peak == session_forecast event_probability; wind/agreement/gust
        # all read from real optional fields, never invented.
        for token in ("event_probability", "expected_wind_max_kt",
                      "expected_gust_max_kt", "model_agreement",
                      "station_input_age_minutes"):
            self.assertIn(token, self.html, token)

    def test_no_fabricated_metro_telemetry_in_cluster(self):
        for placeholder in ("Civic Center", "Central Line", "Motor temp",
                            "Brake press", "design-reference.png"):
            self.assertNotIn(placeholder, self.html)


class LakeSampleButtonTests(unittest.TestCase):
    """Local one-tap collection and latest real lake observation UI."""

    def setUp(self):
        self.html = _read()

    def test_button_present_near_top_before_main(self):
        self.assertIn('id="lakeSampleBtn"', self.html)
        btn = self.html.index('id="lakeSampleBtn"')
        main = self.html.index('id="dashboardMain"')
        self.assertLess(btn, main, "sample button must sit above the console")

    def test_button_calls_local_collection_api(self):
        start = self.html.index('id="lakeSampleBtn"')
        button = self.html[self.html.rfind("<button", 0, start):self.html.index(">", start) + 1]
        self.assertIn('onclick="collectNow()"', button)
        self.assertIn("fetch('./api/collect'", self.html)

    def test_latest_wind_readout_is_present(self):
        for element_id in ("latestWindMean", "latestWindGust", "latestWindDir",
                           "latestWindTemp", "latestWindTime"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("/1.852, 'kt'", self.html)

    def test_dashboard_wind_displays_use_knots(self):
        self.assertNotIn("+' m/s'", self.html)
        self.assertNotIn("'km/h'", self.html)

    def test_button_has_no_embedded_credential(self):
        # never ship a token/PAT in the static page
        for bad in ("ghp_", "github_pat_", "Authorization", "token="):
            self.assertNotIn(bad, self.html)

    def test_forecast_refresh_button_calls_local_api(self):
        self.assertIn('id="forecastRefreshBtn"', self.html)
        self.assertIn('onclick="refreshForecastNow()"', self.html)
        self.assertIn("fetch('./api/refresh-forecast'", self.html)
        self.assertIn("fetch(`./api/forecast-status", self.html)

    def test_reference_station_readings_disclose_provisional_resolution(self):
        self.assertIn("Provisional live", self.html)
        self.assertIn("resolution_minutes", self.html)


if __name__ == "__main__":
    unittest.main()
