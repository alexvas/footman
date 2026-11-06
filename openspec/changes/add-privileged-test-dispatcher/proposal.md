# Proposal

## Why

An agent needs to request execution of a configured command as a privileged user inside a prepared container without receiving Docker socket access. Running a fixed staged source snapshot prevents accidental execution of additional workdir edits and makes the retained result attributable.

## What Changes

- Reuse the existing `footman.filesystem` descriptor/directory/stream owners and `footman.process` process coordinator. Implement Git, Docker, output persistence and reconciliation as domain adapters, not parallel resource-lifecycle implementations.
- Build a standalone stdio MCP server with one shared dispatch implementation; it has no direct command-execution CLI.
- Exercise that executable with the real MCP SDK `ClientSession` over a project-owned stdio transport backed by `OwnedProcess`. Deliver and independently validate opt-in owned child stdin support in Phase 8, limited to the process foundation, its tests and documentation, retaining closed stdin by default for Git/Docker. Phase 9 then integrates that API with the SDK transport and public MCP server; the SDK supplies the client session, not a newly implemented full client or public client application. Do not use the SDK's subprocess-owning `stdio_client` or introduce an exception to single-owner lifecycle rules.
- Introduce an operator-owned configuration file specifying the repository, container-launch argv template, required `image_id` (full `sha256:` followed by 64 hexadecimal characters, or 12–64 hexadecimal characters without a prefix, such as `d9e853e87e55`) selecting the image to launch, execution user, command argv, in-container snapshot destination, working directory, snapshot storage, output-directory template and timeout.
- Generate one UUIDv7 per accepted request using Python 3.14+ `uuid.uuid7()`. Derive the millisecond UTC timestamp from that UUID and serialize all 128 bits as a fixed-width, 26-character lowercase Crockford Base32 `run_id`.
- Export the complete staged Git tree under canonical `snapshot_directory/<run_id>`, created exclusively with visible collision failure and no renaming, without changing the development index, HEAD, branches or workdir and without creating commits.
- Launch a dedicated container using a startup-validated literal argv template shaped as `docker run --detach=true --pull=never [OPTION ...] -- {image_id} [COMMAND_ARG ...]`, where each additional option is a single `--name=value` argument (no additional `detach` or `pull`), and the sole standalone `{image_id}` occupies the image operand immediately after `--`. Reject placeholder occurrences elsewhere, wrappers, short/bare flags and separate option values. Require exit zero and only the container ID on stdout, with diagnostics on stderr; inspect that exact container to confirm its identity and running state. Record its full observed image ID from container inspection for attribution. Substitute the configured image ID literally; Docker resolves the literal operand locally with mandatory `--pull=never`, without implicit pulling or a separate pull fallback; Docker resolution errors cause launch failure. Local hexadecimal-name tags can resolve instead of intended IDs, so literal substitution is not a strict image-identity guarantee. Operators avoid conflicting local names; observed image identity remains diagnostic. Missing or additional pull options are rejected at startup. Copy the complete exported staged snapshot into the configured destination inside that container before executing the configured command through `docker exec`. Copy failure prevents command execution. Remove the container after execution. Document obtaining image IDs using `docker image ls`, or a full ID from an operator-owned tag using `docker image inspect --format '{{.Id}}' footman-sandbox:latest`, and updating configuration/restarting the service after an image rebuild.
- Expand `output_dir` using optional `{utc_datetime}` and `{phase}` placeholders into an exclusive per-run directory (for example `{phase}/{utc_datetime}`) containing `stdout.txt`, `stderr.txt`, `result.json` and separate startup logs.
- Retain complete separate configured-command stdout/stderr from `docker exec`, separate startup stdout/stderr and a durable `result.json`, preserving all five host files; return a structured MCP result with the command `return_code`, outcome and full paths to every retained output file.
- Escalate manifest-publication failures as application errors and report the affected MCP invocation with `isError: true`, preserving available status, paths and diagnostics without crashing the server. Do not retry publication or rewrite the manifest after failure. A post-rename directory-sync failure can leave a visible success outcome whose publication durability is unconfirmed; that file does not establish successful completion of the invocation.
- Expose an MCP tool that runs the configured command and accepts only an optional safe `phase` label for result organization. The label has no predefined values and does not select or modify commands. Command selection and arguments belong to configuration, not agent requests.

## Capabilities

### New Capabilities

- `privileged-test-dispatch`: Execute a configured privileged command inside a container against a fixed staged snapshot and retain attributable output through stdio MCP.

### Modified Capabilities

None.

## Impact

This repository is the standalone dispatcher project; implementation belongs at its root. The plan is portable and requires no knowledge of any consumer's workflow, test organization, domain, packaging or permission model.

The resource-lifecycle foundation is already present; its helper tests do not establish dispatcher completion. This change integrates those primitives without moving Docker, Git, recursive snapshot cleanup or manifest-publication policy into the filesystem foundation.

Runtime dependencies are Python 3.14+, host Git and Docker CLI, an operator-prepared image containing the tools required by the configured command (for example Cargo, Gradle or npm), and an MCP SDK selected and pinned during implementation. Development takes place inside a container without Docker access: phases 1–10 use real disposable Git repositories and, from Phase 9, a real MCP client, but a controlled Docker CLI stub. Phase 8 independently validates the process foundation before MCP integration in Phase 9; Phase 10 covers integrated acceptance and portability. A separate final Phase 11 runs a minimal real-Docker acceptance suite on the host against the exact built wheel, through a runner that records evidence and resource reconciliation. Docker is not a prerequisite for the development gate; host acceptance remains mandatory for final delivery. The dispatcher does not arrange consumer test suites, parse test counts, judge test completeness, provision application-specific users or create container images. It launches and cleans up a dedicated container per run using the configured launch contract. It reports command execution, not project correctness or commit readiness.
