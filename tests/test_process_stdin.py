"""Independent generic-child contracts for the opt-in owned stdin foundation."""

from __future__ import annotations

import inspect
import os
import sys
import threading
from dataclasses import FrozenInstanceError, fields
from unittest import mock

import pytest

import footman.process as process
from tests.test_process_lifecycle import Child, workers

EXISTING_EXPORTS = {
    "OwnedProcess",
    "ProcessResult",
    "ProcessStream",
    "StreamOutcome",
    "LifecycleResult",
    "CleanupIncomplete",
    "FailureInfo",
    "FailureKind",
    "FailurePhase",
    "StopPolicy",
    "StopStage",
    "StopContext",
    "ManagedAction",
    "ActionResult",
}


def api(name):
    assert hasattr(process, name), f"missing owned stdin API: {name}"
    return getattr(process, name)


@pytest.mark.parametrize(
    "name,base",
    [
        ("StdinWriteError", Exception),
        ("StdinStateError", RuntimeError),
        ("StdinCloseTimeout", TimeoutError),
    ],
)
def test_public_exception_exports(name, base):
    cls = api(name)
    assert cls.__bases__ == (base,)
    assert name in process.__all__
    assert EXISTING_EXPORTS <= set(process.__all__)


def test_public_observation_is_frozen_exact_record():
    cls = api("StdinWriteObservation")
    assert "StdinWriteObservation" in process.__all__
    assert tuple(f.name for f in fields(cls)) == (
        "requested_bytes",
        "accepted_bytes",
        "outcome",
        "error",
        "timed_out",
    )
    record = cls(7, 3, "failed", OSError("write"), False)
    for name in ("requested_bytes", "accepted_bytes", "outcome", "error", "timed_out"):
        with pytest.raises(FrozenInstanceError):
            setattr(record, name, None)


def test_write_error_readonly_declared_cause_not_python_chaining():
    cause = BrokenPipeError("peer")
    before = dict(vars(cause))
    wrapper = api("StdinWriteError")(accepted_bytes=3, cause=cause, timed_out=False)
    assert wrapper.accepted_bytes == 3
    assert wrapper.cause is cause
    assert wrapper.timed_out is False
    assert wrapper.__cause__ is None
    for name in ("accepted_bytes", "cause", "timed_out"):
        with pytest.raises(AttributeError):
            setattr(wrapper, name, None)
    assert vars(cause) == before


@pytest.mark.parametrize("mode", ["", "inherit", "PIPE", None, True, False, 1, [], {}])
def test_invalid_mode_before_factory_or_worker(mode):
    factory = mock.Mock()
    with pytest.raises((TypeError, ValueError)):
        process.OwnedProcess.launch(["child"], stdin_mode=mode, factory=factory)
    factory.assert_not_called()
    assert not workers()


def test_dedicated_mode_defaults_closed():
    parameter = inspect.signature(process.OwnedProcess.launch).parameters.get(
        "stdin_mode"
    )
    assert parameter is not None, "stdin requires a dedicated launch keyword"
    assert parameter.default == "closed"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.fixture
def child_owner():
    """The coordinator alone acquires, signals, reaps and releases this child."""
    owner = process.OwnedProcess.launch(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
        ],
        stdin_mode="pipe",
        deadline_seconds=5,
        grace_seconds=1,
    )
    try:
        yield owner
    finally:
        status = owner.shutdown()
        if not status.complete:
            status = owner.wait_cleanup(2)
        assert status.complete, status
        assert not workers()


def test_explicit_eof_child_exits_without_signalling_or_duplicate_writer(child_owner):
    owner = child_owner
    assert owner.stdin_write_observation() is None
    assert owner.write_stdin(b"hello\x00world", timeout_seconds=1) == 11
    record = owner.stdin_write_observation()
    assert (record.requested_bytes, record.accepted_bytes, record.outcome) == (
        11,
        11,
        "completed",
    )
    assert record.error is None and record.timed_out is False
    assert owner.stdin_write_observation() is record
    with (
        mock.patch.object(
            owner._proc, "terminate", wraps=owner._proc.terminate
        ) as terminate,
        mock.patch.object(owner._proc, "kill", wraps=owner._proc.kill) as kill,
    ):
        assert owner.close_stdin(timeout_seconds=1) is None
        assert owner.close_stdin(timeout_seconds=1) is None
        result = owner.capture()
        assert result.returncode == 0
        assert result.stdout == b"hello\x00world" and result.stderr == b""
        terminate.assert_not_called()
        kill.assert_not_called()
    assert result.lifecycle.complete and not result.lifecycle.failures
    assert tuple(s for s, _ in result.stream_outcomes) == tuple(process.ProcessStream)
    assert len(result.stream_outcomes) == 2
    assert owner.shutdown().complete
    assert owner.stdin_write_observation() is None
    assert record.accepted_bytes == 11


@pytest.mark.parametrize("method", ["write_stdin", "close_stdin"])
@pytest.mark.parametrize(
    "timeout",
    [None, True, False, "1", [], {}, 0, -1, float("nan"), float("inf"), -float("inf")],
)
def test_timeout_validation_preserves_live_owner_and_observation(
    child_owner, method, timeout
):
    owner = child_owner
    assert owner.write_stdin(b"", timeout_seconds=1) == 0
    prior = owner.stdin_write_observation()
    expected = (
        TypeError
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        else ValueError
    )
    with mock.patch.object(process.os, "write", wraps=os.write) as write:
        with pytest.raises(expected):
            function = getattr(owner, method)
            if method == "write_stdin":
                function(b"invalid must not write", timeout_seconds=timeout)
            else:
                function(timeout_seconds=timeout)
        write.assert_not_called()
    assert owner.stdin_write_observation() is prior
    assert owner.returncode is None
    assert owner.write_stdin(b"valid", timeout_seconds=1) == 5
    owner.close_stdin(timeout_seconds=1)
    assert owner.capture().stdout == b"valid"


@pytest.mark.parametrize(
    "data", [None, "text", bytearray(b"bytes"), memoryview(b"bytes"), 1, True]
)
def test_non_bytes_rejection_is_not_channel_failure(child_owner, data):
    owner = child_owner
    with pytest.raises(TypeError):
        owner.write_stdin(data, timeout_seconds=1)
    assert owner.stdin_write_observation() is None
    assert owner.write_stdin(b"valid", timeout_seconds=1) == 5


def test_closed_mode_rejects_without_lazy_pipe_or_observation():
    child = Child()
    child.returncode = 0
    factory = mock.Mock(return_value=child)
    owner = process.OwnedProcess.launch(["child"], factory=factory)
    try:
        assert owner.stdin_write_observation() is None
        for method in (
            lambda: owner.write_stdin(b"", timeout_seconds=1),
            lambda: owner.close_stdin(timeout_seconds=1),
        ):
            with pytest.raises(api("StdinStateError")):
                method()
        assert owner.stdin_write_observation() is None
    finally:
        assert owner.shutdown().complete
    assert not workers()


def test_short_writes_are_completed_on_caller_thread_with_bounded_slices(child_owner):
    owner = child_owner
    original = os.write
    calls = []
    caller = threading.current_thread()

    def short(fd, data):
        calls.append((threading.current_thread(), len(data)))
        return original(fd, data[:3])

    with mock.patch.object(process.os, "write", short):
        assert owner.write_stdin(b"abcdefghijk", timeout_seconds=1) == 11
    assert len(calls) >= 4
    assert all(thread is caller and size <= 65536 for thread, size in calls)
    owner.close_stdin(timeout_seconds=1)
    assert owner.capture().stdout == b"abcdefghijk"


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError("peer closed"),
        OSError("write failure"),
        KeyboardInterrupt("control"),
    ],
)
def test_partial_failure_identity_progress_no_retry_and_no_exception_mutation(
    child_owner, error
):
    owner = child_owner
    original = os.write
    calls = 0
    before = dict(vars(error))

    def fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, data[:3])
        raise error

    with mock.patch.object(process.os, "write", fail):
        if isinstance(error, Exception):
            with pytest.raises(api("StdinWriteError")) as caught:
                owner.write_stdin(b"abcdefgh", timeout_seconds=1)
            assert caught.value.cause is error
            assert caught.value.__cause__ is error
            assert caught.value.accepted_bytes == 3
            assert caught.value.timed_out is False
        else:
            with pytest.raises(KeyboardInterrupt) as caught:
                owner.write_stdin(b"abcdefgh", timeout_seconds=1)
            assert caught.value is error
    record = owner.stdin_write_observation()
    assert record.requested_bytes == 8 and record.accepted_bytes == 3
    assert record.error is error and not record.timed_out
    assert record.outcome == (
        "failed" if isinstance(error, Exception) else "interrupted"
    )
    assert vars(error) == before
    matching = [f for f in owner.cleanup_status.failures if f.error is error]
    assert len(matching) == 1
    assert matching[0].phase is process.FailurePhase.COORDINATION
    assert matching[0].stream is None
    assert matching[0].kind is (
        process.FailureKind.ORDINARY
        if isinstance(error, Exception)
        else process.FailureKind.INTERRUPTION
    )
    with pytest.raises(api("StdinStateError")):
        owner.write_stdin(b"retry forbidden", timeout_seconds=1)
    assert owner.stdin_write_observation() is record
    # Cleanup must not redeliver an already-raised interruption as a fixture
    # error; its retained lifecycle identity remains independently observable.
    owner.close_stdin(timeout_seconds=1)


@pytest.mark.parametrize("error", [OSError("write failed"), KeyboardInterrupt("stop")])
def test_waiting_writer_rejects_failed_channel_preserving_observation(
    child_owner, error
):
    owner = child_owner
    seeded = threading.Event()
    failing = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    results = {}
    writes = []
    original_write = os.write
    original_acquire = owner._stdin_acquire

    def acquire(end, *, writing):
        if threading.current_thread() is waiter and seeded.is_set():
            waiting.set()
        return original_acquire(end, writing=writing)

    def write(fd, data):
        writes.append((threading.current_thread(), bytes(data)))
        if threading.current_thread() is leader:
            failing.set()
            assert release.wait(5)
            raise error
        return original_write(fd, data)

    def wait_then_write():
        try:
            assert owner.write_stdin(b"seed", timeout_seconds=5) == 4
            results["before"] = owner.stdin_write_observation()
            seeded.set()
            assert failing.wait(5)
            try:
                owner.write_stdin(b"forbidden", timeout_seconds=5)
            except BaseException as exc:
                results["waiting_error"] = exc
            results["after"] = owner.stdin_write_observation()
        except BaseException as exc:
            results["waiter_defect"] = exc

    def fail_write():
        try:
            assert seeded.wait(5)
            owner.write_stdin(b"failure", timeout_seconds=5)
        except BaseException as exc:
            results["leader_error"] = exc

    waiter = threading.Thread(target=wait_then_write)
    leader = threading.Thread(target=fail_write)
    with (
        mock.patch.object(owner, "_stdin_acquire", acquire),
        mock.patch.object(process.os, "write", write),
    ):
        waiter.start()
        leader.start()
        try:
            assert waiting.wait(5)
        finally:
            release.set()
            leader.join(5)
            waiter.join(5)
    assert not leader.is_alive() and not waiter.is_alive()
    assert "waiter_defect" not in results
    assert isinstance(results["waiting_error"], api("StdinStateError"))
    assert results["after"] is results["before"]
    assert results["before"].accepted_bytes == 4
    if isinstance(error, Exception):
        assert results["leader_error"].cause is error
    else:
        assert results["leader_error"] is error
    assert writes == [(waiter, b"seed"), (leader, b"failure")]
    owner.close_stdin(timeout_seconds=1)


@pytest.mark.parametrize("state", ["shutdown", "reaped", "expired"])
def test_empty_write_rechecks_state_and_deadline_after_gate_wait(fake_launch, state):
    owner, _ = fake_launch()
    waiting = threading.Event()
    acquired = threading.Event()
    results = {}
    original_acquire = owner._stdin_acquire
    original_time = process.time
    deadline = None

    def acquire(end, *, writing):
        nonlocal deadline
        deadline = end
        waiting.set()
        original_acquire(end, writing=writing)
        acquired.set()

    def monotonic():
        if (
            state == "expired"
            and threading.current_thread() is writer
            and acquired.is_set()
        ):
            return deadline + 1
        return original_time.monotonic()

    def write():
        try:
            owner.write_stdin(b"", timeout_seconds=5)
        except BaseException as exc:
            results["error"] = exc
        results["observation"] = owner.stdin_write_observation()

    writer = threading.Thread(target=write)
    clock = mock.Mock(wraps=original_time)
    clock.monotonic.side_effect = monotonic
    owner._stdin_gate.acquire()
    with (
        mock.patch.object(owner, "_stdin_acquire", acquire),
        mock.patch.object(process, "time", clock),
        mock.patch.object(process.os, "write") as os_write,
    ):
        writer.start()
        try:
            assert waiting.wait(5)
            if state == "shutdown":
                owner._stop.set()
            elif state == "reaped":
                owner._reaped = True
        finally:
            owner._stdin_gate.release()
            writer.join(5)
            owner._stop.clear()
            owner._reaped = False
        assert not writer.is_alive()
        os_write.assert_not_called()
    error = results["error"]
    assert isinstance(error, api("StdinWriteError"))
    assert error.accepted_bytes == 0
    assert error.timed_out is (state == "expired")
    assert isinstance(
        error.cause, TimeoutError if state == "expired" else api("StdinStateError")
    )
    observation = results["observation"]
    assert observation.requested_bytes == observation.accepted_bytes == 0
    assert observation.outcome == "failed"
    assert observation.error is error.cause
    assert observation.timed_out is (state == "expired")
    assert owner._stdin_calls == 0
    assert owner._stdin_failed
    assert any(f.error is error.cause for f in owner.cleanup_status.failures)


def test_backpressure_timeout_has_exact_prefix_and_no_retry():
    owner = process.OwnedProcess.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin_mode="pipe",
        grace_seconds=1,
        deadline_seconds=5,
    )
    try:
        payload = b"x" * (2 * 1024 * 1024)
        with pytest.raises(api("StdinWriteError")) as caught:
            owner.write_stdin(payload, timeout_seconds=0.1)
        record = owner.stdin_write_observation()
        assert 0 < record.accepted_bytes < len(payload)
        assert caught.value.accepted_bytes == record.accepted_bytes
        assert caught.value.cause is record.error
        assert record.timed_out and caught.value.timed_out
        with pytest.raises(api("StdinStateError")):
            owner.write_stdin(b"", timeout_seconds=1)
    finally:
        assert owner.shutdown().complete
    assert not workers()


def test_eof_request_rejects_even_empty_write_without_replacing_observation(
    child_owner,
):
    owner = child_owner
    owner.write_stdin(b"", timeout_seconds=1)
    prior = owner.stdin_write_observation()
    owner.close_stdin(timeout_seconds=1)
    with pytest.raises(api("StdinStateError")):
        owner.write_stdin(b"", timeout_seconds=1)
    assert owner.stdin_write_observation() is prior
    assert owner.close_stdin(timeout_seconds=1) is None


class StdinPipe:
    """Descriptor-shaped test double; owns no operating-system resource."""

    def __init__(self, error=None):
        self.attempts = 0
        self.error = error
        self.hook = None

    def fileno(self):
        return 987654

    def close(self):
        self.attempts += 1
        if self.hook:
            self.hook()
        if self.error:
            raise self.error

    def write(self, data):
        raise AssertionError("must use direct nonblocking os.write, not buffering")


@pytest.fixture
def fake_launch(monkeypatch):
    original_blocking, original_write = os.set_blocking, os.write
    owners = []

    def blocking(fd, mode):
        if fd != 987654:
            return original_blocking(fd, mode)
        assert mode is False

    def write(fd, data):
        return len(data) if fd == 987654 else original_write(fd, data)

    monkeypatch.setattr(process.os, "set_blocking", blocking)
    monkeypatch.setattr(process.os, "write", write)

    def launch(child=None, *, omit_stdin=False, **kwargs):
        child = child or Child()
        if not omit_stdin and not hasattr(child, "stdin"):
            child.stdin = StdinPipe()

        def factory(*args, **options):
            assert options["stdin"] == process.subprocess.PIPE
            return child

        owner = process.OwnedProcess.launch(
            ["child"],
            factory=factory,
            stdin_mode="pipe",
            grace_seconds=0.1,
            **kwargs,
        )
        owners.append(owner)
        return owner, child

    yield launch
    for owner in owners:
        assert owner.shutdown().complete or owner.wait_cleanup(2).complete
    assert not workers()


@pytest.mark.parametrize("position", [1, 2, 3])
@pytest.mark.parametrize("error", [OSError("prepare"), KeyboardInterrupt("prepare")])
def test_all_pipe_owners_prepared_before_factory(position, error):
    real = process._prepare_stream
    count = 0
    factory = mock.Mock()

    def prepare(**kw):
        nonlocal count
        count += 1
        if count == position:
            raise error
        return real(**kw)

    with mock.patch.object(process, "_prepare_stream", prepare):
        with pytest.raises(type(error)) as caught:
            process.OwnedProcess.launch(["child"], factory=factory, stdin_mode="pipe")
    assert caught.value is error
    factory.assert_not_called()
    assert not workers()


def test_factory_pipe_argument_and_prepared_stdin_label(fake_launch):
    owner, child = fake_launch()
    assert owner._stdin_owner.label == "stdin"
    assert owner._stdin_owner.stream is child.stdin
    assert owner._stdin_fd == child.stdin.fileno()
    assert tuple(process.ProcessStream.__members__) == ("STDOUT", "STDERR")


@pytest.mark.parametrize("missing", ["absent", "none", "stdout", "stderr"])
def test_missing_or_aliased_stdin_fails_closed_independent_release(
    fake_launch, missing
):
    child = Child()
    if missing != "absent":
        child.stdin = None if missing == "none" else getattr(child, missing)
    owner, child = fake_launch(child, omit_stdin=missing == "absent")
    status = owner.wait_cleanup(1)
    assert status.complete
    assert any(
        f.phase is process.FailurePhase.SETUP and f.stream is None
        for f in status.failures
    )
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert child.returncode is not None
    _assert_failed_setup_rejects_eof(owner)
    assert child.stdout.attempts == child.stderr.attempts == 1


def _assert_failed_setup_rejects_eof(owner):
    before = owner.cleanup_status
    observation = owner.stdin_write_observation()
    cleanup_end = owner._cleanup_end
    eof_deadline = owner._stdin_eof_deadline
    with mock.patch.object(owner, "_stdin_acquire") as acquire:
        for _ in range(2):
            with pytest.raises(process.StdinStateError):
                owner.close_stdin(timeout_seconds=1)
        acquire.assert_not_called()
    assert owner.cleanup_status == before
    assert owner.stdin_write_observation() is observation
    assert owner._cleanup_end == cleanup_end
    assert owner._stdin_eof_deadline == eof_deadline
    assert not owner._stdin_eof_requested


@pytest.mark.parametrize("error", [OSError("handoff"), KeyboardInterrupt("handoff")])
@pytest.mark.parametrize("close_error", [None, OSError("stdin close")])
def test_partial_stdin_configuration_releases_every_sibling(
    fake_launch, error, close_error
):
    child = Child()
    child.stdin = StdinPipe(close_error)
    with mock.patch.object(process.os, "set_blocking", side_effect=error):
        if isinstance(error, Exception):
            owner, _ = fake_launch(child)
        else:
            with pytest.raises(KeyboardInterrupt) as caught:
                fake_launch(child)
            assert caught.value is error
            owner = caught.value.cleanup
    status = owner.wait_cleanup(1)
    assert status.complete
    assert status.failures[0].error is error
    assert status.failures[0].phase is process.FailurePhase.SETUP
    assert status.failures[0].stream is None
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1
    assert owner.shutdown().complete
    _assert_failed_setup_rejects_eof(owner)
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1
    if close_error:
        assert any(
            f.error is close_error
            and f.phase is process.FailurePhase.RELEASE
            and f.stream is None
            for f in status.failures
        )


@pytest.mark.parametrize(
    "operation", [None, OSError("write"), KeyboardInterrupt("write")]
)
def test_write_plus_close_and_sole_close_failures_retained_once(fake_launch, operation):
    close_error = OSError("stdin sole close")
    child = Child()
    child.stdin = StdinPipe(close_error)
    reporter = mock.Mock()
    owner, _ = fake_launch(child, reporter=reporter)
    if operation:
        with mock.patch.object(process.os, "write", side_effect=operation):
            with pytest.raises(
                api("StdinWriteError")
                if isinstance(operation, Exception)
                else KeyboardInterrupt
            ) as caught:
                owner.write_stdin(b"payload", timeout_seconds=1)
        if isinstance(operation, Exception):
            assert caught.value.cause is operation
        else:
            assert caught.value is operation
        snapshot = owner.stdin_write_observation()
        assert snapshot.error is operation
    for _ in range(2):
        with pytest.raises(OSError) as caught:
            owner.close_stdin(timeout_seconds=1)
        assert caught.value is close_error
    status = owner.shutdown()
    assert status.complete
    assert status.failures[0].error is (operation or close_error)
    expected = [(close_error, process.FailurePhase.RELEASE)]
    if operation:
        expected.append((operation, process.FailurePhase.COORDINATION))
    for error, phase in expected:
        matches = [f for f in status.failures if f.error is error]
        assert (
            len(matches) == 1
            and matches[0].phase is phase
            and matches[0].stream is None
        )
    assert owner.wait_cleanup(1).failures == status.failures
    assert owner.cleanup_status.failures == status.failures
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1
    reporter.assert_not_called()

    # Negative controls: this assertion helper detects both dropped and
    # output-misattributed release observations, not only a matching message.
    def require_release(failures):
        assert any(
            f.error is close_error
            and f.phase is process.FailurePhase.RELEASE
            and f.stream is None
            for f in failures
        )

    require_release(status.failures)
    with pytest.raises(AssertionError):
        require_release(tuple(f for f in status.failures if f.error is not close_error))
    with pytest.raises(AssertionError):
        require_release(
            (
                process.FailureInfo(
                    close_error,
                    process.FailureKind.ORDINARY,
                    process.FailurePhase.RELEASE,
                    process.ProcessStream.STDOUT,
                ),
            )
        )


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt("close"), RuntimeError("close defect")]
)
def test_close_interruption_or_defect_and_repeat_independent_cleanup(
    fake_launch, error
):
    child = Child()
    child.stdin = StdinPipe(error)
    owner, _ = fake_launch(child)
    for _ in range(2):
        with pytest.raises(type(error)) as caught:
            owner.close_stdin(timeout_seconds=1)
        assert caught.value is error
    status = owner.shutdown()
    assert status.complete and status.failures[0].error is error
    assert status.failures[0].kind is (
        process.FailureKind.INTERRUPTION
        if not isinstance(error, Exception)
        else process.FailureKind.UNEXPECTED
    )
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1


def test_gate_timeout_zero_progress_no_payload_queue_or_writer_worker(fake_launch):
    owner, _ = fake_launch()
    initial_workers = set(workers())
    owner._stdin_gate.acquire()
    try:
        with pytest.raises(api("StdinWriteError")) as caught:
            owner.write_stdin(b"blocked", timeout_seconds=0.02)
        assert caught.value.accepted_bytes == 0 and caught.value.timed_out
        assert owner.stdin_write_observation().accepted_bytes == 0
        assert set(workers()) == initial_workers
        # The sole queue remains the existing bounded output spool.
        import queue

        assert [v for v in vars(owner).values() if isinstance(v, queue.Queue)] == [
            owner._output
        ]
    finally:
        owner._stdin_gate.release()


def test_eof_timeout_retains_owner_no_close_beneath_writer_and_no_new_budget(
    fake_launch,
):
    owner, child = fake_launch()
    entered, release = threading.Event(), threading.Event()
    results = []
    original = process.os.write

    def blocked(fd, data):
        entered.set()
        assert release.wait(2)
        return original(fd, data)

    def writer():
        results.append(owner.write_stdin(b"active", timeout_seconds=1))

    with mock.patch.object(process.os, "write", blocked):
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            assert entered.wait(1)
            continuation = owner._continuation
            with pytest.raises(api("StdinCloseTimeout")):
                owner.close_stdin(timeout_seconds=0.02)
            deadline = owner._stdin_eof_deadline
            with pytest.raises(api("StdinCloseTimeout")):
                owner.close_stdin(timeout_seconds=100)
            assert owner._stdin_eof_deadline == deadline
            assert child.stdin.attempts == 0
            assert owner._continuation is continuation
            with pytest.raises(api("StdinStateError")):
                owner.write_stdin(b"", timeout_seconds=1)
        finally:
            release.set()
            thread.join(2)
    assert not thread.is_alive() and results == [6]
    assert owner.shutdown().complete or owner.wait_cleanup(1).complete
    assert child.stdin.attempts == 1


def test_shutdown_cancels_backpressure_writer_without_close_race():
    owner = process.OwnedProcess.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin_mode="pipe",
        grace_seconds=0.2,
        deadline_seconds=5,
    )
    entered = threading.Event()
    captured = threading.Event()
    results = []
    original = os.write

    def write(fd, data):
        result = original(fd, data)
        entered.set()
        return result

    def writer():
        try:
            owner.write_stdin(b"x" * (2 * 1024 * 1024), timeout_seconds=10)
        except Exception as error:
            results.append((error, owner.stdin_write_observation()))
            captured.set()

    retire = owner._retire_stdin_observations

    def retire_after_bridge(result):
        # Capture on the writing thread before explicit final retirement, as
        # required of a transport bridge; do not race its accessor afterward.
        assert captured.wait(2)
        retire(result)

    thread = threading.Thread(target=writer)
    patch = mock.patch.object(process.os, "write", write)
    patch.start()
    thread.start()
    try:
        assert entered.wait(1)
        with mock.patch.object(
            owner, "_retire_stdin_observations", retire_after_bridge
        ):
            status = owner.shutdown()
        thread.join(2)
        assert not thread.is_alive()
        assert status.complete or owner.wait_cleanup(1).complete
        assert len(results) == 1
        error, observation = results[0]
        assert isinstance(error, api("StdinWriteError"))
        assert error.cause is observation.error
        assert not error.timed_out
    finally:
        thread.join(2)
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
        patch.stop()
    assert not workers()


def test_large_syscall_bound_and_output_only_callbacks(child_owner):
    owner = child_owner
    sizes = []
    original = os.write

    def bounded(fd, data):
        sizes.append(len(data))
        return original(fd, data)

    with mock.patch.object(process.os, "write", bounded):
        assert owner.write_stdin(b"x" * 200000, timeout_seconds=2) == 200000
    assert sizes and max(sizes) <= 65536
    owner.close_stdin(timeout_seconds=1)
    chunks, ends = [], []
    result = owner.stream(
        lambda s, b: chunks.append((s, b)),
        on_stream_end=lambda s, o: ends.append((s, o)),
    )
    assert sum(len(b) for _, b in chunks) == 200000
    assert {s for s, _ in chunks} <= set(process.ProcessStream)
    assert set(ends) == {(s, process.StreamOutcome.EOF) for s in process.ProcessStream}
    assert result.lifecycle.complete


def test_observations_per_live_thread_last_call_then_prune_and_retire(fake_launch):
    import weakref

    owner, _ = fake_launch()
    assert isinstance(owner._stdin_observations, weakref.WeakKeyDictionary)
    owner.write_stdin(b"main", timeout_seconds=1)
    main = owner.stdin_write_observation()
    ready, release = threading.Event(), threading.Event()
    observations = []

    def worker():
        observations.append(owner.stdin_write_observation())
        owner.write_stdin(b"worker", timeout_seconds=1)
        first = owner.stdin_write_observation()
        observations.append(first)
        # Same live worker must see its prior record before its next call.
        assert owner.stdin_write_observation() is first
        owner.write_stdin(b"next", timeout_seconds=1)
        observations.append(owner.stdin_write_observation())
        ready.set()
        assert release.wait(2)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(1)
        assert owner.stdin_write_observation() is main
        assert len(owner._stdin_observations) == 2
        assert observations[0] is None
        assert (
            observations[1].accepted_bytes == 6 and observations[2].accepted_bytes == 4
        )
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    # Keep the actual terminated Thread externally referenced; GC isn't pruning.
    assert owner.stdin_write_observation() is main
    assert len(owner._stdin_observations) == 1
    newcomers = []
    newcomer = threading.Thread(
        target=lambda: newcomers.append(owner.stdin_write_observation())
    )
    newcomer.start()
    newcomer.join(2)
    assert newcomers == [None]
    owner.close_stdin(timeout_seconds=1)
    assert owner.stdin_write_observation() is main
    assert owner.cleanup_status.complete is False
    assert owner.shutdown().complete
    assert owner.stdin_write_observation() is None
    assert len(owner._stdin_observations) == 0
    with pytest.raises(api("StdinStateError")):
        owner.write_stdin(b"", timeout_seconds=1)
    assert owner.stdin_write_observation() is None
    assert main.accepted_bytes == 4


def test_deterministic_same_ident_distinct_thread_objects_never_share_records(
    fake_launch,
):
    owner, _ = fake_launch()
    first, second = threading.Thread(), threading.Thread()
    # Numeric identity is deliberately equal; actual object identity is not.
    first._ident = second._ident = 123
    with (
        mock.patch.object(first, "is_alive", return_value=True),
        mock.patch.object(second, "is_alive", return_value=True),
        mock.patch.object(process, "_stdin_thread", return_value=first),
    ):
        owner.write_stdin(b"first", timeout_seconds=1)
        saved = owner.stdin_write_observation()
        with mock.patch.object(process, "_stdin_thread", return_value=second):
            assert owner.stdin_write_observation() is None
            owner.write_stdin(b"second", timeout_seconds=1)
            assert owner.stdin_write_observation().accepted_bytes == 6
        assert owner.stdin_write_observation() is saved
        with mock.patch.object(first, "is_alive", return_value=False):
            assert owner.stdin_write_observation() is None
            assert first not in owner._stdin_observations


@pytest.mark.parametrize("state", ["shutdown", "reap"])
def test_reap_or_shutdown_reads_do_not_retire_pending_observations(fake_launch, state):
    owner, child = fake_launch()
    owner.write_stdin(b"record", timeout_seconds=1)
    record = owner.stdin_write_observation()
    owner._stdin_gate.acquire()
    try:
        if state == "reap":
            child.returncode = 0
            owner._wait_child(0)
        else:
            status = owner.shutdown()
            assert not status.complete
            cleanup_end = owner._cleanup_end
            assert owner.shutdown().complete is False
            assert owner._cleanup_end == cleanup_end
        assert owner.stdin_write_observation() is record
        _ = owner.cleanup_status
        assert owner.stdin_write_observation() is record
        with pytest.raises(api("StdinStateError")):
            owner.write_stdin(b"", timeout_seconds=1)
        if state == "reap":
            with pytest.raises(api("StdinStateError")):
                owner.close_stdin(timeout_seconds=1)
    finally:
        owner._stdin_gate.release()
    assert owner.shutdown().complete or owner.wait_cleanup(1).complete
    assert owner.stdin_write_observation() is None
    assert record.accepted_bytes == 6


def test_cleanup_status_prunes_dead_threads_without_retiring_live_snapshot(fake_launch):
    owner, _ = fake_launch()
    owner.write_stdin(b"live", timeout_seconds=1)
    saved = owner.stdin_write_observation()
    thread = threading.Thread(
        target=lambda: owner.write_stdin(b"dead", timeout_seconds=1)
    )
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert len(owner._stdin_observations) == 2
    assert not owner.cleanup_status.complete
    assert len(owner._stdin_observations) == 1
    assert owner.stdin_write_observation() is saved


def test_unexpected_write_defect_is_not_demoted(fake_launch):
    owner, _ = fake_launch()
    error = RuntimeError("internal defect")
    with mock.patch.object(process.os, "write", side_effect=error):
        with pytest.raises(api("StdinWriteError")) as caught:
            owner.write_stdin(b"x", timeout_seconds=1)
    assert caught.value.cause is error
    match = next(f for f in owner.cleanup_status.failures if f.error is error)
    assert match.kind is process.FailureKind.UNEXPECTED


def test_concurrent_eof_shutdown_close_winner_settles_once(fake_launch):
    owner, child = fake_launch()
    entered, release = threading.Event(), threading.Event()
    child.stdin.hook = lambda: (entered.set(), release.wait(2))
    results = []
    thread = threading.Thread(
        target=lambda: results.append(owner.close_stdin(timeout_seconds=1))
    )
    thread.start()
    try:
        assert entered.wait(1)
        status = owner.shutdown()
        assert not status.complete
        assert child.stdin.attempts == 1
        assert not owner.wait_cleanup(0).complete
        assert owner._stdin_owner.released
    finally:
        release.set()
        thread.join(2)
    assert results == [None]
    assert owner.wait_cleanup(1).complete
    assert child.stdin.attempts == 1


@pytest.mark.parametrize("mode", ["closed", "pipe"])
def test_stdin_override_never_launches_even_with_dedicated_mode(mode):
    factory = mock.Mock()
    with pytest.raises(ValueError):
        process.OwnedProcess.launch(
            ["child"],
            stdin_mode=mode,
            factory=factory,
            popen_kwargs={"stdin": process.subprocess.PIPE},
        )
    factory.assert_not_called()
    assert not workers()


def test_factory_failure_in_pipe_mode_retains_no_unacquired_stdin():
    error = OSError("factory")
    owner = process.OwnedProcess.launch(
        ["child"], stdin_mode="pipe", factory=mock.Mock(side_effect=error)
    )
    assert owner.shutdown().complete
    status = owner.wait_cleanup(1)
    assert status.failures[0].error is error
    assert status.failures[0].phase is process.FailurePhase.SETUP
    assert status.failures[0].stream is None
    assert owner.stdin_write_observation() is None
    _assert_failed_setup_rejects_eof(owner)
    assert not workers()


def test_output_handoff_failure_also_releases_prepared_stdin(fake_launch):
    error = OSError("output handoff")
    with mock.patch.object(process.OwnedProcess, "_transfer", side_effect=error):
        owner, child = fake_launch()
    assert owner.wait_cleanup(1).complete
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1
    assert any(
        f.error is error and f.phase is process.FailurePhase.SETUP
        for f in owner.cleanup_status.failures
    )
    _assert_failed_setup_rejects_eof(owner)
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1


@pytest.mark.parametrize(
    "error", [OSError("launch callback"), KeyboardInterrupt("launch callback")]
)
def test_failed_launch_callback_cannot_report_eof_success(fake_launch, error):
    child = Child()
    child.stdin = StdinPipe()
    callback = mock.Mock(side_effect=error)
    if isinstance(error, Exception):
        owner, _ = fake_launch(child, on_launched=callback)
    else:
        with pytest.raises(KeyboardInterrupt) as caught:
            fake_launch(child, on_launched=callback)
        assert caught.value is error
        owner = caught.value.cleanup
    assert owner.wait_cleanup(1).complete
    _assert_failed_setup_rejects_eof(owner)
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1


@pytest.mark.parametrize(
    "error", [OSError("setup publication"), KeyboardInterrupt("setup publication")]
)
def test_failed_setup_publication_cannot_report_eof_success(fake_launch, error):
    original_make = process._make_continuation

    def prepare(action):
        owner = action.__self__
        original_set = owner._setup_done.set
        calls = 0

        def publish():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error
            original_set()

        owner._setup_done.set = publish
        return original_make(action)

    child = Child()
    child.stdin = StdinPipe()
    with mock.patch.object(process, "_make_continuation", prepare):
        if isinstance(error, Exception):
            owner, _ = fake_launch(child)
        else:
            with pytest.raises(KeyboardInterrupt) as caught:
                fake_launch(child)
            assert caught.value is error
            owner = caught.value.cleanup
    assert owner.wait_cleanup(1).complete
    _assert_failed_setup_rejects_eof(owner)
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1


def test_shutdown_released_valid_stdin_repeat_eof_observes_success(fake_launch):
    owner, child = fake_launch()
    assert owner.shutdown().complete
    cleanup_end = owner._cleanup_end
    for _ in range(2):
        assert owner.close_stdin(timeout_seconds=1) is None
    assert owner._cleanup_end == cleanup_end
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1


def test_observation_thread_churn_pruned_on_publication_read_and_cleanup(fake_launch):
    owner, _ = fake_launch()
    threads = []
    for _ in range(20):
        t = threading.Thread(target=lambda: owner.write_stdin(b"x", timeout_seconds=1))
        t.start()
        t.join(1)
        assert not t.is_alive()
        threads.append(t)  # deliberate strong external retention
    # Each publication prunes all prior dead contributors, not only reads.
    assert len(owner._stdin_observations) <= 1
    assert owner.stdin_write_observation() is None
    assert len(owner._stdin_observations) == 0
    owner.write_stdin(b"main", timeout_seconds=1)
    assert owner.shutdown().complete
    assert owner.stdin_write_observation() is None


def test_concurrent_pending_close_is_ledgered_with_stable_identity(fake_launch):
    owner, child = fake_launch()
    entered, release = threading.Event(), threading.Event()
    child.stdin.hook = lambda: (entered.set(), release.wait(2))
    errors = []
    thread = threading.Thread(target=lambda: owner.close_stdin(timeout_seconds=1))
    thread.start()
    try:
        assert entered.wait(1)
        for _ in range(2):
            with pytest.raises(api("StdinCloseTimeout")) as caught:
                owner.close_stdin(timeout_seconds=1)
            errors.append(caught.value)
        assert errors[0] is errors[1]
        matches = [f for f in owner.cleanup_status.failures if f.error is errors[0]]
        assert (
            len(matches) == 1 and matches[0].phase is process.FailurePhase.COORDINATION
        )
        assert matches[0].stream is None
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_stdin_interruption_collection_does_not_attach_attributes(fake_launch):
    owner, child = fake_launch()
    error = KeyboardInterrupt("write")
    before = dict(vars(error))
    with mock.patch.object(process.os, "write", side_effect=error):
        with pytest.raises(KeyboardInterrupt) as caught:
            owner.write_stdin(b"x", timeout_seconds=1)
    assert caught.value is error
    owner.close_stdin(timeout_seconds=1)
    child.returncode = 0
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.capture()
    assert caught.value is error
    assert vars(error) == before


def test_git_and_docker_launches_do_not_opt_in_stdin():
    import ast
    from pathlib import Path

    checked = 0
    for name in ("snapshot.py", "container.py", "supervision.py"):
        root = Path(process.__file__).parent
        tree = ast.parse((root / name).read_bytes())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("launch", "_launch")
            ):
                checked += 1
                for keyword in node.keywords:
                    if keyword.arg == "stdin_mode":
                        assert (
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value == "closed"
                        )
    assert checked >= 3


@pytest.mark.parametrize(
    "error", [OSError("EOF coordination"), KeyboardInterrupt("EOF coordination")]
)
def test_eof_gate_interruption_or_failure_retains_continuation(fake_launch, error):
    owner, child = fake_launch()
    gate = owner._stdin_gate

    class InterruptedGate:
        def acquire(self, *args, **kwargs):
            if "timeout" in kwargs:
                raise error
            return gate.acquire(*args, **kwargs)

        def release(self):
            return gate.release()

    owner._stdin_gate = InterruptedGate()
    before = dict(vars(error))
    with pytest.raises(type(error)) as caught:
        owner.close_stdin(timeout_seconds=1)
    assert caught.value is error and vars(error) == before
    match = next(f for f in owner.cleanup_status.failures if f.error is error)
    assert match.phase is process.FailurePhase.COORDINATION and match.stream is None
    assert match.kind is (
        process.FailureKind.ORDINARY
        if isinstance(error, Exception)
        else process.FailureKind.INTERRUPTION
    )
    assert owner.shutdown().complete or owner.wait_cleanup(1).complete
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1
    assert vars(error) == before


def test_shutdown_fallback_stdin_sole_close_failure_is_not_lost(fake_launch):
    child = Child()
    error = OSError("fallback stdin close")
    child.stdin = StdinPipe(error)
    owner, _ = fake_launch(child)
    status = owner.shutdown()
    assert status.complete and status.failures[0].error is error
    assert status.failures[0].phase is process.FailurePhase.RELEASE
    assert status.failures[0].stream is None
    assert owner.wait_cleanup(1).failures == status.failures
    assert owner.shutdown().failures == status.failures
    for _ in range(2):
        with pytest.raises(OSError) as caught:
            owner.close_stdin(timeout_seconds=1)
        assert caught.value is error
    assert owner.cleanup_status.failures == status.failures
    assert child.stdin.attempts == child.stdout.attempts == child.stderr.attempts == 1


def test_stdin_documentation_contract_and_phase_retargeting():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "docs/process-lifecycle.md").read_text()
    for token in (
        'stdin_mode="pipe"',
        'stdin_mode="closed"',
        "write_stdin(data: bytes, *, timeout_seconds: float) -> int",
        "close_stdin(*, timeout_seconds: float) -> None",
        "StdinWriteObservation",
        "StdinWriteError(Exception)",
        "StdinStateError(RuntimeError)",
        "unbound/failed setup",
        "StdinCloseTimeout(TimeoutError)",
        "stdin_write_observation() -> StdinWriteObservation | None",
        "65536",
        "weak actual Thread-object keys",
        "not as a second lifecycle",
        "wrapper.cause is observation.error is matching_ledger_failure.error",
        "stream=None",
        "SETUP",
        "COORDINATION",
        "RELEASE",
    ):
        assert token in text
    assert "No process-tree/group management, interactive stdin API" not in text
    assert "Every child receives" not in text
    forbidden = {
        "docs/testing.md": (
            "phase 8 protocol",
            "Phase 8 MCP",
            "Phase 10 actual Docker",
            "phases 1–9",
            "Phase 10 separately",
            "phase 10 creates",
        ),
        "docs/python-checks.md": ("phases 1–9", "Phase 10 adds", "until phase 10"),
        "docs/results.md": ("Phase 8 MCP",),
        "docs/configuration.md": ("Phase 10 host",),
        "docs/snapshots.md": ("Phase 10 obligations",),
        "src/footman/startup.py": ("delivered in Phase 8",),
    }
    for name, tokens in forbidden.items():
        content = (root / name).read_text()
        assert not any(token in content for token in tokens), name


@pytest.mark.parametrize("alias", ["stdout", "stderr"])
@pytest.mark.parametrize("failure", ["none", "close", "interrupt"])
def test_distinct_wrapper_same_fd_quarantines_only_conflict(tmp_path, alias, failure):
    result = _isolated_fd_alias_control(tmp_path, alias, failure)
    sibling = "stderr" if alias == "stdout" else "stdout"
    assert result["initial_complete"] is result["complete"] is False
    assert not result["stdin_ever_bound"]
    assert result["input_calls_rejected"] == [True, True]
    assert result["counts"] == {"stdin": 0, alias: 0, sibling: 1}
    assert result["candidate_retained"]
    assert result["alive"]["stdin"] and result["alive"][alias]
    assert result["continuation_same"] and result["continuation_alive"]
    assert result["deadline_same"] and result["actions_same"]
    assert result["returncode"] is not None
    assert not result["launched"]
    assert result["failures_stable"]
    setup = next(f for f in result["failures"] if f["phase"] == "SETUP")
    assert setup["type"] == "TypeError" and setup["stream"] is None
    assert alias in setup["message"] and "retained" in setup["message"]
    if failure != "none":
        release = next(f for f in result["failures"] if f["phase"] == "RELEASE")
        assert release["stream"] == sibling.upper()
        assert release["type"] == (
            "OSError" if failure == "close" else "KeyboardInterrupt"
        )
        assert result["failures"][0]["phase"] == (
            "SETUP" if failure == "close" else "RELEASE"
        )


@pytest.mark.parametrize(
    "scenario", ["probe", "probe-interrupt", "stdout-probe", "stdout-probe-interrupt"]
)
@pytest.mark.parametrize("failure", ["none", "close", "interrupt"])
def test_stdin_fd_probe_failure_retains_unproven_group(tmp_path, scenario, failure):
    result = _isolated_fd_alias_control(tmp_path, scenario, failure)
    assert result["complete"] is False
    assert not result["stdin_ever_bound"]
    assert result["input_calls_rejected"] == [True, True]
    assert result["counts"] == {"stdin": 0, "stdout": 0, "stderr": 1}
    assert result["candidate_retained"]
    assert result["alive"]["stdin"] and result["alive"]["stdout"]
    assert result["continuation_same"] and result["continuation_alive"]
    assert result["deadline_same"] and result["actions_same"]
    assert not result["launched"]
    setup = next(f for f in result["failures"] if f["phase"] == "SETUP")
    assert setup["stream"] is None
    assert setup["type"] == (
        "KeyboardInterrupt" if scenario.endswith("interrupt") else "OSError"
    )


@pytest.mark.parametrize("scenario", ["fd-bool", "fd-negative", "fd-text"])
def test_invalid_descriptor_metadata_does_not_claim_safe_cleanup(tmp_path, scenario):
    result = _isolated_fd_alias_control(tmp_path, scenario, "none")
    assert result["complete"] is False and not result["launched"]
    assert result["counts"] == {"stdin": 0, "stdout": 0, "stderr": 1}
    assert result["alive"]["stdin"] and result["alive"]["stdout"]
    assert result["failures"][0]["phase"] == "SETUP"
    assert result["failures"][0]["type"] == "TypeError"


@pytest.mark.parametrize("failure", ["none", "close", "interrupt"])
def test_explicitly_descriptorless_stdin_releases_independent_owners(tmp_path, failure):
    result = _isolated_fd_alias_control(tmp_path, "descriptorless-stdin", failure)
    assert result["complete"] and not result["launched"]
    assert result["counts"] == {"stdin": 1, "stdout": 1, "stderr": 1}
    assert not result["continuation_alive"]
    setup = next(f for f in result["failures"] if f["phase"] == "SETUP")
    assert setup["type"] == "TypeError" and setup["stream"] is None


@pytest.mark.parametrize("scenario", ["distinct", "opposite"])
def test_distinct_fd_numbers_do_not_trigger_alias_retention(tmp_path, scenario):
    result = _isolated_fd_alias_control(tmp_path, scenario, "none")
    assert result["complete"] and result["initial_complete"]
    assert len(set(result["descriptor_numbers"])) == 3
    assert result["counts"] == {"stdin": 1, "stdout": 1, "stderr": 1}
    assert result["failures"] == []
    assert result["launched"] and not result["continuation_alive"]
    assert result["deadline_same"] and result["actions_same"]


@pytest.mark.parametrize("failure", ["close", "interrupt"])
def test_distinct_fds_sole_close_failure_keeps_existing_ledger(tmp_path, failure):
    result = _isolated_fd_alias_control(tmp_path, "distinct", failure)
    assert result["complete"] and result["launched"]
    assert result["stdin_ever_bound"]
    assert result["counts"] == {"stdin": 1, "stdout": 1, "stderr": 1}
    assert len(result["failures"]) == 1
    release = result["failures"][0]
    assert release["phase"] == "RELEASE" and release["stream"] == "STDERR"
    assert release["type"] == ("OSError" if failure == "close" else "KeyboardInterrupt")


def test_retention_wait_interruption_keeps_same_unresolved_group(tmp_path):
    result = _isolated_fd_alias_control(tmp_path, "park-interrupt", "none")
    assert result["complete"] is False
    assert result["counts"] == {"stdin": 0, "stdout": 0, "stderr": 1}
    assert result["alive"]["stdin"] and result["alive"]["stdout"]
    assert result["continuation_same"] and result["continuation_alive"]
    assert result["deadline_same"] and result["actions_same"]
    assert result["failures_stable"]
    assert result["failures"][0]["phase"] == "COORDINATION"
    assert result["failures"][0]["type"] == "KeyboardInterrupt"
    assert any(
        f["phase"] == "SETUP" and f["type"] == "TypeError" for f in result["failures"]
    )


def _isolated_fd_alias_control(tmp_path, scenario, failure):
    import json
    from pathlib import Path

    script = Path(__file__).with_name("process_stdin_alias_child.py")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")
    }
    owner = process.OwnedProcess.launch(
        [sys.executable, "-P", str(script), scenario, failure],
        deadline_seconds=5,
        grace_seconds=1,
        popen_kwargs={"cwd": str(tmp_path), "env": env},
    )
    with owner:
        result = owner.capture()
    assert result.returncode == 0, result.stderr
    assert result.lifecycle.complete and not result.lifecycle.failures
    assert result.stderr == b""
    assert not workers()
    return json.loads(result.stdout)


def test_registered_suite_documentation_contract():
    from pathlib import Path

    from tests.test_green_gate import assert_documented_suite

    root = Path(__file__).resolve().parents[1]
    for name in ("docs/testing.md", "docs/python-checks.md", "README.md"):
        assert_documented_suite((root / name).read_text())
    command = ".venv/bin/python -m pytest tests/test_process_stdin.py tests/test_process_lifecycle.py tests/test_process_orthogonal_results.py tests/test_process_single_collection.py -n 2 --dist worksteal --tb=short -q"
    assert (root / "docs/testing.md").read_text().count(command) == 1
