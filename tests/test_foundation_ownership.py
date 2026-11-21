"""Fault injection for new harness file/process branches, not owner substitutes."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from footman.filesystem.operations import PosixDescriptorOps
from tests import dispatcher_harness as harness


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("fault", ["operation-close", "close", "interrupt", "success"])
def test_fixture_file_authority(tmp_path, monkeypatch, operation, fault):
    path = tmp_path / "bytes"
    harness.write_bytes(path, b"original")
    owners = []
    closes = []
    original_owner = harness.OwnedDescriptor
    original_operation = getattr(os, operation)
    primary = (
        KeyboardInterrupt("fixture interrupted")
        if fault == "interrupt"
        else OSError("operation")
    )
    close_error = OSError("close")

    class Ops(PosixDescriptorOps):
        def close(self, fd):
            closes.append(fd)
            super().close(fd)
            if fault in {"operation-close", "close"}:
                raise close_error

    def adopt(ops, fd, *, label):
        owner = original_owner(ops, fd, label=label)
        owners.append(owner)
        return owner

    def act(*args):
        if fault in {"operation-close", "interrupt"}:
            raise primary
        return original_operation(*args)

    monkeypatch.setattr(harness, "PosixDescriptorOps", Ops)
    monkeypatch.setattr(harness, "OwnedDescriptor", adopt)
    monkeypatch.setattr(os, operation, act)
    expected = primary if fault in {"operation-close", "interrupt"} else close_error
    if fault == "success":
        if operation == "read":
            assert harness.read_bytes(path) == b"original"
        else:
            harness.write_bytes(path, b"replacement")
    else:
        with pytest.raises(BaseException) as caught:
            if operation == "read":
                harness.read_bytes(path)
            else:
                harness.write_bytes(path, b"replacement")
        assert caught.value is expected
    assert len(owners) == len(closes) == 1
    owners[0].close()
    assert len(closes) == 1


def test_stub_concurrent_recorded_identities(tmp_path):
    stub = harness.DockerStub.create(tmp_path)
    sentinel = tmp_path / "unrelated"
    harness.write_bytes(sentinel, b"preserve")
    ids = [f"{i:064x}" for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda identity: stub.invoke(["rm", "-f", identity]), ids)
        )
    assert all(result.returncode == 0 for result in results)
    assert {call["container_id"] for call in stub.calls()} == set(ids)
    assert harness.read_bytes(sentinel) == b"preserve"


def test_helper_failed_child_and_repeat_cleanup(tmp_path, monkeypatch):
    owners = []
    launch = harness.OwnedProcess.launch

    def tracked(*args, **kwargs):
        owner = launch(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(harness.OwnedProcess, "launch", tracked)
    with pytest.raises(AssertionError, match="helper failed"):
        harness.run([sys.executable, "-c", "raise SystemExit(19)"], cwd=tmp_path)
    assert owners[0].cleanup_status.complete
    assert owners[0].shutdown().complete
    assert owners[0].wait_cleanup(0).complete


@pytest.mark.parametrize("operation_failure", [False, True])
def test_helper_operation_and_independent_close_failures(
    tmp_path, monkeypatch, operation_failure
):
    from tests.test_process_lifecycle import Child

    primary = OSError("read")
    close_errors = (OSError("stdout close"), OSError("stderr close"))
    child = Child(errors=close_errors)
    child.returncode = 0
    if operation_failure:

        def fail_read(size):
            raise primary

        child.stdout.read = fail_read
    launch = harness.OwnedProcess.launch
    owners = []

    def injected(*args, **kwargs):
        owner = launch(*args, factory=lambda *a, **k: child, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(harness.OwnedProcess, "launch", injected)
    with pytest.raises(OSError) as caught:
        harness.run(["fixture-child"], cwd=tmp_path)
    if operation_failure:
        assert caught.value is primary
    else:
        assert caught.value in close_errors
    assert child.stdout.attempts == child.stderr.attempts == 1
    owners[0].shutdown()
    owners[0].wait_cleanup(0)
    assert child.stdout.attempts == child.stderr.attempts == 1


def test_helper_retains_same_owner_and_result_on_incomplete_cleanup(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from footman.process import LifecycleResult

    owners = []
    launch = harness.OwnedProcess.launch
    capture = harness.OwnedProcess.capture

    def tracked(*args, **kwargs):
        owner = launch(*args, **kwargs)
        owners.append(owner)
        return owner

    def incomplete(self):
        return replace(capture(self), lifecycle=LifecycleResult(complete=False))

    monkeypatch.setattr(harness.OwnedProcess, "launch", tracked)
    monkeypatch.setattr(harness.OwnedProcess, "capture", incomplete)
    with pytest.raises(AssertionError) as caught:
        harness.run([sys.executable, "-c", "pass"], cwd=tmp_path)
    assert caught.value.cleanup is owners[0]
    assert not caught.value.process_result.lifecycle.complete
    assert caught.value.cleanup.wait_cleanup(0).complete
    assert len(owners) == 1


@pytest.mark.sleepy
def test_stub_deadline_retains_settled_owner_without_restarting_work(
    tmp_path, monkeypatch
):
    stub = harness.DockerStub.create(tmp_path)
    owners = []
    launch = harness.OwnedProcess.launch

    def tracked(*args, **kwargs):
        assert kwargs["deadline_seconds"] == 0.1
        owner = launch(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(harness.OwnedProcess, "launch", tracked)
    # invoke uses check=False: a timeout must still raise at the lifecycle
    # boundary rather than return a seemingly completed scripted operation.
    with pytest.raises(AssertionError, match="helper lifecycle failed") as caught:
        stub.invoke(
            ["rm", "a" * 64],
            delay=5,
            deadline_seconds=0.1,
            stdout=b"scripted operation completed",
        )
    result = caught.value.process_result
    owner = caught.value.cleanup
    assert result.timed_out
    assert result.lifecycle.complete
    assert result.lifecycle.failures == ()
    assert result.stdout == b""
    assert result.returncode is not None and result.returncode != 0
    assert len(owners) == 1
    assert owner is owners[0]
    assert len(stub.records) == 1
    # A deadline may fire before Python reaches the record write on a busy
    # machine. Neither an invocation record nor its absence proves completion.
    record_exists = stub.records[0].is_file()
    calls = stub.calls() if record_exists else []
    if record_exists:
        assert calls == [{"argv": ["rm", "a" * 64], "container_id": "a" * 64}]

    def forbidden(*args, **kwargs):
        raise AssertionError("cleanup observation restarted work or a budget")

    monkeypatch.setattr(harness.OwnedProcess, "launch", forbidden)
    monkeypatch.setattr(harness.OwnedProcess, "shutdown", forbidden)
    assert owner.cleanup_status.complete
    assert owner.wait_cleanup(0).complete
    assert owner.wait_cleanup(0.1).complete
    assert owner.cleanup_status.complete
    assert caught.value.cleanup is owner
    assert caught.value.process_result is result
    assert len(owners) == len(stub.records) == 1
    assert stub.records[0].is_file() == record_exists
    assert (stub.calls() if record_exists else []) == calls


@pytest.mark.sleepy
def test_helper_interruption_settles_owner(tmp_path, monkeypatch):
    owners = []
    launch = harness.OwnedProcess.launch
    control = KeyboardInterrupt("fixture capture")

    def tracked(*args, **kwargs):
        owner = launch(*args, **kwargs)
        owners.append(owner)
        return owner

    def interrupt(self):
        raise control

    monkeypatch.setattr(harness.OwnedProcess, "launch", tracked)
    monkeypatch.setattr(harness.OwnedProcess, "capture", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        harness.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path)
    assert caught.value is control
    assert owners[0].wait_cleanup(1).complete
    assert owners[0].shutdown().complete
