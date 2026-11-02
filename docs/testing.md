# Testing

## Green gate (G)

A successful full pytest run requires no skips, xfails or xpasses, including
collection and fixture-setup skips. `tests/green_skip_policy.py`, registered in
`tests/conftest.py`, reports each violation and makes an otherwise successful
session exit nonzero. There are currently no approved skip exceptions. Missing
prerequisites must be resolved, not hidden by skipping tests. Existing failures
or interruptions retain their nonzero exit status. `tests/test_green_skip_policy.py`
checks the policy through isolated pytest subprocesses.

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
uv run --locked python -m pytest
uv build
uv run --locked footman --help
```

`tests/test_scaffold.py` checks package import, startup argument parsing, absence
of direct-dispatch subcommands and explicit failure of the unimplemented server.
These tests do not establish phase 1 completion or any dispatcher capability.
In particular, Docker isolation fixtures and RED evidence have not been delivered.

The assertion in `test_scaffold_fails_explicitly_without_protocol_output` is a
temporary stub check, not a permanent configuration contract. Phase 2 explicitly
replaces it with a missing-file process test using a guaranteed absent path under
`tmp_path`: require nonzero exit, empty stdout and nonempty stderr identifying a
configuration-load failure, not the generic not-implemented error. Do not pin an
exact nonzero code or full diagnostic wording. Record the replacement's RED
failure against the stub and make it GREEN with the loader. Keep a separate
process test for invalid fields in an existing TOML file. Do not create a valid
TOML merely to assert startup failure: valid service startup need not fail.

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

## Writing verification helpers

In verification helpers and pytest plugins, use explicit
`raise AssertionError(...)` for checks that must remain active under `python -O`;
bare `assert` statements are removed by optimization. In tests themselves, use
ordinary `assert` statements to retain pytest assertion introspection.

## Verification guard suites

The full-suite command includes:

- `tests/test_green_skip_policy.py`: isolated pytest runs proving skip/xfail/xpass
  rejection and preservation of existing failures and interruptions.
- `tests/test_lifecycle_doc_links.py`: relative file links and Markdown heading
  anchors in both lifecycle guides, with injected broken links/anchors in scratch
  copies. Anchors use lowercase ATX-heading slugs, stripped punctuation and
  duplicate-heading suffixes; fenced code is ignored. Explicit HTML anchors and
  non-Markdown fragment targets are unsupported and must not be used in these
  guides. Remote URLs are not checked for availability.

## Future implementation

Follow the change's RED → GREEN → INTROSPECT → VALIDATE phase gates. Register each
new phase suite's exact command here before recording RED. Do not skip required
real MCP client or Docker tests. Runtime Git/Docker and an operator-prepared image
will be required for integration tests, but not for current scaffold tests.
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
