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

## Current scaffold

Prerequisites: Python 3.14+ and `uv`; package-index access for the first install.
Run from the repository root:

```sh
uv sync --extra dev --locked
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
uv build
uv run --locked footman --help
uv run --locked python -m footman --help
scripts/check-green
```

The pytest command is the mandated full-suite run: `pytest-xdist` distributes
whole files (`--dist loadfile`) across eight workers, so module-level fixtures
and import-time state never cross processes. Use this command, not a serial or
narrowed variant, for G; `docs/python-checks.md` records the parallelism rules
and the gate entry points. If a task explicitly requires a serial `unittest
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
scaffold suite superseded by phase 8. The phase 8 protocol suite adds coverage
for valid MCP startup; it does not supersede missing-file startup refusal.

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
.venv/bin/python -m pytest tests/test_foundation.py tests/test_foundation_ownership.py tests/test_scaffold.py -n 2 --dist loadfile --tb=short -q
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
env -u PYTHONPATH "$smoke/venv/bin/python" -m pytest "$repo/tests/test_foundation.py" "$repo/tests/test_foundation_ownership.py" "$repo/tests/test_scaffold.py" -n 2 --dist loadfile --tb=short -q
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
.venv/bin/python -m pytest tests/test_configuration.py tests/test_scaffold.py -n 2 --dist loadfile --tb=short -q
```

The missing-file startup replacement alone can be recorded before the remaining
configuration tests are authored:

```sh
.venv/bin/python -m pytest tests/test_scaffold.py -n 2 --dist loadfile --tb=short -q
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
.venv/bin/python -m pytest tests/test_output_reservation.py -n 2 --dist loadfile --tb=short -q
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
.venv/bin/python -m pytest tests/test_snapshot.py -n 2 --dist loadfile --tb=short -q
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
.venv/bin/python -m pytest tests/test_launcher.py tests/test_launcher_execution.py tests/test_launcher_faults.py tests/test_launcher_boundaries.py -n 2 --dist loadfile --tb=short -q
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
consumer code on the host. Phase 7 adds persistence, Phase 8 MCP responses,
and Phase 10 actual Docker copy/execution behavior. Phase 6 adds recorded-ID
domain cleanup; settled local stub children do not prove container removal.

For validation also run every earlier documented phase suite, the unscoped
full green gate, build/help and exact-wheel installation checks. Infrastructure
summaries preserve raw sensitive prefixes; see [configuration.md](configuration.md)
for copy prerequisites, non-root permissions and all five files' access/retention.

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
bare `assert` statements are removed by optimization. In tests themselves, use
ordinary `assert` statements to retain pytest assertion introspection.

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

## Future implementation

Follow the change's RED → GREEN → INTROSPECT → VALIDATE phase gates. Register each
new phase suite's exact command here before recording RED. Phase suites may
select narrower test paths but do not replace the mandated full-suite G run.
Every fenced pytest suite command must include `-n <N>` (an integer from 1
through 1024) and `--dist loadfile`, with no disabled xdist or alternate worker
count override. Document the exact mandated full-suite command once; additional
parallel phase-suite examples are allowed. There is no fence-label bypass for
serial commands. Intentionally serial policy probes remain in test code, not
verification-suite examples. See `docs/python-checks.md` for the same contract.
Development phases 1–9 run inside the Docker-free development container:
`tests/` is the complete development suite, using disposable real Git repositories,
a real MCP client when delivered, and the controlled Docker CLI executable stub.
No Docker socket, daemon, image or npm is required for this gate. The stub records
literal argv and fixture-owned identities and scripts responses, bytes, delays
and failures; it does not prove actual container behavior.

Phase 10 separately introduces `host_tests/` and the mandatory real-Docker host
acceptance gate against an exact installed wheel and an operator-prepared image.
That suite is not part of development discovery; missing host prerequisites fail
host acceptance, never become skips. Passing development checks does not establish
final host acceptance. Until phase 10 creates that package, static roots remain
`src/`, `tests/`, `scripts/`, and ty checks `src/` only.
Never exercise index mutation or broad container cleanup against this repository;
use disposable repositories and recorded fixture-owned container IDs.

Capture complete stdout/stderr temporarily and preserve runner exit status.
Nonzero checks are unsuccessful verification except explicitly expected, recorded
RED failures as described above; retain warnings and explain missing prerequisites. Store verification reports, handoffs and evidence outside the
repository under `/tmp/gorshochek/ephemeral/footman.git/` (verification reports in
its `verification/` subdirectory). This is ephemeral, machine-local storage, not
Git history: its contents may disappear and quickly become stale. Do not commit
these reports or rely on their continued existence for project setup.

Each evidence summary must remain self-contained: revision/local changes, exact
commands, environment, statuses, test counts, warnings and limitations. Recheck
applicability before reuse; historical success is not current verification.
Permanent report storage is not required. If a summary is missing or stale,
rerun the affected checks and recreate it before relying on completion claims.
Do not invent lost historical RED evidence; reproduce the documented negative
control when needed. This phase-report policy does not change the dispatcher's
runtime retention contract for `result.json` or command logs. Do not claim
passing scaffold tests prove MCP, snapshot, cleanup or result-contract acceptance.
