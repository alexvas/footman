# Spec Delta

## Purpose

Execute an operator-configured command as a privileged container user against a fixed staged source snapshot through stdio MCP, with attributable retained results.

## ADDED Requirements

### Requirement: Operator configuration
The dispatcher SHALL read and validate versioned configuration once at startup, immutable until restart. It SHALL define repository, launch argv template, user, command argv, mount destination, cwd, snapshot storage, output template and positive bounded `timeout_seconds`/`cleanup_grace_seconds`. Statically detectable invalid fields, values and paths SHALL prevent startup. Requests SHALL NOT override command or environment. TOML edits SHALL take effect only after restart.

#### Scenario: Invalid configuration
- **WHEN** configuration contains an empty command, unknown field or working directory outside the snapshot
- **THEN** the server exits nonzero before serving any request, reports the validation error on stderr, and creates no run output directory or snapshot and starts no container or configured command

#### Scenario: Missing configuration file
- **WHEN** the server starts with a configuration path that does not exist
- **THEN** it exits nonzero with empty stdout and a nonempty stderr diagnostic identifying the configuration-load failure, without serving requests or creating run resources

#### Scenario: Runtime-only validation failure
- **WHEN** valid startup configuration later encounters an output/snapshot reservation collision, a disappeared path or a mismatched actual container mount
- **THEN** the affected request fails with an unsuccessful tool result and applicable cleanup, rather than being treated as a startup configuration error

#### Scenario: Configured command
- **WHEN** the server starts with a valid configured executable and argument array, such as Cargo, a Gradle wrapper or npm
- **THEN** requests execute that argv without tool-specific restrictions or argument rewriting

#### Scenario: Configuration edited after startup
- **WHEN** the TOML file changes after successful server startup
- **THEN** later requests use the original validated configuration and effective identity without reloading the file

#### Scenario: Configuration applied after restart
- **WHEN** the server restarts after the TOML file changes
- **THEN** it loads and validates the new file once, uses it if valid, and refuses startup if invalid

### Requirement: Run identity and timestamp
Each accepted request SHALL generate one UUIDv7 with Python 3.14+ `uuid.uuid7()`. Its epoch-millisecond `UUID.time` SHALL define the fixed UTC `utc_timestamp` and `{utc_datetime}`. `run_id` SHALL encode all 128 bits, most significant digit first, as exactly 26 lowercase Crockford Base32 characters using `0123456789abcdefghjkmnpqrstvwxyz`, with leading zeroes, no separators and no check symbol.

#### Scenario: Shared request time
- **WHEN** a request receives a UUIDv7, including during a stalled or backward-moving system clock
- **THEN** its timestamp and output timestamp derive from that UUID's millisecond time, without a second clock read or replacement UUID

#### Scenario: Canonical identity serialization
- **WHEN** a run ID is serialized and decoded
- **THEN** it is 26 lowercase ASCII characters with first digit between `0` and `7`, and decoding recovers the original 128-bit UUID, version, variant and timestamp

### Requirement: Exclusive snapshot reservation
Each snapshot path SHALL be `<canonical snapshot_directory>/<run_id>`, confined under that root and created exclusively before capture. Existing destinations, symlinks and concurrent reservation collisions SHALL fail visibly without reuse, ID regeneration or renaming. A request that failed reservation SHALL NOT modify or delete that destination.

#### Scenario: Snapshot collision
- **WHEN** two requests attempt to reserve the same snapshot path
- **THEN** only one acquires ownership; the other fails before capture or launch without changing or cleaning the winner's directory

#### Scenario: Snapshot root escape
- **WHEN** path resolution or a symlink replacement would put the destination outside the canonical snapshot root
- **THEN** reservation fails without exporting source or launching a container

### Requirement: Fixed staged snapshot
Each request SHALL capture one complete Git index tree and export only that tree. Staged additions, changes and deletions SHALL be included; unstaged and untracked content SHALL be excluded. The development index, files, HEAD and branches SHALL remain unchanged. No commit or patch application SHALL be required.

#### Scenario: Partially staged source
- **WHEN** a file has staged content and additional workdir edits
- **THEN** only its staged version is executed and the result identifies the captured tree

#### Scenario: Concurrent staging
- **WHEN** the index changes after capture
- **THEN** the run continues using the captured tree rather than rereading current source state

#### Scenario: Unresolved index
- **WHEN** the index contains unresolved entries or capture fails
- **THEN** the configured command is not executed and the caller receives a dispatch failure

### Requirement: Isolated source export
The dispatcher SHALL use a fresh source directory without development Git metadata, source mounts or ignored build output. Export SHALL preserve staged blob content and executable modes without executing host-side Git hooks or filters. Unsupported gitlinks, unsafe paths and escaping symlinks SHALL fail explicitly.

#### Scenario: Development build artifacts
- **WHEN** the development directory contains ignored dependencies or compiled executables
- **THEN** they are not copied into the source snapshot as inputs

#### Scenario: Checkout filter
- **WHEN** repository configuration defines a checkout filter
- **THEN** export does not execute it and retains the staged blob bytes

### Requirement: Git entry mode preservation
Export SHALL create exact-byte regular files for modes `100644`/`100755`, directories for tree mode `040000`, and actual symlinks for mode `120000` using blob bytes as targets. Symlink targets SHALL remain inside the snapshot after chain resolution. Absolute/outward targets, cycles, unprovable containment, unsupported modes and `160000` gitlinks SHALL be rejected. Export writes SHALL NOT follow exported links; Git tree modes SHALL NOT be treated as stored directory permissions.

#### Scenario: Internal symlink export
- **WHEN** a staged `120000` entry refers to a target inside the snapshot
- **THEN** export creates a symlink with the blob's target bytes rather than a regular file containing those bytes

#### Scenario: Unsafe symlink chain
- **WHEN** a link chain escapes the snapshot or containment cannot be established
- **THEN** export fails before container launch without writing through that link

### Requirement: Parameterized container launch
The dispatcher SHALL launch a new dedicated container using a configured argv template with `{snapshot_path}` replaced by the canonical temporary snapshot path, without shell evaluation, word splitting or recursive substitution. The template SHALL mount this path at the configured container destination. Invalid placeholders or an incorrect mount SHALL fail dispatch.

#### Scenario: Snapshot path with spaces
- **WHEN** the temporary snapshot path contains spaces
- **THEN** substitution preserves it within its original argument without creating additional arguments

### Requirement: Container launch output contract
Startup SHALL exit zero and return only one full Docker container ID on stdout, optionally followed by one newline; diagnostics SHALL go to stderr. The ID SHALL identify the new dedicated container. Nonzero exit, malformed output or a container not running SHALL prevent configured-command execution. Startup output and status SHALL be retained separately.

#### Scenario: Valid launch
- **WHEN** startup exits zero with one valid container ID and inspection confirms the expected running container and mount
- **THEN** subsequent execution uses exactly that ID

#### Scenario: Invalid launch result
- **WHEN** startup exits nonzero or stdout contains no ID, extra text or multiple IDs
- **THEN** the configured command does not run and startup diagnostics/status are retained as dispatch failure

### Requirement: Configured container execution
The dispatcher SHALL execute configured command argv in the newly launched container as the configured user from the in-container cwd `{container_snapshot_directory}/{working_directory}`, normalized and confined below the configured mount destination. The dispatcher SHALL NOT substitute the host snapshot path into exec argv. It SHALL use literal argv, closed stdin and no TTY. It SHALL NOT infer test organization, add filters or provision project-specific identities.

#### Scenario: Configured execution
- **WHEN** launch validation succeeds
- **THEN** execution uses the returned container ID, configured user, mounted snapshot and exact command argv

#### Scenario: Container working directory
- **WHEN** the mount destination is `/workspace` and the configured relative working directory is `subdir`
- **THEN** execution uses `/workspace/subdir`, not the host snapshot path; `.` uses `/workspace`

#### Scenario: Missing execution prerequisite
- **WHEN** the container, user, working directory or executable is unavailable
- **THEN** the run reports an execution/dispatch failure rather than a successful command

### Requirement: MCP-only dispatch interface
The dispatcher SHALL run as a stdio MCP server invoked as `footman --config <config.toml>`. It SHALL expose `run_privileged_command` accepting only an optional string `phase` label. Configuration selection SHALL be operator-controlled. MCP stdout SHALL contain only protocol messages, and agents SHALL receive no Docker socket, direct command-execution CLI or general-purpose host command interface.

#### Scenario: Unexpected tool argument
- **WHEN** an MCP client supplies command arguments, paths or any field other than `phase`
- **THEN** the tool rejects the request before snapshot capture or container execution

#### Scenario: Server startup
- **WHEN** an operator starts the dispatcher with a valid configuration path
- **THEN** it serves `run_privileged_command` over stdio using that fixed configuration

### Requirement: MCP result summary
After each dispatched run, the MCP tool result SHALL report the outcome, the configured command's `return_code` when it was started, and absolute paths to each created `stdout.txt`, `stderr.txt`, `startup-stdout.txt`, `startup-stderr.txt` and `result.json`. `return_code` SHALL be null when no command exit code is available. The result SHALL preserve outcome separately so dispatch, timeout, cleanup and output failures remain distinguishable.

#### Scenario: Command result with retained output
- **WHEN** a configured command completes and its output files are retained
- **THEN** the MCP tool result includes its outcome, return code and absolute paths to all five retained files

#### Scenario: Dispatch failure before command execution
- **WHEN** dispatch fails before the configured command starts
- **THEN** the MCP tool result identifies the dispatch outcome and has a null `return_code`

### Requirement: Output directory template
`output_dir` SHALL support only optional `{utc_datetime}` and `{phase}` placeholders. Expansion SHALL use one filesystem-safe UTC timestamp with fractional seconds per request, without recursive substitution. Unknown placeholders SHALL fail. The expanded directory SHALL be outside repository/snapshot storage and created exclusively before execution; existing destinations SHALL NOT be reused.

#### Scenario: Concurrent timestamps
- **WHEN** multiple requests use the same timestamp
- **THEN** if their expanded paths coincide, exclusive directory creation allows only one request to reserve that path and the other fails visibly without overwriting files

#### Scenario: Static output template
- **WHEN** a template has no placeholders, or has only `{phase}` and the same phase is reused, while its prior output directory still exists
- **THEN** the next reservation fails as `output_failed` without renaming or overwriting; configuration remains valid

#### Scenario: Existing output destination
- **WHEN** the expanded directory already exists or is a symlink
- **THEN** dispatch fails without reusing it or overwriting existing results

### Requirement: Optional phase label
The MCP tool SHALL accept an optional nonempty phase string matching `[A-Za-z0-9_-]+`, with no predefined phase values. A label SHALL be required when `output_dir` contains `{phase}`. Invalid or missing required labels SHALL fail before snapshot capture or launch. The label SHALL affect output organization only and SHALL NOT select or modify commands.

#### Scenario: Phase in output template
- **WHEN** the template contains `{phase}` and the request supplies `review_2`
- **THEN** the output path includes that safe component and the configured command remains unchanged

#### Scenario: Invalid or absent phase
- **WHEN** the label contains dots or path separators, is empty, or is absent while required by the template
- **THEN** the request fails before output reservation or execution

### Requirement: Complete separate output
Each run directory SHALL contain complete separate command streams in `stdout.txt` and `stderr.txt`, a structured `result.json`, and separate startup streams in `startup-stdout.txt` and `startup-stderr.txt`. Storage failure SHALL prevent successful reporting. Retained command output SHALL NOT be filtered or silently truncated.

#### Scenario: Large output or storage failure
- **WHEN** the command writes large amounts to both streams or storage fails
- **THEN** the dispatcher drains both streams without pipe deadlock and reports any incomplete retention as unsuccessful

### Requirement: Attributable command outcomes
Results SHALL record run ID, UTC timestamp, optional phase, expanded output directory, snapshot tree, configuration identity, exact command, cwd, user, container/image identity, timestamps, exit status/signal, output paths and cleanup state. Command success SHALL be based on exit status, not test-count or domain-output interpretation. Incomplete, timed-out or unresolved runs SHALL NOT report success.

#### Scenario: Command exits zero without tests
- **WHEN** the configured command exits zero without executing tests
- **THEN** the dispatcher reports command success without claiming useful or complete verification

#### Scenario: Command failure
- **WHEN** the configured command exits nonzero
- **THEN** the result preserves that status and MCP returns an unsuccessful tool result

### Requirement: Bounded run lifecycle
The dispatcher SHALL enforce `timeout_seconds` for startup/execution and `cleanup_grace_seconds` for container cleanup and confirmation. It SHALL remove its dedicated container by recorded ID on completion or failure. Local Docker-client exit SHALL NOT imply container termination. Snapshot deletion SHALL wait for confirmed container cleanup. Unresolved resources SHALL be reported for operator reconciliation; unrelated containers SHALL remain untouched.

#### Scenario: Timeout with survivors
- **WHEN** the deadline expires and container removal cannot be confirmed
- **THEN** the result reports timeout and unresolved resources, retaining the snapshot and known container identity

#### Scenario: Launch loses container identity
- **WHEN** startup fails or times out without a valid attributable ID
- **THEN** the dispatcher reports potentially unresolved resources without guessing a container to delete or retrying startup

#### Scenario: Command completes
- **WHEN** the configured command exits and the container ID is known
- **THEN** the dispatcher removes only that dedicated container, confirms removal and safely cleans or explicitly reports the retained temporary snapshot

#### Scenario: Client disconnect
- **WHEN** an MCP client disconnects after execution starts
- **THEN** the run remains supervised to a retained outcome without automatic retry

### Requirement: Limited success claim
The dispatcher SHALL report execution of the configured command for the captured snapshot only. It SHALL NOT claim that unstaged workdir edits were tested, that tests were sufficient or that the repository is ready for integration.

#### Scenario: Workdir differs from snapshot
- **WHEN** execution succeeds while additional workdir edits exist
- **THEN** the result identifies only the staged snapshot as executed
