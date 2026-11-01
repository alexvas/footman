# Testing

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

For a non-editable installation smoke check, build a wheel and install that exact
wheel into a fresh virtual environment, then invoke its executable with `--help`
and run `python -m footman --help` outside the source checkout.
Do not treat editable-package import success as wheel-installation verification.

## Future implementation

Follow the change's RED → GREEN → INTROSPECT → VALIDATE phase gates. Register each
new phase suite's exact command here before recording RED. Do not skip required
real MCP client or Docker tests. Runtime Git/Docker and an operator-prepared image
will be required for integration tests, but not for current scaffold tests.
Never exercise index mutation or broad container cleanup against this repository;
use disposable repositories and recorded fixture-owned container IDs.

Capture complete stdout/stderr temporarily and preserve runner exit status.
Nonzero checks are unsuccessful verification; retain warnings and explain missing
prerequisites. Store verification reports, handoffs and evidence outside the
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
