# Process lifecycle ownership

`footman.process.OwnedProcess` launches one direct child, owns one
`footman.filesystem.streams.OwnedStream` per captured pipe, and coordinates
readers, deadlines and bounded cleanup. By default `stdin_mode="closed"` gives
the child `stdin=subprocess.DEVNULL`, never inherited service/protocol stdin.
Popen manages its internal DEVNULL handle synchronously. Explicit
`stdin_mode="pipe"` enables managed binary writes and EOF under one prepared
`OwnedStream`; no raw stdin stream/descriptor is exposed to the caller.
`popen_kwargs` cannot override stdin in either mode. Consumers import explicit modules;
filesystem ownership never imports process or domain policy.

The examples below illustrate the supported API; they are not automatically
extracted or executed. Behavior is covered by `tests/test_process_*.py`, including
[tests/test_process_lifecycle.py](../tests/test_process_lifecycle.py).
These suites use Python subprocesses and require no Docker installation.

## Capture a command

Use `launch` to acquire the child and `capture` to collect bounded stdout/stderr.
The context manager retains cleanup responsibility on every exit path.

```python
import sys

from footman.process import OwnedProcess

with OwnedProcess.launch([sys.executable, "-c", "print('hello')"]) as owner:
    result = owner.capture()

assert result.stdout == b"hello\n"
assert result.stderr == b""
assert result.returncode == 0
assert result.lifecycle.complete
assert result.lifecycle.failures == ()
```

Each owner accepts exactly one validated `capture()` or
`stream(callback, *, on_stream_end=None)` attempt. A second, mixed or racing
attempt raises RuntimeError immediately, including after failure or
interruption. Invalid callback arguments do not consume the claim. There is no
late subscription, output replay or cleanup restart.

Launch prepares synchronization, stream slots and continuation readiness before
acquisition. Do not adopt an already-running Popen, reconstruct owners, close
borrowed pipes or adopt a pipe's fileno. Supported factories provide both
captured streams; missing streams are controlled setup failures, not empty
success.

## Managed child stdin

Opt in only for protocols needing child input. Git/Docker helpers retain closed
stdin. `launch(..., stdin_mode="closed")` accepts exactly `"closed"` and `"pipe"`;
unsupported strings and non-strings (including bool) fail before any acquisition,
factory call or worker creation. In pipe mode, the prepared internal
`OwnedStream(label="stdin")` binds `Popen.stdin` immediately. Popen encapsulates
its internal child-end handoff/release; Footman does not acquire a manual pipe or
keep another parent write endpoint. Missing stdin or aliasing either captured
output is a SETUP/None failure, never a silent closed-mode fallback. Independent
siblings still release once on partial setup failure.

Before binding stdin, pipe mode also compares available integer `fileno()`
values, not just stream-object identity. Different wrappers returning the same
fd already represent an incompatible factory ownership contract. Stdin is not
assigned a second `OwnedStream` authority: the canonical output owner and the
extra wrapper are retained by the prepared continuation, and neither conflicting
wrapper is closed or left for garbage collection. Setup fails before readers or
the launch callback start. Proven-independent siblings still release once, with
ordinary close failures and interruptions retained separately.

This is intentionally **incomplete cleanup**, not repaired ownership. Generic
fd-owning wrappers have no universal safe disarm operation; closing one and
letting the other's finalizer run could close a reused fd. `cleanup_status` and
`wait_cleanup` therefore keep `complete=False`, the same owners/continuation and
original budget. Repeat calls cannot retry close, disarm the group or obtain a
new allowance. Once independent work settles, the continuation parks while
retaining the group. There is no force-release/recovery API: fix the factory and
account for the retained group until the owning process exits; do not report
successful cleanup or rely on Python finalizers.

Unprovable descriptor identity (a failing probe or invalid descriptor metadata)
also fails closed and retains potentially conflicting fd-bearing resources.
Independent probes are attempted even after a probe failure/interruption;
explicitly descriptorless streams (no fileno method or UnsupportedOperation)
remain independently releasable. Descriptorless stdin is invalid for writing,
but can be released normally once its lack of a descriptor is established.
The comparison is **integer fd equality**, not inode/open-file-description
identity: distinct descriptors from `os.dup()` or opposite pipe ends remain
valid. Factories must provide truthful, stable descriptor metadata; this is not
a malicious-factory or concurrent descriptor-mutation sandbox.

```python
import sys

from footman.process import OwnedProcess

with OwnedProcess.launch(
    [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
    stdin_mode="pipe",
    deadline_seconds=5,
) as owner:
    assert owner.stdin_write_observation() is None
    assert owner.write_stdin(b"hello", timeout_seconds=1) == 5
    progress = owner.stdin_write_observation()
    owner.close_stdin(timeout_seconds=1)
    result = owner.capture()
    assert result.returncode == 0 and result.stdout == b"5\n"
    assert owner.shutdown().complete
    assert owner.stdin_write_observation() is None
    assert progress.accepted_bytes == 5
```

`write_stdin(data: bytes, *, timeout_seconds: float) -> int` returns `len(data)`
only after every byte has been accepted by the OS pipe, not consumed or processed
by the child. Short writes are completed internally under the same deadline.
Empty bytes return 0 only while writable. There is no writer worker, write queue
or coordinator-owned queued payload: writes run directly on the calling thread,
serialized by a gate, with nonblocking OS writes of at most 65536-byte views of
the caller's payload. Gate and readiness waits consume the one call timeout and
observe coordinator shutdown cancellation. The general lifecycle lock is not
held across gate/readiness waits or OS I/O.

Both write and EOF require a finite positive numeric timeout. Nonnumeric and
boolean values raise TypeError; nonfinite, zero and negative values raise
ValueError. Non-bytes data raises TypeError. Validation precedes writing or
additional acquisition and leaves the existing child, owners and prior write
observation unchanged. No invalid call creates a pipe lazily or writes DEVNULL.

On ordinary timeout, EPIPE, shutdown cancellation or other write failure,
`StdinWriteError(Exception)` has read-only `accepted_bytes: int`, `cause:
Exception` and `timed_out: bool`. The accepted count is the exact OS-written
prefix; success never returns a partial count. The declared `cause` differs from
Python's `__cause__`; raising explicitly chains `raise wrapper from cause`, so
both then identify the original exception. Construction alone does not set
`__cause__`. Process-control interruptions are re-raised as the identical object,
without wrapping or adding progress/cleanup attributes. Read their progress on
the calling thread through the accessor below. Any attempted write failure or
interruption fails the channel permanently: no payload or suffix retry, even
when zero bytes were accepted. Argument/state rejections are not channel failures.

`close_stdin(*, timeout_seconds: float) -> None` explicitly requests EOF before
shutdown, prevents new writes and waits within its original deadline for the
active writer and sole release. It does not wait for child consumption or reap.
The active writer may finish or encounter its own timeout/cancellation; EOF never
closes beneath it. Successful repeated close returns None without another close.
A sole-close failure is retained and raised by identity on repeat close, without
retry. `StdinCloseTimeout(TimeoutError)` means EOF is pending, not delivered.
Repeated pending close observes the same retained outcome and original deadline,
not a fresh waiting allowance. The prepared continuation settles the same owner
once active work quiesces, even after the EOF waiting deadline; there is no new
cleanup budget. Concurrent observers cannot confuse a release claim with a
completed close. Ordinary close failure and actual interruption remain distinct
from successful resource settlement.

`StdinStateError(RuntimeError)` rejects either operation in closed mode and
writes (including empty writes) after EOF request, failed channel, shutdown
initiation or observed reap. `close_stdin` also rejects unbound/failed setup,
including missing/None or identity-aliased stdin, factory/configuration/handoff
failure, a failed launch callback or failed setup publication. An empty prepared ownership slot or
completed cleanup of a failed launch is not successful EOF delivery. Only a
successfully set up stdin pipe can observe/repeat a completed release; later
write failure does not prohibit requesting EOF, and normal shutdown release
still preserves successful-close or retained close-error observations. After shutdown/reap, close only observes already
requested/released EOF state; it cannot initiate a new close or allowance.
Foundation shutdown retains its launch-selected StopPolicy signal/reap semantics;
it is **not** an implicit graceful EOF protocol. For an EOF-driven child, call
close_stdin while it may still run, then collect/wait for its normal exit before
shutdown. Sending EOF alone proves neither protocol success nor settled cleanup.

### Write acceptance observations

All four new public exports are `StdinWriteObservation`, `StdinWriteError`,
`StdinStateError` and `StdinCloseTimeout`; existing exports are preserved.
`stdin_write_observation() -> StdinWriteObservation | None` returns the calling
thread's last finished attempted write, not a cumulative or global last call.
The frozen record has exactly `requested_bytes: int`, `accepted_bytes: int`,
`outcome: Literal["completed", "failed", "interrupted"]`, `error: BaseException |
None` and `timed_out: bool`. Success has full acceptance, error=None and
timed_out=False. Failures/interruption retain the original error by identity.
Progress is published before return/raise, including zero-byte gate-wait failure.
Initial and closed-mode reads return None without a state error. Argument/state
rejection leaves the previous record unchanged; repeated reads return the same
snapshot until another attempted call finishes or the store is retired. EOF,
other callers and cleanup cannot mutate a handed-out record.

Records use weak actual Thread-object keys, never numeric get_ident/native IDs.
Synchronized publication, reads and cleanup prune terminated contributors even
when external code retains their Thread objects. There is at most one record per
live contributor, not cumulative thread history. A new Thread sees None despite
a recycled numeric ident; reuse of the same still-live worker legitimately sees
its own previous record. Async bridges must capture the new snapshot on the
writing thread before another call or final cleanup, then hand it across.

Reap, shutdown initiation, pending cleanup and read-only cleanup_status do not
retire observations: the caller still reads its last record or None without
raising for lifecycle state. A completed explicit shutdown() or wait_cleanup()
retires and clears the whole store only after active write/EOF calls quiesce and
publish. Further reads return None, and invalid later calls cannot resurrect it;
previously returned snapshots and lifecycle failures remain usable. Pruning and
retirement are bookkeeping, not resource release or a renewed budget. Record
immutability is shallow; exception internals are not frozen.

### Stdin failures and the output boundary

ProcessStream remains exactly STDOUT/STDERR. Stdin adds no third reader,
stream_outcomes entry or chunk/end callback. Stdin failures use the unchanged
FailureInfo shape with stream=None: SETUP for acquisition/handoff,
COORDINATION for writes/EOF, RELEASE for sole-owner close. Release ownership has
the stable label `stdin`. Expected timeout/EPIPE/cancellation/write failures are
explicitly ORDINARY; unexpected defects retain UNEXPECTED and actual
process-control exceptions retain INTERRUPTION. The reporter remains strictly
output-callback-only; stdin errors cannot disappear as diagnostic callbacks.

Write/EOF operations and cleanup_status/wait_cleanup retain the original stdin
failures, including sole-close failure after settlement. Active operation and
interruption precedence, independent sibling cleanup and at-most-once release
remain unchanged. Incomplete cleanup retains the same owner/continuation, not a
new pipe, close retry, output collection or budget. See the independent
[stdin contracts](../tests/test_process_stdin.py) and the exact registered
[owned stdin suite](testing.md#phase-8-owned-stdin-suite).

## Stream output

Chunk callbacks receive `(ProcessStream, bytes)` on the collecting thread,
outside state locks. They must return promptly. An optional end callback receives
`(ProcessStream, StreamOutcome)` once per acquired stream, after its assigned
chunks. No callback begins after collection delivers its result or interruption.

```python
import sys

from footman.process import OwnedProcess, ProcessStream, StreamOutcome

received = bytearray()
endings = {}


def chunk(stream, data):
    if stream is ProcessStream.STDOUT:
        received.extend(data)


def end(stream, outcome):
    endings[stream] = outcome


with OwnedProcess.launch([sys.executable, "-c", "print('hello')"]) as owner:
    result = owner.stream(chunk, on_stream_end=end)

assert bytes(received) == b"hello\n"
assert endings[ProcessStream.STDOUT] is StreamOutcome.EOF
assert endings[ProcessStream.STDERR] is StreamOutcome.EOF
assert result.lifecycle.complete and not result.lifecycle.failures
```

Each stream transitions from PENDING to one terminal outcome:

- **EOF** — clean completion; a sibling failure does not revise it.
- **READ_FAILED** — an actual read failure, not a callback error.
- **ABORTED** — collection discontinued; this does not prove the reader has
  stopped or that releasing its descriptor is safe.

Clean EOF is reported promptly. Completion before callback registration or
racing dispatch cannot lose or duplicate the terminal attempt. Domain collectors
own incremental redaction and separate clean-EOF versus abort finalization;
never replay generic raw output tails through a diagnostic sink.

Ordinary callback exceptions are diagnostic-only. An optional launch `reporter`
receives the exception plus typed `stream` and `callback_kind` keyword context,
without eager stringification. Domains supply bounded secret/URL sanitization.
Without a reporter, only a fixed safe message is logged. Ordinary reporter
failure invokes an independent fixed-safe fallback once; ordinary fallback
failure is best effort, never recursive. These paths do not store callback
errors or change operation authority. Non-Exception control flow from a
callback, reporter or fallback is not diagnostic-only: it propagates after
cleanup.

## Interpret results and failures

The frozen records have exactly these fields:

- `FailureInfo(error: BaseException, kind: FailureKind, phase: FailurePhase,
  stream: ProcessStream | None = None)`.
- `ActionResult(done: bool, cleanup_complete: bool,
  failures: tuple[FailureInfo, ...] = ())`.
- `LifecycleResult(complete: bool, failures: tuple[FailureInfo, ...] = ())`.
- `ProcessResult(stdout: bytes, stderr: bytes, returncode: int | None,
  timed_out: bool, lifecycle: LifecycleResult, stream_outcomes=())`.
  `stream_outcomes` is an immutable tuple of `(ProcessStream, StreamOutcome)`
  pairs in STDOUT/STDERR order.

These dimensions are independent:

- `returncode` describes the child exit, not cleanup success. A nonzero exit is
  not itself a lifecycle failure.
- `timed_out` records that the execution deadline fired; it does not necessarily
  identify the authoritative failure.
- `lifecycle.complete` means ownership cleanup has settled, not that the command
  or every close succeeded.
- `lifecycle.failures` is the single aggregate failure sequence, with selected
  authority first and each exact exception object retained once.

Ordinary failures and unexpected defects return ProcessResult rather than
raising from collection. Adapters decide whether to project or raise them. The
precedence is first interruption, unexpected defect, original ordinary operation
failure, then ordinary cleanup failure. Generic results never grant independent
release authority.

A custom factory can fail without acquiring a child. The prepared owner still
provides the same result contract:

```python
from footman.process import FailureKind, FailurePhase, OwnedProcess


def unavailable(*args, **kwargs):
    raise OSError("process service unavailable")


with OwnedProcess.launch(["worker"], factory=unavailable) as owner:
    result = owner.capture()

failure = result.lifecycle.failures[0]
assert failure.kind is FailureKind.ORDINARY
assert failure.phase is FailurePhase.SETUP
assert result.returncode is None
assert result.lifecycle.complete
```

Validation/preparation may raise before a usable prepared owner exists. After
that boundary, ordinary launch/setup failures return the same failed owner;
its sole collection returns available data. A failed factory has no pipes and
no fabricated child. Context exit does not rethrow failures already returned
as values.

FailureKind has ORDINARY, UNEXPECTED and INTERRUPTION. FailurePhase has SETUP,
READ, JOIN, RELEASE, WAIT, ACTION, CALLBACK, REPORTER and COORDINATION. Context
validation follows these rules:

| phase | stream context |
| --- | --- |
| READ, CALLBACK, REPORTER | required |
| SETUP, JOIN, RELEASE | a stream or None |
| WAIT, ACTION, COORDINATION | None |

Release OSError is ordinary; reader join uses `ordinary=()`, so join OSError is
unexpected. InterruptedError follows OSError policy, not automatic control-flow
classification. Invalid enum, phase/stream or kind/control-flow pairings are
rejected. Returned classifications are explicit data, not inferred from the
exception's Python type.

Immutability applies to record fields, bytes and tuples, not the internals of
shared exception objects. There are no generic CleanupResult aliases, redundant
error/failure_info fields, ProcessResult.cleanup, callback-error stores or
exception-attached output snapshots. `StdinWriteObservation` is explicitly
permitted only for per-call OS-acceptance accounting, not as a second lifecycle
ledger or callback-error store. For a write failure,
`wrapper.cause is observation.error is matching_ledger_failure.error`: the same
underlying exception is retained without copying or mutation; independent cleanup
failures remain separate ledger entries. Read-only wrapper fields and shallow
record immutability do not freeze exception internals.

## Handle interruption

KeyboardInterrupt, SystemExit, GeneratorExit and other non-Exception
BaseException propagate after bounded independent cleanup, preserving the exact
first-interruption object. Keep access to the owner for cleanup observation;
there is no generic interruption output snapshot or `owner.result` accessor.

```python
import sys

from footman.process import OwnedProcess

control = KeyboardInterrupt("caller interruption")


def interrupt(stream, data):
    raise control


with OwnedProcess.launch([sys.executable, "-c", "print('ready')"]) as owner:
    try:
        owner.stream(interrupt)
    except KeyboardInterrupt as caught:
        assert caught is control
    else:
        raise AssertionError("interruption was not propagated")

assert owner.wait_cleanup(0.1).complete
```

Domains retain their own bounded sanitized capture for CLI/host reporting;
that capture is independent of generic ownership and exception propagation.

## Shutdown and observe cleanup

`shutdown()` requests bounded cleanup without collecting output. Repeated or
concurrent shutdown calls share the same request, ownership and clocks. They do
not restart work or obtain new budgets.

```python
import sys

from footman.process import OwnedProcess

with OwnedProcess.launch(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    grace_seconds=1,
) as owner:
    status = owner.shutdown()
    if not status.complete:
        status = owner.wait_cleanup(1)

assert status.complete
assert owner.cleanup_status.complete
```

`cleanup_status`, `wait_cleanup(timeout)` and `shutdown()` expose immutable
LifecycleResult observations. An incomplete result retains the same owners and
prepared continuation. Waiting is bounded; it does not retry a close or restart
shutdown. Waiting longer is observation, not a second collection attempt.

Completion requires child reap, actual reader-stop barriers, terminal release
attempts and publication of winning outcomes—not successful closes. A timed
join or child exit alone is not permission to release a descriptor beneath a
live reader. If a reader never stops, its owner remains retained; completion is
not promised. Descriptor-less compatibility streams may need sole-owner release
to unblock; they never justify fd-reuse risks.

Late failures appear in new read-only cleanup observations. They do not mutate
delivered results/errors, replay output, create domain diagnostics or invoke
new callback subscriptions. On context exit without collection, controlled
incomplete cleanup carries observation access; an active body failure uses the
shared precedence rules.

## Advanced: managed actions and budgets

Use a launch-selected StopPolicy when cleanup includes domain work in addition
to local child termination. It consists of StopStage caps, a
`local_cleanup_seconds` reserve and optional draining. Every cap/reserve must be
finite, positive and fit the total `grace_seconds`.

Each phase clock starts before action initiation. Action work and configured
child waiting share that cap and the remaining total deadline. Early completion
does not enlarge another cap. Local-stop-before-action plans preserve adapter
ordering; configured draining delays reader cancellation only within its cap.

The action protocol separates initiation, observation and resource settlement:

- `prepare()`, `start(context)` and `cancel(deadline)` return None.
- Only `observe()` returns ActionResult.
- `done` means the request settled, not that it succeeded.
- `cleanup_complete` describes helper resource settlement independently.
- Failures can be known before `done`; `done` can be true while cleanup remains
  incomplete.

Protocol boundaries must be nonblocking. Actions cannot override shutdown,
control the raw main child or release its pipes. Auxiliary children use separate
default-policy OwnedProcess owners; their execution and cleanup must fit the
parent's remaining allowance.

Transfer helper failures explicitly: preserve each FailureInfo.error and kind,
and rebase context to ACTION/None. Never infer helper kind from Python type,
later mutable status or text. The helper retains its original context; the
parent contains aggregate failures once, not nested ActionResults. Bare errors
and missing classifications are not supported action transport. Local cleanup
completion does not prove external work succeeded.

Docker/container status interpretation remains domain policy. No Docker/npm
adapter is included in Footman's helper port.

## Executable contract scenarios

Keep fault injection in tests rather than in usage examples. Concrete scenarios:

- [Helper failure handoff](../tests/test_process_strict_actions_and_reporting.py "test_helper_snapshot_and_parent_context_are_orthogonal"): preserve identity/kind and rebase parent context.
- [Rejected second collection](../tests/test_process_single_collection.py "test_reject_repeated_and_mixed_collection_without_work"): no callbacks, replay or repeated cleanup.
- [Immutable late observation](../tests/test_process_single_collection.py "test_late_cleanup_is_a_new_observation_not_a_changed_collection_value"): retain the owner without changing the delivered value.
- [Terminal notification race](../tests/test_process_strict_actions_and_reporting.py "test_completion_racing_dispatch_has_once_only_end_attempt"): chunks precede the once-only end attempt.
- [Reporter/default/fallback](../tests/test_process_strict_actions_and_reporting.py "test_reporting_defaults_are_fixed_safe_and_nonrecursive"): fixed-safe, nonrecursive diagnostics.
- [Rejected legacy transport](../tests/test_process_orthogonal_results.py "test_no_legacy_field_conversion"): no conversion of obsolete result fields.

## Exclusions and ownership rules

### Fully encapsulated synchronous subprocess calls

A synchronous `subprocess.run(...)`, including `capture_output=True`, does not
require migration to `OwnedProcess` when process and pipe lifecycles stay entirely
inside the standard library and the application receives only a completed result
with output values, not a live child or fd-owning streams.

When reviewing ownership, classify these calls as library-owned lifecycles,
outside the application's exposed-process/stream ownership proof. This is not a
fourth permitted direct-close category or proof of `OwnedStream` adoption. It does
not exempt caller-managed descriptors or streams passed to a subprocess call:
they retain their own ownership obligations. Code that acquires and manages a
live subprocess or its captured streams must still use `OwnedProcess.launch`.

### Other exclusions

No process-tree/group management, raw/interactive stdin exposure, PTY or arbitrary
external reader ownership is introduced. Managed opt-in binary stdin/EOF is the
only input API, with closed stdin by default and no popen_kwargs override. No
existing-process adoption or Docker/npm/redaction/staging/publication authority
is introduced. Never subclass to replace shutdown, retry ambiguous close,
reconstruct owners or assign independent stage budgets.

See [filesystem-lifecycle.md](filesystem-lifecycle.md) for single-owner release.
Domain-specific adapters must define their own policy separately.
