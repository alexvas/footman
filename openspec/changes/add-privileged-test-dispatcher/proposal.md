# Proposal

## Why

An agent needs to request execution of a configured command as a privileged user inside a prepared container without receiving Docker socket access. Running a fixed staged source snapshot prevents accidental execution of additional workdir edits and makes the retained result attributable.

## What Changes

- Build a standalone stdio MCP server with one shared dispatch implementation; it has no direct command-execution CLI.
- Introduce an operator-owned configuration file specifying the repository, container-launch argv template, execution user, command argv, mount location, working directory, snapshot storage, output-directory template and timeout.
- Generate one UUIDv7 per accepted request using Python 3.14+ `uuid.uuid7()`. Derive the millisecond UTC timestamp from that UUID and serialize all 128 bits as a fixed-width, 26-character lowercase Crockford Base32 `run_id`.
- Export the complete staged Git tree under canonical `snapshot_directory/<run_id>`, created exclusively with visible collision failure and no renaming, without changing the development index, HEAD, branches or workdir and without creating commits.
- Launch a dedicated container using a configured argv template parameterized by `{snapshot_path}` to mount the temporary snapshot. Require exit zero and only the container ID on stdout, with diagnostics on stderr; execute the configured command in that container and remove it after execution.
- Expand `output_dir` using optional `{utc_datetime}` and `{phase}` placeholders into an exclusive per-run directory (for example `{phase}/{utc_datetime}`) containing `stdout.txt`, `stderr.txt`, `result.json` and separate startup logs.
- Retain complete separate stdout/stderr and a durable `result.json`; return a structured MCP result with the command `return_code`, outcome and full paths to every retained output file.
- Expose an MCP tool that runs the configured command and accepts only an optional safe `phase` label for result organization. The label has no predefined values and does not select or modify commands. Command selection and arguments belong to configuration, not agent requests.

## Capabilities

### New Capabilities

- `privileged-test-dispatch`: Execute a configured privileged command inside a container against a fixed staged snapshot and retain attributable output through stdio MCP.

### Modified Capabilities

None.

## Impact

This repository is the standalone dispatcher project; implementation belongs at its root. The plan is portable and requires no knowledge of any consumer's workflow, test organization, domain, packaging or permission model.

Dependencies are Python 3.14+, host Git and Docker CLI, an operator-prepared image containing the tools required by the configured command (for example Cargo, Gradle or npm), and an MCP SDK selected and pinned during implementation. The dispatcher does not arrange consumer test suites, parse test counts, judge test completeness, provision application-specific users or create container images. It launches and cleans up a dedicated container per run using the configured launch contract. It reports command execution, not project correctness or commit readiness.
