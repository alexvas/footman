"""Scenario tests are defined locally, collected once, never re-exported."""

import importlib
import inspect
from collections import Counter

import pytest


@pytest.mark.parametrize(
    "support_name,collector_names,expected_names",
    [
        pytest.param(
            "launcher_case_support",
            (
                "test_launcher_execution",
                "test_launcher_validation",
                "test_launcher_copy_streams",
            ),
            (
                "test_dev_exec_image_and_wrapper_argv",
                "test_dev_exec_status_and_streams",
                "test_dev_snapshot_copy_identity",
                "test_nonroot_prerequisite_error_status",
                "test_dev_image_id_forms",
                "test_invalid_inspection_forbids_copy",
                "test_invalid_startup_forbids_inspection",
                "test_inspection_unknown_image_is_diagnostic_only",
                "test_copy_paths_are_literal",
                "test_documentation_copy_contract",
                "test_failure_suppresses_downstream_calls",
                "test_summary_continues_real_drain",
                "test_summary_raw_prefix_chunk_independent",
            ),
            id="launcher_execution_cases-collector_names0",
        ),
        pytest.param(
            "dispatch_support",
            (
                "test_dispatch_lifecycle",
                "test_dispatch_confirmation",
                "test_dispatch_deadlines",
            ),
            (
                "test_dev_cleanup_sentinel_identity",
                "test_dev_survivor_removal_order",
                "test_supervisor_cleanup_interruption_retains_state_and_continuation",
                "test_prior_snapshot_removal_failure_cannot_be_upgraded",
                "test_rm_failure_does_not_suppress_confirmation",
                "test_snapshot_permission_failure_after_confirmation_is_unsuccessful",
                "test_unconfirmed_listing_retains_snapshot",
                "test_unknown_identity_never_guesses_removal",
                "test_cleanup_observation_requires_bounded_timeout",
                "test_command_setup_failure_still_removes_known_container",
                "test_cumulative_deadline_not_fresh_per_child",
                "test_independent_requests_do_not_share_cleanup_state",
                "test_interruption_retains_authority_and_known_identity_cleanup",
                "test_nonzero_terminal_path_removes_only_recorded_id",
                "test_shared_deadline_suppresses_downstream_work",
            ),
            id="dispatch_lifecycle_cases-collector_names1",
        ),
    ],
)
def test_split_cases_collected_once_without_replacing_contracts(
    support_name, collector_names, expected_names
):
    support = importlib.import_module(f"tests.{support_name}")
    assert not any(name.startswith("test_") for name in vars(support))
    collected = Counter()
    for collector_name in collector_names:
        collector = importlib.import_module(f"tests.{collector_name}")
        for name, function in inspect.getmembers(collector, inspect.isfunction):
            if name.startswith("test_"):
                assert function.__module__ == collector.__name__, (
                    "define scenario tests in their collected module, not a re-export"
                )
                collected[name] += 1
    assert collected == Counter(dict.fromkeys(expected_names, 1))
