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


def assert_policy_trace(actual, *, working, cleanup, prefix):
    """Require a predeclared exact prefix and independent literal cleanup."""
    expected = (*working[:prefix], *cleanup)
    if tuple(actual) != expected:
        raise AssertionError(
            f"unexpected policy trace: {actual!r}; expected {expected!r}"
        )


def assert_allowances(actual, expected):
    """Check positive shared remainders, not approximate admission decisions."""
    import pytest

    actual, expected = tuple(actual), tuple(expected)
    if not all(value > 0 for value in actual):
        raise AssertionError(f"nonpositive admitted allowance: {actual!r}")
    if actual != pytest.approx(expected, rel=1e-12, abs=1e-12):
        raise AssertionError(
            f"renewed or incorrect budget: {actual!r}; expected {expected!r}"
        )


class ScriptedHelper:
    """Explicitly synthetic no-fd helper; no process, stream or release acquired.

    Delivers separate protocol bytes/EOF exactly once. Time is charged only at
    the named completion boundary, never on clock reads or cleanup observation.
    Context exit counts are observations, not simulated OS close evidence.
    """

    def __init__(self, step, clock, *, expenditure=None):
        from footman.process import LifecycleResult

        self.step = step
        self.clock = clock
        self.expenditure = expenditure
        self.collections = 0
        self.context_exits = 0
        self.cleanup_status = LifecycleResult(True)
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.context_exits += 1

    def stream(self, callback, *, on_stream_end=None):
        from footman.process import ProcessResult, ProcessStream, StreamOutcome

        if self.collections:
            raise RuntimeError("scripted helper already collected")
        self.collections += 1
        for stream, key in (
            (ProcessStream.STDOUT, "stdout"),
            (ProcessStream.STDERR, "stderr"),
        ):
            data = self.step.get(key, b"")
            if data:
                callback(stream, data)
            if on_stream_end is not None:
                on_stream_end(stream, StreamOutcome.EOF)
        if self.expenditure is not None:
            seconds, label = self.expenditure
            self.clock.advance(seconds, label=label)
        self.result = ProcessResult(
            self.step.get("stdout", b""),
            self.step.get("stderr", b""),
            self.step.get("status", 0),
            False,
            self.cleanup_status,
            tuple((s, StreamOutcome.EOF) for s in ProcessStream),
        )
        return self.result


class LogicalScenario:
    """Real supervisor admission/parsing with synthetic helpers and one clock.

    Only footman.container's time reference changes, before construction. The
    supervisor/cleanup/process clocks and real watchdogs remain untouched.
    Acquired helpers are no-fd values; no new OS lifecycle branch is introduced.
    """

    def __init__(self, source, config, *, expenditures=()):
        from tests.clock_support import ManualClock
        from tests.dispatch_support import removal
        from tests.launcher_case_support import steps

        self.source, self.config = source, config
        self.clock = ManualClock()
        self.working = tuple(steps(source, config))
        self.cleanup = tuple(removal())
        self.expenditures = dict(expenditures)
        self.journal = LaunchJournal(
            working=tuple(tuple(s["argv"]) for s in self.working),
            cleanup=tuple(tuple(s["argv"]) for s in self.cleanup),
        )
        self.helpers = []
        self._script = {tuple(s["argv"]): (i, s) for i, s in enumerate(self.working)}
        self._script.update({tuple(s["argv"]): (None, s) for s in self.cleanup})

    def __enter__(self):
        import footman.container as container
        from tests.clock_support import module_clock
        from tests.dispatch_support import api

        self._scope = module_clock(container, self.clock)
        self._scope.__enter__()
        try:
            self.run = api().RunSupervisor(self.config, self.source, launch=self.launch)
        except BaseException as error:
            self._scope.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(self, *args):
        return self._scope.__exit__(*args)

    def launch(self, argv, **options):
        def acquire(actual, **passed):
            index, step = self._script[tuple(actual)]
            helper = ScriptedHelper(
                step, self.clock, expenditure=self.expenditures.get(index)
            )
            self.helpers.append(helper)
            return helper

        return self.journal.launch(argv, acquire=acquire, **options)

    def execute(self):
        self.run.execute(lambda *_: None, lambda *_: None)
        for attempt in self.journal.snapshot():
            if attempt.owner is not None:
                self.journal.observe_result(attempt.ordinal, attempt.owner.result)
        self.assert_helpers_settled()
        return self.run

    def assert_helpers_settled(self):
        for helper in self.helpers:
            if helper.collections != 1 or helper.context_exits != 1:
                raise AssertionError(
                    "scripted helper must be collected/exited exactly once"
                )
            if not helper.cleanup_status.complete or helper.cleanup_status.failures:
                raise AssertionError("scripted helper did not settle cleanly")
