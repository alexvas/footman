# Tasks

## Binding implementation contract

This entire document is normative. MUST, MUST NOT and REQUIRED are binding, including requirements outside checkboxes. A checked box asserts that its stated completion criterion has been demonstrated; it is not a progress estimate. All boxes start unchecked. Planning-artifact existence is not implementation completion.

Read `proposal.md`, `design.md`, `specs/dispatch-deadline-verification/spec.md`, repository `AGENTS.md`, `docs/testing.md`, `docs/python-checks.md`, `docs/filesystem-lifecycle.md` and `docs/process-lifecycle.md` before implementation. This checklist makes the existing plan executable; it does not override those requirements. If they conflict, stop and request a planning correction. Do not choose a weaker interpretation.

### Scope and exclusions

The implementation MUST deliver parent-side launch evidence, deterministic deadline-policy tests, a separate real-process timeout check, scoped child-record interpretation, identity-aware cleanup verification and bounded failure diagnostics.

Permitted implementation paths are:

- `tests/test_dispatch_deadlines.py`: all new deadline-behavior contract/regression test functions belong here so the registered lifecycle suite continues to collect them without changing its command. The inventory parameter registrations permitted below are not new behavior-test functions.
- `tests/dispatch_deadline_support.py`: a new, narrowly scoped support module if needed; it is not a replacement process foundation or general runner.
- `tests/test_phase_case_inventory.py`: registration-only changes to explicit expected function names for the existing three dispatch collectors, plus a support-module registration if `dispatch_deadline_support` is created. Preserve existing collectors, unrelated registrations, exact Counter equality, once-only collection and `__module__` ownership checks; do not change guard logic or derive the expected inventory from the observed functions.
- `tests/launcher_support.py`, `tests/dispatch_support.py`: only compatibility-preserving support needed by this change.
- `tests/docker_cli_stub.py`: only explicit fixture modes needed to test record availability or a controlled failure; its existing default behavior MUST remain unchanged.
- `docs/testing.md`: documentation of this change's test layers and evidence interpretation. Existing registered command lines MUST remain unchanged.
- This change's `tasks.md`: task-completion boxes only during implementation. Do not rewrite this contract while implementing it.

Verification summaries and temporary logs belong outside the checkout under `/tmp/gorshochek/ephemeral/footman.git/verification/make-dispatch-deadline-tests-deterministic/`. Record a separate directory for each phase and attempt. Do not create committed verification runners or report files.

The implementer MUST NOT change production source, package/build metadata, lockfiles, check tooling, the shared clock implementation, MCP fixtures, another OpenSpec change or durable specs. If another implementation path or a production fix becomes necessary, stop and request approval before expanding scope.

The implementer MUST NOT add dependencies, OS process-tree polling, a benchmark/repeat runner, Cold-gate optimizations, helper retries, new shutdown budgets or Phases 10–11 work. Do not stage, unstage, reset, restore or otherwise manipulate the developer index. Preserve unrelated local changes.

### Inventory and documentation guard obligations

`tests/test_phase_case_inventory.py::test_split_cases_collected_once_without_replacing_contracts` inspects functions in `test_dispatch_lifecycle`, `test_dispatch_confirmation` and `test_dispatch_deadlines`. Its expected inventory contains unqualified function names such as `test_shared_deadline_suppresses_downstream_work`, NOT parametrized node IDs such as `test_shared_deadline_suppresses_downstream_work[3]`. Adding only parameter cases does not require duplicate function-name entries.

Explicitly register every added test function and replace an old name only when its function is actually renamed with its prior contract retained. Preserve all unaffected names and every existing collector. Do not make the guard accept subsets, filter functions, auto-discover expected names, remove ownership checks or ignore imported/re-exported tests. If `tests/dispatch_deadline_support.py` is created, add a registration-only parameter row using support name `dispatch_deadline_support`, an empty collector tuple and an empty expected-name tuple; the unchanged guard then forbids any `test_*` definition or re-export in that support module without duplicating the dispatch inventory. Do not register a nonexistent module.

Each phase has an explicit GREEN inventory checkpoint. Later test-function additions/renames in that same phase MUST also update the registration and rerun CI before acceptance; an earlier successful checkpoint does not waive later inventory drift. S01–S10 do not include this guard, so S07 success alone is not sufficient. CI is supplemental; G remains mandatory.

`tests/test_green_gate.py::assert_documented_suite` constrains `docs/testing.md`. Across the entire document, including prose, the canonical full-suite string `.venv/bin/python -m pytest tests -n 8 --dist worksteal --tb=short -q` MUST occur exactly once. Every added fenced pytest suite/focused example MUST be a single physical command line with exactly one `-n <decimal>` setting (ASCII decimal, 1–1024) and exactly one `--dist worksteal`. Do not use `--numprocesses`, another/duplicate `--dist`, disabled xdist or a serial focused example. Leave any existing pure `pytest --help`/`--version` probes alone. Do not change `tests/test_green_gate.py` to accommodate invalid documentation. CD checks these rules and is supplemental, not a substitute for G.

### Required meanings and observations

| Term | Required meaning |
| --- | --- |
| Working launch | One of startup, inspection, mkdir, cp or configured-command execution. |
| Cleanup launch | Recorded-ID `rm` or removal confirmation, using the existing cleanup allowance. |
| Requested | The injected parent launch callable was entered; record this before attempting acquisition. |
| Acquired | The existing launcher returned a sole `OwnedProcess` owner, or an explicitly synthetic no-fd owner in a logical test. Never infer acquisition from a requested path. |
| Failed acquisition | The launch attempt raised before successful owner handoff; retain the original exception and distinguish interruption. |
| Positive timeout | A retained result for that exact acquired working attempt has `timed_out=True`. A non-null run failure, elapsed wall time, a missing file or a negative exit status alone is insufficient. |
| Local settlement | Every relevant acquired owner reports complete cleanup. Completion alone does not erase cleanup failures. Unexpected lifecycle/release failures remain failures. |
| Child record | Evidence that the child reached its recording point. Its absence is not proof that no launch happened. |
| Known identity | The production startup parser retained a valid attributable container ID. Do not infer it from a partial record, a fixture's hoped-for output or a requested operand. |

Use exact expected argv generated from the predeclared fixture script, never from the actual journal being checked. Compare ordered argv prefixes, not just invocation counts. Assign stable request ordinals under a lock and associate acquisition/result observations with those ordinals, not list position after filtering. Cleanup can run on a different thread.

The journal verifies admission at the existing parent launch boundary. It MUST NOT claim atomic kernel process creation before an absolute timestamp: an already admitted parent or child can be descheduled. Do not replace controlled-clock admission assertions with comparisons of real timestamps.

A journal or diagnostic object borrows observations only; it MUST NOT adopt, signal, reap, close or independently collect a child or its streams. Use exactly the existing sole owners and one stream collection per real child. New acquisition, handoff and cleanup branches require the lifecycle fault coverage below. An exception while recording a newly acquired owner MUST NOT orphan that owner.

### Required execution and record outcomes

The predeclared working order is:

| Index | Operation |
| --- | --- |
| 0 | startup `docker run` |
| 1 | launch-validation `docker container inspect` |
| 2 | `docker exec ... mkdir` |
| 3 | `docker cp` |
| 4 | configured `docker exec` command |

After execution budget exhaustion, no next working request is permitted. Cleanup requests are not violations of this rule. Working and cleanup scripts MUST have independent strict cursors; do not assume cleanup starts at a fixed overall launch index, search ahead for a matching argv or accept arbitrary commands.

Logical tests MUST patch only `footman.container` through the existing `module_clock` adapter, before constructing the supervisor. Their clock starts at zero and advances only at named scenario boundaries. They MUST execute the real deadline/admission and parsing logic through `launch=` with no-fd scripted helpers. Do not patch `_remaining`, mutate the stored deadline, patch the shared `time` module, change process/cleanup clocks, or use sleeps to spend logical policy time. Scripted helpers emit separate stdout/stderr and terminal outcomes and expose collection/context-exit counts.

The logical boundary scenarios retain `timeout_seconds=0.3` and `cleanup_grace_seconds=1`; the cumulative scenario retains `timeout_seconds=0.5` and `cleanup_grace_seconds=2`. For exact boundary cases, advance directly from zero to the configured timeout or a clearly greater value. For cumulative arithmetic use explicit expenditures and `pytest.approx` with `rel=1e-12, abs=1e-12`; independently assert every admitted allowance is strictly positive. Do not use approximate comparison to decide whether an exhausted budget is positive. The advancement journal MUST match the intended named expenditures exactly once each.

The real-process scenario MUST keep `timeout_seconds=0.3`, `cleanup_grace_seconds=1`, and a deliberately slow terminal working helper with `delay=2`. Earlier helpers have no artificial delay. Supply all five working steps, not a prefix chosen to force a desired timeout stage. Accept an earlier working timeout only with its own positive timeout evidence; do not require reaching the terminal step. Do not increase these bounds, start the clock after a ready handshake or retry to obtain a particular stage. Real watchdogs remain real and do not prove that the child reached its sleep.

| Observed record/result | Required verdict |
| --- | --- |
| Successful working helper | Complete, valid record required; expected argv and stdin EOF required. |
| Successful cleanup helper | Complete, valid record required; expected argv and stdin EOF required. |
| Specifically identified acquired working helper with positive timeout and completed local cleanup | Absent or genuinely incomplete record is permitted as unavailable evidence; a complete record still requires validation. |
| Acquisition/import/ordinary exit failure without positive timeout | MUST NOT satisfy the timeout assertion. |
| Incomplete local cleanup or unexpected lifecycle/release failure | Verification fails, regardless of record availability. |
| Complete record with wrong argv, invalid schema or stdin not at EOF | Verification fails, including for a timed-out helper. |
| Permission, no-follow, read or close error reading a record | Verification fails; do not classify these as mere absence/truncation. |

For the known sequenced stub format, treat an empty file or a proper byte prefix of the expected serialized JSON as incomplete. A successful helper's incomplete file still fails. Arbitrary corrupt JSON MUST NOT be accepted by catching all decode errors. An expected prefix is only an eligibility check for unavailable diagnostics; it does not establish child argv, stdin EOF or execution. Keep the full actual bytes available for failure diagnosis within the diagnostic bounds.

If the identity is known, require literal recorded-ID rm followed by confirmation, complete successful empty listing, complete local helper settlement, no unresolved resources and expected snapshot deletion. If startup times out without an attributable identity, require no rm/confirmation launch, complete local settlement, unresolved-resource reporting and snapshot retention. The latter is a passing negative-contract test, not a successful dispatch or proven absence of external resources. Expected unknown-identity domain failures are not unexpected local release failures.

Failure rendering MUST identify phase/ordinal/argv, passed allowances, acquisition outcome, retained timeout/exit/stream observations, record availability and cleanup state. Bound the rendered diagnostic to 64 KiB total UTF-8 bytes, mark truncation explicitly, and retain untruncated in-memory values for assertions. Do not add hot-path diagnostic files or a second output reader. Ordinary rendering/read failures MUST NOT replace an existing primary assertion; interruptions MUST retain their required precedence.

### Lifecycle fault matrix

For EACH newly introduced or changed acquisition/handoff/wrapper/cleanup branch, identify it in a phase coverage table and provide these independently executable cases:

- Operation plus ordinary close failure: preserve the operation failure and retain the close failure.
- Sole close failure: keep it observable and do not report successful verification.
- Interruption: preserve interruption identity/authority and attempt independent cleanup.
- Repeated cleanup/observation: release at most once; retain owners and continuation when incomplete; create no new budget.
- Independent resources: attempt every independent release despite another ordinary release failure.
- Handoff/concurrency where present: maintain one release authority and coherent observations across the transfer/interleaving.

A no-fd fake MUST acquire no real descriptor or child. Synthetic failure/result objects are permitted for classifier and fault tests but MUST be labeled as synthetic and MUST NOT count as real-process timeout evidence. Reuse earlier matrix tests only when they exercise the same branch unchanged; record that mapping explicitly. If no new branch exists, state why in the coverage table instead of silently omitting the matrix.

### Verification command registry

All commands run from the repository root unless explicitly stated otherwise. Prerequisites: Linux/POSIX, an unprivileged user, Python 3.14+, Git, uv, GNU cp, package-index access when required, and the locked development environment. Resolve missing prerequisites; do not skip. The documented setup command is `uv sync --extra dev --locked`. If setup changes checked code/configuration or dependencies unexpectedly, stop and reconcile before claiming verification.

| ID | Exact command |
| --- | --- |
| G | `GREEN_CHECKS_WORKERS=8 scripts/check-green` |
| S01 | `.venv/bin/python -m pytest tests/test_foundation.py tests/test_foundation_ownership.py tests/test_scaffold.py -n 3 --dist worksteal --tb=short -q` |
| S02 | `.venv/bin/python -m pytest tests/test_configuration.py tests/test_scaffold.py -n 2 --dist worksteal --tb=short -q` |
| S03 | `.venv/bin/python -m pytest tests/test_scaffold.py -n 1 --dist worksteal --tb=short -q` |
| S04 | `.venv/bin/python -m pytest tests/test_output_reservation.py -n 1 --dist worksteal --tb=short -q` |
| S05 | `.venv/bin/python -m pytest tests/test_snapshot.py -n 1 --dist worksteal --tb=short -q` |
| S06 | `.venv/bin/python -m pytest tests/test_launcher.py tests/test_launcher_execution.py tests/test_launcher_validation.py tests/test_launcher_copy_streams.py tests/test_launcher_faults.py tests/test_launcher_boundaries.py -n 6 --dist worksteal --tb=short -q` |
| S07 | `.venv/bin/python -m pytest tests/test_dispatch_lifecycle.py tests/test_dispatch_confirmation.py tests/test_dispatch_deadlines.py tests/test_dispatch_cleanup_faults.py -n 4 --dist worksteal --tb=short -q` |
| S08 | `.venv/bin/python -m pytest tests/test_results.py -n 1 --dist worksteal --tb=short -q` |
| S09 | `.venv/bin/python -m pytest tests/test_process_stdin.py tests/test_process_lifecycle.py tests/test_process_orthogonal_results.py tests/test_process_single_collection.py -n 2 --dist worksteal --tb=short -q` |
| S10 | `.venv/bin/python -m pytest tests/test_mcp_transport.py tests/test_mcp_server.py -n 2 --dist worksteal --tb=short -q` |
| CI | `.venv/bin/python -m pytest tests/test_phase_case_inventory.py -n 2 --dist worksteal --tb=short -q` |
| CD | `.venv/bin/python -m pytest tests/test_green_gate.py -n 2 --dist worksteal --tb=short -q` |
| B | `uv build` |
| H1 | `uv run --locked footman --help` |
| H2 | `uv run --locked python -m footman --help` |

S07 is the already registered lifecycle command and is the RED/regression command for every phase in THIS change. Before each RED invocation, record its exact command and the new test node IDs in that phase's evidence. Do not change the registered command, worker count or scheduler. Put the new tests in its existing modules as specified above. The names Phase 1/2/3 below refer to this change, not the historical foundation/configuration/reservation phases represented by S01–S10.

W is the exact-wheel smoke procedure; it is REQUIRED for each phase to avoid an implicit installation-applicability decision. After B succeeds:

1. From the repository root, set `REPO=$(pwd -P)` and `WHEEL="$REPO/dist/footman-0.1.0-py3-none-any.whl"`. Run `test -f "$WHEEL"` and then `sha256sum "$WHEEL"`, recording both statuses and the digest. Require that exact artifact; do not select a different stale artifact or rebuild inside W.
2. Reserve a fresh external directory with `SMOKE=$(mktemp -d /tmp/footman-foundation.XXXXXX)`. Record the command status and absolute path. Never reuse an environment from another phase or attempt.
3. Run `uv venv --python python3.14 "$SMOKE/venv"`.
4. Run `uv pip install --python "$SMOKE/venv/bin/python" "$WHEEL" 'pytest==8.4.2' 'pytest-xdist==3.8.0'`. This is a separate foundation verification environment, NOT a service venv.
5. From `SMOKE`, run the import-provenance command below; every resolved module file MUST be beneath the new venv and outside the checkout. Capture the exact expanded command.
6. From `SMOKE`, run `env -u PYTHONPATH "$SMOKE/venv/bin/footman" --help`.
7. From `SMOKE`, run `env -u PYTHONPATH "$SMOKE/venv/bin/python" -m footman --help`.

Exact import-provenance command for W step 5 (run with the recorded `REPO` and `SMOKE` variables):

```sh
env -u PYTHONPATH "$SMOKE/venv/bin/python" -c '
import importlib
import sys
from pathlib import Path

prefix = Path(sys.prefix).resolve()
checkout = Path(sys.argv[1]).resolve()
for name in (
    "footman.filesystem.cleanup",
    "footman.filesystem.streams",
    "footman.filesystem.operations",
    "footman.filesystem.descriptors",
    "footman.process",
):
    module = importlib.import_module(name)
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(prefix) or origin.is_relative_to(checkout):
        raise AssertionError(f"unexpected installed origin: {name}: {origin}")
    print(name, origin)
' "$REPO"
```

W passes only when all steps succeed and the imported package comes from that exact installation. Record every expanded command and status. Do not remove unrelated dist artifacts or environments to make installation succeed. Retain this phase's environment until F completes.

F is the installed-foundation regression in that SAME W environment, from `SMOKE`:

`env -u PYTHONPATH "$SMOKE/venv/bin/python" -m pytest "$REPO/tests/test_foundation.py" "$REPO/tests/test_foundation_ownership.py" "$REPO/tests/test_scaffold.py" -n 3 --dist worksteal --tb=short -q`

If a documented command or wheel name has changed externally since this contract was approved, stop and reconcile the plan rather than silently changing commands.

### RED, GREEN, INTROSPECT and VALIDATE rules

Run each phase strictly in the subsection order below. A later subsection MUST NOT start before the preceding subsection's tasks pass their completion criteria. Later tests and later implementation MUST NOT be used to reconstruct an earlier RED claim.

- RED: write the phase's new contract tests without implementing the missing target behavior. A RED test-authoring box certifies executable assertions describing the required behavior, not a passing implementation; the separate RED-recording box certifies their observed nonzero run. Tests MUST collect normally and fail through assertions identifying required missing behavior, not broken fixtures, import/collection errors, missing tools or deliberate corruption of working production code. Use an assertion-based required-support check before importing a not-yet-created support module so absence is an identified contract failure, not a collection failure. Run S07 and retain complete output and the actual nonzero status temporarily. Identify the new failing nodes and their causes. Existing unrelated failures do not prove this phase's RED. If no genuine RED can be obtained, stop and request a plan correction; do not manufacture a failure or mark RED complete from a passing sensitivity test.
- GREEN: implement only this phase's missing behavior and its local documentation. Finish its relevant lifecycle matrix. Run G, B, H1, H2 and W successfully before declaring the GREEN candidate. The expected RED nonzero is not passing verification.
- INTROSPECT: review the listed invariants against actual code, tests and failure observations and record the findings. A finding requiring a change sends this phase back to its relevant RED/GREEN tasks; add a regression before fixing a newly found defect. Repeat the affected review and checks afterward. Do not waive a finding to advance.
- VALIDATE: complete S01–S10 and F on the accepted phase candidate, plus confirm G/B/H1/H2/W remain applicable. All required checks MUST have exit zero, no skipped/xfail/xpass outcomes and no incomplete cleanup. Summarize the delivered phase and its limitations. Only then may the next phase start.

G runs the unscoped lint, format, syntax, type and full parallel test checks. Never add scope/fix arguments, change its worker allocation, disable the registered skip plugin, suppress warnings, deselect tests or loosen assertions to obtain a pass. Execute every independent required check even after another fails; preserve the first unsuccessful status and all individual statuses, and settle every launched check. A failed prerequisite blocks only dependent checks (for example W after a failed B); explicitly record blocked checks as NOT VERIFIED. They prevent acceptance.

Use the budgeted scheduling policy in `docs/testing.md` when capacity is established to meet its CPU/affinity/quota condition; otherwise run sequentially. Keep every registered worker count and the shared specialised-worker budget. Do not deduplicate overlapping suites or replace them with a focused command. Focused runs are supplemental only. No new 100-run series or performance benchmark is required by this change.

A phase candidate is the checked code, tests, configuration and build inputs, not an index fingerprint. Checks may be reused between stages within that phase only if those checked inputs remain unchanged and the recorded results still apply. Code/test/configuration changes require fresh affected verification; a new phase requires its own candidate verification. Task-box edits, evidence edits and staging changes by someone else do not themselves invalidate results. Do not enforce permanent fingerprints as a completion condition.

Capture complete stdout/stderr temporarily for every invocation and preserve the runner's actual status; do not report the status of a pipe/filter/logger instead. For each phase, deliver a self-contained evidence summary with checked revision, local-change presence, exact expanded commands, relevant environment and CPU observations, actual statuses, test counts, significant warnings, blocked checks, identified RED failures, review findings and limitations. A durable summary MUST NOT depend on a transient log or pytest scratch link to explain its result. Full logs need not be permanently stored or committed.

Failures remain unsuccessful evidence. After a failure, diagnose and make a justified in-scope correction before fresh verification; do not rerun an unchanged candidate until it happens to pass. If a failure requires an out-of-scope change, finish independent checks, record the block and ask for scope approval. In particular, do not bypass an earlier phase gate because the old deadline test or another pending change fails.

Historical cold gate 030 and the 100-run series remain historical: 100/100 MCP successes and 99/100 cold-gate successes, unsuccessful overall. This change MUST NOT claim to establish that missing record's exact historical cause or overwrite earlier evidence. Completion of this change does not complete the pending dispatcher change or real-Docker acceptance.

## Phase dependency DAG and completion rule

```text
Phase 1 --> Phase 2 --> Phase 3
```

| Phase | Direct dependencies | Entry condition | Required deliverables |
| --- | --- | --- | --- |
| 1 | None | Prerequisites and scope understood; no implementation started | Tested journal/cursors, strict settlement/record baseline, diagnostics, lifecycle matrix, support documentation, RED and accepted candidate evidence. |
| 2 | Phase 1 | Every Phase-1 checkbox complete and its VALIDATE accepted | Deterministic boundary/cumulative scenarios, no-fd helper support, clock isolation/restoration tests, policy documentation, RED and accepted candidate evidence. |
| 3 | Phase 2 | Every Phase-2 checkbox complete and its VALIDATE accepted | Real-process timeout scenario, scoped record handling, explicit identity branches, failure controls, updated documentation, RED and final accepted evidence. |

No phase may depend on itself or a later phase. Within a phase, tasks are ordered by their dependencies and stage. Do not borrow unfinished support from a later phase to make an earlier phase pass. A phase is complete only when every task in all four stages is checked and every required deliverable exists. Tests and documentation land in the phase that introduces the behavior, not in a later cleanup phase. Every spec requirement is allocated below; none is deferred to archive.

## 1. Phase 1 — Parent-side launch evidence

### RED

- [x] 1.1 Add a request-before-acquisition contract test; completion: it demonstrates that an entered launch is recorded before the injected acquisition callback runs, even with no child file.
- [x] 1.2 Add failed-acquisition classification tests; completion: they require the original acquisition exception and forbid an acquired outcome for that attempt.
- [x] 1.3 Add acquisition-interruption classification tests; completion: they require preservation of the same interruption object without a false acquired outcome.
- [x] 1.4 Add independent-cursor contract tests; completion: every valid working prefix can transition to the two exact cleanup requests, while reordered/unknown/extra requests are rejected.
- [x] 1.5 Add journal concurrency tests using real synchronization rather than sleeps; completion: request ordinals and acquisition/result associations stay coherent across a controlled cleanup-thread interleaving.
- [x] 1.6 Add strict record/settlement baseline tests; completion: missing successful-helper records, complete invalid records and incomplete owners each remain failures for existing callers.
- [x] 1.7 Add diagnostic-precedence contract tests; completion: required diagnostic fields and the 64-KiB bound are checked, and ordinary rendering failures cannot replace an existing primary failure.
- [x] 1.8 Record Phase-1 RED with S07 before implementation; completion: the phase summary contains the predeclared command, actual nonzero status and new assertion failures attributable to missing Phase-1 behavior, with complete output temporarily captured.

### GREEN

- [x] 1.9 Implement parent request journaling; completion: test 1.1 passes with exact immutable observation snapshots and untruncated argv available to assertions.
- [x] 1.10 Synchronize Phase-1 dispatch inventory registration; completion: explicit expected function names include every Phase-1 addition/rename currently present, preserve all unaffected contracts, and CI exits zero with exact equality and module ownership unchanged. If the deadline support module exists, its no-test-functions registration is present. Refresh this registration and CI again for later Phase-1 function changes.
- [x] 1.11 Implement acquisition outcome association; completion: tests 1.2 and 1.3 pass without replacing the original exception/interruption or creating a second owner.
- [x] 1.12 Implement the independent strict script cursors; completion: test 1.4 passes without search-ahead, wildcard argv acceptance or reliance on a fixed overall cleanup offset.
- [x] 1.13 Implement synchronization for the journal; completion: test 1.5 passes and each request retains one stable ordinal and its own outcome.
- [x] 1.14 Separate settlement checking from strict record validation without enabling timeout tolerance yet; completion: test 1.6 passes and existing `DockerSequence` behavior remains strict by default.
- [x] 1.15 Implement bounded diagnostic rendering; completion: test 1.7 passes without additional stream collection or mandatory diagnostic-file I/O.
- [x] 1.16 Verify operation-plus-close precedence for each Phase-1 ownership branch; completion: the matrix maps every such branch to a passing case preserving the operation failure and retaining the ordinary close failure.
- [x] 1.17 Verify sole-close failure visibility for each Phase-1 ownership branch; completion: the mapped cases reject successful verification when release alone fails.
- [x] 1.18 Verify interruption cleanup for each Phase-1 ownership branch; completion: the mapped cases preserve interruption identity and attempt every independent release.
- [x] 1.19 Verify repeat-cleanup behavior for each Phase-1 ownership branch; completion: mapped cases prove once-only release and retained incomplete owners/continuation without a fresh budget.
- [x] 1.20 Verify independent-resource cleanup for each Phase-1 ownership branch; completion: mapped cases prove a sibling ordinary release failure cannot suppress another independent release.
- [x] 1.21 Verify handoff/concurrency ownership for each applicable Phase-1 branch; completion: mapped cases prove no owner is orphaned or released twice across the transfer/interleaving, with explicit reasons for non-applicable matrix cells.
- [x] 1.22 Document the journal contract in support docstrings and the relevant testing-guide section; completion: the text distinguishes request/acquisition/child record/settlement, preserves registered commands, retains exactly one canonical full-suite-command occurrence in `docs/testing.md`, and every added fenced pytest suite/focused example has exactly one decimal `-n <N>` and exactly one `--dist worksteal`, with no `--numprocesses` or disabled xdist; CD exits zero.
- [x] 1.23 Run G on the Phase-1 GREEN candidate; completion: actual exit zero, all independent gate checks attempted, no skip/xfail/xpass and complete check-process cleanup.
- [x] 1.24 Run B on the Phase-1 candidate; completion: exit zero and the exact produced wheel path and digest are recorded.
- [x] 1.25 Run H1 on the Phase-1 candidate; completion: exit zero with captured help output.
- [x] 1.26 Run H2 on the Phase-1 candidate; completion: exit zero with captured help output.
- [x] 1.27 Complete W for the Phase-1 wheel; completion: a fresh external environment passes exact-wheel import provenance and both installed help checks, with each status recorded.

### INTROSPECT

- [x] 1.28 Audit the Phase-1 journal's observation boundary; completion: record a review showing that neither child files nor launch counts substitute for exact parent argv and acquisition outcomes.
- [x] 1.29 Audit Phase-1 ownership and concurrency; completion: record the branch-to-matrix mapping and confirm no extra owner, second collection, independent signal/reap or unprotected handoff remains.
- [x] 1.30 Audit compatibility of existing support callers; completion: record the strict-default regression results and resolve every unintended change before proceeding.

### VALIDATE

- [x] 1.31 Run S01–S10 once each on the accepted Phase-1 candidate; completion: every suite passes with no skip/xfail/xpass and its counts/status are recorded.
- [x] 1.32 Run F in the Phase-1 W environment; completion: exit zero with no skip/xfail/xpass and installed-package provenance retained.
- [x] 1.33 Accept the Phase-1 evidence package; completion: its self-contained summary accounts for every required command and deliverable, confirms G/B/H1/H2/W applicability after review, and identifies no unresolved failure or out-of-scope change.

## 2. Phase 2 — Deterministic deadline policy

Dependency: accepted Phase 1. Do not begin RED until task 1.33 and all preceding Phase-1 tasks are complete.

### RED

- [ ] 2.1 Add the logical-scenario helper contract tests; completion: no-fd scripted results deliver separate streams/EOF, expose collection/context-exit counts and support explicit named clock expenditure without acquiring a real resource.
- [ ] 2.2 Add boundary-oracle contract tests for positions 1, 2 and 3; completion: they require exact expected working prefixes and the absence of the next launch after exhaustion, and reject an isolated trace containing the forbidden request.
- [ ] 2.3 Add cumulative-budget oracle tests; completion: they require the shared remaining allowances and reject a synthetic trace that renews the full timeout for each child.
- [ ] 2.4 Add clock-restoration contract tests; completion: normal return, assertion failure and interruption restore the original module reference without touching process/cleanup clocks.
- [ ] 2.5 Record Phase-2 RED with S07 before implementing the missing scenario support; completion: the summary identifies actual new assertion failures and the actual nonzero status, not a fabricated production defect or a passing negative-control run.

### GREEN

- [ ] 2.6 Implement no-fd scripted scenario helpers; completion: test 2.1 passes and every helper has exactly one collection and context-exit observation without owning a real fd or child.
- [ ] 2.7 Implement scoped logical-clock scenario setup; completion: test 2.4 passes with the adapter installed before supervisor construction and restored on all three exit paths.
- [ ] 2.8 Replace position 1 with deterministic expiry before inspection; completion: real admission logic records only startup as working work and then exact known-ID cleanup, with no inspection request.
- [ ] 2.9 Replace position 2 with deterministic expiry before mkdir; completion: real admission logic records only startup/inspection before exact known-ID cleanup, with no mkdir request.
- [ ] 2.10 Replace position 3 with deterministic expiry before cp; completion: real admission logic records only startup/inspection/mkdir before exact known-ID cleanup, with no cp request.
- [ ] 2.11 Add exact-deadline admission verification; completion: setting logical time exactly to the configured deadline forbids the next working request at each of the three boundaries.
- [ ] 2.12 Add past-deadline admission verification; completion: advancing logical time beyond the deadline forbids the next working request at each boundary.
- [ ] 2.13 Add positive-remainder admission verification; completion: before expiry the same next operation is requested with a strictly positive remaining allowance, preventing a vacuous always-reject oracle.
- [ ] 2.14 Rewrite `test_cumulative_deadline_not_fresh_per_child`; completion: named expenditures produce the expected decreasing positive allowances under the prescribed tolerance and exhaustion prevents the next working request without sleeps.
- [ ] 2.15 Synchronize Phase-2 dispatch inventory registration; completion: explicit expected function names include every Phase-2 addition/rename currently present, including any renamed boundary/cumulative functions, retain unaffected contracts and any created support-module registration, and CI exits zero with exact equality and module ownership unchanged. Refresh registration and CI for later Phase-2 function changes.
- [ ] 2.16 Verify logical-time expenditure integrity; completion: scenario advancement journals contain exactly the intended deltas/labels once, and repeated clock reads leave them unchanged.
- [ ] 2.17 Verify policy-oracle sensitivity; completion: isolated forbidden-request and renewed-budget traces from tests 2.2/2.3 are rejected while valid traces are accepted, without editing production source.
- [ ] 2.18 Document deterministic-policy scenarios; completion: `docs/testing.md` explains clock scope, exact admission semantics and the real-process distinction without changing suite commands, retains exactly one canonical full-suite-command occurrence, and every added fenced pytest suite/focused example has exactly one decimal `-n <N>` and exactly one `--dist worksteal`, with no `--numprocesses` or disabled xdist; CD exits zero.
- [ ] 2.19 Run G on the Phase-2 GREEN candidate; completion: actual exit zero, no skip/xfail/xpass and complete check-process cleanup.
- [ ] 2.20 Run B on the Phase-2 candidate; completion: exit zero and its exact wheel path/digest are recorded.
- [ ] 2.21 Run H1 on the Phase-2 candidate; completion: exit zero with captured help output.
- [ ] 2.22 Run H2 on the Phase-2 candidate; completion: exit zero with captured help output.
- [ ] 2.23 Complete W for the Phase-2 wheel; completion: a new external environment passes exact-wheel provenance and both installed help checks with all statuses recorded.

### INTROSPECT

- [ ] 2.24 Audit use of the real policy boundary; completion: record that tests execute production admission/parsing through `launch=` rather than patched `_remaining`, stored deadlines or clock-read side effects.
- [ ] 2.25 Audit clock-domain isolation; completion: record restoration-test results and confirm no real watchdog, process clock or cleanup clock is replaced and no scenario participant survives teardown.
- [ ] 2.26 Audit Phase-2 coverage completeness; completion: map inspection/mkdir/cp, exact/past/positive boundaries and cumulative spending to executed node IDs, and confirm no real-resource branch was introduced without its matrix.

### VALIDATE

- [ ] 2.27 Run S01–S10 once each on the accepted Phase-2 candidate; completion: every suite passes with no skip/xfail/xpass and its counts/status are recorded.
- [ ] 2.28 Run F in the Phase-2 W environment; completion: exit zero with no skip/xfail/xpass and installed-package provenance retained.
- [ ] 2.29 Accept the Phase-2 evidence package; completion: its self-contained summary accounts for all required commands/deliverables, confirms G/B/H1/H2/W applicability after review and identifies no unresolved failure or out-of-scope change.

## 3. Phase 3 — Real-process timeout and record-race verification

Dependency: accepted Phase 2. Do not begin RED until task 2.29 and all preceding Phase-2 tasks are complete. Phase-1 journal/cursor/diagnostic support and Phase-2 policy support MUST already be accepted, not copied from an unfinished branch.

### RED

- [ ] 3.1 Add per-helper timeout-record eligibility tests; completion: only an acquired positively timed-out working helper with completed local cleanup can have unavailable record evidence.
- [ ] 3.2 Add incomplete-record classification tests; completion: proper expected prefixes are distinguishable from complete invalid records, arbitrary corruption and independent read/release errors.
- [ ] 3.3 Add timeout-verdict rejection tests; completion: synthetic acquisition/import/ordinary-exit failures and unexpected lifecycle failures cannot satisfy the real-timeout verdict merely because the run failed.
- [ ] 3.4 Add known-identity cleanup-verdict tests; completion: the verdict requires exact ordered rm/confirmation, complete empty successful listing, local settlement and expected snapshot deletion.
- [ ] 3.5 Add unknown-identity cleanup-verdict tests; completion: no guessed cleanup, retained snapshot and explicit unresolved state are required despite local settlement.
- [ ] 3.6 Record Phase-3 RED with S07 before implementation; completion: actual new assertion failures establish missing Phase-3 behavior with the exact nonzero status and complete output temporarily captured.

### GREEN

- [ ] 3.7 Implement explicit per-attempt timeout-record eligibility; completion: test 3.1 passes without changing strict defaults or inferring timeout/acquisition from file absence.
- [ ] 3.8 Implement incomplete-record classification; completion: test 3.2 passes under the prescribed prefix rule without a blanket file/decode-error filter.
- [ ] 3.9 Implement the positive-timeout verdict; completion: test 3.3 passes using the exact retained attempt result and rejecting unexplained status/lifecycle failures.
- [ ] 3.10 Implement the known-identity cleanup verdict; completion: test 3.4 passes and rejects incomplete/nonempty/failed confirmation or premature snapshot deletion.
- [ ] 3.11 Implement the unknown-identity cleanup verdict; completion: test 3.5 passes and rejects guessed cleanup, missing unresolved state or deleted snapshot.
- [ ] 3.12 Add the stage-independent real-process timeout scenario; completion: real sole-owned children use the fixed 0.3/1-second allowances and full working script with terminal delay 2, the actual timed-out attempt is identified, and no later working request appears.
- [ ] 3.13 Synchronize Phase-3 dispatch inventory registration; completion: explicit expected function names include every Phase-3 addition/rename currently present, retain all unaffected contracts and any created support-module registration, and CI exits zero with exact equality and module ownership unchanged. Refresh registration and CI for later Phase-3 function changes.
- [ ] 3.14 Verify real-process local settlement; completion: task 3.12 checks every acquired owner after execution and fails on incomplete cleanup or unexpected release failure without new waits/budgets outside the existing owner contract.
- [ ] 3.15 Add deterministic absent-record classification coverage; completion: explicitly synthetic acquired/timed-out/settled attempt observations admit only their own deliberately absent fixture record, with no claim that synthetic data proves real child execution. Real timeout evidence remains independently required by task 3.12.
- [ ] 3.16 Add deterministic interrupted-record classification coverage; completion: explicitly supplied proper-prefix fixture bytes are admitted only for the mapped synthetic timed-out/settled attempt. Do not require a real child to reach a write before the deadline, seed fake child evidence into the real integration scenario, or claim an actual interrupted write from this classifier test.
- [ ] 3.17 Verify complete-record strictness for timed-out helpers; completion: complete wrong-argv, invalid-schema and stdin-not-EOF records are rejected even when their synthetic or real result is timed out, with synthetic controls labeled.
- [ ] 3.18 Verify mandatory records for successful helpers; completion: a successful working or cleanup helper with absent/incomplete/corrupt evidence is rejected and cannot inherit another attempt's timeout eligibility.
- [ ] 3.19 Verify known-identity handling through the real supervisor using controlled no-fd results; completion: the executed known-ID branch confirms absence, deletes its snapshot and reports no unresolved resources without relying on random real-child stage timing.
- [ ] 3.20 Verify unknown-identity handling through the real supervisor using controlled no-fd results; completion: the executed startup-timeout branch retains its snapshot and unresolved state, launches no guessed cleanup and settles local owners.
- [ ] 3.21 Verify Phase-3 operation-plus-close precedence; completion: every new/changed ownership branch has a passing case preserving the operation failure while retaining the ordinary close failure, or a justified unchanged-branch/non-applicability mapping.
- [ ] 3.22 Verify Phase-3 sole-close failure visibility; completion: the mapped cases reject successful verification when release alone fails.
- [ ] 3.23 Verify Phase-3 interruption cleanup; completion: the mapped cases preserve interruption identity and attempt independent releases.
- [ ] 3.24 Verify Phase-3 repeat-cleanup behavior; completion: the mapped cases prove once-only release and retained incomplete owners/continuation without a fresh budget.
- [ ] 3.25 Verify Phase-3 independent-resource cleanup; completion: mapped cases prove one ordinary release failure cannot suppress a sibling independent release.
- [ ] 3.26 Verify Phase-3 handoff/concurrency ownership; completion: applicable mapped cases prove a coherent handoff/interleaving with one release authority; justify non-applicable cells explicitly.
- [ ] 3.27 Document the final evidence rules; completion: guide/docstrings explain scoped record absence, stage-independent real timeout and both identity branches without claiming Docker semantics or historical root cause; preserve registered commands and exactly one canonical full-suite-command occurrence in `docs/testing.md`; every added fenced pytest suite/focused example has exactly one decimal `-n <N>` and exactly one `--dist worksteal`, with no `--numprocesses` or disabled xdist; CD exits zero.
- [ ] 3.28 Run G on the Phase-3 GREEN candidate; completion: actual exit zero, no skip/xfail/xpass and complete check-process cleanup.
- [ ] 3.29 Run B on the Phase-3 candidate; completion: exit zero and its exact wheel path/digest are recorded.
- [ ] 3.30 Run H1 on the Phase-3 candidate; completion: exit zero with captured help output.
- [ ] 3.31 Run H2 on the Phase-3 candidate; completion: exit zero with captured help output.
- [ ] 3.32 Complete W for the Phase-3 wheel; completion: a new external environment passes exact-wheel provenance and both installed help checks with all statuses recorded.

### INTROSPECT

- [ ] 3.33 Audit the timeout/record decision table; completion: record executed passing/rejected node IDs for every row and confirm no missing evidence is upgraded into child-execution or external-success proof.
- [ ] 3.34 Audit Phase-3 lifecycle and budgets; completion: record the branch-to-matrix mapping and confirm sole release authority, once-only collection, fixed allowances, no helper retries and no independent signal/wait/reap/borrowed close.
- [ ] 3.35 Audit final scope and spec coverage; completion: map all seven spec requirements to executed nodes, verify each phase's documentation is present, and confirm no unauthorized path, Cold-gate optimization, pending-change task or production behavior was changed.

### VALIDATE

- [ ] 3.36 Run S01–S10 once each on the accepted final candidate; completion: every suite passes with no skip/xfail/xpass and its counts/status are recorded.
- [ ] 3.37 Run F in the Phase-3 W environment; completion: exit zero with no skip/xfail/xpass and installed-package provenance retained.
- [ ] 3.38 Accept the final evidence package; completion: the self-contained summary accounts for all required commands/deliverables, confirms final G/B/H1/H2/W applicability after review, preserves unsuccessful historical results and states no unresolved failure or unauthorized scope expansion.

## Workflow follow-up

- Review the completed implementation and evidence before requesting archive. Archive and spec sync are not implementation tasks and are not authorized by this checklist.
- Do not mark the separate `add-privileged-test-dispatcher` change complete based on this change's result.
