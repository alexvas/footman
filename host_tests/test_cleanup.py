"""Operator-only timeout/removal proof and separate injected host-path faults."""

from typing import Any

from tests import host_acceptance_assertions as assertions


def test_timeout_survivor(cleanup_flow: Any) -> None:
    cleanup_flow.mcp_timeout()
    record = cleanup_flow.run("image", "timeout")
    assertions.verify_timeout_cleanup(record["manifest"], record["observations"])
    assertions.verify_generated_entries(record["observations"]["generated"])


def test_generated_entries(cleanup_flow: Any) -> None:
    record = cleanup_flow.run("wrapper", "complete")
    assertions.require(
        record["manifest"]["outcome"] == "succeeded", "generated-entry run unsuccessful"
    )
    assertions.verify_generated_entries(record["observations"]["generated"])


def test_snapshot_retention(cleanup_flow: Any) -> None:
    for fault in ("permission", "owner"):
        record = cleanup_flow.run("image", "complete", fault)
        assertions.verify_safe_retention(
            record["manifest"],
            snapshot_present=record["observations"]["snapshot_after_run"],
        )
        assertions.require(
            record["reconciled"] is True, "retained fixture snapshot unresolved"
        )
