"""Cleanup interruption and survivor identity contracts."""

import os
import threading

import pytest

from tests.clock_support import ManualClock, module_clock
from tests.dispatch_support import api, removal, run_case
from tests.dispatcher_harness import read_bytes, write_bytes
from tests.launcher_case_support import ID, config, source, steps
from tests.launcher_support import DockerSequence

__all__ = ["source"]


@pytest.mark.sleepy
@pytest.mark.parametrize("boundary", ["rm", "listing", "finalization"])
@pytest.mark.parametrize("prior_interruption", [False, True])
def test_supervisor_cleanup_interruption_retains_state_and_continuation(
    tmp_path, source, monkeypatch, boundary, prior_interruption
):
    module = api()
    control = KeyboardInterrupt("supervisor cleanup interrupted")
    active = threading.Event()
    directory = tmp_path / "interrupted-cleanup"
    directory.mkdir()
    script = steps(source)[:2]
    script[1]["status"] = 0 if prior_interruption else 125
    first_control = KeyboardInterrupt("dispatch interrupted before cleanup")
    authoritative = first_control if prior_interruption else control
    cleanup_steps = removal(rm={"status": 1})
    selected = 0 if boundary == "rm" else 1
    cleanup_steps[selected]["delay"] = 0.3
    sequence = DockerSequence(directory, [*script, *cleanup_steps])

    def launch(argv, **kwargs):
        owner = sequence.launch(argv, **kwargs)
        if list(argv) == cleanup_steps[selected]["argv"]:
            active.set()
        return owner

    run = module.RunSupervisor(config(cleanup_grace_seconds=4), source, launch=launch)
    if prior_interruption:
        original_inspect = run.execution._inspect

        def interrupted_inspect(operation, identity):
            original_inspect(operation, identity)
            raise first_control

        monkeypatch.setattr(run.execution, "_inspect", interrupted_inspect)
    continuation = run.cleanup
    original_wait = run.wait_cleanup
    original_observe = continuation.observe

    def interrupt_observation():
        raise control

    def interrupted_wait(timeout):
        assert active.wait(2), "cleanup helper did not start"
        if boundary == "finalization":
            status = original_wait(timeout)
            assert status.complete
            monkeypatch.setattr(continuation, "observe", interrupt_observation)
            return status
        assert not original_observe().done, "interruption must occur during cleanup"
        raise control

    monkeypatch.setattr(run, "wait_cleanup", interrupted_wait)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run.execute(lambda *_: None, lambda *_: None)
        assert caught.value is authoritative
        assert run.failure is authoritative
        assert run.unresolved_resources
        assert not run.snapshot_deleted and source.path.exists()
        assert run.cleanup is continuation
        secondary = getattr(authoritative, "_transaction_secondary", [])
        if prior_interruption:
            assert run.execution.failure is first_control
            assert any(error is control for error in secondary)
        else:
            assert any(error is run.execution.failure for error in secondary)
        if boundary != "rm":
            assert any(
                error is continuation.operations[0].failure for error in secondary
            )
    finally:
        monkeypatch.setattr(continuation, "observe", original_observe)
        monkeypatch.setattr(run, "wait_cleanup", original_wait)
        assert original_wait(2).complete
        sequence.assert_settled()
    assert continuation.confirmed_absent
    assert run.failure is authoritative and run.unresolved_resources
    assert source.path.exists()  # Late observation cannot restart finalization.
    before = list(sequence.parameters)
    owners = tuple(continuation.owners)
    deadline = continuation._deadline
    for _ in range(3):
        assert original_wait(0.01).complete
    with pytest.raises(RuntimeError, match="already attempted"):
        run.execute(lambda *_: None, lambda *_: None)
    assert sequence.parameters == before
    assert tuple(continuation.owners) == owners
    assert continuation._deadline == deadline
    assert len(sequence.calls()) == 4


@pytest.mark.sleepy
def test_dev_survivor_removal_order(tmp_path, source):
    script = [*steps(source), *removal()]
    script[4]["delay"] = 2
    # This is a real exec-timeout/removal-order test, not an aggregate
    # startup-deadline test. Four Python stub startups must not compete with
    # exec for 0.4 seconds of scheduler time. Only container admission time is
    # logical; child timers, cleanup and watchdogs remain real.
    import footman.container as container

    clock = ManualClock()
    directory = tmp_path / "stub"
    directory.mkdir()
    sequence = DockerSequence(directory, script)

    def launch(argv, **kwargs):
        if list(argv) == script[0]["argv"]:
            # Regression: preparation alone exceeds the former whole-run
            # budget, without spending logical policy time.
            threading.Event().wait(0.5)
        if list(argv) == script[3]["argv"]:
            clock.advance(9.6, label="copy admission before exec")
        return sequence.launch(argv, **kwargs)

    with module_clock(container, clock):
        run = api().RunSupervisor(
            config(timeout_seconds=10, cleanup_grace_seconds=2), source, launch=launch
        )
        run.execute(lambda *_: None, lambda *_: None)
    assert [options["deadline_seconds"] for _, options in sequence.parameters[:4]] == [
        10,
        10,
        10,
        10,
    ]
    assert sequence.parameters[4][1]["deadline_seconds"] == pytest.approx(0.4)
    assert [(entry.seconds, entry.label) for entry in clock.journal()] == [
        (9.6, "copy admission before exec")
    ]
    assert run.execution.command_result is not None
    assert run.execution.command_result.timed_out
    assert run.failure is not None
    assert run.cleanup.confirmed_absent
    assert run.snapshot_deleted
    assert not source.path.exists()
    assert len(sequence.calls()) == 7
    assert all(call["container_id"] == ID for call in sequence.calls()[-2:])
    sequence.assert_settled()
    before = list(sequence.parameters)
    for _ in range(3):
        assert run.wait_cleanup(0.1).complete
        assert run.cleanup_status.complete
    assert sequence.parameters == before


@pytest.mark.sleepy
def test_dev_cleanup_sentinel_identity(tmp_path, source):
    sentinel = tmp_path / "unrelated"
    sentinel.mkdir()
    write_bytes(sentinel / "keep", b"untouched")
    # Even an outward local link must be unlinked, never traversed by cleanup.
    os.symlink(sentinel, source.path / "outside")
    run, sequence = run_case(tmp_path, source)
    assert run.cleanup.confirmed_absent and run.snapshot_deleted
    assert run.failure is None
    assert read_bytes(sentinel / "keep") == b"untouched"
    assert all("c" * 64 not in p[0] for p in sequence.parameters)
    assert sequence.parameters[-2][0] == ["docker", "rm", "-f", ID]
    sequence.assert_settled()
