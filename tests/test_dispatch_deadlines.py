"""Shared deadlines and independent request ownership contracts."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.dispatch_support import api, removal, run_case
from tests.launcher_case_support import ID, config, source, steps
from tests.launcher_support import DockerSequence

__all__ = ["source"]


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
