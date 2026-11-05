"""Per-run Docker reconciliation, separate from local process ownership.

No raw child, borrowed pipe or global Docker inventory grants cleanup authority.
The same action and helper owners survive incomplete observations.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

from footman.configuration import Configuration
from footman.container import (
    ChunkSink,
    ContainerError,
    ContainerExecution,
    InfrastructureOperation,
    _timestamp,
)
from footman.filesystem.cleanup import CleanupFailures
from footman.process import (
    ActionResult,
    FailureInfo,
    FailureKind,
    FailurePhase,
    LifecycleResult,
    OwnedProcess,
    ProcessStream,
    StopContext,
    StreamOutcome,
)
from footman.snapshot import SnapshotReservation


def _kind(error: BaseException) -> FailureKind:
    if not isinstance(error, Exception):
        return FailureKind.INTERRUPTION
    if isinstance(error, (OSError, ContainerError)):
        return FailureKind.ORDINARY
    return FailureKind.UNEXPECTED


class ContainerCleanup:
    """Nonblocking ManagedAction with exactly one ordered rm/listing attempt.

    The worker collects each default-policy helper once. cancel only narrows its
    original deadline; it cannot signal/reap a borrowed child or renew work.
    observe keeps helper settlement separate from domain request completion.
    """

    def __init__(
        self, identity: str | None, *, launch: Callable[..., Any] = OwnedProcess.launch
    ) -> None:
        self.identity = identity
        self._launch = launch
        self.operations = [InfrastructureOperation(n) for n in ("rm", "confirmation")]
        self.owners: list[OwnedProcess] = []
        self.confirmed_absent = False
        self._failures: list[FailureInfo] = []
        self._lock = threading.RLock()
        self._finished = threading.Event()
        self._started = False
        self._deadline = 0.0
        self.local_reserve = 0.001
        self._thread = threading.Thread(target=self._work, daemon=True)

    def prepare(self) -> None:
        # All synchronization and continuation state was prepared before launch.
        return None

    def start(self, context: StopContext) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._deadline = context.deadline
            try:
                self._thread.start()
            except BaseException as error:
                self._record(error)
                self._finished.set()

    def cancel(self, deadline: float) -> None:
        with self._lock:
            if not self._started:
                # A prepared action can be cancelled before stage initiation.
                # Settle through the SAME continuation inside the supplied
                # allowance, preserving the parent's local-cleanup reserve.
                self.start(
                    StopContext("cancelled", None, deadline - self.local_reserve)
                )
            else:
                self._deadline = min(self._deadline, deadline)

    def _record(self, error: BaseException, kind: FailureKind | None = None) -> None:
        with self._lock:
            if not any(f.error is error for f in self._failures):
                self._failures.append(
                    FailureInfo(error, kind or _kind(error), FailurePhase.ACTION)
                )

    def _helper(self, operation: InfrastructureOperation) -> None:
        with self._lock:
            remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ContainerError("container cleanup allowance exhausted")
        # Helper execution AND default-policy local cleanup fit this remainder.
        # Preserve a positive share for the second helper, without fresh grace.
        allocation = remaining / 2 if operation is self.operations[0] else remaining
        grace = allocation / 4
        operation.started_at = _timestamp()
        try:
            boundary_error: BaseException | None = None
            try:
                with self._launch(
                    operation.argv,
                    deadline_seconds=allocation - grace,
                    grace_seconds=grace,
                    capture_limit=0,
                ) as owner:
                    with self._lock:
                        self.owners.append(owner)
                    operation.result = owner.stream(
                        operation.collector.chunk, on_stream_end=operation.collector.end
                    )
            except BaseException as error:
                boundary_error = error
            result = operation.result
            if result is not None:
                operation.collector.finish(result)
            primary: BaseException | None = operation.collector.failure
            if (
                primary is None
                and result is not None
                and result.returncode not in (None, 0)
            ):
                primary = ContainerError(operation.name + " returned nonzero status")
            if primary is not None:
                self._record(primary)
            failures = CleanupFailures(primary)
            for failure in () if result is None else result.lifecycle.failures:
                self._record(failure.error, failure.kind)

                def propagate(error: BaseException = failure.error) -> None:
                    raise error

                failures.run(
                    propagate,
                    ordinary=(Exception,)
                    if failure.kind is FailureKind.ORDINARY
                    else (),
                )
            if boundary_error is not None:
                self._record(boundary_error)

                def boundary() -> None:
                    assert boundary_error is not None
                    raise boundary_error

                failures.run(boundary, ordinary=(OSError, ContainerError))
            if (
                result is None
                or result.timed_out
                or not result.lifecycle.complete
                or any(
                    s.outcome is not StreamOutcome.EOF
                    for s in operation.collector.summaries.values()
                )
            ):

                def incomplete() -> None:
                    raise ContainerError("cleanup helper observation incomplete")

                failures.run(incomplete, ordinary=(ContainerError,))
            selected = failures.complete() or primary
            if selected is not None:
                raise selected
            if result is None or result.returncode != 0:
                raise ContainerError(
                    operation.name + " returned nonzero/unknown status"
                )
        finally:
            operation.ended_at = _timestamp()

    def _work(self) -> None:
        try:
            if self.identity is None:
                raise ContainerError("startup identity unknown; resources unresolved")
            rm, listing = self.operations
            rm.argv = ("docker", "rm", "-f", self.identity)
            listing.argv = (
                "docker",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "id=" + self.identity,
                "--format",
                "{{.ID}}",
            )
            for operation in self.operations:
                try:
                    self._helper(operation)
                    if operation is listing:
                        summary = listing.collector.summaries[ProcessStream.STDOUT]
                        # Exactly empty, fully collected stdout; never inspect
                        # semantics, a tail, an error or an absent status.
                        if summary.observed_bytes == 0:
                            self.confirmed_absent = True
                        else:
                            data = bytes(summary.prefix)
                            if (
                                data.removesuffix(b"\n").lower()
                                == self.identity.encode().lower()
                            ):
                                raise ContainerError("recorded container still present")
                            raise ContainerError(
                                "unexpected removal confirmation output"
                            )
                except BaseException as error:
                    operation.failure = error
                    self._record(error)
                    try:
                        diagnostic = str(error)
                    except BaseException as rendering_error:
                        self._record(rendering_error)
                        diagnostic = "cleanup diagnostic rendering unavailable"
                    operation.diagnostics.append(diagnostic)
                    # Independent confirmation follows ordinary rm failures.
                    # Interruption and defects remain authoritative, but do not
                    # grant permission to forget a known external resource.
        except BaseException as error:
            self._record(error)
        finally:
            self._finished.set()

    def observe(self) -> ActionResult:
        with self._lock:
            owners = tuple(self.owners)
            done = self._finished.is_set()
        for owner in owners:
            for failure in owner.cleanup_status.failures:
                self._record(failure.error, failure.kind)
        with self._lock:
            return ActionResult(
                done,
                done and all(owner.cleanup_status.complete for owner in owners),
                tuple(
                    sorted(
                        self._failures,
                        key=lambda f: (
                            0
                            if f.kind is FailureKind.INTERRUPTION
                            else 1
                            if f.kind is FailureKind.UNEXPECTED
                            else 2
                        ),
                    )
                ),
            )


class RunSupervisor:
    """Internal one-attempt run boundary; no result persistence or public CLI.

    Local execution children reserve at most 1/10 of the configured cleanup
    allowance each. Four pre-command children plus the command's 1/10 local
    reserve and 1/2 domain stage fit ONE allowance. Failure before command launch
    uses the same domain action with only that reserved half, never a new plan.
    """

    def __init__(
        self,
        config: Configuration,
        snapshot: SnapshotReservation,
        *,
        launch: Callable[..., Any] = OwnedProcess.launch,
    ) -> None:
        self.execution = ContainerExecution(config, snapshot, launch=launch)
        self.snapshot = snapshot
        self.cleanup = ContainerCleanup(None, launch=launch)
        self.cleanup.local_reserve = config.cleanup_grace_seconds / 10
        self.failure: BaseException | None = None
        self.snapshot_deleted = False
        self.unresolved_resources = False
        self._allowance = config.cleanup_grace_seconds / 2
        self._lock = threading.Lock()
        self._started = False
        self.execution.command_cleanup_action = self.cleanup

    def execute(
        self,
        startup: ChunkSink,
        command: ChunkSink,
        *,
        startup_end: Callable[[ProcessStream, StreamOutcome], None] | None = None,
        command_end: Callable[[ProcessStream, StreamOutcome], None] | None = None,
    ) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("run supervision already attempted")
            self._started = True
        primary: BaseException | None = None
        try:
            self.execution.execute(
                startup, command, startup_end=startup_end, command_end=command_end
            )
        except BaseException as error:
            primary = error
        failures = CleanupFailures(primary)
        self.failure = primary
        # Until finalization establishes otherwise, interruption must leave a
        # conservative resource state, not the constructor's optimistic flags.
        self.unresolved_resources = True
        recorded: set[int] = set()

        def retain(infos: tuple[FailureInfo, ...]) -> None:
            for info in infos:
                if id(info.error) in recorded:
                    continue
                recorded.add(id(info.error))

                def propagate(error: BaseException = info.error) -> None:
                    raise error

                failures.run(
                    propagate,
                    ordinary=(Exception,) if info.kind is FailureKind.ORDINARY else (),
                )

        def wait() -> None:
            self.cleanup.identity = self.execution.container_id
            # If command shutdown already started this action, start is a no-op;
            # the original stage deadline remains authoritative.
            self.cleanup.prepare()
            self.cleanup.start(
                StopContext("terminal", None, time.monotonic() + self._allowance)
            )
            with self.cleanup._lock:
                remaining = max(0, self.cleanup._deadline - time.monotonic())
            retain(self.wait_cleanup(remaining).failures)

        failures.run(wait, ordinary=(OSError, ContainerError))
        observation = ActionResult(False, False)
        local_complete = False

        def observe() -> None:
            nonlocal observation, local_complete
            observation = self.cleanup.observe()
            retain(observation.failures)
            statuses = self.execution.cleanup_status
            for status in statuses:
                retain(status.failures)
            local_complete = all(s.complete for s in statuses)
            self.unresolved_resources = not (
                self.cleanup.confirmed_absent
                and observation.cleanup_complete
                and local_complete
            )

        # An interrupted wait cannot suppress independent observations, while
        # interrupted final observation keeps the conservative defaults above.
        # Neither boundary restarts the retained action or grants fresh grace.
        failures.run(observe, ordinary=(OSError, ContainerError))
        if (
            self.cleanup.confirmed_absent
            and observation.cleanup_complete
            and local_complete
        ):

            def remove() -> None:
                self.snapshot.remove(
                    deadline=self.cleanup._deadline + self.cleanup.local_reserve
                )
                if self.snapshot.removal_failure is not None:
                    raise self.snapshot.removal_failure
                self.snapshot_deleted = not self.snapshot.path.exists()
                if not self.snapshot_deleted:
                    raise ContainerError("owned snapshot remains after cleanup")

            failures.run(remove, ordinary=(Exception,))
        if (
            self.unresolved_resources
            or not observation.cleanup_complete
            or not local_complete
        ):

            def unresolved() -> None:
                raise ContainerError(
                    "run has unresolved resources or incomplete cleanup"
                )

            failures.run(unresolved, ordinary=(ContainerError,))
        try:
            self.failure = failures.complete() or primary
        except BaseException as error:
            self.failure = error
            raise
        if self.failure is not None and not isinstance(self.failure, Exception):
            raise self.failure

    @property
    def cleanup_status(self) -> LifecycleResult:
        action = self.cleanup.observe()
        statuses = self.execution.cleanup_status
        failures = list(action.failures)
        for status in statuses:
            for info in status.failures:
                if not any(existing.error is info.error for existing in failures):
                    failures.append(info)
        return LifecycleResult(
            action.done
            and action.cleanup_complete
            and all(s.complete for s in statuses),
            tuple(failures),
        )

    def wait_cleanup(self, timeout: float) -> LifecycleResult:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError(
                "cleanup observation timeout must be finite and nonnegative"
            )
        end = time.monotonic() + timeout
        while not self.cleanup_status.complete and time.monotonic() < end:
            time.sleep(min(0.002, max(0, end - time.monotonic())))
        return self.cleanup_status
