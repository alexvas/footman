# Output reservation and retention

After phase-only input validation, each accepted request calls Python 3.14+
standard-library `uuid.uuid7()` exactly once. Its epoch-millisecond `UUID.time`
defines the fixed UTC `utc_timestamp` (for example `2026-11-01T10:30:00.123Z`)
and filesystem-safe `{utc_datetime}` (`20261101T103000123Z`). No independent
wall-clock read is used, including when UUIDv7's logical clock advances beyond
a stalled or backward-moving clock.

The UUID's entire unsigned 128-bit integer is serialized most-significant digit
first as exactly 26 lowercase Crockford Base32 digits, including leading zeroes,
using `0123456789abcdefghjkmnpqrstvwxyz`. The first digit is `0`–`7`. This `run_id`
identifies snapshot storage and result metadata; it is not an output placeholder.
The UUID, run ID and both timestamp representations stay fixed for the request.

## Paths and exclusivity

The operator's `output_dir` supports only optional `{utc_datetime}` and `{phase}`.
Expansion is single-pass, without shell evaluation or recursive substitution.
Canonical configuration-directory and storage-root names are literal even when
they contain braces. Relative templates resolve against the operator TOML's
directory, never the client's cwd. See [configuration.md](configuration.md) and
[the example](../examples/footman.toml).

A phase is required when its placeholder occurs, and must be a nonempty
`[A-Za-z0-9_-]+` string. It only organizes output, never selects a command. A
supplied phase is retained as metadata even when the template does not use it.

Shared parents may already exist; their modes are not changed. Missing parents
are created with mode `0700`. The final run directory is created exclusively
with mode `0700`. Existing destinations, including symlinks and regular files,
fail as `output_failed`: no reuse, overwrite, automatic suffix, renaming or UUID
regeneration. Exactly one contender may reserve an identical expanded path.
A static template is valid but can succeed only once while that directory
remains. A phase-only template collides when the same phase is reused. Prefer
`{utc_datetime}` for repeated runs, without assuming it prevents collisions.

At request time, canonical ancestors and repository/snapshot overlap are checked
again. Directory capabilities walk components without following symlinks;
replaced roots and symlinked parents are rejected. Before creating files, the
acquired directory's Linux `/proc/self/fd` path must equal the requested path,
and the configured repository/snapshot paths are resolved again to check for
overlap. That second overlap check detects a forbidden configured path replaced
by a symlink into, or above, the output destination after preflight. It does not
track directory identity: renaming a repository/snapshot root elsewhere while
leaving its configured path absent is not detected by resolving that old path.
An unavailable descriptor-path lookup fails closed. The acquired-path comparison
detects an already-open output ancestor moved during traversal; neither check
is a lease against later renames. All writes are relative to
the exclusively created directory capability. Output must remain outside the
development repository and snapshot storage. Operators must protect storage
parents against unauthorized renames or mutation; this is not a hostile-code
sandbox.

## Fixed retained files

Each successfully reserved run directory has five exclusively created regular
files, each mode `0600`:

- `stdout.txt`: configured command stdout
- `stderr.txt`: configured command stderr
- `startup-stdout.txt`: launcher stdout, separate from command output
- `startup-stderr.txt`: launcher diagnostics, separate from command output
- `result.json`: durable run manifest

Reservation creates handles, not evidence of execution. In Phase 3 these files
start empty; initial/final manifest persistence and stream draining are delivered
in Phase 7. An empty `result.json` is not a completed or running manifest. Neither
reservation nor a command's exit status establishes useful test coverage.

`footman.identity.RequestIdentity` supplies the shared identity independently of
output reservation. Each file descriptor has one private `OwnedDescriptor`;
directories have their own `DirectoryDescriptor` capabilities. The reservation's
`files` mapping exposes only immutable borrowed views with `fd` and `released`,
not close or transfer authority. Its `paths` mapping is read-only too. Views stop
providing descriptor numbers when release starts. Callers must settle writers
before closing the reservation: a borrowed integer fd does not synchronize an
in-flight write with cleanup. Concurrent cleanup callers wait for the serialized
release to finish; `cleanup_failure` retains its failure without retrying closes.
Independent releases are attempted once,
including after interruption or partial setup. Operation failures remain
primary over ordinary close errors; sole close errors remain observable.
Repeated/concurrent cleanup does not retry release. Closing capabilities does
not delete evidence. No snapshot capture or container launch is permitted until
all five files have been reserved successfully. Failure reports expose absolute
paths only for files actually created, not unavailable or complete evidence.
Partial directories/files are retained on setup failure and are never reused.

## Capacity, sensitivity and manual retention

Logs can be sensitive and arbitrarily large. Plan disk capacity and operator
access; restrictive host modes do not protect against an administrator. A full
or inaccessible filesystem causes an unsuccessful output result, not success
with fabricated evidence paths. Do not mix startup/infrastructure diagnostics
into command streams or interpret output as project correctness.

There is no automatic rotation or evidence deletion. Operators choose manual
retention/archival and remove only identified run output after it is no longer
needed. Never delete active-run evidence to make a colliding request succeed;
use a new configured destination, label or later timestamp instead. Temporary
snapshot cleanup is separate from retained-output lifetime and does not authorize
deleting retained logs or manifests.
