"""Operator-only real MCP/container/copy/manifest acceptance."""

from typing import Any

from tests import host_acceptance_assertions as assertions


def test_staged_copy_execution(host_flow: Any) -> None:
    for kind in ("image", "wrapper"):
        for status in (0, 17):
            for phase in (None, "host_phase"):
                scenario = host_flow.prepare(kind, status, phase)
                observed = host_flow.execute(scenario)
                host_flow.verify(scenario, observed)
    for preparation in ("missing-parent", "existing"):
        scenario = host_flow.prepare("image", 0, None, preparation)
        observed = host_flow.execute(scenario)
        assertions.verify_preparation_failure(
            observed["value"],
            observed["manifest"],
            observed["streams"],
            scenario["destination"],
        )
