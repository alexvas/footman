"""Docker-free copied-fixture controls, not a real-host suite or runner.

All reads, writes and children delegate to existing tested sole-owner helpers.
The generated fixtures consume only fixed-path JSON; they never launch Docker.
Actual MCP/manifest observations come from the caller's scripted-Docker flow.
"""

import base64
import hashlib
import json
import sys

from tests import host_acceptance_contract_support as h
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel
from tests.host_acceptance_assertions import EXPECTATION_DIAGNOSTICS

BASELINE_MARKER = b"FIXTURE_BASELINE_VERIFIED"
OUTCOMES_MARKER = b"FIXTURE_OUTCOMES="

ASSERTION_SUPPORT = (
    "tests/__init__.py",
    "tests/green_skip_policy.py",
    "tests/mcp_provenance.py",
    "tests/host_acceptance_assertions.py",
)
NORMAL_SUITE = "host_tests/test_normal_expectations.py"
NEGATIVE_SUITE = "host_tests/test_negative_expectations.py"

CONFTEST = '''"""Generated Docker-free fixture, not delivered real-host support."""
from pathlib import Path
import base64
import json
import pytest
from tests import host_acceptance_assertions as assertions
from tests import mcp_provenance as p

pytest_plugins = ["tests.green_skip_policy"]
_ROOT = Path(__file__).resolve().parents[1]
_EXPECTATION = None
_REPORTS = []
_COLLECTION_ERRORS = []
_INTERNAL_ERRORS = []
_INTERRUPTED = False

def pytest_runtest_logreport(report):
    crash = getattr(report.longrepr, "reprcrash", None)
    _REPORTS.append({
        "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
        "wasxfail": hasattr(report, "wasxfail"),
        "longrepr": str(report.longrepr) if report.longrepr is not None else "",
        "message": crash.message if crash is not None else None,
    })

def pytest_collectreport(report):
    if not report.passed:
        _COLLECTION_ERRORS.append(str(report.longrepr))

def pytest_internalerror(excrepr, excinfo):
    _INTERNAL_ERRORS.append(str(excrepr))

def pytest_keyboard_interrupt(excinfo):
    global _INTERRUPTED
    _INTERRUPTED = True

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        return
    teardown = [r for r in _REPORTS if r["when"] == "teardown"]
    value = {
        "schema_version": 1, "collected": session.testscollected,
        "exitstatus": int(session.exitstatus), "reports": _REPORTS,
        "collection_errors": _COLLECTION_ERRORS, "internal_errors": _INTERNAL_ERRORS,
        "interrupted": _INTERRUPTED,
        "cleanup_complete": len(teardown) == 1 and teardown[0]["outcome"] == "passed",
    }
    print("\\nFIXTURE_OUTCOMES=" + json.dumps(value, sort_keys=True), flush=True)

def pytest_sessionstart(session):
    global _EXPECTATION
    for module in (assertions, p):
        assertions.require(
            Path(module.__file__).resolve().parent == _ROOT / "tests",
            "ambient assertion/support fallback",
        )
    selected = [Path(arg).resolve() for arg in session.config.args]
    negative = selected == [_ROOT / "host_tests/test_negative_expectations.py"]
    normal = selected == [_ROOT / "host_tests/test_normal_expectations.py"]
    assertions.require(normal or negative, "unexpected fixture suite selection")
    path = _ROOT / "host_tests/expectation_input.json"
    if negative:
        _EXPECTATION = assertions.validate_expectation_input(p.load(path))
    else:
        assertions.require(not path.exists() and not path.is_symlink(),
                           "unexpected normal expectation input")

def pytest_report_header(config):
    return "Docker-free copied assertion fixture; development interpreter"

@pytest.fixture
def observations():
    print("FIXTURE_OBSERVATIONS_ACQUIRED")
    value = p.load(_ROOT / "host_tests/observations.json")
    value["streams"] = {
        key: base64.b64decode(data) for key, data in value["streams"].items()
    }
    for key in ("expected_stdout", "expected_stderr", "staged_bytes"):
        if key != "staged_bytes" or value.get(key) is not None:
            value[key] = base64.b64decode(value[key])
    return value

@pytest.fixture
def expectation():
    return _EXPECTATION
'''

NORMAL_TEST = """from tests.host_acceptance_assertions import verify_run

def test_normal_expectations(observations):
    verify_run(**observations)
"""

NEGATIVE_TEST = """from tests.host_acceptance_assertions import verify_expectation, verify_run

def test_negative_expectations(observations, expectation):
    # Establish the baseline first: unrelated invalid evidence is not detection.
    verify_run(**observations)
    print("FIXTURE_BASELINE_VERIFIED", flush=True)
    actual = {
        "staged_bytes": observations["staged_bytes"],
        "argv": observations["manifest"]["command_argv"],
        "command_status": observations["manifest"]["return_code"],
        "cleanup": observations["manifest"]["cleanup"]["confirmed_absent"],
    }
    case = expectation["case_id"]
    verify_expectation(case, actual[case], expectation["expected_override"])
"""


def copied_fixture(tmp, observations, *, profile, expectation=None, raw_input=None):
    """Prepare explicit normal/negative copies; generated input is not source."""
    h.check(profile in {"normal", "negative"}, "unknown copied fixture profile")
    workspace = tmp / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "host_tests").mkdir()
    sources = []
    for relative in ASSERTION_SUPPORT:
        data = p.read(h.ROOT / relative)
        p.write(workspace / relative, data)
        h.check(p.read(workspace / relative) == data, "assertion copy bytes differ")
        sources.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    p.write(workspace / "host_tests/__init__.py", b"")
    p.write(workspace / "host_tests/conftest.py", CONFTEST.encode())
    suite = NEGATIVE_SUITE if profile == "negative" else NORMAL_SUITE
    source = NEGATIVE_TEST if profile == "negative" else NORMAL_TEST
    p.write(workspace / suite, source.encode())
    encoded = dict(observations)
    encoded["streams"] = {
        key: base64.b64encode(data).decode("ascii")
        for key, data in observations["streams"].items()
    }
    for key in ("expected_stdout", "expected_stderr", "staged_bytes"):
        encoded[key] = base64.b64encode(observations[key]).decode("ascii")
    p.write(workspace / "host_tests/observations.json", p.canonical(encoded))
    if expectation is not None or raw_input is not None:
        data = p.canonical(expectation) if raw_input is None else raw_input
        p.write(workspace / "host_tests/expectation_input.json", data)
    ini = tmp / "host.ini"
    p.write(ini, b"[pytest]\naddopts = --strict-config --strict-markers\n")
    record = {
        "profile": profile,
        "suite": suite,
        "source_support": sources,
        "inventory": h.inventory(workspace, ("tests/", "host_tests/"))[1],
        "generated_inputs": ["host_tests/observations.json"]
        + (
            ["host_tests/expectation_input.json"]
            if expectation is not None or raw_input is not None
            else []
        ),
        "interpreter_context": "development; copied assertions, not wheel/host proof",
    }
    p.write(tmp / "copy-profile.json", p.canonical(record))
    return workspace, ini, record


def execute_fixture(tmp, workspace, ini, suite):
    result = wheel.command(
        [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "-c",
            ini,
            "--rootdir",
            workspace,
            workspace / suite,
            "-n",
            "1",
            "--dist",
            "worksteal",
            "--tb=short",
            "-q",
        ],
        cwd=workspace,
        env=wheel.clean_environment(),
        timeout_seconds=180,
        cleanup_reserve_seconds=6,
    )
    p.write(tmp / "fixture-operation.json", p.canonical(h.operation_record(result)))
    return result


def _has_exact_negative_outcomes(streams, case):
    lines = [
        line[len(OUTCOMES_MARKER) :]
        for stream in streams
        for line in stream.splitlines()
        if line.startswith(OUTCOMES_MARKER)
    ]
    if len(lines) != 1:
        return False
    try:
        value = json.loads(lines[0], object_pairs_hook=p.unique)
    except ValueError:
        return False
    keys = {
        "schema_version",
        "collected",
        "exitstatus",
        "reports",
        "collection_errors",
        "internal_errors",
        "interrupted",
        "cleanup_complete",
    }
    if type(value) is not dict or set(value) != keys:
        return False
    if any(
        type(value[key]) is not int or value[key] != 1
        for key in ("schema_version", "collected", "exitstatus")
    ):
        return False
    if (
        value["collection_errors"] != []
        or value["internal_errors"] != []
        or value["interrupted"] is not False
        or value["cleanup_complete"] is not True
    ):
        return False
    reports = value["reports"]
    if type(reports) is not list or len(reports) != 3:
        return False
    for report, when, outcome in zip(
        reports,
        ("setup", "call", "teardown"),
        ("passed", "failed", "passed"),
        strict=True,
    ):
        if type(report) is not dict or set(report) != {
            "nodeid",
            "when",
            "outcome",
            "wasxfail",
            "longrepr",
            "message",
        }:
            return False
        if (
            report["nodeid"] != NEGATIVE_SUITE + "::test_negative_expectations"
            or report["when"] != when
            or report["outcome"] != outcome
            or report["wasxfail"] is not False
            or type(report["longrepr"]) is not str
        ):
            return False
        if when != "call" and (
            report["longrepr"] != "" or report["message"] is not None
        ):
            return False
    return reports[1]["message"] == f"AssertionError: {EXPECTATION_DIAGNOSTICS[case]}"


def classify_negative_result(result, case):
    """Attribute fixture failure only after an independently verified baseline.

    command() already requires settled children and complete stream collection.
    A diagnostic alone cannot distinguish invalid baseline evidence from the
    deliberate expectation mismatch. Require the controller's exact structured
    outcomes: one collected item, passed setup/teardown and only its intended
    assertion failure. Missing/duplicate/malformed reports fail closed. Child
    collection alone does not establish successful pytest fixture cleanup.
    """
    h.check(case in EXPECTATION_DIAGNOSTICS, "unknown negative result case")
    streams = (result["stdout"], result["stderr"])
    baseline = (
        sum(stream.splitlines().count(BASELINE_MARKER) for stream in streams) == 1
    )
    detected = (
        baseline
        and type(result["status"]) is int
        and result["status"] == 1
        and _has_exact_negative_outcomes(streams, case)
    )
    if result["status"] == 0:
        failure_kind = "unexpected-success"
    elif not baseline:
        failure_kind = "invalid-observation/setup"
    else:
        failure_kind = "assertion" if detected else "unrelated-failure"
    return {
        "detected": detected,
        "baseline_verified": baseline,
        "failure_kind": failure_kind,
    }
