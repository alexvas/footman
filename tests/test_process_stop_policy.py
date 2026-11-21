"""Domain-neutral stop-policy contracts (upstream Jigbox 6BB.14–6BB.17)."""

from __future__ import annotations

import importlib
import sys
import threading
import time
from contextlib import ExitStack
from unittest import mock

import pytest

from tests.clock_support import ManualClock, module_clock, owner_waits, stage_pause
from tests.test_process_lifecycle import Child, secondary, workers

pytestmark = pytest.mark.process_time


@pytest.fixture
def api():
    return importlib.import_module("footman.process")


class Action:
    """Nonblocking action with distinct request and owned-resource settlement."""

    def __init__(self, api, *, error=None, pending=False, prepare_error=None):
        self.api = api
        self.error = error
        self.pending = pending
        self.prepare_error = prepare_error
        self.calls = []
        self.contexts = []
        self.started = threading.Event()
        self.release = threading.Event()

    def prepare(self):
        self.calls.append("prepare")
        if self.prepare_error:
            raise self.prepare_error

    def start(self, context):
        self.calls.append("start")
        self.contexts.append(context)
        self.started.set()

    def observe(self):
        settled = not self.pending or self.release.is_set()
        failures = ()
        if self.error is not None:
            # This fixture synthesizes a failure at its own action boundary.
            kind = (
                self.api.FailureKind.INTERRUPTION
                if not isinstance(self.error, Exception)
                else self.api.FailureKind.ORDINARY
                if isinstance(self.error, OSError)
                else self.api.FailureKind.UNEXPECTED
            )
            failures = (
                self.api.FailureInfo(self.error, kind, self.api.FailurePhase.ACTION),
            )
        return self.api.ActionResult(settled, settled, failures)

    def cancel(self, deadline):
        self.calls.append("cancel")


def plan(
    api,
    action=None,
    *,
    cap=0.015,
    reserve=0.025,
    drain=False,
    wait=False,
    normal=False,
    local=False,
):
    return api.StopPolicy(
        (api.StopStage(cap, action, wait, normal, local),), reserve, drain
    )


def launch(api, policy, child=None, **kwargs):
    child = child or Child()
    return api.OwnedProcess.launch(
        ["child"],
        grace_seconds=0.08,
        stop_policy=policy,
        factory=lambda *a, **k: child,
        **kwargs,
    )


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["cap", "reserve"])
def test_invalid_budgets_precede_factory(api, bad, field):
    factory = mock.Mock()
    options = {field: bad}
    with pytest.raises(ValueError):
        api.OwnedProcess.launch(
            ["child"],
            grace_seconds=0.08,
            stop_policy=plan(api, **options),
            factory=factory,
        )
    factory.assert_not_called()
    assert not workers()


@pytest.mark.parametrize("policy", [object(), "policy", 1])
def test_invalid_policy_never_acquires(api, policy):
    factory = mock.Mock()
    with pytest.raises(TypeError):
        api.OwnedProcess.launch(["child"], stop_policy=policy, factory=factory)
    factory.assert_not_called()


def test_over_total_and_invalid_action_precede_preparation(api):
    action = Action(api)
    for policy in (plan(api, action, cap=0.07, reserve=0.02), plan(api, object())):
        factory = mock.Mock()
        with pytest.raises((ValueError, TypeError)):
            api.OwnedProcess.launch(
                ["child"], grace_seconds=0.08, stop_policy=policy, factory=factory
            )
        factory.assert_not_called()
    assert action.calls == []


@pytest.mark.parametrize("position", [0, 1])
def test_prepare_failure_cleans_each_prepared_slot_and_idle_worker(api, position):
    failure = OSError("readiness")
    actions = [Action(api), Action(api)]
    actions[position].prepare_error = failure
    policy = api.StopPolicy(tuple(api.StopStage(0.01, a) for a in actions), 0.02)
    factory = mock.Mock()
    with pytest.raises(OSError) as caught:
        api.OwnedProcess.launch(
            ["child"], grace_seconds=0.08, stop_policy=policy, factory=factory
        )
    assert caught.value is failure
    factory.assert_not_called()
    assert all(a.calls.count("cancel") == 1 for a in actions[: position + 1])
    assert all("start" not in a.calls for a in actions)
    assert not workers()


def test_factory_failure_cleans_policy_preparation(api):
    action = Action(api)
    error = OSError("factory")
    owner = api.OwnedProcess.launch(
        ["child"],
        grace_seconds=0.08,
        stop_policy=plan(api, action),
        factory=mock.Mock(side_effect=error),
    )
    result = owner.capture()
    assert result.lifecycle.complete and result.lifecycle.failures[0].error is error
    assert action.calls == ["prepare", "cancel"]
    assert not workers()


@pytest.mark.parametrize(
    "trigger", ["explicit", "deadline", "context", "setup", "reader"]
)
def test_all_triggers_use_prepared_policy_without_main_authority(
    api, trigger, process_clock
):
    action = Action(api)
    child = Child()
    options = {}
    error = OSError("trigger")
    if trigger == "deadline":
        options["deadline_seconds"] = 0.01
    if trigger == "setup":
        options["on_launched"] = mock.Mock(side_effect=error)
    patches = (
        mock.patch.object(api.OwnedProcess, "_read_chunk", side_effect=error)
        if trigger == "reader"
        else mock.patch.object(time, "perf_counter", wraps=time.perf_counter)
    )
    with patches:
        if trigger == "setup":
            owner = launch(api, plan(api, action), child, **options)
            assert owner.capture().lifecycle.failures[0].error is error
            assert owner.wait_cleanup(1).complete
        else:
            owner = launch(api, plan(api, action), child, **options)
            if trigger == "deadline":
                process_clock.advance(0.01, label="deadline-trigger")
            if trigger in ("deadline", "reader"):
                assert action.started.wait(1)
            if trigger == "context":
                owner.__exit__(None, None, None)
            else:
                owner.shutdown()
            assert owner.wait_cleanup(1).complete
    assert (
        action.calls.count("prepare")
        == action.calls.count("start")
        == action.calls.count("cancel")
        == 1
    )
    context = action.contexts[0]
    assert context.reason == trigger or (
        trigger == "explicit" and context.reason == "cancel"
    )
    assert set(context.__dataclass_fields__) == {"reason", "returncode", "deadline"}
    assert not hasattr(context, "process") and not hasattr(context, "stdout")
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert not workers()


def test_natural_completion_skips_stop_but_runs_required_finalization(api):
    actions = [Action(api), Action(api)]
    policy = api.StopPolicy(
        (
            api.StopStage(0.01, actions[0]),
            api.StopStage(0.01, actions[1], run_on_normal=True),
        ),
        0.02,
    )
    child = Child()
    child.returncode = 0
    result = launch(api, policy, child).capture()
    assert result.lifecycle.complete
    assert "start" not in actions[0].calls
    assert actions[1].contexts[0].reason == "natural"
    assert all(a.calls.count("cancel") == 1 for a in actions)


def _report_cleanup(defects):
    if defects:
        for error in defects[1:]:
            defects[0].add_note(f"independent cleanup also failed: {error!r}")
        raise defects[0]


class StageClockScenario:
    """Stage-budget scenario whose launch happens inside the installed timeline.

    Cleanup attempts shutdown and settlement independently. Failed settlement
    retains the timeline and pause until explicit attachment recovery; an active
    scenario exception keeps authority, with cleanup errors reported as notes.
    """

    def __init__(self, api):
        self.api = api
        self.clock = ManualClock(origin=100.0)
        self.actions = [Action(api), Action(api), Action(api)]
        self.child = Child()
        self.policy = api.StopPolicy(
            tuple(api.StopStage(0.01, a, wait_child=True) for a in self.actions), 0.02
        )
        for action in self.actions:
            original = action.start

            def start(context, original=original):
                assert context.deadline == pytest.approx(self.clock.monotonic() + 0.01)
                original(context)
                self.clock.advance(0.006, label="action-start")

            action.start = start
        self.settled, self.alive, self.logical = None, ("unsettled",), None

    def settle(self):
        defects = []
        try:
            self.owner.shutdown()
        except BaseException as error:
            defects.append(error)
        try:
            self.settled = self.owner.wait_cleanup(1)
        except BaseException as error:
            defects.append(error)
        self.alive = tuple(workers())
        self.logical = self.api.time.monotonic()
        if self.settled is None or not self.settled.complete or self.alive:
            defects.append(AssertionError("stage scenario settlement incomplete"))
        _report_cleanup(defects)
        self.patches.close()

    def run(self, fail=None):
        with module_clock(self.api, self.clock):
            self.patches = ExitStack()
            self.patches.enter_context(
                stage_pause(self.api, self.clock, label="stage-pause")
            )
            try:
                self.owner = launch(self.api, self.policy, self.child)
            except BaseException:
                self.patches.close()
                raise
            with owner_waits(
                self.owner,
                self.clock,
                watchdogs=("_phase_done", 1),
                settle=self.settle,
            ) as attachment:
                self.attachment = attachment
                if fail is not None:
                    fail()
                self.owner.shutdown()
                assert self.owner.wait_cleanup(1).complete


@pytest.mark.own_process_time
def test_stage_clock_starts_before_initiation_and_wait_shares_cap(api):
    scenario = StageClockScenario(api)
    scenario.run()
    assert [a.contexts[0].deadline for a in scenario.actions] == pytest.approx(
        [100.01, 100.02, 100.03]
    )
    assert scenario.child.returncode is not None
    assert scenario.settled.complete
    # The owner and its workers settled while the logical timeline was still
    # installed; the module's real reference returns only after they finish.
    assert scenario.alive == () and 100.0 <= scenario.logical < 101.0
    assert api.time is time and not workers()


@pytest.mark.own_process_time
def test_stage_clock_fault_after_launch_settles_on_logical_time(api):
    injected = RuntimeError("injected after owner launch")
    scenario = StageClockScenario(api)

    def fail():
        raise injected

    with pytest.raises(RuntimeError) as caught:
        scenario.run(fail=fail)
    # The injected failure stays authoritative: the cleanup path raised no
    # secondary error, and it settled the owner and its workers on the logical
    # timeline instead of leaving them to a restored real one.
    assert caught.value is injected
    assert scenario.settled.complete
    assert scenario.alive == () and 100.0 <= scenario.logical < 101.0
    assert api.time is time and not workers()
    assert [a.contexts[0].deadline for a in scenario.actions] == pytest.approx(
        [100.01, 100.02, 100.03]
    )
    assert scenario.child.returncode is not None


@pytest.mark.real_process_time
def test_failure_advances_immediately_and_local_order_is_coordinator_owned(api):
    child = Child()
    first = Action(api, error=OSError("request"))
    second = Action(api)

    def start(context):
        assert child.actions == ["terminate"]
        second.contexts.append(context)
        second.calls.append("start")

    second.start = start
    policy = api.StopPolicy(
        (
            api.StopStage(0.03, first, wait_child=True),
            api.StopStage(0.01, second, local_stop_before=True),
        ),
        0.02,
    )
    begin = time.monotonic()
    owner = launch(api, policy, child)
    result = owner.shutdown()
    assert time.monotonic() - begin < 0.08
    assert result.failures[0].error is first.error and result.complete
    assert second.calls.count("start") == 1


@pytest.mark.parametrize("primary_type", [None, ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("action_type", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.own_process_time
def test_action_fault_precedence_independent_cleanup_and_repeat(
    api, primary_type, action_type, position
):
    primary = primary_type("operation") if primary_type else None
    error = action_type("action")
    actions = [Action(api), Action(api)]
    actions[position].error = error
    child = Child()
    policy = api.StopPolicy(tuple(api.StopStage(0.01, a) for a in actions), 0.025)
    clock = ManualClock(origin=100.0)
    # This is a precedence/once-only proof, not a wall-clock scheduling test.
    # Pin its budget so loaded workers cannot skip an otherwise eligible stage.
    with module_clock(api, clock):
        owner = launch(api, policy, child)
        result = owner.shutdown(primary=primary)
        assert owner.wait_cleanup(1).complete
    expected = (
        primary
        if primary and (primary_type is KeyboardInterrupt or action_type is OSError)
        else error
    )
    assert result.failures[0].error is expected and result.complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert all(a.calls.count("start") == a.calls.count("cancel") == 1 for a in actions)
    assert owner.shutdown() == result
    assert len(result.failures) == len({id(f.error) for f in result.failures})


@pytest.mark.sleepy
def test_pending_action_retains_owner_and_late_error_is_observation_only(api):
    action = Action(api, pending=True)
    owner = launch(api, plan(api, action))
    try:
        result = owner.shutdown()
        assert not result.complete
        assert not owner.wait_cleanup(0.01).complete
        action.error = RuntimeError("late")
        action.release.set()
        observed = owner.wait_cleanup(1)
        assert observed.complete and observed.failures[0].error is action.error
        assert not result.failures and not result.complete
        assert action.calls == ["prepare", "start", "cancel"]
    finally:
        action.release.set()
        owner.wait_cleanup(1)
    assert not workers()


class PublicationGap:
    """The publication-gap scenario shared by the merge test and its fault path.

    The first delivery pauses at publication; the merger must enter the owner's
    phase wait before that pause resumes.  The scenario joins only threads it
    actually started, releases the publication barrier on every exit path, and
    records its own settlement, so a scenario failure is never replaced by a
    cleanup error and a join defect can never skip ``owner.wait_cleanup``.
    Incomplete settlement retains the clock and phase-wait patch until explicit
    attachment recovery. Cleanup errors remain visible without replacing an
    active scenario exception.
    """

    def __init__(self, api):
        self.api = api
        self.clock = ManualClock()
        self.action = Action(api)
        self.published, self.resume = threading.Event(), threading.Event()
        self.first, self.later = KeyboardInterrupt("first"), KeyboardInterrupt("later")
        self.results, self.join_defects, self.resumptions = [], [], []
        self.started, self.settled, self.alive, self.processes = [], None, (), ()

    def prepare(self):
        self.owner = launch(self.api, plan(self.api, self.action))
        real_publish = self.owner._publish

        def publish(complete):
            real_publish(complete)
            self.published.set()
            resumed = self.resume.wait(1)
            # A publication that timed out here strands the continuation, so the
            # scenario records it even when the assertion below is not reached.
            self.resumptions.append(resumed)
            assert resumed

        self.owner._publish = publish
        self.caller = threading.Thread(
            target=lambda: self.results.append(self.owner.shutdown(primary=self.first))
        )
        self.merger = threading.Thread(
            target=lambda: self.results.append(self.owner.shutdown(primary=self.later))
        )
        self.merger_waiting = threading.Event()
        phase_wait = self.owner._phase_done.wait

        def wait(timeout=None):
            if threading.current_thread() is self.merger:
                self.merger_waiting.set()
            return phase_wait(timeout)

        self.patch = mock.patch.object(self.owner._phase_done, "wait", wait)

    def settle(self):
        self.resume.set()
        defects = []
        for thread in self.started:
            try:
                thread.join(1)
            except BaseException as error:
                self.join_defects.append(f"{thread.name}: {error!r}")
                defects.append(error)
        try:
            self.settled = self.owner.wait_cleanup(1)
        except BaseException as error:
            defects.append(error)
        self.alive = tuple(t.name for t in self.started if t.is_alive())
        self.processes = tuple(w.name for w in workers())
        if (
            self.settled is None
            or not self.settled.complete
            or self.alive
            or self.processes
        ):
            defects.append(AssertionError("publication scenario settlement incomplete"))
        _report_cleanup(defects)
        self.patches.close()

    def run(self, fail=None):
        """Run to the resume barrier; ``fail`` runs before the merger starts."""
        with module_clock(self.api, self.clock):
            self.prepare()
            self.patches = ExitStack()
            with owner_waits(
                self,
                self.clock,
                watchdogs=("published", 1),
                settle=self.settle,
            ) as attachment:
                self.attachment = attachment
                self.patches.enter_context(self.patch)
                self.exercise(fail)

    def exercise(self, fail):
        try:
            self.caller.start()
            self.started.append(self.caller)
            assert self.published.wait(1)
            if fail is not None:
                fail()
            self.merger.start()
            self.started.append(self.merger)
            assert self.merger_waiting.wait(1)
            self.resume.set()
            self.caller.join(1)
            self.merger.join(1)
        finally:
            self.resume.set()


@pytest.mark.own_process_time
def test_primary_merge_in_policy_publication_gap(api):
    scenario = PublicationGap(api)
    scenario.run()
    # Recorded inside the patched scope: no started thread and no unfinished
    # owner survives the phase-wait patch, and no join defect replaced the
    # scenario's own outcome.
    assert scenario.join_defects == [] and scenario.alive == ()
    assert scenario.processes == () and not workers()
    assert scenario.resumptions == [True] and scenario.settled.complete
    first, later, results = scenario.first, scenario.later, scenario.results
    assert len(results) == 2
    assert all(r.failures[0].error is first for r in results)
    assert all(any(f.error is later for f in r.failures) for r in results)
    assert secondary(first) == []
    assert scenario.action.calls.count("start") == 1


@pytest.mark.own_process_time
def test_publication_gap_fault_before_merger_start_releases_started_threads(api):
    injected = RuntimeError("injected before the merger started")
    scenario = PublicationGap(api)

    def fail():
        raise injected

    with pytest.raises(RuntimeError) as caught:
        scenario.run(fail=fail)
    assert caught.value is injected
    # The merger never started, so it was never joined; the started caller and
    # the owner still settled, and the cleanup path added no failure of its own.
    assert scenario.join_defects == [] and scenario.alive == ()
    assert scenario.started == [scenario.caller]
    # The publication barrier was released even though the scenario failed.
    assert scenario.resumptions == [True]
    assert scenario.settled.complete
    assert scenario.processes == () and not workers()
    assert [r.failures[0].error for r in scenario.results] == [scenario.first]
    assert scenario.action.calls.count("start") == 1


@pytest.mark.own_process_time
@pytest.mark.parametrize("scenario_type", [StageClockScenario, PublicationGap])
@pytest.mark.parametrize("primary_type", [None, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("defect_kind", ["raise", "incomplete", "survivor"])
def test_scenario_retains_patches_until_explicit_recovery(
    api, scenario_type, primary_type, defect_kind
):
    from types import SimpleNamespace

    from tests.clock_support import SettlementIncomplete

    scenario = scenario_type(api)
    primary = primary_type("scenario failure") if primary_type else None
    cleanup = OSError("settlement failure")
    original_settle = scenario.settle
    recovery = False
    calls = []
    release = threading.Event()
    observed = []

    def pending_worker():
        release.wait(5)
        observed.append(api.time.monotonic())

    survivor = threading.Thread(target=pending_worker)

    def settle():
        # Inject only at the cleanup boundary, not at scenario observations.
        if recovery:
            release.set()
            if survivor.ident is not None:
                survivor.join(1)
                assert not survivor.is_alive()
            return original_settle()
        calls.append("settle")
        real_wait = scenario.owner.wait_cleanup

        def wait(timeout):
            calls.append("wait")
            if defect_kind == "raise":
                raise cleanup
            result = real_wait(timeout)
            if defect_kind == "incomplete":
                return SimpleNamespace(complete=False)
            return result

        with mock.patch.object(scenario.owner, "wait_cleanup", wait):
            if defect_kind == "survivor":
                survivor.start()
                with mock.patch(__name__ + ".workers", return_value=[survivor]):
                    return original_settle()
            return original_settle()

    scenario.settle = settle
    original_time, original_pause = api.time, api._policy_pause

    def fail():
        if primary is not None:
            raise primary

    try:
        with pytest.raises(primary_type or SettlementIncomplete) as caught:
            scenario.run(fail)
        if primary is not None:
            assert caught.value is primary
            assert any("settlement" in note for note in primary.__notes__)
        assert calls == ["settle", "wait"]
        assert not scenario.attachment.settled
        assert api.time is not original_time
        if scenario_type is StageClockScenario:
            assert api._policy_pause is not original_pause
        else:
            assert "wait" in vars(scenario.owner._phase_done)
        recovery = True
        scenario.attachment.settle_and_restore()
        assert scenario.attachment.settled and not workers()
        if defect_kind == "survivor":
            assert observed and observed[0] == scenario.clock.monotonic()
            assert not survivor.is_alive()
        assert api.time is original_time and api._policy_pause is original_pause
        assert "wait" not in vars(scenario.owner._phase_done)
        # A second recovery cannot overwrite patches installed after recovery.
        sentinel = object()
        with mock.patch.object(api, "time", sentinel):
            scenario.attachment.settle_and_restore()
            assert api.time is sentinel
    finally:
        recovery = True
        scenario.attachment.settle_and_restore()


@pytest.mark.own_process_time
@pytest.mark.parametrize("primary_type", [None, RuntimeError, KeyboardInterrupt])
def test_stage_shutdown_failure_still_attempts_settlement(api, primary_type):
    from tests.clock_support import SettlementIncomplete

    scenario = StageClockScenario(api)
    primary = primary_type("scenario") if primary_type else None
    cleanup = OSError("shutdown")
    original_settle = scenario.settle
    waits = []
    recovery = False

    def settle():
        if recovery:
            return original_settle()
        real_wait = scenario.owner.wait_cleanup

        def wait(timeout):
            waits.append(timeout)
            return real_wait(timeout)

        with (
            mock.patch.object(scenario.owner, "shutdown", side_effect=cleanup),
            mock.patch.object(scenario.owner, "wait_cleanup", wait),
        ):
            return original_settle()

    scenario.settle = settle

    def fail():
        if primary is not None:
            raise primary

    try:
        with pytest.raises(primary_type or SettlementIncomplete) as caught:
            scenario.run(fail)
        if primary is not None:
            assert caught.value is primary
            assert any("shutdown" in note for note in primary.__notes__)
        assert waits == [1]
    finally:
        recovery = True
        scenario.attachment.settle_and_restore()
    assert not workers() and api.time is time


@pytest.mark.own_process_time
@pytest.mark.parametrize("primary_type", [None, RuntimeError, KeyboardInterrupt])
def test_publication_join_failure_does_not_skip_other_cleanup(api, primary_type):
    from tests.clock_support import SettlementIncomplete

    scenario = PublicationGap(api)
    original_settle = scenario.settle
    primary = primary_type("scenario") if primary_type else None
    recovery = False
    attempted = []

    def settle():
        if recovery:
            return original_settle()
        real_wait = scenario.owner.wait_cleanup

        def wait(timeout):
            attempted.append("owner")
            return real_wait(timeout)

        # Include an independently joinable participant even on the early fault path.
        sibling = mock.Mock()
        sibling.name = "independent"
        sibling.is_alive.return_value = False
        scenario.started.append(sibling)
        with (
            mock.patch.object(scenario.caller, "join", side_effect=OSError("join")),
            mock.patch.object(scenario.owner, "wait_cleanup", wait),
        ):
            try:
                original_settle()
            finally:
                assert sibling.join.call_args == mock.call(1)
                scenario.started.remove(sibling)

    scenario.settle = settle

    def fail():
        if primary is not None:
            raise primary

    try:
        with pytest.raises(primary_type or SettlementIncomplete) as caught:
            scenario.run(fail)
        if primary is not None:
            assert caught.value is primary
        assert attempted == ["owner"] and scenario.resume.is_set()
        assert not scenario.attachment.settled
    finally:
        recovery = True
        scenario.attachment.settle_and_restore()
    assert not workers() and api.time is time


class Helper(Action):
    """A separately owned, default-policy auxiliary process."""

    def __init__(self, api, code):
        super().__init__(api)
        self.code = code
        self.owner = None

    def start(self, context):
        super().start(context)
        remaining = context.deadline - time.monotonic()
        self.owner = self.api.OwnedProcess.launch(
            [sys.executable, "-c", self.code],
            grace_seconds=max(0.001, remaining / 3),
            deadline_seconds=max(0.001, remaining / 3),
        )

    def observe(self):
        if self.owner is None:
            return self.api.ActionResult(True, True)
        result = self.owner.cleanup_status
        failures = tuple(
            self.api.FailureInfo(f.error, f.kind, self.api.FailurePhase.ACTION)
            for f in result.failures
        )
        return self.api.ActionResult(result.complete, result.complete, failures)

    def cancel(self, deadline):
        super().cancel(deadline)
        # Nonblocking: the helper's own configured deadline drives cleanup.


@pytest.mark.sleepy
@pytest.mark.parametrize("code", ["import time; time.sleep(10)", "raise SystemExit(7)"])
@pytest.mark.real_process_time
def test_real_pipe_helper_drain_and_fallback(api, code):
    helper = Helper(api, code)
    policy = plan(api, helper, cap=0.15, reserve=0.15, drain=True, wait=True)
    owner = api.OwnedProcess.launch(
        [
            sys.executable,
            "-c",
            "import sys,time; print('ready',flush=True); time.sleep(.04); "
            "sys.stdout.write('x'*100000);sys.stdout.flush();sys.stderr.write('y'*100000);sys.stderr.flush();time.sleep(10)",
        ],
        stop_policy=policy,
        grace_seconds=0.4,
        capture_limit=128,
    )
    try:
        result = owner.shutdown()
        assert owner.wait_cleanup(1).complete
        assert bytes(owner._tails[0]) == b"x" * 128
        assert bytes(owner._tails[1]) == b"y" * 128
        assert result.complete
        assert helper.owner.wait_cleanup(1).complete
        assert not workers()
    finally:
        owner.shutdown()
        owner.wait_cleanup(1)
        if helper.owner:
            helper.owner.shutdown()
            helper.owner.wait_cleanup(1)


@pytest.mark.sleepy
@pytest.mark.real_process_time
def test_drain_without_consumer_does_not_stall_full_spool(api):
    action = Action(api)
    owner = api.OwnedProcess.launch(
        [
            sys.executable,
            "-c",
            "import os,time;os.write(1,b'a'*3000000);os.write(2,b'b'*3000000);time.sleep(10)",
        ],
        stop_policy=plan(api, action, cap=0.25, reserve=0.1, drain=True, wait=True),
        grace_seconds=0.4,
        capture_limit=64,
    )
    result = owner.shutdown()
    assert result.complete
    assert bytes(owner._tails[0]) == b"a" * 64 and bytes(owner._tails[1]) == b"b" * 64
    assert owner._output.qsize() <= 32
    assert not workers()


@pytest.mark.sleepy
@pytest.mark.parametrize("fault", ["startup", "reader"])
@pytest.mark.real_process_time
def test_real_pipe_faults_keep_reader_barrier_and_independent_action(api, fault):
    action = Action(api)
    error = OSError("reader setup")
    seam = "_start_reader" if fault == "startup" else "_read_chunk"
    original = getattr(api.OwnedProcess, seam)

    def fail(self, index):
        if index == 1:
            raise error
        return original(self, index)

    with mock.patch.object(api.OwnedProcess, seam, fail):
        if fault == "startup":
            owner = api.OwnedProcess.launch(
                [sys.executable, "-c", "import time;time.sleep(10)"],
                stop_policy=plan(api, action),
                grace_seconds=0.08,
            )
            assert owner.capture().lifecycle.failures[0].error is error
            assert owner.wait_cleanup(1).complete
        else:
            owner = api.OwnedProcess.launch(
                [sys.executable, "-c", "import time;time.sleep(10)"],
                stop_policy=plan(api, action),
                grace_seconds=0.08,
            )
            assert action.started.wait(1)
            assert owner.wait_cleanup(1).complete
            assert all(owner._safe(i) for i in range(2))
    assert action.calls.count("start") == 1
    assert not workers()
