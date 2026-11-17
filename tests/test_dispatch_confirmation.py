"""Removal confirmation and snapshot-retention contracts."""

import pytest

from tests.clock_support import ManualClock
from tests.dispatch_deadline_support import ScriptedHelper
from tests.dispatch_support import api, removal, run_case
from tests.dispatcher_harness import write_bytes
from tests.launcher_case_support import ID, config, source, steps
from tests.launcher_support import DockerSequence

__all__ = ["source"]


@pytest.mark.parametrize(
    "listing",
    [
        {"stdout": (ID + "\n").encode()},
        {"stdout": b"\n"},
        {"stdout": b"unexpected"},
        {"stdout": (("c" * 64) + "\n").encode()},
        {"status": 1},
        {"status": 125, "stderr": b"transport unavailable"},
        {"delay": 2},
    ],
)
def test_unconfirmed_listing_retains_snapshot(tmp_path, source, listing):
    # Policy/retention is deterministic at the managed-result boundary, not a
    # claim that a cold Python interpreter starts within 0.5s. Real wire/record
    # checks below remain strict and independent of this short policy allowance.
    cfg = config(cleanup_grace_seconds=0.5)
    outcome = dict(listing)
    if "delay" in outcome:
        outcome["timed_out"] = True  # explicit synthetic timeout, no wall sleep
    script = [*steps(source), *removal(listing=outcome)]
    calls, helpers = [], []
    clock = ManualClock()

    def launch(argv, **options):
        index = len(calls)
        assert list(argv) == script[index]["argv"]
        calls.append((list(argv), options))
        helper = ScriptedHelper(script[index], clock)
        helpers.append(helper)
        return helper

    run = api().RunSupervisor(cfg, source, launch=launch)
    run.execute(lambda *_: None, lambda *_: None)
    assert not run.cleanup.confirmed_absent
    assert run.failure is not None
    assert not run.snapshot_deleted
    assert source.path.is_dir()
    assert len(calls) == 7
    assert [call[0] for call in calls][-2:] == [s["argv"] for s in removal()]
    for _, options in calls[-2:]:
        assert 0 < options["deadline_seconds"] + options["grace_seconds"] <= 0.5
    result = run.cleanup.operations[1].result
    assert result.returncode == listing.get("status", 0)
    assert result.stdout == listing.get("stdout", b"")
    assert result.stderr == listing.get("stderr", b"")
    assert result.timed_out == ("delay" in listing)
    for _ in range(2):
        assert run.wait_cleanup(1).complete
    assert len(calls) == 7
    assert all(helper.collections == helper.context_exits == 1 for helper in helpers)


@pytest.mark.parametrize(
    "listing",
    [
        {"stdout": (ID + "\n").encode()},
        {"stdout": b"\n"},
        {"stdout": b"unexpected"},
        {"stdout": (("c" * 64) + "\n").encode()},
        {"status": 1},
        {"status": 125, "stderr": b"transport unavailable"},
    ],
)
def test_unconfirmed_listing_wire_observation_requires_child_record(tmp_path, listing):
    # Separate actual argv/status/bytes/EOF evidence from short-window policy.
    script = removal(listing=listing)[1:]
    sequence = DockerSequence(tmp_path, script)
    with sequence.launch(script[0]["argv"], deadline_seconds=20) as owner:
        result = owner.capture()
    assert (
        not result.timed_out
        and result.lifecycle.complete
        and not result.lifecycle.failures
    )
    sequence.assert_settled()  # a missing record is NEVER accepted here
    assert result.returncode == listing.get("status", 0)
    assert result.stdout == listing.get("stdout", b"")
    assert result.stderr == listing.get("stderr", b"")


def test_settled_real_child_without_record_is_not_wire_evidence(tmp_path):
    script = removal()[1:]
    sequence = DockerSequence(tmp_path, script)
    # Deterministic counterexample: an actual settled child never ran the stub.
    # No Python startup race, delay/sleep, guessed record or fabricated EOF.
    write_bytes(sequence.stub.executable, b"#!/bin/sh\nexit 0\n", mode=0o700)
    with sequence.launch(script[0]["argv"], deadline_seconds=20) as owner:
        result = owner.capture()
    assert result.returncode == 0 and not result.timed_out
    sequence.assert_owners_settled()
    with pytest.raises(FileNotFoundError):
        sequence.assert_records()
    with pytest.raises(FileNotFoundError):
        sequence.assert_settled()


def test_rm_failure_does_not_suppress_confirmation(tmp_path, source):
    run, sequence = run_case(
        tmp_path,
        source,
        script=[*steps(source), *removal(rm={"status": 1, "stderr": b"rm failed"})],
    )
    assert run.cleanup.confirmed_absent
    assert run.failure is not None
    assert run.snapshot_deleted
    assert run.cleanup.operations[0].result.returncode == 1
    assert run.cleanup.operations[1].result.returncode == 0
    from footman.process import FailurePhase

    failures = run.execution.command_result.lifecycle.failures
    for info in run.cleanup.observe().failures:
        assert any(
            f.error is info.error
            and f.kind is info.kind
            and f.phase is FailurePhase.ACTION
            and f.stream is None
            for f in failures
        )
    sequence.assert_settled()


def test_snapshot_permission_failure_after_confirmation_is_unsuccessful(
    tmp_path, source, monkeypatch
):
    from footman.snapshot import SnapshotReservation

    error = PermissionError("injected host EACCES")
    removed = []

    def remove(self, **kwargs):
        removed.append(self.path)
        raise error

    monkeypatch.setattr(SnapshotReservation, "remove", remove)
    run, sequence = run_case(tmp_path, source)
    assert run.cleanup.confirmed_absent
    assert run.failure is error
    assert not run.snapshot_deleted and source.path.exists()
    assert removed == [source.path]
    for _ in range(3):
        run.wait_cleanup(0.01)
    assert removed == [source.path]
    sequence.assert_settled()


def test_prior_snapshot_removal_failure_cannot_be_upgraded(
    tmp_path, source, monkeypatch
):
    from footman.filesystem.descriptors import DirectoryDescriptor

    failure = PermissionError("snapshot deletion denied")
    original = DirectoryDescriptor.list_names

    def denied(self):
        raise failure

    monkeypatch.setattr(DirectoryDescriptor, "list_names", denied)
    with pytest.raises(PermissionError):
        source.remove()
    monkeypatch.setattr(DirectoryDescriptor, "list_names", original)
    run, sequence = run_case(tmp_path, source)
    assert run.failure is failure
    assert not run.snapshot_deleted and source.path.exists()
    sequence.assert_settled()


@pytest.mark.parametrize(
    "startup",
    [
        {"status": 1},
        {"stdout": b"not an id\n"},
        {"delay": 2},
    ],
)
def test_unknown_identity_never_guesses_removal(tmp_path, source, startup):
    script = [{**steps(source)[0], "stdout": b"", **startup}]
    run, sequence = run_case(
        tmp_path, source, script=script, cfg=config(timeout_seconds=0.2)
    )
    assert run.execution.container_id is None
    assert run.failure is not None
    assert run.unresolved_resources
    assert source.path.exists()
    assert len(sequence.parameters) == 1
    assert run.wait_cleanup(1).complete
    sequence.assert_settled()
