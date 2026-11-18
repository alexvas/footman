# Tasks

## Binding execution contract

Implement one supported-boundary behavior at a time in the numbered topological order. Each phase completes RED → GREEN → INTROSPECT → VALIDATE before its dependent phase begins. Preparation and final acceptance are verification procedures. Scope comprises immutable pool acquisition, CPU/worker capacity, session namespaces and replacements, all existing admitted regions, security, original deadlines, sole-owner lifecycle, documentation and measured evidence. Existing production APIs, uv settings, worker allocations and results scheduling retain their contracts.

### Supported seams

- **A:** tests.admission_support.AdmissionPool(*, directory: Path, capacity: int, system: AdmissionSystem | None = None), immutable description with acquisition-scoped resources; acquire(*, deadline: int | float) yields None. Capacity is positive int; bool/wrong types raise TypeError, nonpositive values ValueError. Deadline is finite absolute monotonic int/float; bool/wrong types raise TypeError, NaN/infinity ValueError, exhaustion TimeoutError before work. Construction is resource-free; acquisition checks the existing directory securely.
- **O:** AdmissionSystem and PosixAdmissionSystem from tests.admission_support. Stable descriptors: DescriptorOps; try_lock_exclusive(fd) -> bool; monotonic() -> float; sleep(seconds) -> None. True means acquired, False contention, other errors propagate. Locking borrows the fd; the pool handles slot selection/time/retries/cleanup. Clocks are finite/nondecreasing; sleep accepts finite nonnegative intervals and may return early/be interrupted. Recording/fault adapters delegate real OS operations while real filesystem owners remain active. P06's regular-slot fstat variant changes only st_uid; real ownership, every other field/precision, directory observations and release remain intact.
- **C:** resolve_admission_capacity(*, workers: int, system: CapacitySystem | None = None) -> immutable CapacityDecision(capacity: int, reason: str). Positive capacity, nonempty diagnostic reason with implementation-defined wording. Workers has positive-int validation including bool rejection. CapacitySystem.affinity() -> frozenset[int] and read_text(path: Path) -> str supply raw observations; real resolver discovery/parsing remains active. Reads complete owner cleanup before returning. CapacityObservationUnavailable retains expected unavailability's cause; recognized malformed observations also permit K=1 fallback. Known unlimited quota contributes an unbounded quota term. Interruptions, release failures and unexpected errors propagate. Operation-plus-close preserves primary operation and secondary release evidence as failure.
- **S:** public pytest hooks and session-scoped admission -> immutable SessionAdmission.shared_pool, exported from tests.admission_support. The provider is resource-free and returns a stable pool description per worker. Controller resolves workers=len(final local specs) in pytest_xdist_setupnodes after numeric/auto/logical/--maxprocesses resolution, computes capacity once, and transfers version/decision/reason in pytest_configure_node to initial/replacement nodes. Serial/-n 0 uses workers=1. Namespace derives through injected public tmp_path_factory.getbasetemp(): serial base, local worker/replacement parent. Supported topology is same-host/shared-filesystem local popen. Invalid/unsupported configuration fails before work with pytest.UsageError, prefix 'invalid admission configuration:' and a reason. Distributed payload validation precedes tests; absolute namespace validation occurs at fixture setup. The payload schema remains internal.

Use design.md's contracts as the starting interface/seam record for each phase. Changes to public contracts or seams require agreement before tests. Handshake/channel mechanics are implementation choices within these boundaries.

### Prerequisites and baseline

Read AGENTS.md, docs/testing.md, docs/python-checks.md, docs/filesystem-lifecycle.md and docs/process-lifecycle.md completely before applicable work. Read the glossary/relevant ADRs when present. Establish documented tooling, real uv/git and pytest-xdist prerequisites. Leave index management to the user.

Before checked-code changes, store a self-contained baseline under `/tmp/gorshochek/ephemeral/footman.git/verification/`: revision, local index/worktree changes, exact command/environment, statuses/counts, warnings and limitations. Use the full G command below. Capture worker count and current affinity/quota observations; record legacy admission as one slot and the pool decision as pending until the resolver exists. Current-candidate reports remain machine-local as specified by docs/testing.md. The repository file docs/verification/session-build-admission-planning-baseline.md is a retained historical summary, with the same historical-only role as docs/testing-worksteal.md. Its missing CPU observations limit comparisons with that run to descriptive wall-time/check-count comparisons. A controlled CPU/capacity comparison requires a fresh baseline with those observations and comparable conditions.

### Suite registration before P01 RED

- [ ] PRE.1 Register the new phase suite in docs/testing.md before recording P01 RED. Append exactly one fenced command `.venv/bin/python -m pytest tests/test_admission_pool.py -n 1 --dist worksteal --tb=short -q`. Completion: the document contains exactly one occurrence of the canonical mandated full-suite command, exactly one new suite command, and the queue table retains exactly rows 1..13 and their existing allocations; test_host_interfaces_are_registered_before_red remains satisfied. This registration is a prerequisite documentation task. The registered command is S below and runs in EVERY phase's VALIDATE, accumulating all delivered pool phases. Its validation obligation is independent of queue-row allocation; test_host_interfaces_are_registered_before_red pins queue rows to 1..13.

### Exact verification procedures

Run from repository root. Each phase's literal test name N selects a node in planned tests/test_admission_pool.py:

`.venv/bin/python -m pytest tests/test_admission_pool.py::N -n 1 --dist worksteal --tb=short -q`

- **R(N):** run that exact focused command, capture complete stdout/stderr and actual nonzero status. The assertion demands the specified final behavior and fails because that behavior is missing. Explicitly unimplemented callable scaffolding is permitted. Establish imports, syntax, setup and tooling prerequisites first. Freeze the behavior/seam contract at meaningful RED.
- **T(N):** run the same focused command on the candidate; require exit 0 and all parameter cases passing, with zero skips/xfails/xpasses.
- **S:** run exactly `.venv/bin/python -m pytest tests/test_admission_pool.py -n 1 --dist worksteal --tb=short -q`, the suite registered by PRE.1. Capture complete stdout/stderr and actual status; require exit 0 and zero skips/xfails/xpasses. Every phase's VALIDATE includes S alongside T and G, covering all earlier delivered pool phases.
- **M(module):** run the exact existing registered command from docs/testing.md for the migrated contract's owning module, capturing complete stdout/stderr, actual exit status, counts and warnings in E; require exit 0 and zero skips/xfails/xpasses. Resolve these literal mappings:
  - M(transport): `.venv/bin/python -m pytest tests/test_mcp_transport.py tests/test_mcp_server.py -n 8 --dist worksteal --tb=short -q` (priority-10 MCP registration; C01–C05).
  - M(workspace): `.venv/bin/python -m pytest tests/test_host_workspace.py -n 8 --dist worksteal --tb=short -q` (workspace registration; C06–C10).
  - M(tooling): `.venv/bin/python -m pytest tests/test_host_build_tooling.py tests/test_host_provenance.py tests/test_host_provenance_faults.py tests/test_host_workspace.py tests/test_host_orchestration_core.py -n 8 --dist worksteal --tb=short -q` (host-tooling registration; C11–C13).
  Record the corresponding current docs/testing.md registration with each run. An owning C-migration phase runs its M command(s) alongside T+G in GREEN and INTROSPECT, after applicable edits, and independently alongside T+S+G in VALIDATE. Earlier registered specialised commands required by docs/testing.md remain part of subsequent validation. Attempt every independent procedure and preserve each actual status.
- **G:** run exactly `GREEN_CHECKS_WORKERS=8 scripts/check-green`. Require exit 0 for lint, format, compile, types and full pytest, with zero skips/xfails/xpasses. Attempt every independent leg and preserve all statuses and the first failing status. This full gate applies to every delivered GREEN/INTROSPECT/VALIDATE candidate.
- **E:** record revision, local changes, exact commands/environment, actual statuses/counts, significant warnings, limitations and applicability under `/tmp/gorshochek/ephemeral/footman.git/verification/`, following docs/testing.md's machine-local current-candidate evidence policy. Preserve full RED output with its record. Ordinary logs may be temporary; durable summaries are self-contained. Checked-code/test/config changes require fresh applicable checks; staging/evidence-only edits preserve check applicability.

Record exact nested child commands/statuses and fresh absolute basetemp for each invocation. P11's auto/logical/capped/serial probes are scenario inputs; registered verification commands retain positive literal -n and worksteal. Keep the registered skip-policy plugin active and capture complete output/statuses.

### Common stage gates and lifecycle companions

- **RED:** separate contract record, necessary callable scaffolding, focused behavioral tracer creation and R execution. Existing working behavior remains operational. Scaffolding explicitly identifies missing behavior. Advance on meaningful RED with frozen acceptance criteria.
- **GREEN:** implement current behavior behind the frozen boundary, preserve earlier phases, document the slice separately, and deliver passing T+G/E. Contract defects return to an agreed revised RED contract/test and fresh RED evidence.
- **INTROSPECT:** review contract compliance, coupling, independent expectations, duplication, scope, complexity and lifecycle. Record dispositions. Perform justified internal refactoring here, preserving the frozen public contract; otherwise record the decision to retain the structure. Run T after every refactoring and G on the candidate.
- **VALIDATE:** independently rerun T+S+G and every applicable M command, including earlier registered specialised commands required by docs/testing.md; audit acceptance, seams, earlier-phase applicability and evidence. Every numbered VALIDATE task below inherits these mandatory procedures even where its focused completion clause abbreviates T+G. Proceed after all deliverables/checks pass.

Every acquisition/transfer/wrapper/cleanup branch includes operation-plus-close, sole-close, interruption and repeat-cleanup companions, plus independent cleanup/handoff/concurrency where applicable. These cases belong to the introducing phase's resource contract. External O close records and public acquisition progress are the observation seams; filesystem owners execute real release paths. Independent behaviors requiring separate cycles receive separately agreed phases. Maintain one release authority per resource, one close attempt, stable active slot-file identities and sequential acquisitions around already-admitted helpers. New subprocess helpers use OwnedProcess.launch.

### Caller migration procedures

P13–P19 use design.md's seven required keyword-only admission boundaries, preserving other arguments, envelopes and results. bind/scripted_binding forward to invoke; acquiring callers own acquisition. Installation's existing attribution_only branch accepts explicit None; workspace rejects explicit None before work; heavy paths require a pool.

Before each caller-owning RED, run:

`rg -n 'build_and_install_contract_wheel|prepared_root|invoke|prepared_command|provision_workspace|reuse_for_test|standalone_policy|scripted_binding|bind' tests host_tests scripts`

Retain file:line lists and counts, classifying definitions/imports/calls/aliases, and inspect indirect/dynamic forwarding. Include mcp_contract_support world/provider/selection/swap/fault paths, mcp_attribution_support, build_failure_support and h.prepared_root/h.invoke aliases as applicable. Update every affected call in its signature-changing phase. Refresh inventory/dispositions in GREEN/INTROSPECT/VALIDATE E. Future-caller variants begin in their owning phase. Design.md's C01–C13 legacy-contract inventory also assigns every existing admission guard to a migration owner. Retain equivalent behavior in the registered MCP suite; each owning phase records old function/cases, replacement function/cases, preserved assertions, removed API dependency and exact verification/status in E. C01's disposition enumerates all 12 point/fault pairs: {metadata, flock, body} × {operation-close, sole-close, interruption, repeat}, with the primary identity, secondary release evidence, independent cleanup and repeat-release observation for each applicable case. Refresh anchors from source and account for tests/conftest.py:27–96 wiring as its dependencies change.

P17 introduces reuse_for_test.admission and migrates R3. It also introduces standalone_policy.admission, updates every standalone_policy caller and forwards the same pool to reuse_for_test. Existing R2 protection continues until P19; existing R7 protection continues until P18. Record all three regions' active mechanisms at P17 review. P18 migrates R7, retaining R2 protection and P17 forwarding. P19 migrates R2 and completes legacy removal. Pool slot filenames are distinct from legacy mcp-build.lock; pool and legacy acquisitions form separate exclusion domains. During P13–P18, K bounds pool holders while legacy holders can run concurrently, so total admitted work can exceed K and the resolved CPU budget. Each intermediate phase's GREEN/INTROSPECT/VALIDATE E records active pool/legacy regions, their separate bounds, and this aggregate-concurrency limitation. Full-session K acceptance applies after P19 removes legacy admission.

**Direct variants:** construct per-test AdmissionPool(directory=<unique tmp_path namespace>, capacity=1, system=<recording/fault O>) and pass admission=pool. The namespace is separate from outer session resources, giving T/G identical controlled capacity. Cross-caller variants give this pool to two already-supported callers and observe real contention/release/progress.

**Nested wiring variants:** use P11's one-worker harness and real application fixtures/adapters. A supported scenario admission override requests the original fixture, preserves directory/capacity, supplies recording O and returns SessionAdmission(shared_pool=pool). Real callers/controller policy remain active. The external holder at inner B plus O contention/progress establishes provider-to-caller wiring.

**P13 nested scope:** retain the base inner command; request real admission, prepare repository inputs through tests.mcp_contract_support.prepare_inputs and call supported build_and_install_contract_wheel directly with admission=admission.shared_pool and short positive timeout_seconds. This establishes provider-to-installation-caller integration. Lazy-fixture dependency/forwarding is established by inventory review with existing application tests/full G as regression evidence. Evidence labels distinguish these scopes.

**Fixture dependencies:** tests/test_admission_pool.py explicitly requests admission and, when an already-built base is needed, _mcp_worker_wheel. Fixture availability is maintained independently of collector-specific admission patches; explicit dependencies select heavy provisioning. Existing callers retain working transitional setup until migrated.

**Ordering/observations:** finish admitted prerequisites and release their setup admission before the test hold. P13 uses prepared repository inputs as installation is the observed operation. Pair ready/attempt/entry/release handshakes with positive O busy-lock observations and post-release progress within a recorded bounded window. Window expiry is a test failure. Positive TimeoutError tests use installation's existing short duration or invoke's short absolute deadline. Subsequent controls are separate calls with their own budget. prepared_root/reuse_for_test retain existing envelopes and use contention/release/progress. Literal spec/worked examples determine expected values. Public seam changes needed for an observation require agreement before tests.

## Phase dependency table

Every phase also consumes the preceding phase's VALIDATE evidence. Functional dependencies below identify implementation deliverables.

| ID | Slice | Functional dependencies |
| --- | --- | --- |
| P01 | Acquire/release and namespace tracer | Baseline; A/O |
| P02 | Configuration validation | P01 constructor/acquire |
| P03 | Bounded free-slot admission/progress | P01 sole-owner acquisition |
| P04 | Waiting deadline | P03 busy-pool behavior |
| P05 | Acquisition expiry | P04 deadline |
| P06 | Safe-node validation | P03 scan/O |
| P07 | Provisional workers/affinity | P02 validation; C |
| P08 | Effective v2 quota | P07 resolver/raw C |
| P09 | Effective v1 quota | P08 semantics/owned reads |
| P10 | Conservative fallback | P09 discovery |
| P11 | Shared session/replacements | P03 pool; P10 resolver |
| P12 | Invalid setup refusal | P11 configuration |
| P13 | Installation | P11 provider; P12 refusal |
| P14 | Host preparation | P13 installation |
| P15 | Host execution R5/R8 | P14 integration |
| P16 | Workspace | P11 provider; P05 deadline |
| P17 | Clone and required forwarding | P16 migrated callers |
| P18 | Policy provisioning | P17 clone/forwarding; R7 inventory |
| P19 | Private support/final migration | P17 clone; P18 provisioning; prior migrations |

## 1. P01 — Acquire/release tracer

Acceptance: resource-free K=1 pool descriptions in arbitrary directories A/B; while A is held B admits; after release A admits again. Seam A+O. Scope: minimum acquisition/adapter and initial namespace isolation.

### RED
- [ ] 1.1 Record exports, A/O signatures, validation, side effects, ownership and observation scope from design.md; completion: test contract identifies acquire/release and namespace observations.
- [ ] 1.2 Add necessary callable unimplemented new-interface scaffolding; completion: imports/calls work and existing legacy behavior remains operational.
- [ ] 1.3 Create `test_pool_admits_and_releases`; completion: hold A, observe real B entry, then A reacquisition, with lifecycle companions and external handshakes.
- [ ] 1.4 Execute R(test_pool_admits_and_releases); completion: missing behavior explains the failed assertion and contract/E are frozen.

### GREEN
- [ ] 1.5 Implement K=1 acquisition/release; completion: arbitrary valid namespace/deadline behavior satisfies the contract.
- [ ] 1.6 Document construction, directory identity/isolation, stable slot-file identities, ownership and errors; completion: matches P01 contract.
- [ ] 1.7 Execute T(test_pool_admits_and_releases)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 1.8 Record common review findings/dispositions; completion: contract and lifecycle requirements satisfied.
- [ ] 1.9 Apply justified internal refactoring or retain structure; run T after edits and G afterward; completion: passing E.

### VALIDATE
- [ ] 1.10 Independently execute T(test_pool_admits_and_releases)+G and audit deliverables; completion: all common gates/P01 acceptance pass.

## 2. P02 — Configuration validation

Acceptance: invalid capacity types/values raise specified TypeError/ValueError before I/O; valid descriptions remain immutable. Seam A+O; consumes P01.

### RED
- [ ] 2.1 Record constructor/deadline types, values and errors; completion: literal examples match design.md.
- [ ] 2.2 Create `test_pool_rejects_invalid_configuration`; completion: type/value parameters and zero-resource observations exercise validation.
- [ ] 2.3 Execute R(test_pool_rejects_invalid_configuration); completion: absent validation explains failure.

### GREEN
- [ ] 2.4 Implement frozen validation; completion: invalid inputs fail before I/O and P01 behavior remains valid.
- [ ] 2.5 Document accepted inputs/errors/immutability; completion: matches examples.
- [ ] 2.6 Execute T(test_pool_rejects_invalid_configuration)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 2.7 Record validation review; completion: independent expected values and public observations.
- [ ] 2.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 2.9 Independently execute T(test_pool_rejects_invalid_configuration)+G; completion: P01/P02 acceptance passes.

## 3. P03 — Bounded free-slot admission and release progress

Acceptance: K=2 permits two overlapping holders; caller three positively observes contention and enters after release, with at most K holders throughout. Include K=1 exclusion/progress control. Seam A+O; consumes P01. Free-slot use, bound and progress form one tracer contract.

### RED
- [ ] 3.1 Freeze ready/attempt/entry/release protocol and lifecycle companions, including busy-probe release faults; completion: positive O contention and real child handshakes establish observations.
- [ ] 3.2 Create `test_pool_uses_available_capacity`; completion: K=2 overlap, saturated waiting, bounded release progress, K=1 control and helper lifecycle coverage.
- [ ] 3.3 Execute R(test_pool_uses_available_capacity); completion: absent bounded free-slot behavior explains failure; freeze the complete contract.

### GREEN
- [ ] 3.4 Implement bounded free-slot acquisition/progress; completion: one held slot per request, at most K holders, busy probes released once, failures propagated.
- [ ] 3.5 Document free-slot use, saturated waiting and implementation-defined contender ordering; completion: original budget retained.
- [ ] 3.6 Execute T(test_pool_uses_available_capacity)+G; completion: all variants/companions pass, exit 0/E.

### INTROSPECT
- [ ] 3.7 Record scan/bound/progress/lifecycle review; completion: sole owners, stable inode identities, external observations and configured bound retained.
- [ ] 3.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 3.9 Independently execute T(test_pool_uses_available_capacity)+G; completion: overlap, limit, waiting, progress, control and prior phases pass.

## 4. P04 — Waiting consumes original deadline

Acceptance: controlled elapsed time in a saturated pool exhausts the original allowance and raises TimeoutError before work. Seam A+O; consumes P03.

### RED
- [ ] 4.1 Freeze monotonic/timeout contract, exact/past examples and interruption companions; completion: literal independent time examples.
- [ ] 4.2 Create `test_pool_wait_uses_original_deadline`; completion: controlled O time drives real acquisition with external work observation.
- [ ] 4.3 Execute R(test_pool_wait_uses_original_deadline); completion: missing original-deadline accounting explains failure.

### GREEN
- [ ] 4.4 Implement waiting/scans against the same absolute deadline; completion: elapsed work spends the original allowance.
- [ ] 4.5 Document waiting/timeout semantics; completion: existing production budgets preserved.
- [ ] 4.6 Execute T(test_pool_wait_uses_original_deadline)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 4.7 Record deadline/cleanup review; completion: controlled time provides the policy oracle.
- [ ] 4.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 4.9 Independently execute T(test_pool_wait_uses_original_deadline)+G; completion: timeout precedes work and prior phases pass.

## 5. P05 — Deadline expires during acquisition

Acceptance: a lock obtained at/after deadline is released, with TimeoutError preceding protected entry. Seam A+O; consumes P04.

### RED
- [ ] 5.1 Freeze final-boundary semantics and companion release faults; completion: literal exact/past cases.
- [ ] 5.2 Create `test_pool_checks_deadline_before_entry`; completion: O advances time at the external lock boundary.
- [ ] 5.3 Execute R(test_pool_checks_deadline_before_entry); completion: late protected entry explains failure.

### GREEN
- [ ] 5.4 Implement final-boundary timeout/release; completion: expired acquisition releases and reports timeout before work.
- [ ] 5.5 Document expiry during acquisition; completion: deadline covers entry.
- [ ] 5.6 Execute T(test_pool_checks_deadline_before_entry)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 5.7 Record boundary/precedence review; completion: timeout retains precedence over ordinary close failure.
- [ ] 5.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 5.9 Independently execute T(test_pool_checks_deadline_before_entry)+G; completion: final-boundary and earlier deadline cases pass.

## 6. P06 — Safe-node validation

Acceptance: encountered symlink/nonregular/wrong-owner directory or slot terminates admission with a visible security error before protected entry. Seam A+O; consumes P03.

### RED
- [ ] 6.1 Freeze unsafe-node/error observations. For foreign UID delegate real open/fstat, select a real regular slot by stat type, change only st_uid, preserve every other field/precision and directory observation, actual ownership/fd/release. Completion: trace establishes that admission's slot-owner validation consumed the supplied observation after DirectoryDescriptor's successful directory validation. Failure to establish the observation is a prerequisite error.
- [ ] 6.2 Create `test_pool_rejects_unsafe_node`; completion: real validation variants and operation/close companions.
- [ ] 6.3 Execute R(test_pool_rejects_unsafe_node); completion: missing security behavior explains failure. If behavior already satisfies the contract, agree a revised meaningful phase boundary before proceeding.

### GREEN
- [ ] 6.4 Implement current security refusal; completion: security errors propagate distinctly from contention.
- [ ] 6.5 Document safe-node requirements/errors; completion: matches frozen contract.
- [ ] 6.6 Execute T(test_pool_rejects_unsafe_node)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 6.7 Record security/lifecycle review; completion: secure node checks and sole-release semantics retained.
- [ ] 6.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 6.9 Independently execute T(test_pool_rejects_unsafe_node)+G; completion: refusal/resource companions pass.

## 7. P07 — Provisional worker/affinity capacity

Acceptance: intermediate max(1, min(workers, len(affinity))) for valid affinity; workers=8/affinity=6 yields 6. Input scope is workers and raw affinity; quota discovery belongs to P08/P09, final fallback to P10, session integration to P11. Seam C; consumes P02.

### RED
- [ ] 7.1 Record C signatures/result, workers validation, error classification and export path; completion: provisional calculation/observation scope explicit.
- [ ] 7.2 Add necessary callable unimplemented resolver scaffolding; completion: missing behavior identified explicitly.
- [ ] 7.3 Create `test_capacity_respects_workers_and_affinity`; completion: literal provisional examples, worker validation and relevant resource companions. P08/P09 extend raw fixtures to valid-unlimited quota as their discovery lands.
- [ ] 7.4 Execute R(test_capacity_respects_workers_and_affinity); completion: missing provisional policy explains failure.

### GREEN
- [ ] 7.5 Implement worker/affinity policy/result; completion: calculation uses workers and affinity observations.
- [ ] 7.6 Document intermediate contract/input errors and quota-phase handoff; completion: scope matches acceptance.
- [ ] 7.7 Execute T(test_capacity_respects_workers_and_affinity)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 7.8 Record policy/seam review; completion: independent expected values and affinity-based bound.
- [ ] 7.9 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 7.10 Independently execute T(test_capacity_respects_workers_and_affinity)+G; completion: provisional resolver/pool phases pass.

## 8. P08 — Effective cgroup v2 quota

Acceptance: real membership/mount-root/ancestor discovery caps capacity; workers=8/affinity=6/quota=2.5 yields 2, sub-CPU quota yields 1, known unlimited retains worker/affinity bound. Seam C; consumes P07.

### RED
- [ ] 8.1 Freeze raw quota/ancestor/unlimited examples and owned-I/O companions; completion: independent expected results through resolver seam.
- [ ] 8.2 Create `test_capacity_respects_v2_quota`; completion: finite/unlimited/ancestor/fractional real-discovery variants. Extend P07 fixtures with raw valid-unlimited v2 observations, retaining worker/affinity expectations.
- [ ] 8.3 Execute R(test_capacity_respects_v2_quota); completion: missing v2 behavior explains failure.

### GREEN
- [ ] 8.4 Implement effective v2 discovery through C/owners; completion: applicable ancestor minimum respected, reads released once.
- [ ] 8.5 Document v2 semantics; completion: known-unlimited and unknown classifications explicit.
- [ ] 8.6 Execute T(test_capacity_respects_v2_quota)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 8.7 Record discovery/lifecycle review; completion: raw observations, sole owners and released streams.
- [ ] 8.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 8.9 Independently execute T(test_capacity_respects_v2_quota)+G; completion: examples/companions pass.

## 9. P09 — Effective cgroup v1 quota

Acceptance: v1 quota/period, membership/mount roots and ancestor restrictions implement corresponding CPU-cap semantics. Seam C; consumes P08.

### RED
- [ ] 9.1 Freeze raw v1 examples and new resource companions; completion: literal independent results.
- [ ] 9.2 Create `test_capacity_respects_v1_quota`; completion: real discovery variants, including valid-unlimited observations for retained worker/affinity expectations.
- [ ] 9.3 Execute R(test_capacity_respects_v1_quota); completion: missing v1 behavior explains failure.

### GREEN
- [ ] 9.4 Implement v1 discovery; completion: finite/unlimited/fractional/ancestor cases satisfy contract.
- [ ] 9.5 Document supported membership/mount discovery; completion: applicable layouts represented.
- [ ] 9.6 Execute T(test_capacity_respects_v1_quota)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 9.7 Record v1/v2 consistency/ownership review; completion: shared semantics and supported boundaries retained.
- [ ] 9.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 9.9 Independently execute T(test_capacity_respects_v1_quota)+G; completion: both quota generations/prior phases pass.

## 10. P10 — Conservative unknown-budget fallback

Acceptance: unknown/unsupported/malformed/inaccessible CPU observations yield capacity one and a diagnostic; interruption/release/unexpected failures propagate. Seam C; consumes P09.

### RED
- [ ] 10.1 Freeze unavailable/unlimited classification and release precedence; completion: reason semantics and failure identities explicit.
- [ ] 10.2 Create `test_capacity_reports_conservative_fallback`; completion: discovery limitations and lifecycle companions through C.
- [ ] 10.3 Execute R(test_capacity_reports_conservative_fallback); completion: missing conservative result or failure propagation explains failure.

### GREEN
- [ ] 10.4 Implement classified fallback; completion: unknown yields one, known-unlimited preserves bounded policy, interruptions/close faults propagate.
- [ ] 10.5 Document limitations/diagnostics; completion: matches classification.
- [ ] 10.6 Execute T(test_capacity_reports_conservative_fallback)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 10.7 Record exception review; completion: fallback limited to specified expected observation failures after clean cleanup.
- [ ] 10.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 10.9 Independently execute T(test_capacity_reports_conservative_fallback)+G; completion: complete capacity policy/release semantics pass.

## 11. P11 — Shared session and replacement pool

Acceptance: real one-worker session K=1 times out against outer holder at inner B and acquires after release. Final planned worker count determines one controller decision shared with initial/replacement nodes; serial uses workers=1. Seams S/A; consumes P03/P10.

### RED
- [ ] 11.1 Record design.md's plugin/import/environment contract: tests.admission_plugin registered once via repository pytest_plugins or explicit inner -p; generated W outside inventory roots; cwd repository root; absolute .venv Python; isolated I/rootdir/confcutdir; fresh absolute B; explicit xdist/green_skip_policy/admission_plugin; -n 1 --dist worksteal. Scenario hooks load from W/conftest.py; ini pythonpath serves subsequent imports and early plugin imports use cwd. Use clean_environment, remove PYTEST_ADDOPTS/PYTEST_PLUGINS/PYTHONSAFEPATH, set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 and retain executable paths. Record public final-spec resolution, serial/replacement policy and namespace derivation through injected tmp_path_factory. Establish ready after provider/basetemp initialization, then outer AdmissionPool(directory=B, capacity=1) hold/attempt/release/progress. Completion: prerequisite setup succeeds and real resource/handshake observations are specified.
- [ ] 11.2 Add necessary callable fixture/hook scaffolding; completion: new boundary explicitly identifies missing behavior and existing collectors remain operational.
- [ ] 11.3 Create `test_session_workers_share_admission`; completion: deterministic inner K=1 timeout under hold and separate post-release acquisition; initial/replacement delivery in the same tracer. Terminate initial worker outside acquisition, record restart allowance/crash identity/status and settle helpers. Include numeric/auto/logical/capped/serial input variants; W/conftest.py's public auto-worker hook returns three and --maxprocesses=2 yields two worker-ready events. The real CPU resolver bounds admission. Include helper lifecycle companions and exact child commands/statuses.
- [ ] 11.4 Execute R(test_session_workers_share_admission); completion: missing shared-session behavior explains failure after successful setup.

### GREEN
- [ ] 11.5 Implement final-spec count resolution, one saved decision and generic initial/replacement transfer, serial local initialization, SessionAdmission fixture and public-temp-factory namespace derivation. Completion: shared absolute directory/capacity, resource-free provider, unsupported topology/count and nonabsolute namespace refusal before work.
- [ ] 11.6 Document worker modes/caps, serial/replacement policy, topology and exclusive basetemp use. Completion: migrated scope accurate; fenced docs/testing.md commands use one positive decimal -n and one --dist worksteal with xdist active; queue priorities 1..13/allocations retained.
- [ ] 11.7 Execute T(test_session_workers_share_admission)+G; completion: asserted child scenarios and outer/full checks pass/E.

### INTROSPECT
- [ ] 11.8 Record session/harness review; completion: internal payload schema, single plugin registration and real resolver/owner paths retained.
- [ ] 11.9 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 11.10 Independently execute T(test_session_workers_share_admission)+G; completion: contention/progress, replacement decision/namespace, expected crash settlement, final-count/cap/serial variants and P01 namespace isolation pass; full G provides ordinary multiworker regression evidence.

## 12. P12 — Invalid distributed setup refusal

Acceptance: missing/malformed configuration produces UsageError with the specified prefix and a nonzero enclosing session before protected work. Seam S; consumes P11.

### RED
- [ ] 12.1 Record UsageError/refusal and P11 harness contracts. Put controlled pytest_configure_node corruption in W/conftest.py with @pytest.hookimpl(trylast=True), after the real plugin's ordinary transfer hook. Establish successful uncorrupted setup first. Freeze missing/version/decision-field/capacity cases and external work observations; separately exercise nonabsolute namespace at the public provider boundary. Completion: mutations reach real worker validation and successful setup establishes prerequisites.
- [ ] 12.2 Create `test_invalid_session_configuration_fails_setup`; completion: controlled delivery exercises real setup/refusal.
- [ ] 12.3 Execute R(test_invalid_session_configuration_fails_setup); completion: missing refusal explains failure.

### GREEN
- [ ] 12.4 Implement setup refusal; completion: invalid delivery ends setup before provider/work.
- [ ] 12.5 Document recovery by fixing configuration and rerunning; completion: fenced docs/testing.md commands retain positive -n/worksteal and existing queue allocations.
- [ ] 12.6 Execute T(test_invalid_session_configuration_fails_setup)+G; completion: expected child failure asserted; outer/full checks exit 0/E.

### INTROSPECT
- [ ] 12.7 Record refusal/harness review; completion: failure identities preserved and payload schema internal.
- [ ] 12.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 12.9 Independently execute T(test_invalid_session_configuration_fails_setup)+G; completion: invalid setup stops before work and valid sessions pass.

## 13. P13 — Installation admission

Acceptance: installation acquires the explicit pool, including early worker-session setup. Direct and nested tests observe supported installation-caller integration; inventory review and existing application/full-suite tests establish lazy-fixture migration/regression evidence. Seams installation/A/S; consumes P11/P12.

### RED
- [ ] 13.1 Freeze design.md's R1–R8 inventory, current lines/deadlines/owner phases and direct/wiring dispositions. Prepare matching repository inputs via tests.mcp_contract_support.prepare_inputs before hold. Drive supported build_and_install_contract_wheel with explicit local K=1 recording-O pool and short positive timeout_seconds. Nested variant retains base inner command, requests admission and directly passes admission.shared_pool to that caller. Record input/handshake/observation bounds; review lazy-fixture explicit admission dependency/forwarding. Completion: prepared inputs, scoped evidence and prerequisites ready.
- [ ] 13.2 Create `test_worker_installation_uses_session_admission`; completion: isolated caller and nested real-provider variants positively observe contention/short timeout, then a separate successful call after release; include lifecycle companions. Timeout input is a duration converted by the caller to its original deadline.
- [ ] 13.3 Execute R(test_worker_installation_uses_session_admission); completion: missing shared-pool use explains failure.

### GREEN
- [ ] 13.4 Migrate installation configuration/acquisition and explicit lazy-fixture forwarding; completion: caller owns acquisition and isolated destinations retained.
- [ ] 13.4.1 Migrate C01–C04 transport guards from design.md in tests/test_mcp_transport.py: sole-owner faults, unsafe nodes, serialization/original timeout and setup deadline. Use capacity-one AdmissionPool/O for acquisition guards and the supported installation caller for the setup envelope. Completion: all original fault/security/time cases remain in the registered MCP suite, real owner paths and external O observations replace legacy/internal interception, and per-function dispositions include all 12 C01 point/fault pairs, and passing T+M(transport)+G evidence is recorded in E; VALIDATE additionally runs S and the applicable registered regression procedures. C05 shared-session identity remains owned by P19.
- [ ] 13.5 Document early-session installation and evidence scopes; completion: migrated/remaining callers distinguished.
- [ ] 13.6 Execute T(test_worker_installation_uses_session_admission)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 13.7 Record installation/owner review; completion: original budgets, attribution and explicit dependencies retained.
- [ ] 13.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 13.9 Independently execute T(test_worker_installation_uses_session_admission)+G; completion: scoped caller/provider observations, lazy-fixture inventory review and application regression evidence recorded distinctly.

## 14. P14 — Host preparation

Acceptance: prepared_root consumes the supplied pool across its existing copy/git/inventory work and original 120-second envelope. Seams prepared_root/A/S; consumes P13.

### RED
- [ ] 14.1 Freeze signature/deadline, complete prerequisites before hold, and record bounded ready/attempt/O-busy/entry/release observations; completion: isolated K=1, supported cross-caller and real-wiring variants specified.
- [ ] 14.2 Create `test_host_preparation_uses_session_admission`; completion: actual contention while held and valid prepared root after release within the safeguard; include lifecycle companions.
- [ ] 14.3 Execute R(test_host_preparation_uses_session_admission); completion: missing common-pool use explains failure.

### GREEN
- [ ] 14.4 Migrate preparation admission; completion: original envelope, admitted region and isolated roots preserved with required admission keyword.
- [ ] 14.4.1 Migrate the prepared_root half of C12 test_prepared_root_and_call_admission_charge_original_envelope in the registered host-build-tooling module. Completion: the original 120-second envelope, admission-held work and elapsed-time observation remain explicit through the pool/O seam; retain a working prepared-command half until P15, with both portions/dispositions and passing T+M(tooling)+G recorded in E; VALIDATE additionally runs S and applicable registered regression procedures.
- [ ] 14.5 Document preparation boundaries; completion: copy/git/inventory admission explicit.
- [ ] 14.6 Execute T(test_host_preparation_uses_session_admission)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 14.7 Record preparation review; completion: public outcome observations and original region retained.
- [ ] 14.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 14.9 Independently execute T(test_host_preparation_uses_session_admission)+G; completion: output/admission/prior phases pass.

## 15. P15 — Host wrapper and prepared execution

Acceptance: invoke R5 and promoted supported prepared_command R8 acquire the supplied pool with original deadlines/results. Consumes P14; seams supported callers/A/S.

### RED
- [ ] 15.1 Freeze R5/R8 contracts and every collector/helper/guard call in inventory, including tests/test_host_build_tooling.py:24. Promote _prepared_command to tests.host_acceptance_contract_support.prepared_command; record non-None pool, supplied absolute deadline and deadline-minus-now/result observations. Completion: supported import and external execution observations specified.
- [ ] 15.2 Create `test_host_wrapper_uses_session_admission`; completion: direct local K=1 recording-O, supported cross-caller and nested actual collector/provider wiring for both R5/R8; short supplied deadlines, original result/remainder and lifecycle companions.
- [ ] 15.3 Execute R(test_host_wrapper_uses_session_admission); completion: missing pool use explains failure.

### GREEN
- [ ] 15.4 Migrate both regions, promote/import supported prepared_command and update all calls/guards. R8 acquires inside the caller and returns wheel.command(timeout_seconds=deadline - time.monotonic()) result. Completion: original envelopes/arguments/results and two distinct live boundaries/evidence retained.
- [ ] 15.4.1 Complete C12's prepared-command half and migrate C13 test_wrapper_admission_release_preserves_primary_and_refuses_publication in the registered host-build-tooling module. Completion: supported prepared_command retains supplied deadline/remainder/results, invoke retains operation-close/sole-close/interruption precedence, independent/repeat release and publication refusal, observed via explicit pool/O and public outputs; per-function/portion dispositions and passing T+M(tooling)+G results are in E; VALIDATE additionally runs S and applicable registered regression procedures. C11 shared-session identity remains owned by P19.
- [ ] 15.5 Document execution admission and existing attribution-only cases; completion: existing bypass scope retained.
- [ ] 15.6 Execute T(test_host_wrapper_uses_session_admission)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 15.7 Record wrapper/deadline review; completion: original command allowance and sequential ownership retained.
- [ ] 15.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 15.9 Independently execute T(test_host_wrapper_uses_session_admission)+G; completion: both execution regions/prior phases pass.

## 16. P16 — Mandatory workspace envelope

Acceptance: provision_workspace requires pool admission, holds it across provision and passes the original remaining allowance. Functional dependencies P11 provider/P05 deadline; execution follows P15 VALIDATE. Seams workspace caller/A/O/S.

### RED
- [ ] 16.1 Resolve actual lazy wheel-base prerequisite and complete setup admission before hold; supply isolated K=1 recording-O pool explicitly. Freeze missing-pool error and original-envelope observations; include actual-wiring/cross-caller variants. Completion: valid prerequisites and intentional missing-admission case distinguished.
- [ ] 16.2 Create `test_workspace_preserves_admission_envelope`; completion: literal elapsed-time example, before-work refusal for missing pool and lifecycle companions.
- [ ] 16.3 Execute R(test_workspace_preserves_admission_envelope); completion: missing integration explains failure.

### GREEN
- [ ] 16.4 Migrate required keyword-only admission: AdmissionPool | None and actual provider forwarding; completion: borrowed deadline/owners preserved, None rejected before work, direct/wiring observations pass.
- [ ] 16.4.1 Migrate C07–C10 workspace guards in the registered host-workspace module: missing pool, original command remainder, operation/close/interruption/repeat/independent release, and real contention timeout followed by fresh acquisition. Completion: explicit admission=None establishes before-work refusal; capacity-one pool/O drives the real workspace caller, original deadline and launch allowance survive elapsed acquisition, original fault/result assertions remain, and per-function dispositions and passing T+M(workspace)+G results are in E; VALIDATE additionally runs S and applicable registered regression procedures. C06 collector coverage remains owned by P19.
- [ ] 16.5 Document mandatory pool/original allowance; completion: matches contract.
- [ ] 16.6 Execute T(test_workspace_preserves_admission_envelope)+G; completion: existing contention/deadline/failure assertions retained with capacity-one pools/E.

### INTROSPECT
- [ ] 16.7 Record workspace compatibility review; completion: production behavior and old guard strength retained.
- [ ] 16.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 16.9 Independently execute T(test_workspace_preserves_admission_envelope)+G; completion: mandatory admission/original remainder/prior phases pass.

## 17. P17 — Clone admission and required forwarding

Acceptance: reuse_for_test clone R3 consumes the supplied pool with original copy isolation. Introduce standalone_policy.admission and every caller's forwarding in this phase; preserve R7 legacy protection until P18 and R2 until P19. Consumes P16; seams supported callers/A/S.

### RED
- [ ] 17.1 Freeze both signatures' complete direct/indirect inventories, including standalone_policy's reuse call (tests/host_acceptance_contract_support.py:993) and its callers (tests/test_host_acceptance_contracts.py:2523 anchor). Record required same-pool forwarding and protected R2/R7 transitions. Fully resolve base/setup before isolated K=1 hold; record bounded O-contention/entry/release observations and actual-wiring/cross-caller variants. Completion: valid clone inputs and current/future region ownership explicit.
- [ ] 17.2 Create `test_clone_copy_uses_session_admission`; completion: supplied pool contention/progress, valid isolated clone and same-pool forwarding observations plus wiring/cross-caller/lifecycle companions.
- [ ] 17.3 Execute R(test_clone_copy_uses_session_admission); completion: missing common-pool behavior explains failure.

### GREEN
- [ ] 17.4 Migrate R3 and required reuse_for_test.admission at every call, including private-support consumers. Add standalone_policy.admission, update all callers/forwarders and pass the same pool to reuse_for_test. Completion: refreshed inventories, original budgets/isolation, protected R2/R7 and direct/wiring checks pass.
- [ ] 17.5 Document clone boundary, forwarding and transitional protections; completion: scope matches phase.
- [ ] 17.6 Execute T(test_clone_copy_uses_session_admission)+G; completion: exit 0/E.

### INTROSPECT
- [ ] 17.7 Record isolation/lifecycle/inventory review; completion: R3 supplied-pool use, standalone forwarding, R7 protection until P18, R2 protection until P19, independent mutable support preserved.
- [ ] 17.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 17.9 Independently execute T(test_clone_copy_uses_session_admission)+G; completion: clone/forwarding/isolation/prior phases pass.

## 18. P18 — Standalone policy provisioning

Acceptance: R7's separate uv pip install uses the forwarded pool and original local 120-second envelope after clone releases. Consumes P17 clone/signatures/forwarding; seams standalone_policy/external command/A/O/S.

### RED
- [ ] 18.1 Refresh standalone_policy call inventory and freeze R7 (:1002), deadline (:1001) and remainder (:1017). Record supported external-command/completion handshakes establishing clone release before the K=1 provisioning hold. Completion: base prepared, sequential operation and actual-wiring observations specified.
- [ ] 18.2 Create `test_policy_suite_provisioning_uses_admission`; completion: completed clone released, later provisioning contends then progresses after release with original checks; include direct/cross-caller/wiring and lifecycle companions.
- [ ] 18.3 Execute R(test_policy_suite_provisioning_uses_admission); completion: missing R7 pool/deadline behavior explains failure after successful prerequisites.

### GREEN
- [ ] 18.4 Replace R7's working legacy acquisition with admission.acquire(deadline=deadline) inside standalone_policy. Retain/re-audit P17 parameter/caller/reuse forwarding. Completion: clone releases before R7, original local deadline/remainder/results and distinct acquisition location retained, inventory refreshed.
- [ ] 18.5 Document separate policy-provisioning boundary; completion: eight-region/session descriptions include it distinctly.
- [ ] 18.6 Execute T(test_policy_suite_provisioning_uses_admission)+G; completion: exit 0/E with R7 evidence separate from clone evidence.

### INTROSPECT
- [ ] 18.7 Record chain/deadline/owner review; completion: separate sequential R3/R7 acquisitions with original checks/budgets.
- [ ] 18.8 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E.

### VALIDATE
- [ ] 18.9 Independently execute T(test_policy_suite_provisioning_uses_admission)+G and audit R7 evidence; completion: sequence/wiring/remainder/prior phases pass.

## 19. P19 — Private support and completed migration

Acceptance: private-support R2 uses supplied pool; all eight regions use the final acquisition API with existing boundaries/deadlines/results preserved. Consumes P17/P18/prior migrations; seams reuse caller/A/S.

### RED
- [ ] 19.1 Refresh reuse inventory and protected R2 transition. Resolve base/support inputs and complete setup admission before isolated K=1 hold; explicitly call reuse_for_test(..., admission=pool, private_support=True). Freeze result/ownership/removal criteria and bounded O-contention/entry/release protocol, including actual-wiring/cross-caller variants. Completion: valid prerequisites and positive observations specified under existing envelope.
- [ ] 19.2 Create `test_private_support_copy_uses_session_admission`; completion: contention while held, independent valid support output after release, actual-wiring/cross-caller observations and lifecycle companions.
- [ ] 19.3 Execute R(test_private_support_copy_uses_session_admission); completion: missing supplied-pool use explains failure.

### GREEN
- [ ] 19.4 Migrate private-support acquisition; completion: original copy isolation and intentional attribution-only cases retained.
- [ ] 19.4.1 Migrate C05/C06/C11 shared-path/identity guards in their registered modules to real provider/caller contention and release-progress behavior. C05/C11 run the P11-style isolated one-worker nested invocation with fresh B, real provider and recording/fault O; their outer module remains the registered verification entry point. Resolve inputs before a short real hold at inner B, then use controlled O monotonic observations to exercise timeout accounting and release for a separate progress control. An explicit context owner bounds the hold and releases it on failure/interruption. Record maximum real hold duration, handshake bounds, release/settlement observations and inner/outer statuses; only intended inner contenders use B, preserving unrelated callers' allowances. Replace collector-gated private-conftest invocation with supported fixture/caller coverage for all six heavy collectors and explicit non-heavy configurations. Complete removal of legacy admission wiring while preserving wheel-provider availability. Completion: all per-function cases and P13–P16 dispositions audited, registered failure/contention/interruption/repeat/independent-cleanup coverage retained, and T+M(transport)+M(workspace)+M(tooling)+G pass/E; VALIDATE additionally runs S and applicable registered regression procedures.
- [ ] 19.5 Complete R1–R8 live-location/deadline/direct+wiring inventory and record individual attribution-only cases in E. Remove legacy API/configuration and migrate identity guards to capacity-one/session behavior. Run `rg -n -F 'build_slot' tests host_tests scripts docs/testing.md`, `rg -n -F 'BUILD_SLOT_PATH' tests host_tests scripts docs/testing.md`, and `rg -n -F '_mcp_build_admission' tests/test_host_workspace.py`; each expected exit 1 establishes legacy-name absence. Review public replacement guards for missing admission/work coverage/contention/deadlines. Completion: eight positively accounted regions plus absence searches, with expected search statuses distinguished from test-runner results.
- [ ] 19.6 Update docs/testing.md single-file promise/final API docs; completion: six collectors/lazy setup/host/prepared/clones/support/workspace accurate; Phase-11 exclusive-results scheduling, priorities 1..13 and allocations retained; fenced commands use one positive decimal -n/worksteal with xdist active. Existing documentation/registration guards pass in G.
- [ ] 19.7 Execute T(test_private_support_copy_uses_session_admission)+G; completion: exit 0/E with final API migration complete.

### INTROSPECT
- [ ] 19.8 Review each region against code/direct+wiring evidence, every spec requirement and old guard; completion: all eight live regions, callers, original deadlines/checks and intentional bypass dispositions accounted for.
- [ ] 19.9 Apply justified internal refactoring or retain structure; T after edits, G afterward; completion: passing E and frozen public boundaries retained.

### VALIDATE
- [ ] 19.10 Independently execute T(test_private_support_copy_uses_session_admission)+G and audit all contracts/tests/evidence; completion: current acceptance and previous-phase applicability pass.
- [ ] 19.11 Repeat `GREEN_CHECKS_WORKERS=8 scripts/check-green` sequentially on unchanged final code on the baseline machine; completion: both final runs successful, statuses/counts/wall and final K/affinity/quota recorded. Comparison with the retained historical baseline is descriptive wall time and check counts only; CPU/capacity-controlled conclusions use a fresh baseline with matching recorded observations.
- [ ] 19.12 Deliver self-contained verification/performance summary outside openspec; completion: comparison conditions/limitations explicit, K>1 results investigated where improvement is absent, claims supported by measurements.

## Traceability

Each row identifies the literal N for R/T and its owning acceptance. Every introducing resource branch also carries common lifecycle companions. Each phase's final acceptance runs T+S+G and applicable M procedures; P19 also repeats full G.

| Outcome | Phase / seam | Literal test N |
| --- | --- | --- |
| Resource-free description; enter/release; distinct namespaces | P01 A/O | test_pool_admits_and_releases |
| Immutable input validation | P02 A/O | test_pool_rejects_invalid_configuration |
| Free slots; K bound; saturated waiting; release progress | P03 A/O | test_pool_uses_available_capacity |
| Original waiting deadline | P04 A/O | test_pool_wait_uses_original_deadline |
| Final-boundary expiry/release | P05 A/O | test_pool_checks_deadline_before_entry |
| Safe-node validation | P06 A/O | test_pool_rejects_unsafe_node |
| Workers/affinity bound | P07 C | test_capacity_respects_workers_and_affinity |
| Effective v2 quota | P08 C | test_capacity_respects_v2_quota |
| Effective v1 quota | P09 C | test_capacity_respects_v1_quota |
| Conservative diagnostics/failure precedence | P10 C | test_capacity_reports_conservative_fallback |
| Shared initial/replacement decision/namespace | P11 S | test_session_workers_share_admission |
| Invalid setup refusal | P12 S | test_invalid_session_configuration_fails_setup |
| R4 installation; provider-to-caller integration; lazy dependency review/regression | P13 caller/A/S | test_worker_installation_uses_session_admission |
| R6 host preparation | P14 caller/A/S | test_host_preparation_uses_session_admission |
| R5/R8 wrapper/prepared collector integration | P15 caller/A/S | test_host_wrapper_uses_session_admission |
| R1 mandatory workspace/original remainder | P16 caller/A/O/S | test_workspace_preserves_admission_envelope |
| R3 clone/standalone forwarding | P17 caller/A/S | test_clone_copy_uses_session_admission |
| R7 sequential policy provisioning | P18 caller/A/O/S | test_policy_suite_provisioning_uses_admission |
| R2 private support; complete eight-region migration | P19 caller/A/S | test_private_support_copy_uses_session_admission |

## Workflow

Implementation starts on a separate apply request. Execute the phases above, retain current-candidate evidence under `/tmp/gorshochek/ephemeral/footman.git/verification/`, and archive after current acceptance and full verification pass.
