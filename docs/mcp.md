# MCP client setup

Install Footman in the operator's Python 3.14+ environment and prepare the
[operator-owned TOML configuration](configuration.md). The service entry point
is only:

```sh
footman --config /absolute/operator/config.toml
```

A stdio MCP client starts that process and owns its connection. A typical client
configuration is:

```json
{"mcpServers":{"footman":{"command":"/absolute/venv/bin/footman","args":["--config","/absolute/operator/config.toml"]}}}
```

Use your client's stdio transport; `tests/mcp_transport.py` is repository test
infrastructure, not a production client dependency. There are no `run` or `mcp`
subcommands, general host-command tool or request-level configuration selector.
Only JSON-RPC protocol messages go to stdout. Generated diagnostics and logging
go to stderr; inherited stdio handles remain runtime-owned and are borrowed by
[explicit UTF-8 facades](process-lifecycle.md#inherited-stdio-and-diagnostics).

## Tool arguments

Discover `run_privileged_command`. Its complete input schema is:

```json
{"type":"object","properties":{"phase":{"type":"string","pattern":"^[A-Za-z0-9_-]+$"}},"additionalProperties":false}
```

Call it with `{}` when the output template does not require a phase, or with
`{"phase":"review_2"}`. The phase is an organization label, not a command
selector. It changes neither argv nor execution identity. If `output_dir`
contains `{phase}`, `{}` is rejected. Empty, non-string or unsafe phase labels
and extra fields are rejected before output reservation, snapshot capture or
Docker execution. All paths, image, command, user and budgets come from TOML.

## Startup versus request failure

Configuration loads and validates once before serving requests. Missing or
statically invalid TOML exits nonzero with stderr diagnostics, no protocol stdout
and no run output, snapshot, container or configured-command side effects.
Editing TOML has no effect on the running process: restart to load a new file.
A restart also refuses a newly invalid file.

A later reservation collision, disappeared/replaced storage path, invalid
container identity/running state or failed snapshot copy is instead a request
failure, with available retained evidence and applicable cleanup. It does not
reload configuration or justify retrying launch/copy automatically.

## Reading results

The structured tool result has `outcome`, nullable configured-command
`return_code`, and absolute paths for files actually created. Normal retained
runs include `stdoutPath`, `stderrPath`, `startupStdoutPath`,
`startupStderrPath` and `resultPath`. Read those exact host paths with an
appropriately authorized file reader. Command and startup streams stay separate
and byte-exact; the manifest contains infrastructure-operation observations,
including disclosed excerpt truncation, not additional helper-output path fields.
Missing or incomplete evidence is not reported as complete.

Every unsuccessful aggregate outcome has `isError: true`, including command
failure, timeout, unresolved cleanup and output persistence failure. Code zero
reports command execution only, not test sufficiency or repository readiness.
See [the result contract](results.md) and its sensitive-output retention policy.

Manifest publication errors also return `isError: true`, retaining the original
publication diagnostic, observed nullable command status and created paths. A
post-rename directory-sync failure can leave a visible `outcome: succeeded` in
`result.json` while publication remains unconfirmed and the invocation failed.
Visible bytes and command code zero do not prove durability or invocation
success. Footman neither rewrites/rolls back that manifest nor retries publication
or execution. One ordinary publication error does not terminate the service;
a subsequent independent request can succeed on the same connection.

An accepted run remains supervised if the MCP request is cancelled or the
connection reaches EOF. EOF stops new requests, and accepted work settles within
its existing execution/cleanup bounds before normal service exit. Clients must
allow those bounds rather than imposing an unrelated short subprocess shutdown
timeout. EOF alone is not proof of settled cleanup. Catastrophic process/host
loss is not recoverable supervision: retained incomplete manifests identify
resources for reconciliation. Do not automatically repeat a disconnected call;
it may already have executed.

## Verification boundary

The Docker-free [Phase-9 suite](testing.md#phase-9-mcp-transport-and-server-suite)
uses the real SDK `ClientSession`, a project-owned sole-process transport,
disposable Git inputs and a Docker CLI stub. It includes a separately built,
noneditable installed-wheel client/service context. Test-only provenance and
lifecycle inputs belong to the bootstrap, never this production CLI or MCP tool.
Real Docker semantics and final host acceptance remain a separate gate.
