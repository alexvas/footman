# Python source and green-gate checks

One deterministic inventory feeds Ruff linting, Ruff formatting and syntax
compilation for the repository-owned Python source. The tooling lives in
[`scripts/python_checks.py`](../scripts/python_checks.py); the entry points
below are thin wrappers that run it with the project virtual environment. The
canonical pytest commands are in [the testing guide](testing.md).

Run everything from the repository root.

## Installation

The check tools are exact pins in the `dev` extra, so no check depends on an
unrecorded global installation:

```sh
uv sync --extra dev --locked    # creates/updates .venv from uv.lock
```

That installs `pytest==8.4.2`, `pytest-xdist==3.8.0`, `ruff==0.17.1` and
`ty==0.0.73`. `pyproject.toml` also pins Ruff's `required-version = "==0.17.1"`,
so a different Ruff refuses to run instead of silently checking with another
policy. Every entry point requires its interpreter or tool under `.venv/bin` and
exits `127` with a diagnostic when it is missing. Inside the tooling, Ruff is
resolved from the interpreter running it (`.venv/bin/ruff`) first and only then
from `PATH`. Python 3.14+ and `uv` are prerequisites.

## Commands

| Purpose | Command |
| --- | --- |
| Run the whole gate | `scripts/check-green` |
| Check lint (read-only) | `scripts/check-python-lint` |
| Check formatting (read-only) | `scripts/check-python-format` |
| Compile every selected source | `scripts/check-python-compile` |
| Type check the package | `scripts/check-types` |
| Print the inventory (diagnostic) | `.venv/bin/python scripts/python_checks.py list` |

- Each entry point returns the underlying check status. A finding is a
  nonzero status; it is never rewritten as success by a pipe, wrapper or
  summary.
- `--root <directory>` runs the same checks against another repository root
  that has `src/`, `tests/` and `scripts/`. The tooling contract tests use it
  for fixture trees.
- `ruff check` and `ruff format --check` run with the repository
  configuration, `--no-cache`, and no fixing flags. Check mode never rewrites
  a source file, and compilation never writes bytecode.

## Source selection rules

- **Roots:** `src/`, `tests/`, `scripts/`. A missing root is an error, never
  an empty inventory.
- **Included:** every `.py` and `.pyi` file under those roots, plus every
  extensionless file whose first line is a recognized Python shebang. A
  recognized shebang is either a direct interpreter path whose final component
  is `python`, `python3`, or a versioned `python3.<minor>` name
  (`#!/usr/bin/python3.14`), or `/usr/bin/env` followed by one of those names
  (`#!/usr/bin/env python3`). Trailing arguments are ignored. The selector does
  not parse a shell command, interpret other `env` options, or execute the
  interpreter. A Python script without a recognizable shebang must be listed in
  `EXPLICIT_PYTHON_SOURCES` in `scripts/python_checks.py`; an unlisted
  extensionless file with no shebang is an error, not a silent omission. The
  repository's own `scripts/check-*` wrappers are `#!/bin/sh` and are therefore
  recorded as non-Python exclusions, not dropped silently.
- **Excluded, each with a recorded reason:** `__pycache__`, `.git`,
  `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.venv`, `vendor` and
  `generated` directories; symbolic links are never followed, and a symlinked
  `.py`/`.pyi` file is an error; non-Python file extensions and non-Python
  shebangs are not Python source.
- `openspec/**` and `docs/**` planning material is outside the scoped roots.
- **Same inventory for Ruff.** The selected paths are passed to Ruff
  explicitly, including extensionless scripts, because Ruff's default recursion
  only discovers `.py`/`.pyi`. Before running, the tooling compares the
  inventory with default `ruff check --show-files` discovery: it rejects a
  configuration exclusion that hides a selected module, while discovery beneath
  `vendor`, `generated` or cache directories is ignored because those paths are
  intentionally outside the shared inventory. It then validates coverage for
  the requested mode: lint uses `ruff check --show-files` with the explicit
  inventory, and format uses the read-only
  `ruff format --check --verbose` processed-file report. Every selected path,
  including extensionless scripts, must appear in the mode report; otherwise
  the path is named and the check fails. This catches `force-exclude` and
  `[tool.ruff.format].exclude` overrides even when Ruff is given explicit
  filenames.
- **Compilation is a syntax check.** It runs `compile()` on the raw source
  bytes, so a source-encoding declaration is honoured, no module code is
  imported or executed, and no bytecode is left in the source tree. The tooling
  disables bytecode writing before importing its own repository support
  modules, so direct CLI and wrapper execution cannot create `__pycache__`
  entries while loading those modules.
- **Deterministic output.** Paths are sorted, repository-relative POSIX paths
  without duplicates.
- **Tooling ownership.** Each regular-file read adopts one `OwnedDescriptor`,
  each traversed directory one `DirectoryDescriptor` capability, and every
  external command one `OwnedProcess` with one internal stream owner per
  captured pipe, per [filesystem-lifecycle.md](filesystem-lifecycle.md) and
  [process-lifecycle.md](process-lifecycle.md). The contract tests for those
  three branches live in `tests/test_python_check_lifecycle.py`.

## Permanent green gate (G)

Before delivering a GREEN/INTROSPECT/VALIDATE candidate state and before any
commit, run:

```sh
scripts/check-green
```

The gate runs `scripts/check-python-lint`, `scripts/check-python-format`,
`scripts/check-python-compile`, `scripts/check-types`, and the exact full-suite
parallel pytest command in [testing.md](testing.md). It attempts every
independent check and returns the first failing status; an incomplete process
cleanup also fails, and an interruption keeps its identity. No scope or fix
arguments are supported: `scripts/check-green --fix` exits nonzero rather than
narrowing the gate. `tests/test_green_gate.py` pins the commanded plan, the
worker-count range and the preserved statuses.

G additionally requires the skip review in [testing.md](testing.md): no skips,
xfails or xpasses, and no approved skip exceptions in this repository. A phase
suite's documented RED recording stays exempt from G, as the testing guide
describes.

The gate also requires the two installation checks the testing guide records
(`uv build` and the wheel-installation smoke test) whenever package contents or
build metadata change.

### Parallel test running

The mandated suite command uses **work stealing** across eight workers:

```sh
.venv/bin/python -m pytest tests -n 8 --dist worksteal --tb=short -q
```

Document the exact mandated full-suite command above once in each testing
guide or README. Additional phase-suite commands may select narrower test
paths for RED/regression recording, but do not replace G. Every fenced pytest
suite example must use `-n <N>` (an integer from 1 through 1024) and
`--dist worksteal`, without disabling xdist or overriding the worker count with
another option. Bare serial suite commands and fence-label bypasses are not
accepted; intentionally serial policy probes belong in test code, not as
verification-suite examples.

`--dist worksteal` redistributes pending tests from busier workers instead of
pinning whole files to one worker. Tests in a module may run in different
interpreters, and module/session fixtures can initialize in multiple workers.
Do not rely on file-wide execution order, mutable module state shared between
tests, or globally unique session fixtures. Module-scoped clocks are local to
one worker; tests must establish their own timing assumptions and isolate
mutable resources. `tests/conftest.py` and `tests/green_skip_policy.py` run in
each worker, and the controller observes every report, so a skip, xfail or xpass
produced in a worker blocks the session exactly as in a serial run.
`tests/test_green_skip_policy.py` proves this under both serial and worksteal
invocations. See the [historical adoption record](testing-worksteal.md) for the
1000-run stability experiment and the deferred I/O investigation.

`pytest-xdist` is a required development tool, not an optional accelerator: if
it is missing, a parallel invocation fails at argument parsing instead of
degrading to a serial run.

`scripts/check-green` uses eight workers by default. `GREEN_CHECKS_WORKERS`
changes only the pytest parallelism, never the suite or the other checks, and
accepts any integer from `1` through `1024`; an invalid value fails before any
check runs:

```sh
GREEN_CHECKS_WORKERS=4 scripts/check-green
```

When validation also requires specialised phase suites, use the
[working schedule in the testing guide](testing.md#working-schedule-gate-plus-specialised-suites):
on machines with at least 32 available logical CPUs (respecting affinity and CPU
quotas), the gate's eight workers run alongside a separate eight-worker budget
for the exact registered specialised commands. Below that capacity, or when it
cannot be established, run gate then specialised commands sequentially. Neither
mode narrows this gate or replaces any required command, status or cleanup check.

### Environmental prerequisites instead of skips

Some contracts are only meaningful for an unprivileged user (the unreadable
`mode-000` source probes in `tests/test_python_source_selection.py` and
`tests/test_python_syntax_compilation.py`) and some require the project
virtual environment. Those are recorded as prerequisites: the test fails with
an actionable message instead of skipping, because this repository has no
approved skip exceptions. The complete `tests/` development gate is Docker-free:
phases 1–10 use real disposable Git repositories and a controlled Docker CLI stub,
not Docker access or npm. The MCP protocol suites use real SDK clients; only
Docker is replaced at the CLI boundary.
Phase 11 adds separately discovered `host_tests/` and mandatory real-Docker host
acceptance against an exact installed wheel and an operator-prepared image;
development success cannot replace that gate. Missing host prerequisites fail
host acceptance rather than skipping. This boundary is currently documentation
only: retain the three source roots and `src/`-only ty scope until phase 11
creates the host package and updates the inventories together.

### Capturing complete gate output

For temporary complete diagnostic retention, run in a separate Bash shell:

```sh
log=$(mktemp /tmp/footman-green.XXXXXX.log)
scripts/check-green >"$log" 2>&1
status=$?
cat "$log"
printf 'Temporary log: %s; gate exit: %s\n' "$log" "$status"
exit "$status"
```

Do not use a `tee`/filter/upload exit status as the gate result. Keep the full
log while diagnosing and preparing the evidence summary. Verification reports
live outside the repository under
`/tmp/gorshochek/ephemeral/footman.git/verification/` as the testing guide
records; neither summaries nor logs are committed or required permanently.

## Type checks

`scripts/check-types` runs `ty check --output-format concise src` with the
Python 3.14 semantics configured in `pyproject.toml`
(`[tool.ty.environment].python-version` and `[tool.ty.src].include = ["src"]`).
The checked tree is the maintained package only: `tests/` typing is not part of
the gate yet, which is a recorded scope boundary, not a filter or a skip.
`tests/test_green_gate.py` rejects an entry point that narrows the typed tree
below the declared include list.

## Justified specific suppressions

A suppression is acceptable only when the necessary construct cannot be
expressed without changing an enforced contract. Use the narrowest code, place
the rationale next to the directive, and record the accepted reason here.
Blanket per-file suppressions and rule disabling are not accepted.

The reviewed directives currently in force (locations identify the suppressed
constructs; line numbers may move):

- **`B904` in `src/footman/process.py`** at `raise result` in launch cleanup
  and `raise exc` in context exit: preserve the authoritative cleanup cause and
  secondary diagnostics, or the original non-Exception primary unchanged.
  Adding exception chaining would rewrite its lifecycle context.
- **`B023` in `src/footman/process.py`** at the `StopContext` construction:
  the action runs within that loop iteration; binding the loop values as
  parameters would change the enforced callback signature.
- **`B905` in `src/footman/process.py`** at the zip calls in
  `_complete` and `_continue_cleanup` pairing `self._action_prepared` with
  `self._action_results` (action-prepared flags and action results), and in
  `_snapshot_locked` pairing `ProcessStream` with `self._outcomes`: these pair
  fixed action slots or enum-defined outcome slots, not independent
  variable-length inputs.
- **`B010` in `src/footman/process.py`** at both `setattr(error, "cleanup", self)`
  calls: dynamic cleanup-observation attachment is intentional; the type checker
  rejects direct attribute assignment on `BaseException`.
- **`E402` in `scripts/green_checks.py`** at the `python_checks` import and
  **`E402` in `scripts/python_checks.py`** at the descriptors, operations and
  process imports: CLI import-path preparation must happen first so the tools
  run from a checkout or copied tool tree without an installed package.

The `# noqa: F401` on `import footman.filesystem.cleanup` inside
`tests/test_filesystem_cleanup.py`'s `_ISOLATED_PROBE` string is probe-source
text, not an in-force directive in the checked inventory: Ruff (including
RUF100) does not evaluate string contents. It records the probe's intentional
import-for-side-effect if that generated source is linted separately.

Any new suppression must be added to this ledger with its specific rationale,
with the rationale also placed next to the directive. Review existing entries
when moving or removing directives; do not remove load-bearing suppressions
merely because their constructs are unusual.

## No hosted continuous integration yet

This repository has no push/PR workflow, so the local gate above is the only
enforcement point; no hosted run is claimed. A future workflow would provision
Python 3.14 and `uv sync --extra dev --locked`, then run the same commands
without write-mode fixes.
