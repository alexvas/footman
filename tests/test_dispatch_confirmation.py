"""Removal confirmation and snapshot-retention contracts."""

import pytest

from tests.dispatch_support import removal, run_case
from tests.launcher_case_support import ID, config, source, steps

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
    run, sequence = run_case(
        tmp_path,
        source,
        script=[*steps(source), *removal(listing=listing)],
        cfg=config(cleanup_grace_seconds=0.5),
    )
    assert not run.cleanup.confirmed_absent
    assert run.failure is not None
    assert not run.snapshot_deleted
    assert source.path.is_dir()
    assert len(sequence.parameters) == 7
    assert [p[0] for p in sequence.parameters][-2:] == [s["argv"] for s in removal()]
    assert run.wait_cleanup(1).complete
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
