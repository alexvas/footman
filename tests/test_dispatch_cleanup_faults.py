"""Phase 6 domain ownership, diagnostic, budget and no-follow fault matrix."""

import base64
import errno
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from footman.process import (
    FailureInfo,
    FailureKind,
    FailurePhase,
    LifecycleResult,
    ProcessResult,
    ProcessStream,
    StopContext,
    StreamOutcome,
)
from tests.dispatch_support import api, removal
from tests.dispatcher_harness import read_bytes, write_bytes
from tests.launcher_case_support import ID, source

__all__ = ["source"]


class Helper:
    def __init__(
        self,
        data=b"",
        *,
        code=0,
        failure=None,
        outcome=StreamOutcome.EOF,
        complete=True,
        interruption=None,
    ):
        self.data = data
        self.code = code
        self.failure = failure
        self.outcome = outcome
        self.complete = complete
        self.interruption = interruption
        self.collections = 0
        self.exits = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exits += 1

    @property
    def cleanup_status(self):
        return LifecycleResult(
            self.complete, () if self.failure is None else (self.failure,)
        )

    def wait_cleanup(self, timeout):
        return self.cleanup_status

    def stream(self, chunk, *, on_stream_end):
        self.collections += 1
        for stream in ProcessStream:
            for start in range(0, len(self.data), 37):
                chunk(stream, self.data[start : start + 37])
            on_stream_end(stream, self.outcome)
        if self.interruption:
            raise self.interruption
        return ProcessResult(
            b"wrong trailing capture",
            b"wrong tail",
            self.code,
            False,
            self.cleanup_status,
            tuple((s, self.outcome) for s in ProcessStream),
        )


def action_case(helpers, *, allowance=1):
    module = api()
    calls = []

    def launch(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return helpers[len(calls) - 1]

    action = module.ContainerCleanup(ID, launch=launch)
    action.prepare()
    action.start(StopContext("test", 0, time.monotonic() + allowance))
    end = time.monotonic() + 2
    while not action.observe().done and time.monotonic() < end:
        time.sleep(0.001)
    assert action.observe().done
    return action, calls


@pytest.mark.parametrize("operation", [0, 1])
def test_helper_raw_leading_prefix_and_counts(operation):
    raw = (
        b"https://user:password@example/?token=secret\xff"
        + b"a" * 4040
        + b"\xfeTRAILING" * 500
    )
    helpers = [Helper(), Helper()]
    helpers[operation].data = raw
    action, calls = action_case(helpers)
    for stream in ProcessStream:
        summary = action.operations[operation].collector.summaries[stream]
        assert base64.b64decode(summary.excerpt_base64) == raw[:4096]
        assert summary.observed_bytes == len(raw)
        assert summary.retained_bytes == 4096
        assert summary.truncated
        assert summary.outcome is StreamOutcome.EOF
    assert action.confirmed_absent is (operation == 0)
    assert [list(argv) for argv, _ in calls] == [s["argv"] for s in removal()]
    for _, kwargs in calls:
        assert kwargs["capture_limit"] == 0
        assert "stop_policy" not in kwargs  # separately owned default-policy helpers
        assert kwargs["deadline_seconds"] + kwargs["grace_seconds"] <= 1
    assert all(h.collections == h.exits == 1 for h in helpers)


@pytest.mark.parametrize("operation", [0, 1])
@pytest.mark.parametrize(
    "outcome", [StreamOutcome.ABORTED, StreamOutcome.READ_FAILED, StreamOutcome.PENDING]
)
def test_partial_helper_observation_never_guesses_total(operation, outcome):
    helpers = [Helper(), Helper()]
    helpers[operation].data = b"partial\xff"
    helpers[operation].outcome = outcome
    action, _ = action_case(helpers)
    record = action.operations[operation]
    assert record.failure is not None
    summary = record.collector.summaries[ProcessStream.STDOUT]
    assert summary.observed_bytes == 8 and summary.retained_bytes == 8
    assert summary.outcome is outcome
    assert not summary.truncated
    assert action.confirmed_absent is (operation == 0)


@pytest.mark.parametrize("operation", [0, 1])
@pytest.mark.parametrize("kind", list(FailureKind))
def test_helper_failure_identity_kind_rebased_and_no_retry(operation, kind):
    error = (
        KeyboardInterrupt("control")
        if kind is FailureKind.INTERRUPTION
        else OSError("injected")
    )
    info = FailureInfo(error, kind, FailurePhase.RELEASE, ProcessStream.STDERR)
    helpers = [Helper(), Helper()]
    helpers[operation].failure = info
    action, calls = action_case(helpers)
    failures = action.observe().failures
    assert any(
        f.error is error
        and f.kind is kind
        and f.phase is FailurePhase.ACTION
        and f.stream is None
        for f in failures
    )
    assert helpers[operation].failure is info
    before = len(calls)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: action.cancel(time.monotonic() + 100), range(8)))
    for _ in range(3):
        action.start(StopContext("repeat", None, time.monotonic() + 100))
        action.observe()
    assert len(calls) == before
    assert all(h.collections <= 1 and h.exits <= 1 for h in helpers)
    assert not action.confirmed_absent if operation == 1 else True


@pytest.mark.parametrize("operation", [0, 1])
def test_collector_failure_survives_zero_status_and_close_failure(
    monkeypatch, operation
):
    module = api()
    from footman.container import InfrastructureCollector

    error = OSError("collector")
    close = OSError("close")
    original = InfrastructureCollector._retain
    helpers = [Helper(), Helper()]
    helpers[operation].data = b"bytes"
    helpers[operation].failure = FailureInfo(
        close, FailureKind.ORDINARY, FailurePhase.RELEASE
    )
    action = module.ContainerCleanup(ID, launch=lambda *a, **k: helpers.pop(0))
    selected = action.operations[operation].collector

    def retain(self, summary, data):
        if self is selected:
            raise error
        original(self, summary, data)

    monkeypatch.setattr(InfrastructureCollector, "_retain", retain)
    action.prepare()
    action.start(StopContext("test", 0, time.monotonic() + 1))
    end = time.monotonic() + 2
    while not action.observe().done and time.monotonic() < end:
        time.sleep(0.001)
    assert action.observe().done
    errors = [f.error for f in action.observe().failures]
    assert error in errors and close in errors
    assert selected.failure is error
    assert action.operations[operation].failure is error


@pytest.mark.parametrize("operation", [0, 1])
def test_helper_launch_failure_has_no_guessed_confirmation(operation):
    module = api()
    calls = []
    error = OSError("transport failed before launch")

    def launch(argv, **kwargs):
        index = len(calls)
        calls.append(tuple(argv))
        if index == operation:
            raise error
        return Helper()

    action = module.ContainerCleanup(ID, launch=launch)
    action.prepare()
    action.start(StopContext("test", 0, time.monotonic() + 1))
    end = time.monotonic() + 2
    while not action.observe().done and time.monotonic() < end:
        time.sleep(0.001)
    assert action.observe().done
    assert any(f.error is error for f in action.observe().failures)
    assert action.confirmed_absent is (operation == 0)
    assert len(calls) == 2


def test_supervisor_cleanup_observation_retains_local_failures(source):
    from types import SimpleNamespace

    module = api()
    error = OSError("startup stream release")
    info = FailureInfo(
        error, FailureKind.ORDINARY, FailurePhase.RELEASE, ProcessStream.STDOUT
    )
    from footman.process import ActionResult
    from tests.launcher_case_support import config

    run = module.RunSupervisor(config(), source)
    run.execution = SimpleNamespace(cleanup_status=(LifecycleResult(True, (info,)),))
    run.cleanup = SimpleNamespace(observe=lambda: ActionResult(True, True))
    assert run.cleanup_status.complete
    assert run.cleanup_status.failures == (info,)
    assert run.wait_cleanup(0).failures == (info,)


@pytest.mark.parametrize("operation", [0, 1])
def test_nonzero_operation_precedes_sole_context_close_failure(operation):
    module = api()
    close = OSError("close after nonzero helper")

    class NonzeroClose(Helper):
        def __exit__(self, *args):
            super().__exit__(*args)
            raise close

    helpers = [Helper(), Helper()]
    helpers[operation] = NonzeroClose(code=1)
    action, calls = action_case(helpers)
    failure = action.operations[operation].failure
    assert isinstance(failure, module.ContainerError)
    assert "nonzero" in str(failure)
    assert any(f.error is close for f in action.observe().failures)
    assert len(calls) == 2
    assert action.confirmed_absent is (operation == 0)


def test_diagnostic_rendering_failure_cannot_suppress_confirmation():
    defect = RuntimeError("broken diagnostic rendering")

    class BadText(OSError):
        def __str__(self):
            raise defect

    operation = BadText()
    info = FailureInfo(operation, FailureKind.ORDINARY, FailurePhase.RELEASE)
    action, calls = action_case([Helper(failure=info), Helper()])
    assert len(calls) == 2
    assert action.confirmed_absent
    failures = action.observe().failures
    assert failures[0].error is defect
    assert any(f.error is operation for f in failures)
    assert action.operations[0].failure is operation


def test_action_continuation_start_failure_is_terminal_and_retained(monkeypatch):
    module = api()
    error = RuntimeError("thread unavailable")
    calls = []
    action = module.ContainerCleanup(ID, launch=lambda *a, **k: calls.append(a))

    def unavailable():
        raise error

    monkeypatch.setattr(action._thread, "start", unavailable)
    action.prepare()
    action.start(StopContext("test", 0, time.monotonic() + 1))
    observation = action.observe()
    assert observation.done and observation.cleanup_complete
    assert observation.failures[0].error is error
    assert observation.failures[0].kind is FailureKind.UNEXPECTED
    action.start(StopContext("repeat", 0, time.monotonic() + 100))
    action.cancel(time.monotonic() + 100)
    assert calls == [] and not action.confirmed_absent


def test_cancel_prepared_idle_action_settles_without_renewal():
    module = api()
    calls = []
    action = module.ContainerCleanup(ID, launch=lambda *a, **k: calls.append(a))
    action.prepare()
    action.cancel(time.monotonic() - 1)
    end = time.monotonic() + 1
    while not action.observe().done and time.monotonic() < end:
        time.sleep(0.001)
    assert action.observe().done and action.observe().cleanup_complete
    action.start(StopContext("late", 0, time.monotonic() + 100))
    assert calls == [] and not action.confirmed_absent


@pytest.mark.parametrize("operation", [0, 1])
def test_collection_interruption_preserves_exact_object(operation):
    control = KeyboardInterrupt("helper interrupted")
    helpers = [Helper(), Helper()]
    helpers[operation].interruption = control
    action, _ = action_case(helpers)
    assert action.observe().failures[0].error is control
    assert action.observe().failures[0].kind is FailureKind.INTERRUPTION
    assert all(h.collections == h.exits == 1 for h in helpers)
    assert action.confirmed_absent is (operation == 0)


def test_exhausted_action_never_acquires_or_renews_budget():
    module = api()
    calls = []
    action = module.ContainerCleanup(ID, launch=lambda *a, **k: calls.append(a))
    action.prepare()
    action.start(StopContext("expired", None, time.monotonic() - 1))
    end = time.monotonic() + 1
    while not action.observe().done and time.monotonic() < end:
        time.sleep(0.001)
    assert action.observe().done and action.observe().cleanup_complete
    for _ in range(3):
        action.start(StopContext("new", None, time.monotonic() + 100))
        action.cancel(time.monotonic() + 100)
        assert action.observe().done
    assert calls == [] and not action.confirmed_absent


@pytest.mark.parametrize("operation", [0, 1])
def test_context_exit_cannot_mask_domain_collector_failure(monkeypatch, operation):
    module = api()
    from footman.container import InfrastructureCollector

    domain = OSError("domain retention")
    release = OSError("context exit")

    class ExitFailure(Helper):
        def __exit__(self, *args):
            super().__exit__(*args)
            raise release

    helpers = [Helper(), Helper()]
    helpers[operation] = ExitFailure(b"data")
    action = module.ContainerCleanup(ID, launch=lambda *a, **k: helpers.pop(0))
    collector = action.operations[operation].collector
    original = InfrastructureCollector._retain

    def retain(self, summary, data):
        if self is collector:
            raise domain
        original(self, summary, data)

    monkeypatch.setattr(InfrastructureCollector, "_retain", retain)
    action.prepare()
    action.start(StopContext("test", 0, time.monotonic() + 1))
    end = time.monotonic() + 2
    while not action.observe().done and time.monotonic() < end:
        time.sleep(0.001)
    result = action.observe()
    assert result.done
    assert action.operations[operation].failure is domain
    assert result.failures[0].error is domain
    assert any(f.error is release for f in result.failures)


def test_done_does_not_mean_helper_settlement():
    helpers = [Helper(), Helper(complete=False)]
    action, calls = action_case(helpers)
    assert action.observe().done
    assert not action.observe().cleanup_complete
    assert not action.confirmed_absent
    before = list(calls)
    helpers[1].complete = True
    assert action.observe().cleanup_complete
    assert (
        not action.confirmed_absent
    )  # late settlement cannot upgrade invalid collection
    assert calls == before


@pytest.mark.parametrize(
    "fault", ["permission", "owner", "interrupt", "close", "operation-close"]
)
def test_dev_snapshot_permission_retention(tmp_path, source, monkeypatch, fault):
    api()
    from footman.filesystem.descriptors import DirectoryDescriptor

    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    write_bytes(sentinel / "keep", b"untouched")
    os.symlink(sentinel, source.path / "sentinel-link")
    error = (
        KeyboardInterrupt("interrupted")
        if fault == "interrupt"
        else PermissionError(errno.EACCES, "denied")
    )
    close_error = OSError("close")
    released = []
    original_close = DirectoryDescriptor.close
    original_list = DirectoryDescriptor.list_names
    original_stat = DirectoryDescriptor.stat_child

    def list_names(self):
        if capability_path(self) == source.path and fault in (
            "permission",
            "interrupt",
            "operation-close",
        ):
            raise error
        return original_list(self)

    def stat_child(self, name, **kwargs):
        result = original_stat(self, name, **kwargs)
        if capability_path(self) == source.path and fault == "owner":
            fields = list(result)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return result

    def close(self):
        path = None if self.released else capability_path(self)
        if not self.released:
            released.append(self.fd)
        original_close(self)
        if (
            path is not None
            and path.parent == source.path
            and fault in ("close", "operation-close")
        ):
            raise close_error

    monkeypatch.setattr(DirectoryDescriptor, "list_names", list_names)
    monkeypatch.setattr(DirectoryDescriptor, "stat_child", stat_child)
    monkeypatch.setattr(DirectoryDescriptor, "close", close)
    if fault in ("close", "operation-close"):
        (source.path / "directory").mkdir()
    with pytest.raises(BaseException) as caught:
        source.remove()
    if fault in ("interrupt", "permission", "operation-close"):
        assert caught.value is error
    elif fault == "close":
        assert caught.value is close_error
    assert source.path.exists()
    assert read_bytes(sentinel / "keep") == b"untouched"
    before = list(released)
    source.remove()  # once-only failed attempt, not a blind retry
    assert released == before


def capability_path(capability):
    from pathlib import Path

    return Path(os.readlink(f"/proc/self/fd/{capability.fd}"))


def test_snapshot_cleanup_consumes_only_remaining_allowance(source, monkeypatch):
    api()
    from footman.filesystem.descriptors import DirectoryDescriptor

    original = DirectoryDescriptor.list_names

    def slow_listing(self):
        names = original(self)
        time.sleep(0.03)
        return names

    monkeypatch.setattr(DirectoryDescriptor, "list_names", slow_listing)
    with pytest.raises(Exception, match="allowance"):
        source.remove(deadline=time.monotonic() + 0.01)
    assert read_bytes(source.path / "payload") == b"staged\n"
    assert source.removal_failure is not None
    source.remove()
    assert source.path.exists()


def test_snapshot_move_fails_without_mutating_sentinel(tmp_path, source):
    api()
    moved = tmp_path / "moved-snapshot"
    source.path.rename(moved)
    try:
        with pytest.raises(Exception, match="moved"):
            source.remove()
        assert moved.is_dir()
        assert read_bytes(moved / "payload") == b"staged\n"
        assert source.removal_failure is not None
    finally:
        moved.rename(source.path)


def test_snapshot_concurrent_cleanup_is_one_attempt(source, monkeypatch):
    api()
    from footman.filesystem.descriptors import DirectoryDescriptor

    child = source.path / "concurrent-child"
    child.mkdir()
    opens = []
    original = DirectoryDescriptor.open_directory

    def opening(self, name, **kwargs):
        if name == "concurrent-child":
            opens.append(name)
        return original(self, name, **kwargs)

    monkeypatch.setattr(DirectoryDescriptor, "open_directory", opening)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: source.remove(), range(8)))
    assert opens == ["concurrent-child"]
    assert not source.path.exists()


def test_snapshot_cleanup_operation_plus_close_preserves_authority(source, monkeypatch):
    api()
    from footman.filesystem.descriptors import DirectoryDescriptor

    child = source.path / "directory"
    child.mkdir()
    operation = OSError("list failed")
    close = OSError("close failed")
    original_list = DirectoryDescriptor.list_names
    original_close = DirectoryDescriptor.close
    releases = []

    def listing(self):
        if capability_path(self) == child:
            raise operation
        return original_list(self)

    def closing(self):
        selected = not self.released and capability_path(self) == child
        if selected:
            releases.append(self.fd)
        original_close(self)
        if selected:
            raise close

    monkeypatch.setattr(DirectoryDescriptor, "list_names", listing)
    monkeypatch.setattr(DirectoryDescriptor, "close", closing)
    with pytest.raises(OSError) as caught:
        source.remove()
    assert caught.value is operation
    assert close in getattr(operation, "_transaction_secondary", [])
    assert len(releases) == 1
    source.remove()
    assert len(releases) == 1
