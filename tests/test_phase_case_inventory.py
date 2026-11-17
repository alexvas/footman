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
                "test_unconfirmed_listing_wire_observation_requires_child_record",
                "test_settled_real_child_without_record_is_not_wire_evidence",
                "test_unknown_identity_never_guesses_removal",
                "test_cleanup_observation_requires_bounded_timeout",
                "test_command_setup_failure_still_removes_known_container",
                "test_timeout_record_eligibility_is_per_attempt",
                "test_timeout_record_classifies_only_expected_prefixes",
                "test_timeout_verdict_requires_positive_retained_result",
                "test_known_identity_timeout_cleanup_verdict",
                "test_unknown_identity_timeout_cleanup_verdict",
                "test_real_timeout_is_stage_independent",
                "test_deadline_record_reader_sole_owner_fault_matrix",
                "test_successful_helper_cannot_inherit_timeout_record_tolerance",
                "test_controlled_timeout_identity_branches_use_real_supervisor",
                "test_cumulative_deadline_not_fresh_per_child",
                "test_independent_requests_do_not_share_cleanup_state",
                "test_interruption_retains_authority_and_known_identity_cleanup",
                "test_nonzero_terminal_path_removes_only_recorded_id",
                "test_logical_helper_streams_counts_and_expenditure",
                "test_logical_boundary_oracle_rejects_forbidden_request",
                "test_logical_cumulative_oracle_rejects_renewed_budget",
                "test_logical_scenario_restores_only_container_clock",
                "test_shared_deadline_suppresses_downstream_work",
                "test_request_is_journaled_before_acquisition",
                "test_failed_acquisition_is_not_an_owner",
                "test_acquisition_interruption_keeps_identity",
                "test_independent_script_cursors_accept_every_working_prefix",
                "test_independent_script_cursors_reject_nonliteral_requests",
                "test_journal_interleaving_keeps_stable_ordinals_and_results",
                "test_existing_sequence_keeps_strict_records_and_settlement",
                "test_sequence_settlement_and_record_checks_are_separate",
                "test_journal_diagnostics_are_bounded_without_truncating_observations",
                "test_diagnostic_failure_preserves_primary_authority",
                "test_journal_handoff_fault_matrix",
                "test_settlement_does_not_reclassify_expected_operation_failure",
                "test_deadline_failure_policy_rejects_unexpected_or_cleanup_failures",
                "test_settlement_and_failure_policy_are_independent",
                "test_acquisition_failure_survives_failed_journal_publication",
                "test_failed_handoff_keeps_primary_when_owner_exit_raises",
                "test_failed_handoff_snapshots_remain_coherent_during_cleanup",
                "test_diagnostics_preserve_first_interruption",
                "test_journal_retains_record_observations_without_reading_or_tolerating_them",
                "test_journal_rejects_invalid_observation_ordinals",
            ),
            id="dispatch_lifecycle_cases-collector_names1",
        ),
        pytest.param(
            "dispatch_deadline_support", (), (), id="dispatch_deadline_support"
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
