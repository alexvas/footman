# Retained command results

Footman reports execution of the configured command against one captured staged
snapshot, not testing of the current workdir, test sufficiency, or readiness to
integrate. A zero status with no tests is still command success. Footman never
parses test counts or domain output.

## Outcome precedence

Evaluate this table top to bottom; the first matching row wins. Status, signal,
persistence and cleanup details remain separate even when a higher-priority
outcome wins. A failed container removal is retained even if listing subsequently
confirms absence. No unresolved run is successful.

| Observation | Outcome |
| --- | --- |
| Process-control interruption at any boundary | `interrupted` |
| Execution deadline observed expired (including with command code 0) | `timed_out` |
| Write, flush, close or manifest publication failed, or a started stream was not completely retained | `output_failed` |
| Snapshot/launch/inspection/preparation/copy or lifecycle failure, unresolved container/helper/snapshot cleanup, or no observed command status | `dispatch_failed` |
| Observed command-process status nonzero (including negative signal status) | `command_failed` |
| Observed command-process status 0 and all required observations settled successfully | `succeeded` |

The nullable `return_code` is the observed configured `docker exec` process
status, never the startup/helper/server status. Negative values preserve the
local client's signal observation (`signal` is its positive number); this does
not establish the in-container child's status. Unknown status is null, never
inferred from a successful launcher or cleanup. Timeout or interruption may
coexist with an observed code 0; that code cannot override the outcome.

Examples: `{ "outcome": "succeeded", "return_code": 0 }`,
`{ "outcome": "command_failed", "return_code": 7 }`, and
`{ "outcome": "dispatch_failed", "return_code": null }`.
Code 0 plus failed storage is `output_failed`; code 0 plus unconfirmed cleanup
is `dispatch_failed`; interruption plus storage failure is `interrupted`.

## Five files and attribution

The exclusive restrictive run directory contains `stdout.txt`, `stderr.txt`,
`startup-stdout.txt`, `startup-stderr.txt`, and `result.json`. Only actually
created files have absolute returned path fields (`stdoutPath`, `stderrPath`,
`startupStdoutPath`, `startupStderrPath`, `resultPath`). No helper-output file or
path field is added. Storage setup failures may expose fewer paths. File creation
is not a claim of complete retention.

The manifest records schema version, UUID-derived run identity/time and phase,
output template/expanded directory, staged tree/object format/contextual HEAD,
effective configuration digest, exact configured and Docker argv, cwd/user,
startup status/streams, configured and observed image identities, container ID,
execution timestamps/status/signal, outcome, output paths, persistence and cleanup
observations, warnings and limitations. The configuration identity is a digest,
not a serialization of the configuration or inherited environment. Exact argv
and raw diagnostics are nevertheless sensitive.

An initial `outcome: running` manifest is written before snapshot capture. Atomic
same-directory checkpoints retain subsequently known identities. After stream
retention and cleanup are accounted for, a same-directory exclusive temporary
file is written, fsynced and closed by its sole owner before atomic replacement
of `result.json`. The borrowed output directory capability is then fsynced.
Only successful completion confirms finalization and updates the committed
in-memory manifest state. The initial file and its directory are also fsynced.
Failed finalization is unsuccessful and can leave the last readable running
manifest, not a confirmed final result. For example a retained initial manifest has
`return_code: null`, `ended_at: null`, pending/not-started infrastructure records,
and unknown tree/container IDs. A running manifest after process interruption
is evidence of incompleteness, not success; reconcile only recorded resources.
An output failure response can preserve code 0 while its files are incomplete or
its manifest remains running. Check persistence, cleanup and response diagnostics
rather than trusting the code or file's existence alone. No automatic retry,
rotation or deletion of retained evidence occurs.

## Publication errors and confirmation

Command observations, visible manifest bytes, confirmed publication and successful
application/MCP completion are different facts. Replacement is visible before the
following directory fsync completes. If that sync fails, `result.json` may already
contain `outcome: succeeded` and `return_code: 0`. These bytes describe observations
prepared for publication; they do **not** certify durable publication or successful
invocation. Confirmed in-memory state stays at the last successful publication and
`manifest_finalized` remains false. After failed or interrupted confirmation, the
file alone cannot establish that confirmation succeeded.

Initial-write, checkpoint and final-publication failures are application errors.
`RetainedRun.execute()` first attempts all independent applicable releases, then
raises `PublicationError` rather than returning an ordinary unsuccessful run.
Its `response` carries available outcome/status/absolute paths and current cleanup
observations. Its `publication_failure` preserves the publication exception and
its `failure` preserves authoritative operation/cleanup failure separately;
`run.failure` retains that same authority. Response diagnostics include
`publication_failure`, `failure` and `manifest_finalized`. A higher-priority
interruption still propagates as its original process-control exception, never
wrapped as `PublicationError`; the run retains the publication diagnostic too.

No publication retry, rollback or corrective manifest write follows failure,
including when initial writing or a checkpoint failed before execution finished.
Cleanup does not require rewriting retained evidence. Ordinary release failures
cannot replace an active operation failure or interruption; independent releases
are attempted once and repeat cleanup cannot restart publication.

The Phase 8 MCP adapter must translate ordinary publication application errors
to a structured unsuccessful tool result with `isError: true`, the publication
diagnostic, observed nullable command code and paths for created files. For
example, command code 0 plus directory-sync failure yields `output_failed`
unless interruption or timeout has precedence, even if the visible manifest says
`succeeded`. A publication invocation error alone must not terminate the server;
a subsequent independent request can succeed. This is the adapter contract,
not a claim that Phase 7 delivers or verifies the public MCP server. Never infer
invocation success solely from command code, manifest contents or file existence.

## Infrastructure summaries and sensitive evidence

Ordered `infrastructure_operations` cover inspect, mkdir, cp, removal (`rm`) and
confirmation, including not-started/pending operations. Records contain literal
argv, nullable start/end timestamps and status/signal, lifecycle failures, parse
diagnostics, collector failure and independent stdout/stderr observations.
Each domain-owned `stream()` collector uses `capture_limit=0`, counts every raw
callback byte, selects the first **4096 source bytes**, and continues draining.
Buffered `capture()` results and generic process tails are not excerpt sources.

`excerpt_base64` is the direct Base64 encoding of that unchanged raw prefix.
`observed_bytes` counts delivered raw bytes; `retained_bytes` counts decoded
prefix bytes; `truncated` means more than 4096 source bytes were observed.
`completeness` independently records pending, EOF, READ_FAILED or ABORTED terminal
observations (serialized using the process enum values). Incomplete observation
has only a partial observed count, not a guessed total. `truncated: false` on an
aborted stream does not prove no bytes were lost. Collector failures remain
explicit even if child status is zero. Summary truncation is expected and is not
truncation of the complete separate raw command/startup files.

There is **no content redaction**. URLs, credential-like text, token assignments,
invalid bytes and cutoff fragments are preserved unchanged. Infrastructure
diagnostics, command/startup output and generated messages can contain credentials
or environment-derived secrets. Base64 offers **no secrecy**. All five files
require restrictive access, disk planning and operator-managed retention. Do not
publish them as harmless test summaries. Real Docker behavior remains subject to
the separate host acceptance gate; CLI-stub results establish adapter contracts
only. See [reconciliation](reconciliation.md) for incomplete resources.
