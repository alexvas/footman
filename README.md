# Privileged test dispatcher

Standalone Python project for executing an operator-configured command against a
staged Git snapshot through stdio MCP.

**Status:** dispatcher scaffold with ported filesystem and process helpers.
These helpers do not establish completion of any dispatcher implementation phase.
Snapshot capture, Docker lifecycle, output
retention and the MCP server are not implemented. Starting with `--config`
currently fails explicitly without reading the file or executing any command.
The MCP SDK will be selected and pinned when protocol implementation begins;
there are no runtime dependencies yet.

## Development

Requires Python 3.14+ and `uv`. The planned run identity uses the standard-library
`uuid.uuid7()`; no UUID compatibility implementation is required.

```sh
uv sync --extra dev --locked
uv run --locked footman --help
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
scripts/check-green
```

The pytest command is the mandated full-suite run: `pytest-xdist` distributes
whole files across eight workers. `scripts/check-green` is the permanent gate: it
adds the Ruff lint/format, syntax-compilation and `ty` checks over one Python
source inventory and returns the first failing status; see
[docs/python-checks.md](docs/python-checks.md).

The planned server invocation (not yet functional) is:

```sh
footman --config /absolute/path/config.toml
```

`footman` is the process entry point used by an MCP client to start the stdio
service, not an interface for directly executing tests. There are no `run` or
`mcp` subcommands. Requests will be made through the MCP
`run_privileged_command` tool, not a direct command-execution CLI.

## Layout

- `pyproject.toml`, `uv.lock`: package/build metadata and locked dependencies.
- `src/footman/`: package and operator entry point.
- `src/footman/filesystem/`, `src/footman/process.py`: descriptor, stream and subprocess helpers ported from Jigbox.
- `scripts/`: green gate and read-only source/type check entry points.
- `tests/`: scaffold, helper lifecycle, check-tooling and verification-policy suites; see [docs/testing.md](docs/testing.md).
- `docs/testing.md`: verification commands and limitations.
- `docs/python-checks.md`: source inventory, tool installation, parallel test running and the gate contract.
- `openspec/changes/add-privileged-test-dispatcher/`: implementation contract.

This repository is the standalone implementation project. No further transfer
of the planning artifacts is required. See [testing documentation](docs/testing.md)
for test prerequisites and evidence requirements.

### Descriptor and stream lifecycle

Before adding code that owns a descriptor or stream, read the contract and illustrative examples in [docs/filesystem-lifecycle.md](docs/filesystem-lifecycle.md). Every new ownership path must name one shared `footman.filesystem` owner and add lifecycle tests; the mandatory rule is also recorded in the root [AGENTS.md](AGENTS.md).

For subprocess coordination, use `OwnedProcess` and the launch-configured policies in [docs/process-lifecycle.md](docs/process-lifecycle.md), rather than independent pipe or wait/reap cleanup. The guide distinguishes bounded shutdown from incomplete cleanup and local child exit from managed-action settlement.
