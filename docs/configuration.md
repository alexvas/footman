# Operator configuration

Use [the annotated example](../examples/footman.toml) as an operator-owned file,
not executable configuration discovered in the consumer repository. Start with
`footman --config /operator/config.toml`. Configuration is loaded and validated
once before entering the service adapter. Restart to apply TOML edits; changing
or deleting the file does not change an already constructed application's
configuration or its effective identity.

Valid configuration starts the stdio MCP service; see [client setup](mcp.md).
Statically invalid startup configuration is refused with a nonzero status and a
stderr diagnostic, without protocol stdout, output/snapshot reservation or
container/command launch. Later reservation, filesystem, container-validation or
copy failures are unsuccessful request results, not configuration reloads.
There is no direct dispatch CLI or configuration-validation subcommand.

## Version 1 schema

Unknown and missing fields are errors. Required fields:

- `version`: integer `1` (not a boolean).
- `repository`: existing directory. Git index validation belongs to capture.
- `snapshot_directory`: host storage root.
- `output_dir`: host path template. Only `{utc_datetime}` and `{phase}` are
  supported. Either, both or neither may occur; `{run_id}` is not supported.
- `image_id`: required string, either `sha256:` followed by exactly 64 hex
  characters, or 12–64 unprefixed hex characters (for example `d9e853e87e55`).
  Uppercase hex is accepted; tags, whitespace and other lengths are not.
- `container_start_argv`: nonempty array of nonempty NUL-free strings, shaped as
  `docker run --detach=true --pull=never [OPTION ...] -- {image_id} [COMMAND_ARG ...]`.
  Exactly one standalone `{image_id}` immediately follows the first delimiter.
  No other placeholders, conversions or format specifications are accepted.
- `command_argv`: nonempty array of nonempty NUL-free strings, including every
  argument after the executable. Empty elements fail startup before acquiring
  run resources. Spaces, shell metacharacters and braces are literal.
- `container_snapshot_directory`: fresh absolute POSIX in-container copy
  destination; its parent must already exist, the final directory must be absent.
  `/workspace/source` requires an existing `/workspace`, with `source` absent.
- `working_directory`: nonempty relative POSIX path normalized below the copied
  snapshot; `.` selects its root, and `subdir` selects `/workspace/source/subdir`
  when the destination is `/workspace/source`. Never the host snapshot path.
- `timeout_seconds`: finite number **greater than 0 and at most 86400**.
- `cleanup_grace_seconds`: finite number **greater than 0 and at most 300**.

Optional `user` defaults to `root`; when supplied it is a nonempty string.
Boolean timeouts/grace, NUL-containing strings, invalid TOML and nonregular
configuration files are rejected. The configuration file itself is opened
no-follow; point `--config` at the regular operator file rather than a symlink.

Relative host paths resolve against the supplied configuration file's directory,
not the caller's working directory. Existing ancestors and symlinks are
canonicalized. Repository, snapshot storage and output storage must not overlap
in either direction. For dynamic output templates, storage is the static path
prefix before the first placeholder-bearing component in the supplied template.
Placeholder parsing never applies to the configuration directory or names
introduced by canonicalization (including symlink targets); braces in those
names remain literal. The effective configuration retains the canonical literal
`output_root` separately from the template-only `output_suffix`; the combined
`output_dir` is a display path, not a template to reparse. Configuration identity
includes that distinction. Parent navigation cannot escape the storage prefix. Validation creates no directories. Disappeared/replaced
paths, exclusive-reservation collisions and actual container identity/copy checks
remain request-time checks in later adapters; startup validation is not a
promise that mutable filesystem or Docker state will remain valid.

Static output paths and phase-only paths are valid, but the next reservation
fails while the same expanded destination exists. No overwriting, automatic
renaming or automatic retry is permitted. Prefer `{utc_datetime}` for repeat
use, without assuming it makes collisions impossible.

## Request authority and lifetime

The internal request boundary accepts `{}` or an object containing only `phase`.
A phase must match nonempty ASCII `[A-Za-z0-9_-]+`; there are no enumerated
values. A phase is required if the output template uses `{phase}`. Otherwise it
may be absent; a supplied phase is retained even if the template does not use
it. Invalid requests never enter the downstream dispatch callback. A phase
cannot alter argv, image, user, paths, timeout or configuration selection.

Effective configuration uses frozen records and tuples, with a SHA-256 identity
computed from the normalized explicit fields. Identity includes no environment
values and exposes no raw configuration text. It is unchanged across requests
and post-startup TOML edits; a restarted application loads and validates again.
Treat the operator file and configured argv as sensitive: hashing the effective
configuration is attribution, not encryption or approval of the command.

## Operator prerequisites

Prepare the image and its long-running main process yourself. Footman does not
build images, provision identities or infer application readiness. Staged code
running as root is not a malicious-code sandbox. Launch/command argv is not
shell-evaluated. Cargo, Gradle and npm examples are interchangeable configuration,
not dispatcher runtime dependencies or implicitly selected commands.

Obtain a local image ID using `docker image ls` (for example `d9e853e87e55`), or
obtain its full ID using
`docker image inspect --format '{{.Id}}' footman-sandbox:latest`.
After rebuilding, obtain the new ID, update `image_id` and restart Footman.
Format validation at startup does not prove image existence and never builds or
pulls images. Each request substitutes the configured value literally at the
validated image operand. Docker resolves full/abbreviated operands locally with
mandatory `--pull=never`; resolution errors propagate without pull fallback,
operand rewriting or retry. A local hexadecimal-name tag such as
`d9e853e87e55:latest` can resolve to another image, including when the intended
ID is absent. There is no ID-only selection or every-absent-ID-fails guarantee.
Avoid conflicting local names: that is the operator's responsibility. Full
observed image identity from container inspection is diagnostic-only, not an
image-equality gate; unknown/unusable metadata is reported with diagnostics.

### Launch and transfer contract

The exact prefix is `docker run --detach=true --pull=never`. Additional options
must each be one `--name=value` argument, split only at the first equals sign.
An empty value in `--name=` is allowed because the argument itself is nonempty.
No additional detach/pull options are accepted, even duplicate `--pull=never`.
The mandatory `--` precedes the sole standalone image placeholder; optional
container command arguments thereafter are literal (including a command-level
`--`). Wrappers, alternate executables, Docker global options, short/bare flags,
separate option values, missing delimiters and misplaced/embedded/duplicate
placeholders are rejected at startup. This is startup-only shape validation;
Docker validates actual option names/values at launch. Footman adds no implicit
Docker flags. See the loadable annotated TOML for a dedicated keep-alive example.

Dedicated `docker run` must exit zero and output only its new full 64-hex
container ID, optionally one trailing LF, on stdout; diagnostics go to stderr.
The operator prepares keep-alive and readiness, not Footman. A single exact-ID
inspection must confirm the same running container before preparation/copy/exec;
failures suppress all downstream work. There is no shared-container reuse.

The image or operator-controlled startup must prepare `/workspace` before using
`/workspace/source`. The image must provide `mkdir` supporting `--`. Ancestors
must be directory-only and symlink-free, and paths stable throughout preparation
and transfer. These are operator prerequisites, not race-proof in-container
containment guarantees. Footman creates only the final destination with `mkdir`
without -p, then copies the complete exported staged snapshot contents:

```text
docker exec -u root <id> mkdir -- <destination>
docker cp <canonical-host-snapshot>/. <id>:<destination>/
docker exec -u <user> -w <destination>/<relative-cwd> <id> <command-argv...>
```

Missing parent or existing destination fails mkdir and prevents copy/command
execution; partial/failing copy likewise prevents command execution, preserving
the known ID for applicable cleanup. No launch/copy retry or stale-tree merge.
The source is the staged export, never the workdir; `/.` avoids an extra run-ID
nesting level. Absolute paths containing spaces, colons and newlines remain
literal argv; NUL/non-absolute paths and unresolvable host-source identities
fail explicitly. The Docker CLI connection transfers bytes; the daemon need not
access the host snapshot path. Actual local/remote Docker environment support,
byte/mode/symlink preservation and fresh-destination semantics remain subject to
real-Docker host acceptance, not established by a scripted development CLI.
Follow the [operator-only host protocol](testing.md#operator-only-host-acceptance)
for the single manual command and exact-path evidence handoff; documented
Docker-free controls are not observations that these host semantics passed.

Root mkdir and ordinary docker cp produce root-owned destination/copy entries.
A non-root configured user needs traversal/read/execute permissions and whatever
command-specific write access it requires. The operator/image or an explicitly
configured command wrapper supplies suitable permissions. Root ownership alone
does not imply all non-root commands fail. Footman infers no chown/chmod, provisions
no UID/GID, imposes no root-only execution restriction and propagates access
errors through the relevant operation's status. Exec uses closed stdin, no TTY,
exact configured user/argv and normalized copied-snapshot-relative cwd.

### Sensitive evidence

All five files (`stdout.txt`, `stderr.txt`, `startup-stdout.txt`,
`startup-stderr.txt`, `result.json`) can contain credentials or environment-derived
secrets in output, raw infrastructure excerpts and generated diagnostic messages.
There is no content redaction. Infrastructure summaries select the leading 4096
raw source bytes per stream through domain-owned streaming collectors, continue
draining, and record observed/retained counts, truncation and terminal completeness.
Base64 offers no secrecy. Incomplete observations disclose partial counts, never
invent totals or clean EOF. Summary truncation does not truncate the complete
command/startup streams. Operators must restrict access to all five files and
manage retention and disk capacity; no automatic deletion/rotation is provided.

The configuration suite and foundation regression commands are in
[testing.md](testing.md). Real launch, reservation, MCP and Docker behavior is
not established by Phase 2 configuration tests.
