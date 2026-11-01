# Design

## Context

See proposal.md for motivation. This is a standalone tool for an arbitrary Git repository. The operator provides a prepared Docker image and a configuration file containing a container-launch command template. The dispatcher need not understand what the configured command does or how the project organizes tests.

## Goals / Non-Goals

**Goals:** Export the staged source state, execute one configured command inside a container as a configured privileged user, retain exact output/status, and expose it through one stdio MCP tool.

**Non-Goals:** Test discovery, test-count interpretation, assessment of verification completeness, project-specific prerequisites, image building, application provisioning, package lifecycle management, workflow orchestration, arbitrary agent-supplied commands, or a malicious-code sandbox. Staging is a mistake guard, not security approval. The exported source has no development Git metadata; builds requiring it are unsupported.

## Decisions

### 1. One MCP server engine and an operator-owned configuration file

The host-side stdio MCP server owns configuration, snapshot capture, Docker execution and results. Use Python 3.14+ and standard-library `uuid.uuid7()`; do not introduce a UUID compatibility implementation. The pinned MCP SDK is an implementation choice.

Use a strict versioned TOML configuration file. Example:

```toml
version = 1
repository = "/path/to/repository"
container_start_argv = [
  "docker", "run", "--detach",
  "--mount", "type=bind,src={snapshot_path},dst=/workspace",
  "prepared-build-image", "sleep", "infinity"
]
user = "root"
command_argv = ["cargo", "test", "--locked"]
working_directory = "."
snapshot_directory = "/path/to/host/snapshots"
container_snapshot_directory = "/workspace"
output_dir = "/path/to/results/{phase}/{utc_datetime}"
timeout_seconds = 3600
cleanup_grace_seconds = 10
```

Paths, image and keep-alive command above are illustrative, not defaults tied to a particular machine. The image must supply an appropriate long-running main process. Resolve relative host paths against the configuration file directory, not the caller cwd. Canonicalize the repository and external host storage roots. Require snapshot/output storage outside the development repository; reject overlapping storage roots that could mix logs and exported inputs. `working_directory` is relative to the exported source and must resolve inside it. The container snapshot root is the absolute mount destination agreed with the launch template. Every run has its own temporary host snapshot and dedicated container. `container_start_argv` is a nonempty literal argv template; replace `{snapshot_path}` within argument values with the canonical temporary host snapshot path, without shell evaluation or word splitting. Reject unknown placeholders and a template missing `{snapshot_path}`. Do not recursively expand substituted values. Configuration must mount that path at `container_snapshot_directory`. The Docker daemon must be able to bind-mount the host path; remote-daemon layouts without that visibility are unsupported. If Docker mount syntax cannot represent a path, reject it explicitly rather than silently altering the mount. `command_argv` is a nonempty string array with a nonempty executable as its first element; it is not restricted to any build tool. For example, `["cargo", "test", "--locked"]`, `["./gradlew", "test"]` or `["npm", "test"]` can be configured. There is no implicit shell parsing, substitution or agent override. The image must provide the executable or the snapshot must contain it, as with a project wrapper. User defaults to `root` and is recorded; the operator is responsible for configuring the intended privileged identity. Timeouts/grace are positive bounded values. Reject unknown fields, unsupported versions and statically invalid values/paths at server startup, before serving any request. On startup validation failure, exit nonzero, report the validation error on stderr, create no run output directory or snapshot, and start no container or configured command.

Configuration is loaded and validated once at server startup and remains immutable for every request served by that process. Subsequent TOML file edits have no effect on the running process; a new configuration is loaded and validated only after server restart. Configuration immutability does not freeze the filesystem or Docker state: retain per-request checks for expanded paths, exclusive output/snapshot reservation, path disappearance or symlink replacement, and actual container/mount inspection. Failures detectable only then are request-level failures with retained diagnostics and applicable cleanup, not startup errors. Do not reserve per-run directories, capture snapshots or launch containers merely to validate startup configuration. Record its effective non-secret identity with the result. The agent cannot select containers, argv, arbitrary paths, users or timeouts. An optional `phase` label affects only output organization as defined below, not execution configuration. No source repository configuration file is automatically evaluated as dispatcher configuration.

### 2. Export a fixed staged snapshot

Before index capture, exclusively create the source directory at `<canonical snapshot_directory>/<run_id>` using the identity defined in §5. Resolve and enforce confinement under the canonical snapshot root, without traversing symlink replacements. An existing destination (including a symlink) or concurrent reservation collision fails visibly before capture/launch; do not reuse it, regenerate the ID or rename the directory. A failed reservation grants no ownership: never modify or delete that destination. Only the request that successfully reserved a directory may export into or clean it up. Independent requests use distinct reserved paths.

Capture the repository's actual index in a private temporary index; derive one tree using `GIT_INDEX_FILE` pointing at the private copy. A direct `git write-tree` can update index cache metadata, so never run it against the development index. Sanitize inherited Git repository/index redirection. Support ordinary and linked-worktree repositories, and handle split indexes explicitly or fail with an unsupported-format diagnostic. Reject unresolved index entries.

Read one opened index version and detect inconsistent capture rather than mixing versions. Git normally replaces indexes atomically during staging. After capture, later staging or workdir edits do not affect the run. Record the full tree ID and Git object format; HEAD is optional context, not the tested content identity. Creating tree objects is permitted; creating commits or refs, registering worktrees, applying patches and changing development files/index bytes are not.

Export raw NUL-delimited tree entries and blob bytes into a new directory. Do not copy contents from the workdir. Include staged additions, modifications and deletions; exclude unstaged-only content, untracked files and ignored build output. Interpret each Git entry by mode, rather than writing every blob as a regular file:

- `100644` / `100755`: create a regular file with exact blob bytes and the corresponding non-executable/executable file mode.
- Tree entries (`040000`, commonly rendered `40000`): create directories and descend into tree entries. Git does not retain arbitrary directory permission bits; use safe host permissions, not fabricated source directory modes.
- `120000`: read the blob as the symlink target and create an actual filesystem symlink, not a regular file containing target text. Validate that the target resolves within this snapshot, including link chains; reject absolute/outward targets, cycles or targets whose containment cannot be established. Use filesystem-byte-preserving target handling, or explicitly reject unsupported targets. Check containment without following links during export writes; a lexical `..` check alone is insufficient.
- `160000`: reject gitlinks explicitly. Reject unsupported entry modes rather than coercing them to regular files.

Avoid checkout conversions, smudge filters, hooks, `export-ignore` and `export-subst`; do not run consumer executable code on the host. Reject gitlinks, unsafe paths and outward symlinks; never follow exported symlinks while writing. Preserve unusual file names or reject unsupported names explicitly.

The snapshot has no `.git`. Required generated output and dependencies are handled by the configured command and prepared environment, not copied from the development tree. The dispatcher does not add build steps or infer missing project inputs.

### 3. Launch a dedicated container and execute the command

After completing the snapshot, expand and execute `container_start_argv` on the host using literal argv, closed stdin and no TTY. The template launches a new dedicated container with the temporary snapshot bind-mounted, not the development repository. The startup command has this contract:

- Exit zero means launch succeeded; stdout contains exactly one full Docker container ID (64 hexadecimal characters), optionally followed by one newline. No banners, extra lines or container logs are allowed there.
- Diagnostics go to stderr. Nonzero exit means startup failed; the configured command must not run even if stdout contains an ID.
- The container remains running for subsequent `docker exec`; launch success alone does not imply application readiness. The configured image/entrypoint must be ready for the configured command without an inferred domain-specific readiness check.
- The returned ID belongs to the new container created for this request. The dispatcher may inspect, execute in, and remove that container; returning a shared or unrelated container is a configuration-contract violation.

Validate the output and inspect that exact ID, its running state, image identity and snapshot mount before executing the configured command. Missing/malformed/extra stdout, failed inspection or mismatched mount is dispatch failure. Record startup argv, exit status and complete startup stdout/stderr separately from the configured command's output. If a failed/timed-out startup created a container without returning a valid attributable ID, report potentially unresolved resources for operator reconciliation; do not guess an ID, rerun startup or remove unrelated containers.

Validate the working directory inside the mounted snapshot and invoke literal argv, conceptually:

```text
docker exec -u <configured-user> -w {container_snapshot_directory}/{working_directory} <container-id> <command_argv...>
```

The `-w` value is a normalized in-container path below the configured mount destination: for example `/workspace/subdir` for `container_snapshot_directory = "/workspace"` and `working_directory = "subdir"`, or `/workspace` for `"."`. Validate containment in the container path namespace. The dispatcher must never substitute the host snapshot path into exec argv; it is used only for host export, launch mount substitution, mount inspection and cleanup.

Do not use `-i`, `-t` or shell interpolation. Close stdin and keep stdout/stderr separate. Configuration determines the complete command; the dispatcher adds no feature flags, test filters, target selection or test-layout requirements. Check only generic execution prerequisites: Docker availability, successful launch contract, configured identity availability, mount/working-directory access and executable availability. Domain-specific UID/group/ACL or other probes belong to the invoked command, if needed.

Each request uses a fresh host snapshot and its own container, so no shared-container serialization is required. Use fresh build output within that snapshot so a previous compiled executable cannot stand in for it. The image supplies the toolchain and configured dependencies/caches; the tool does not claim hermetic builds. The bind mount may be writable for command-generated output: it is an expendable run copy, not the development repository.

`timeout_seconds` covers startup and command execution; bounded cleanup has a separate configured `cleanup_grace_seconds`. Both are positive bounded configuration values with documented validation limits. Local `docker exec` termination does not prove the child exited. On every terminal path after an attributable container ID is known, remove only that dedicated container (for example `docker rm -f <recorded-id>`) and confirm removal. Preserve command outcome and cleanup failure separately; an unconfirmed cleanup prevents aggregate success. Do not delete the mounted host snapshot until container cleanup is confirmed. Snapshot cleanup must safely handle root-owned files created through the mount and must not follow symlinks or broaden into other paths; report retained paths if host removal fails. Record unresolved resources for operator reconciliation rather than killing unrelated processes or claiming success. On disconnect, a started run stays supervised to a recorded outcome, with no automatic retry. An interrupted dispatcher leaves an incomplete result with any known container ID and snapshot path for reconciliation.

### 4. MCP tool with an optional output label

The operator starts the stdio MCP server as:

```text
footman --config <config.toml>
```

MCP exposes `run_privileged_command` with input schema:

```json
{"type":"object","properties":{"phase":{"type":"string","pattern":"^[A-Za-z0-9_-]+$"}},"additionalProperties":false}
```

The tool invocation supplies either `{}` or an object containing only `phase`. The server loads one configuration at startup, and every tool invocation uses that fixed configuration and result contract. The label is optional unless `output_dir` contains `{phase}`; then missing phase is an input error. It is a nonempty string matching `[A-Za-z0-9_-]+`, without fixed semantic values; dots, slashes, backslashes and traversal components are rejected. A supplied label is recorded even if the template omits it. Phase never selects commands or changes launch/command argv. Reject invalid labels, unknown fields and missing required phase before reserving output, snapshot capture or Docker execution. MCP stdout is reserved for protocol messages; diagnostics use stderr or retained files. A failed run returns a structured unsuccessful tool result, not an unexplained server crash. The tool result reports `outcome`, nullable command `return_code`, and absolute paths for every created `stdout.txt`, `stderr.txt`, `startup-stdout.txt`, `startup-stderr.txt` and `result.json`; outcome remains separate from the return code for failures that do not start or complete a command.

### 5. Output and result semantics

After input validation, at the start of processing each accepted request, call standard-library `uuid.uuid7()` exactly once. Its `UUID.time` value (Unix epoch milliseconds) is the authoritative request UTC timestamp. Derive `utc_timestamp` at exactly millisecond precision and the filesystem-safe `{utc_datetime}` value (for example `20261101T103000123Z`) from that same value, not a separate wall-clock read. Python's UUIDv7 generator can advance its logical timestamp for monotonicity when the clock stalls or moves backward; use the returned UUID time even then. Do not rewrite its timestamp bits or generate a second UUID to make the clock readings agree.

Serialize the UUID's full 128-bit unsigned integer in most-significant-digit-first Crockford Base32 using alphabet `0123456789abcdefghjkmnpqrstvwxyz`. The canonical `run_id` has exactly 26 lowercase ASCII characters, includes leading zeroes, and has no hyphens, UUID text prefix, padding character or check symbol. The first digit is `0`–`7` because 26 digits encode 130 bits and the top two bits must be zero. Do not encode UUID text or apply RFC 4648 Base32: those yield a different representation. Decoding must recover the original UUID, including version 7, variant and timestamp. The run ID, UUID timestamp and UTC representations remain fixed throughout the request.

`output_dir` is a path template supporting only optional `{utc_datetime}` and `{phase}`. `{run_id}` is not supported as an output placeholder; the run ID identifies the snapshot directory and result metadata. Expand once, without recursive substitution or shell evaluation. Both placeholders remain optional: a static path can be reserved only once while it remains present; later requests fail with `output_failed`. A phase-only path similarly collides when that phase is reused. Document these consequences in the annotated configuration example and recommend `{utc_datetime}` for repeat use; even timestamp-based collisions must fail visibly. If expanded paths collide, exclusive directory creation fails visibly without overwriting results or silently changing the requested path. Unknown placeholders and missing/invalid phase are errors. Resolve relative templates against the configuration directory. Validate the expanded path and its canonical existing ancestors to keep it outside the repository and snapshot storage; do not traverse substituted symlinks.

Create the final run directory exclusively with restrictive permissions before snapshot capture or container launch. Existing directories, including symlinks, cause a visible output failure without overwriting files or resuming a previous run. Shared parent directories may already exist. Use fixed filenames within this directory:

```text
stdout.txt
stderr.txt
result.json
startup-stdout.txt
startup-stderr.txt
```

Use exclusive file creation and restrictive host permissions. Retain complete stdout/stderr without filtering, truncation or mixing streams. Drain concurrently or redirect directly to files to avoid pipe deadlock. Keep launch stdout/stderr and dispatch diagnostics separate from the configured command's bytes: retain startup streams in `startup-stdout.txt` and `startup-stderr.txt` referenced by the result, with infrastructure failures described in the result and MCP tool response. Storage failure cannot produce success.

Write an initial running manifest, then atomically finalize `result.json` after output and cleanup are accounted for. The durable manifest supports audit and a client disconnect; the MCP tool result returns its own full path plus the absolute paths of the other created retained files. Record schema version, run ID, fixed UTC timestamp, optional phase label, output template and expanded directory, snapshot tree/object format, contextual HEAD, configuration identity, exact startup/command/Docker argv, startup status and output paths, cwd, user, container/image identity, timestamps, exit status/signal, outcome, output paths, cleanup state and warnings/limitations. Do not record secrets from the environment. Outcomes distinguish succeeded, command_failed, dispatch_failed, output_failed, timed_out and interrupted. An incomplete or unresolved run is unsuccessful. The exit code determines command success; do not parse test counts, reject zero tests, judge skipped tests or interpret domain output.

The result states only that the configured command completed against the captured snapshot. It does not claim that the current workdir was tested or that tests were complete/correct. Logs can be sensitive and large: document disk planning and manual retention, without automatic rotation/deletion of retained evidence.

## Risks / Trade-offs

- Running staged code as container root is powerful -> explicit operator configuration; no safety claim from staging or narrow MCP arguments.
- Export conversions can change source bytes -> raw blob export and byte/mode/symlink fixture tests.
- Docker-client exit can leave a running container -> retain its returned ID and confirm dedicated-container removal.
- Startup can fail after creating resources without yielding an ID -> retain startup diagnostics and report unresolved resources; no blind retry or guessed cleanup.
- Bind-mounted builds can leave root-owned files -> scoped no-follow snapshot cleanup and explicit retained-path reporting.
- Startup stdout may be contaminated -> enforce the single-container-ID contract before configured-command execution.
- Command exit zero does not establish useful testing -> deliberately leave interpretation to the caller, preserving exact output/status.
- Output can fill storage or expose secrets -> restrictive external directories, visible output failures and documented retention.

## Migration Plan

This repository is the standalone dispatcher project; the planning artifacts are already in place and require no further migration. Implement at the repository root, prepare an image separately, configure the launch template and execution command, then verify the stdio MCP server with a real MCP client. Rollback disables the dispatcher configuration; retain results and reconcile incomplete executions by recorded run identity, without modifying repository Git state or deleting unrelated containers.
