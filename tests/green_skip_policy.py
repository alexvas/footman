"""Reject all skips and xfail/xpass reports, including collection skips."""

import pytest


class SkipReview:
    def __init__(self):
        self.findings = []

    def _review(self, report):
        if hasattr(report, "wasxfail"):
            self.findings.append(f"{report.nodeid}: xfail/xpass: {report.wasxfail}")
        elif report.failed and str(report.longrepr).startswith("[XPASS(strict)]"):
            self.findings.append(f"{report.nodeid}: {report.longrepr}")
        elif report.skipped:
            self.findings.append(f"{report.nodeid}: skip: {report.longrepr}")

    def pytest_runtest_logreport(self, report):
        self._review(report)

    def pytest_collectreport(self, report):
        self._review(report)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        # Preserve failures, interruptions and other existing nonzero statuses.
        if self.findings and not session.exitstatus:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter):
        if self.findings:
            terminalreporter.section("BLOCKED: skips or xfail/xpass")
            for finding in sorted(set(self.findings)):
                terminalreporter.write_line(finding)


def pytest_configure(config):
    config.pluginmanager.register(SkipReview(), "green-skip-review")
