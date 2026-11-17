"""One isolated expectation mismatch after a complete real-flow baseline."""

from typing import Any

from tests import host_acceptance_assertions as assertions


def test_negative_expectations(host_flow: Any, expectation: Any) -> None:
    scenario = host_flow.prepare("image", 0, None)
    observed = host_flow.execute(scenario)
    baseline = host_flow.verify(scenario, observed)
    print("FIXTURE_BASELINE_VERIFIED", flush=True)
    actual = {
        "staged_bytes": baseline["staged_bytes"],
        "argv": observed["manifest"]["command_argv"],
        "command_status": observed["manifest"]["return_code"],
        "cleanup": observed["manifest"]["cleanup"]["confirmed_absent"],
    }
    case = expectation["case_id"]
    assertions.verify_expectation(case, actual[case], expectation["expected_override"])
