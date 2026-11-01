# Privileged test dispatcher

Standalone Python project for executing an operator-configured command against a
staged Git snapshot through stdio MCP.

**Status:** project scaffold only. Snapshot capture, Docker lifecycle, output
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
uv run --locked python -m pytest
```

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
- `tests/`: scaffold tests; add behavior-specific suites during implementation.
- `docs/testing.md`: verification commands and limitations.
- `openspec/changes/add-privileged-test-dispatcher/`: implementation contract.

This repository is the standalone implementation project. No further transfer
of the planning artifacts is required. See [testing documentation](docs/testing.md)
for test prerequisites and evidence requirements.
