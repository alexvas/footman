"""Resource-free pytest registration for isolated host acceptance.

Host execution keeps real OS timing. No development clock fixtures or
checkout conftest inheritance are required.
"""

import json
from typing import Any

import pytest


class Outcomes:
    """Resource-free assertion/teardown attribution; controller reports only."""

    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.collection_errors: list[str] = []
        self.internal_errors: list[str] = []
        self.interrupted = False

    def pytest_runtest_logreport(self, report: Any) -> None:
        crash = getattr(report.longrepr, "reprcrash", None)
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "wasxfail": hasattr(report, "wasxfail"),
                "longrepr": str(report.longrepr) if report.longrepr is not None else "",
                "message": crash.message if crash is not None else None,
            }
        )

    def pytest_collectreport(self, report: Any) -> None:
        if not report.passed:
            self.collection_errors.append(str(report.longrepr))

    def pytest_internalerror(self, excrepr: Any, excinfo: Any) -> None:
        self.internal_errors.append(str(excrepr))

    def pytest_keyboard_interrupt(self, excinfo: Any) -> None:
        self.interrupted = True

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        if hasattr(session.config, "workerinput"):
            return
        teardown = [r for r in self.reports if r["when"] == "teardown"]
        value = {
            "schema_version": 1,
            "collected": session.testscollected,
            "exitstatus": int(session.exitstatus),
            "reports": self.reports,
            "collection_errors": self.collection_errors,
            "internal_errors": self.internal_errors,
            "interrupted": self.interrupted,
            "cleanup_complete": session.testscollected > 0
            and len(teardown) == session.testscollected
            and len({r["nodeid"] for r in teardown}) == session.testscollected
            and all(r["outcome"] == "passed" for r in teardown)
            and not self.interrupted
            and not self.internal_errors,
        }
        print("\nFIXTURE_OUTCOMES=" + json.dumps(value, sort_keys=True), flush=True)


HOST_MARKERS = (
    "process_time: process scenario (real OS timing on the host)",
    "real_process_time: keep operating-system process timing real",
    "own_process_time: scenario supplies its own process clock",
)
