# Design

## Context

See `proposal.md` for motivation. Discovery established:

- `tests/test_dispatch_deadlines.py` uses real 2-second stub sleeps, a shared 0.3-second execution budget and an exact launch count for the inspection/mkdir/copy cases. Its cumulative test also combines real sleeps with an assumption about which child is reached.
- `DockerSequence.launch` records parameters and a future record path before calling `OwnedProcess.launch`. `assert_settled` first checks owner settlement, then reads every child record. A requested path is therefore not evidence that the child wrote anything.
- `tests/docker_cli_stub.py` imports `tests.dispatcher_harness`, evaluates an stdin read, and writes its record before its configured sleep. Missing-file evidence does not identify which earlier boundary failed.
- `ContainerExecution` establishes its shared deadline in its constructor, calls `_remaining` before each working launch, and retains startup/infrastructure/command results. `RunSupervisor` owns a separate cleanup plan and preserves unresolved resources when no ID is attributable.
- `tests/clock_support.py` already provides read-pure `ManualClock` and module-local `module_clock`. Existing launcher and cleanup tests demonstrate no-fd scripted helpers through the `launch=` seam.
- There are no durable specs yet. The pending `privileged-test-dispatch` delta's bounded-run, recorded-removal and independent-cleanup requirements remain authoritative production context; they are not changed by this verification-only capability.

Design is included because clock isolation, threaded cleanup and shared support behavior benefit from explicit decisions before implementation.

## Goals / Non-Goals

**Goals:**

- Make admission assertions independent of Python child startup and wall-clock scheduling.
- Retain real executable-boundary timeout coverage and strong cleanup/record checks.
- Make failure evidence distinguish admission, acquisition, timeout, record availability and resource settlement.

**Non-Goals:**

- No production edits, new timeout API, larger allowances, retries, process-tree observer, or change to registered worker counts.
- No guarantee of atomic OS process creation relative to the deadline: this verifies supervisor admission at its existing boundary, not an impossible scheduling guarantee between the last check and kernel execution.
- No inference that cold gate 030 was definitely a timeout-before-record case; no claim of renewed Phase-9 GREEN or completion from this plan.

## Decisions

### 1. Treat the launch journal as the admission oracle

Use a test-local, thread-safe journal at the injected launch boundary. Each attempt has a stable ordinal, phase (working or cleanup), literal argv, passed execution/grace allowance and acquisition outcome. Retained operation results supply timeout, status and stream/lifecycle observations. Real monotonic timestamps are diagnostic only; logical admission assertions use the controlled clock.

Record the request before acquisition. Distinguish an acquired owner from an exception and preserve the original exception, including interruption. A journal entry is not child-execution evidence and does not acquire release authority.

Prefer a deadline-specific adapter and preserve the default strict `DockerSequence` API for unrelated callers. If a shared API is extended, new tolerance is explicit and opt-in, with regression tests for the unchanged default.

Alternative rejected: child JSON as the launch oracle, since recording occurs after interpreter startup. Also rejected: inferring order from counts alone or polling OS process trees.

### 2. Separate working and cleanup scripts

The current single positional script expects cleanup at one fixed offset. A real timeout at an earlier stage moves that offset and can make a valid removal look like an unexpected execution operation.

A deadline-only adapter keeps independent strict working and cleanup cursors and one parent journal. It admits only the predeclared ordered working prefix and literal recorded-ID cleanup argv; it does not search ahead, wildcard unknown commands or synthesize child success. It can compose independently fixture-owned sequences in separate directories. Cleanup may run on its existing worker thread, so request ordering and journal snapshots require a lock.

This allows the test to inspect the actual execution prefix without changing production control flow or silently weakening argv validation.

### 3. Use one logical policy clock for exact boundaries

Install `module_clock` only on `footman.container` before constructing `RunSupervisor`, so the deadline origin and subsequent reads share one timeline. Do not patch the shared `time` module, `footman.process`, threaded cleanup timing or real watchdogs.

Use no-fd scripted helpers at the existing launch seam. They return the required protocol bytes, EOF and explicit lifecycle results, recording collections/context exits. After a preceding successful helper completes, the scenario explicitly advances logical time to exhaust the budget before inspection, mkdir or cp (the existing positions 1, 2 and 3). Assert the exact working argv prefix and the absence of the next working request. Known-ID rm and confirmation still run through their separate allowance.

Add exact-deadline, past-deadline and positive-remainder controls, choosing explicit advances that avoid ambiguous floating-point boundary expectations. Rework the cumulative test to charge named expenditures and compare passed remainders with appropriate numerical tolerance, not wall-clock sleeps. Clock reads never spend time, and the advancement journal proves each intended charge once.

Alternative rejected: patching `_remaining` or returning a prewritten timeout error, because that bypasses the policy under test. Also rejected: starting the execution clock after a child-ready handshake, which excludes startup from the real production budget.

### 4. Keep a stage-independent real-process integration layer

Retain the current short execution allowance and deliberately over-budget helper work, using actual `OwnedProcess` children and exactly one existing stream collection per child. Provide the full working script with a deliberately slow terminal working operation; accept any earlier working-helper timeout only when its retained result positively reports `timed_out`. Do not accept an arbitrary failure, early nonzero/import error, acquisition failure or unexpected lifecycle failure as equivalent evidence.

Use the parent journal and actual results to identify the timed-out helper and require no later working request. Do not require the delayed terminal operation to have been reached. Assert every acquired owner has completed cleanup and preserve incomplete observations as failure.

Handle attribution explicitly:

- Known ID: require exact rm/listing argv, successful complete empty listing, full cleanup child records, completed helper settlement, no unresolved resources and expected snapshot deletion.
- Unknown ID after startup timeout: require no guessed-ID removal/listing, complete local settlement, explicit unresolved-resource reporting and snapshot retention. This is a passing negative-contract test, not a successful dispatched run.

Cover both attribution branches with controlled scenarios so random startup timing does not decide whether a branch is tested.

A wall-clock watchdog remains real as a containment bound, not evidence that a particular stub sleep was reached. No deadline is renewed to obtain a convenient outcome.

### 5. Scope record tolerance to positive timeout evidence

Separate owner-settlement checks from child-record validation. A missing/incomplete record is diagnostic-only for the specifically mapped, acquired, timed-out working helper whose local cleanup completed. A complete record is still checked for argv and stdin EOF. Successful preceding helpers and successful cleanup helpers retain mandatory complete, valid records.

Do not implement tolerance by filtering missing paths or globally ignoring JSON errors. Do not infer stdin EOF, child entry, correct child argv or external success from a missing record. Deliberately absent and interrupted-record controls exercise this classification without needing a probabilistic startup race; invalid complete records, non-timeout failures and missing successful-helper records must remain failures.

### 6. Keep diagnostics bounded and lifecycle-neutral

Render a bounded per-attempt table on assertion failure: phase, ordinal, argv, allowances, acquisition result, retained timeout/exit/stream observations, record availability and owner cleanup state. Prefer memory observations during execution and pytest failure output afterward; no mandatory hot-path debug file or extra stream reader.

Do not let a secondary record-read/rendering failure replace the original assertion. Use existing retained results and owners rather than collecting streams again or independently signaling, waiting/reaping or closing borrowed handles. Any new owner handoff or cleanup branch must preserve operation/interruption precedence and receive the mandatory fault matrix.

## Risks / Trade-offs

- [Shared support becomes permissive] -> Keep existing defaults strict; add targeted negative tests proving unexpected argv, missing successful records, invalid complete records and incomplete cleanup still fail.
- [A logical test only tests its fake] -> Run the real dispatcher admission logic and protocol parsing; use a positive-remainder control and explicit advancement evidence. Keep independent real-process timeout coverage.
- [A prior helper times out before the intended delayed step] -> Validate the actual ordered prefix, retain the specific timeout result and handle identity-aware cleanup rather than assuming a fixed script offset.
- [Extreme OS stalls still break a bounded real-process integration run] -> Preserve that failure and diagnostics; do not promise universal timing immunity, enlarge budgets, retry until pass or reinterpret missing positive timeout evidence as success.
- [Clock domains leak into cleanup or other tests] -> Patch only the component module, use scoped restoration, settle all scenario participants and test restoration on assertion failure/interruption.
- [Record omission conceals a real fault] -> Require per-helper positive timeout and settlement, keep complete-record validation, and test rejected non-timeout and wrong-argv controls.
- [Historical evidence is overwritten] -> Keep the 100-run result (100/100 MCP, 99/100 cold gates) historical and unsuccessful overall. Store new verification separately with exact commands/statuses and candidate/environment details.

## Migration Plan

1. Add and test deadline-local journal/script support while retaining existing strict callers.
2. Replace the three wall-clock stage cases and cumulative-budget case with logical-policy scenarios; add the separate real-process integration and record-classification controls.
3. Document the layers and run the affected registered suites and unscoped gate plus applicable documented build/help/install checks. Focused iterations aid diagnosis but never substitute for G.
4. Keep only this change's task boxes eligible for completion. Do not alter the pending dispatcher change or its existing verification history.

Rollback is limited to reverting this change's test/support/doc edits by normal review; no runtime data migration, dependency change or production rollback is involved. Do not stage, reset or modify unrelated working-tree/index changes as part of implementation.
