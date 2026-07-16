"""Offline tests for .github/workflows/wingcheck.yml's pull_request safety:
a PR-triggered run must only ever be able to run the lightweight `validate`
job (syntax check + offline tests), never forecast/learn/backtest/scrape/
commit. Deliberately does NOT depend on PyYAML (not a project dependency,
see requirements.txt) - these are plain-text structural checks against the
workflow file, which is a perfectly adequate way to catch an `if:` regression
without adding a dependency just for this test."""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "wingcheck.yml")
COPY_ME_PATH = os.path.join(REPO_ROOT, "COPY-ME_workflow.yml")


def _read(path):
    with open(path) as f:
        return f.read()


def _job_block(text, job_name):
    """Returns the raw text of one top-level job block (from its `  name:`
    line up to the next top-level job or EOF), for simple substring/regex
    checks against just that job."""
    pattern = re.compile(rf"^  {re.escape(job_name)}:\n(.*?)(?=^  \w+:\n|\Z)", re.MULTILINE | re.DOTALL)
    m = pattern.search(text)
    assert m, f"job {job_name!r} not found in workflow"
    return m.group(1)


def _git_add_block(job_text):
    """Returns the full `git add ...` command from a job's commit step,
    joining any `\\`-continued lines - some commit steps stage several
    paths across multiple lines."""
    lines = job_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("git add"))
    block_lines = [lines[start]]
    i = start
    while block_lines[-1].rstrip().endswith("\\"):
        i += 1
        block_lines.append(lines[i])
    return "\n".join(block_lines)


class PullRequestTriggerTests(unittest.TestCase):
    def setUp(self):
        self.text = _read(WORKFLOW_PATH)

    def test_pull_request_trigger_present(self):
        # `pull_request:` must appear inside the top-level `on:` block.
        on_block = re.search(r"^on:\n(.*?)^jobs:", self.text, re.MULTILINE | re.DOTALL).group(1)
        self.assertIn("pull_request:", on_block)

    def test_validate_job_exists_and_runs_only_on_pull_request(self):
        job = _job_block(self.text, "validate")
        self.assertIn("github.event_name == 'pull_request'", job)

    def test_validate_job_only_compiles_and_tests(self):
        job = _job_block(self.text, "validate")
        self.assertIn("py_compile", job)
        self.assertIn("unittest discover", job)
        # None of the operational scripts or a commit/push step may appear.
        for forbidden in (
            "forecast_and_log.py", "verify_and_learn.py", "kitesailing_weather.py",
            "backtest.py", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "git commit", "git push",
        ):
            self.assertNotIn(forbidden, job, f"validate job must not reference {forbidden!r}")

    def test_operational_jobs_cannot_be_satisfied_by_a_pull_request_event(self):
        """Each operational job's `if:` must require github.event_name to be
        'schedule' or 'workflow_dispatch' - a pull_request event can never
        make any branch of these conditions true."""
        for job_name in ("backtest", "forecast", "sample_kitesailing", "learn",
                         "sync_historical_data", "station_research"):
            job = _job_block(self.text, job_name)
            if_line = re.search(r"if:.*?(?=\n  \w|\n    \w+:|\Z)", job, re.DOTALL)
            self.assertIsNotNone(if_line, f"{job_name} has no `if:` condition")
            condition = if_line.group(0)
            self.assertNotIn("pull_request", condition,
                              f"{job_name}'s if: condition must not reference pull_request")
            # Every event_name comparison in the condition must be to
            # 'schedule' or 'workflow_dispatch', never anything else.
            events_checked = re.findall(r"github\.event_name == '([^']+)'", condition)
            self.assertTrue(events_checked, f"{job_name} doesn't gate on event_name at all")
            self.assertTrue(all(e in ("schedule", "workflow_dispatch") for e in events_checked),
                             f"{job_name} gates on unexpected event(s): {events_checked}")

    def test_backtest_manual_path_runs_tests_then_backtest_then_commits(self):
        job = _job_block(self.text, "backtest")
        self.assertIn("unittest discover -s tests", job)
        self.assertIn("python backtest.py", job)
        self.assertIn("git commit", job)
        self.assertIn("git push", job)
        # Tests must run BEFORE the backtest script and the commit.
        test_pos = job.index("unittest discover -s tests")
        backtest_pos = job.index("python backtest.py")
        commit_pos = job.index("git commit")
        self.assertLess(test_pos, backtest_pos)
        self.assertLess(backtest_pos, commit_pos)


class ForecastJobRefreshesDashboardTests(unittest.TestCase):
    """The 2026-07-16 dashboard-visibility fix: the forecast job (07:00 and
    10:00 CEST) must refresh docs/dashboard_data.json immediately, instead
    of leaving today's/tomorrow's forecast invisible until the evening
    learn job runs."""

    def setUp(self):
        self.job = _job_block(_read(WORKFLOW_PATH), "forecast")

    def test_forecast_job_runs_refresh_dashboard(self):
        self.assertIn("python refresh_dashboard.py", self.job)

    def test_refresh_dashboard_runs_after_forecast_and_log(self):
        forecast_pos = self.job.index("python forecast_and_log.py")
        refresh_pos = self.job.index("python refresh_dashboard.py")
        self.assertLess(forecast_pos, refresh_pos)

    def test_forecast_commit_includes_dashboard_data(self):
        commit_section = self.job[self.job.index("git add"):]
        git_add_line = commit_section.splitlines()[0]
        self.assertIn("docs/dashboard_data.json", git_add_line)
        self.assertIn("logs/predictions.jsonl", git_add_line)

    def test_refresh_runs_before_the_commit(self):
        refresh_pos = self.job.index("python refresh_dashboard.py")
        commit_pos = self.job.index("git add")
        self.assertLess(refresh_pos, commit_pos)


class CopyMeWorkflowSyncTests(unittest.TestCase):
    def test_copy_me_workflow_matches_real_workflow(self):
        self.assertEqual(_read(WORKFLOW_PATH), _read(COPY_ME_PATH))


class ConcurrencyGroupTests(unittest.TestCase):
    def test_concurrency_group_is_scoped_per_branch_and_never_cancels(self):
        text = _read(WORKFLOW_PATH)
        block = re.search(r"^concurrency:\n(.*?)^jobs:", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertIn("github.ref", block)
        self.assertIn("cancel-in-progress: false", block)


class ForecastJobArchivesStationDataTests(unittest.TestCase):
    """Section 12: forecast jobs must sync station data before running the
    forecast (so forecast_and_log.py's diagnostics have fresh input) and
    commit the append-only forecast-vintage archive + issuance log it
    writes internally."""

    def setUp(self):
        self.job = _job_block(_read(WORKFLOW_PATH), "forecast")

    def test_syncs_historical_data_before_forecast(self):
        sync_pos = self.job.index("historical_data.py sync")
        forecast_pos = self.job.index("python forecast_and_log.py")
        self.assertLess(sync_pos, forecast_pos)

    def test_commits_forecast_vintages_and_issuance_log(self):
        git_add_block = _git_add_block(self.job)
        self.assertIn("logs/forecast_issuances.jsonl", git_add_block)
        self.assertIn("logs/historical/forecast_vintages/", git_add_block)

    def test_manual_dispatch_condition_excludes_other_flags(self):
        # Ticking sync_historical_data or run_station_analysis alone must
        # NOT also fire a real forecast/Telegram send as a side effect.
        if_line = re.search(r"if:.*?(?=\n    runs-on)", self.job, re.DOTALL).group(0)
        self.assertIn("inputs.sync_historical_data != true", if_line)
        self.assertIn("inputs.run_station_analysis != true", if_line)


class SyncHistoricalDataJobTests(unittest.TestCase):
    def setUp(self):
        self.job = _job_block(_read(WORKFLOW_PATH), "sync_historical_data")

    def test_runs_tests_before_sync(self):
        test_pos = self.job.index("unittest discover -s tests")
        sync_pos = self.job.index("historical_data.py sync")
        self.assertLess(test_pos, sync_pos)

    def test_commit_only_touches_historical_manifests(self):
        git_add_line = _git_add_block(self.job)
        self.assertIn("logs/historical/manifests/", git_add_line)
        for forbidden in ("weights.json", "docs/dashboard_data.json", "docs/research"):
            self.assertNotIn(forbidden, git_add_line, f"sync_historical_data must not stage {forbidden!r}")

    def test_never_references_weights_or_telegram(self):
        for forbidden in ("weights.json", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "backtest.py"):
            self.assertNotIn(forbidden, self.job)


class StationResearchJobTests(unittest.TestCase):
    """station_research runs station_analysis.py's fixed family comparisons
    and publishes the research dashboard data, but must never overwrite
    production weights or the main dashboard - there is no workflow option
    to promote a feature into production (a deliberate, explicit
    constraint - see docs/STATION_RESEARCH.md)."""

    def setUp(self):
        self.job = _job_block(_read(WORKFLOW_PATH), "station_research")

    def test_runs_tests_before_station_analysis(self):
        test_pos = self.job.index("unittest discover -s tests")
        analysis_pos = self.job.index("python station_analysis.py")
        self.assertLess(test_pos, analysis_pos)

    def test_commit_never_stages_weights_or_main_dashboard(self):
        git_add_line = _git_add_block(self.job)
        self.assertIn("logs/historical/reports/", git_add_line)
        self.assertIn("docs/research/research_data.json", git_add_line)
        for forbidden in ("weights.json", "docs/dashboard_data.json"):
            self.assertNotIn(forbidden, git_add_line, f"station_research must not stage {forbidden!r}")

    def test_never_references_weights_json_or_backtest(self):
        self.assertNotIn("weights.json", self.job)
        self.assertNotIn("python backtest.py", self.job)

    def test_runs_refresh_research_dashboard(self):
        self.assertIn("python refresh_research_dashboard.py", self.job)


class WorkflowDispatchInputsTests(unittest.TestCase):
    """Every workflow_dispatch boolean input referenced by an `if:`
    condition must actually be declared, so a typo can't silently make a
    flag permanently false."""

    def test_all_dispatch_inputs_referenced_in_ifs_are_declared(self):
        text = _read(WORKFLOW_PATH)
        inputs_block = re.search(r"workflow_dispatch:\n(.*?)^  pull_request:", text,
                                  re.MULTILINE | re.DOTALL).group(1)
        declared = set(re.findall(r"^\s+(\w+):\n\s+description:", inputs_block, re.MULTILINE))
        referenced = set(re.findall(r"inputs\.(\w+)", text))
        self.assertTrue(referenced)
        self.assertTrue(referenced.issubset(declared), f"undeclared inputs referenced: {referenced - declared}")

    def test_run_station_analysis_input_declared(self):
        text = _read(WORKFLOW_PATH)
        self.assertIn("run_station_analysis:", text)


if __name__ == "__main__":
    unittest.main()
