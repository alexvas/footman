# Testing

## Green gate (G)

Run the whole gate from the repository root with `scripts/check-green`. It runs
the Ruff lint, Ruff format, syntax-compilation and type checks recorded in
[python-checks.md](python-checks.md), then the mandated full-suite parallel
pytest command below. It attempts every independent check, keeps the first
failing status, fails on incomplete process cleanup, and accepts no scope or fix
arguments. `GREEN_CHECKS_WORKERS` changes only the pytest worker count (an
integer from 1 to 1024); it never narrows the suite or the other checks.
`tests/test_green_gate.py` pins that plan, the worker range and the preserved
statuses.

A successful full pytest run requires no skips, xfails or xpasses, including
collection and fixture-setup skips. `tests/green_skip_policy.py`, registered in
`tests/conftest.py`, reports each violation and makes an otherwise successful
session exit nonzero. There are currently no approved skip exceptions. Missing
prerequisites must be resolved, not hidden by skipping tests. Existing failures
or interruptions retain their nonzero exit status. `tests/test_green_skip_policy.py`
checks the policy through isolated pytest subprocesses under both the serial and
the parallel invocation, so a worker cannot hide a skip from the controller.

G applies to a delivered GREEN/INTROSPECT/VALIDATE candidate state: the full-suite,
build, help and applicable installation checks below must succeed. A phase
suite's documented RED recording is exempt from G, not an unsuccessful attempt
to obtain G. Register the exact suite command before RED, capture complete
stdout/stderr and the actual nonzero exit status, and identify failures caused
by missing required behavior (not broken fixtures or missing prerequisites).
Do not weaken requirements or treat historical RED evidence as current passing
verification. Never skip/xfail tests, disable the plugin, filter required tests
or suppress warnings to obtain G. A green helper suite does not imply dispatcher
phase completion.

## Working schedule: gate plus specialised suites

When validation requires both G and the delivered phase suites and the machine
has **at least 32 available logical CPUs**, the working schedule is **budgeted
parallel execution**, not gate followed by every suite. Count CPUs available to
the process, respecting affinity and container CPU quotas; a host-wide CPU count
alone is insufficient. Use this schedule only if both the available logical CPU
count and any effective CPU quota provide at least 32 CPUs. If capacity is below
32 or cannot be established, run the gate and then the required specialised
commands sequentially by default. Keep the registered per-command worker counts
in either mode. Run from the repository root against one unchanged candidate:

- Start the unscoped gate with its own eight-worker allocation. Set
  `GREEN_CHECKS_WORKERS=8` explicitly if the inherited environment might override
  the default; do not add scope or fix arguments to `scripts/check-green`.
- Independently schedule the required specialised commands below with a **shared
  budget of eight workers**. Use each command exactly as registered, including
  its worker count and `--dist worksteal`; do not combine or deduplicate commands.
- Scan the following queue in order. Start a command when its entire worker
  allocation fits the remaining specialised budget; a later, smaller command
  may start while an earlier command waits for capacity. Return its allocation
  only after the command and its owned cleanup have settled.

| Queue priority | Registered suite | Workers |
| --- | --- | ---: |
| 1 | Phase 7 results | 1 |
| 2 | Phase 4 snapshot | 1 |
| 3 | Phase 5 launcher, including split scenario modules | 6 |
| 4 | Phase 6 lifecycle, including split scenario modules | 4 |
| 5 | Phase 8 owned stdin (all four registered modules) | 2 |
| 6 | Phase 1 foundation | 3 |
| 7 | Phase 3 output reservation | 1 |
| 8 | Phase 2 configuration | 2 |
| 9 | Standalone scaffold regression registered under Phase 2 | 1 |
| 10 | Phase 9 MCP transport and server (both registered modules) | 2 |
| 11 | Phase 10 integrated acceptance and portability | 2 |
| 12 | Phase 11 Docker-free host-runner contracts (`tests/test_host_acceptance_contracts.py`) | 2 |
| 13 | Standalone Linux proc-observer regression (`tests/test_proc_observer.py`) | 2 |

**Phase-11 real-base serialization exception.** The registered Phase-11 command
and the unscoped gate both collect
`test_orchestrator_unpatched_default_direct_and_loaded_legs_reconcile_exact_runs`.
That contract uses the same literal results base across pytest sessions, so these
invocations must not overlap. Run the gate through full process/resource
settlement before starting the registered Phase-11 command (queue priority 12),
even with at least 32 available CPUs. Other invocations collecting that contract
and operator host runs must also have exclusive access to the real base during
this leg. Do not infer isolation from distinct run IDs: another session's
resources are deliberately unattributed to this session's ledger. If exclusive
scheduling cannot otherwise be assured, use the sequential fallback for the
whole verification schedule. This rule changes neither registered commands nor
worker counts; it adds no filter, skip, locking CLI option or results override.
Unknown concurrent changes still fail verification without deletion.

Validation runs every registered specialised command required by the delivered
phase, including each earlier phase suite. Phase-11 registration established
twelve commands; the separately registered observer repair adds priority 13,
bringing the current queue to thirteen commands without renumbering earlier
entries. Current observer-repair verification requires its standalone command
alongside the unchanged Phase-10 command; it does not supersede the historical
Phase-10 registration or attribute observer coverage to earlier results.
Historical Phase-10 validation covers eleven commands
(through queue priority 11), without a dependency on the later Phase-11 suite.
The historical Phase-9 validation set contains ten commands (through queue
priority 10), without a dependency on the later Phase-10 suite.
Thus results, snapshot and launcher can initially run alongside the
gate: specialised allocation 1 + 1 + 6, plus the gate's separate 8. The limit
of 16 is a count of top-level xdist workers, **not** a limit on all OS processes:
pytest controllers, check tools and test-owned child processes are additional.
For narrower phase work, schedule only suites required by that phase; this does
not waive any earlier-suite or full-gate requirement. Add future delivered suites
to this scheduling policy when their commands are registered.

Keep independent complete stdout/stderr logs, command/start/end observations
and actual exit statuses for every invocation. Attempt every independent check,
retain the first observed failing status as well as all individual statuses,
and wait for every launched command and owned cleanup before declaring the
combined run complete. An unsettled cleanup is a failure, not free capacity for
a replacement invocation. Do not stop the other checks on a test failure, hide
warnings or skips, or treat one successful branch as success of the other.
Build/help/applicable exact-wheel installation checks remain separately required;
this schedule does not replace them or the historical RED recording rules.
Use the existing process/filesystem ownership contracts in any orchestration
helper. The queue is a scheduling policy, not a new committed runner; do not
depend on a machine-local ephemeral benchmark script to run project checks.

The policy was selected from three rotated measurements per mode on a Linux
development machine with 32 available logical CPUs: median combined wall time
was 111.67 s for gate then sequential suites, 85.27 s for gate plus a sequential
suite branch, and 28.44 s for this budgeted schedule. All commands passed. These
are historical **loadfile** performance observations, predating worksteal
adoption, not current verification or a guarantee for worksteal/different I/O
loads. The admission budgets are retained; future timings must name the scheduler
actually used. The automatic working-policy choice is limited to the
32-or-more-available-CPU condition above; lower or unknown capacity uses the
sequential fallback, not an unmeasured concurrent default. Do not silently change
registered commands or drop required checks. Concurrent invocations deliberately
repeat coverage and must keep their fixture-owned mutable state isolated.

## Current development prerequisites

Prerequisites: Python 3.14+ and `uv`; package-index access for the first install.
Run from the repository root:

```sh
uv sync --extra dev --locked
.venv/bin/python -m pytest tests -n 8 --dist worksteal --tb=short -q
uv build
uv run --locked footman --help
uv run --locked python -m footman --help
scripts/check-green
```

The pytest command is the mandated full-suite run: `pytest-xdist` uses
`--dist worksteal` across eight workers and redistributes pending tests from
busier workers. Tests from one file may execute in different processes;
module/session fixtures may therefore initialize once in each participating
worker. Do not rely on file-wide worker affinity, mutable module globals shared
between tests, or a session fixture being globally unique. Keep mutable resources
fixture-owned and isolated per worker/request, and synchronize only through
explicitly owned interfaces. Use this command, not a serial or narrowed variant,
for G; `docs/python-checks.md` records the parallelism rules and gate entry points.
The [worksteal adoption record](testing-worksteal.md) preserves the historical
1000-run validation, CPU observations and deferred I/O investigation. If a task explicitly requires a serial `unittest
discover` run, run that as well: pytest does not replace an explicitly required
verification.

`tests/test_scaffold.py` checks package import, startup argument parsing, absence
of direct-dispatch subcommands and missing-configuration startup refusal.
These scaffold checks alone do not establish any dispatcher capability.
The Phase 1 suite below adds installation and disposable-harness coverage.

`test_missing_configuration_file_refuses_startup` permanently replaces the
old temporary unimplemented-server assertion. It uses a guaranteed absent path
under `tmp_path`: nonzero exit, empty stdout and nonempty stderr identifying a
configuration-load failure, without pinning a code or full diagnostic wording.
The separate `test_invalid_existing_toml_refuses_startup` checks invalid fields
in an existing file, field-specific diagnostics and absence of run side effects.
Do not create a valid TOML merely to assert startup failure: valid service startup
need not fail.

Run all scaffold coverage, including that permanent replacement, in later
regression runs. Do not skip/xfail/filter a temporary assertion or mark the whole
scaffold suite superseded by protocol delivery. The MCP protocol suite adds
valid startup coverage; it does not supersede missing-file startup refusal.

When package contents or build metadata change, G also requires a non-editable
installation smoke check. Build a wheel and install that exact wheel into a fresh
virtual environment. Outside the checkout, with `PYTHONPATH` unset, import
`footman.filesystem.cleanup`, `.streams`, `.operations`, `.descriptors` and
`footman.process`, invoke the installed `footman --help`, and run the installed
`python -m footman --help`. Record the exact commands and exit statuses. Do not
treat editable-package import success as wheel-installation verification.

## Phase 1 foundation suite

Prerequisites: Linux/POSIX, an unprivileged user, Python 3.14+, Git and uv,
with `uv sync --extra dev --locked` completed. No Docker access is needed.
Register and run this suite for RED and every later foundation regression:

```sh
.venv/bin/python -m pytest tests/test_foundation.py tests/test_foundation_ownership.py tests/test_scaffold.py -n 3 --dist worksteal --tb=short -q
```

The installation test builds one exact wheel and installs it into a fresh venv
outside the checkout with PYTHONPATH removed; it checks explicit lifecycle
imports and both installed help entry points. Isolated negative controls use
`FOOTMAN_FOUNDATION_CONTROL=missing-module`, `missing-executable`, or
`mutate-fixture` before the same command. Each must exit nonzero with an
assertion failure; they never edit production code or the developer index.

For VALIDATE, run the same foundation suite in a fresh non-editable environment
(the absolute test paths load repository test support, never checkout `src/`).
From the repository root, after the documented `uv build`:

```sh
repo=$PWD
smoke=$(mktemp -d /tmp/footman-foundation.XXXXXX)
uv venv --python python3.14 "$smoke/venv"
uv pip install --python "$smoke/venv/bin/python" "$repo/dist/footman-0.1.0-py3-none-any.whl" 'pytest==8.4.2' 'pytest-xdist==3.8.0'
cd "$smoke"
env -u PYTHONPATH "$smoke/venv/bin/python" -m pytest "$repo/tests/test_foundation.py" "$repo/tests/test_foundation_ownership.py" "$repo/tests/test_scaffold.py" -n 3 --dist worksteal --tb=short -q
```

Record the exact wheel path/digest, environment path, each command status and
complete pytest output. The nested installation smoke also verifies module
provenance outside the checkout. Test scratch directories are pytest-owned;
Git fixtures and stub call records live only below those directories. Stub
cleanup never enumerates or removes real containers. Remove only the recorded
external environment when no longer needed; retain evidence separately.

## Phase 2 configuration suite

Prerequisites are the same as the Phase 1 suite; Docker is not required.
Run this command for configuration RED and subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_configuration.py tests/test_scaffold.py -n 2 --dist worksteal --tb=short -q
```

The missing-file startup replacement alone can be recorded before the remaining
configuration tests are authored:

```sh
.venv/bin/python -m pytest tests/test_scaffold.py -n 1 --dist worksteal --tb=short -q
```

The configuration suite covers strict fields/types, canonical overlap and
symlink ancestors, numeric bounds, templates, phase-only rejection before the
downstream callback, immutable configuration across edits/restarts, process-level
startup refusal, and config-reader failure/interruption/repeat-release branches.
The example is loaded against disposable fixture paths. See
[configuration.md](configuration.md) and [the annotated TOML](../examples/footman.toml).
The internal application boundary is not MCP registration or a direct CLI.
Run both the Phase 1 and Phase 2 commands above for Phase 2 validation, as well
as the unscoped full green gate and build/help/installation checks.

## Phase 3 output reservation suite

Use the Phase 1 prerequisites; no Docker access is needed. Run this exact
command for reservation RED and subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_output_reservation.py -n 1 --dist worksteal --tb=short -q
```

This suite covers one UUIDv7 and its Crockford/millisecond identity, single-pass
output expansion, exclusive directories/fixed files, collision races, symlink
replacement, restrictive modes and ownership/failure cleanup. Storage setup must
finish before any snapshot or launch callback. See
[output-reservation.md](output-reservation.md) for paths, identity and retention.
For validation also run the Phase
1 and Phase 2 commands, the unscoped green gate, build/help and exact-wheel smoke.

## Phase 4 staged snapshot suite

Use the Phase 1 prerequisites and disposable real Git repositories; no Docker
access is required. Run this exact command for RED and snapshot regression:

```sh
.venv/bin/python -m pytest tests/test_snapshot.py -n 1 --dist worksteal --tb=short -q
```

The suite covers exclusive snapshot ownership, private-index capture and raw,
mode-aware staged export. The staged-byte `dev_snapshot_mount_identity` case
makes no Docker mount claim. See [snapshot identity and limitations](snapshots.md).
Validation also runs all earlier phase commands,
the unscoped green gate, build/help and exact-wheel installation checks.
Setting `FOOTMAN_SNAPSHOT_CONTROL=workdir-bytes` before the same suite command
is an isolated negative control: the partial-staging assertion must fail when
its expected bytes deliberately describe the workdir instead of the index.
It does not modify production code or the developer repository.
`FOOTMAN_SNAPSHOT_CONTROL=stale-capability-path` before the same command is a
second isolated negative control: the new move probes report stale expected
paths for their tracked capability fds, so assertions requiring early identity
rejection must fail. Production files and release authority are unchanged.
The normal suite also covers missing/shared-parent creation, concurrent-parent
reopening, walk-time symlink replacement, removed/moved capabilities, once-only
release on those failures, and exported/created-directory modes.

## Phase 5 launcher suite

Use the Phase 1 prerequisites; Docker is not required. Run this command for
launcher RED and subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_launcher.py tests/test_launcher_execution.py tests/test_launcher_validation.py tests/test_launcher_copy_streams.py tests/test_launcher_faults.py tests/test_launcher_boundaries.py -n 6 --dist worksteal --tb=short -q
```

The suite covers required image-ID formats and immutable startup validation,
constrained never-pull launch templates, exact-ID/running-state inspection,
root mkdir without -p followed by literal contents-copy argv, and configured
exec user/cwd/argv/status/separate streams. The sequenced executable stub records
per-request argv, status and fixture IDs, rejects unexpected order and never
accesses Docker. Ownership faults cover each local helper, explicit collector
failure, partial/pending stream observations and once-only collection.

`dev_snapshot_copy_identity` checks real exported host staged bytes, executable
mode and actual symlink as the requested copy source; it does not establish
in-container preservation or absence of stale merging. `dev_image_id_forms`
scripts Docker resolution errors and local hexadecimal-name collisions; literal
operands and --pull=never are adapter observations, not Docker resolution proofs.
`dev_exec_image_and_wrapper_argv` and the process half of
`dev_exec_status_and_streams` check both executable configurations without running
consumer code on the host. Result delivery adds persistence, MCP delivery adds
protocol responses, and real-Docker host acceptance establishes actual copy and
execution behavior. Phase 6 adds recorded-ID
domain cleanup; settled local stub children do not prove container removal.

For validation also run every earlier documented phase suite, the unscoped
full green gate, build/help and exact-wheel installation checks. Infrastructure
summaries preserve raw sensitive prefixes; see [configuration.md](configuration.md)
for copy prerequisites, non-root permissions and all five files' access/retention.

## Phase 6 supervised lifecycle suite

Use the Phase 1 prerequisites; Docker is not required. Run this exact command
for supervision/cleanup RED and subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_dispatch_lifecycle.py tests/test_dispatch_confirmation.py tests/test_dispatch_deadlines.py tests/test_dispatch_cleanup_faults.py -n 4 --dist worksteal --tb=short -q
```

The suite exercises shared deadlines, independent requests, recorded-ID removal
and the separate complete-empty-listing confirmation protocol, snapshot retention
and no-follow cleanup. See [incomplete-run reconciliation](reconciliation.md). Its Docker CLI scenarios are simulations, not real Docker
removal or copy proofs. Validation also runs every earlier phase suite, the
unscoped green gate, build/help and exact-wheel installation checks.

### Deterministic deadline-policy scenarios

`LogicalScenario` installs the shared read-pure `ManualClock` through
`module_clock` on `footman.container` only, before constructing the real
supervisor. The clock starts at zero. Named successful-helper completion
boundaries spend time explicitly; repeated reads, scheduler pauses and cleanup
observations spend none. Process, supervisor cleanup and watchdog clocks remain
real. The module reference is restored on normal return, assertion failure and
interruption; no logical participant survives scenario execution.

Synthetic no-fd helpers run through `launch=` and deliver separate protocol
streams and EOF to production parsing. They expose exactly one collection and
context exit, but are not evidence of real child execution or OS release.
Inspection, mkdir and cp admission cases each check exact and past exhaustion:
no next working request is permitted when the remaining budget is zero or
negative. Positive-remainder controls admit that same operation, and cumulative
named expenditures require decreasing positive shared allowances (numerical
comparison uses relative and absolute tolerance 1e-12). Assertions compare
predeclared literal working prefixes and independent recorded-ID removal and
confirmation, not launch counts or child-written files. Advancement journals
prove the intended charges exactly once.

These are deterministic policy tests, not real-process timeout evidence or
Docker semantics. Real executable-boundary checks must independently retain a
positive timeout and local settlement; logical helpers cannot establish either
for an actual child. Missing child records do not become acceptable merely
because the logical policy tests pass.

The real-child `test_dev_survivor_removal_order` also isolates admission time
with `ManualClock` on `footman.container` only. Preparation receives a positive
10-second allowance; copy admission explicitly spends 9.6 logical seconds, so
exec receives 0.4 seconds against a two-second stub delay. A deliberate
0.5-second preparation pause exceeds the old aggregate budget and guards against
scheduler-dependent early exhaustion. Child deadlines and cleanup remain real;
all seven child records, exec timeout, exact-ID removal/confirmation and snapshot
deletion remain mandatory. This is removal-order coverage, not evidence for a
10-second aggregate wall-time deadline; the independent real-process deadline
scenario below retains that responsibility. No production budget is changed.

### Settlement, records and expected failures

`DockerSequence.assert_owners_settled()` checks local cleanup completion only
and returns the retained lifecycle observations. Completion does not mean the
operation or every release succeeded. `assert_records()` separately requires
valid child records; the compatibility entry point `assert_settled()` requires
both completion and strict records, leaving operation verdicts to each scenario.
A child record proves that the helper reached its recording point, not that
cleanup or external work succeeded; a parent launch request alone proves neither
acquisition nor child execution.

The deadline-only `LaunchJournal` records entry to the parent launch boundary
before acquisition, with a stable ordinal, literal argv and the passed execution
and grace allowances. Its working and cleanup scripts have independent strict
cursors: any valid working prefix may transition to exact recorded-ID cleanup,
without search-ahead or a fixed cleanup offset. Immutable snapshots distinguish
requested, acquired, failed and interrupted attempts; results and caller-supplied
child-record observations attach to the original ordinal, including across
cleanup-thread interleavings. Requested paths and launch counts never substitute
for acquisition outcomes or ordered exact argv. Admission observations do not
claim atomic kernel process creation before an absolute timestamp.

The journal and its diagnostics borrow existing owners and results. Successful
handoff leaves context exit with the caller; if publication fails after
acquisition, the launch wrapper forwards context exit exactly once to that same
sole owner, retaining it for observation even if cleanup is incomplete. It never
creates a second owner, collects again or independently signals/reaps/releases
borrowed pipes. Acquisition exceptions and first interruptions retain authority
over ordinary observation/release failures. Diagnostics use retained observations
only, are capped at 64 KiB of UTF-8 with an explicit truncation marker, and keep
full argv and record bytes in memory for assertions. Record availability is
explicitly unobserved until the caller supplies evidence; retaining absent,
available or read-failed evidence neither validates a record nor permits its
absence. No timeout-record tolerance is enabled by this Phase-1 journal itself.

Phase-3 deadline verdicts opt in per attempt: only an acquired working helper
with its own positive timeout result and completed, failure-free local cleanup
may lack a child record. Empty bytes or a proper byte prefix of the independently
expected sequenced-stub JSON are unavailable diagnostics, not proof of child
execution, argv or stdin EOF. Complete records remain strict, including schema,
argv and stdin EOF. Successful working and cleanup helpers require complete
records and cannot inherit a neighboring helper's timeout eligibility.
`read_child_record` uses one `OwnedDescriptor` and no-follow opening: only
open-time file absence is classified as absent; read, permission and release
errors remain failures. It never retries a close.

The real-process scenario keeps the configured 0.3-second execution and 1-second
cleanup budgets, supplies all five working steps and delays only the terminal
step by 2 seconds. Existing production policy subdivides cleanup allowances
among helpers; no budget is enlarged or renewed. The verdict identifies the
actual retained timeout and forbids later working requests, without requiring a
particular stage or any child write to have occurred. Real watchdogs stay real.
Known identity requires exact recorded-ID removal and complete successful empty
listing, local settlement and snapshot deletion. Startup timeout without an
attributable identity requires no guessed cleanup, unresolved-resource reporting
and snapshot retention. Controlled no-fd outcomes exercise both identity
branches independently of real-child stage timing. Synthetic absent/prefix,
invalid-record and lifecycle controls prove classification only, not OS child
execution or external-resource success. These tests neither prove Docker
semantics nor establish the historical missing record's root cause.

Deadline-specific `assert_lifecycle_failures()` defaults to rejecting every
retained failure. Negative scenarios can declare exact expected ACTION/CALLBACK
observations for that helper: exception identity, kind, phase, stream, order and
multiplicity must match. Expectations must be independently declared, never
copied from the actual failures to make a check pass. Release or other helper
infrastructure failures and unexpected defects cannot be declared acceptable.
An expected operation failure therefore cannot hide a simultaneous release
failure, incomplete local cleanup or a missing mandatory record. These checks
only inspect retained observations; they do not collect again, retry cleanup or
renew budgets. Existing domain-failure/interruption scenarios retain their
separate verdict assertions.

## Phase 7 result delivery suite

Use the Phase 1 prerequisites; Docker is not required. Run this exact command
for result RED and subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_results.py -n 1 --dist worksteal --tb=short -q
```

This suite covers complete separate stream persistence, atomic manifests,
raw infrastructure summaries and deterministic outcome conflicts. See
[result fields, precedence and evidence limitations](results.md). Validation
also runs every earlier phase suite, the unscoped green gate, build/help and
exact-wheel installation checks. Stub results do not establish Docker semantics.
Publication regressions cover initial/checkpoint/final failure escalation,
post-rename directory-sync uncertainty, cleanup precedence and no corrective
publication. `FOOTMAN_RESULTS_CONTROL=publication-order` before the same result
suite command is an isolated negative control: it deliberately expects directory
sync before replacement and must fail the ordering assertion without changing
production code or the developer index.

## Phase 8 owned stdin suite

Use the Phase 1 prerequisites; no MCP or Docker is required. Register and run
this exact command for owned-stdin RED and subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_process_stdin.py tests/test_process_lifecycle.py tests/test_process_orthogonal_results.py tests/test_process_single_collection.py -n 2 --dist worksteal --tb=short -q
```

The stdin suite exercises the opt-in process foundation independently of MCP.
`tests/process_stdin_alias_child.py` isolates incompatible-factory fd-alias and
metadata-probe controls: its streams use synthetic descriptor numbers, and it
reports deliberate retention/incomplete cleanup before OS exit contains the
otherwise permanent continuation. These tests cover finalizer safety, sibling
releases, interruptions and repeated observation without leaking a retained
worker into pytest. Distinct-number/shared-file-identity controls are simulations,
not evidence of real dup acquisition; the generic-child EOF test separately
exercises actual Popen pipes. No real raw fd is acquired by the synthetic streams.
The other three modules retain the default DEVNULL, rejected stdin override,
output-only public values and once-only output collection regression pins.
Validation also requires every earlier phase suite, the unscoped green gate,
build/help and exact-wheel installation checks.

## Phase 9 MCP transport and server suite

Use the Phase 1 prerequisites plus GNU `cp` (private installation clones) and
the pinned MCP SDK/`anyio` from `uv sync --extra dev --locked`; no Docker access
and no Docker daemon are
required. Register and run this exact command for MCP transport/server RED and
subsequent regression:

```sh
.venv/bin/python -m pytest tests/test_mcp_transport.py tests/test_mcp_server.py -n 2 --dist worksteal --tb=short -q
```

`tests/test_mcp_transport.py` holds the project-owned test-client transport, the
development-only protocol peer, the server bootstrap/provenance bootstrap
contracts, the reserved-line attestation parser, the parent-owned provenance
evidence branch, the lifecycle input/journal contract, both canonical wheel argv
variants and the Phase-9-owned `tests/mcp_wheel_support.py` build/install
fixture driven by the inert `tests/wheel_attribution_cases.py` records.
`tests/test_mcp_server.py` holds the real `ClientSession` coverage of the actual
service: initialization and tool discovery in the development and isolated
installed-wheel contexts, the structured result contract, the MCP publication
error boundary, disconnect supervision and concurrent independent requests.
Missing `uv`, `python3.14`, package-index access or the built wheel contract is
an explicit nonzero prerequisite failure in these tests, never a skip, xfail or
filtered collection.

The wheel fixture performs one real bare `uv build` from a recorded
disposable matching-input repository copy, binds the exact produced wheel path
and its independently computed SHA-256, and installs that wheel non-editably
into a fresh venv outside the checkout. It never builds in the candidate
checkout and never reuses a `dist/` candidate: the candidate checkout's `dist/`
inventory is recorded and re-checked unchanged around every scripted or
disposable build leg. `uv` output-collision cases refuse before invoking the
tool and delete nothing; the operator remedies are removing only the named
conflicting paths outside the fixture or invoking a different clean
matching-input root. `attribution_only=True` stops after build attribution,
before any venv, dependency or install acquisition, and stub-built artifacts
never feed an install. The reviewed static build profile rejects ambient Hatch
settings, unreviewed `UV_*` settings and discovered uv configuration files before
invocation; cache/index/transport/concurrency settings explicitly listed in the
fixture are allowed. Such preflight refusal never deletes prior artifacts.

The two MCP test modules lazily create one attributed real build and fresh
noneditable service venv per participating worker, through a session fixture.
Its immutable wheel/install/support copy is reused; tests keep independent
configs, Git repositories, manifests, request resources and evidence. Support
shadow/mutation controls receive a private support copy as well. No
worker that runs only primitive tests needs an installation. Recorded setup
paths/counts distinguish workers; cache reuse rechecks candidate build inputs.
Destructive package/launcher controls use private copy-on-write installation
clones (`cp --reflink=auto`, with independent-copy fallback), never hardlinks to
the base. The clone interpreter's install scheme is checked, Footman's public
launcher shebang is relocated, and archive-authoritative expectations are
remapped and its separate script digest recorded. These are recorded installation
clones, not new installs or general-purpose relocatable venvs; activation and
unrelated dependency CLI launchers are not used. Direct fixture/attribution and
wheel-swap boundary tests still exercise their own public build surface.

One fixture-owned admission file per pytest invocation bounds real provisioning
and native clone-copy work across workers. Waiting consumes the original setup
deadline, without renewal. Scripted attribution-only cases bypass heavy-work
admission. The service venv contains no pytest/xdist; installed-foundation checks
provision their own test environment. Shared-base reuse, private manifests,
clone mutation isolation, relocated launcher/layout and process-owner fault
matrices are included in the registered MCP suite, alongside admission
failure/contention/interruption/repeat/independent-cleanup cases.

Hostile-locale real-child cases are separate from the inherited-locale cases.
They set `LC_ALL=C`, `LANG=C`, `PYTHONIOENCODING=ascii`, `PYTHONUTF8=0` and
`PYTHONCOERCECLOCALE=0` after environment cleaning, record those launch settings
and assert the child's effective ASCII stdin/stdout encodings. A split-frame
protocol peer echoes non-ASCII arguments; the real service initializes/discovers
and returns a Unicode phase-validation response through its borrowed UTF-8
facades, with no reservation and no stdout diagnostic contamination. Child
stderr confirms the effective locale and retains the diagnostic namespace guard.

The wheel context runs copied `tests/mcp_wheel_client.py` with the fresh venv's
absolute interpreter and `-P`. That client uses the copied transport and installed
wheel's process foundation; development pytest only orchestrates it. The client
loads the recorded sibling `tests` package explicitly without inserting its root
into `sys.path`. Its foundation checks are post-import attribution, separate from
the service bootstrap's pre-dispatch checks, not a universal pre-import guarantee.
The service still uses the canonical `python -P <bootstrap> --expected-origin
<manifest> --entrypoint <installed-footman> -- --config <config>` argv, with
`--lifecycle <input>` immediately before `--` only in lifecycle contract tests.

The declared normal copied support closure is `tests/__init__.py`,
`tests/green_skip_policy.py`, `tests/mcp_transport.py`,
`tests/mcp_provenance.py`, `tests/mcp_server_child.py` and
`tests/mcp_lifecycle.py`. The independent Phase-9 fixture additionally copies
`tests/mcp_wheel_client.py`; neither that client nor
`tests/mcp_wheel_support.py` is a dependency of normal real-host support.
`tests/mcp_protocol_child.py`, the Docker stub and assertion-driver modules are
not transitive dependencies of that closure. Each copied file's identity is
recorded and checked against its source. Dependencies are constrained by the
candidate's `uv.lock`; provisioning commands, statuses and complete streams are
kept separately from the exact build observation. Scripted attribution cases use
only the declared three-input profile, never these real provisioning inputs.

The optional lifecycle input is schema version 1 with exactly `scratch_root`,
`journal_path`, `instance_token`, `service_deadline_monotonic` and
`settlement_allowance_seconds` in addition to `schema_version`. The helper binds
it to the original launch/scratch budget before acquisition; the bootstrap
independently validates before journal publication and attestation. Rejection
before acquisition leaves no journal; a publication failure may leave partial
records but cannot authorize dispatch. The journal records token/PID/Linux
process start-time incarnation before service dispatch and retains terminal
observations when available. Its sole owner attempts each independent release
once and retains failures on repeat observation. EOF and absolute lifetime use
the same input reader, with no competing reader or renewed action/cleanup
budget. A journal or helper exit is not authority to signal/reap/adopt the
service or proof of absent external resources. Later helper-loss integration
must independently establish service-incarnation absence and reconciliation.

See [Test-bootstrap stderr provenance](#test-bootstrap-stderr-provenance) for
the reserved stderr namespace these tests use and the diagnostic collision
guard they prove.

## Standalone Linux proc-observer regression suite

Register and run the observer repair separately, with the same Linux/POSIX and
unprivileged-user prerequisites as Phase 10:

```sh
.venv/bin/python -m pytest tests/test_proc_observer.py -n 2 --dist worksteal --tb=short -q
```

This module covers synthetic proc ownership, PID reuse, narrow leaf-open
disappearance and independent/repeated cleanup failure authority. It is required
for current observer-repair verification alongside the original Phase-10 suite
and the unscoped gate, with its own two-worker allocation at queue priority 13.
It is not part of the historical Phase-10 registration or validation counts.

## Phase 10 integrated acceptance and portability suite

Register and run the Docker-free acceptance module with its original command:

```sh
.venv/bin/python -m pytest tests/test_dispatch_acceptance.py -n 2 --dist worksteal --tb=short -q
```

Prerequisites are the Phase-9 tools and pinned SDK/dependencies, including uv,
Python 3.14+, Git, GNU cp and package-index access for provisioning. Missing
prerequisites fail nonzero, never skip or xfail. This module belongs to unscoped
development collection; it is not real-Docker host acceptance.

The checkout integration leg uses the development same-child bootstrap and
checked src/footman expectations, real staged Git fixtures, literal Docker CLI
stub calls and retained manifests. Later fixture index/workdir edits are
explicitly recorded separately from dispatcher mutation. Setting
`FOOTMAN_ACCEPTANCE_CONTROL` to `tree`, `argv`, `path` or `status` before the
same command deliberately supplies a wrong expectation and must fail; it changes
neither production code nor the developer repository.

The portability fixture is pytest-integrated: a module fixture builds with
bare uv build from a matching-input disposable root, binds its successful wheel
output and SHA-256, provisions a fresh external venv and installs that exact
wheel noneditably. Each participating worksteal
worker owns its own build/venv/scratch paths and reuses only its own successful
setup. The module fixture delegates to a lazy worker-session resource so leaving
and re-entering the module cannot trigger a second build/venv. The no-obtain
branch is directly tested by
`test_worker_scope_without_obtain_acquires_and_finishes_nothing` on normal,
operation-failure and interruption exits: zero factory/preparation/completion
calls, repeat cleanup and closed-scope rejection. A two-module probe interleaves
all eight collected cases under worksteal and records exactly one setup/completion
per participating worker, with distinct roots and once-only failure-aware cleanup.
That probe has no idle worker and makes no empirical idle-worker-count claim;
not acquiring for non-participants follows structurally from the lazy scope and
the directly executed no-obtain branch tests. Scripted attribution artifacts never feed installation. The relocated
SDK helper, copied transport/bootstrap and Docker-stub closure must use that
venv, clean environments and wheel-authoritative expected origins. The fresh-venv helper alone collects service stderr, confirms its attestation
and writes/releases/reads back complete provenance evidence. The orchestrator
consumes bounded ordinary JSON, never helper stderr as service attestation.
Helper post-import attribution is separate from service pre-dispatch checks;
neither is a universal pre-import guarantee. Lifecycle accounting and
helper-loss reconciliation separately establish service-incarnation absence
and stub-descendant/resource reconciliation. The orchestrator owns only the
helper, never the helper's service owner: absence observation does not grant
signal/reap authority or permission to renew an allowance. This is a structural
ownership guarantee, not an instrumented zero-attempt counter. After abrupt
helper loss, service exit/reap status is unavailable, not an observed null status.
The real observation carriers are `service_incarnation_absent`,
`helper_cleanup_complete`, the retained service journal, and (for cooperative
interruption) `helper-service-cleanup.json`. Live/unknown/observer-failure and
unsettled-resource controls must remain unsuccessful with retained scratch.
Request/lifecycle files record the original deadline and settlement binding;
configured 10/2-second action bounds and 60/5-second lifetime/settlement bounds
are inputs, not hardcoded observation fields asserted against themselves.
The relocated helper reports the actual environment mapping it passed to the
service as a bounded view: PATH, the actual PYTHONNOUSERSITE value, key count,
key-set digest, filtered-prefix key names and counts. Values of unrelated
variables are not dumped. The poison-environment leg leaves PYTHONPATH,
PYTHONHOME, PYTHONUSERBASE and PYTEST/FOOTMAN_RELOCATED canaries in the helper
launch environment; only this leg uses helper `-E` so invalid PYTHONHOME cannot
break interpreter startup before the filter runs. The helper reports seeing
those incoming key names and must remove them from the service environment,
retaining only PYTHONNOUSERSITE=1 among PYTHON* keys. Actual service provenance
must still match the fresh wheel. This exercises the helper's filter, not the
orchestrator cleaner. `test_acceptance_clean_environment_removes_import_redirection`
separately tests the Phase-10 cleaner against ambient and explicit-extra poison,
including PYTHONSAFEPATH/STARTUP/INSPECT, unknown PYTHON* and FOOTMAN_BOOTSTRAP_*,
without mutating the parent environment. Result metadata remains subject to the
existing 65536-byte bound; key-set metadata is not OS-child self-attestation.

The checkout integration test alone does not establish wheel portability. Phase-11 host tooling is not a prerequisite or a
substitute. Only fixture-owned disposable build outputs may be created; existing
candidate dist artifacts must remain untouched. Validation also requires every
earlier registered suite, the unscoped gate and build/help/exact-wheel smoke.

The copied closure is the Phase-9 normal support list plus
`tests/mcp_relocated_client.py`, `tests/relocated_docker_child.py`,
`tests/docker_cli_stub.py` and `tests/dispatcher_harness.py`. The relocated
loaders explicitly resolve the recorded tests package; the service bootstrap
never adds that copy root to sys.path. Declared support and uv.lock identities
are rechecked at completion. The annotated TOML's command and launch argv are
preserved literally; only disposable host paths/output organization and the
10/2-second action bounds are substituted. The service has an independently
bound original 60-second lifetime and five-second settlement allowance.

Fixed bootstrap-only publication-loss gates exercise interruption before the
journal, before provenance and after provenance. They use one serial borrowed
stdin reader before any SDK reader exists, within the original deadline; no
inherited stream is adopted or closed. Gate PID/incarnation records are test
accounting, not provenance evidence or a public service capability. A missing
journal remains an unsuccessful launch observation; a recorded gate identity
can independently reconcile that negative fixture through read-only absence
observation, never signal/reap authority. Unresolved identities/resources retain
scratch and fail closed. Reconciliation binds each distinct full launch ID to
exactly one literal `rm -f <id>`, one complete canonical recorded-ID listing argv
and one retained result manifest with that same container_id. Equal counts or
successful stub statuses alone cannot prove this: the relocated stub derives
expected_argv from its received argv. Multiplicity-aware argv/manifest matching
rejects wrong targets, duplicate cleanup, mismatched filters, extra arguments,
duplicate launch identities and mismatched/duplicate manifest identities even
when all counts/statuses/EOF and snapshot-absence checks remain valid.
`test_reconciliation_requires_exact_recorded_id_cleanup_and_manifest` exercises
these count-preserving controls with real retained fixture files; the wrong-ID,
duplicate-cleanup and mismatched-filter controls also run in optimized subprocess
probes. They prove recorded fixture identity accounting, not real Docker removal.
Helpers alive during interruption retain their sole
process owner and original cleanup budget. The loss collector protects that
owner immediately after launch, before constructing or starting its thread.
Shutdown and collector joining are independent cleanup actions; ordinary cleanup
errors cannot replace an active operation failure or interruption. A failed or
interrupted start after handoff still joins the actual started thread. If start
has not published a joinable identity, the failed observation retains the same
owner/thread rather than claiming settlement or retrying startup. Constructor,
start/pre-post-handoff, body, shutdown, join, incomplete-owner and live-collector
branches have once-only/precedence tests, including real-child settlement and
repeat cleanup.

`tests/acceptance_build_support.py::build_and_bind_wheel` remains independently
callable for later host-helper attribution parity; production code does not
import it. Both existing build implementations retain a nonzero build's status
and streams if the subsequent input check fails, with separately retained
post-check diagnostics. A post-check interruption retains its authority and
chains the earlier build failure. These combined-failure controls run in both
registered MCP and acceptance suites.

## Test-bootstrap stderr provenance

This is test infrastructure, not a production MCP feature and not a requirement
that external MCP clients provide attestation. The production service emits no
provenance record; only `tests/mcp_server_child.py` does.

`tests/mcp_server_child.py` runs inside the sole service child. Before
dispatching any service code it resolves the Footman package root, enumerates
every regular `*.py` below it (including `filesystem/`) without following
symlinks and without importing the modules, and compares the exact
relative-path/module-name/SHA-256 set with an independently generated
schema-version-1 expected-origin manifest. Verification failure exits nonzero
with an ordinary stderr diagnostic and no attestation.

**Reserved namespace and grammar.** A physical stderr line that begins with
ASCII `FOOTMAN_PROVENANCE_` is reserved for that bootstrap record. The only
valid record is `FOOTMAN_PROVENANCE_V1 `, one canonical JSON object and exactly
one `LF`, at most 512 source bytes including prefix and `LF`, with no `CR`, no
BOM, no embedded literal newline and no trailing bytes. The object has exactly
`schema_version` (integer `1`), `source_count` (non-boolean integer in
`0..2**63-1` matching the verified set) and `set_sha256` (64 lowercase
hexadecimal characters of the canonical source-set array SHA-256), serialized
with `sort_keys=True`, `ensure_ascii=True` and `separators=(",", ":")`. The
canonical source set is an array of objects with exactly `path`
(package-relative POSIX source path) and `sha256` (lowercase source digest),
sorted by `path`. A second reserved-prefix line is a duplicate failure even if
its payload is malformed.

**Emission.** The bootstrap builds the complete record in memory, then performs
exactly one write of the whole record to the borrowed inherited binary stderr,
checks that the write returned the full record length, and flushes, before any
service or SDK/server task or logging writer starts. A short write, a write or
flush failure, or an interruption fails the bootstrap without re-emitting the
record, without retry, and without dispatching the service; inherited stderr is
never adopted into an owner and never closed by the bootstrap.

**Parsing and evidence.** The immediate parent (the Phase-9 fixture or a later
runner) collects the child's stderr through one `stream()` collection and parses
it incrementally: at most 512 candidate bytes plus fixed line state, ordinary
diagnostic bodies drained and discarded, arbitrary chunk boundaries handled, and
excerpts, capture limits and retained tails never supplied to the parser. A
complete matching line is provisional, so MCP initialization may start while the
service runs; duplicates keep being detected for the whole child lifetime. Final
confirmation requires exactly one matching record, no parser failure and
terminal stderr `StreamOutcome.EOF`; `READ_FAILED`, `ABORTED` or a pending
terminal outcome means stderr verification is incomplete even when a valid line
was already seen, and an unterminated reserved candidate at EOF is a truncated
attestation rather than absence.

Only after final confirmation does the parent write its own schema-version-1
evidence file, at an absolute path it chose and recorded under external scratch
or evidence directories, through sole owners, and read the persisted bytes back
and recompute the set, count and digest before claiming retention. A parent-side
expected set alone is never child-verified evidence; missing, duplicate,
truncated, malformed, mismatched, unwritable or inconsistent attestation or
evidence fails closed while the child's own process status stays separate.

**Diagnostic collision guard and raw bytes.** Footman's generated diagnostics,
logging including multiline exception formatting, configuration and startup
refusals and generated helper-failure reports never begin a physical stderr line
with the reserved prefix; the server prefixes each physical diagnostic line, and
this guard is proven against actual collected server stderr. The guard is a
formatting boundary only: retained raw helper, command and startup bytes stay
byte-exact in their own files and are never redacted, rewritten or blindly
forwarded into service stderr.

**Ordinary fixture and synthetic controls.** Ordinary `tests/mcp_protocol_child.py`
fixture modes emit no reserved-prefix line and authorize no verified-set
evidence. Reserved, duplicate or malformed parser inputs come from explicitly
synthetic byte/stream controls in `tests/test_mcp_transport.py` marked as parser
controls; they are never service attestations.

**Lifecycle journal is separate evidence.** The bootstrap's optional
`--lifecycle <absolute input>` test-bootstrap input and the journal it publishes
before dispatch are distinct from provenance attestation and evidence. They
account for the instance token, PID, OS process incarnation, start status and
inherited deadlines, and grant no release authority to any observer. See
[inherited stdio and diagnostics](process-lifecycle.md#inherited-stdio-and-diagnostics)
for the runtime-owned inherited stream rule that keeps this namespace writable
only by the bootstrap.

## Descriptor, stream and process helpers

`tests/test_filesystem_*.py` and `tests/test_process_*.py` cover the helpers
ported from Jigbox into `footman.filesystem` and `footman.process`. Run them
with the full-suite command above. They require Linux/POSIX and Python 3.14+,
not Docker or npm. Shared deterministic clocks live in `tests/clock_support.py`
and `tests/conftest.py`; their zero origin avoids host-uptime rounding at exact
deadline boundaries.

The port excludes Jigbox-specific npm captured-action/timeline adapters,
transaction compatibility/re-export checks, and repository-wide enforcement
inventories. Generic diagnostic tests use a local domain-error stand-in.
These exclusions are scope boundaries, not pytest skips or failure filters.
The lifecycle guides distinguish illustrative examples from executable tests;
no upstream repository-wide convergence or documentation-execution gate is claimed.
Foundation imports in all four modules are restricted to stdlib and explicit
`footman.filesystem.*` submodules; injected violations exercise that boundary.
Upstream Jigbox phase/task labels in test comments are provenance only, not
coverage claims for Footman's OpenSpec phases.

## Python source and tooling checks

`scripts/python_checks.py` builds one deterministic Python inventory over `src/`,
`tests/` and `scripts/` and feeds it to Ruff lint, Ruff format and syntax
compilation; `scripts/check-types` type checks the package. Installation,
source-selection rules, the parallelism contract and the gate entry points are in
[python-checks.md](python-checks.md). `tests/test_python_source_selection.py`,
`tests/test_python_syntax_compilation.py`, `tests/test_python_ruff_parity.py`,
`tests/test_python_check_status.py` and `tests/test_python_check_lifecycle.py`
are the ported Jigbox tooling contracts; `tests/test_green_gate.py` pins the
gate plan itself. Every new ownership branch in the tooling (one descriptor per
source read, one directory capability per traversed directory, one process owner
per check command) has the mandated operation-plus-close, sole-close,
interruption and repeat-cleanup cases.

## Writing verification helpers

In verification helpers and pytest plugins, use explicit
`raise AssertionError(...)` for checks that must remain active under `python -O`;
bare `assert` statements are removed by optimization. Phase-10 portability,
attribution and shared build-failure verification use explicit checks, with an
AST regression against reintroducing optimizable assertion statements.
`test_verification_helpers_reject_invalid_evidence_under_optimization` launches
actual `python -O` children, requires optimization enabled and tests valid
controls before rejecting bad attribution, changed build identity, unresolved
worker resources, journal/gate token mismatch, missing removal/failed stub
records, oversized results, incomplete cleanup and reserved helper output.
`tests/acceptance_verification_probe.py` is development-only probe code, not a
relocated/host support dependency or wheel-provenance proof. In tests themselves, use
ordinary `assert` statements to retain pytest assertion introspection.

### Diagnosing Linux incarnation-observer ownership failures

A `/proc/<pid>` ownership refusal retains the filesystem exception's same-fd
`fstat` snapshot (uid/gid, effective identity, mode, device/inode, link count,
size and nanosecond timestamps), plus the observer's target PID, expected
start-time ticks, observer PID and monotonic timestamp. These notes appear in
complete captured pytest tracebacks. Every failed proc-stat read, including raw
`OSError` and interruption, carries target PID/start ticks, observer identity,
monotonic time, `operation=read-proc-stat`, errno and descriptor stage when
available. Error identity and traceback remain intact. These notes add no
read/retry/child owner and do not turn unknown observations into confirmed
absence. The specialized test-only `read_proc_stat(pid)` uses no-follow
capabilities with `require_owner=False` only for the numeric proc PID directory:
procfs ownership can change after task exit, including to overflow UID/GID 65534
inside a rootless user namespace. Ownership alone never establishes absence.
Generic `read()` and the production directory ownership guard remain unchanged.
Only ENOENT opening the exact PID directory, or ENOENT/ESRCH opening its `stat`
leaf, establishes absence. Errors reading an acquired file, unrelated path/errno,
permissions, and all close failures stay unknown; even a known absence cannot
hide failure releasing the directory. Start-time comparison still rejects PID
reuse and resource reconciliation remains independent. A real-kernel regression pins
a proc-directory capability while the child is alive, releases its stdin,
collects/reaps through its sole `OwnedProcess`, then opens `stat` relative to the
pinned capability: this kernel returns `ESRCH`, now narrowly interpreted as
absence at that leaf-open boundary. The same-fd fstat snapshots before and after
collection preserve measured ownership and namespace maps without requiring a
particular UID transition as a success condition. That reproduces a mechanism, not
the historical failure's exact cause. Cleanup count mismatch diagnostics include
launch/remove/confirmation counts, launch IDs, raw call records and scratch root.

For a requested isolated lifetime reproduction, retain the registered parallel
settings and select only the explicit node:

```sh
.venv/bin/python -m pytest 'tests/test_dispatch_acceptance.py::test_relocated_helper_loss_reconciliation[lifetime]' -n 2 --dist worksteal --tb=short -q
```

Compare this with repeated executions of the exact full eight-worker development
command registered above, not merely the two-worker Phase-10 module. Each repeat
is a separate invocation with full output, actual exit status and worker-local
fixture resources. Give every invocation its own exclusively created, short
`PYTEST_DEBUG_TEMPROOT` parent (for example `/tmp/fpd-<id>/r01`); do not reuse it
for the next invocation. Keep that root outside later runs' rotation/deletion
targets. The first outer session's `pytest-0` contains its worker-local artifacts;
record and verify that exact path, rather than selecting newest scratch. Retain
`service-journal.jsonl`, `lifecycle.json`, `launch-intent.json`,
`helper-service-cleanup.json`, all `stub-records`, result/stream evidence and run
manifests in place, along with the complete tree and input identity. Store the
absolute scratch path in the durable run record. Do not archive only the outer
pytest traceback: subsequent default pytest sessions retain just three roots
and can otherwise rotate away the failing case evidence. Use short roots to
avoid changing installed launcher behavior through oversized venv shebangs.
Expected Phase-11 RED failures remain unsuccessful runs;
classify any additional lifecycle failure separately, without filtering it or
retrying merely to report a passing result. Repeated runs do not waive the final
unscoped green gate or build/help/exact-wheel installation requirements.

## Verification guard suites

The full-suite command includes:

- `tests/test_green_skip_policy.py`: isolated pytest runs proving skip/xfail/xpass
  rejection and preservation of existing failures and interruptions, under both
  the serial and the mandated parallel invocation.
- `tests/test_lifecycle_doc_links.py`: relative file links and Markdown heading
  anchors in both lifecycle guides, with injected broken links/anchors in scratch
  copies. Anchors use lowercase ATX-heading slugs, stripped punctuation and
  duplicate-heading suffixes; fenced code is ignored. Explicit HTML anchors and
  non-Markdown fragment targets are unsupported and must not be used in these
  guides. Remote URLs are not checked for availability.

## Phase 11 host-runner RED contracts

Register the Docker-free runner/build/inventory contracts before Phase-11 RED:

```sh
.venv/bin/python -m pytest tests/test_host_acceptance_contracts.py -n 2 --dist worksteal --tb=short -q
```

Prerequisites are the development environment above, Git, PATH-resolved
`python3.14` and `uv`; no real Docker executable, socket or daemon is used.
Apply the **Phase-11 real-base serialization exception** above: do not overlap
this registered command with the unscoped gate or another collector of its
shared-base contract. Wait for complete settlement, or use the sequential
fallback, without changing either command's worker count.
The tests belong to the unscoped development collection. They cover bootstrap
interfaces/tools/foundation isolation, exact wrapper-selected roots and relocation,
source versus README-inclusive build identity, public build attribution/collisions,
post-check failure precedence, isolated runner policy/provisioning, archive and
full-package provenance/evidence controls, static inventory/typing, and collection
isolation. Wrapper-dependent bodies stop at an explicit missing-interface
assertion until GREEN supplies the interfaces: their build/repeat/parity/runner
outcomes must not be reported as observed behavior before that implementation.
Missing public interfaces are implementation RED, never a passing expected build
rejection. Broken profiles, tools, copied dependencies or synthetic HEADs are
setup errors, not attribution-case rejections; direct preparation/instrumentation
self-tests exercise those distinctions independently of the missing wrappers.

Disposable prepared roots have explicit inventories and a resolvable synthetic
Git HEAD assembled without staging. The six-root source identity is independent
of the three-path build identity; README-only changes affect only the latter.
Scoped code/config/untracked/kind/executable/link changes, excluded planning/docs/
dist/cache changes, and fixture-only staging changes have separate contracts.
Only recorded disposable outputs may be removed by fixture-controlled remedies;
the helper must refuse both predicted sdist/wheel collisions without deletion.
Real-uv repeat-build tests use complete disposable roots, never candidate dist.
Candidate-checkout dist is observed and must remain unchanged around these legs.
Scripted tools exercise attribution only; their artifacts/manifests never feed an
installation. Unsafe-archive controls additionally install a blocking tool spy
so even an erroneous runner cannot install those scripted artifacts.

The separately discovered real-Docker suite is named `host_tests/`. Its public
interfaces, registered now and not yet implemented, are:

```sh
scripts/build-acceptance-wheel --manifest <build-manifest>
scripts/run-host-acceptance --wheel <exact-wheel> --source-identity <build-manifest> --image <configured-image-id> --evidence <directory>
```

The build interface wraps exactly bare `uv build` in its own physically resolved
wrapper repository root, not the caller cwd. The runner must independently
compare its wrapper-root source identity, README-inclusive build-input identity
and exact wheel digest against the controlled manifest. Both future wrappers
are `#!/bin/sh`, use PATH `python3.14`/`uv` without requiring checkout `.venv`,
and replace PYTHONPATH with exactly their resolved root's `src/` for bootstrap
only. Installed-wheel children must not inherit that injection. Missing bootstrap
tools must diagnose and exit 127. These are planned, explicitly tested exceptions
to the check entry points' `.venv/bin` rule, not changes to existing check tools.
The two shell paths will be recorded non-Python inventory exclusions when
implemented; their Python implementations must be inventoried automatically.

`tests/host_acceptance_assertions.py` supplies resource-free checks over supplied
MCP/manifest/stream/cleanup observations. The Docker-free controls obtain actual
MCP responses and retained files with a scripted Docker CLI, then deliberately
mismatch tree/argv/status/cleanup and related observations without changing
production code or the developer repository. Actual optimized subprocesses check
that invalid evidence remains rejected under `python -O`. These are assertion
sensitivity controls, not proof of in-container copy, execution or removal.
The future host suite must explicitly copy this assertion module in addition to
the normal transport/bootstrap closure if it reuses it; it is not a dependency
of that existing closure.

Task 11.2 extends this same registered suite with copied-fixture controls in
`tests/host_expectation_support.py`. These disposable profiles copy the assertion
module, its resource-owning JSON support and the skip-policy plugin; no real
Docker or wheel installation is claimed by these controls. Both profiles use the
same assertions. Only the negative profile contains the dedicated negative
module and fixed `host_tests/expectation_input.json`. Copied conftest validates
that input before the observation fixture runs; normal profiles reject it.
Schema 1 has exactly `schema_version`, `case_id` and `expected_override`:
`staged_bytes` uses a UTF-8 string, `argv` a nonempty NUL-free string array,
`command_status` an integer (not boolean), and `cleanup` a boolean
confirmed-absence expectation. Values change expectations only, never commands
or observations. Separate subprocesses retain actual nonzero statuses and full
streams for each targeted mismatch. Negative fixtures print the exact line
`FIXTURE_BASELINE_VERIFIED` only after shared `verify_run` succeeds, before the
intentional expectation check. Pytest capture remains enabled so xdist retains
this marker in failing-test output. A targeted detection requires exactly one
baseline marker and raw status 1, plus exactly one controller-emitted
`FIXTURE_OUTCOMES=` schema-1 JSON record: one collected negative item, passed
setup, the intended assertion failure and passed teardown, with no collection
or internal errors, interruption, skip/xfail/xpass or incomplete cleanup. The
case diagnostic must belong to the failed call's assertion report, not just
appear elsewhere in output. Missing, duplicate or malformed outcome records
fail closed; diagnostic/summary text alone is insufficient. Copied regressions
combine each intended mismatch with teardown cleanup failure, another assertion
failure or another item's setup error; none may count as successful detection. Same-field missing/invalid staged bytes,
argv, command status and cleanup observations must be classified as
`invalid-observation/setup`, even if baseline verification emits the target
case's diagnostic. `FIXTURE_OBSERVATIONS_ACQUIRED` may already be present, but
the baseline marker must not be. Traceback source snippets and duplicate marker
lines do not establish baseline confirmation. Setup/JSON errors and unrelated
baseline failures are not detections. Shared `verify_run` must require both
`staged_bytes` and `expected_staged_bytes` and validate them unconditionally.
Regression controls delete both fields or set both to `None`, in normal and
negative copied profiles; these must fail baseline validation before any
baseline-success marker, never count as a targeted mismatch, and preserve raw
failure status. There is no optional staged-evidence bypass. Staged-byte
observations here are captured Git blob
bytes, not proof of Docker transfer. Generated inputs and source-support hashes
are recorded separately in the copy inventories. Single-file orchestrator RED
contracts belong to this suite too; its interface is planned, not delivered:
`/home/aavasiljev/work/home/footman.git/scripts/verify_host_acceptance.py footman-sandbox:latest`.

`tests/host_orchestration_support.py` adds behavioral RED contracts for that
single-file interface to the same registered command. Missing-script assertions
precede preparation, so an absent implementation cannot masquerade as a passing
negative control or fixture error. Core executions select only a complete
prepared root by copied script location, with recorded synthetic HEAD, full
inventory, source/build-input identities and candidate-dist preservation. A
loaded-module probe patches only `_RESULTS_BASE` and `_execute_prepared_leg`;
it adds no production option/environment channel, never loads the future real
runner and never runs runner CLI main. Patched-base core contracts use
fixture-owned external bases; the separate unpatched contract below retains
the production literal. The missing-interpreter check separately executes the exact
shebang through `/usr/bin/env` from unrelated cwd and must fail with status 127
before script execution. The script-authored missing-uv prerequisite instead
requires a preserved nonzero status, a uv diagnostic and no image inspection,
build or prepared-leg execution; it does not prescribe a numeric exit code.
Separate RED contracts now pin unpatched default-base/direct-executable
behavior; loaded-module seam tests do not substitute for those contracts.
These bodies still stop at the missing-orchestrator assertion, before touching
the real base, and are not passing default-path execution evidence.

`tests/host_results_support.py` supplies test-only no-follow results inventories
(including caches and dangling links), explicit successful-reservation claims,
external evidence preservation and once-only confined cleanup. Loaded-module
probes persist successful immediate-child mkdir operations and their device/inode
identities to exclusive receipt files outside results cleanup targets, before
returning from the instrumented operation. Claims verify those receipt identities
inside the ledger's directory capability; neither a directory diff nor report.json
establishes ownership. Receipts survive lost/malformed stdout and completion
profile errors. The direct-executable leg runs the real shebang-selected Python
in a fresh external `venv --without-pip`, without installed Footman or inherited
PYTHONPATH. Normal interpreter `.pth` processing installs a stdlib-only mkdir
observer: no launcher, runpy, sys.path injection, test-support imports or Footman
preload supplies the orchestrator's bootstrap. Script argv, script-directory
search path and normal `__main__` execution remain interpreter-controlled. The
observer can write receipts through foundation capabilities only after the
script has itself loaded them; it never imports them on the script's behalf.
Earlier mkdirs remain unowned. Paired direct controls prove that importing
Footman without the script's own validated source bootstrap fails with the real
ModuleNotFoundError and is rejected by the direct contract's success check,
while a self-bootstrapped script imports from its prepared source and reserves
an attributable run. Separate early-mkdir and per-owner fault controls cover
missing authority, operation/close failure, interruption and repeated release.
This remains observation-instrumented Docker-free testing, not real-host proof.
Orchestrator source, constant and CLI remain unchanged. Both observers delegate
to the real mkdir first; failed exclusive mkdir emits no receipt. A failed
receipt write leaves unknown resources unowned, never inferred as owned from
newness.

The accounting fixture registers all ledgers (including explicit real-base
ledgers) before invocation. Execution, observation parsing and accounting remain
within that guaranteed fixture scope; accounting also runs in finally scopes
around execution even when core.invoke fails before returning observations.
Teardown attempts every independent reconciliation and reports unresolved cleanup.
Per-receipt failures do not prevent accounting for independent valid receipts;
unknown resources block deletion. Cleanup receives the preserved snapshot as a
mandatory allowlist, verifies copied bytes against its digests, preflights each
opened subtree and checks membership plus entry identity/mode/content/link target
before deletion. Fresh enumeration never grants deletion authority: concurrent
additions remain untouched, changed entries fail cleanup and repeat finish
rethrows the latched failure without retry. Deterministic post-copy, post-preflight
and pre-leaf controls include independent sibling cleanup and close failures.
These checks are not an atomic filesystem transaction against changes after the
last verification. Active failures/interruption retain precedence
over ordinary accounting errors, with cleanup failures surfaced separately.
Unknown changes, inaccessible inventories,
reservation losers and pre-existing operator runs grant no deletion authority.
Local fault controls exercise operation-plus-close, sole-close, interruption,
repeat and independent cleanup; deliberate cleanup failures retain evidence
before explicit fixture reconciliation. The unpatched contract uses the literal
shared base and obeys the real-base serialization exception in the scheduling
section: its gate and registered Phase-11 invocations must not overlap. Its direct
legs use an explicit PATH containing only fixture tools and approved isolated
bootstrap tools, not inherited PATH. Per-leg control records require the fixture
Docker image-inspection executable and exactly one controlled build (no calls
for unknown options); complete synthetic negative/normal case IDs, raw statuses
and counts are checked. These are prepared-root doubles, not real Docker proof
or a sandbox against arbitrary absolute executable paths. Test evidence remains
outside its exact deletion targets; operator evidence remains unowned and
retained for exact-path handoff.

Results inventories anchor recursive directory, regular-file and link reads to
open directory descriptors; leaf links use `readlink(..., dir_fd=...)` and are
never followed. The ledger records the results base's device/inode alongside its
initial inventory and checks that identity at snapshots, claims and cleanup
reopenings, as well as before/after removals. Base pathname binding is verified
by securely walking its parent without following ancestor links, then using a
no-follow child stat; matching leaf identity through a replacement ancestor
symlink is not accepted. Parent-check stat failures/interruption and close
failures follow sole-owner release and repeat-cleanup rules.
Replacing even an empty base, or
moving unchanged children into a replacement base, fails without claiming or
deleting the replacement's resources. Identity failure is terminal for ledger
cleanup; repeated cleanup reports the same failure without retrying releases.
The deliberate missing-base fixture starts its ledger only from the recorded
successful exclusive base-creation receipt, rather than rebinding a ledger for
a removed base. That receipt supplies the immutable base identity and proves an
initially empty base; each later run still needs its own successful reservation
claim, and any unclaimed descendant prevents cleanup.

`tests.host_results_support.read_accepted_report(path)` reads only the supplied
file through the existing sole-owner reader and validates a local completion
envelope: schema, final/success state, run-directory identity and absolute
repository-root attribution. It is shared with the unpatched positive contract,
not an acceptance function defined inside its own test. Local controls accept
the supplied record's distinct run/root identity even with a newer valid,
unrelated record present, and reject partial, incomplete, missing or inaccessible
supplied evidence without falling back. They test this shared helper only, not
the full independent candidate/provenance/resource verification required at
operator resumption or actual host-to-container mount access. The separate
orchestrator readback-denial probe remains an implementation-dependent RED
contract. No separate operator mount-confirmation declaration is required. Finalization/readback and
unsafe/unwritable/collision-base contracts remain RED until the orchestrator
is implemented. If the results base is absent, the orchestrator must create
it automatically through secure directory capabilities, then reserve a fresh
run directory. A file or symlink in its place is not a reusable base; denied
creation fails before Docker/build execution and grants no ownership. The
missing-base RED contract records successful base/run mkdir receipts and
preserves inventories and results before exact test-owned cleanup. Its separate
inert-marker readiness case exercises that driver without pretending the
orchestrator is delivered. No operator mkdir step or path override is required.

The core boundary receives one typed dataclass with `wheel`, `source_identity`
(the controlled manifest path), `image`, `evidence`, `workspace`, `profile`,
`suite_paths` and nullable `case_id`. The test double returns a structured value
with `pytest_status`, full separate `stdout`/`stderr` bytes, `counts` (collected,
passed, failed, skipped, xfailed, xpassed), `failure_kind`, `cleanup_complete`
and `evidence_complete`. These are private test-pinned Python interchange fields,
not a public runner CLI/API or a runner-process status. Reports retain raw
pytest statuses, a per-negative `detected` observation and the first unexpected
status separately. Orchestrator RED pins require `baseline_verified` to record
confirmation from the retained baseline marker, and `detected` to require that
confirmation plus the target diagnostic/count/status checks. Neither is inferred
from a matching diagnostic or the double's assertion failure kind alone.
A matching diagnostic without baseline confirmation must yield `detected=false`
and `failure_kind="invalid-observation/setup"`. This remains an unimplemented
orchestrator contract, not passing execution. Zero requires every targeted detection and the normal suite,
evidence and reconciliation. The double's outputs/counts are explicitly
synthetic, not observations of host tests or installation. The planned core
report uses `report.json`, schema 1, `state`, `success`, `first_unexpected_status`,
root/bootstrap/revision/both-identity attribution, `commands` with complete
stream paths and `legs` with raw `pytest_status` and detection observations.

`tests/host_orchestration_tool.py` is a recorded, inert Docker-inspection/build
boundary double in a separate child, never normal host support or a production
helper. It delegates reads/writes to existing sole owners, records exact argv,
cwd and support origin, and returns binary diagnostics/statuses. Its build
output is inert wheel-labelled bytes with a matching synthetic manifest: no uv
build, install or usable-wheel claim is made. No image build/pull or service
launch is possible through the scripted Docker double's exact inspect grammar.
Independent readiness tests execute these tools and the stdlib probe against an
inert marker without the absent orchestrator, distinguishing working fixture
setup from RED due to missing implementation. The registered core RED contracts
in this same suite pin tag/ID parsing, build/binding rejection, all four negative
legs plus the separate normal profile, unrelated/prerequisite/policy/zero-collection
failures, first-status precedence, independent leg attempts, incomplete
cleanup/evidence, interruption, wrong-root/missing foundation, runner-root refusal
before execution, import/env poisoning and fresh repeated evidence without
prior-artifact/report reuse. The orchestrator-dependent contracts currently fail
at the missing single-file interface assertion; their passing implementation
belongs to the implementation/integration tasks. These existing pins establish
neither passing orchestration execution nor real-host success.
Copied-support path insertion in the separate test-tool child is an attributed
stub-loader exception only; the orchestrator probe adds no second sys.path root,
and no test-tool bootstrap authority may reach an installed service.

`tests/host_runner_probe.py` is copied only by development runner-contract fixtures,
not normal real-host support. Its real SDK traffic uses copied transport and an
installed-wheel foundation, with the direct-wheel canonical argv (no --lifecycle),
clean service environment and archive-derived expectations. It deliberately
exercises stale/missing/extra/symlink/wrong-root sources, entry-source controls,
malformed/partial/duplicate/overlong/incomplete attestation and evidence read/write/
close/interruption faults. Synthetic parser terminal changes are labelled as such;
actual transport terminals and child status/cleanup remain separate observations.
Failure before versus after complete attestation is distinct from retained
provenance. Source/README/support mutations also target completion rechecks.

The probe is independently executed using the retained Phase-9 real-wheel fixture
before its use behind the future runner. Private mutable installation clones are
attributed as clones, not fresh installs. A separate isolated pytest fixture adds
locked pytest/xdist dependencies only to its private clone and executes positive,
skip/xfail/xpass/failure/empty controls with explicit config and host suite paths.
Nested policy violations remain deliberately unsuccessful inner commands, not
passing host acceptance. This validates fixture readiness, not the absent runner.

`tests/host_install_proxy.py` is a development tool spy, never an installed-wheel
client or production dependency. It delegates native uv through the existing
process owner without cleaning the received environment, records actual statuses/
full streams, and can alter a private installation before expectations are made
(including self-consistent stale RECORD data, stale __main__ and a swapped copy).
Its native failure/interruption/independent-delivery controls preserve authority.
The stub image lookup/retag controls are explicit development simulations; neither
they nor successful SDK traffic proves real Docker resolution/copy/removal.

The runner, not an operator's ambient pytest command, will invoke the explicit
copied `host_tests/` path with explicit isolated configuration, exactly one worker
setting and `--dist worksteal`. It must reject zero collection, skips, xfails,
xpasses, missing prerequisites and unresolved cleanup. Host execution requires
an operator-prepared local image and a Docker-capable host; no automatic image
build/pull or development-container socket access is permitted. Docker-free
contracts do not prove real Docker semantics or complete task 11.2's real-host
negative control. All RED tasks must complete before GREEN task 11.3; production
static roots/ty scope remain unchanged during this RED work.

### Recorded wrapper contract formats

The RED tests pin concrete evidence layouts for implementation, in addition to
the CLI interfaces above. These are test/build tooling contracts, not new Footman
runtime result fields or production MCP capabilities:

- A successful schema-1 build manifest records `revision`, `repository_root`,
  `wrapper_path`, `caller_cwd`, `source_identity`, `source_inventory`,
  `build_input_identity`, `build_input_inventory`, and exact `wheel_path`/
  `wheel_sha256` plus `sdist_path`/`sdist_sha256`. Source identity roots are
  `src/`, `tests/`, `host_tests/`, `scripts/`, `pyproject.toml`, `uv.lock`; the
  independent build identity has exactly `src/`, `pyproject.toml`, `README.md`.
  Both identity objects use schema_version=1, ordered roots and lowercase SHA-256
  over sorted relative-path/kind/executable/source-digest/link-target records.
- Each build invocation retains `<manifest-stem>-operation.json` beside its
  requested manifest, including failures/refusals. It records `argv`, `cwd`,
  `attempted`, nullable observed `status`, raw `stdout_path`/`stderr_path` beneath
  that recorded evidence directory when attempted, and separate `post_check_error`
  when applicable. No attempted build means no fabricated status/empty streams;
  a nonzero build's original status and binary output survive a later input error.
- The runner retains `report.json` under --evidence, recording success/cleanup,
  root/caller/wrapper attribution, both identities and wheel digest, external
  venv/workspace/expected-origin/provenance paths, and `commands` with literal argv,
  cwd, actual status and environment key names (not unrelated environment values).
  Installed help/import, dependency installation and host pytest cannot inherit
  bootstrap PYTHONPATH. Generated expectations/provenance stay outside identity
  roots; module hashes come from the bound archive, never installed RECORD.
- Isolated host pytest receives `FOOTMAN_HOST_CONTEXT`, an external generated
  JSON path containing `venv_python`, `server_script`, `support_root`,
  `expected_origin_path`, `provenance_evidence_path`, `neutral_cwd` and
  `configured_image_id`. This is runner-to-fixture input, never passed to the
  service. The immediate parent alone writes/reads back final verified evidence.
  `FOOTMAN_HOST_CONTRACT_*` and `FOOTMAN_HOST_TOOL_*` are development probe controls,
  not public service options; probes remove all host-context/control keys before
  launching the service. Normal real-host support excludes these probe/proxy files
  and the relocated Docker-stub bundle used only by isolated development controls.

Phase-11 development validation adds this registered two-worker command after
the eleven earlier specialised commands, using the same shared eight-worker
specialised budget and CPU-capacity condition. Pending RED failures are not a
passing gate and do not invalidate the historical Phase-10 result as history.
Record exact commands, full output, statuses, reached assertions and limitations
in the configured external verification directory. Do not mark a partial task
complete or interpret absence of Docker as valid behavioral RED evidence.

## Future implementation

Follow the change's RED → GREEN → INTROSPECT → VALIDATE phase gates. Register each
new phase suite's exact command here before recording RED. Phase suites may
select narrower test paths but do not replace the mandated full-suite G run.
Every fenced pytest suite command must include `-n <N>` (an integer from 1
through 1024) and `--dist worksteal`, with no disabled xdist or alternate worker
count override. Document the exact mandated full-suite command once; additional
parallel phase-suite examples are allowed. There is no fence-label bypass for
serial commands. Intentionally serial policy probes remain in test code, not
verification-suite examples. See `docs/python-checks.md` for the same contract.
Development phases 1–10 run inside the Docker-free development container:
`tests/` is the complete development suite, using disposable real Git repositories,
a real SDK MCP client, and the controlled Docker CLI executable stub.
No Docker socket, daemon, image or npm is required for this gate. The stub records
literal argv and fixture-owned identities and scripts responses, bytes, delays
and failures; it does not prove actual container behavior.

Phase 11 separately introduces `host_tests/` and the mandatory real-Docker host
acceptance gate against an exact installed wheel and an operator-prepared image.
That suite is not part of development discovery; missing host prerequisites fail
host acceptance, never become skips. Passing development checks does not establish
final host acceptance. Until phase 11 creates that package, static roots remain
`src/`, `tests/`, `scripts/`, and ty checks `src/` only.
Never exercise index mutation or broad container cleanup against this repository;
use disposable repositories and recorded fixture-owned container IDs.

Capture complete stdout/stderr temporarily and preserve runner exit status.
Nonzero checks are unsuccessful verification except explicitly expected, recorded
RED failures as described above; retain warnings and explain missing prerequisites. Store verification reports, handoffs and evidence outside the
repository under `/tmp/gorshochek/ephemeral/footman.git/` (verification reports in
its `verification/` subdirectory). This is ephemeral, machine-local storage, not
Git history: its contents may disappear and quickly become stale. Do not commit
these current-candidate reports or rely on their continued existence for project
setup. The explicitly retained [worksteal adoption record](testing-worksteal.md)
is a historical summary, not a live verification certificate or a requirement to
retain its full machine-local logs.

Each evidence summary must remain self-contained: revision/local changes, exact
commands, environment, statuses, test counts, warnings and limitations. Recheck
applicability before reuse; historical success is not current verification.
Permanent report storage is not required. If a summary is missing or stale,
rerun the affected checks and recreate it before relying on completion claims.
Do not invent lost historical RED evidence; reproduce the documented negative
control when needed. This phase-report policy does not change the dispatcher's
runtime retention contract for `result.json` or command logs. Do not claim
passing scaffold tests prove MCP, snapshot, cleanup or result-contract acceptance.
