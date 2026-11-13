"""Deadline-only parent observations, never child execution or release authority.

Requests precede acquisition. Independent literal scripts permit cleanup after
any working prefix. Snapshots borrow owners/results; they neither collect nor
signal/wait/reap. Child records and local settlement remain separate evidence.
Only a failed launch handoff forwards context exit to the same sole owner.
"""

import threading
from dataclasses import dataclass, replace
from types import SimpleNamespace

from footman.filesystem.cleanup import CleanupFailures
from footman.process import FailureInfo, FailureKind, FailurePhase


def assert_lifecycle_failures(lifecycle, *, expected=()):
    """Check one helper's failure policy, independently of local completion.

    Default: no failures. A negative scenario may name exact typed operation
    observations from ACTION or CALLBACK. Exception identity, kind, phase,
    stream, order and multiplicity must all match; text/type alone never match.
    Release/setup/read/wait/etc. failures and unexpected defects cannot be
    whitelisted, even by supplying the same FailureInfo. Expected observations
    must be declared for this helper, never inferred from its actual failures.

    This borrows observations only. It neither raises/mutates the retained
    errors nor collects/waits/releases anything. Callers must independently
    establish settlement and validate mandatory child records.
    """
    expected = tuple(expected)
    for info in expected:
        if not isinstance(info, FailureInfo):
            raise TypeError("expected failures must be typed FailureInfo observations")
        if (
            info.phase not in (FailurePhase.ACTION, FailurePhase.CALLBACK)
            or info.kind is FailureKind.UNEXPECTED
        ):
            raise AssertionError("only expected operation failures may be declared")
    actual = lifecycle.failures
    if len(actual) != len(expected) or any(
        observed.error is not declared.error
        or observed.kind is not declared.kind
        or observed.phase is not declared.phase
        or observed.stream is not declared.stream
        for observed, declared in zip(actual, expected, strict=True)
    ):
        raise AssertionError(
            f"unexpected helper failures: actual={actual!r}; expected={expected!r}"
        )


def _raise(error):
    raise error


def _secondary(primary, error):
    """Ordinary observation defects are secondary; first control retains authority."""
    failures = CleanupFailures(primary)
    failures.run(lambda: _raise(error), ordinary=(Exception,))
    failures.complete()


@dataclass(frozen=True)
class ChildRecordObservation:
    """Caller-supplied evidence only, not validation or timeout eligibility."""

    availability: str = "unobserved"
    data: bytes | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class LaunchAttempt:
    ordinal: int
    phase: str
    argv: tuple
    deadline_seconds: object
    grace_seconds: object
    acquisition: str = "requested"
    owner: object = None
    error: BaseException | None = None
    result: object = None
    record: ChildRecordObservation = ChildRecordObservation()


@dataclass
class _Acquisition:
    """Prepared privately, then published as one coherent reference."""

    acquisition: str = "requested"
    owner: object = None
    error: BaseException | None = None


class LaunchJournal:
    """Observe exact admission and protected handoff without adopting an owner.

    Each request has one stable ordinal, literal argv and passed allowances.
    Acquisition runs outside the lock. A prepared outcome reference publishes
    all acquisition fields together, including if lock entry is interrupted.
    Result/record observations use that same ordinal, never filtered positions.
    """

    def __init__(self, *, working, cleanup):
        self._working = tuple(tuple(argv) for argv in working)
        self._cleanup = tuple(tuple(argv) for argv in cleanup)
        self._working_cursor = 0
        self._cleanup_cursor = 0
        self._cleaning = False
        self._attempts = []
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            observations = []
            for slot in self._attempts:
                outcome = slot.outcome
                observations.append(
                    replace(
                        slot.request,
                        acquisition=outcome.acquisition,
                        owner=outcome.owner,
                        error=outcome.error,
                        result=slot.result,
                        record=slot.record,
                    )
                )
            return tuple(observations)

    def _publish(self, slot, outcome):
        with self._lock:
            slot.outcome = outcome

    def launch(self, argv, *, acquire, **options):
        literal = tuple(argv)
        with self._lock:
            cleanup = bool(literal and len(literal) > 1 and literal[1] in ("rm", "ps"))
            # Match only the current exact cleanup step, never search ahead.
            if self._cleanup_cursor < len(self._cleanup):
                cleanup = cleanup or literal == self._cleanup[self._cleanup_cursor]
            phase = "cleanup" if cleanup else "working"
            request = LaunchAttempt(
                len(self._attempts),
                phase,
                literal,
                options.get("deadline_seconds"),
                options.get("grace_seconds"),
            )
            slot = SimpleNamespace(
                request=request,
                outcome=_Acquisition(),
                result=None,
                record=request.record,
            )
            self._attempts.append(slot)
            try:
                if cleanup:
                    if (
                        self._cleanup_cursor >= len(self._cleanup)
                        or literal != self._cleanup[self._cleanup_cursor]
                    ):
                        raise AssertionError("unexpected cleanup argv")
                    self._cleaning = True
                    self._cleanup_cursor += 1
                else:
                    if (
                        self._cleaning
                        or self._working_cursor >= len(self._working)
                        or literal != self._working[self._working_cursor]
                    ):
                        raise AssertionError("unexpected working argv")
                    self._working_cursor += 1
            except AssertionError as error:
                slot.outcome = _Acquisition("failed", error=error)
                raise
        # Prepare all outcome storage before the acquisition/handoff boundary.
        acquired = _Acquisition("acquired")
        failed = _Acquisition("failed")
        interrupted = _Acquisition("interrupted")
        try:
            owner = acquire(literal, **options)
        except BaseException as error:
            outcome = failed if isinstance(error, Exception) else interrupted
            outcome.error = error
            try:
                self._publish(slot, outcome)
            except BaseException as publication_error:
                # A single-reference fallback still gives coherent snapshots.
                slot.outcome = outcome
                _secondary(error, publication_error)
            raise
        acquired.owner = owner
        try:
            self._publish(slot, acquired)
        except BaseException as error:
            acquired.error = error
            slot.outcome = acquired
            # Handoff failed: forward exactly once to the existing sole owner's
            # context exit. Retain it even when its continuation is incomplete.
            # Do not signal/reap/collect or release borrowed streams ourselves.
            failures = CleanupFailures(error)
            failures.run(
                lambda error=error: owner.__exit__(
                    type(error), error, error.__traceback__
                ),
                ordinary=(OSError,),
            )
            failures.complete()
            raise
        return owner

    def _slot(self, ordinal):
        if type(ordinal) is not int or not 0 <= ordinal < len(self._attempts):
            raise AssertionError("unknown request ordinal")
        return self._attempts[ordinal]

    def observe_result(self, ordinal, result):
        with self._lock:
            slot = self._slot(ordinal)
            if slot.outcome.acquisition != "acquired":
                raise AssertionError("result requires acquired attempt")
            if slot.result is not None:
                raise AssertionError("result already associated")
            slot.result = result

    def observe_record(self, ordinal, *, availability, data=None, error=None):
        """Retain caller observations without opening, validating or tolerating files."""
        if availability not in ("unobserved", "available", "absent", "read-failed"):
            raise ValueError("unknown record availability")
        observation = ChildRecordObservation(availability, data, error)
        with self._lock:
            self._slot(ordinal).record = observation

    def render_diagnostics(self):
        """Render retained observations only, at most 64 KiB of UTF-8."""
        rows = []
        for attempt in self.snapshot():
            result = attempt.result
            owner = attempt.owner
            # Compact metadata precedes potentially huge argv/record bytes so
            # truncation leaves independent timeout/settlement dimensions visible.
            rows.append(
                f"phase={attempt.phase} ordinal={attempt.ordinal} "
                f"deadline_seconds={attempt.deadline_seconds!r} grace_seconds={attempt.grace_seconds!r} "
                f"acquisition={attempt.acquisition} error={attempt.error!r} "
                f"timed_out={getattr(result, 'timed_out', None)!r} "
                f"returncode={getattr(result, 'returncode', None)!r} "
                f"stream_outcomes={getattr(result, 'stream_outcomes', None)!r} "
                f"cleanup={getattr(owner, 'cleanup_status', None)!r} "
                f"record={attempt.record.availability!r} argv={attempt.argv!r} "
                f"record_data={attempt.record.data!r} record_error={attempt.record.error!r}"
            )
        encoded = "\n".join(rows).encode("utf-8", errors="backslashreplace")
        if len(encoded) <= 65536:
            return encoded.decode("utf-8")
        marker = b"\n[diagnostics truncated]"
        return (
            encoded[: 65536 - len(marker)].decode("utf-8", errors="ignore")
            + marker.decode()
        )

    def annotate_failure(self, primary):
        """Ordinary diagnostic defects are secondary; the first control wins."""
        try:
            primary.add_note(self.render_diagnostics())
        except BaseException as error:
            _secondary(primary, error)
            try:
                primary.add_note("deadline diagnostics unavailable")
            except BaseException as note_error:
                _secondary(primary, note_error)
