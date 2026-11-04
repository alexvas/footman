"""Phase 6 Docker-free supervision and recorded-identity cleanup contracts."""

import importlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.dispatcher_harness import read_bytes, write_bytes
from tests.launcher_support import DockerSequence
from tests.test_launcher_execution import ID, config, source, steps

# Re-export the real staged-source fixture for this module's isolated requests.
__all__ = ["source"]


def api():
    import footman

    assert (Path(footman.__file__).parent / "supervision.py").is_file(), (
        "required bounded run supervision and recorded-ID cleanup are absent"
    )
    return importlib.import_module("footman.supervision")


def removal(identity=ID, **outcomes):
    return [
        {
            "argv": ["docker", "rm", "-f", identity],
            "container_id": identity,
            **outcomes.get("rm", {}),
        },
        {
            "argv": [
                "docker",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "id=" + identity,
                "--format",
                "{{.ID}}",
            ],
            "container_id": identity,
            **outcomes.get("listing", {}),
        },
    ]


def run_case(tmp_path, source, *, script=None, cfg=None):
    cfg = cfg or config()
    stub_directory = tmp_path / "stub"
    stub_directory.mkdir()
    sequence = DockerSequence(
        stub_directory, script or [*steps(source, cfg), *removal()]
    )
    run = api().RunSupervisor(cfg, source, launch=sequence.launch)
    run.execute(lambda *_: None, lambda *_: None)
    return run, sequence


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


def test_dev_survivor_removal_order(tmp_path, source):
    script = [*steps(source), *removal()]
    script[4]["delay"] = 2
    run, sequence = run_case(
        tmp_path,
        source,
        script=script,
        cfg=config(timeout_seconds=0.4, cleanup_grace_seconds=2),
    )
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


@pytest.mark.parametrize("position", [1, 2, 3])
def test_shared_deadline_suppresses_downstream_work(tmp_path, source, position):
    script = steps(source)[: position + 1]
    script[position]["delay"] = 2
    script.extend(removal())
    start = time.monotonic()
    run, sequence = run_case(
        tmp_path,
        source,
        script=script,
        cfg=config(timeout_seconds=0.3, cleanup_grace_seconds=1),
    )
    assert time.monotonic() - start < 2
    assert run.failure is not None
    assert run.cleanup.confirmed_absent
    assert len(sequence.parameters) == position + 3
    sequence.assert_settled()


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
def test_nonzero_terminal_path_removes_only_recorded_id(tmp_path, source, stage):
    script = steps(source)[: stage + 1]
    script[stage]["status"] = 125
    script.extend(removal())
    run, sequence = run_case(tmp_path, source, script=script)
    assert run.cleanup.confirmed_absent and run.snapshot_deleted
    if stage < 4:
        assert run.failure is not None
        assert run.execution.command_result is None
    else:
        assert run.execution.command_result.returncode == 125
    assert len(sequence.calls()) == stage + 3
    sequence.assert_settled()


def test_cumulative_deadline_not_fresh_per_child(tmp_path, source):
    script = steps(source)[:3]
    script[0]["delay"] = 0.15
    script[1]["delay"] = 0.15
    script[2]["delay"] = 2
    script.extend(removal())
    run, sequence = run_case(
        tmp_path, source, script=script, cfg=config(timeout_seconds=0.5)
    )
    deadlines = [options["deadline_seconds"] for _, options in sequence.parameters[:3]]
    assert deadlines[0] > deadlines[1] > deadlines[2]
    assert deadlines[2] < 0.2
    assert run.failure is not None and run.cleanup.confirmed_absent
    sequence.assert_settled()


@pytest.mark.parametrize("stage", [0, 4])
def test_interruption_retains_authority_and_known_identity_cleanup(
    tmp_path, source, stage
):
    module = api()
    control = KeyboardInterrupt("caller")
    script = steps(source)[: stage + 1]
    if stage:
        script.extend(removal())
    directory = tmp_path / "interrupt-stub"
    directory.mkdir()
    sequence = DockerSequence(directory, script)
    run = module.RunSupervisor(config(), source, launch=sequence.launch)

    def interrupt(*args):
        raise control

    # Supply data so the domain callback is actually entered.
    if stage:
        script[stage]["stdout"] = b"ready"
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute(
            interrupt if stage == 0 else lambda *_: None,
            interrupt if stage else lambda *_: None,
        )
    assert caught.value is control
    assert run.failure is control
    assert run.wait_cleanup(2).complete
    if stage:
        assert run.cleanup.confirmed_absent and run.snapshot_deleted
    else:
        assert run.unresolved_resources and source.path.exists()
    sequence.assert_settled()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_cleanup_observation_requires_bounded_timeout(source, timeout):
    run = api().RunSupervisor(config(), source)
    with pytest.raises(ValueError):
        run.wait_cleanup(timeout)


def test_command_setup_failure_still_removes_known_container(tmp_path, source):
    from footman.process import OwnedProcess

    directory = tmp_path / "setup-stub"
    directory.mkdir()
    sequence = DockerSequence(directory, [*steps(source)[:4], *removal()])
    attempted = False

    def launch(argv, **kwargs):
        nonlocal attempted
        if len(sequence.parameters) == 4 and not attempted:
            attempted = True

            def unavailable(*args, **options):
                raise OSError("command client unavailable")

            return OwnedProcess.launch(argv, factory=unavailable, **kwargs)
        return sequence.launch(argv, **kwargs)

    run = api().RunSupervisor(config(), source, launch=launch)
    run.execute(lambda *_: None, lambda *_: None)
    assert attempted and run.failure is not None
    assert run.cleanup.confirmed_absent and run.snapshot_deleted
    assert run.cleanup_status.complete
    sequence.assert_settled()


def test_independent_requests_do_not_share_cleanup_state(tmp_path):
    from footman.snapshot import reserve_snapshot
    from tests.dispatcher_harness import GitFixture

    # Separate fixture-owned sequence records, no real or guessed containers.
    module = api()
    repo = GitFixture.create(tmp_path / "independent-repo")
    root = tmp_path / "independent-snapshots"
    with (
        reserve_snapshot(root, "0" * 26) as first,
        reserve_snapshot(root, "0" * 25 + "1") as second,
    ):
        snapshots = [first, second]
        sequences = []
        for i, snapshot in enumerate(snapshots):
            tree = snapshot.capture(repo.path)
            snapshot.export(tree)
            directory = tmp_path / str(i)
            directory.mkdir()
            identity = ("a" if i == 0 else "c") * 64
            script = steps(snapshot)
            for entry in script:
                entry["argv"] = [arg.replace(ID, identity) for arg in entry["argv"]]
                if "stdout" in entry:
                    entry["stdout"] = entry["stdout"].replace(
                        ID.encode(), identity.encode()
                    )
            sequences.append(DockerSequence(directory, [*script, *removal(identity)]))
        runs = [
            module.RunSupervisor(config(), snapshot, launch=sequence.launch)
            for snapshot, sequence in zip(snapshots, sequences, strict=True)
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda r: r.execute(lambda *_: None, lambda *_: None), runs))
        for run, sequence in zip(runs, sequences, strict=True):
            assert run.cleanup.confirmed_absent
            assert len(sequence.calls()) == 7
            assert run.cleanup_status.complete
            sequence.assert_settled()
        assert runs[0].cleanup.operations is not runs[1].cleanup.operations
        assert all(run.snapshot_deleted for run in runs)
        assert runs[0].execution.container_id != runs[1].execution.container_id
