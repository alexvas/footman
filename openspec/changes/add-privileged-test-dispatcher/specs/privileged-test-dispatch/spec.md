# Spec Delta

## Purpose

Execute an operator-configured command as a privileged container user against a fixed staged source snapshot through stdio MCP, with attributable retained results.

## ADDED Requirements

### Requirement: Operator configuration
The dispatcher SHALL validate versioned configuration once at startup, immutable until restart. It SHALL define repository, launch argv template, required `image_id`, user, command argv, snapshot destination, cwd, snapshot storage, output template and positive bounded `timeout_seconds`/`cleanup_grace_seconds`. Static field, value and path errors SHALL prevent startup. Requests SHALL NOT override command or environment. TOML edits SHALL take effect only after restart.

#### Scenario: Invalid configuration
- **WHEN** configuration contains an empty command, unknown field or working directory outside the snapshot
- **THEN** the server exits nonzero before serving any request, reports the validation error on stderr, and creates no run output directory or snapshot and starts no container or configured command

#### Scenario: Missing configuration file
- **WHEN** the server starts with a configuration path that does not exist
- **THEN** it exits nonzero with empty stdout and a nonempty stderr diagnostic identifying the configuration-load failure, without serving requests or creating run resources

#### Scenario: Runtime-only validation failure
- **WHEN** valid startup configuration later encounters an output/snapshot reservation collision, a disappeared path, a failed container identity/running-state check or failed snapshot transfer
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

### Requirement: Nonempty configured argv elements
`command_argv` and `container_start_argv` SHALL each be a nonempty array of nonempty NUL-free strings. This SHALL apply to every element, not only the executable.

#### Scenario: Empty or NUL-containing argv element
- **WHEN** any element of `command_argv` or `container_start_argv` is an empty string or contains NUL
- **THEN** startup rejects configuration with a diagnostic before serving requests or creating run output, snapshots, containers or configured-command side effects

#### Scenario: Empty option value in a nonempty argument
- **WHEN** an otherwise valid launch template includes an equals-form option such as `--name=`
- **THEN** the empty option value does not violate the nonempty-argument requirement because the whole argv element is nonempty

### Requirement: Launch image configuration
`image_id` SHALL be a required string matching either `sha256:[0-9A-Fa-f]{64}` or `[0-9A-Fa-f]{12,64}` in its entirety. It SHALL contribute to the effective configuration identity. Format validation SHALL NOT imply image existence or trigger image build/pull.

#### Scenario: Invalid launch image configuration
- **WHEN** `image_id` is absent, non-string, malformed, outside the allowed lengths or a tag instead of an image ID
- **THEN** startup exits nonzero with a configuration diagnostic before serving requests or creating run output, snapshots, containers or configured-command side effects

#### Scenario: Valid launch image configuration
- **WHEN** configuration supplies a full prefixed image ID or an unprefixed 12–64-character hexadecimal ID such as `d9e853e87e55`
- **THEN** startup accepts that field and includes it in effective configuration identity, without assuming image existence or building/pulling an image

### Requirement: Image identity operator instructions
Operator documentation SHALL describe obtaining local image IDs through `docker image ls` or a full ID by tag through `docker image inspect`, and updating configuration and restarting Footman after rebuilding the image.

#### Scenario: Obtain and refresh the configured image ID
- **WHEN** an operator prepares or rebuilds an image tagged `footman-sandbox:latest`
- **THEN** instructions show copying the ID column from `docker image ls` or the full output of `docker image inspect --format '{{.Id}}' footman-sandbox:latest` into `image_id`, and restarting Footman to load the updated value

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
The dispatcher SHALL launch a new dedicated container by replacing only the validated image operand with the configured image ID. Docker SHALL resolve the literal operand locally at launch under its name/ID semantics with `--pull=never`, without implicit pulling or a separate pull fallback. Substitution SHALL NOT use shell evaluation, word splitting or recursion.

#### Scenario: Fixed launch image
- **WHEN** a request launches a container after the operator retargets the image's original tag
- **THEN** launch argv still uses the configured `image_id` literally, with abbreviated-ID resolution performed by Docker

#### Scenario: Unresolved launch image
- **WHEN** Docker returns a resolution error for the configured operand
- **THEN** mandatory `--pull=never` prevents pulling even if the hexadecimal operand is interpreted as an image name; the dispatcher retains the literal operand, Docker's status and diagnostics, returns failure without pull fallback, and does not inspect, copy or execute the configured command

### Requirement: Local image resolution limitation
Footman SHALL guarantee literal operand substitution and disabled pulling, not ID-only Docker resolution or failure for every absent ID. Image inspection SHALL remain diagnostic for image identity, without an equality gate. Operator documentation SHALL explain that local hexadecimal-name tags may resolve instead of intended IDs and assign avoidance of conflicting local names to the operator.

#### Scenario: Local hexadecimal-name collision
- **WHEN** the configured operand is `d9e853e87e55` and Docker successfully resolves a local `d9e853e87e55:latest` tag to another image, including when the intended ID is absent
- **THEN** Footman preserves the literal operand and never-pull policy, records the observed image identity separately, and does not reject dispatch solely for that image mismatch; container identity/running-state and copy/execution checks still apply

### Requirement: Constrained launch template
Startup SHALL validate `docker run --detach=true --pull=never [OPTION ...] -- {image_id} [COMMAND_ARG ...]` under design §1. Options SHALL be `--name=value`, split at the first `=`, with nonempty names and no further `detach` or `pull`. The sole placeholder SHALL be standalone `{image_id}` at the image operand immediately after the delimiter. Command arguments SHALL be literal and placeholder-free. Invalid shapes/placeholders SHALL fail before run-resource acquisition.

#### Scenario: Constrained image operand
- **WHEN** launch argv is `docker run --detach=true --pull=never --privileged=true -- {image_id} sleep infinity`
- **THEN** startup accepts its shape and launch replaces exactly the image operand with the configured ID, leaving options and command arguments unchanged

#### Scenario: Missing or conflicting pull policy
- **WHEN** a template omits the fixed `--pull=never` prefix element, replaces it with `--pull=always` or `--pull=missing`, or adds another pull option (including another `--pull=never`)
- **THEN** startup rejects it with a diagnostic and no run-resource side effects

#### Scenario: Invalid launch shape
- **WHEN** launch argv uses a wrapper, alternate executable, Docker global options, short/bare flags, separate option values, an additional detach option, a missing options delimiter or an unrelated image before `{image_id}`
- **THEN** startup fails with a validation diagnostic before serving requests or creating run output, snapshots, containers or configured-command processes

#### Scenario: Invalid launch placeholder
- **WHEN** launch argv omits `{image_id}`, repeats or embeds it, places it anywhere except the image operand, or contains an unknown placeholder, including any `{snapshot_path}` occurrence even alongside `{image_id}`
- **THEN** startup rejects configuration before creating run resources

#### Scenario: Launch arguments
- **WHEN** a validated template is expanded for a request
- **THEN** configured `image_id` is substituted literally and the launch argv does not receive the host snapshot path

### Requirement: Staged snapshot container copy
After launch and container identity/running-state inspection, the dispatcher SHALL copy the complete exported staged snapshot contents into `container_snapshot_directory` of that exact container before exec. Transfer SHALL preserve exact bytes, executable modes and actual internal symlinks without dereferencing them. It SHALL NOT transfer the workdir, `.git` or unstaged/untracked/ignored inputs. It SHALL NOT add an enclosing run-ID directory.

#### Scenario: Staged bytes copied
- **WHEN** a staged file differs from later workdir edits
- **THEN** the container receives its staged bytes at the configured snapshot destination, with the host snapshot and development state unchanged

#### Scenario: Snapshot copy path characters
- **WHEN** absolute host source or container destination paths contain spaces, colons or newlines
- **THEN** startup/per-request validation accepts those characters and literal copy argv preserves each path as one argument

#### Scenario: Invalid copy source or destination
- **WHEN** a path contains NUL, is non-absolute, or the host source's canonical identity cannot be resolved/validated under snapshot ownership
- **THEN** validation fails with the specific cause; later filesystem/Docker errors retain their actual diagnostics

### Requirement: Fresh copy destination and transfer failure
The dispatcher SHALL run literal `docker exec -u root <id> mkdir -- <destination>`, then, only on success, `docker cp <host-snapshot>/. <id>:<destination>/`. Existing destination, missing parent or failed/partial copy SHALL prevent command execution and retain the ID for cleanup without retry. Preparation/copy argv, status and diagnostics SHALL form infrastructure-summary records in `result.json`.

#### Scenario: Missing parent or preexisting destination
- **WHEN** the destination's parent is missing or the destination already exists
- **THEN** `mkdir` fails, snapshot copying and the configured command do not start, and dispatch reports failure with applicable container cleanup

#### Scenario: Partial snapshot transfer
- **WHEN** copying fails after creating some container files
- **THEN** the configured command does not run, transfer failure is reported separately, and the attributable container remains subject to cleanup

### Requirement: Container copy prerequisites
The image SHALL provide `mkdir` supporting `--`. The operator SHALL provide an existing destination parent with directory-only, symlink-free ancestors and keep that path stable during preparation/copy. Footman SHALL create only the final directory using `mkdir` without `-p`. Operator documentation and the annotated TOML SHALL state these prerequisites; in-container path stability is an operator responsibility.

#### Scenario: Prepared destination parent
- **WHEN** `container_snapshot_directory` is `/workspace/source`
- **THEN** the image or operator-controlled startup supplies `/workspace` before copying, `/workspace/source` is initially absent, and Footman creates `source` before copying the snapshot contents

### Requirement: Copied source permissions
Operator documentation and the annotated TOML SHALL explain root-owned destination creation and ordinary `docker cp` container-root ownership. The operator SHALL arrange traversal/read/execute access and command-required write permissions for a non-root configured user. Footman SHALL preserve the configured identity and report access failures through the applicable operation status.

#### Scenario: Non-root configured execution
- **WHEN** the operator configures a non-root user
- **THEN** instructions explain the required permissions for the copied tree and any operator-supplied permission step, without assuming root ownership alone prohibits execution or automatically provisioning ownership

### Requirement: Container launch output contract
Startup SHALL exit zero and return only one full Docker container ID on stdout, optionally followed by one newline; diagnostics SHALL go to stderr. The ID SHALL identify the new dedicated container. Nonzero exit, malformed output or a container not running SHALL prevent configured-command execution. Startup output and status SHALL be retained separately.

#### Scenario: Valid launch
- **WHEN** startup exits zero with one valid container ID and inspection confirms the exact returned container ID and its running state
- **THEN** subsequent execution uses exactly that ID

#### Scenario: Invalid launch result
- **WHEN** startup exits nonzero or stdout contains no ID, extra text or multiple IDs
- **THEN** the configured command does not run and startup diagnostics/status are retained as dispatch failure

### Requirement: Container inspection and image attribution
Before copying or exec, inspection SHALL confirm the exact returned container ID and its running state. Configured `image_id` and the full observed `Image` ID (`sha256:` plus 64 hexadecimal characters) from container inspection SHALL be recorded separately for attribution. Unavailable or unusable observed identity SHALL be recorded as unknown with a diagnostic. Image selection SHALL rely on literal `{image_id}` launch substitution.

#### Scenario: Observed image attribution
- **WHEN** inspection confirms the exact running container
- **THEN** the result records configured `image_id`, including an abbreviated value, and the full observed `Image` ID separately, using unknown with a diagnostic for unavailable or unusable observed identity

#### Scenario: Invalid container inspection
- **WHEN** inspection fails, identifies a different container ID or reports a non-running container
- **THEN** copying and configured-command execution do not start, dispatch fails, and the recorded launch container ID remains available for applicable cleanup without retry or automatic configuration update

### Requirement: Launch inspection protocol
The dispatcher SHALL run one literal `docker container inspect --format {{.Id}}|{{.State.Status}}|{{.Image}} <id>` for launch validation, with the format as one argv element, closed stdin and the remaining request deadline. It SHALL record argv/status, bounded stdout/stderr excerpts and parse diagnostics in the `result.json` infrastructure summary. Container validation and image attribution SHALL share that result.

#### Scenario: Inspection record parsing
- **WHEN** inspect exits zero with one record and at most one trailing LF
- **THEN** its first two pipe-delimited fields must be a full 64-hex ID equal to the returned launch ID (hex case insignificant) and status exactly `running`; the optional remainder is full `sha256:<64 hex>` image metadata or unknown with a diagnostic if absent/empty/malformed

#### Scenario: Invalid mandatory inspection data
- **WHEN** inspect exits nonzero, returns multiple records, omits/unparseably encodes ID or status, identifies a different container or reports a non-running state
- **THEN** dispatch fails before mkdir/copy/command execution and retains diagnostics and the recorded launch ID for applicable cleanup

### Requirement: Configured container execution
The dispatcher SHALL execute configured command argv in the newly launched container as the configured user from the in-container cwd `{container_snapshot_directory}/{working_directory}`, normalized and confined below the configured snapshot destination. The dispatcher SHALL NOT substitute the host snapshot path into exec argv. It SHALL use literal argv, closed stdin and no TTY. It SHALL NOT infer test organization, add filters or provision project-specific identities.

#### Scenario: Configured execution
- **WHEN** launch validation and snapshot copying succeed
- **THEN** execution uses the returned container ID, configured user, copied staged snapshot and exact command argv

#### Scenario: Container working directory
- **WHEN** the snapshot destination is `/workspace` and the configured relative working directory is `subdir`
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

### Requirement: Manifest publication confirmation and failure escalation
Publication SHALL write and fsync one exclusive same-directory temporary file, release its sole descriptor owner, atomically replace this request's `result.json`, then fsync the borrowed output directory capability. Only successful completion SHALL confirm finalization and committed in-memory state. Failure SHALL NOT trigger retry, rollback or corrective rewriting; visible bytes SHALL NOT alone certify publication durability or invocation success.

#### Scenario: Durable final publication
- **WHEN** final publication succeeds
- **THEN** temporary-file fsync and descriptor release precede replacement, directory fsync follows replacement, and only then are finalization and committed in-memory state confirmed

#### Scenario: Directory sync fails after visible replacement
- **WHEN** a manifest containing `outcome: succeeded` has replaced `result.json` but the subsequent directory fsync fails
- **THEN** its visible bytes are left unchanged, no publication retry or corrective write occurs, finalization remains unconfirmed, and the application error reaches MCP as `isError: true` with `output_failed` unless a higher-priority outcome applies, the publication diagnostic, observed command status and available paths; the visible success outcome is not proof of durable publication or successful invocation

### Requirement: Publication application errors
Initial-write, checkpoint and final-publication failures SHALL escalate as explicit application errors after independent applicable cleanup, not merely ordinary unsuccessful internal run returns. Active operation and interruption precedence SHALL be preserved, with publication failure separately observable. Ordinary error translation SHALL NOT swallow process-control interruptions.

#### Scenario: Earlier publication failure
- **WHEN** initial writing, checkpoint publication or final publication fails before replacement
- **THEN** the application records and propagates publication failure after independent applicable cleanup, MCP reports `isError: true` with available observations and paths, and no retry or fabricated confirmation occurs

#### Scenario: Publication failure and cleanup failure
- **WHEN** publication fails and an independent release also fails
- **THEN** both remain observable, operation/interruption precedence is preserved, every independent release is attempted once, and repeated cleanup does not repeat publication or descriptor release

#### Scenario: Publication interrupted
- **WHEN** publication is interrupted by a process-control exception
- **THEN** interruption remains authoritative through independent cleanup, finalization is not confirmed, ordinary error translation does not swallow the interruption, and no corrective publication occurs

### Requirement: MCP publication error boundary
The MCP boundary SHALL translate ordinary publication application errors into structured tool results with `isError: true`, an unsuccessful aggregate outcome, publication diagnostics, nullable observed command status and absolute paths for created evidence. One such invocation error SHALL NOT alone terminate the server or restart the failed run.

#### Scenario: Server remains usable after publication error
- **WHEN** a real MCP client receives a publication application error for one invocation and submits a subsequent valid independent request to the same server
- **THEN** the first tool result has `isError: true`, the server remains available, and the later request can complete successfully without restarting or duplicating the failed run

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
Each run directory SHALL contain complete separate command streams in `stdout.txt` and `stderr.txt`, a structured `result.json`, and separate startup streams in `startup-stdout.txt` and `startup-stderr.txt`. Storage failure SHALL prevent successful reporting. Retained command output SHALL NOT be filtered or silently truncated. Command streams SHALL come from configured `docker exec`. Infrastructure summary and bounded diagnostic excerpts SHALL reside in `result.json`.

#### Scenario: Large output or storage failure
- **WHEN** the command writes large amounts to both streams or storage fails
- **THEN** the dispatcher drains both streams without pipe deadlock and reports any incomplete retention as unsuccessful

### Requirement: Infrastructure diagnostic summary
`result.json` SHALL record inspect, mkdir, cp and cleanup operations in order: argv, timestamps, status/lifecycle failure and parse diagnostics. Stream summaries SHALL select the first 4096 source bytes and publish raw Base64 excerpts with observed/retained byte counts, truncation disclosure and terminal completeness. Adapters SHALL drain within operation budgets. Unavailable observations SHALL remain explicit.

#### Scenario: Infrastructure output exceeds summary limit
- **WHEN** an infrastructure helper emits more than 4096 bytes on a stream
- **THEN** the summary discloses truncation and byte counts, preserves its bounded excerpt and completion state, and retains the five-file output contract with full command/startup streams

#### Scenario: Interrupted infrastructure observation
- **WHEN** a helper's stream or status is unavailable or incomplete
- **THEN** its record identifies the lifecycle failure/completeness and observed bytes without inventing a full-output count or successful exit status

### Requirement: Infrastructure excerpt streaming
Each infrastructure child SHALL use `capture_limit=0` and one domain-owned `stream()` sink, counting raw callback bytes and selecting at most the first 4096 per stream while continuing to drain/discard. Terminal completeness SHALL derive from `on_stream_end`/`StreamOutcome` (EOF, READ_FAILED, ABORTED), independently of child exit. Collector failures SHALL be explicit. Buffered capture or generic raw tails SHALL NOT supply excerpts.

#### Scenario: Leading excerpt and continued drain
- **WHEN** a helper emits more than 4096 bytes with distinguishable leading/trailing content
- **THEN** its selected source prefix is the leading 4096 bytes, observed count covers every delivered byte, source truncation is true, and EOF is reported only when observed

#### Scenario: Aborted diagnostic drain
- **WHEN** stream collection aborts or fails after partial delivery
- **THEN** the sink reports only observed raw bytes and READ_FAILED/ABORTED, distinguishing source truncation from incomplete collection without inventing a total or replaying a process tail

### Requirement: Raw diagnostic retention
Infrastructure excerpts SHALL preserve selected raw bytes directly in Base64, including URLs, credential-like text, invalid text bytes and boundary fragments. Diagnostics SHALL use no content redaction or sanitization. Operator documentation SHALL identify all five result files as potentially sensitive and require restrictive access and operator-managed retention. The manifest SHALL NOT separately enumerate or serialize the inherited environment.

#### Scenario: Byte-preserving diagnostic prefix
- **WHEN** helper output contains a URL, token assignment or invalid text bytes, including across chunks or the 4096-byte cutoff
- **THEN** decoding the excerpt recovers exactly the selected raw prefix, retained byte count describes that prefix and observed count/truncation remain independent of chunk boundaries

#### Scenario: Sensitive retained evidence
- **WHEN** process output or generated diagnostics contain credentials or environment-derived secrets
- **THEN** documentation explains that those values can appear in retained files, Base64 offers no secrecy, and the operator controls evidence access and retention

### Requirement: Attributable command outcomes
Results SHALL record run ID, UTC timestamp, optional phase, expanded output directory, snapshot tree, configuration identity, exact command, cwd, user, container/image identity, timestamps, exit status/signal, output paths, cleanup state and infrastructure diagnostic summary. Command success SHALL be based on exit status, not test-count or domain-output interpretation. Incomplete, timed-out or unresolved runs SHALL NOT report success.

#### Scenario: Command exits zero without tests
- **WHEN** the configured command exits zero without executing tests
- **THEN** the dispatcher reports command success without claiming useful or complete verification

#### Scenario: Command failure
- **WHEN** the configured command exits nonzero
- **THEN** the result preserves that status and MCP returns an unsuccessful tool result

### Requirement: Bounded run lifecycle
The dispatcher SHALL enforce `timeout_seconds` for startup/inspection/copy/execution and `cleanup_grace_seconds` for container cleanup and confirmation. It SHALL remove its dedicated container by recorded ID on completion or failure. Local Docker-client exit SHALL NOT imply container termination. Snapshot deletion SHALL wait for confirmed container cleanup. Unresolved resources SHALL be reported for operator reconciliation; unrelated containers SHALL remain untouched.

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

### Requirement: Recorded container removal protocol
Cleanup SHALL use literal `docker rm -f <id>` then `docker container ls --all --no-trunc --filter id=<id> --format {{.ID}}`, with closed stdin and the shared cleanup allowance. Successful complete listing with empty stdout SHALL confirm absence. The recorded ID in the listing SHALL mean retained container. Other output, nonzero status or lifecycle failure SHALL leave removal unconfirmed. Removal failures SHALL remain observable independently of confirmation.

#### Scenario: Confirmation fails
- **WHEN** listing fails, times out, has incomplete collection or produces malformed/unexpected output
- **THEN** cleanup reports unconfirmed resources and retains the host snapshot, regardless of local client exit or an inspect error

#### Scenario: Container confirmed absent
- **WHEN** listing exits zero with complete successful collection and exactly empty stdout
- **THEN** cleanup records confirmed absence and may proceed with owned snapshot cleanup, retaining any earlier removal failure in the run summary

### Requirement: Single-owner resource integration
Every descriptor, directory capability, fd-owning stream and managed child SHALL have one release authority throughout its lifecycle. Independent releases SHALL each be attempted once. Operation failures SHALL remain authoritative over ordinary close failures; sole close failures SHALL remain observable. Interruption SHALL retain authority. Repeated or concurrent cleanup SHALL NOT repeat release or restart cleanup work.

#### Scenario: Operation and close both fail
- **WHEN** an export or output operation fails and an independent resource release also fails
- **THEN** the operation failure remains authoritative over the ordinary close failure, both are retained, and other independent releases are attempted

#### Scenario: Sole close failure
- **WHEN** execution and writes succeed but closing a retained output resource fails
- **THEN** the close failure is observable and the run is not reported as successful

#### Scenario: Repeated or concurrent cleanup
- **WHEN** multiple callers request cleanup for the same run resources
- **THEN** they observe the same ownership/continuation without repeating releases, restarting actions or renewing cleanup budgets

#### Scenario: Interrupted resource operation
- **WHEN** resource handling is interrupted
- **THEN** interruption remains authoritative, applicable independent cleanup is attempted, and any incomplete ownership remains supervised and reported

### Requirement: Independent cleanup and persistence observations
The dispatcher SHALL distinguish local child exit, external container removal, helper-resource settlement and output persistence. Startup/inspection/copy/execution operations SHALL share the request execution deadline. Cleanup stages, helper execution and local cleanup SHALL fit one cleanup allowance with a local-cleanup reserve. Observing incomplete cleanup SHALL NOT renew that allowance or initiate replacement cleanup.

#### Scenario: Output callback fails with zero command status
- **WHEN** a stream writer fails but the process coordinator treats the callback exception as diagnostic-only and the command exits zero
- **THEN** the dispatcher independently records the persistence failure and reports unsuccessful incomplete output

#### Scenario: Helper done before resource settlement
- **WHEN** a cleanup helper reports its work done but its readers, child or release attempts have not settled
- **THEN** cleanup remains incomplete and the run cannot report aggregate success

#### Scenario: Cleanup budget exhausted
- **WHEN** the cleanup allowance expires with pending resources and a caller subsequently observes cleanup
- **THEN** observation uses the retained owners and continuation without a new action or cleanup budget, and unresolved resources remain reported

### Requirement: Limited success claim
The dispatcher SHALL report execution of the configured command for the captured snapshot only. It SHALL NOT claim that unstaged workdir edits were tested, that tests were sufficient or that the repository is ready for integration.

#### Scenario: Workdir differs from snapshot
- **WHEN** execution succeeds while additional workdir edits exist
- **THEN** the result identifies only the staged snapshot as executed
