# Operator configuration

Use [the annotated example](../examples/footman.toml) as an operator-owned file,
not executable configuration discovered in the consumer repository. Start with
`footman --config /operator/config.toml`. Configuration is loaded and validated
once before entering the service adapter. Restart to apply TOML edits; changing
or deleting the file does not change an already constructed application's
configuration or its effective identity.

The protocol/dispatch adapter is delivered in later phases. At this phase valid
configuration reaches the explicitly unimplemented MCP adapter; invalid startup
configuration is already refused with a nonzero status and a stderr diagnostic,
without protocol stdout, output/snapshot reservation or container/command launch.
There is no direct dispatch CLI or configuration-validation subcommand.

## Version 1 schema

Unknown and missing fields are errors. Required fields:

- `version`: integer `1` (not a boolean).
- `repository`: existing directory. Git index validation belongs to capture.
- `snapshot_directory`: host storage root.
- `output_dir`: host path template. Only `{utc_datetime}` and `{phase}` are
  supported. Either, both or neither may occur; `{run_id}` is not supported.
- `container_start_argv`: nonempty string array with nonempty executable and at
  least one `{snapshot_path}` occurrence. No other placeholders, conversions or
  format specifications are accepted.
- `command_argv`: nonempty string array with nonempty executable. Arguments are
  literal, including spaces, shell metacharacters, braces and empty arguments.
- `container_snapshot_directory`: absolute POSIX in-container mount destination.
- `working_directory`: nonempty relative POSIX path normalized below that mount;
  `.` selects the mount itself, and `subdir` selects `/workspace/subdir` when
  the mount is `/workspace`. This is never the host snapshot path.
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
paths, exclusive-reservation collisions and actual container/mount identity
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
build images, provision identities or infer application readiness. The Docker
daemon must be able to bind-mount the host snapshot path; a remote daemon
without that visibility is unsupported. Staged code running as root is not a
malicious-code sandbox. Launch/command argv is not shell-evaluated. Cargo,
Gradle and npm examples are interchangeable configuration, not dispatcher
runtime dependencies or implicitly selected commands.

The configuration suite and foundation regression commands are in
[testing.md](testing.md). Real launch, reservation, MCP and Docker behavior is
not established by Phase 2 configuration tests.
