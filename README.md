# Privileged test dispatcher

Standalone Python project for executing an operator-configured command against a
staged Git snapshot through stdio MCP.

**Under development.** Not all described capabilities are implemented.
Implementation progress is tracked in the [change tasks](openspec/changes/add-privileged-test-dispatcher/tasks.md);
verification results are recorded in evidence reports. See [testing documentation](docs/testing.md)
for their storage location and applicability rules; these temporary reports may
not be available.

See [operator configuration](docs/configuration.md) and the
[annotated example](examples/footman.toml) for configuration details.

## Development

Requires Python 3.14+ and `uv`. Run identity uses the standard-library
`uuid.uuid7()`; no UUID compatibility implementation is required.

```sh
uv sync --extra dev --locked
uv run --locked footman --help
.venv/bin/python -m pytest tests -m 'not sleepy and not auto_stress' -n auto --dist worksteal --tb=short -q
.venv/bin/python -m pytest tests -m 'auto_stress' -n 8 --dist worksteal --tb=short -q
.venv/bin/python -m pytest tests -m 'sleepy and not auto_stress' -n 8 --dist worksteal --tb=short -q
scripts/check-green
```

The three pytest groups form the mandated full-suite run: execute them in order,
stop at the first nonzero status, and retain each group's complete output.
`pytest-xdist` uses work stealing with `auto`, eight and eight workers; tests in one file need not share a worker or
fixture instance. `scripts/check-green` is the permanent gate: it adds the Ruff
lint/format, syntax-compilation and `ty` checks over one Python source inventory
and returns the first failing status; see [docs/python-checks.md](docs/python-checks.md).
The [worksteal adoption record](docs/testing-worksteal.md) documents the historical
1000 consecutive full-suite runs and the pending I/O investigation.

The server entry point is:

```sh
footman --config /absolute/path/config.toml
```

`footman` is the process entry point used by an MCP client to start the stdio
service, not an interface for directly executing tests. There are no `run` or
`mcp` subcommands. The dispatch interface is the MCP
`run_privileged_command` tool, not a direct command-execution CLI. See
[MCP client setup](docs/mcp.md) for arguments, retained paths, restart semantics
and publication-error handling.

## Layout

- `pyproject.toml`, `uv.lock`: package/build metadata and locked dependencies.
- `src/footman/`: package and operator entry point.
- `src/footman/filesystem/`, `src/footman/process.py`: descriptor, stream and subprocess helpers ported from Jigbox.
- `scripts/`: green gate and read-only source/type check entry points.
- `tests/`: Docker-free dispatcher, real MCP stdio, foundation/lifecycle, check-tooling and verification-policy suites. Disposable Git repositories and controlled Docker CLI peers isolate execution; installed-wheel client/service fixtures check package attribution. See [docs/testing.md](docs/testing.md).
- `tests/test_configuration.py`: configuration, request-boundary and startup-lifetime suite.
- `docs/configuration.md`: operator configuration schema and authority.
- `examples/footman.toml`: annotated operator configuration example.
- `docs/testing.md`: verification commands and limitations.
- `docs/python-checks.md`: source inventory, tool installation, parallel test running and the gate contract.
- `openspec/changes/add-privileged-test-dispatcher/`: implementation contract.

This repository is the standalone implementation project. No further transfer
of the planning artifacts is required. See [testing documentation](docs/testing.md)
for test prerequisites and evidence requirements.

### Descriptor and stream lifecycle

Before adding code that owns a descriptor or stream, read the contract and illustrative examples in [docs/filesystem-lifecycle.md](docs/filesystem-lifecycle.md). Every new ownership path must name one shared `footman.filesystem` owner and add lifecycle tests; the mandatory rule is also recorded in the root [AGENTS.md](AGENTS.md).

For subprocess coordination, use `OwnedProcess` and the launch-configured policies in [docs/process-lifecycle.md](docs/process-lifecycle.md), rather than independent pipe or wait/reap cleanup. The guide distinguishes bounded shutdown from incomplete cleanup and local child exit from managed-action settlement.
