# Design

## Context and scope

The current test support uses one secure `build_slot` flock and one basetemp-derived session path. Six collectors, lazy worker installation and host helpers use it. The migration preserves existing heavy-work boundaries, deadlines, unsafe-node rejection and sole-owner cleanup while allowing bounded concurrency.

Implementation follows tasks.md's vertical RED → GREEN → INTROSPECT → VALIDATE phases. Each phase delivers one observable behavior through a supported boundary. Production runners, filesystem foundation, uv configuration, worker allocations and exclusive-results scheduling retain their existing contracts. Scheduling within the admission pool is implementation-defined.

## 1. Immutable pool API

Export the following interfaces from tests/admission_support.py:

```python
AdmissionPool(*, directory: Path, capacity: int,
              system: AdmissionSystem | None = None)
AdmissionPool.acquire(*, deadline: int | float) -> ContextManager[None]
```

Construction creates an immutable configuration description; OS work occurs during acquisition. Live descriptors belong to individual acquisitions. The selected system adapter may maintain controlled mutable time.

Capacity is a positive int, with bool classified as an invalid type. Wrong type raises TypeError; nonpositive capacity raises ValueError. Deadline is finite absolute monotonic time expressed as int or float, with bool classified as an invalid type. Wrong type raises TypeError; NaN/infinity raises ValueError. Directory existence and safety are checked securely at acquisition. Successful entry yields None; exhaustion raises TimeoutError before protected work. Error-message wording is implementation-defined.

P01's initial K=1 tracer includes arbitrary-directory namespace isolation: hold A, enter B while A remains held, then release and reacquire A. P03 adds bounded free-slot admission, saturated waiting and release progress as one behavior: two overlapping K=2 holders, positive contention for caller K+1, entry after release with at most K holders, and a K=1 control.

Pool slot filenames differ from legacy mcp-build.lock and retain stable inode identities while active. The legacy implementation continues protecting unmigrated callers during the transition. During P13–P18 these are separate exclusion domains: K bounds pool holders, while legacy holders can overlap them. Aggregate admitted work can therefore exceed K and the resolved CPU budget. Each intermediate phase's GREEN/INTROSPECT/VALIDATE evidence identifies active pool/legacy regions, their separate bounds and the aggregate-concurrency limitation. P19's completed migration establishes the session-wide K bound across all regions. Final migration removes build_slot, BUILD_SLOT_PATH, _wheel_build_slot_path, module-global admission patches and the tests.conftest import/__wrapped__ identity guard for _mcp_build_admission. Supported caller/fixture behavior tests carry their coverage forward.

## 2. Operating-system adapter

```python
AdmissionSystem.descriptors: DescriptorOps
AdmissionSystem.try_lock_exclusive(fd: int) -> bool
AdmissionSystem.monotonic() -> float
AdmissionSystem.sleep(seconds: float) -> None
```

The descriptors property returns one stable DescriptorOps instance from footman.filesystem.operations. Locking borrows the fd; True denotes acquisition and False denotes contention. Other errors propagate. The clock is finite/nondecreasing. Sleep accepts finite nonnegative intervals and may return early or be interrupted; acquisition rechecks time afterward. Slot selection, deadlines, retries and cleanup belong to AdmissionPool. The adapter has resource-free lifecycle semantics; the pool borrows it.

PosixAdmissionSystem delegates to PosixDescriptorOps, nonblocking exclusive flock, time.monotonic and time.sleep. Recording/fault adapters delegate to real OS operations and record external acquisitions/releases. Tests observe returned failures, context behavior, public acquisition progress and external close attempts. Filesystem owners remain active throughout fault injection.

P06's foreign-UID variant uses a narrowly scoped DescriptorOps.fstat observation: after a real fstat of a real regular slot fd, change only st_uid to a value unequal to effective UID. Preserve actual ownership/fd/release, all other stat fields and precision, and all directory metadata. Select the observation by its real regular-file type. DirectoryDescriptor receives its original directory observations and succeeds; admission's slot-owner validation consumes the foreign-UID observation. Adapter trace establishes that this prerequisite was supplied; failure to establish it is a prerequisite error.

## 3. Free-slot search and deadline accounting

Try available slots nonblockingly. Each request holds at most one slot, and the configured K bounds simultaneous holders. Busy probes are released once before further scanning. Search order and polling strategy are implementation details. Only contention permits another probe; security, open, metadata and release failures propagate.

Waiting, scans and retries consume the original absolute deadline. Check that deadline before protected entry, including after obtaining a lock. On late acquisition, release the lock and raise TimeoutError. Maintain active slot-file identities throughout use so flock exclusion remains attached to the same inode. Progress after release is required; ordering among competing callers is implementation-defined.

## 4. Resource ownership

DirectoryDescriptor owns directory descriptors; adopt each acquired raw fd immediately into OwnedDescriptor, preparing constructor inputs first. Use explicit footman.filesystem submodules. Each descriptor has one release authority. Attempt every independent release exactly once. Preserve an active operation failure/interruption over ordinary close errors and retain secondary cleanup evidence. A sole close failure propagates. Repeated cleanup observes the original settled/continuing state.

Test subprocesses use OwnedProcess.launch and its lifecycle contract. Each phase introducing an acquisition, wrapper, transfer or cleanup branch includes operation-plus-close, sole-close, interruption and repeat-cleanup cases, plus independent release/handoff/concurrency where applicable. Fault injection uses the external-system adapter while the real owners execute their release paths.

## 5. Capacity resolver

```python
resolve_admission_capacity(
    *, workers: int, system: CapacitySystem | None = None,
) -> CapacityDecision
CapacitySystem.affinity() -> frozenset[int]
CapacitySystem.read_text(path: Path) -> str
```

CapacityDecision is immutable with positive capacity: int and nonempty diagnostic reason: str; diagnostic wording is implementation-defined. Workers is a positive int, with bool classified as an invalid type. Wrong type raises TypeError; nonpositive values raise ValueError.

CapacitySystem is separate from AdmissionSystem and defaults to real OS observations. Tests supply raw affinity/file observations. The resolver discovers membership, mounts and ancestor paths and parses the text. read_text completes owner cleanup before returning text.

Final capacity is max(1, floor(min(workers, affinity CPU count, effective quota))). Resolve applicable Linux cgroup v1/v2 membership, mount roots and ancestor restrictions. Known unlimited quota contributes an unbounded quota term. Unknown/inaccessible/malformed discovery yields capacity one with a reason. Nondistributed execution supplies workers=1.

CapacityObservationUnavailable(Exception) represents expected missing/inaccessible observations or unsupported affinity, retaining the cause. This exception and explicitly recognized invalid observations (malformed text, empty affinity, unresolved hierarchy) permit conservative fallback. Release failures, KeyboardInterrupt, SystemExit and unexpected errors propagate. Convert expected observation failures only after successful owner cleanup, outside owner contexts and with cleanup evidence checked through the foundation's supported contract. Operation-plus-close preserves the primary operation and secondary release evidence as failure; sole-close failure propagates.

P07 delivers a provisional worker/affinity-only calculation: max(1, min(workers, len(affinity))) for valid affinity. Its workers=8/affinity=6 example yields 6 under that intermediate contract. P08 introduces real v2 discovery/parsing and unlimited-quota assertions; P09 adds v1 equivalents; P10 completes fallback classification. Each quota-owning phase extends the existing worker/affinity test with raw valid-unlimited fixtures parsed by its real discovery. P11 integrates the completed resolver with session admission.

## 6. Session configuration and provider

For distributed same-host/shared-filesystem runs, public pytest_xdist_setupnodes(config, specs) receives the final planned local popen specs after numeric/auto/logical resolution and --maxprocesses. Use len(specs) as workers, validate the positive count and supported topology, resolve capacity once, and save the decision before pytest_configure_node transfers it. Initial and replacement nodes receive the same version/decision/reason. Serial/-n 0 execution initializes one local decision with workers=1 before admission fixture use.

Unsupported remote/chdir topologies and invalid/unresolved distributed counts raise pytest.UsageError before protected work. Worker configuration validates the delivered decision before tests. Missing payload, unsupported version, missing/invalid decision fields or nonpositive/bool capacity raise pytest.UsageError with prefix 'invalid admission configuration:' and a reason. The enclosing session exits nonzero; exact xdist status is implementation-dependent. This failure ends setup before provider creation or protected work.

The session-scoped admission fixture returns immutable SessionAdmission, exported from tests/admission_support.py. Its read-only shared_pool: AdmissionPool is a stable description per worker, referring to the invocation's shared absolute directory/capacity. SessionAdmission has resource-free lifecycle semantics. Callers acquire through admission.shared_pool.

Namespace derives from the injected public tmp_path_factory.getbasetemp(): serial uses that path; local workers and replacements use its parent. The delivered payload contains the decision; namespace is derived at fixture setup. A nonabsolute derived namespace raises the same UsageError before provider creation/work. Acquisition validates filesystem node safety. Distinct invocations use distinct basetemps; each explicit basetemp has one active invocation. The payload schema remains an internal implementation detail.

### Session plugin registration and inner invocation

Put admission fixture/controller-worker hooks in importable tests/admission_plugin.py. Repository conftest registers it through pytest_plugins; isolated nested sessions load it explicitly. tests/admission_support.py remains the pool/provider API.

Generate inner tests, isolated ini I and scenario-only W/conftest.py in a fresh workspace W outside repository tests/, host_tests/ and scripts/. Launch from repository-root cwd using the absolute repository .venv Python. Early -p imports use repository-root cwd; ini pythonpath sets the absolute repository root for subsequent imports. Scenario hooks and handshakes load through normal W/conftest.py discovery under --confcutdir W. The scenario conftest contains scenario hooks and supported fixture overrides; tests.admission_plugin supplies the single admission implementation/registration.

Inner command:

`<absolute .venv Python> -m pytest -c I --rootdir W --confcutdir W --basetemp B -p xdist -p tests.green_skip_policy -p tests.admission_plugin W/test_session.py -n 1 --dist worksteal --tb=short -q`

I clears addopts/testpaths. Record W, I, fresh absolute B and every exact child command/status. Replacement variants add their recorded restart allowance. P11 option-input variants use the worker modes/caps defined below. Registered verification commands retain positive literal worker counts and worksteal.

Construct env = wheel.clean_environment(); remove PYTEST_ADDOPTS, PYTEST_PLUGINS and PYTHONSAFEPATH, then set PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'. Retain executable paths and use ordinary python -m pytest invocation. clean_environment strips PYTHON* after merging. A successful setup/ready control establishes imports, plugin registration, fixture availability and basetemp initialization. Import/collection/setup faults are prerequisite failures; RED requires the specified missing behavior.

### Session verification scenarios

The deterministic contention variant has one inner worker, hence K=1 under the real resolver. After pytest initializes B and a worker signals provider-ready, pause the caller. The outer harness constructs AdmissionPool(directory=B, capacity=1), acquires a hold, and permits the inner attempt. Observe worker TimeoutError while held, then a separate successful acquisition after release with its own deadline. Contention/progress establishes common lock identity. Handshakes and positive OS observations establish ordering.

P11 also terminates an initial worker outside acquisition and observes replacement TimeoutError under the same B hold, then post-release entry. Record restart allowance, expected crash identity/status and settled owned helpers. This variant uses the same generic configure-node transfer contract.

Worker-count variants cover literal positive -n, auto, logical, capped auto/logical and intentional serial/no-distribution inputs. W/conftest.py implements public pytest_xdist_auto_num_workers returning three; --maxprocesses=2 yields two observed initial worker-ready events. The real capacity resolver remains active and may conservatively yield one. Compare provider behavior with the observed planned-worker bound. Record these commands as scenario inputs.

P12's controller-side pytest_configure_node corruption hook lives in W/conftest.py and uses @pytest.hookimpl(trylast=True). The real admission plugin uses ordinary hook ordering, so corruption runs after its transfer and remains visible to worker validation. Start with a successful uncorrupted control; then exercise missing/version/decision-field/capacity refusal through real worker setup. Separately check nonabsolute namespace refusal at the public provider boundary.

Harness mechanics use explicit owners, bounded waits, OwnedProcess launches and lifecycle tests. Public API/seam changes require agreement before tests; internal channel mechanics are implementation choices. Existing application tests/full G provide ordinary multiworker regression evidence.

## 7. Caller migration and deterministic integration

Preserve existing admitted regions, results, deadlines and attribution-only branches. Each acquiring boundary takes a required keyword-only admission parameter:

```python
build_and_install_contract_wheel(repository_root, *, admission: AdmissionPool | None,
    evidence_directory, timeout_seconds=120, attribution_only=False, env=None)
prepared_root(tmp, *, admission: AdmissionPool)
invoke(root, name, args, *, admission: AdmissionPool, cwd, evidence,
    env=None, label="operation", deadline=None)
provision_workspace(workspace, wheel, wheel_sha256, *,
    admission: AdmissionPool | None, interpreter)
reuse_for_test(base, destination, *, admission: AdmissionPool,
    mutable=False, private_support=False)
standalone_policy(base, tmp, case, *, admission: AdmissionPool)
prepared_command(argv, *, admission: AdmissionPool, cwd, env, deadline)
```

Installation accepts explicit None for its existing attribution_only=True branch. Workspace rejects explicit None before work. Heavy installation paths and other acquiring callers require a pool. bind/scripted_binding forward the same pool to invoke; acquisition belongs to the acquiring caller. Sequential helpers complete/release their own acquisitions before later admitted work.

P15 promotes tests/test_host_build_tooling.py::_prepared_command to supported tests.host_acceptance_contract_support.prepared_command and migrates collector calls/guards to that import. R8 acquires inside this boundary with the supplied absolute deadline, invokes wheel.command(timeout_seconds=deadline - time.monotonic()) and returns the original result.

Each signature-changing phase updates every direct/indirect call and forwarding chain together. Freeze file:line/count inventories before tests, inspecting imports, aliases and dynamic paths as well as grep results. Include mcp_contract_support world/provider/selection/swap/fault paths, mcp_attribution_support, build_failure_support and h.prepared_root/h.invoke aliases where affected. Refresh dispositions in GREEN/INTROSPECT/VALIDATE evidence.

P17 adds admission to reuse_for_test and all callers, migrates R3, and retains existing R2 protection until P19. It also adds standalone_policy's required admission parameter, updates all its callers/forwarders and passes the same pool to its preceding reuse_for_test call (source anchor tests/host_acceptance_contract_support.py:993; caller anchor tests/test_host_acceptance_contracts.py:2523). R7 retains existing legacy protection until P18. P17 review accounts for migrated R3 plus protected R2/R7. P18 migrates R7's acquisition while preserving the P17 signatures/forwarding. P19 migrates R2 and removes residual legacy admission wiring after the complete audit.

Direct tests in P13–P19 construct a per-test AdmissionPool(directory=<unique tmp_path namespace>, capacity=1, system=<recording/fault O>) and pass admission=pool. This gives focused T and full G the same deterministic local capacity. Resolve actual fixture prerequisites and release their setup admission before holding this pool. Cross-caller variants give the same pool to two already-supported callers and observe real contention, release and progress.

Nested wiring variants use the real session plugin and application fixtures/adapters. A supported scenario admission override requests the original fixture, preserves directory/capacity, constructs a recording-O pool and returns SessionAdmission(shared_pool=pool). Real callers and capacity resolution remain active. The external B holder and positive O contention/progress establish provider-to-caller wiring.

P13's nested scope is specifically real-provider-to-supported-installation-caller integration. Keep the base inner command, request admission, prepare inputs via tests.mcp_contract_support.prepare_inputs, then call build_and_install_contract_wheel(..., admission=admission.shared_pool, timeout_seconds=<short positive duration>). Both isolated and nested variants use prepared repository inputs. Dedicated lazy-fixture dependency review records its explicit admission dependency/forwarding; existing application tests/full G provide lazy-setup regression evidence. The nested observation establishes provider-to-caller integration only.

Tests/test_admission_pool.py explicitly requests admission and, for already-built-base prerequisites in P16–P19, the existing lazy worker-wheel fixture _mcp_worker_wheel. Fixture availability is maintained independently of collector-specific admission patches; heavy provisioning is selected by explicit dependencies. Existing callers retain working transitional setup until migrated. Public fixtures/callers carry the behavioral coverage.

### Eight-region inventory

Freeze current locations in P13 RED; the following are pre-migration anchors. Record each live acquisition, preserved deadline and direct/wiring evidence in the final inventory.

| ID | Existing region | Deadline source | Phase |
| --- | --- | --- | --- |
| R1 | tests/mcp_wheel_support.py:69 workspace provision | workspace.deadline, borrowed unchanged | P16 |
| R2 | tests/mcp_wheel_support.py:295 private-support copy | reuse_for_test end = monotonic() + 120 | P19 |
| R3 | tests/mcp_wheel_support.py:307 installation clone | same reuse_for_test end | P17 |
| R4 | tests/mcp_wheel_support.py:533 build/install | end = monotonic() + timeout_seconds | P13 |
| R5 | tests/host_acceptance_contract_support.py:66 wrapper | supplied deadline or monotonic() + 120 | P15 |
| R6 | tests/host_acceptance_contract_support.py:91 prepared root | monotonic() + 120 | P14 |
| R7 | tests/host_acceptance_contract_support.py:1002 standalone policy uv install | monotonic() + 120 at :1001; remainder at :1017 | P18 |
| R8 | tests/test_host_build_tooling.py:24 prepared by-path leg | supplied absolute deadline; remainder at :26 | P15 |

R3 completes and releases before R7. R7 acquires inside standalone_policy with its original local envelope and result checks. Observable external-command/completion handshakes establish clone completion before the harness targets R7. R8 retains collector call-site/wiring evidence at the supported prepared_command boundary. Record intentional attribution-only cases individually. Final acceptance requires all eight live boundaries, followed by legacy-name absence checks.

### Observation ordering

Resolve inputs and already-built bases, completing their admitted setup, before taking the test hold. P13 uses prepared repository inputs as installation is the observed operation. Pair ready/attempt handshakes and external-work entry markers with positive busy-lock observations through O, then release/progress inside a recorded bounded observation window. A safeguard timeout is a test failure.

Positive TimeoutError observations use existing short caller-supplied budgets: installation timeout_seconds is a duration; invoke accepts an absolute deadline. A subsequent control is a separate operation with its own budget. prepared_root/reuse_for_test retain their existing envelopes and use positive contention plus release/progress observations. Changes needed to expose a required observation through a public seam require agreement before tests.

### Legacy contract migration inventory

The following 13 existing contract functions remain represented in the registered MCP suite as their API dependencies migrate. Anchors identify pre-migration source; refresh them before work. Each owner records a per-function disposition in E: original cases/assertions, retained or replacement function/cases, pool/caller equivalent, removed dependency and exact focused/full verification statuses. For a split contract, record each portion and its handoff separately. C01's per-case disposition enumerates every combination in {metadata, flock, body} × {operation-close, sole-close, interruption, repeat}: 12 cases, including protected-body failure and repeat-cleanup observations.

| ID | Existing contract function and source anchor | Migration owner and equivalent |
| --- | --- | --- |
| C01 | tests/test_mcp_transport.py:716 test_build_admission_sole_owner_faults (legacy call :784) | P13: capacity-one pool/O with all 12 {metadata, flock, body} × {operation-close, sole-close, interruption, repeat} cases; preserve primary identity, secondary release evidence, independent directory release, repeat-cleanup observations and fresh acquisition progress. |
| C02 | tests/test_mcp_transport.py:809 test_build_admission_rejects_unsafe_nodes (import :813) | P13: pool security guard using P06's supported unsafe-node observations; preserve symlink/directory/FIFO refusal before entry. |
| C03 | tests/test_mcp_transport.py:827 test_build_admission_serializes_and_preserves_original_timeout (call :860) | P13: two callers of one capacity-one pool, positive real busy observation, controlled O time; preserve mutual exclusion, original timeout, holder release and elapsed-time assertions. |
| C04 | tests/test_mcp_transport.py:886 test_build_admission_does_not_renew_setup_deadline (patch :906) | P13: supported build_and_install_contract_wheel with explicit pool/O; observe original absolute deadline and remaining build/command allowance after controlled admission delay through supported external work observations. |
| C05 | tests/test_mcp_transport.py:917 test_real_builds_use_invocation_shared_admission (identity :922) | P19: real admission provider plus installation/caller contention and post-release progress in an isolated nested invocation's fresh B namespace, through the bounded guard procedure below. |
| C06 | tests/test_host_workspace.py:88 test_shared_admission_covers_actual_provisioning_collectors (private wiring :89–97) | P19: supported fixture/caller variants cover the six existing heavy collectors and existing non-heavy configurations through explicit dependencies; positive shared-pool work/contention for heavy paths and preserved lightweight behavior for non-heavy paths. |
| C07 | tests/test_host_workspace.py:100 test_workspace_admission_cannot_silently_bypass_shared_slot (patch :103) | P16: explicit admission=None at provision_workspace raises before acquisition/provision. |
| C08 | tests/test_host_workspace.py:113 test_workspace_admission_wait_charges_original_command_deadline (path :123) | P16: real capacity-one pool/O consumes seven seconds of original workspace envelope; preserve deadline 220 and command/cleanup allowance total 113 from initial time 100, plus primary launch failure and settled resources. |
| C09 | tests/test_host_workspace.py:151 test_workspace_admission_owner_faults_and_independent_release (path :167) | P16: explicit pool/O with success, operation-close, sole-close and interruption-close cases; preserve returned result, primary identity, one provisioning call, independent releases and repeat-cleanup observations. |
| C10 | tests/test_host_workspace.py:213 test_workspace_admission_contention_exhausts_without_provisioning (path :218) | P16: isolated capacity-one real hold and O time; timeout precedes provision, original deadline retained, post-release independent acquisition succeeds. |
| C11 | tests/test_host_build_tooling.py:452 test_host_and_mcp_use_one_nonvacuous_admission_path | P19: host and MCP supported callers use the real provider in an isolated nested invocation with fresh B; bounded positive contention/release/progress establishes shared admission. |
| C12 | tests/test_host_build_tooling.py:457 test_prepared_root_and_call_admission_charge_original_envelope (path :465) | P14 owns prepared_root portion; P15 owns promoted prepared_command portion. Preserve original deadline observations 130 and 33, time spent while admitted, held-work ordering and remaining command allowance 17 via explicit pool/O and supported external work observations. P14 retains the working legacy prepared-command portion until P15. |
| C13 | tests/test_host_build_tooling.py:497 test_wrapper_admission_release_preserves_primary_and_refuses_publication (path :517) | P15: invoke with explicit pool/O; preserve operation-close, sole-close and interruption-close failure identity, independent directory/slot release, repeat cleanup and publication refusal. |

Tests/conftest.py:27–96 provides the legacy path, session installation and collector admission wiring consumed by these guards. P13 replaces installation's path dependency with explicit admission; P14/P15/P16 update their respective caller chains; P19 completes identity/collector guard migration and removes residual admission globals while preserving wheel-provider availability. The guards execute real owners with fault injection at O and supported work/output boundaries. Existing registered-module coverage and the new pool suite complement each other. Final acceptance audits C01–C13 dispositions alongside R1–R8 before legacy-name absence searches.

### Bounded session-namespace guards

C05/C11 execute through their existing registered outer modules and launch P11-style isolated one-worker sessions with fresh B. The inner real provider selects its normal namespace/capacity; its supported recording/fault O override drives deadline accounting through controlled monotonic observations. Resolve prerequisites before the hold. The outer harness's explicit AdmissionPool(directory=B, capacity=1) context holds only inner B, with a short recorded maximum wall duration and bounded ready/attempt/release handshakes. Failure/interruption exits that context and settles OwnedProcess helpers. Positive timeout observations use controlled O time; a separate post-release call proves progress. Only intended inner contenders share B, keeping unrelated outer-worker callers' original allowances intact. E records the hold/handshake bounds, actual release/settlement observations and exact inner/outer statuses.

## 8. Verification and migration

Execute tasks.md in topological order. Freeze each public behavior/seam contract at meaningful RED. New scaffolding explicitly reports unimplemented behavior; existing behavior continues operating. Lifecycle companions belong to the same resource contract; independent behaviors receive separately agreed phases.

INTROSPECT permits justified internal refactoring with the frozen contract preserved. Every delivered GREEN/INTROSPECT/VALIDATE candidate passes the exact full scripts/check-green gate, with zero skips/xfails/xpasses. Every VALIDATE additionally runs the full registered tests/test_admission_pool.py command S from tasks.md, accumulating delivered phases. C-migration phases run tasks.md's exact registered-module M commands alongside their focused T and G in GREEN/INTROSPECT, and independently with T+S+G in VALIDATE; subsequent validation includes earlier required registered specialised commands. Complete stdout/stderr and actual statuses/counts/warnings are recorded for S and M. RED records the actual nonzero status and complete output as missing-behavior evidence. Contract corrections reopen RED by agreement.

Before P01 RED, register exactly one fenced `.venv/bin/python -m pytest tests/test_admission_pool.py -n 1 --dist worksteal --tb=short -q` command in docs/testing.md. Preserve the canonical full-suite command's single occurrence and queue rows 1..13 with existing allocations; the suite registration is a prerequisite documentation task. S is a mandatory per-phase validation command with its own evidence; its obligation is independent of the queue-row allocation pinned by test_host_interfaces_are_registered_before_red.

Store baseline and current-candidate verification reports under `/tmp/gorshochek/ephemeral/footman.git/verification/`, following docs/testing.md's machine-local evidence policy. Capture revision/local changes, exact commands, environment, statuses/counts, warnings and limitations. The retained repository file docs/verification/session-build-admission-planning-baseline.md serves the same historical-only role as docs/testing-worksteal.md. Comparisons to that historical run cover descriptive wall time and check counts; its missing affinity/quota/K observations limit CPU/performance attribution. Controlled capacity comparisons require a fresh pre-change baseline with raw CPU observations, legacy one-slot admission identified and comparable execution conditions. Record final K/affinity/quota independently. Final verification repeats the gate on the unchanged candidate and reports measured results.

Risks and mitigations: repeated gates require execution time; external-system fault injection preserves owner observability; cgroup reads require lifecycle coverage; original deadlines and conservative capacity bound polling/I/O effects; eight-region and call-site inventories preserve migration coverage; existing exclusive-results scheduling remains authoritative for its workload. Rollback, when needed, is a separately authorized coherent integration change.
