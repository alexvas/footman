"""Shared test-only controlled logical clock core.

Policy-time tests must decide *themselves* when their scenario's logical time
moves.  A scheduler pause, a thread-interleaving delay or simply reading a clock
more often is not elapsed policy time, so this core separates three things that
duplicated test clocks used to blend together:

* a **read** (:meth:`ManualClock.monotonic`) never advances time and never
  records anything, so read frequency and interleaving cannot change a scenario;
* an **advance** (:meth:`ManualClock.advance`) is an explicit, scenario-owned
  boundary and the only way logical time moves; and
* a **journal** (:meth:`ManualClock.journal`) records every accepted advance
  exactly once as an immutable ``delta + optional scenario label`` pair, so a
  test can prove the expenditure it intended instead of measuring wall time or
  counting calls.

Logical time is therefore distinct from wall time.  Real synchronization and
real test watchdogs stay real: this module deliberately provides no sleeping and
no automatic wait accounting.  Advancement sites belong in the scenario, and the
scoped adapters attach this core to a selected module's time reference, a
selected stage pause, a selected event timeout or an explicitly modelled
interrupted wait.

Time and the journal are updated under one lock, so concurrent scenario threads
and supervisors share a single coherent timeline. The journal is also the single
source of expenditure: charged time is never a second running total, because a
running total drifts away from the deltas that were actually accepted (a large
accumulator absorbs small deltas, while Python's compensated ``sum`` does not).
:meth:`ManualClock.elapsed` therefore sums the journal, and
:meth:`ManualClock.advance` validates that same prospective sum before appending,
so ``elapsed()`` always equals ``sum(record.seconds for record in clock.journal())``
exactly, at any epoch magnitude. That summation costs ``O(len(journal))`` per
read and per advancement, which is bounded by a scenario's advancement count.
Every rejected operation leaves time and journal unchanged, which is what makes a
charged-once assertion meaningful.
"""

from __future__ import annotations

import contextlib
import math
import numbers
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Advancement:
    """One accepted logical-time expenditure: a delta and an optional label.

    ``label`` names the scenario boundary that charged the delta, which lets a
    test assert *which* stage, timeout or interrupted wait paid for elapsed
    logical time.
    """

    seconds: float
    label: str | None = None


def _as_real_float(value: object, kind: str) -> float:
    """Return *value* as a float, or raise an actionable ``TypeError`` naming *kind*."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(
            f"{kind} must be a real number, got {type(value).__name__} "
            f"(value={value!r})"
        )
    return float(value)


class ManualClock:
    """Thread-safe logical clock advanced only by explicit, valid deltas.

    The clock is monotonic by construction: advancement accepts only finite
    nonnegative deltas that keep both the resulting logical time and the
    resulting journal sum finite.  A rejected advancement raises an actionable
    :class:`TypeError`, :class:`ValueError` or :class:`OverflowError` and leaves
    time and journal untouched.
    """

    def __init__(self, origin: float = 0.0) -> None:
        origin_seconds = _as_real_float(origin, "ManualClock clock origin")
        if not math.isfinite(origin_seconds):
            raise ValueError(
                f"ManualClock clock origin must be a finite number, "
                f"got {origin_seconds!r}"
            )
        self._now = origin_seconds
        self._journal: list[Advancement] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        """Return the current logical time; never touches time or journal."""
        with self._lock:
            return self._now

    def advance(self, seconds: float, *, label: str | None = None) -> float:
        """Charge one scenario boundary and return the new logical time.

        *seconds* must be a real, finite, nonnegative number and must keep the
        resulting logical time and the resulting journal sum finite.  Each
        accepted advance is journalled exactly once; every rejection leaves time
        and journal unchanged, so a failed advancement attempt is not an
        expenditure.
        """
        delta = _as_real_float(seconds, "ManualClock.advance seconds")
        if label is not None and not isinstance(label, str):
            raise TypeError(
                f"ManualClock.advance label must be a string or None, "
                f"got {type(label).__name__}"
            )
        with self._lock:
            if not math.isfinite(delta):
                raise ValueError(
                    f"ManualClock.advance seconds must be a finite number, "
                    f"got {delta!r} (label={label!r})"
                )
            if delta < 0.0:
                raise ValueError(
                    f"ManualClock.advance logical time never moves backwards, so "
                    f"seconds must be nonnegative, got {delta!r} (label={label!r})"
                )
            now = self._now + delta
            if not math.isfinite(now):
                raise OverflowError(
                    f"ManualClock.advance would overflow the finite logical time: "
                    f"{self._now!r} + {delta!r} is not finite (label={label!r})"
                )
            charged = self._journal_charged_locked(extra=delta)
            if not math.isfinite(charged):
                raise OverflowError(
                    f"ManualClock.advance would overflow the finite charged "
                    f"journal total: summing the journal with {delta!r} is not "
                    f"finite (label={label!r})"
                )
            self._now = now
            self._journal.append(Advancement(delta, label))
            return now

    def journal(self) -> tuple[Advancement, ...]:
        """Return an immutable snapshot of the accepted advances so far.

        The snapshot is a point-in-time copy: later accepted advances do not
        change an earlier snapshot, and a snapshot consumer cannot mutate clock
        state through it.
        """
        with self._lock:
            return tuple(self._journal)

    def elapsed(self) -> float:
        """Return the charged logical time: the sum of the journalled deltas.

        The value is summed from the journal itself, so it always equals
        ``sum(record.seconds for record in self.journal())`` exactly, whatever
        the epoch's magnitude or the deltas' magnitudes.  A delta too small to
        change ``monotonic()`` is still charged, and neither the epoch's spacing
        nor a naive running total can inflate or erase a charged interval.
        """
        with self._lock:
            return self._journal_charged_locked()

    def _journal_charged_locked(self, extra: float | None = None) -> float:
        """Sum the journalled deltas, optionally including one prospective one.

        The caller must hold the lock.  Using one summation path for both
        :meth:`elapsed` and the :meth:`advance` guard keeps the reported charge
        identical to the documented journal sum, instead of drifting through a
        separate accumulator.
        """
        seconds = [record.seconds for record in self._journal]
        if extra is not None:
            seconds.append(extra)
        return sum(seconds)


class CallableClock:
    """A :class:`ManualClock` exposed as a component's injected clock callable.

    Some components take a zero-argument monotonic callable (an activity
    monitor's ``clock=``/``shutdown_clock=``, an observability harness's
    injected clock) instead of reading a module's own ``time`` reference, so
    ``module_clock`` is the wrong tool: there is no module reference to scope.
    This adapter is that callable, backed by the same read-pure, journalled
    core, so an injected-seam scenario gets validated advancement and expendable
    journal evidence instead of another local ``self.now += seconds`` fake.

    ``advance_to`` is the one extra convenience such schedules need: it turns an
    absolute scheduled deadline into exactly one explicit delta, charges nothing
    when that deadline has already passed, and never rewinds.  Time still moves
    only at named boundaries; no read advances it.
    """

    def __init__(self, origin: float = 0.0) -> None:
        self._clock = ManualClock(origin=origin)

    def __call__(self) -> float:
        """Return the current logical time; touches neither time nor journal."""
        return self._clock.monotonic()

    @property
    def now(self) -> float:
        """Readable alias of :meth:`__call__` for scenario assertions."""
        return self._clock.monotonic()

    def advance(self, seconds: float, *, label: str | None = None) -> float:
        """Charge one scenario boundary and return the new logical time."""
        return self._clock.advance(seconds, label=label)

    def advance_to(self, when: float, *, label: str | None = None) -> float:
        """Charge the single delta up to absolute logical time *when*.

        An already-reached or already-passed deadline charges nothing, so a
        schedule that fires late is not silently billed for the lateness, and a
        second arrival at the same deadline is not charged twice.
        """
        target = _as_real_float(when, "CallableClock.advance_to absolute time")
        if not math.isfinite(target):
            raise ValueError(
                f"CallableClock.advance_to absolute time must be a finite "
                f"number, got {target!r} (label={label!r})"
            )
        delta = target - self._clock.monotonic()
        if delta <= 0.0:
            return self._clock.monotonic()
        return self._clock.advance(delta, label=label)

    def elapsed(self) -> float:
        """Return the charged logical time, summed from the journal."""
        return self._clock.elapsed()

    def journal(self) -> tuple[Advancement, ...]:
        """Return the immutable advancement journal."""
        return self._clock.journal()


# ---------------------------------------------------------------------------
# Scoped adapters: one module timeline, selected waits, real watchdogs
# ---------------------------------------------------------------------------


class ClockDomainError(RuntimeError):
    """A module's time reference already carries a different logical timeline."""


class UnsupportedTimeout(ValueError):
    """A simulated wait cannot represent the requested timeout."""


class WatchdogTimeout(AssertionError):
    """A real coordination watchdog expired: the awaited work never completed."""


class UnsupportedWatchdogTarget(ValueError):
    """A real-wait path cannot supervise an object with no real wait operation."""


class SettlementIncomplete(RuntimeError):
    """A scenario's settlement step failed, so its scenario patches stay armed.

    A live participant may still read logical timestamps or call an installed
    wait, so nothing is restored automatically: the event adapters, the startup
    hook and every module time reference of that attachment's clock stay in
    place until the scenario settles its workers and calls
    ``attachment.settle_and_restore()``.
    """


class _Signalling(Protocol):
    """The observation a modelled wait polls instead of blocking on."""

    def is_set(self) -> bool: ...


class _Waitable(_Signalling, Protocol):
    """A real synchronization object that can be waited on and observed."""

    def wait(self, timeout: float | None = ...) -> bool: ...


def _validated_interval(seconds: object, kind: str, *, label: str) -> float:
    """Return *seconds* as a finite nonnegative float or raise actionably.

    ``kind`` names the installed scenario function and ``label`` names the
    boundary being charged, so every refusal identifies both the adapter and
    the scenario step that caused it.
    """
    try:
        delta = _as_real_float(seconds, kind)
    except TypeError as error:
        raise TypeError(f"{error} (label={label!r})") from error
    if not math.isfinite(delta):
        raise ValueError(
            f"{kind} (label={label!r}) needs a finite logical interval, got {delta!r}"
        )
    if delta < 0.0:
        raise ValueError(
            f"{kind} (label={label!r}) needs a nonnegative logical interval, "
            f"got {delta!r}"
        )
    return delta


@dataclass(frozen=True, slots=True)
class ModuleClock:
    """Handle for one installed module-local logical time reference."""

    module: ModuleType
    clock: ManualClock
    functions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Domain:
    """One installed module timeline: a clock plus the reads it overrides."""

    clock: ManualClock
    functions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RetainedDomain:
    """One module time reference kept patched because participants are live.

    The recorded ``original`` is what that one context found on entry, so a
    nested inner frame records the enclosing frame's patched reference rather
    than the module's real one.
    """

    module: ModuleType
    original: object
    domain: _Domain


_ACTIVE_DOMAINS: dict[object, list[_Domain]] = {}
_ACTIVE_PARTICIPANTS: dict[ManualClock, int] = {}
_RETAINED_DOMAINS: dict[ManualClock, list[_RetainedDomain]] = {}
# One lock guards all three registries: a module may only leave its timeline
# when no attachment still has a live participant, so both facts must move
# together.  Nothing under this lock calls back into code that takes it again.
_DOMAIN_LOCK = threading.Lock()


def _restore_retained_domains(entries: list[_RetainedDomain]) -> None:
    """Give retained modules their real time reference back, outermost last.

    ``entries`` are recorded in unwind order, innermost context first, because
    that is the order in which the retained ``module_clock`` frames exited.  Each
    entry remembers the reference its own context found on entry: an inner frame
    recorded the wrapper installed by the enclosing frame, and only the outermost
    frame recorded the module's original real-time reference.  Restoring in that
    recorded order therefore ends with the outermost entry, which puts the
    original reference back and leaves no wrapper behind; reversing it would put
    the original back first and then re-install the stale wrapper of an already
    exited inner frame.
    """
    for entry in entries:
        with _DOMAIN_LOCK:
            stack = _ACTIVE_DOMAINS.get(entry.module)
            if stack is not None:
                stack.pop()
                if not stack:
                    del _ACTIVE_DOMAINS[entry.module]
        entry.module.time = entry.original


def _retained_domains_for(module: ModuleType) -> list[_RetainedDomain]:
    """Return every retained context recorded for *module*, oldest retained first.

    The caller must already hold ``_DOMAIN_LOCK``.  Retention is recorded per
    clock, but it constrains **that module**: a different module sharing the same
    clock has no patched reference of its own to lose, so it stays enterable.
    """
    return [
        entry
        for entries in _RETAINED_DOMAINS.values()
        for entry in entries
        if entry.module is module
    ]


def _leave_domain(
    module: ModuleType,
    original: object,
    domain: _Domain,
    *,
    error: BaseException | None,
) -> None:
    """Restore *module*'s time reference unless participants still need it.

    A module-patched timeline must outlive the participants that read it, so a
    pending attachment retains the patched reference and the domain entry
    instead of handing real time to a worker that is still running.  With an
    error already propagating the retention is only noted, keeping that error
    authoritative; with none, the scenario is told to recover explicitly.
    """
    with _DOMAIN_LOCK:
        pending = _ACTIVE_PARTICIPANTS.get(domain.clock, 0) > 0
        if pending:
            _RETAINED_DOMAINS.setdefault(domain.clock, []).append(
                _RetainedDomain(module, original, domain)
            )
        else:
            stack = _ACTIVE_DOMAINS.get(module)
            if stack is not None:
                stack.pop()
                if not stack:
                    del _ACTIVE_DOMAINS[module]
    if not pending:
        module.time = original
        return
    module_name = getattr(module, "__name__", repr(module))
    note = (
        f"module {module_name!r} keeps its logical time reference: a scenario "
        f"participant of that clock has not settled, and restoring real time "
        f"while it still runs would mix two timelines. Settle the participants "
        f"and call attachment.settle_and_restore(), which restores this module "
        f"reference too"
    )
    if error is None:
        raise SettlementIncomplete(
            f"module_clock cannot leave the timeline of module {module_name!r}: {note}"
        )
    _add_note(error, note)


class _ModuleTime:
    """A module's time reference: selected reads are logical, the rest is real.

    Only the ``functions`` named by the scenario read the logical clock.  Every
    other attribute — ``sleep``, ``monotonic_ns``, ``perf_counter`` — is the
    real standard-library object, so real synchronization and real integration
    measurement keep working inside a patched module.
    """

    def __init__(self, real: object, domain: _Domain) -> None:
        self._real = real
        self._domain = domain

    def __getattr__(self, name: str) -> object:
        domain = self._domain
        if name in domain.functions:
            clock = domain.clock

            def logical_read() -> float:
                return clock.monotonic()

            return logical_read
        return getattr(self._real, name)


@contextlib.contextmanager
def module_clock(
    module: ModuleType,
    clock: ManualClock,
    *,
    functions: tuple[str, ...] = ("monotonic",),
) -> Iterator[ModuleClock]:
    """Install *clock* as *module*'s time reference and restore it on exit.

    The shared standard-library ``time`` module is never patched: only this
    module's own reference is replaced, and only the selected zero-argument
    clock reads change meaning.  One module has one active timeline, so an
    overlapping context with a different clock or a different override set is
    rejected with :class:`ClockDomainError` instead of silently nesting.  The
    reference is restored on normal exit, assertion failure and interruption.

    A module whose context was retained by a failed settlement step stays closed
    to re-entry until that recovery completes: entering again - even with the
    same clock - would hide a reference some live participant may still be
    reading.  Settle the participants and call
    ``attachment.settle_and_restore()`` first, then enter a context for that
    module again.  Compatible same-clock nesting stays allowed while nothing is
    retained, and retention never blocks a *different* module of the same clock.
    """
    requested = tuple(functions)
    module_name = getattr(module, "__name__", repr(module))
    if not requested:
        raise ValueError(
            f"module_clock needs at least one clock function for module "
            f"{module_name!r} (pass functions=('monotonic',) for the usual case)"
        )
    original = getattr(module, "time", None)
    if original is None:
        raise ValueError(
            f"module {module_name!r} has no time reference for module_clock to "
            f"replace; a patched module must bind its own time reference"
        )
    for function in requested:
        if not callable(getattr(original, function, None)):
            raise ValueError(
                f"module_clock cannot override {function!r} of module "
                f"{module_name!r}: not a callable of its time reference"
            )

    wanted = _Domain(clock, requested)
    with _DOMAIN_LOCK:
        retained = _retained_domains_for(module)
        if retained:
            oldest = retained[0]
            raise ClockDomainError(
                f"module {module_name!r} is still inside a retained logical time "
                f"context (origin {oldest.domain.clock.monotonic()!r}, functions "
                f"{oldest.domain.functions!r}): an earlier module_clock of this "
                f"module left its patched reference in place because its "
                f"settlement step failed, and its participants may still read "
                f"that timeline. Entering another context, even with the same "
                f"clock, would hide the retained one. Complete the recovery "
                f"first: settle the participants and call "
                f"attachment.settle_and_restore(), which gives this module its "
                f"original time reference back; only then can module_clock be "
                f"entered for it again"
            )
        stack = _ACTIVE_DOMAINS.get(module)
        if stack:
            current = stack[-1]
            if current != wanted:
                raise ClockDomainError(
                    f"module {module_name!r} already runs one logical clock "
                    f"timeline (origin {current.clock.monotonic()!r}, functions "
                    f"{current.functions!r}); a module-patched context has a "
                    f"single timeline, so the requested timeline (origin "
                    f"{clock.monotonic()!r}, functions {requested!r}) is not "
                    f"supported: reuse the module's active clock or patch a "
                    f"different module"
                )
            stack.append(wanted)
        else:
            _ACTIVE_DOMAINS[module] = [wanted]
    try:
        module.time = _ModuleTime(original, wanted)
        yield ModuleClock(module=module, clock=clock, functions=requested)
    except BaseException as propagating:
        _leave_domain(module, original, wanted, error=propagating)
        raise
    else:
        _leave_domain(module, original, wanted, error=None)


@dataclass(frozen=True, slots=True)
class _Replacement:
    """One attribute swap plus its exactly-once undo."""

    target: object
    attribute: str
    existed: bool
    original: object

    def undo(self) -> None:
        if self.existed:
            setattr(self.target, self.attribute, self.original)
        else:
            delattr(self.target, self.attribute)


def _replace_attribute(target: object, attribute: str, value: object) -> _Replacement:
    """Install *value* as *target*'s instance attribute and return its undo.

    Only an existing *instance* attribute is put back: where the replaced name
    came from the class, undo deletes the instance attribute so the class
    binding becomes visible again instead of freezing a bound method on it.
    """
    try:
        namespace = vars(target)
    except TypeError as error:
        raise ValueError(
            f"{type(target).__name__} exposes no instance attributes, so the "
            f"adapter cannot replace {attribute!r} on it"
        ) from error
    existed = attribute in namespace
    original = namespace.get(attribute)
    setattr(target, attribute, value)
    return _Replacement(target, attribute, existed, original)


class StagePause:
    """One module's stage pause replaced by an explicit logical advancement."""

    module: ModuleType
    function: str
    clock: ManualClock
    label: str

    def __init__(
        self, module: ModuleType, function: str, clock: ManualClock, label: str
    ) -> None:
        self.module = module
        self.function = function
        self.clock = clock
        self.label = label
        self._charges: list[float] = []

    @property
    def charged(self) -> tuple[float, ...]:
        """Every interval this pause charged, in order."""
        return tuple(self._charges)

    def __call__(self, seconds: float) -> None:
        """Charge exactly *seconds* of logical time for one policy pause."""
        delta = _validated_interval(
            seconds, f"stage pause {self.function}", label=self.label
        )
        self.clock.advance(delta, label=self.label)
        self._charges.append(delta)


@contextlib.contextmanager
def stage_pause(
    module: ModuleType,
    clock: ManualClock,
    *,
    function: str = "_policy_pause",
    label: str = "stage-pause",
) -> Iterator[StagePause]:
    """Advance *clock* by each interval *module*'s selected pause requests.

    Only this named function advances logical time, and only by the finite
    nonnegative interval the policy itself asked for.  Reading the module's
    clock stays free, so a scenario can prove that elapsed policy time came from
    stage consumption rather than from how often a loop read a clock.  A refusal
    leaves both the clock and :attr:`StagePause.charged` untouched, and the
    original function is restored on every exit path.
    """
    module_name = getattr(module, "__name__", repr(module))
    original = getattr(module, function, None)
    if not callable(original):
        raise ValueError(
            f"stage_pause cannot replace {function!r} of module {module_name!r}: "
            f"it is not a callable the scenario can install"
        )
    pause = StagePause(module, function, clock, label)
    setattr(module, function, pause)
    try:
        yield pause
    finally:
        setattr(module, function, original)


class ModelledWait:
    """One selected event's timeout modelled as a charged logical interval."""

    clock: ManualClock
    label: str

    def __init__(self, target: _Signalling, clock: ManualClock, label: str) -> None:
        self._target = target
        self.clock = clock
        self.label = label
        self._charges: list[float] = []

    @property
    def charged(self) -> tuple[float, ...]:
        """Every interval this modelled wait charged, in order."""
        return tuple(self._charges)

    def wait(self, timeout: float | None = None) -> bool:
        """Model one bounded wait: charge its interval, report the real state.

        An already-set event costs nothing, because the scenario observed
        completion rather than an expiry.  A modelled expiry charges exactly the
        requested interval and reports what the real component actually did.  An
        unbounded or invalid timeout is refused instead of advanced by ``None``:
        real synchronization belongs to :func:`await_event`, which charges
        nothing.
        """
        if timeout is None:
            raise UnsupportedTimeout(
                f"{self.label} cannot model an unbounded wait: logical time "
                f"cannot advance by None; await the real condition with "
                f"await_event(...) or pass the finite interval this scenario "
                f"charges"
            )
        try:
            requested = _validated_interval(
                timeout, f"modelled wait {self.label}", label=self.label
            )
        except (TypeError, ValueError) as error:
            raise UnsupportedTimeout(
                f"{self.label} cannot model an unbounded or invalid wait "
                f"interval {timeout!r}: {error}; use await_event(...) for real "
                f"synchronization instead"
            ) from error
        if self._target.is_set():
            return True
        self.clock.advance(requested, label=self.label)
        self._charges.append(requested)
        return self._target.is_set()


@contextlib.contextmanager
def model_event_wait(
    event: object,
    clock: ManualClock,
    *,
    attribute: str = "wait",
    label: str = "event-timeout",
) -> Iterator[ModelledWait]:
    """Model *event*'s selected wait attribute on one selected timeline.

    The replacement never blocks, so a pending policy loop must terminate on
    logical time.  Real completion is still observable: the returned value is
    the real event's state after the charged interval, never a manufactured one.
    """
    wait = ModelledWait(event, clock, label)  # type: ignore[arg-type]
    replacement = _replace_attribute(event, attribute, wait.wait)
    try:
        yield wait
    finally:
        replacement.undo()


class JoinBudget:
    """One interrupted wait/join: a single armed deadline, explicit expenditure.

    Production joins arm one deadline and then consume it with observation
    steps.  Repeated observation must not re-arm that deadline, and an
    interruption must charge only the interval the scenario says was already
    spent.  Every charge is measured against the deadline's remaining
    allowance rather than against a per-adapter ledger, so polling and one
    interruption together cannot spend more than the allowance and a boundary
    that advanced the shared clock from elsewhere is accounted for.  This
    object makes those statements explicit, so a migrated test can prove the
    remaining budget instead of measuring wall time.
    """

    clock: ManualClock
    label: str
    total: float

    def __init__(self, clock: ManualClock, total: float, label: str) -> None:
        self.clock = clock
        self.label = label
        self.total = total
        self._deadline = clock.monotonic() + total
        self._interrupted = False

    @property
    def deadline(self) -> float:
        """The one armed deadline; observing it never re-arms or re-charges."""
        return self._deadline

    def remaining(self) -> float:
        """The unspent part of the one allowance; reading it charges nothing."""
        return max(0.0, self._deadline - self.clock.monotonic())

    def charge_poll(self, seconds: float) -> bool:
        """Charge one observation step against what the armed deadline allows.

        The allowance is the one armed deadline, not a poll-only ledger: an
        earlier modelled interruption and any other scenario boundary that
        advanced the shared clock have already spent part of it, so a poll is
        measured against :meth:`remaining` and an excessive charge is refused
        before time or journal changes.  The journal, not a second ledger, is
        the record of what this budget charged.
        """
        delta = _validated_interval(
            seconds, f"{self.label} poll", label=f"{self.label} poll"
        )
        unspent = self.remaining()
        if delta > unspent:
            raise ValueError(
                f"{self.label} poll cannot charge {delta!r}: only {unspent!r} "
                f"of the {self.total!r}-second {self.label} allowance is unspent"
            )
        self.clock.advance(delta, label=f"{self.label} poll")
        return self.remaining() > 0.0

    def interrupt_after(
        self, seconds: float, error: BaseException | None = None
    ) -> None:
        """Charge the modelled partial expenditure once, then raise *error*.

        The charge is one expenditure for this interrupted boundary and may not
        exceed what the armed deadline still allows, so an interruption cannot
        spend what a poll or another scenario boundary already charged; a
        refused first interruption changes neither time nor journal.  Repeated
        observation of the same interrupted result is not an expenditure at
        all: it charges nothing and re-raises the scenario's original error
        object, never a copy.
        """
        delta = _validated_interval(
            seconds, f"{self.label} interrupted wait", label=f"{self.label} interrupted"
        )
        if not self._interrupted:
            unspent = self.remaining()
            if delta > unspent:
                raise ValueError(
                    f"{self.label} interrupted wait cannot charge {delta!r}: "
                    f"only {unspent!r} of the {self.total!r}-second "
                    f"{self.label} allowance is unspent"
                )
            self.clock.advance(delta, label=f"{self.label} interrupted")
            self._interrupted = True
        if error is not None:
            raise error

    def interrupt(self, error: BaseException) -> None:
        """Charge the whole unspent allowance once, then raise *error*."""
        self.interrupt_after(self.remaining(), error)


@contextlib.contextmanager
def join_budget(
    clock: ManualClock, *, total: float, label: str = "join"
) -> Iterator[JoinBudget]:
    """Model one wait/join allowance of *total* logical seconds.

    The allowance is armed once, on entry: a scenario that observes the
    interrupted join again sees the same deadline and the same expenditure,
    never a fresh budget.  The context itself charges nothing, so the journal
    shows only the intervals the scenario actually modelled.
    """
    allowance = _validated_interval(total, f"{label} join allowance", label=label)
    if allowance == 0.0:
        raise ValueError(
            f"{label} join allowance must be a positive finite interval to model, "
            f"got {allowance!r}"
        )
    budget = JoinBudget(clock, allowance, label)
    yield budget


@dataclass(frozen=True, slots=True)
class _ModelledSelection:
    """One owner event whose wait is modelled on the scenario timeline."""

    attribute: str
    label: str


@dataclass(frozen=True, slots=True)
class _WatchdogSelection:
    """One owner event whose wait keeps real semantics under a real ceiling."""

    attribute: str
    seconds: float
    name: str


class _WaitAttachments:
    """One scoped attachment of scenario waits to selected owner events.

    Installing an attachment claims a participant on its clock: the claim is
    what keeps an enclosing ``module_clock`` from handing real time back while
    any attached worker may still be running.  The claim is released only when
    the participants settled and every installation was undone, either by the
    context's own exit or by the explicit ``settle_and_restore`` recovery path.
    """

    clock: ManualClock
    settle: Callable[[], None] | None

    def __init__(
        self,
        clock: ManualClock,
        modelled: tuple[_ModelledSelection, ...],
        watchdogs: tuple[_WatchdogSelection, ...],
        settle: Callable[[], None] | None,
    ) -> None:
        self.clock = clock
        self.modelled = modelled
        self.watchdogs = watchdogs
        self.settle = settle
        self._modelled_waits: list[ModelledWait] = []
        self._real_watchdogs: list[RealWatchdog] = []
        self._installed: list[tuple[object, str, _Replacement]] = []
        self._hook: _Replacement | None = None
        self._settlement_failure: BaseException | None = None
        self._restored = False
        self._contexts = contextlib.ExitStack()
        self._claimed = False
        self._claim_participation()

    @property
    def attributes(self) -> tuple[str, ...]:
        """Every selected owner event attribute this attachment replaced."""
        return tuple(attribute for _, attribute, _ in self._installed)

    @property
    def owners(self) -> tuple[object, ...]:
        """Every owner whose events this attachment replaced."""
        return tuple(dict.fromkeys(owner for owner, _, _ in self._installed))

    @property
    def modelled_waits(self) -> tuple[ModelledWait, ...]:
        return tuple(self._modelled_waits)

    @property
    def real_watchdogs(self) -> tuple[RealWatchdog, ...]:
        return tuple(self._real_watchdogs)

    @property
    def settled(self) -> bool:
        """Whether the participants settled and every installation was released.

        ``False`` after a failed settlement step: the adapters, the startup hook
        and the module timelines of this attachment's clock are still installed
        on purpose, because a participant may still be using them.
        """
        return self._restored and self._settlement_failure is None

    @property
    def settlement_failure(self) -> BaseException | None:
        """The settlement error that keeps this attachment installed, if any."""
        return self._settlement_failure

    def install(self, owner: object) -> None:
        """Resolve and validate one owner's selected events, then replace them.

        Every selection is looked up and every watchdog target validated before
        the first attribute is replaced, so a refused selection can never leave
        a half-installed owner whose remaining waits are part scenario and part
        real.
        """
        prepared: list[tuple[str, object, Callable[[float | None], bool]]] = []
        for selection in self.modelled:
            event = getattr(owner, selection.attribute)
            adapter = ModelledWait(event, self.clock, selection.label)
            self._modelled_waits.append(adapter)
            prepared.append((selection.attribute, event, adapter.wait))
        for selection in self.watchdogs:
            event = getattr(owner, selection.attribute)
            # Construction resolves the class-level real wait, so an
            # instance-level-only target is refused here, before any swap.
            adapter = RealWatchdog(event, selection.seconds, selection.name)
            self._real_watchdogs.append(adapter)
            prepared.append((selection.attribute, event, adapter.wait))
        for attribute, event, wait in prepared:
            self._installed.append(
                (owner, attribute, _replace_attribute(event, "wait", wait))
            )

    def restore(self) -> None:
        """Undo every installed wait exactly once, innermost installation first."""
        for _, _, replacement in reversed(self._installed):
            replacement.undo()
        self._installed.clear()

    def enter_context[T](self, context: contextlib.AbstractContextManager[T]) -> T:
        """Keep a scenario patch installed through settlement and recovery.

        Unlike the caller's ExitStack, this stack is not unwound when settlement
        fails. Its contexts leave only after all participants have settled.
        """
        if self._restored:
            raise RuntimeError("cannot install a patch on a restored attachment")
        return self._contexts.enter_context(context)

    def run_settlement(self) -> None:
        """Run the scenario's settlement step, letting any failure escape."""
        if self.settle is not None:
            self.settle()

    def retain_for_recovery(
        self, failure: BaseException, *, hook: _Replacement | None = None
    ) -> None:
        """Record a failed settlement and keep every installation armed."""
        self._settlement_failure = failure
        if hook is not None:
            self._hook = hook

    def discharge(self) -> None:
        """Undo a retained startup hook and every installed wait, once, then release.

        Called only after the participants settled: by the context's own exit or
        by ``settle_and_restore``.  Repeated calls do nothing, so a scenario that
        recovered explicitly and then left the context cannot undo anything
        twice.
        """
        if self._restored:
            return
        if self._hook is not None:
            self._hook.undo()
            self._hook = None
        self.restore()
        self._contexts.close()
        self._restored = True
        self._release_participation()

    def settle_and_restore(self) -> None:
        """Recover from a failed settlement step: settle, then restore once.

        The scenario releases its barriers (in a ``finally`` block) and calls
        this.  The settlement step runs again; if the participants still have
        not settled it raises :class:`SettlementIncomplete` again and every
        scenario wait, the startup hook and the module timelines stay installed.
        Only a successful settlement restores them, exactly once, so a live
        worker can never observe a restored wait or restored real time.  The
        call is idempotent after a successful restoration.
        """
        if self._restored:
            return
        try:
            self.run_settlement()
        except BaseException as error:
            self._settlement_failure = error
            raise SettlementIncomplete(
                "the settlement step of this attachment still fails, so its "
                "participants are not settled: the scenario waits, the startup "
                "hook and the module timelines of this clock stay installed; "
                "release the scenario barriers, let the workers finish, and call "
                "settle_and_restore() again"
            ) from error
        self._settlement_failure = None
        self.discharge()

    def _claim_participation(self) -> None:
        with _DOMAIN_LOCK:
            _ACTIVE_PARTICIPANTS[self.clock] = (
                _ACTIVE_PARTICIPANTS.get(self.clock, 0) + 1
            )
            self._claimed = True

    def _release_participation(self) -> None:
        retained: list[_RetainedDomain] = []
        with _DOMAIN_LOCK:
            if not self._claimed:
                return
            self._claimed = False
            remaining = _ACTIVE_PARTICIPANTS.get(self.clock, 0) - 1
            if remaining > 0:
                _ACTIVE_PARTICIPANTS[self.clock] = remaining
            else:
                _ACTIVE_PARTICIPANTS.pop(self.clock, None)
                retained = _RETAINED_DOMAINS.pop(self.clock, [])
        _restore_retained_domains(retained)


def _prepare_selections(
    modelled: object,
    watchdogs: object,
) -> tuple[tuple[_ModelledSelection, ...], tuple[_WatchdogSelection, ...]]:
    """Validate and normalise the selected owner events of one attachment."""
    prepared_modelled = tuple(
        _ModelledSelection(str(attribute), str(label))
        for attribute, label in _selection_pairs(modelled, "Modelled wait", (2,))
    )
    prepared_watchdogs = tuple(
        _WatchdogSelection(
            str(attribute),
            _positive_interval(seconds, f"real watchdog {attribute}"),
            str(name) if name is not None else str(attribute),
        )
        for attribute, seconds, name in _selection_pairs(
            watchdogs, "Real watchdog", (2, 3)
        )
    )
    selected = [item.attribute for item in prepared_modelled] + [
        item.attribute for item in prepared_watchdogs
    ]
    if not selected:
        raise ValueError(
            "a wait attachment needs at least one selected owner event attribute "
            "(pass modelled=(attribute, label) or watchdogs=(attribute, seconds))"
        )
    duplicated = sorted({name for name in selected if selected.count(name) > 1})
    if duplicated:
        raise ValueError(
            f"one wait attribute cannot receive two adapters: {duplicated}; "
            f"choose either the modelled interval or the real watchdog per event, "
            f"because the timeout semantics of one wait have one owner"
        )
    return prepared_modelled, prepared_watchdogs


def _selection_pairs(
    spec: object, kind: str, allowed: tuple[int, ...]
) -> tuple[tuple[object, ...], ...]:
    """Return normalised selections for one attachment family.

    A bare tuple whose first element is an attribute name selects one owner
    event; a mapping or a sequence of such tuples selects several. Padding the
    optional watchdog name keeps every selection comparable, so a transposed or
    malformed selection cannot silently attach an adapter to the wrong attribute.
    """
    if isinstance(spec, tuple) and spec and isinstance(spec[0], str):
        candidates: tuple[object, ...] = (spec,)
    elif isinstance(spec, Mapping):
        candidates = tuple(spec.items())
    else:
        candidates = tuple(spec)  # type: ignore[arg-type]
    prepared = []
    for candidate in candidates:
        if not isinstance(candidate, tuple) or len(candidate) not in allowed:
            raise ValueError(
                f"{kind} selection must be a tuple of (attribute, detail...) with "
                f"{' or '.join(str(fields) for fields in allowed)} elements, got "
                f"{candidate!r}"
            )
        prepared.append(candidate + (None,) * (max(allowed) - len(candidate)))
    return tuple(prepared)


def _positive_interval(seconds: object, kind: str) -> float:
    """Return *seconds* as a strictly positive finite interval."""
    interval = _validated_interval(seconds, kind, label=kind)
    if interval == 0.0:
        raise ValueError(
            f"{kind} must give a positive real coordination ceiling, got {interval!r}"
        )
    return interval


def _class_real_wait(target: object, kind: str) -> Callable[..., bool]:
    """Return the real wait that *target*'s **class** provides, or refuse.

    An instance attribute named ``wait`` is exactly what ``model_event_wait``,
    ``real_watchdog`` and ``owner_waits`` install, and an installed adapter need
    not be bound to the object it replaced, so no identity test on an instance
    attribute can tell a real wait from a scenario adapter.  A target whose class
    exposes no ``wait`` therefore has no real wait at all: refusing it is the
    only answer that cannot let a fabricated success or a charged modelled
    interval stand in for real coordination.
    """
    real_wait = getattr(type(target), "wait", None)
    if not callable(real_wait):
        raise UnsupportedWatchdogTarget(
            f"{kind} cannot supervise {type(target).__name__}: its class exposes "
            f"no real wait operation, so there is nothing real to wait on; an "
            f"instance attribute named 'wait' is a scenario adapter, never a "
            f"real wait, so supervise an object whose class provides "
            f"wait(timeout) - as threading.Event does - or leave this "
            f"coordination unpatched"
        )
    return real_wait  # type: ignore[return-value]


def _prepare_attachment(
    clock: ManualClock,
    modelled: object,
    watchdogs: object,
    settle: Callable[[], None] | None,
) -> _WaitAttachments:
    """Validate one attachment specification without installing anything."""
    prepared_modelled, prepared_watchdogs = _prepare_selections(modelled, watchdogs)
    if settle is not None and not callable(settle):
        raise ValueError(
            f"a wait attachment settle step must be callable, got {settle!r}"
        )
    return _WaitAttachments(clock, prepared_modelled, prepared_watchdogs, settle)


class RealWatchdog:
    """One named real coordination ceiling for a wait that is not the subject.

    The requested timeout of the code being supervised is replaced by this
    scenario ceiling, so supervision can never block for an unbounded or
    policy-sized interval.  The result is still the real observation: an expired
    watchdog reports the real unfinished state, and nothing here advances
    logical time or manufactures completion.
    """

    seconds: float
    name: str

    def __init__(self, target: _Waitable, seconds: float, name: str) -> None:
        self._target = target
        self._class_wait = _class_real_wait(target, f"real watchdog {name}")
        self.seconds = seconds
        self.name = name
        self._requested: list[float | None] = []

    @property
    def calls(self) -> tuple[float | None, ...]:
        """The timeouts the supervised code asked for, in order."""
        return tuple(self._requested)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait on the real condition under the scenario's own ceiling.

        Only the class-level real wait resolved at construction is called, so a
        watchdog can never re-enter itself, never answer from an installed
        instance override and never charge time.  A target with no class-level
        wait is refused when this watchdog is built, so there is no fallback
        here that could reach an instance-level adapter.
        """
        self._requested.append(timeout)
        return bool(self._class_wait(self._target, self.seconds))


def _add_note(error: BaseException, note: str) -> None:
    """Attach a secondary diagnostic without replacing the authoritative error."""
    notes = getattr(error, "__notes__", None)
    if notes is None:
        notes = []
        try:
            error.__notes__ = notes
        except AttributeError, TypeError:
            return
    notes.append(note)


def await_event(event: object, timeout: float, *, name: str) -> bool:
    """Wait for *actual* completion through the real path, charging no time.

    This is the separate real-wait path a modelled timeout must never become:
    it advances no logical time, ignores any scenario-installed wait override
    on the same event, and raises :class:`WatchdogTimeout` on expiry instead of
    reporting a fabricated completion.  It must not be used for a wait whose
    timeout semantics are the subject of the test.

    The real wait is the one the object's *class* provides, called with the
    object as its receiver.  An instance attribute named ``wait`` is never
    consulted: that is exactly what :func:`model_event_wait`, :func:`real_watchdog`
    and :func:`owner_waits` install, and an installed adapter need not be bound
    to the event it replaced (a closure, a ``functools.partial`` or a helper
    bound to its own adapter object all replace it), so identifying an override
    by what it is bound to would let the modelled or foreign wait answer a real
    coordination check.
    """
    ceiling = _positive_interval(timeout, f"real watchdog {name}")
    real_wait = _class_real_wait(event, f"real watchdog {name!r}")
    completed = real_wait(event, ceiling)
    if completed:
        return True
    raise WatchdogTimeout(
        f"real watchdog {name!r} expired after {ceiling!r} real seconds without "
        f"the awaited completion; a watchdog bounds coordination, advances no "
        f"logical time and never manufactures completion, so the scenario must "
        f"settle the real condition it named"
    )


def await_condition(
    predicate: Callable[[], bool],
    timeout: float,
    *,
    name: str,
    poll_interval: float = 0.005,
) -> None:
    """Wait for a real readiness condition, naming it when the ceiling expires.

    This is what replaces an incidental ``time.sleep`` standing between a
    publication and the step that depends on it: the scenario states which
    condition must hold, the wait is bounded, no logical clock moves, and an
    expired ceiling raises :class:`WatchdogTimeout` for that named condition
    instead of letting the next assertion fail with an empty observation.  It is
    real synchronization, so it must not be used where the bounded return itself
    is the behaviour under test.
    """
    ceiling = _positive_interval(timeout, f"real readiness wait {name}")
    pause = _positive_interval(poll_interval, f"readiness poll interval of {name}")
    deadline = time.monotonic() + ceiling
    while True:
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise WatchdogTimeout(
                f"real readiness wait {name!r} expired after {ceiling!r} real "
                f"seconds with the condition still false; the barrier or worker "
                f"that publishes it did not settle"
            )
        time.sleep(min(pause, remaining))


@contextlib.contextmanager
def real_watchdog(
    event: object,
    timeout: float,
    *,
    attribute: str = "wait",
    name: str = "watchdog",
) -> Iterator[RealWatchdog]:
    """Give one selected event a real ceiling while logical time stays frozen.

    The installed wait keeps real semantics and reports the real state; use
    :func:`await_event` when the test itself must fail on expiry.
    """
    ceiling = _positive_interval(timeout, f"real watchdog {name}")
    watchdog = RealWatchdog(event, ceiling, name)  # type: ignore[arg-type]
    replacement = _replace_attribute(event, attribute, watchdog.wait)
    try:
        yield watchdog
    finally:
        replacement.undo()


def _exit_attachment(
    attachment: _WaitAttachments,
    hook: _Replacement | None,
    reporting: BaseException | None,
    *,
    settle_first: bool = True,
) -> None:
    """Settle the scenario's workers, then undo every installation once.

    A settlement step that fails means a participant may still be running, so
    nothing is restored: the event adapters, the startup hook and the module
    timelines of this attachment's clock stay installed, and the scenario must
    settle its workers and call ``attachment.settle_and_restore()``.  There is
    deliberately no "restore anyway" path: a wait or a clock handed back under a
    live worker mixes two timelines.  The scenario's own error stays
    authoritative, with the settlement failure and the recovery path attached to
    it as notes.
    """
    if settle_first:
        try:
            attachment.run_settlement()
        except BaseException as error:
            attachment.retain_for_recovery(error, hook=hook)
            recovery = (
                "release the scenario barriers in a finally block, wait for the "
                "workers, then call attachment.settle_and_restore(); it restores "
                "the waits, the startup hook and the retained module timelines"
            )
            if reporting is not None:
                _add_note(
                    reporting,
                    f"scenario settlement also failed after the scenario error: "
                    f"{error!r}; the scenario waits, startup hook and module "
                    f"timelines of this attachment stay installed because its "
                    f"participants may still be running; {recovery}",
                )
                return
            raise SettlementIncomplete(
                f"the settlement step of this attachment failed: {error!r}; the "
                f"scenario waits, startup hook and module timelines stay "
                f"installed because its participants may still be running; {recovery}"
            ) from error
    if hook is not None:
        hook.undo()
    attachment.discharge()


@contextlib.contextmanager
def settlement_guard(
    clock: ManualClock,
    *,
    settle: Callable[[], None] | None = None,
) -> Iterator[_WaitAttachments]:
    """Retain a clock domain until settlement, without replacing any waits.

    Recovery uses ``settle_and_restore`` just like a wait attachment.
    """
    if settle is not None and not callable(settle):
        raise ValueError("a settlement guard settle step must be callable")
    attachment = _WaitAttachments(clock, (), (), settle)
    try:
        yield attachment
    except BaseException as propagating:
        _exit_attachment(attachment, None, propagating)
        raise
    else:
        _exit_attachment(attachment, None, None)


@contextlib.contextmanager
def owner_waits(
    owner: object,
    clock: ManualClock,
    *,
    modelled: object = (),
    watchdogs: object = (),
    settle: Callable[[], None] | None = None,
) -> Iterator[_WaitAttachments]:
    """Attach scenario waits to one owner's selected events for a whole scenario.

    The clock domain and the wait adapters must outlive the owner's workers:
    *settle* runs before anything is restored, so an owner that still reads
    logical timestamps never sees real time again mid-scenario.  On a normal
    exit, an assertion failure or an interruption the restoration happens once.
    If *settle* fails the participants may still be running, so nothing is
    restored: the attachment keeps its waits, and an enclosing ``module_clock``
    keeps its logical time reference, until the scenario settles the workers and
    calls ``attachment.settle_and_restore()``.
    """
    attachment = _prepare_attachment(clock, modelled, watchdogs, settle)
    try:
        attachment.install(owner)
    except BaseException:
        _exit_attachment(attachment, None, None, settle_first=False)
        raise
    try:
        yield attachment
    except BaseException as propagating:
        _exit_attachment(attachment, None, propagating)
        raise
    else:
        _exit_attachment(attachment, None, None)


@contextlib.contextmanager
def attached_waits(
    target: type,
    clock: ManualClock,
    *,
    startup: str = "_start_supervisor",
    modelled: object = (),
    watchdogs: object = (),
    settle: Callable[[], None] | None = None,
) -> Iterator[_WaitAttachments]:
    """Install scenario waits on every owner through one selected startup hook.

    Owners created while the context is active get their waits installed
    before the original hook publishes a worker, so no thread can use a wait
    that is not yet the scenario's.  Owners created outside the attachment are
    untouched, and both the hook and every instance attribute are restored
    after the participating workers settle.  A failing settlement step keeps the
    hook and every installed wait armed until ``settle_and_restore()`` proves the
    workers settled.
    """
    hook = getattr(target, startup, None)
    if not callable(hook):
        raise AttributeError(
            f"{getattr(target, '__name__', repr(target))} has no callable startup "
            f"hook {startup!r} to attach the scenario waits to; name the hook "
            f"that publishes the worker's waits"
        )
    attachment = _prepare_attachment(clock, modelled, watchdogs, settle)

    def start_attached(owner: object, *args: object, **kwargs: object) -> object:
        attachment.install(owner)
        return hook(owner, *args, **kwargs)

    try:
        replacement = _replace_attribute(target, startup, start_attached)
    except BaseException:
        attachment.discharge()
        raise
    try:
        yield attachment
    except BaseException as propagating:
        _exit_attachment(attachment, replacement, propagating)
        raise
    else:
        _exit_attachment(attachment, replacement, None)
