# Tasks

## Binding execution contract

This checklist is a delivery contract, not a list of suggestions. Implement it at the root of this repository, which is the standalone dispatcher project, not a consumer repository. Read `proposal.md`, `design.md` and `specs/privileged-test-dispatch/spec.md` before implementation. Do not silently weaken requirements to make tests pass. If a contradiction or unsupported prerequisite prevents compliance, stop the affected phase, report it and obtain a planning decision; do not invent replacement behavior.

### Scope and required observable behavior

- Provide one host executable: `footman --config <config.toml>`, serving stdio MCP. This is the service process entry point for MCP clients, not a direct command-execution interface. Do not add `run` or `mcp` subcommands or a direct execution CLI. The only dispatch tool is `run_privileged_command` with `{}` or an object containing only optional string `phase`.
- Use operator-owned, versioned TOML. Load and validate the configuration once at server startup and keep it immutable for requests served by that process; TOML edits have no effect until restart, when the new file must be loaded and validated; no request may select configuration, command, user, container, image, timeout or arbitrary paths. Record the effective non-secret configuration identity for each run. Never discover executable configuration in the consumer repository.
- Require Python 3.14+; use standard-library `uuid.uuid7()` with no UUID compatibility implementation. After input validation call it once per accepted request; derive the fixed millisecond `utc_timestamp` and `{utc_datetime}` from that UUID's `time`, not a separate clock read. Encode its full 128-bit integer, most significant digit first, as exactly 26 lowercase Crockford Base32 digits using `0123456789abcdefghjkmnpqrstvwxyz`, with leading zeroes and no separators/check symbol. The first digit must be `0`–`7`; decoding must recover the same UUID. Use its returned time even if UUIDv7 monotonicity advances it beyond a stalled/backward clock.
- Exclusively reserve `<canonical snapshot_directory>/<run_id>` before capture, confined under the canonical root with no symlink traversal. An existing path or collision fails visibly without regeneration/renaming/reuse. Never clean up a path this request failed to reserve.
- Capture one staged Git tree without changing development index bytes, workdir files, HEAD or refs. Export only raw staged content to a fresh host directory without `.git`. Creating Git tree objects is permitted; creating commits/refs, applying patches and registering worktrees are prohibited.
- Reserve an exclusive result directory before snapshot capture or container launch. Launch a new dedicated container using the configured literal argv and validate its returned identity and snapshot mount. Execute exactly the configured argv as the configured user/cwd with closed stdin and no TTY.
- Retain complete command stdout/stderr separately from startup stdout/stderr. Retain an initial and then finalized `result.json`. Return an MCP summary containing `outcome`, nullable command `return_code` and absolute paths to every created retained file.
- Supervise started runs through completion even if the MCP client disconnects. Enforce startup/execution timeout and bounded cleanup. Never report aggregate success with incomplete output or unresolved resources.
- Do not infer test layout, add build/test flags, interpret test counts, provision project identities, build consumer images, claim hermeticity or claim repository readiness. Cargo, Gradle and npm are interchangeable configuration examples, not runtime dependencies. Staging is a mistake guard, not approval to execute hostile code as root.

### Existing foundation and mandatory adapter ownership

The scaffold and lifecycle primitives already exist (foundation port `905e8be`); do not recreate them or claim their passing suites complete dispatcher phases. Follow design decision 6 and `docs/filesystem-lifecycle.md` / `docs/process-lifecycle.md`. Use explicit filesystem submodule imports, directory capabilities, immediate raw-fd adoption, sole stream owners, `closefd=False` wrappers and `CleanupFailures` for independent releases. Managed Git/Docker children and test helpers use `OwnedProcess.launch`; never implement parallel reader, close/reap/signal or shutdown mechanisms. Keep Docker/Git/publication/recursive-deletion policy in domain adapters.

For every new acquisition, wrapper, transfer or cleanup branch, add operation-plus-close, sole-close, interruption and repeat-cleanup tests, plus independent release, handoff and concurrency where applicable. Run them in the phase introducing the branch, not only phase 6 or 9. Retain the same owners through incomplete cleanup and observe them without retry or renewed budgets. Existing helper suites remain mandatory regression coverage.

### Required test matrices

Each matrix is normative. A task referencing a matrix must cover every listed case, with named assertions. Do not replace real-container checks with mocks. Use mocks/fault injection for destructive or hard-to-reproduce failures; real-container tests must additionally verify actual Docker semantics.

**C — Configuration and inputs:** missing config file causes a nonzero process exit, empty stdout and a nonempty stderr diagnostic identifying the configuration-load failure (not a generic not-implemented message), without requiring a specific nonzero code or OS error wording; one-time startup loading/static validation; invalid startup configuration exits nonzero before serving requests, reports its validation error on stderr and creates no run output/snapshot/container/command side effects; per-request checks still reject reservation collisions, disappeared/replaced paths and mismatched actual mounts as runtime failures without reloading configuration; post-startup TOML edits do not change any request's configuration or effective identity; restart loads/validates the new file and refuses startup if invalid; version 1; unsupported/missing versions; unknown fields; wrong types; empty launch/command arrays or executable; relative paths resolved against configuration location, not caller cwd; default user `root`; positive bounded timeout/grace with documented numerical limits; relative working directory normalized below the configured in-container mount destination; absolute container mount destination; repository/snapshot/output root separation including canonical ancestors and symlinks; launch template requires `{snapshot_path}` and rejects unknown placeholders; output template accepts only optional `{utc_datetime}`/`{phase}` and rejects `{run_id}`; static and phase-only templates remain valid but collide while their previously expanded destination exists (document this in the example). Phase is a nonempty `[A-Za-z0-9_-]+` string, with no enumerated values; reject empty, dots, separators, non-string and extra fields. Require phase when its placeholder is used. Otherwise accept absence, and record a supplied phase even if unused by the template. Invalid requests cause no output reservation, snapshot capture or Docker call. A phase never changes argv.

**S — Snapshot:** run-ID-derived path confined under canonical snapshot root; exclusive reservation; existing directory/symlink and injected same-ID concurrent collision fail without renaming, ID regeneration, reuse or cleanup of the winner; partial staging; staged addition/change/deletion; unstaged, untracked and ignored exclusion; no `.git`; `100644`/`100755` exact-byte regular files with executable-mode preservation; `040000` tree entries become directories without inventing source directory permission metadata; `120000` blobs become actual symlinks with exact target bytes, not regular files; reject unsupported modes; binary bytes; internal symlink-chain containment and rejection of absolute/outward targets, cycles or unprovable containment; never follow exported links while writing; spaces, newlines and other unusual names preserved or explicitly rejected; escaping symlinks/unsafe paths/gitlinks rejected; configured checkout filters/hooks never invoked; `export-ignore`/`export-subst` never transform export; ordinary and linked worktrees; unresolved index rejection; split-index support or explicit unsupported-format failure; inherited Git repository/index redirection sanitized; atomic index replacement during capture; later staging/workdir changes cannot change the captured input. Concurrent staging by a fixture is not a dispatcher mutation: compare dispatcher-induced changes separately from deliberately injected fixture changes.

**O — Output:** one standard-library UUIDv7 per accepted request; canonical 26-digit lowercase Crockford Base32 round-trip of its 128 bits, leading-zero and first-digit bounds, version/variant/time preserved; fixed `utc_timestamp` with millisecond precision and filesystem-safe `{utc_datetime}` derived from the same `UUID.time`, including millisecond-boundary and stalled/backward-clock cases; no second clock read or UUID regeneration; run ID retained as metadata and snapshot identity, not an output placeholder; one-time nonrecursive template expansion; exclusive run-directory/file creation with restrictive permissions; existing directory/symlink and simultaneous identical expanded-path rejection without overwrite or automatic renaming; static and repeated-phase destinations fail as `output_failed` while prior directories remain; shared parents permitted; inaccessible/full storage and write/finalization failure; absolute returned paths; byte-for-byte separate large simultaneous command streams; warnings preserved; startup streams separate; infrastructure diagnostics never appended to command streams. Retain `stdout.txt`, `stderr.txt`, `startup-stdout.txt`, `startup-stderr.txt` and `result.json` for a normally completed run. On early/storage failure return paths only for files actually created, never assert unavailable evidence is complete.

**L — Launch and execution:** literal argv with spaces/metacharacters and no shell/word splitting/recursive expansion; reject Docker mount paths that its syntax cannot represent; startup success requires exit zero and exactly 64 hexadecimal container-ID characters with at most one trailing newline; reject missing/short/malformed/multiple IDs and extra text; nonzero startup forbids execution even with an ID; inspect returned ID, running state, image identity and exact mount; mismatch/stopped/missing container prevents execution; missing user/executable/cwd is unsuccessful; command uses configured user and normalized in-container cwd `{container_snapshot_directory}/{working_directory}` (`/workspace/subdir`, or `/workspace` for `.`); host snapshot path is never substituted into exec argv; exact argv, closed stdin and no `-i`/`-t`; include an image executable and a snapshot-local wrapper. Image readiness/keep-alive and host bind-mount visibility are operator prerequisites, not inferred checks or provisioning steps.

**R — Results and lifecycle:** command exits 0 and nonzero; command exits 0 with zero tests still means command success only; startup/execution deadline; signal/interruption; local Docker-client death while descendants survive; known-ID container removal and confirmation; failed removal; snapshot retained until removal confirmed; root-owned snapshot files; symlink-safe deletion confined to the successfully reserved snapshot; a reservation loser never deletes the winner's directory; unrelated containers untouched; startup loses valid attributable ID (report potentially unresolved resources, no guessed cleanup or startup retry); concurrent independent requests; client disconnect does not abandon or duplicate the run; initial manifest survives interruption; atomic finalization failure cannot report success.

**A — Adapter ownership and persistence:** apply the mandatory branch fault matrix above to output reservation/files, config reads, private index, raw export, Git/Docker children, manifest publication and snapshot deletion. Verify no borrowed-pipe release or double ownership; every independent release attempted once; operation authority preserved over ordinary close failure; interruption preserved; repeat/concurrent cleanup once-only; helper failure identity/kind preserved and rebased to ACTION/None; one collection per child; local child exit distinct from container removal and helper settlement; shared execution deadline; one total cleanup budget including helper cleanup and local reserve; incomplete owners retained through read-only observation. Inject writer callback errors that the coordinator treats as diagnostic-only, EOF/read failure/abort, flush/close failure and code-zero conflicts; the domain sink must independently prevent success and report incomplete output. Include helper `done` with `cleanup_complete` false and exhausted-budget observations without renewed work.

### Result contract and failure rules

- MCP result fields are `outcome`, `return_code`, `stdoutPath`, `stderrPath`, `startupStdoutPath`, `startupStderrPath` and `resultPath`. Include a path field only when that file was created, and use an absolute host path. A normal retained run includes all five path fields. Explain absent/incomplete files in structured failure diagnostics; do not return fabricated paths as retained evidence.
- `return_code` is the configured command's observed exit code, not startup/Docker-client/server status. Use null when no command exit code is available. Preserve signal and infrastructure status separately. Do not invent a command exit code from a transport failure.
- `outcome` distinguishes `succeeded`, `command_failed`, `dispatch_failed`, `output_failed`, `timed_out` and `interrupted`. Keep command status and cleanup diagnostics separate so, for example, code 0 plus unresolved cleanup is not `succeeded`. Document deterministic outcome precedence before implementing outcome aggregation; timeout/interruption/output/cleanup failures must not be masked by code 0. Use an unsuccessful MCP tool result for every unsuccessful aggregate outcome.
- `result.json` is the durable manifest, not replaced by an MCP response. It records schema version, run ID, fixed UTC timestamp, optional phase, output template/expanded path, tree ID/object format, optional contextual HEAD, non-secret configuration identity, exact startup/command/Docker argv, startup status/paths, cwd, user, container/image identity, lifecycle timestamps, command exit/signal, outcome, output paths, cleanup state and warnings/limitations. Unknown identity/status values remain explicitly unknown; do not infer them from HEAD or a successful launcher exit.
- Runtime ordering is input validation → one UUIDv7 and derived fixed timestamp/run ID → exclusive output reservation/initial manifest → exclusive run-ID snapshot reservation → snapshot capture/export → launch/inspection → command execution → bounded cleanup → manifest finalization → MCP response. Failure skips unsafe downstream actions but still performs applicable cleanup and reports available evidence.

### Phase gates, verification and evidence

- Every phase executes RED → GREEN → INTROSPECT → VALIDATE in that order. Do not start GREEN until every RED task in that phase is complete. Do not start INTROSPECT until GREEN is complete, or VALIDATE until INTROSPECT is complete. A later phase may start only after every task in its dependencies is checked complete.
- RED completion requires executing the specified new tests and recording failures attributable to missing required behavior. Syntax errors, missing tools and broken fixtures are not acceptable RED evidence. A passing new test must be strengthened to demonstrate the missing behavior, or its already implemented behavior must be documented with an explicit negative control that fails the assertion; do not deliberately break working production code.
- Every delivered GREEN/INTROSPECT/VALIDATE candidate must run `scripts/check-green` from the repository root without scope/fix arguments, following `docs/testing.md` and `docs/python-checks.md`. Attempt every independent check and preserve the first failing status. Also run mandated build/help and applicable exact-wheel installation checks; phase suites do not replace the full gate or real Docker/MCP acceptance. Documented RED remains exempt, with exact output/status and missing-behavior attribution.
- GREEN completion requires the named tests to pass with the implementation, without weakening tests. Internal dispatch entry points used by tests are not a public direct-execution CLI.
- INTROSPECT produces a written checklist of the named review items and file/test references. Correct discovered defects before passing the gate. Add a regression test first for each defect and rerun the affected checks. Unresolved contract defects block the phase.
- VALIDATE runs the phase suite and all earlier phase suites without exclusions. This requires preserving behavior coverage, not preserving assertions tied to a temporary scaffold: phase 2 explicitly replaces the not-implemented startup test with permanent missing-file coverage before validation. Do not skip, xfail or filter the obsolete test; replace it as specified and keep the replacement in every later regression run. A task is complete only when its stated assertion or artifact can be checked independently. Do not mark a phase complete based solely on a narrative claim.
- Phase 1 must publish literal verification commands for the selected language/toolchain in the standalone project's testing documentation. For each later phase, register its named suite there before recording RED. Subsequent instructions to run a suite mean run that documented command, not an improvised filtered subset.
- Capture full stdout/stderr temporarily and preserve runner exit statuses. Every nonzero status is unsuccessful verification (except the explicitly expected, recorded RED failure). Warnings are retained and explained, never filtered away. Missing Docker/runtime/image prerequisites block real-container validation; skipped tests are not substitutes for passing required checks.
- Store self-contained phase summaries, verification reports and handoffs outside Git under `/tmp/gorshochek/ephemeral/footman.git/`, with verification summaries in `verification/`. This storage is ephemeral; permanent retention is not required. Each summary identifies checked revision, local changes, exact commands, relevant runtime/Git/Docker/OS/image environment, exit statuses, test counts, significant warnings and limitations. Do not require committing summaries or full logs. All phase evidence and completion criteria below require self-contained, currently applicable summaries, not durable storage. Missing or stale reports cannot substantiate current completion claims: rerun the affected checks and recreate their summaries before relying on those claims. Do not invent historical RED results; reproduce the documented negative control if its evidence must be recreated. Code/test/configuration changes invalidate affected earlier results and require fresh checks; edits limited to evidence or staging do not. Ephemeral phase-report storage does not alter the runtime retention contract for `result.json` and command logs.
- Final acceptance requires every checkbox complete, every matrix case mapped to a passing test, all required real-container checks executed, and no unresolved contract violation. Historical RED evidence is not current verification.

### Phase dependency DAG

The explicit edges are: `1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9`. Each phase depends on its immediate predecessor and transitively on all earlier phases. There are no other prerequisites between phases, no forward edges and no cycles. Sequential dependencies are intentional: output retention exists before any launch can fail, and lifecycle cleanup exists before the public MCP adapter can dispatch.

## 1. Phase 1 — Standalone foundation

**Depends on:** none.
**Deliverables:** installable executable scaffold, pinned runtime/SDK dependencies, isolated test harness, testing documentation, foundation evidence.

### RED
- [ ] 1.1 Review and extend installation smoke coverage for the existing executable and lifecycle modules. Completion: missing executable/module behavior is detected by a documented isolated negative control; preserve existing working code and record actual RED output/status rather than claiming this repository is empty.
- [ ] 1.2 Write a harness-isolation test using a disposable Git repository. Completion: its negative control detects an intentional fixture mutation without touching the developer repository.

### GREEN
- [ ] 1.3 Integrate the existing executable scaffold and lifecycle foundation with pinned dependencies. Completion: the installation smoke test passes for the exact built wheel in a fresh environment outside the checkout with PYTHONPATH unset, importing all explicit filesystem modules and footman.process and checking both executable/module help entry points; do not reimplement owners.
- [ ] 1.4 Implement disposable Git/Docker test fixtures using the shared owners and recorded resource identities. Completion: isolation and matrix A fault tests pass; fixture cleanup operates only on recorded paths/IDs, with no independent raw-child control or duplicate release authority.
- [ ] 1.5 Publish testing prerequisites and literal suite commands for the chosen toolchain. Completion: another invocation from a clean checkout can execute the foundation command exactly as documented.

### INTROSPECT
- [ ] 1.6 Audit the foundation boundary. Completion: a review record confirms no consumer-specific paths, direct dispatch subcommands, automatic image building or tests targeting the developer index.

### VALIDATE
- [ ] 1.7 Validate the foundation installation. Completion: the documented foundation suite passes in a fresh environment and its self-contained, currently applicable verification summary includes runtime/dependency versions and exit status.

## 2. Phase 2 — Operator configuration and request validation

**Depends on:** phase 1.
**Deliverables:** strict TOML loader, immutable effective configuration, input validator, annotated example, configuration suite and evidence.

### RED
- [ ] 2.1 Write table-driven configuration tests for matrix C's configuration cases. Completion: tests fail on absent validation, including canonical-path overlap, configured cleanup-grace validation and placeholder rejection; static/phase-only templates are accepted. Loader-level cases may call internal functions; startup refusal is independently tested by 2.4.
- [ ] 2.2 Write tool-input validation tests for matrix C's phase/extra-field cases. Completion: tests fail on absent validation and count zero downstream side-effect calls for each invalid input.
- [ ] 2.3 Write configuration-lifetime tests. Completion: tests fail if a later request observes post-startup TOML edits, if restart ignores new valid configuration, or if invalid configuration permits startup.
- [ ] 2.4 Write a process-level invalid-configuration startup test. Completion: executing `footman --config <invalid>.toml` with a statically invalid field in an existing file fails the test unless the process exits nonzero, reports the validation error on stderr, serves no requests and creates no run output/snapshot/container/command side effects; the scaffold's generic not-implemented error must not satisfy this test. Keep this test separate from missing-file coverage.
- [ ] 2.5 Replace `test_scaffold_fails_explicitly_without_protocol_output` with a permanent missing-config-file process test. Completion: use a guaranteed absent path under `tmp_path`; assert nonzero exit, empty stdout and nonempty stderr identifying a configuration-load failure, with no exact exit-code or full-message pin. The current generic not-implemented stub must make this replacement RED. Record that failure; do not count the old passing stub-message assertion as configuration coverage.

### GREEN
- [ ] 2.6 Implement strict TOML loading, parsing and canonical configuration validation. Completion: the configuration table and both process-level missing-file/invalid-existing-TOML tests pass with actual load/validation diagnostics, including explicit numerical timeout/grace bounds and root-default assertions; the generic not-implemented failure satisfies neither test.
- [ ] 2.7 Implement phase-only request validation. Completion: the tool-input table passes with no output/snapshot/Docker calls for rejected requests.
- [ ] 2.8 Implement process-lifetime configuration immutability. Completion: configuration-lifetime tests pass with one load/validation at startup, unchanged configuration/identity after TOML edits, new configuration after restart and startup rejection for invalid files.
- [ ] 2.9 Publish the annotated TOML example. Completion: the loader accepts a fixture-instantiated copy and the text explains relative paths, operator ownership, required image keep-alive, Docker bind visibility, static-path and reused-phase collisions, and recommends `{utc_datetime}` for repeat use without promising collision-free execution.

### INTROSPECT
- [ ] 2.10 Audit configuration authority. Completion: a review record references tests proving no consumer config auto-discovery, request override, shell evaluation or phase-driven command selection.

### VALIDATE
- [ ] 2.11 Validate configuration delivery. Completion: documented configuration and all earlier suites pass without exclusions, including the permanent missing-file replacement and separate invalid-existing-TOML test; counts/statuses are recorded in a self-contained, currently applicable verification summary.

## 3. Phase 3 — Exclusive output reservation

**Depends on:** phase 2.
**Deliverables:** UUIDv7/Crockford request identity and shared millisecond timestamp, output path expansion/reservation, fixed output handles, reservation documentation, reservation suite and evidence.

### RED
- [ ] 3.1 Write request-identity tests for matrix O's UUIDv7/Crockford/time cases. Completion: absent identity logic fails round-trip, alphabet/width/leading-zero, version/variant, single-generation and exact timestamp-equality assertions, including supplied UUID times at millisecond boundaries and stalled/backward-clock cases.
- [ ] 3.2 Write reservation tests for matrix O's path, collision and permission cases. Completion: absent reservation logic causes failures, including exactly one winner for concurrent identical paths and failure of repeated static/phase-only destinations.
- [ ] 3.3 Write pre-execution storage-failure tests. Completion: injected directory/file creation failure fails the tests unless downstream snapshot/launch calls remain zero.

### GREEN
- [ ] 3.4 Implement request identity using one standard-library `uuid.uuid7()` call and Crockford serialization. Completion: identity tests pass with timestamps derived only from that UUID's `time`, with no fallback implementation, second clock read or ID regeneration.
- [ ] 3.5 Implement single-pass output-template expansion. Completion: timestamp/phase/path tests pass using the fixed request identity without recursive substitution or collision renaming.
- [ ] 3.6 Implement exclusive run-directory and fixed-file reservation through directory capabilities and sole file owners. Completion: collision, symlink, permissions, creation-failure and matrix A ownership tests pass without overwriting preexisting bytes; all acquired resources remain protected on partial setup failure.
- [ ] 3.7 Document output reservation and retention policy. Completion: the document names all five files, shared UUIDv7/millisecond identity, supported placeholders, static/phase-only collision behavior, sensitive/large output and manual retention without automatic evidence deletion.

### INTROSPECT
- [ ] 3.8 Audit request identity and filesystem containment. Completion: the review records UUID/time/encoding provenance, canonical-ancestor checks, no-follow operations and failure paths, with no independent timestamp or path that overwrites existing evidence or escapes configured storage.

### VALIDATE
- [ ] 3.9 Validate reservation delivery. Completion: documented reservation and all earlier suites pass, with UUID/time, race-test and storage-failure results in a self-contained, currently applicable verification summary.

## 4. Phase 4 — Immutable staged snapshot export

**Depends on:** phase 3.
**Deliverables:** exclusive run-ID snapshot reservation, private-index capture, mode-aware raw export, snapshot limitations documentation, snapshot suite and evidence.

### RED
- [ ] 4.1 Write snapshot-reservation tests for matrix S's path/ownership cases. Completion: absent reservation fails confinement and exclusive-creation assertions, including a forced same-ID concurrent collision proving one owner and no loser-induced modification/deletion.
- [ ] 4.2 Write capture tests for matrix S's index/worktree/concurrency cases. Completion: absent private capture fails assertions on captured identity and development-state preservation.
- [ ] 4.3 Write export tests for matrix S's bytes/modes/path/filter cases. Completion: absent raw export fails exact-byte, actual symlink-type/target and prohibited-hook/filter invocation assertions for the entry modes defined by matrix S.

### GREEN
- [ ] 4.4 Implement exclusive run-ID snapshot reservation. Completion: reservation tests pass for canonical-root confinement, existing-path/symlink rejection and same-ID collision without regeneration, renaming or touching the winner's path.
- [ ] 4.5 Implement private-index tree capture with sanitized Git redirection after successful snapshot reservation, using shared file owners and OwnedProcess for managed Git children. Completion: capture tests pass for ordinary/linked worktrees, unresolved entries, split-index policy, concurrent replacement and matrix A operation/cleanup faults.
- [ ] 4.6 Implement raw tree/blob export using directory capabilities, sole file owners and process streaming without following exported symlinks. Completion: export tests pass for mode-specific creation, exact bytes/targets and explicit rejection of gitlinks, unsupported modes, unsafe paths/chains; matrix A covers sink errors, partial export and independent resource release.
- [ ] 4.7 Document snapshot identity and limitations. Completion: documentation states exclusive run-ID path ownership/collision failure, mode-specific export, staged-only inputs, no `.git`, no ignored artifacts, split-index policy and no commit/ref/index/workdir mutation.

### INTROSPECT
- [ ] 4.8 Audit Git and export side effects. Completion: the review confirms `write-tree` targets only the private index, no consumer code runs on the host, snapshot writes cannot traverse an exported symlink and failed reservations grant no cleanup ownership.

### VALIDATE
- [ ] 4.9 Validate snapshot delivery. Completion: documented snapshot and all earlier suites pass with collision ownership, before/after development-state assertions and captured tree identity in a self-contained, currently applicable verification summary.

## 5. Phase 5 — Dedicated container launch and exact execution

**Depends on:** phase 4.
**Deliverables:** launch parser/inspection, exact exec adapter, real-container execution tests, operator launch documentation and evidence.

Local process/pipe ownership through `OwnedProcess` is mandatory already in phases 4–5; phase 6 adds Docker domain supervision, not basic safe child acquisition. Startup/execution share a request deadline, and local cleanup allocations must fit the single run cleanup allowance.

Tests in this phase must use fixture-owned fallback cleanup for containers, because production lifecycle supervision is delivered in phase 6. The fallback must record IDs, never scan for arbitrary containers to remove, and must not be used as proof of production cleanup.

### RED
- [ ] 5.1 Write launcher contract tests for matrix L's startup and mount cases. Completion: absent validation fails tests and invalid launches have zero configured-command invocations.
- [ ] 5.2 Write exact-execution tests for matrix L's command cases. Completion: absent exec adaptation fails assertions for literal argv, in-container mount-relative cwd (not the host path), user, closed stdin and no TTY.

### GREEN
- [ ] 5.3 Implement literal snapshot-path substitution and startup contract parsing. Completion: launcher tests pass for valid output and reject every invalid stdout/status case without command execution.
- [ ] 5.4 Implement exact-ID container inspection. Completion: inspection tests pass for running state, image identity and canonical snapshot mount, rejecting each mismatch before exec.
- [ ] 5.5 Implement configured container execution through OwnedProcess.launch, also using it for managed launch/inspection children. Completion: exact-execution and matrix A process-integration tests pass for image executables and snapshot wrappers, unavailable user/executable/cwd and nonzero exit; no borrowed-pipe release, duplicate collection or custom readers/reaping.
- [ ] 5.6 Document the launch/execution contract. Completion: instructions show dedicated `docker run` outputting only its ID, operator-prepared keep-alive/readiness, mount-syntax limitations and interchangeable command examples without adding implicit flags.

### INTROSPECT
- [ ] 5.7 Audit execution authority. Completion: a review record confirms no shared-container reuse, source-directory mount, host execution of exported code, shell interpolation or request-provided Docker arguments.

### VALIDATE
- [ ] 5.8 Validate real mounted-snapshot execution. Completion: a real-container test observes root identity, exact arguments and staged source content, with image identity and command status retained in evidence.
- [ ] 5.9 Validate launcher regression. Completion: documented launcher and all earlier suites pass, with invalid-output/mount cases recorded and all fixture containers reconciled.

## 6. Phase 6 — Supervised lifecycle and safe cleanup

**Depends on:** phase 5.
**Deliverables:** bounded supervisor, recorded-ID removal/confirmation, snapshot cleanup, reconciliation documentation, lifecycle suite and evidence.

A local Docker process exiting is not evidence that the container stopped. A launch with no valid attributable ID is not permission to guess an ID. Root-owned files are not permission to delete a parent directory or follow symlinks.

### RED
- [ ] 6.1 Write supervision tests for matrix R's deadlines/interruption/concurrency cases. Completion: absent supervision fails bounded-time and independent-run assertions.
- [ ] 6.2 Write cleanup tests for matrix R's known-ID/unknown-ID and snapshot cases. Completion: absent cleanup fails ordered-removal assertions and tests detect any deletion of an unrelated sentinel container/path.

### GREEN
- [ ] 6.3 Implement one shared startup/inspection/execution deadline and one separate total cleanup allowance using launch-selected StopPolicy/StopStage caps and a local-cleanup reserve. Completion: supervision and matrix A budget tests pass within documented tolerances; helpers consume remaining allowance, not fresh grace, and local-client termination does not prove container removal.
- [ ] 6.4 Implement recorded-ID removal/confirmation as a domain ManagedAction with nonblocking protocol boundaries and separately owned default-policy helper children. Completion: normal/failure/timeout tests and matrix A verify failure identity/kind handoff to ACTION/None, done versus cleanup_complete, retained owners/continuation and bounded cleanup_status/wait_cleanup observations without renewed work; unknown-ID startup reports unresolved resources without guessed removal or retry.
- [ ] 6.5 Implement confined no-follow snapshot cleanup using directory capabilities and sole owners. Completion: root-owned/symlink and matrix A branch-fault tests either safely delete the owned snapshot after confirmed container removal or retain/report it without touching sentinels; owner validation/permission failures do not justify weakened confinement.
- [ ] 6.6 Document incomplete-run reconciliation. Completion: instructions use recorded run/container/snapshot identities, explain unconfirmed resources and prohibit broad container deletion or blind retries.

### INTROSPECT
- [ ] 6.7 Audit terminal-path ownership. Completion: a path-by-path review accounts for success, startup failure, execution failure, timeout, interruption and removal failure without premature snapshot deletion or abandoned known containers.

### VALIDATE
- [ ] 6.8 Validate real-container cleanup semantics. Completion: real tests confirm removal after success/failure/timeout, surviving-child handling and preservation of an unrelated sentinel container.
- [ ] 6.9 Validate lifecycle regression. Completion: documented lifecycle and all earlier suites pass and a self-contained, currently applicable verification summary identifies any intentionally retained fault-injection resources and their reconciliation.

## 7. Phase 7 — Complete output and durable results

**Depends on:** phase 6.
**Deliverables:** lossless stream retention, initial/final manifests, outcome aggregation, result documentation, result suite and evidence.

### RED
- [ ] 7.1 Write stream-retention tests for matrix O's byte/large-output/failure cases. Completion: absent draining/retention fails byte equality or bounded-completion assertions without hiding warnings.
- [ ] 7.2 Write manifest tests for the complete result contract. Completion: missing required fields, nonexistent returned evidence paths or non-atomic finalization cause explicit failures.
- [ ] 7.3 Write outcome tests for matrix R's status cases. Completion: tests reject aggregate success for timeout, interruption, storage failure or unresolved cleanup even when command status is 0.

### GREEN
- [ ] 7.4 Implement lossless separate sinks over OwnedProcess.stream with explicit domain persistence status. Completion: simultaneous large output and matrix A tests cover diagnostic-only callback exceptions, write/flush/close failure and EOF/read-failed/aborted streams; code zero cannot mask incomplete retention, no unbounded capture or startup/diagnostic contamination is allowed.
- [ ] 7.5 Implement initial and atomic final result.json persistence with a domain-owned same-directory temporary-file/publication protocol. Completion: manifest and matrix A tests pass for readable interrupted initial state, operation-plus-close, sole-close, interruption/repeat cleanup and unsuccessful failed finalization; no temporary fd/stream loses its sole owner.
- [ ] 7.6 Document deterministic outcome precedence. Completion: a decision table covers every outcome, signal/unknown status and conflicting failure case, preserves cleanup detail and assigns no successful outcome to unresolved runs.
- [ ] 7.7 Implement result aggregation from lifecycle observations. Completion: outcome tests pass against the documented decision table without inventing command return codes or interpreting test output.
- [ ] 7.8 Document result fields and evidence limitations. Completion: examples show code 0, nonzero, null code, retained initial manifest and incomplete storage without claiming workdir testing or verification sufficiency.

### INTROSPECT
- [ ] 7.9 Audit evidence integrity. Completion: the review confirms complete-field attribution, secret-safe configuration identity, status provenance and no path listed as retained evidence before file creation.

### VALIDATE
- [ ] 7.10 Validate result delivery. Completion: documented result and all earlier suites pass with large-stream byte counts, finalization failures and status-table results in a self-contained, currently applicable verification summary.

## 8. Phase 8 — MCP protocol and response contract

**Depends on:** phase 7.
**Deliverables:** public stdio server/tool, structured response adapter, disconnect supervision, client setup documentation, protocol suite and evidence.

### RED
- [ ] 8.1 Write real-client protocol tests for server initialization/tool discovery. Completion: absent MCP registration fails discovery of `run_privileged_command` with the exact phase-only schema.
- [ ] 8.2 Write real-client response tests for the result contract. Completion: absent response adaptation fails outcome/code/absolute-path assertions for success, command failure and pre-command dispatch failure.
- [ ] 8.3 Write real-client disconnect/concurrent-request tests. Completion: absent run supervision fails assertions for retained completion and exactly one launch per accepted request.

### GREEN
- [ ] 8.4 Expose the stdio MCP tool through the startup executable. Completion: discovery/input tests pass with protocol-only stdout, operator-controlled config and no direct dispatch subcommand.
- [ ] 8.5 Map durable run observations into structured MCP responses. Completion: response tests pass with all five paths on normal runs, null unavailable command code and unsuccessful tool results on aggregate failure.
- [ ] 8.6 Preserve supervision independently of client connection lifetime. Completion: disconnect/concurrency tests pass without duplicate retries, abandoned accepted runs or mixed run outputs.
- [ ] 8.7 Publish real-client setup instructions. Completion: documented startup uses only `footman --config <config.toml>` and examples demonstrate `{}`, valid phase, required phase rejection and reading files by absolute paths. Instructions must show that statically invalid startup configuration exits nonzero with stderr diagnostics before serving requests or creating run resources, distinguish later request-level filesystem/reservation/mount failures, and explain that TOML edits require restart.

### INTROSPECT
- [ ] 8.8 Audit MCP capability boundaries. Completion: a review record proves no Docker socket/general host-command interface is exposed and rejected inputs never reach output reservation or Docker.

### VALIDATE
- [ ] 8.9 Validate MCP delivery. Completion: documented protocol and all earlier suites pass using a real MCP client, with clean protocol stdout and retained disconnect outcomes in a self-contained, currently applicable verification summary.

## 9. Phase 9 — Integrated acceptance and portability

**Depends on:** phase 8.
**Deliverables:** integrated acceptance tests, requirement-to-test coverage map, fresh-location verification and final acceptance evidence.

This phase verifies integration; it must not be the first phase to test configuration, snapshots, launch, cleanup, retention or MCP behavior. Do not defer missing earlier tests/documentation to this phase.

### RED
- [ ] 9.1 Write an integrated staged-fixture acceptance test. Completion: a negative control changing expected tree/argv/path/status causes an assertion failure; the test covers real MCP → real Docker → retained manifest flow and phase/no-phase requests.
- [ ] 9.2 Write a fresh-location portability acceptance test. Completion: a negative control introducing a developer-specific path is detected by installation/example execution outside the original checkout path.

### GREEN
- [ ] 9.3 Close integration defects exposed by the staged-fixture test. Completion: the test passes with exact staged identity despite later fixture changes, complete logs, expected statuses and no dispatcher-induced development-state mutation.
- [ ] 9.4 Close portability defects exposed by relocation. Completion: installation and the annotated example pass at a fresh location without consumer-specific prerequisites or source-project absolute paths.

### INTROSPECT
- [ ] 9.5 Produce the acceptance coverage map. Completion: every spec requirement/scenario and each C/S/O/L/R/A matrix case links to a named executed test and its current result; no required case is unmapped or replaced with a skip.
- [ ] 9.6 Audit the complete phase graph and delivered scope. Completion: the review confirms all phases have RED/GREEN/INTROSPECT/VALIDATE evidence, dependencies point only backward and the implementation contains no out-of-scope public capability.

### VALIDATE
- [ ] 9.7 Run the complete documented regression and real-container acceptance commands. Completion: every command exits zero, required tests execute without exclusions and all owned fixture resources are reconciled.
- [ ] 9.8 Publish final acceptance evidence. Completion: a self-contained, currently applicable summary in the configured ephemeral verification directory records revision/local changes, exact commands, environment/image, statuses/counts, warnings, limitations and coverage-map location, clearly separating historical RED from current passing checks; missing or stale summaries require re-verification before relying on completion claims.

## Workflow follow-up

- This repository is already the standalone dispatcher project and contains the change's planning artifacts. Implement at its root; no copy or migration to another project is required.
- Have the operator prepare the image and configuration. The dispatcher neither builds that image nor provisions consumer applications.
- After implementation, use the applicable apply/archive workflow only on explicit request. Archiving is not an implementation task or a substitute for acceptance verification.
