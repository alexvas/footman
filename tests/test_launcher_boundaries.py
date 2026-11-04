"""Reconciliation identities and handoff boundary regressions."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from footman.process import LifecycleResult, ProcessResult, ProcessStream, StreamOutcome
from tests.launcher_support import DockerSequence
from tests.stdin_support import PAYLOAD, live_stdin
from tests.test_launcher_execution import ID, IMAGE, api, config, source, steps
from tests.test_launcher_faults import PreparedOwner

__all__ = ["source"]


def test_helpers_cannot_read_live_parent_protocol_stdin(tmp_path):
    # The outer lifecycle is wholly encapsulated by subprocess.run. Its stdin
    # is borrowed from one owner; the FIFO writer remains open throughout.
    script = """
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from footman.container import ContainerExecution
from tests.launcher_support import DockerSequence
from tests.test_launcher_execution import config, steps
source = SimpleNamespace(path=Path('/snapshot'), copy_source=lambda: Path('/snapshot'))
with TemporaryDirectory() as directory:
    sequence = DockerSequence(Path(directory), steps(source))
    run = ContainerExecution(config(), source, launch=sequence.launch)
    run.execute(lambda *args: None, lambda *args: None)
    sequence.assert_settled()
    if len(sequence.calls()) != 5:
        raise AssertionError('not every Docker helper executed')
print('all five helpers received closed stdin')
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with live_stdin(tmp_path / "protocol.pipe") as owner:
        result = subprocess.run(
            [sys.executable, "-c", script],
            stdin=owner.fd,
            capture_output=True,
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"all five helpers received closed stdin\n"
        assert os.read(owner.fd, len(PAYLOAD)) == PAYLOAD


def test_sequence_preserves_process_launch_options(tmp_path):
    sequence = DockerSequence(tmp_path, [{"argv": ["docker", "run"]}])
    factory = Mock(wraps=subprocess.Popen)
    with sequence.launch(
        ["docker", "run"],
        factory=factory,
        popen_kwargs={"cwd": tmp_path, "env": {"FOOTMAN_TEST_OPTION": "preserved"}},
    ) as owner:
        result = owner.capture()
    assert result.returncode == 0
    options = factory.call_args.kwargs
    assert options["cwd"] == tmp_path
    assert options["env"]["FOOTMAN_TEST_OPTION"] == "preserved"
    assert "FOOTMAN_STUB_SCENARIO" in options["env"]
    assert options["stdin"] == subprocess.DEVNULL
    sequence.assert_settled()


@pytest.mark.parametrize(
    "primary", [None, OSError("operation"), KeyboardInterrupt("interrupt")]
)
def test_live_stdin_sole_owner_release_and_primary(tmp_path, monkeypatch, primary):
    from footman.filesystem.operations import PosixDescriptorOps

    release = PosixDescriptorOps.close
    close_error = OSError("close")
    calls = []

    def close(ops, fd):
        calls.append(fd)
        release(ops, fd)
        raise close_error

    monkeypatch.setattr(PosixDescriptorOps, "close", close)
    owner = None
    borrowed = None
    with pytest.raises(BaseException) as caught:
        with live_stdin(tmp_path / "owned.pipe") as owner:
            borrowed = owner.fd
            if primary is not None:
                raise primary
    assert caught.value is (close_error if primary is None else primary)
    owner.close()
    owner.close()
    assert calls == [borrowed]


def test_startup_close_failure_preserves_attributable_identity():
    module = api()
    error = OSError("sole release failure")
    owner = PreparedOwner(error=error)
    owner.chunks = (ID + "\n").encode()
    snapshot = SimpleNamespace(copy_source=lambda: "/snapshot")
    run = module.ContainerExecution(config(), snapshot, launch=lambda *a, **kw: owner)
    with pytest.raises(OSError) as caught:
        run.execute(lambda *a: None, lambda *a: None)
    assert caught.value is error
    assert run.container_id == ID
    assert run.startup_result.returncode == 0
    assert run.command_result is None


def test_startup_sink_failure_does_not_stop_id_parser():
    module = api()
    error = OSError("startup output storage failed")

    class SplitOwner(PreparedOwner):
        def stream(self, chunk, *, on_stream_end):
            for data in (ID[:10].encode(), ID[10:].encode(), b"\n"):
                chunk(ProcessStream.STDOUT, data)
            for s in ProcessStream:
                on_stream_end(s, StreamOutcome.EOF)
            return ProcessResult(
                b"",
                b"",
                0,
                False,
                LifecycleResult(True),
                tuple((s, StreamOutcome.EOF) for s in ProcessStream),
            )

    def fail(*args):
        raise error

    run = module.ContainerExecution(
        config(),
        SimpleNamespace(copy_source=lambda: "/snapshot"),
        launch=lambda *a, **kw: SplitOwner(),
    )
    with pytest.raises(OSError) as caught:
        run.execute(fail, lambda *a: None)
    assert caught.value is error
    assert run.container_id == ID


def test_revalidate_source_after_mkdir_before_copy(tmp_path, source):
    module = api()
    seq = DockerSequence(tmp_path, steps(source))
    moved = source.path.with_name("moved")

    def launch(argv, **kwargs):
        owner = seq.launch(argv, **kwargs)
        if argv[1:3] == ("exec", "-u") and "mkdir" in argv:
            stream = owner.stream

            def mutate(*args, **kw):
                result = stream(*args, **kw)
                source.path.rename(moved)
                return result

            owner.stream = mutate
        return owner

    run = module.ContainerExecution(config(), source, launch=launch)
    with pytest.raises(Exception, match="moved"):
        run.execute(lambda *a: None, lambda *a: None)
    assert len(seq.calls()) == 3
    assert run.container_id == ID
    assert run.command_result is None
    seq.assert_settled()


def test_repeat_attempt_cannot_relaunch(tmp_path, source):
    module = api()
    seq = DockerSequence(tmp_path, steps(source))
    run = module.ContainerExecution(config(), source, launch=seq.launch)
    run.execute(lambda *a: None, lambda *a: None)
    with pytest.raises(RuntimeError, match="already attempted"):
        run.execute(lambda *a: None, lambda *a: None)
    assert len(seq.calls()) == 5
    seq.assert_settled()


def test_independent_sequences_concurrent_unique_identity(tmp_path, source):
    module = api()

    def request(letter):
        directory = tmp_path / letter
        directory.mkdir()
        identity = letter * 64
        script = steps(source)
        for step in script:
            step["argv"] = [arg.replace(ID, identity) for arg in step["argv"]]
        script[0]["stdout"] = (identity + "\n").encode()
        script[1]["stdout"] = f"{identity}|running|{IMAGE}".encode()
        seq = DockerSequence(directory, script)
        run = module.ContainerExecution(config(), source, launch=seq.launch)
        run.execute(lambda *a: None, lambda *a: None)
        seq.assert_settled()
        assert len(seq.calls()) == 5
        return run.container_id, set(seq.stub.records)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(request, ["a", "c"])
    assert first[0] != second[0]
    assert first[1].isdisjoint(second[1])


def test_sequenced_stub_rejects_wrong_order_and_records_actual_status(tmp_path):
    seq = DockerSequence(tmp_path, [{"argv": ["docker", "run"], "status": 0}])
    with seq.launch(["docker", "cp"], capture_limit=0) as owner:
        result = owner.stream(lambda *a: None)
    assert result.returncode == 97
    assert seq.calls()[0]["status"] == 97
    assert owner.cleanup_status.complete
    with pytest.raises(AssertionError, match="additional"):
        seq.launch(["docker", "run"])
    assert len(seq.owners) == 1


@pytest.mark.parametrize(
    "path, diagnostic", [("relative", "absolute"), ("/bad\x00path", "NUL")]
)
def test_invalid_copy_source_reports_specific_cause(source, path, diagnostic):
    from pathlib import Path

    from footman.snapshot import SnapshotError

    original = source.path
    try:
        source.path = Path(path)
        with pytest.raises(SnapshotError, match=diagnostic):
            source.copy_source()
    finally:
        source.path = original


def test_copy_source_requires_complete_live_export(tmp_path):
    from footman.snapshot import SnapshotError, reserve_snapshot
    from tests.test_launcher_execution import RUN_ID

    with reserve_snapshot(tmp_path / "storage", RUN_ID) as snapshot:
        with pytest.raises(SnapshotError, match="completed"):
            snapshot.copy_source()
