"""Adapter failure handoff and once-only collection, without raw child authority."""

from types import SimpleNamespace

import pytest

from footman.process import (
    FailureInfo,
    FailureKind,
    FailurePhase,
    LifecycleResult,
    ProcessResult,
    ProcessStream,
    StreamOutcome,
)
from tests.launcher_case_support import ID, IMAGE, api, config


class PreparedOwner:
    def __init__(self, *, error=None, complete=True, interrupt=None):
        self.error = error
        self.complete = complete
        self.interrupt = interrupt
        self.collections = 0
        self.exits = 0
        self.chunks = b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exits += 1

    @property
    def cleanup_status(self):
        return LifecycleResult(self.complete)

    def wait_cleanup(self, timeout):
        return self.cleanup_status

    def stream(self, chunk, *, on_stream_end):
        self.collections += 1
        chunk(ProcessStream.STDOUT, self.chunks)
        on_stream_end(ProcessStream.STDOUT, StreamOutcome.EOF)
        on_stream_end(ProcessStream.STDERR, StreamOutcome.EOF)
        if self.interrupt:
            raise self.interrupt
        failures = (
            ()
            if self.error is None
            else (
                FailureInfo(
                    self.error,
                    FailureKind.ORDINARY,
                    FailurePhase.RELEASE,
                    ProcessStream.STDERR,
                ),
            )
        )
        return ProcessResult(
            b"tail must not be used",
            b"tail",
            0,
            False,
            LifecycleResult(self.complete, failures),
            tuple((s, StreamOutcome.EOF) for s in ProcessStream),
        )


@pytest.mark.parametrize("stage", range(5))
@pytest.mark.parametrize(
    "kind", ["sole-close", "operation-close", "interrupt", "incomplete"]
)
def test_each_child_failure_retains_owner_and_primary(stage, kind):
    module = api()
    close_error = OSError("release failed")
    operation_error = OSError("domain sink failed")
    interruption = KeyboardInterrupt("caller")
    owners = []

    def launch(argv, **kwargs):
        index = len(owners)
        owner = PreparedOwner(
            error=close_error
            if index == stage and kind in ("sole-close", "operation-close")
            else None,
            complete=not (index == stage and kind == "incomplete"),
            interrupt=interruption if index == stage and kind == "interrupt" else None,
        )
        owner.chunks = (
            (ID + "\n").encode()
            if index == 0
            else (f"{ID}|running|{IMAGE}").encode()
            if index == 1
            else b"bytes"
        )
        owners.append(owner)
        return owner

    snapshot = SimpleNamespace(
        path=__import__("pathlib").Path("/snapshot"),
        copy_source=lambda: __import__("pathlib").Path("/snapshot"),
    )
    run = module.ContainerExecution(config(), snapshot, launch=launch)
    if kind == "operation-close":
        original = module.InfrastructureCollector._retain

        def fail(self, summary, data):
            if len(owners) - 1 == stage:
                raise operation_error
            return original(self, summary, data)

        # Startup/command use their supplied domain sink; infrastructure uses
        # the same collector retention boundary, independently of process tails.
        from unittest.mock import patch

        context = patch.object(module.InfrastructureCollector, "_retain", fail)
    else:
        from contextlib import nullcontext

        context = nullcontext()

    def sink(*args):
        if kind == "operation-close" and len(owners) - 1 == stage:
            raise operation_error

    with context:
        with pytest.raises(BaseException) as caught:
            run.execute(sink, sink)
    if kind == "interrupt":
        assert caught.value is interruption
    elif kind == "operation-close":
        assert caught.value is operation_error
    elif kind == "sole-close":
        assert caught.value is close_error
    else:
        assert isinstance(caught.value, module.ContainerError)
    assert len(owners) == stage + 1
    assert run.processes == owners
    assert all(p.collections == 1 and p.exits == 1 for p in owners)
    for _ in range(2):
        statuses = run.cleanup_status
        assert len(statuses) == len(owners)
    assert all(p.collections == 1 and p.exits == 1 for p in owners)


@pytest.mark.parametrize(
    "outcome", [StreamOutcome.READ_FAILED, StreamOutcome.ABORTED, StreamOutcome.PENDING]
)
def test_zero_exit_does_not_mask_partial_observation(outcome):
    module = api()
    collector = module.InfrastructureCollector()
    collector.chunk(ProcessStream.STDERR, b"partial\xff")
    collector.end(ProcessStream.STDERR, outcome)
    summary = collector.summaries[ProcessStream.STDERR]
    assert summary.observed_bytes == 8
    assert summary.retained_bytes == 8
    assert not summary.truncated
    assert summary.outcome is outcome


def test_collector_failure_is_explicit_and_continues_count(monkeypatch):
    module = api()
    collector = module.InfrastructureCollector()
    error = OSError("retention failed")

    def fail(*args):
        raise error

    monkeypatch.setattr(collector, "_retain", fail)
    collector.chunk(ProcessStream.STDERR, b"first")
    collector.chunk(ProcessStream.STDERR, b"second")
    assert collector.failure is error
    assert collector.summaries[ProcessStream.STDERR].observed_bytes == 11
    assert collector.summaries[ProcessStream.STDERR].retained_bytes == 0


def test_terminal_result_is_not_generic_tail():
    module = api()
    collector = module.InfrastructureCollector()
    collector.chunk(ProcessStream.STDOUT, b"leading")
    result = ProcessResult(
        b"wrong tail",
        b"other tail",
        0,
        False,
        LifecycleResult(True),
        ((ProcessStream.STDOUT, StreamOutcome.ABORTED),),
    )
    collector.finish(result)
    assert collector.summaries[ProcessStream.STDOUT].outcome is StreamOutcome.ABORTED
    assert collector.summaries[ProcessStream.STDERR].outcome is StreamOutcome.PENDING
    assert collector.summaries[ProcessStream.STDOUT].observed_bytes == 7


def test_unsupported_source_prevents_acquisition():
    module = api()
    from pathlib import Path

    calls = []
    for path in (Path("relative"), Path("/missing-source")):
        snapshot = SimpleNamespace(
            path=path,
            copy_source=lambda: (_ for _ in ()).throw(
                ValueError("invalid snapshot source")
            ),
        )
        run = module.ContainerExecution(
            config(), snapshot, launch=lambda *a, **kw: calls.append(a)
        )
        with pytest.raises(ValueError, match="invalid snapshot source"):
            run.execute(lambda *a: None, lambda *a: None)
    assert not calls


@pytest.mark.parametrize("status", [0, 127])
@pytest.mark.parametrize(
    "failure",
    ["sink", "collector", "timeout", "incomplete", "partial", "release", "exit"],
)
def test_rejected_command_retains_returned_result(monkeypatch, status, failure):
    module = api()
    error = OSError(f"command {failure} failed")
    outcomes = tuple(
        (stream, StreamOutcome.ABORTED if failure == "partial" else StreamOutcome.EOF)
        for stream in ProcessStream
    )
    returned = ProcessResult(
        b"command stdout",
        b"command stderr",
        status,
        failure == "timeout",
        LifecycleResult(
            failure != "incomplete",
            (
                FailureInfo(
                    error,
                    FailureKind.ORDINARY,
                    FailurePhase.RELEASE,
                    ProcessStream.STDERR,
                ),
            )
            if failure == "release"
            else (),
        ),
        outcomes,
    )
    observations_at_exit = []

    class CommandOwner(PreparedOwner):
        def stream(self, chunk, *, on_stream_end):
            super().stream(chunk, on_stream_end=on_stream_end)
            return returned

        def __exit__(self, *args):
            observations_at_exit.append(run.command_result)
            super().__exit__(*args)
            if failure == "exit":
                raise error

    owners = [PreparedOwner() for _ in range(4)] + [CommandOwner()]
    owners[0].chunks = (ID + "\n").encode()
    owners[1].chunks = f"{ID}|running|{IMAGE}".encode()
    owners[-1].chunks = b"command output"
    launched = []

    def launch(argv, **kwargs):
        owner = owners[len(launched)]
        launched.append(owner)
        return owner

    if failure == "collector":
        original = module.InfrastructureCollector._retain

        def retain(collector, summary, data):
            if len(launched) == 5:
                raise error
            return original(collector, summary, data)

        monkeypatch.setattr(module.InfrastructureCollector, "_retain", retain)

    def sink(*args):
        if failure == "sink" and len(launched) == 5:
            raise error

    snapshot = SimpleNamespace(copy_source=lambda: "/snapshot")
    run = module.ContainerExecution(config(), snapshot, launch=launch)
    # Intentionally use the SAME sink for startup and command: invocation
    # identity must be explicit, never guessed from the sink or argv.
    with pytest.raises(BaseException) as caught:
        run.execute(sink, sink)
    if failure in ("sink", "collector", "release", "exit"):
        assert caught.value is error
    else:
        assert isinstance(caught.value, module.ContainerError)
        assert str(caught.value) == "local child or stream observation incomplete"
    assert run.failure is caught.value
    assert observations_at_exit == [returned]
    assert run.command_result is returned
    assert run.command_result.returncode == status
    assert run.command_result.stream_outcomes == outcomes
    assert run.command_result.timed_out == (failure == "timeout")
    assert run.command_result.lifecycle.complete == (failure != "incomplete")
    for _ in range(2):
        assert len(run.cleanup_status) == 5
        for owner in owners:
            owner.wait_cleanup(0)
    with pytest.raises(RuntimeError, match="already attempted"):
        run.execute(sink, sink)
    assert run.failure is caught.value
    assert run.command_result is returned
    assert launched == owners
    assert all(owner.collections == 1 and owner.exits == 1 for owner in owners)


def test_command_collection_without_result_retains_none():
    module = api()
    interruption = KeyboardInterrupt("command collection interrupted")
    owners = [PreparedOwner() for _ in range(4)] + [
        PreparedOwner(interrupt=interruption)
    ]
    owners[0].chunks = (ID + "\n").encode()
    owners[1].chunks = f"{ID}|running|{IMAGE}".encode()
    launched = []

    def launch(argv, **kwargs):
        owner = owners[len(launched)]
        launched.append(owner)
        return owner

    run = module.ContainerExecution(
        config(),
        SimpleNamespace(copy_source=lambda: "/snapshot"),
        launch=launch,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute(lambda *args: None, lambda *args: None)
    assert caught.value is interruption
    assert run.failure is interruption
    assert run.command_result is None
    for _ in range(2):
        assert len(run.cleanup_status) == 5
        for owner in owners:
            owner.wait_cleanup(0)
    with pytest.raises(RuntimeError, match="already attempted"):
        run.execute(lambda *args: None, lambda *args: None)
    assert run.command_result is None
    assert launched == owners
    assert all(owner.collections == 1 and owner.exits == 1 for owner in owners)
