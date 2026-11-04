# Incomplete-run reconciliation

Each accepted request has one recorded run ID and an exclusively reserved host
snapshot path. Startup may additionally establish one full container ID. Use
these recorded identities, not names inferred from timestamps, image tags,
process IDs or a scan of Docker resources. Results can be incomplete: absence of
an observed ID is not evidence that startup created no container.

## Automatic cleanup and its limits

The internal run supervisor attempts exactly:

```text
docker rm -f <recorded-id>
docker container ls --all --no-trunc --filter id=<recorded-id> --format {{.ID}}
```

Each filter/format is one literal argument; stdin is closed. Removal and listing
are independent observations. A nonzero rm status remains a failure even if the
listing subsequently confirms absence. An inspect error never confirms absence.
Only exit zero with fully collected, exactly empty listing stdout confirms
absence. The recorded ID means still present; unexpected output, read failure,
transport failure, timeout or incomplete collection means unconfirmed.

A local Docker client exiting or being terminated does not prove that container
work stopped. The host snapshot is retained until absence is confirmed AND the
local children/helpers have settled. Even then deletion can fail: host EACCES,
unexpected ownership or changed path/capability identity produces unsuccessful
safe retention. No automatic chown/chmod, symlink traversal, broad parent removal
or retry relaxes confinement. Real container-created root-owned entries remain
inside that container, not in the copied host source; actual Docker removal
semantics require separate host acceptance.

One monotonic execution deadline covers startup, inspection, mkdir, copy and
configured execution; later children receive only its remainder. Cleanup uses
one separate configured allowance. At most one tenth is reserved per local
child; the command launch selects a domain-action stage capped at one half plus
one tenth local reserve, following the four earlier local-child caps. An early
failure uses the same reserved half for rm/listing. Each default-policy helper's
execution and local cleanup fit its remaining allocation; listing receives no
fresh grace. Stage cancellation and later observations cannot renew work.
Snapshot deletion consumes only the remaining local reserve, checking the same
absolute deadline before further directory mutations. Exhaustion retains the
snapshot unsuccessfully rather than obtaining another allowance. Filesystem
deletion is synchronous capability-based work; individual OS filesystem calls
are not a preemptible time guarantee.

Managed action `done` describes the settled request, not successful removal.
`cleanup_complete` describes helper resource settlement, not external absence.
The same owners and continuation remain reachable when cleanup is incomplete.
`cleanup_status` and bounded `wait_cleanup` observe them; they do not recapture,
relaunch helpers, retry deletion, reopen owners or retroactively upgrade failed
retention. Repeating dispatch on a supervisor is rejected.

## Operator procedure

1. Preserve the recorded run/container/snapshot identities and available startup
   and command evidence. Check whether collection/status/cleanup observations
   were complete. Do not infer a command return code from a Docker error.
2. If a container ID was recorded, investigate only that exact ID and the
   associated run. Reconcile its actual state before considering the recorded
   snapshot for removal. Do not treat the launched image ID as a container ID.
3. If startup lost identity, retain the snapshot and available diagnostics.
   Establish attribution manually from operator-controlled records. Never guess
   an ID, rerun startup to discover it, or delete a container merely because it
   uses the same image/name.
4. When removal is unconfirmed, keep the snapshot. When host cleanup failed,
   inspect the recorded path and ownership without following symlinks. Reconcile
   only resources demonstrably owned by this run. Record any manual intervention
   separately from the unsuccessful automatic attempt.

Never use broad container deletion/pruning, parent-directory deletion, wildcard
cleanup or blind retries to make a run appear successful. Preserve unrelated
sentinel containers and paths. A retry is a new request with its own identity,
not continuation or validation of an incomplete prior run.

All five retained result files can contain raw credentials, URLs, invalid bytes
and environment-derived secrets, including infrastructure Base64 prefixes and
generated diagnostics. Base64 offers no secrecy. Keep restrictive access and
operator-managed retention; do not publish evidence as part of reconciliation.
The lifecycle adapter currently supplies observations; durable result publication
and public MCP result adaptation are separate later delivery phases.
