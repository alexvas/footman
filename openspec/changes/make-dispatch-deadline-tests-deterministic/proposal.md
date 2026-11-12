# Proposal

## Why

Cold gate 030 in the latest 100-run series failed while reading `sequence-3.json`, although the preceding failure, removal-confirmation and launch-count assertions passed. The deadline tests currently conflate parent-side launch attempts, child-written observations and resource settlement; a timed-out Python stub need not reach its recording point, and the retained traceback does not establish why that particular record was missing.

## What Changes

- Separate precise deadline-policy tests from a real-process timeout integration test. Use the existing controlled clock for the inspection/mkdir/copy boundary cases and cumulative remaining-budget assertions, without wall-clock sleeps.
- Use a parent-side launch journal to prove exact working-command prefixes, distinguish requested/acquired/failed launches, and reject downstream working launches after budget exhaustion. Observe cleanup separately under its existing allowance.
- Require a real `ProcessResult.timed_out` and completed local cleanup in the integration test, rather than accepting any run failure. Permit missing or interrupted child records only for that positively identified timed-out helper.
- Preserve strict child records for successful helpers and cleanup, fail on unexpected launch/import/status/lifecycle errors, and report the relevant journal and retained results on assertion failure.
- Keep production behavior, dispatch/cleanup budgets, registered suites, mandatory gate scope and ownership contracts unchanged. No OS process-tree polling, broad debug framework, repeat runner or new dependency is introduced.

## Capabilities

### New Capabilities

- `dispatch-deadline-verification`: Observable verification contracts for launch admission, shared execution-budget accounting, real helper timeout, child-record interpretation and independent cleanup evidence.

### Modified Capabilities

None. There are currently no durable specs under `openspec/specs/`. The pending `add-privileged-test-dispatcher` delta already defines the production bounded-lifecycle and ownership requirements; this change specifies their verification boundary without modifying that pending change or redeclaring its production capability.

## Impact

- Primary targets: `tests/test_dispatch_deadlines.py`, including `test_cumulative_deadline_not_fresh_per_child`, and narrowly scoped deadline scenario support.
- Supporting targets as needed: `tests/launcher_support.py`, `tests/dispatch_support.py`, test-only Docker stub modes and support-contract tests. `tests/test_phase_case_inventory.py` is an additional registration-only target: explicitly register added/renamed dispatch test function names, not parametrized pytest node IDs, without weakening exact-equality or module-ownership guards. If the new deadline support module is created, register it for the existing no-collected-tests support check. Existing strict `DockerSequence` callers must retain their current behavior.
- Reuse `tests/clock_support.py::ManualClock` and `module_clock`, existing `launch=` seams, `InfrastructureOperation.result` and the sole `OwnedProcess` owners; no production-source or package-metadata changes are planned.
- Document the two verification layers in `docs/testing.md`, preserving exactly one occurrence of the canonical full-suite command and the existing parallel-command documentation guard. Validate dispatch inventory registration and documentation guards in each phase, the registered Phase 6 and affected shared-support suites, the unscoped gate and the other applicable documented checks; focused checks and repetitions are supplemental only.
- Historical stress failures remain unsuccessful evidence. This proposal does not reopen or complete tasks in `add-privileged-test-dispatcher`, establish the historical root cause, or authorize Phases 10–11.
