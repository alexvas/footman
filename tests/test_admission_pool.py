"""Public pool contracts; faults delegate real OS release through sole owners."""

import errno
import json
import os
import socket
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from footman.filesystem.descriptors import DescriptorError, UnsafeDescriptorError
from footman.filesystem.operations import PosixDescriptorOps
from footman.filesystem.streams import OwnedStream
from footman.process import (
    FailureInfo,
    FailureKind,
    FailurePhase,
    OwnedProcess,
    ProcessStream,
    StreamOutcome,
)
from tests import mcp_provenance, mcp_wheel_support
from tests.admission_support import (
    AdmissionPool,
    CapacityDecision,
    CapacityObservationUnavailable,
    PosixAdmissionSystem,
    PosixCapacitySystem,
    resolve_admission_capacity,
)


class RecordingDescriptors(PosixDescriptorOps):
    def __init__(self, directory, *, point=None, primary=None, release=None):
        self.directory_identity = (directory.stat().st_dev, directory.stat().st_ino)
        self.point = point
        self.primary = primary
        self.release = release
        self.parent_release = None
        self.events = []
        self.live = {}
        self.opened = []
        self.closed = []
        self.slot_names = []

    def openat(self, directory_fd, name, flags, mode=0):
        if self.point == "open" and directory_fd is None:
            assert self.primary is not None, "open fault requires a primary exception"
            raise self.primary
        fd = super().openat(directory_fd, name, flags, mode)
        # Observations borrow the fd: ownership stays with the actual consumer.
        info = os.fstat(fd)
        kind = (
            "slot"
            if stat.S_ISREG(info.st_mode)
            else "namespace"
            if (info.st_dev, info.st_ino) == self.directory_identity
            else "ancestor"
        )
        token = (len(self.opened), fd, kind)
        self.opened.append(token)
        if kind == "slot":
            self.slot_names.append(name)
        self.live[fd] = token
        self.events.append(("open", kind))
        return fd

    def fstat(self, fd):
        info = super().fstat(fd)
        if self.point == "metadata" and stat.S_ISREG(info.st_mode):
            assert self.primary is not None, (
                "metadata fault requires a primary exception"
            )
            raise self.primary
        return info

    def close(self, fd):
        token = self.live.pop(fd)
        super().close(fd)
        self.closed.append(token)
        kind = token[2]
        self.events.append(("close", kind))
        if kind == "slot" and self.release is not None:
            raise self.release
        if kind == "namespace" and self.parent_release is not None:
            raise self.parent_release


class RecordingSystem(PosixAdmissionSystem):
    def monotonic(self):
        ops = self.descriptors
        assert isinstance(ops, RecordingDescriptors)
        ops.events.append(("monotonic", "clock"))
        return super().monotonic()

    def sleep(self, seconds):
        ops = self.descriptors
        assert isinstance(ops, RecordingDescriptors)
        ops.events.append(("sleep", seconds))
        return super().sleep(seconds)

    def try_lock_exclusive(self, fd):
        ops = self.descriptors
        assert isinstance(ops, RecordingDescriptors)
        ops.events.append(("attempt", "slot"))
        if ops.point == "flock":
            assert ops.primary is not None, "flock fault requires a primary exception"
            raise ops.primary
        acquired = super().try_lock_exclusive(fd)
        ops.events.append(("acquired" if acquired else "busy", "slot"))
        return acquired


def assert_released(ops):
    assert not ops.live
    assert sorted(ops.closed) == sorted(ops.opened)
    assert len(set(ops.closed)) == len(ops.closed)


CASES = (
    ["namespace"]
    + [
        f"{point}:{fault}"
        for point in ("metadata", "flock", "body")
        for fault in ("operation-close", "sole-close", "interruption", "repeat")
    ]
    + [
        f"directory:{fault}"
        for fault in ("operation-close", "sole-close", "interruption", "repeat")
    ]
    + ["open:operation", "open:interruption"]
)


@pytest.mark.parametrize("case", CASES)
def test_pool_admits_and_releases(tmp_path, case):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ops = RecordingDescriptors(a)
    system = RecordingSystem(descriptors=ops)
    pool = AdmissionPool(directory=a, capacity=1, system=system)
    other_ops = RecordingDescriptors(b)
    other = AdmissionPool(
        directory=b, capacity=1, system=RecordingSystem(descriptors=other_ops)
    )
    assert ops.events == other_ops.events == []
    assert list(a.iterdir()) == list(b.iterdir()) == []
    with pytest.raises(FrozenInstanceError):
        # Deliberate mutation: prove the public frozen configuration rejects it.
        pool.capacity = 2  # ty: ignore[invalid-assignment]

    if case == "namespace":
        with pool.acquire(deadline=time.monotonic() + 5) as entered:
            assert entered is None
            ops.events.append(("entered", "body"))
            assert ("acquired", "slot") in ops.events
            assert any(token[2] == "slot" for token in ops.live.values())
            with other.acquire(deadline=time.monotonic() + 5) as second:
                assert second is None
                other_ops.events.append(("entered", "body"))
                assert ("acquired", "slot") in other_ops.events
                assert any(token[2] == "slot" for token in ops.live.values())
            assert_released(other_ops)
        assert_released(ops)
        identities = {(p.stat().st_dev, p.stat().st_ino) for p in a.iterdir()}
        assert identities
        with pool.acquire(deadline=time.monotonic() + 5):
            pass
        assert identities == {(p.stat().st_dev, p.stat().st_ino) for p in a.iterdir()}
        assert_released(ops)
        return

    point, fault = case.split(":")
    primary = (
        KeyboardInterrupt("admission interrupted")
        if fault == "interruption"
        else OSError("admission operation")
        if fault in {"operation-close", "operation"}
        else None
    )
    release = OSError("slot release")
    parent_release = OSError("namespace release")
    ops.point = point if primary is not None else None
    ops.primary = primary
    if point != "open":
        ops.release = None if point == "directory" else release
        ops.parent_release = parent_release
    context = pool.acquire(deadline=time.monotonic() + 5)
    with pytest.raises(BaseException) as caught:
        with context:
            ops.events.append(("entered", "body"))
            if primary is not None:
                raise primary
    if point == "open" and isinstance(primary, OSError):
        assert isinstance(caught.value, DescriptorError)
        assert caught.value.cause is primary
    else:
        assert caught.value is (
            primary or (parent_release if point == "directory" else release)
        )
    if point in {"metadata", "flock", "open"} and primary is not None:
        assert ("entered", "body") not in ops.events
    assert_released(ops)
    if point != "open":
        secondary = getattr(caught.value, "_transaction_secondary", ())
        if primary is not None or point != "directory":
            assert any(item is parent_release for item in secondary)
        if primary is not None and point != "directory":
            assert any(item is release for item in secondary)
        assert sum(token[2] == "slot" for token in ops.closed) == 1
        assert sum(token[2] == "namespace" for token in ops.closed) == 1
    before = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == before
    # Public fresh acquisition proves failed cleanup did not retain the lock.
    ops.point = ops.primary = ops.release = ops.parent_release = None
    with pool.acquire(deadline=time.monotonic() + 5):
        pass
    assert_released(ops)


@pytest.mark.parametrize(
    ("target", "value", "error"),
    [
        *[("capacity", value, TypeError) for value in (True, False, None, "2", 2.0)],
        *[("capacity", value, ValueError) for value in (0, -1)],
        *[("deadline", value, TypeError) for value in (True, False, None, "10", 1j)],
        *[
            ("deadline", value, ValueError)
            for value in (float("nan"), float("inf"), -float("inf"))
        ],
        ("valid", 1, None),
        ("valid", 2, None),
        ("valid-integer-deadline", 1, None),
        ("valid-large-integer-deadline", 1, None),
    ],
)
def test_pool_rejects_invalid_configuration(tmp_path, target, value, error):
    ops = RecordingDescriptors(tmp_path)
    system = RecordingSystem(descriptors=ops)
    if target == "capacity":
        with pytest.raises(error):
            AdmissionPool(directory=tmp_path, capacity=value, system=system)
    else:
        pool = AdmissionPool(
            directory=tmp_path,
            capacity=value if target == "valid" else 1,
            system=system,
        )
        assert ops.events == []
        if target == "deadline":
            with pytest.raises(error):
                with pool.acquire(deadline=value):
                    raise AssertionError("invalid deadline admitted protected work")
        else:
            with pytest.raises(FrozenInstanceError):
                # Deliberate mutation: directory is also part of the frozen contract.
                pool.directory = tmp_path / "other"  # ty: ignore[invalid-assignment]
            deadline = (
                10**1000
                if target == "valid-large-integer-deadline"
                else int(time.monotonic()) + 5
                if target == "valid-integer-deadline"
                else time.monotonic() + 5
            )
            context = pool.acquire(deadline=deadline)
            with context as entered:
                assert entered is None
            before = list(ops.closed)
            context.__exit__(None, None, None)
            assert ops.closed == before
            assert_released(ops)
            return
    assert ops.events == []
    assert list(tmp_path.iterdir()) == []


POOL_CHILD = r"""
import sys
import time
from pathlib import Path
from tests.admission_support import AdmissionPool, PosixAdmissionSystem

class ObservedSystem(PosixAdmissionSystem):
    def try_lock_exclusive(self, fd):
        print('attempt', flush=True)
        acquired = super().try_lock_exclusive(fd)
        print('acquired' if acquired else 'busy', flush=True)
        return acquired

pool = AdmissionPool(directory=Path(sys.argv[1]), capacity=int(sys.argv[2]), system=ObservedSystem())
print('ready', flush=True)
if sys.stdin.readline() != 'attempt\n':
    raise RuntimeError('missing attempt handshake')
with pool.acquire(deadline=time.monotonic()+10):
    print('entered', flush=True)
    if sys.stdin.readline() != 'release\n':
        raise RuntimeError('missing release handshake')
print('released', flush=True)
"""


@pytest.mark.parametrize(
    "case",
    [
        "capacity-one",
        "capacity-two",
        "operation-close",
        "sole-close",
        "interruption",
        "repeat",
        "later-operation-close",
        "later-interruption",
    ],
)
def test_pool_uses_available_capacity(tmp_path, case):
    capacity = 1 if case == "capacity-one" else 2
    directory = tmp_path / "pool"
    directory.mkdir()
    first_ops = RecordingDescriptors(directory)
    first = AdmissionPool(
        directory=directory,
        capacity=capacity,
        system=RecordingSystem(descriptors=first_ops),
    )
    hold = first.acquire(deadline=time.monotonic() + 10)
    primary = (
        KeyboardInterrupt("busy probe interrupted")
        if "interruption" in case
        else OSError("busy probe operation")
        if "operation" in case
        else None
    )
    release = OSError("busy probe release")
    failures = []
    lines = []
    partial = bytearray()
    third_ops = RecordingDescriptors(directory)
    third_context = None
    third_entered = []
    released_child = []
    later = case.startswith("later-")

    class ProgressDescriptors(RecordingDescriptors):
        def fstat(self, fd):
            info = super().fstat(fd)
            if (
                later
                and stat.S_ISREG(info.st_mode)
                and sum(token[2] == "slot" for token in self.opened) == 2
            ):
                release_child()
                self.release = release
                assert primary is not None, (
                    "later-probe fault requires a primary exception"
                )
                raise primary
            return info

    third_ops = ProgressDescriptors(directory)

    def release_child():
        if not released_child:
            released_child.append(True)
            child.write_stdin(b"release\n", timeout_seconds=1)
            child.close_stdin(timeout_seconds=1)

    class ProgressSystem(RecordingSystem):
        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if not acquired:
                assert any(token[2] == "slot" for token in first_ops.live.values())
                if (
                    case != "capacity-two"
                    or sum(event == ("busy", "slot") for event in third_ops.events)
                    >= capacity
                ):
                    release_child()
                if case not in {"capacity-two", "capacity-one"} and not later:
                    third_ops.release = release
                    if primary is not None:
                        raise primary
            return acquired

    def on_chunk(stream, payload):
        nonlocal third_context
        if stream is not ProcessStream.STDOUT:
            return
        partial.extend(payload)
        while b"\n" in partial:
            line, _, rest = partial.partition(b"\n")
            partial[:] = rest
            text = line.decode()
            lines.append(text)
            if failures:
                continue
            try:
                if text == "ready":
                    child.write_stdin(b"attempt\n", timeout_seconds=1)
                elif text == "busy" and capacity == 1:
                    # The same sole context releases its own hold, not a borrowed fd.
                    hold.__exit__(None, None, None)
                elif text == "entered":
                    if capacity == 1:
                        assert first_ops.live == {}
                        release_child()
                    else:
                        assert "busy" in lines and "acquired" in lines
                        pool = AdmissionPool(
                            directory=directory,
                            capacity=2,
                            system=ProgressSystem(descriptors=third_ops),
                        )
                        third_context = pool.acquire(deadline=time.monotonic() + 10)
                        with third_context:
                            assert ("busy", "slot") in third_ops.events
                            assert released_child
                            assert len(set(third_ops.slot_names)) <= capacity
                            third_entered.append(True)
            except BaseException as error:
                failures.append(error)
                try:
                    child.close_stdin(timeout_seconds=1)
                except BaseException as cleanup:
                    failures.append(cleanup)

    argv = [sys.executable, "-u", "-c", POOL_CHILD, str(directory), str(capacity)]
    with hold:
        with OwnedProcess.launch(
            argv,
            stdin_mode="pipe",
            deadline_seconds=10,
            grace_seconds=2,
            popen_kwargs={
                "cwd": str(Path(__file__).resolve().parents[1]),
                "env": mcp_wheel_support.clean_environment(),
            },
        ) as child:
            result = child.stream(on_chunk)
        assert child.cleanup_status.complete
        assert child.wait_cleanup(0).complete
        assert not result.lifecycle.failures
        assert result.returncode == 0 and not result.timed_out, result.stderr.decode(
            "utf-8", "replace"
        )
        assert all(
            outcome is StreamOutcome.EOF for _, outcome in result.stream_outcomes
        )
        assert not partial
    mcp_provenance.write(
        tmp_path / "child-observation.json",
        json.dumps(
            {
                "argv": argv,
                "status": result.returncode,
                "lines": lines,
                "cleanup_complete": child.cleanup_status.complete,
            }
        ).encode(),
    )
    assert_released(first_ops)
    assert_released(third_ops)
    assert len(set(first_ops.slot_names + third_ops.slot_names)) <= capacity
    assert len(list(directory.iterdir())) == capacity
    if third_context is not None:
        before = list(third_ops.closed)
        third_context.__exit__(None, None, None)
        third_context.__exit__(None, None, None)
        assert before == third_ops.closed
    assert lines[0] == "ready" and "attempt" in lines and "busy" in lines
    assert lines[-1] == "released" and lines.count("entered") == 1
    if case in {"capacity-one", "capacity-two"}:
        assert not failures
        if capacity == 2:
            assert third_entered == [True]
    else:
        assert len(failures) == 1
        assert failures[0] is (primary or release)
        assert not third_entered
        if primary is not None:
            assert any(
                item is release
                for item in getattr(primary, "_transaction_secondary", ())
            )
    with first.acquire(deadline=time.monotonic() + 5):
        pass
    assert_released(first_ops)


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "past",
        "initial-exact",
        "initial-past",
        "timeout-close",
        "sleep-operation-close",
        "sleep-interruption",
        "repeat",
    ],
)
def test_pool_wait_uses_original_deadline(tmp_path, case):
    holder = AdmissionPool(directory=tmp_path, capacity=1)
    ops = RecordingDescriptors(tmp_path)
    now = [100.0]
    slept = []
    primary = (
        KeyboardInterrupt("wait interrupted")
        if case == "sleep-interruption"
        else OSError("wait failed")
        if case == "sleep-operation-close"
        else None
    )
    release = OSError("waiting namespace close")
    if case in {"timeout-close", "sleep-operation-close", "sleep-interruption"}:
        ops.parent_release = release

    class ControlledSystem(RecordingSystem):
        def monotonic(self):
            ops.events.append(("monotonic", now[0]))
            return now[0]

        def sleep(self, seconds):
            assert seconds >= 0
            slept.append(seconds)
            if len(slept) > 10:
                raise RuntimeError("waiting renewed or ignored original deadline")
            if primary is not None:
                raise primary
            now[0] += 2 if case == "past" else 1

    pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=ControlledSystem(descriptors=ops)
    )
    deadline = 100 if case == "initial-exact" else 99 if case == "initial-past" else 105
    context = pool.acquire(deadline=deadline)
    with holder.acquire(deadline=time.monotonic() + 5):
        with pytest.raises(BaseException) as caught:
            with context:
                raise AssertionError("exhausted admission entered protected work")
        if primary is not None:
            assert caught.value is primary
        else:
            assert isinstance(caught.value, TimeoutError)
        if ops.parent_release is not None:
            assert any(
                item is release
                for item in getattr(caught.value, "_transaction_secondary", ())
            )
        if not case.startswith("initial-"):
            assert ("busy", "slot") in ops.events
        if primary is None and not case.startswith("initial-"):
            assert now[0] == (106 if case == "past" else 105)
        before = list(ops.closed)
        context.__exit__(None, None, None)
        context.__exit__(None, None, None)
        assert ops.closed == before
        assert_released(ops)
    with holder.acquire(deadline=time.monotonic() + 5):
        pass


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "past",
        "positive",
        "exact-close",
        "past-close",
        "sole-close",
        "interruption-close",
        "repeat",
    ],
)
def test_pool_checks_deadline_before_entry(tmp_path, case):
    ops = RecordingDescriptors(tmp_path)
    now = [100.0]
    release = OSError("late slot release")
    parent_release = OSError("late namespace release")
    interruption = KeyboardInterrupt("protected work interrupted")
    closing = case.endswith("close")
    if closing:
        ops.release = release
        ops.parent_release = parent_release
    late = case not in {"positive", "sole-close", "interruption-close"}

    class LockBoundarySystem(RecordingSystem):
        def monotonic(self):
            return now[0]

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            assert acquired
            now[0] = 106 if case.startswith("past") else 105 if late else 104
            return acquired

    pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=LockBoundarySystem(descriptors=ops)
    )
    context = pool.acquire(deadline=105)
    entered = []
    caught = None
    try:
        with context as value:
            entered.append(True)
            assert value is None
            if case == "interruption-close":
                raise interruption
    except BaseException as error:
        caught = error
    assert ("acquired", "slot") in ops.events
    if late:
        assert isinstance(caught, TimeoutError)
        assert not entered
    elif case == "positive":
        assert caught is None and entered == [True]
    elif case == "sole-close":
        assert caught is release and entered == [True]
    else:
        assert caught is interruption and entered == [True]
    if closing:
        secondary = getattr(caught, "_transaction_secondary", ())
        assert any(item is parent_release for item in secondary)
        if caught is not release:
            assert any(item is release for item in secondary)
    assert_released(ops)
    before = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == before
    with AdmissionPool(directory=tmp_path, capacity=1).acquire(
        deadline=time.monotonic() + 5
    ):
        pass


def _bind_socket_node(path, *, socket_factory=socket.socket, owners=None):
    label = "admission test socket"
    owner = OwnedStream(socket_factory(socket.AF_UNIX, socket.SOCK_STREAM), label=label)
    with owner:
        if owners is not None:
            owners.append(owner)
        owner.stream.bind(str(path))
    return owner


@pytest.mark.parametrize(
    "case", ["operation-close", "sole-close", "interruption", "repeat"]
)
def test_socket_node_owner_lifecycle(tmp_path, case):
    primary = (
        KeyboardInterrupt("socket bind interrupted")
        if case == "interruption"
        else OSError("socket bind failed")
    )
    release = OSError("socket close failed")
    owners = []
    closes = []
    descriptors = []

    class FaultSocket(socket.socket):
        def bind(self, address):
            super().bind(address)
            descriptors.append(self.fileno())
            if case in {"operation-close", "interruption"}:
                raise primary

        def close(self):
            closes.append(self.fileno())
            super().close()
            if case != "repeat":
                raise release

    if case == "repeat":
        _bind_socket_node(
            tmp_path / "socket", socket_factory=FaultSocket, owners=owners
        )
    else:
        with pytest.raises(BaseException) as caught:
            _bind_socket_node(
                tmp_path / "socket", socket_factory=FaultSocket, owners=owners
            )
        assert caught.value is (release if case == "sole-close" else primary)
        if case != "sole-close":
            assert any(
                item is release
                for item in getattr(caught.value, "_transaction_secondary", ())
            )
    assert len(owners) == len(descriptors) == len(closes) == 1
    assert owners[0].released
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF
    owners[0].close()
    owners[0].__exit__(None, None, None)
    assert len(closes) == 1


@pytest.mark.parametrize(
    "case", ["io", "regular-enxio", "stat-error", "stat-interruption"]
)
def test_slot_open_failure_preserves_io_and_cleanup(tmp_path, case):
    with AdmissionPool(directory=tmp_path, capacity=1).acquire(
        deadline=time.monotonic() + 5
    ):
        pass
    primary = OSError(errno.EIO if case == "io" else errno.ENXIO, "slot open failed")
    observation_error = (
        KeyboardInterrupt("slot stat interrupted")
        if case == "stat-interruption"
        else OSError(errno.EACCES, "slot stat denied")
    )
    release = OSError("namespace close failed")
    observations = []

    class OpenFailureDescriptors(RecordingDescriptors):
        enabled = True

        def openat(self, directory_fd, name, flags, mode=0):
            if (
                self.enabled
                and directory_fd in self.live
                and self.live[directory_fd][2] == "namespace"
            ):
                raise primary
            return super().openat(directory_fd, name, flags, mode)

        def statat(self, directory_fd, name, *, follow_symlinks):
            assert self.live[directory_fd][2] == "namespace"
            assert follow_symlinks is False
            observations.append(name)
            if case in {"stat-error", "stat-interruption"}:
                raise observation_error
            info = super().statat(directory_fd, name, follow_symlinks=follow_symlinks)
            assert stat.S_ISREG(info.st_mode)
            return info

    ops = OpenFailureDescriptors(tmp_path)
    ops.parent_release = release
    pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=RecordingSystem(descriptors=ops)
    )
    context = pool.acquire(deadline=time.monotonic() + 5)
    with pytest.raises(BaseException) as caught:
        with context:
            raise AssertionError("failed open entered protected work")
    assert caught.value is (
        observation_error if case == "stat-interruption" else primary
    )
    assert bool(observations) is (case != "io")
    if case == "stat-error":
        assert caught.value.__cause__ is observation_error
    assert any(
        item is release for item in getattr(caught.value, "_transaction_secondary", ())
    )
    assert_released(ops)
    before = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == before
    ops.enabled = False
    ops.parent_release = None
    with pool.acquire(deadline=time.monotonic() + 5):
        pass
    assert_released(ops)


@pytest.mark.parametrize(
    "case",
    [
        "slot-symlink",
        "slot-directory",
        "slot-fifo",
        "slot-socket",
        "slot-socket-close",
        "foreign-slot",
        "foreign-slot-close",
        "foreign-slot-repeat",
        "namespace-symlink",
        "namespace-file",
        "namespace-foreign",
        "safe-sole-close",
        "safe-interruption-close",
    ],
)
def test_pool_rejects_unsafe_node(tmp_path, case):
    directory = tmp_path / "pool"
    directory.mkdir()
    with AdmissionPool(directory=directory, capacity=1).acquire(
        deadline=time.monotonic() + 5
    ):
        pass
    slots = list(directory.iterdir())
    assert len(slots) == 1
    slot = slots[0]
    real_owner = slot.stat().st_uid
    if case.startswith("slot-"):
        slot.unlink()
        if case == "slot-symlink":
            target = tmp_path / "target"
            mcp_provenance.write(target, b"unchanged")
            slot.symlink_to(target)
        elif case == "slot-directory":
            slot.mkdir()
        elif case.startswith("slot-socket"):
            _bind_socket_node(slot)
            assert stat.S_ISSOCK(slot.lstat().st_mode)
        else:
            os.mkfifo(slot, 0o600)
    namespace = directory
    if case == "namespace-symlink":
        namespace = tmp_path / "link"
        namespace.symlink_to(directory, target_is_directory=True)
    elif case == "namespace-file":
        namespace = tmp_path / "file"
        mcp_provenance.write(namespace, b"not a directory")
    observed = []
    supplied = []
    release = OSError("security slot close")
    parent_release = OSError("security namespace close")
    interruption = KeyboardInterrupt("security metadata interrupted")

    class SecurityDescriptors(RecordingDescriptors):
        def fstat(self, fd):
            info = super().fstat(fd)
            regular = stat.S_ISREG(info.st_mode)
            is_namespace = (info.st_dev, info.st_ino) == self.directory_identity
            observed.append(
                "regular" if regular else "namespace" if is_namespace else "ancestor"
            )
            if case == "safe-interruption-close" and regular:
                raise interruption
            selected = (case.startswith("foreign-slot") and regular) or (
                case == "namespace-foreign" and is_namespace
            )
            if not selected:
                return info
            fields = list(info)
            fields[4] = os.geteuid() + 1
            extras = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            tuple_fields = {
                "st_mode",
                "st_ino",
                "st_dev",
                "st_nlink",
                "st_uid",
                "st_gid",
                "st_size",
            }
            changed = os.stat_result(
                fields,
                {
                    name: value
                    for name, value in extras.items()
                    if name not in tuple_fields
                },
            )
            assert changed.st_uid != os.geteuid()
            for name in extras:
                if name != "st_uid":
                    assert getattr(changed, name) == getattr(info, name)
            if regular:
                assert "namespace" in observed[:-1]
                assert os.fstat(fd).st_uid == real_owner
            supplied.append((info, changed))
            return changed

    ops = SecurityDescriptors(directory)
    if case.endswith("close"):
        ops.release = release
        ops.parent_release = parent_release
    pool = AdmissionPool(
        directory=namespace, capacity=1, system=RecordingSystem(descriptors=ops)
    )
    context = pool.acquire(deadline=time.monotonic() + 5)
    with pytest.raises(BaseException) as caught:
        with context:
            if case != "safe-sole-close":
                raise AssertionError("unsafe acquisition entered protected work")
    if case == "safe-sole-close":
        # Successful work is needed to isolate a sole-close failure.
        assert caught.value is release
    elif case == "safe-interruption-close":
        assert caught.value is interruption
    elif case.startswith("namespace-"):
        assert isinstance(caught.value, DescriptorError)
    else:
        assert isinstance(caught.value, UnsafeDescriptorError)
    if case.startswith("foreign-slot") or case == "namespace-foreign":
        assert supplied, "foreign UID observation prerequisite was not supplied"
        assert not any(event == ("attempt", "slot") for event in ops.events)
    if case.endswith("close"):
        secondary = getattr(caught.value, "_transaction_secondary", ())
        assert any(item is parent_release for item in secondary)
        if case not in {"safe-sole-close", "slot-socket-close"}:
            assert any(item is release for item in secondary)
    if case.startswith("slot-socket"):
        assert isinstance(caught.value, UnsafeDescriptorError)
        assert isinstance(caught.value.cause, OSError)
        assert caught.value.cause.errno == errno.ENXIO
        assert not any(kind == "slot" for _, _, kind in ops.opened)
        assert ("attempt", "slot") not in ops.events
    assert_released(ops)
    before = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == before
    if not case.startswith("slot-"):
        with AdmissionPool(directory=directory, capacity=1).acquire(
            deadline=time.monotonic() + 5
        ):
            pass


@pytest.mark.parametrize(
    ("capacity", "reason", "error"),
    [
        *[
            (value, "valid diagnostic", TypeError)
            for value in (True, False, None, "2", 2.0, 1j)
        ],
        *[(value, "valid diagnostic", ValueError) for value in (0, -1)],
        *[(1, value, TypeError) for value in (None, True, 1, 1.0, b"diagnostic")],
        *[(1, value, ValueError) for value in ("", " ", "\t\n", "\u2003")],
        (1, "single worker", None),
        (6, "workers and affinity", None),
        (10**1000, "large integer", None),
        (2, "  keep original diagnostic \n", None),
    ],
)
def test_capacity_decision_validates_direct_construction(capacity, reason, error):
    if error is not None:
        with pytest.raises(error):
            CapacityDecision(capacity=capacity, reason=reason)
    else:
        decision = CapacityDecision(capacity=capacity, reason=reason)
        assert decision.capacity == capacity
        assert decision.reason == reason


@pytest.mark.parametrize(
    ("workers", "affinity", "expected", "error"),
    [
        (8, frozenset(range(6)), 6, None),
        (3, frozenset(range(6)), 3, None),
        (8, frozenset({2}), 1, None),
        (1, frozenset(range(6)), 1, None),
        *[
            (value, frozenset({0}), None, TypeError)
            for value in (True, False, None, "8", 8.0)
        ],
        *[(value, frozenset({0}), None, ValueError) for value in (0, -1)],
        (8, OSError("affinity failed"), None, OSError),
        (8, KeyboardInterrupt("affinity interrupted"), None, KeyboardInterrupt),
        (1, "default", 1, None),
    ],
)
def test_capacity_respects_workers_and_affinity(workers, affinity, expected, error):
    observations = []
    files = v2_observations()

    class RawSystem:
        def affinity(self):
            observations.append("affinity")
            if isinstance(affinity, BaseException):
                raise affinity
            return affinity

        def read_text(self, path):
            observations.append(str(path))
            return files[path]

    system = None if affinity == "default" else RawSystem()
    if error is not None:
        with pytest.raises(error) as caught:
            resolve_admission_capacity(workers=workers, system=system)
        if isinstance(affinity, BaseException):
            assert caught.value is affinity
            assert observations == ["affinity"]
        else:
            assert observations == []
        return
    result = resolve_admission_capacity(workers=workers, system=system)
    assert result.capacity == expected
    assert isinstance(result.capacity, int) and not isinstance(result.capacity, bool)
    assert isinstance(result.reason, str) and result.reason.strip()
    with pytest.raises(FrozenInstanceError):
        # Deliberate mutation: verify decisions cannot be changed after resolution.
        result.capacity = 999  # ty: ignore[invalid-assignment]
    assert observations == ([] if system is None else ["affinity", *map(str, files)])


def v2_observations(
    *, leaf="max 100000", parent="max 100000", root="max 100000", mount_root="/slice"
):
    # Literal raw hierarchy, including an unrelated mount and an escaped space.
    return {
        Path("/proc/self/cgroup"): "0::/slice/team/job\n",
        Path("/proc/self/mountinfo"): (
            "31 20 0:28 /unrelated /other rw - cgroup2 cgroup rw\n"
            f"32 20 0:29 {mount_root} /cpu\\040budgets rw - cgroup2 cgroup rw\n"
        ),
        Path("/cpu budgets/team/job/cpu.max"): leaf + "\n",
        Path("/cpu budgets/team/cpu.max"): parent + "\n",
        Path("/cpu budgets/cpu.max"): root + "\n",
    }


@pytest.mark.parametrize(
    ("case", "leaf", "parent", "root", "workers", "cpus", "expected"),
    [
        ("finite", "250000 100000", "max 100000", "max 100000", 8, 6, 2),
        ("sub-cpu", "50000 100000", "max 100000", "max 100000", 8, 6, 1),
        ("unlimited-leaf", "max 100000", "180000 100000", "max 100000", 8, 6, 1),
        ("ancestor-min", "450000 100000", "250000 100000", "max 100000", 8, 6, 2),
        ("root-min", "max 100000", "max 100000", "150000 100000", 8, 6, 1),
        ("unlimited", "max 100000", "max 100000", "max 100000", 8, 6, 6),
        ("workers", "max 100000", "max 100000", "max 100000", 3, 6, 3),
        ("affinity", "max 100000", "max 100000", "max 100000", 8, 2, 2),
        ("huge", f"{10**400 * 5} {10**400 * 2}", "max 100000", "max 100000", 8, 6, 2),
        ("visible-root", "250000 100000", "max 100000", "max 100000", 8, 6, 2),
        *[
            (layout, "250000 100000", "max 100000", "max 100000", 8, 6, 2)
            for layout in ("widest-view", "root-components", "escaped-root")
        ],
        *[
            (fault, "250000 100000", "max 100000", "max 100000", 8, 6, 2)
            for fault in (
                "owned-repeat",
                "nonregular-close",
                "decode-close",
                "operation-close",
                "sole-close",
                "interruption",
                "later-operation-close",
            )
        ],
    ],
)
def test_capacity_respects_v2_quota(
    tmp_path, case, leaf, parent, root, workers, cpus, expected
):
    files = v2_observations(leaf=leaf, parent=parent, root=root)
    if case == "visible-root":
        files = {
            Path("/proc/self/cgroup"): "0::/team/job\n",
            Path(
                "/proc/self/mountinfo"
            ): "32 20 0:29 / /cpu\\040budgets rw - cgroup2 cgroup rw\n",
            **{
                path: text
                for path, text in files.items()
                if str(path).startswith("/cpu budgets/")
            },
        }
    if case == "widest-view":
        files[Path("/proc/self/mountinfo")] += (
            "33 20 0:29 /slice/team /narrow rw - cgroup2 cgroup rw\n"
        )
    elif case == "root-components":
        files[Path("/proc/self/mountinfo")] = (
            "30 20 0:29 /slic /trap rw - cgroup2 cgroup rw\n"
            + files[Path("/proc/self/mountinfo")]
        )
    elif case == "escaped-root":
        files[Path("/proc/self/cgroup")] = "0::/slice space/team/job\n"
        files[Path("/proc/self/mountinfo")] = (
            "32 20 0:29 /slice\\040space /cpu\\040budgets rw - cgroup2 cgroup rw\n"
        )
    missing_paths = set()
    if case == "visible-root":
        del files[Path("/cpu budgets/cpu.max")]
        files[Path("/cpu budgets/cgroup.controllers")] = "cpu memory pids\n"
        missing_paths = {Path("/cpu budgets/cpu.max"), Path("/cpu budgets/cgroup.type")}
    expected_reads = list(files)
    if missing_paths:
        expected_reads = [
            *list(files)[:-1],
            Path("/cpu budgets/cpu.max"),
            Path("/cpu budgets/cgroup.type"),
            Path("/cpu budgets/cgroup.controllers"),
        ]
    observed = []
    owned = case in {
        "visible-root",
        "nonregular-close",
        "decode-close",
        "owned-repeat",
        "operation-close",
        "sole-close",
        "interruption",
        "later-operation-close",
    }
    primary = (
        KeyboardInterrupt("quota observation interrupted")
        if case == "interruption"
        else OSError("quota observation failed")
    )
    release = OSError("quota read close failed")
    fault_index = 2 if case == "later-operation-close" else 0

    class ReadDescriptors(RecordingDescriptors):
        active = True

        def openat(self, directory_fd, name, flags, mode=0):
            assert not self.live, "previous read must settle before another acquisition"
            return super().openat(directory_fd, name, flags, mode)

        def fstat(self, fd):
            info = super().fstat(fd)
            if (
                self.active
                and len(self.opened) - 1 == fault_index
                and case in {"operation-close", "interruption", "later-operation-close"}
            ):
                raise primary
            return info

        def close(self, fd):
            token = self.live[fd]
            super().close(fd)
            if (
                self.active
                and token[0] == fault_index
                and case
                in {
                    "nonregular-close",
                    "decode-close",
                    "operation-close",
                    "sole-close",
                    "interruption",
                    "later-operation-close",
                }
            ):
                raise release

    ops = ReadDescriptors(tmp_path)
    if owned:
        for path, text in files.items():
            real = tmp_path / str(path).lstrip("/")
            real.parent.mkdir(parents=True, exist_ok=True)
            mcp_provenance.write(real, text.encode())
    damaged = tmp_path / "proc/self/cgroup"
    if case in {"nonregular-close", "decode-close"}:
        damaged.unlink()
        if case == "nonregular-close":
            os.mkfifo(damaged, 0o600)
        else:
            mcp_provenance.write(damaged, b"\xff")

    class RawSystem(PosixCapacitySystem):
        def affinity(self):
            return frozenset(range(cpus))

        def read_text(self, path):
            assert path in files or path in missing_paths, (
                f"unexpected CPU observation: {path}"
            )
            observed.append(path)
            if owned:
                return super().read_text(tmp_path / str(path).lstrip("/"))
            return files[path]

    system = RawSystem(descriptors=ops)
    if owned and case not in {"owned-repeat", "visible-root"}:
        with pytest.raises(BaseException) as caught:
            resolve_admission_capacity(workers=workers, system=system)
        if case == "nonregular-close":
            assert isinstance(caught.value, UnsafeDescriptorError)
        elif case == "decode-close":
            assert isinstance(caught.value, UnicodeDecodeError)
        else:
            assert caught.value is (release if case == "sole-close" else primary)
        if case != "sole-close":
            assert any(
                item is release
                for item in getattr(caught.value, "_transaction_secondary", ())
            )
        assert observed == list(files)[: fault_index + 1]
        assert_released(ops)
        closed_before = list(ops.closed)
        ops.active = False
        if case in {"nonregular-close", "decode-close"}:
            damaged.unlink()
            mcp_provenance.write(damaged, files[Path("/proc/self/cgroup")].encode())
        observed.clear()
        result = resolve_admission_capacity(workers=workers, system=system)
        assert ops.closed[: len(closed_before)] == closed_before
    else:
        result = resolve_admission_capacity(workers=workers, system=system)
    assert result.capacity == expected
    assert result.reason.strip()
    assert observed == expected_reads
    if owned:
        assert_released(ops)
        previous = list(ops.closed)
        observed.clear()
        again = resolve_admission_capacity(workers=workers, system=system)
        assert again.capacity == expected
        assert observed == expected_reads
        assert ops.closed[: len(previous)] == previous
        assert len(ops.closed) == len(previous) + len(files)
        assert_released(ops)


def v1_observations(*, leaf="-1", parent="-1", root="-1"):
    files = {
        Path(
            "/proc/self/cgroup"
        ): "2:cpu,cpuacct:/slice/team/job\n3:cpuset:/elsewhere\n",
        Path("/proc/self/mountinfo"): (
            "30 20 0:28 / /cpuacct rw - cgroup cgroup rw,cpuacct\n"
            "31 20 0:29 /slice /cpu\\040budgets rw - cgroup cgroup rw,cpu,cpuacct\n"
        ),
    }
    for directory, quota in (("team/job", leaf), ("team", parent), ("", root)):
        base = Path("/cpu budgets") / directory
        files[base / "cpu.cfs_quota_us"] = quota + "\n"
        files[base / "cpu.cfs_period_us"] = "100000\n"
    return files


@pytest.mark.parametrize(
    ("case", "leaf", "parent", "root", "workers", "cpus", "expected"),
    [
        ("finite", "250000", "-1", "-1", 8, 6, 2),
        ("sub-cpu", "50000", "-1", "-1", 8, 6, 1),
        ("unlimited-leaf", "-1", "180000", "-1", 8, 6, 1),
        ("ancestor-min", "450000", "250000", "-1", 8, 6, 2),
        ("root-min", "-1", "-1", "150000", 8, 6, 1),
        ("unlimited", "-1", "-1", "-1", 8, 6, 6),
        ("workers", "-1", "-1", "-1", 3, 6, 3),
        ("affinity", "-1", "-1", "-1", 8, 2, 2),
        ("hybrid", "250000", "-1", "-1", 8, 6, 2),
        *[
            (fault, "250000", "-1", "-1", 8, 6, 2)
            for fault in (
                "owned-repeat",
                "operation-close",
                "sole-close",
                "interruption",
            )
        ],
    ],
)
def test_capacity_respects_v1_quota(
    tmp_path, case, leaf, parent, root, workers, cpus, expected
):
    files = v1_observations(leaf=leaf, parent=parent, root=root)
    if case == "hybrid":
        files[Path("/proc/self/cgroup")] += "0::/unified\n"
        files[Path("/proc/self/mountinfo")] += (
            "32 20 0:30 / /unified-mount rw - cgroup2 cgroup rw\n"
        )
    owned = case in {"owned-repeat", "operation-close", "sole-close", "interruption"}
    observations = []
    primary = (
        KeyboardInterrupt("v1 period interrupted")
        if case == "interruption"
        else OSError("v1 period metadata")
    )
    release = OSError("v1 period close")

    class PeriodDescriptors(RecordingDescriptors):
        active = True

        def openat(self, directory_fd, name, flags, mode=0):
            assert not self.live
            return super().openat(directory_fd, name, flags, mode)

        def fstat(self, fd):
            info = super().fstat(fd)
            if self.active and len(self.opened) == 4 and case != "owned-repeat":
                self.release = release
                if case != "sole-close":
                    raise primary
            return info

    ops = PeriodDescriptors(tmp_path)
    if owned:
        for path, text in files.items():
            real = tmp_path / str(path).lstrip("/")
            real.parent.mkdir(parents=True, exist_ok=True)
            mcp_provenance.write(real, text.encode())

    class RawSystem(PosixCapacitySystem):
        def affinity(self):
            return frozenset(range(cpus))

        def read_text(self, path):
            observations.append(path)
            assert path in files
            return (
                super().read_text(tmp_path / str(path).lstrip("/"))
                if owned
                else files[path]
            )

    system = RawSystem(descriptors=ops)
    if owned and case != "owned-repeat":
        with pytest.raises(BaseException) as caught:
            resolve_admission_capacity(workers=workers, system=system)
        assert caught.value is (release if case == "sole-close" else primary)
        if case != "sole-close":
            assert any(
                item is release
                for item in getattr(caught.value, "_transaction_secondary", ())
            )
        assert observations == list(files)[:4]
        assert_released(ops)
        ops.active = False
        ops.release = None
        observations.clear()
    result = resolve_admission_capacity(workers=workers, system=system)
    assert result.capacity == expected
    assert result.reason.strip()
    assert observations == list(files)
    assert_released(ops)
    if owned:
        previous = list(ops.closed)
        observations.clear()
        assert (
            resolve_admission_capacity(workers=workers, system=system).capacity
            == expected
        )
        assert observations == list(files)
        assert ops.closed[: len(previous)] == previous
        assert len(ops.closed) == len(previous) + len(files)
        assert_released(ops)


@pytest.mark.parametrize(
    "case",
    [
        "finite",
        "unlimited",
        "at-root",
        "no-cpu",
        "missing-child",
        "namespace-finite",
        "namespace-at-root",
        "namespace-missing",
        "bind-finite",
        "bind-at-root",
        "bind-missing",
        "root-access",
        "root-read-enoent",
        "type-access",
        "type-read-enoent",
        "root-close",
        "type-close",
        "controller-close",
        "type-interruption",
        "type-operation-close",
    ],
)
def test_v2_hierarchy_root_without_cpu_max(tmp_path, case):
    files = v2_observations(
        leaf="250000 100000" if case == "finite" else "max 100000", mount_root="/"
    )
    membership = Path("/proc/self/cgroup")
    root_quota = Path("/cpu budgets/cpu.max")
    root_type = Path("/cpu budgets/cgroup.type")
    controllers = Path("/cpu budgets/cgroup.controllers")
    files[membership] = "0::/team/job\n"
    if case.startswith("bind-"):
        files = v2_observations()
    if case in {"at-root", "namespace-at-root"}:
        files[membership] = "0::/\n"
    elif case == "bind-at-root":
        files[membership] = "0::/slice\n"
    files[controllers] = "memory pids\n" if case == "no-cpu" else "cpu memory pids\n"
    if case in {"namespace-finite", "namespace-at-root", "bind-finite", "bind-at-root"}:
        files[root_quota] = "250000 100000\n"
    elif case not in {"root-read-enoent", "root-close"}:
        del files[root_quota]
    if case.startswith("namespace-") or case in {
        "type-read-enoent",
        "type-access",
        "type-close",
        "type-interruption",
        "type-operation-close",
    }:
        files[root_type] = "domain\n"
    if case == "missing-child":
        del files[Path("/cpu budgets/team/job/cpu.max")]
    for path, text in files.items():
        real = tmp_path / str(path).lstrip("/")
        real.parent.mkdir(parents=True, exist_ok=True)
        mcp_provenance.write(real, text.encode())
    primary = (
        KeyboardInterrupt("root type interrupted")
        if case == "type-interruption"
        else FileNotFoundError(errno.ENOENT, "metadata failure, not absent leaf")
        if case in {"root-read-enoent", "type-read-enoent"}
        else PermissionError(errno.EACCES, "root observation denied")
    )
    release = PermissionError(errno.EACCES, "root observation close denied")
    observed = []
    operation_target = root_quota if case == "root-read-enoent" else root_type
    close_target = (
        root_quota
        if case == "root-close"
        else controllers
        if case == "controller-close"
        else root_type
    )
    real_operation = str(tmp_path / str(operation_target).lstrip("/"))
    real_close = str(tmp_path / str(close_target).lstrip("/"))

    class RootDescriptors(RecordingDescriptors):
        enabled = True
        current_name = ""

        def openat(self, directory_fd, name, flags, mode=0):
            assert not self.live
            self.current_name = name
            if (
                self.enabled
                and case == "root-access"
                and name == str(tmp_path / str(root_quota).lstrip("/"))
            ):
                raise primary
            return super().openat(directory_fd, name, flags, mode)

        def fstat(self, fd):
            info = super().fstat(fd)
            if (
                self.enabled
                and self.current_name == real_operation
                and case
                in {
                    "root-read-enoent",
                    "type-read-enoent",
                    "type-access",
                    "type-interruption",
                    "type-operation-close",
                }
            ):
                raise primary
            return info

        def close(self, fd):
            super().close(fd)
            if (
                self.enabled
                and self.current_name == real_close
                and case
                in {
                    "root-close",
                    "type-close",
                    "controller-close",
                    "type-interruption",
                    "type-operation-close",
                }
            ):
                raise release

    ops = RootDescriptors(tmp_path)

    class RootSystem(PosixCapacitySystem):
        def affinity(self):
            return frozenset(range(6))

        def read_text(self, path):
            observed.append(path)
            return super().read_text(tmp_path / str(path).lstrip("/"))

    system = RootSystem(descriptors=ops)
    errors = {
        "root-close",
        "type-close",
        "controller-close",
        "type-interruption",
        "type-operation-close",
    }
    if case in errors:
        with pytest.raises(BaseException) as caught:
            resolve_admission_capacity(workers=8, system=system)
        assert caught.value is (
            primary
            if case in {"type-interruption", "type-operation-close"}
            else release
        )
        if case in {"type-interruption", "type-operation-close"}:
            assert any(
                item is release
                for item in getattr(caught.value, "_transaction_secondary", ())
            )
    else:
        result = resolve_admission_capacity(workers=8, system=system)
        expected = (
            2
            if case
            in {
                "finite",
                "namespace-finite",
                "namespace-at-root",
                "bind-finite",
                "bind-at-root",
            }
            else 6
            if case in {"unlimited", "at-root"}
            else 1
        )
        assert result.capacity == expected
        assert result.reason.strip()
    assert root_quota in observed or case == "missing-child"
    if case in {"finite", "unlimited", "at-root", "no-cpu", "controller-close"}:
        assert observed[-2:] == [root_type, controllers]
    if case in {
        "namespace-missing",
        "type-read-enoent",
        "type-access",
        "type-close",
        "type-interruption",
        "type-operation-close",
    }:
        assert observed[-1] == root_type
    if case in {
        "namespace-finite",
        "namespace-at-root",
        "bind-finite",
        "bind-at-root",
        "bind-missing",
        "root-access",
        "root-read-enoent",
        "root-close",
        "missing-child",
    }:
        assert root_type not in observed and controllers not in observed
    assert_released(ops)
    previous = list(ops.closed)
    ops.enabled = False
    again = resolve_admission_capacity(workers=8, system=system)
    assert again.capacity > 0
    assert ops.closed[: len(previous)] == previous
    assert_released(ops)


@pytest.mark.parametrize(
    "case",
    [
        "unlimited-v2",
        "unlimited-v1",
        "affinity-unavailable",
        "affinity-empty",
        "affinity-negative",
        "affinity-bool",
        "affinity-interruption",
        "affinity-exit",
        "affinity-unexpected",
        "read-unavailable",
        "read-raw-io",
        "read-unexpected",
        "membership-malformed",
        "membership-missing",
        "membership-duplicate",
        "membership-relative",
        "membership-traversal",
        "mount-malformed",
        "mount-unresolved",
        "mount-prefix",
        "mount-relative",
        "quota-empty",
        "quota-zero",
        "quota-negative",
        "quota-nonnumeric",
        "quota-extra",
        "period-zero",
        "v1-quota-negative",
        "v1-period-zero",
        "owned-missing",
        "owned-unavailable",
        "owned-operation-close",
        "owned-sole-close",
        "owned-interruption-close",
        "owned-cleanup-interruption",
        "owned-unexpected-close",
        "owned-decode",
        "owned-repeat",
    ],
)
def test_capacity_reports_conservative_fallback(tmp_path, case):
    files = (
        v1_observations()
        if case in {"unlimited-v1", "v1-quota-negative", "v1-period-zero"}
        else v2_observations()
    )
    if case.startswith("owned-"):
        files = v2_observations(leaf="250000 100000")
    membership = Path("/proc/self/cgroup")
    mounts = Path("/proc/self/mountinfo")
    quota = Path("/cpu budgets/team/job/cpu.max")
    replacements = {
        "membership-malformed": (membership, "not membership"),
        "membership-missing": (membership, ""),
        "membership-duplicate": (
            membership,
            "0::/slice/team/job\n0::/slice/team/job\n",
        ),
        "membership-relative": (membership, "0::slice/team/job\n"),
        "membership-traversal": (membership, "0::/slice/../team/job\n"),
        "mount-malformed": (mounts, "not mountinfo"),
        "mount-unresolved": (mounts, "32 20 0:29 /other /cpu rw - cgroup2 cgroup rw\n"),
        "mount-prefix": (mounts, "32 20 0:29 /slic /cpu rw - cgroup2 cgroup rw\n"),
        "mount-relative": (mounts, "32 20 0:29 /slice cpu rw - cgroup2 cgroup rw\n"),
        "quota-empty": (quota, ""),
        "quota-zero": (quota, "0 100000"),
        "quota-negative": (quota, "-1 100000"),
        "quota-nonnumeric": (quota, "bad 100000"),
        "quota-extra": (quota, "max 100000 extra"),
        "period-zero": (quota, "max 0"),
        "v1-quota-negative": (Path("/cpu budgets/team/job/cpu.cfs_quota_us"), "-2"),
        "v1-period-zero": (Path("/cpu budgets/team/job/cpu.cfs_period_us"), "0"),
    }
    if case in replacements:
        path, text = replacements[case]
        files[path] = text
    primary = (
        KeyboardInterrupt("CPU observation interrupted")
        if case in {"affinity-interruption", "owned-interruption-close"}
        else SystemExit("CPU observation exited")
        if case == "affinity-exit"
        else RuntimeError("unexpected observation")
        if case in {"affinity-unexpected", "read-unexpected", "owned-unexpected-close"}
        else OSError(errno.EIO, "unexpected read")
        if case == "read-raw-io"
        else PermissionError(errno.EACCES, "CPU observation denied")
    )
    release = (
        KeyboardInterrupt("CPU close interrupted")
        if case == "owned-cleanup-interruption"
        else OSError(errno.EACCES, "CPU close denied")
    )
    unavailable = CapacityObservationUnavailable(
        "raw CPU observation unavailable", cause=primary
    )
    observations = []
    owned = case.startswith("owned-")
    operation_cases = {
        "owned-unavailable",
        "owned-operation-close",
        "owned-interruption-close",
        "owned-cleanup-interruption",
        "owned-unexpected-close",
    }
    close_cases = {
        "owned-operation-close",
        "owned-sole-close",
        "owned-interruption-close",
        "owned-cleanup-interruption",
        "owned-unexpected-close",
    }

    class FaultDescriptors(RecordingDescriptors):
        enabled = True

        def openat(self, directory_fd, name, flags, mode=0):
            assert not self.live
            return super().openat(directory_fd, name, flags, mode)

        def fstat(self, fd):
            info = super().fstat(fd)
            if self.enabled and case in operation_cases:
                raise primary
            return info

        def close(self, fd):
            super().close(fd)
            if self.enabled and case in close_cases:
                raise release

    ops = FaultDescriptors(tmp_path)
    if owned:
        for path, text in files.items():
            real = tmp_path / str(path).lstrip("/")
            real.parent.mkdir(parents=True, exist_ok=True)
            mcp_provenance.write(real, text.encode())
        if case in {"owned-missing", "owned-decode"}:
            real = tmp_path / "proc/self/cgroup"
            real.unlink()
            if case == "owned-decode":
                mcp_provenance.write(real, b"\xff")

    class RawSystem(PosixCapacitySystem):
        def affinity(self):
            observations.append("affinity")
            if case == "affinity-unavailable":
                raise unavailable
            if case in {
                "affinity-interruption",
                "affinity-exit",
                "affinity-unexpected",
            }:
                raise primary
            return (
                frozenset()
                if case == "affinity-empty"
                else frozenset({-1})
                if case == "affinity-negative"
                else frozenset({True})
                if case == "affinity-bool"
                else frozenset(range(6))
            )

        def read_text(self, path):
            observations.append(path)
            if case == "read-unavailable":
                raise unavailable
            if case in {"read-raw-io", "read-unexpected"}:
                raise primary
            assert path in files
            return (
                super().read_text(tmp_path / str(path).lstrip("/"))
                if owned
                else files[path]
            )

    system = RawSystem(descriptors=ops)
    error_cases = {
        "affinity-interruption",
        "affinity-exit",
        "affinity-unexpected",
        "read-raw-io",
        "read-unexpected",
        *close_cases,
    }
    if case in error_cases:
        expected = (
            release
            if case in {"owned-sole-close", "owned-cleanup-interruption"}
            else primary
        )
        for _ in range(2):
            with pytest.raises(BaseException) as caught:
                resolve_admission_capacity(workers=8, system=system)
            assert caught.value is expected
            if case in close_cases and case != "owned-sole-close":
                secondary = getattr(caught.value, "_transaction_secondary", ())
                assert any(
                    item
                    is (primary if case == "owned-cleanup-interruption" else release)
                    for item in secondary
                )
            assert_released(ops)
    else:
        result = resolve_admission_capacity(workers=8, system=system)
        assert result.capacity == (
            6 if case.startswith("unlimited-") else 2 if case == "owned-repeat" else 1
        )
        assert result.reason.strip()
        assert_released(ops)
        if case == "affinity-unavailable":
            assert unavailable.cause is primary
        if case in {"owned-missing", "owned-unavailable"}:
            with pytest.raises(CapacityObservationUnavailable) as caught_unavailable:
                system.read_text(membership)
            cause = caught_unavailable.value.cause
            assert isinstance(cause, OSError)
            assert cause.errno == (
                errno.ENOENT if case == "owned-missing" else errno.EACCES
            )
            if case == "owned-unavailable":
                assert cause is primary
            assert_released(ops)
    if case.startswith("affinity-"):
        assert set(observations) == {"affinity"}
    if owned:
        previous = list(ops.closed)
        ops.enabled = False
        if case in {"owned-missing", "owned-decode"}:
            path = tmp_path / "proc/self/cgroup"
            if case == "owned-decode":
                path.unlink()
            mcp_provenance.write(path, files[membership].encode())
        assert resolve_admission_capacity(workers=8, system=system).capacity == 2
        assert ops.closed[: len(previous)] == previous
        assert len(ops.closed) == len(previous) + len(files)
        assert_released(ops)


@pytest.mark.parametrize(
    "case",
    [
        "initial",
        "replacement",
        "numeric",
        "auto",
        "logical",
        "auto-cap",
        "logical-cap",
        "serial",
        "zero",
        "operation-close",
        "sole-close",
        "interruption",
        "repeat",
    ],
)
def test_session_workers_share_admission(tmp_path, case, monkeypatch):
    from tests import admission_session_support as session
    from tests.admission_support import SessionAdmission

    modes = {
        "numeric": (["-n", "2"], 2),
        "auto": (["-n", "auto"], 3),
        "logical": (["-n", "logical"], 3),
        "auto-cap": (["-n", "auto", "--maxprocesses", "2"], 2),
        "logical-cap": (["-n", "logical", "--maxprocesses", "2"], 2),
        "serial": ([], 1),
        "zero": (["-n", "0"], 1),
    }
    mode, workers = modes.get(case, (["-n", "1"], 1))
    faults = {"operation-close", "sole-close", "interruption", "repeat"}
    scenario = (
        "modes"
        if case in modes
        else "ready"
        if case in faults
        else "replacement"
        if case == "replacement"
        else "tracer"
    )
    for key, value in {
        "PYTEST_ADDOPTS": "-n 999",
        "PYTEST_PLUGINS": "missing_poison_plugin",
        "PYTHONSAFEPATH": "1",
    }.items():
        monkeypatch.setenv(key, value)
    inputs = session.prepare(tmp_path, mode=mode, scenario=scenario)
    assert (
        not {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSAFEPATH"}
        & inputs["env"].keys()
    )
    assert inputs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    workspace, base = inputs["workspace"], inputs["base"]
    assert workspace.is_absolute() and base.is_absolute()
    assert not any(
        workspace.is_relative_to(inputs["repository"] / name)
        for name in ("tests", "host_tests", "scripts")
    )
    provider = SessionAdmission(shared_pool=AdmissionPool(directory=base, capacity=1))
    with pytest.raises(FrozenInstanceError):
        # Deliberate mutation: session configuration is immutable as well.
        provider.shared_pool = AdmissionPool(directory=base, capacity=2)  # ty: ignore[invalid-assignment]
    assert not base.exists()
    events, failures, contexts, ops_list, owners, results = [], [], [], [], [], []
    streams = {stream: bytearray() for stream in ProcessStream}
    partial = bytearray()
    hold_windows = []
    hold_started = []
    primary = (
        KeyboardInterrupt("session harness interrupted")
        if case == "interruption"
        else OSError("session harness operation")
        if case == "operation-close"
        else None
    )
    release = OSError("session harness slot close")
    parent_release = OSError("session harness directory close")
    expected_decision = resolve_admission_capacity(workers=workers)

    def run():
        with ExitStack() as holds:
            current_hold = None
            started = None

            def chunk(stream, data):
                nonlocal current_hold, started
                streams[stream].extend(data)
                if stream is not ProcessStream.STDOUT:
                    return
                partial.extend(data)
                while b"\n" in partial:
                    raw, _, rest = partial.partition(b"\n")
                    partial[:] = rest
                    if not raw.startswith(b"ADMISSION_EVENT "):
                        continue
                    value = json.loads(raw.removeprefix(b"ADMISSION_EVENT "))
                    events.append(value)
                    if failures:
                        continue
                    try:
                        if value["kind"] == "ready":
                            assert value["capacity"] == expected_decision.capacity
                            assert Path(value["directory"]) == base
                            assert Path(value["base"]) == (
                                base
                                if case in {"serial", "zero"}
                                else base / ("popen-" + value["worker"])
                            )
                            if scenario == "modes":
                                continue
                            ops = RecordingDescriptors(base)
                            ops_list.append(ops)
                            pool = AdmissionPool(
                                directory=base,
                                capacity=1,
                                system=RecordingSystem(descriptors=ops),
                            )
                            current_hold = pool.acquire(deadline=time.monotonic() + 5)
                            holds.enter_context(current_hold)
                            contexts.append(current_hold)
                            started = time.monotonic()
                            hold_started.append(started)
                            assert any(
                                token[2] == "slot" for token in ops.live.values()
                            )
                            if case in faults - {"repeat"}:
                                ops.release, ops.parent_release = (
                                    release,
                                    parent_release,
                                )
                            if case == "interruption":
                                assert isinstance(primary, KeyboardInterrupt)
                                raise primary
                            if scenario != "ready":
                                mcp_provenance.write_observation(
                                    workspace / ("attempt-" + value["worker"]), b"go"
                                )
                        elif (
                            value["kind"] == "report"
                            and "test_timeout_" in value["nodeid"]
                            and value["outcome"] == "passed"
                        ):
                            assert current_hold is not None and started is not None
                            assert (
                                mcp_provenance.read(
                                    workspace / ("busy-" + value["worker"])
                                )
                                == b"busy"
                            )
                            current_hold.__exit__(None, None, None)
                            hold_windows.append(time.monotonic() - started)
                            assert_released(ops_list[-1])
                            mcp_provenance.write_observation(
                                workspace / ("progress-" + value["worker"]), b"go"
                            )
                        elif (
                            value["kind"] == "report"
                            and "test_progress_" in value["nodeid"]
                            and value["outcome"] == "passed"
                        ):
                            assert (
                                mcp_provenance.read(
                                    workspace / ("entered-" + value["worker"])
                                )
                                == b"entered"
                            )
                    except Exception as error:
                        failures.append(error)

            with OwnedProcess.launch(
                inputs["argv"],
                deadline_seconds=20,
                grace_seconds=2,
                popen_kwargs={"cwd": str(inputs["repository"]), "env": inputs["env"]},
            ) as child:
                owners.append(child)
                result = child.stream(chunk)
                results.append(result)
                if failures:
                    raise failures[0]
                if primary is not None:
                    raise primary

    if case in {"operation-close", "sole-close", "interruption"}:
        with pytest.raises(BaseException) as caught:
            run()
        assert caught.value is (primary or release)
        secondary = getattr(caught.value, "_transaction_secondary", ())
        assert any(error is parent_release for error in secondary)
        if primary is not None:
            assert any(error is release for error in secondary)
    else:
        run()
    if scenario == "ready":
        hold_windows.extend(time.monotonic() - start for start in hold_started)
    assert owners
    expected_failures: tuple[FailureInfo, ...] = ()
    if case == "interruption":
        assert isinstance(primary, KeyboardInterrupt)
        expected_failures = (
            FailureInfo(
                primary,
                FailureKind.INTERRUPTION,
                FailurePhase.CALLBACK,
                ProcessStream.STDOUT,
            ),
        )
    for owner in owners:
        assert owner.cleanup_status.complete
        assert owner.wait_cleanup(0).complete
        assert owner.shutdown().complete
        assert owner.cleanup_status.failures == expected_failures
    mcp_provenance.write(
        tmp_path / "child-stdout", bytes(streams[ProcessStream.STDOUT])
    )
    mcp_provenance.write(
        tmp_path / "child-stderr", bytes(streams[ProcessStream.STDERR])
    )
    mcp_provenance.write(
        tmp_path / "child-observation.json",
        mcp_provenance.canonical(
            {
                "argv": inputs["argv"],
                "cwd": str(inputs["repository"]),
                "workspace": str(workspace),
                "ini": str(inputs["ini"]),
                "basetemp": str(base),
                "env_keys": sorted(inputs["env"]),
                "PATH": inputs["env"].get("PATH"),
                "events": events,
                "statuses": [result.returncode for result in results],
                "capture_interrupted": case == "interruption",
                "hold_seconds": hold_windows,
                "execution_seconds": 20,
                "cleanup_seconds": 2,
                "handshake_seconds": 5,
                "restart_allowance": 1 if case == "replacement" else 0,
                "expected_crash_worker": "gw0" if case == "replacement" else None,
                "requested_worker_exit": 23 if case == "replacement" else None,
                "cleanup_complete": all(
                    owner.cleanup_status.complete for owner in owners
                ),
            }
        ),
    )
    for result in results:
        assert not result.timed_out and not result.lifecycle.failures
        assert all(
            outcome is StreamOutcome.EOF for _, outcome in result.stream_outcomes
        )
        assert result.returncode == (1 if case == "replacement" else 0), bytes(
            streams[ProcessStream.STDOUT]
        ).decode("utf-8", "replace")
    assert results or case == "interruption"
    for context, ops in zip(contexts, ops_list, strict=True):
        assert_released(ops)
        previous = list(ops.closed)
        context.__exit__(None, None, None)
        context.__exit__(None, None, None)
        assert ops.closed == previous
        ops.release = ops.parent_release = None
        with AdmissionPool(
            directory=base, capacity=1, system=RecordingSystem(descriptors=ops)
        ).acquire(deadline=time.monotonic() + 5):
            pass
        assert_released(ops)
    assert not failures
    if case in faults:
        assert len(contexts) == 1
        assert len(hold_windows) == 1 and 0 < hold_windows[0] < 22
        return
    ready = [event for event in events if event["kind"] == "ready"]
    assert len(ready) == (2 if case == "replacement" else workers)
    assert len({event["worker"] for event in ready}) == len(ready)
    if case in {"serial", "zero"}:
        assert not any(event["kind"] in {"planned", "delivery"} for event in events)
    else:
        assert [event["workers"] for event in events if event["kind"] == "planned"] == [
            workers
        ]
        delivered = [
            event["payload"] for event in events if event["kind"] == "delivery"
        ]
        assert len(delivered) == len(ready) and all(delivered)
        assert all(payload == delivered[0] for payload in delivered)
    if scenario in {"tracer", "replacement"}:
        assert len(hold_windows) == len(ready)
        assert all(0 < elapsed < 22 for elapsed in hold_windows)
        for item in ready:
            assert (
                mcp_provenance.read(workspace / ("entered-" + item["worker"]))
                == b"entered"
            )
        crashes = [
            event
            for event in events
            if event["kind"] == "down" and event["error"] is not None
        ]
        assert [(event["worker"]) for event in crashes] == (
            ["gw0"] if case == "replacement" else []
        )
        if case == "replacement":
            assert b"test_crash" in streams[ProcessStream.STDOUT]


@pytest.mark.parametrize(
    "case", ["chdir", "remote", "negative", "unresolved", "empty", "relative"]
)
def test_session_configuration_prerequisites(tmp_path, case):
    from tests import admission_session_support as session

    modes = {
        "chdir": ["--tx", "popen//chdir=/tmp"],
        "remote": ["--tx", "ssh=invalid-admission-host"],
        "negative": ["-n", "-1"],
    }
    inputs = session.prepare(
        tmp_path, mode=modes.get(case, ["-n", "1"]), scenario="ready"
    )
    source = session.CONFTEST
    if case == "unresolved":
        source = source.replace(
            "def pytest_configure(config):",
            '@pytest.hookimpl(tryfirst=True)\ndef pytest_configure(config):\n    config.option.numprocesses = "auto"',
        )
    elif case == "empty":
        source += '\n@pytest.hookimpl(specname="pytest_xdist_setupnodes", tryfirst=True)\ndef pytest_empty_nodes(config, specs):\n    specs.clear()\n'
    elif case == "relative":
        source += '\n@pytest.fixture(scope="session")\ndef tmp_path_factory():\n    class Factory:\n        def getbasetemp(self):\n            return Path("relative")\n    return Factory()\n'
    conftest = inputs["workspace"] / "conftest.py"
    conftest.unlink()
    mcp_provenance.write(conftest, source.encode())
    observation = mcp_wheel_support.command(
        inputs["argv"], cwd=inputs["repository"], env=inputs["env"], timeout_seconds=20
    )
    for stream in ("stdout", "stderr"):
        mcp_provenance.write(tmp_path / stream, observation[stream])
    mcp_provenance.write(
        tmp_path / "child-observation.json",
        mcp_provenance.canonical(
            {
                key: value
                for key, value in observation.items()
                if key not in {"stdout", "stderr"}
            }
        ),
    )
    assert observation["status"] != 0
    assert (
        b"invalid admission configuration:"
        in observation["stdout"] + observation["stderr"]
    )
    assert not list(inputs["workspace"].glob("ready-*.json"))
    assert b"NotImplementedError" not in observation["stdout"] + observation["stderr"]


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "payload-null",
        "payload-list",
        "version-missing",
        "version",
        "version-bool",
        "version-float",
        "version-string",
        "decision-missing",
        "decision-null",
        "decision-list",
        "capacity-missing",
        "capacity-zero",
        "capacity-negative",
        "capacity-bool",
        "capacity-float",
        "capacity-string",
        "capacity-null",
        "reason-missing",
        "reason-empty",
        "reason-blank",
        "reason-null",
        "reason-list",
        "decision-extra",
        "relative",
    ],
)
def test_invalid_session_configuration_fails_setup(tmp_path, case):
    from tests import admission_session_support as session

    changes = {
        "missing": "del node.workerinput['footman_admission']",
        "payload-null": "node.workerinput['footman_admission'] = None",
        "payload-list": "node.workerinput['footman_admission'] = []",
        "version-missing": "del payload['version']",
        "version": "payload['version'] = 2",
        "version-bool": "payload['version'] = True",
        "version-float": "payload['version'] = 1.0",
        "version-string": "payload['version'] = '1'",
        "decision-missing": "del payload['decision']",
        "decision-null": "payload['decision'] = None",
        "decision-list": "payload['decision'] = []",
        "capacity-missing": "del payload['decision']['capacity']",
        "capacity-zero": "payload['decision']['capacity'] = 0",
        "capacity-negative": "payload['decision']['capacity'] = -1",
        "capacity-bool": "payload['decision']['capacity'] = True",
        "capacity-float": "payload['decision']['capacity'] = 1.0",
        "capacity-string": "payload['decision']['capacity'] = '1'",
        "capacity-null": "payload['decision']['capacity'] = None",
        "reason-missing": "del payload['decision']['reason']",
        "reason-empty": "payload['decision']['reason'] = ''",
        "reason-blank": "payload['decision']['reason'] = '  '",
        "reason-null": "payload['decision']['reason'] = None",
        "reason-list": "payload['decision']['reason'] = []",
        "decision-extra": "payload['decision']['extra'] = 1",
        "relative": "pass",
    }
    observations = []
    for variant in ("control", "invalid"):
        root = tmp_path / variant
        root.mkdir()
        inputs = session.prepare(root, mode=["-n", "1"], scenario="work")
        inputs["argv"] += ["--max-worker-restart", "0"]
        if variant == "invalid":
            mutation = (
                """
    payload = node.workerinput['footman_admission']
    if not (payload['version'] == 1 and type(payload['decision']['capacity']) is int and payload['decision']['capacity'] > 0 and payload['decision']['reason'].strip()):
        raise AssertionError('uncorrupted transfer prerequisite failed')
    p.write_observation(ROOT / 'original-delivery.json', p.canonical(payload))
"""
                + "    "
                + changes[case]
                + "\n"
            )
            source = session.CONFTEST.replace(
                "def pytest_configure_node(node):\n",
                "def pytest_configure_node(node):\n" + mutation,
            )
            if case == "relative":
                source += '\n@pytest.fixture(scope="session")\ndef tmp_path_factory():\n    class Factory:\n        def getbasetemp(self):\n            return Path("relative")\n    return Factory()\n'
            conftest = inputs["workspace"] / "conftest.py"
            conftest.unlink()
            mcp_provenance.write(conftest, source.encode())
        observation = mcp_wheel_support.command(
            inputs["argv"],
            cwd=inputs["repository"],
            env=inputs["env"],
            timeout_seconds=20,
        )
        for stream in ("stdout", "stderr"):
            mcp_provenance.write(root / stream, observation[stream])
        metadata = {
            key: value
            for key, value in observation.items()
            if key not in {"stdout", "stderr"}
        }
        metadata.update(
            {
                "basetemp": str(inputs["base"]),
                "workspace": str(inputs["workspace"]),
                "ini": str(inputs["ini"]),
                "variant": variant,
                "case": case,
            }
        )
        mcp_provenance.write(
            root / "child-observation.json", mcp_provenance.canonical(metadata)
        )
        observations.append(metadata)
        if variant == "control":
            assert observation["status"] == 0, (
                observation["stdout"] + observation["stderr"]
            )
            assert (
                mcp_provenance.read(inputs["workspace"] / "protected-work")
                == b"entered"
            )
            ready = mcp_provenance.load(inputs["workspace"] / "ready-gw0.json")
            assert ready["directory"] == str(inputs["base"]) and ready["capacity"] == 1
        else:
            original = mcp_provenance.load(
                inputs["workspace"] / "original-delivery.json"
            )
            assert original["version"] == 1 and original["decision"]["capacity"] == 1
            assert observation["status"] != 0
            assert (
                b"invalid admission configuration:"
                in observation["stdout"] + observation["stderr"]
            )
            assert not list(inputs["workspace"].glob("ready-*.json"))
            assert not (inputs["workspace"] / "protected-work").exists()
    assert observations[0]["basetemp"] != observations[1]["basetemp"]


@pytest.mark.parametrize(
    "case",
    [
        "direct",
        "missing",
        "attribution-only",
        "metadata-operation-close",
        "flock-interruption",
        "body-operation-close",
        "sole-close",
        "repeat",
        "nested",
        "nested-operation-close",
        "nested-sole-close",
        "nested-interruption",
        "nested-repeat",
    ],
)
def test_worker_installation_uses_session_admission(
    tmp_path, tmp_path_factory, monkeypatch, admission, case
):
    from tests import admission_installation_support as installation
    from tests import admission_session_support as session
    from tests import mcp_contract_support as support

    # Public tmp factory gives short, exclusively owned launcher paths, separate
    # from the long per-test namespace and the invocation's actual shared pool.
    storage = tmp_path_factory.mktemp("ap13")
    assert admission.shared_pool.directory != tmp_path
    primary = (
        KeyboardInterrupt("installation interrupted")
        if "interruption" in case
        else OSError("installation operation")
    )
    release, parent_release = (
        OSError("installation slot close"),
        OSError("installation parent close"),
    )
    if case.startswith("nested"):
        inputs = session.prepare(tmp_path, mode=["-n", "1"])
        source = installation.SOURCE.replace(
            "Path('__INSTALLATION_ROOT__')", f"Path({str(storage)!r})"
        ).replace("Path('__REPOSITORY__')", f"Path({str(inputs['repository'])!r})")
        if case != "nested":
            source = source[: source.index("def test_timeout(")]
        conftest = session.CONFTEST.replace(
            "def initialized(admission, tmp_path_factory, request):",
            "def initialized(admission, tmp_path_factory, request, installation_inputs):",
        )
        for path, text in (
            (inputs["workspace"] / "test_session.py", source),
            (inputs["workspace"] / "conftest.py", conftest),
        ):
            path.unlink()
            mcp_provenance.write(path, text.encode())
        events, errors, ops_list, contexts, owners, results = [], [], [], [], [], []
        data = {stream: bytearray() for stream in ProcessStream}
        partial = bytearray()
        started, elapsed = [], []

        def run():
            with ExitStack() as stack:

                def chunk(stream, payload):
                    data[stream].extend(payload)
                    if stream is not ProcessStream.STDOUT:
                        return
                    partial.extend(payload)
                    while b"\n" in partial:
                        line, _, rest = partial.partition(b"\n")
                        partial[:] = rest
                        if not line.startswith(b"ADMISSION_EVENT "):
                            continue
                        event = json.loads(line.removeprefix(b"ADMISSION_EVENT "))
                        events.append(event)
                        if errors:
                            continue
                        try:
                            if event["kind"] == "ready":
                                assert (
                                    event["directory"] == str(inputs["base"])
                                    and event["capacity"] == 1
                                )
                                assert (storage / "inputs/src").is_dir() and (
                                    storage / "install"
                                ).is_dir()
                                ops = RecordingDescriptors(inputs["base"])
                                ops_list.append(ops)
                                pool = AdmissionPool(
                                    directory=inputs["base"],
                                    capacity=1,
                                    system=RecordingSystem(descriptors=ops),
                                )
                                context = pool.acquire(deadline=time.monotonic() + 5)
                                stack.enter_context(context)
                                contexts.append(context)
                                started.append(time.monotonic())
                                if case in {
                                    "nested-operation-close",
                                    "nested-sole-close",
                                    "nested-interruption",
                                }:
                                    ops.release, ops.parent_release = (
                                        release,
                                        parent_release,
                                    )
                                if case == "nested-interruption":
                                    raise primary
                                if case == "nested":
                                    mcp_provenance.write_observation(
                                        inputs["workspace"]
                                        / ("attempt-" + event["worker"]),
                                        b"go",
                                    )
                            elif (
                                event["kind"] == "report"
                                and event["nodeid"].endswith("::test_timeout")
                                and event["outcome"] == "passed"
                            ):
                                assert (
                                    mcp_provenance.read(
                                        inputs["workspace"]
                                        / ("busy-" + event["worker"])
                                    )
                                    == b"busy"
                                )
                                contexts[-1].__exit__(None, None, None)
                                assert_released(ops_list[-1])
                                elapsed.append(time.monotonic() - started[-1])
                                mcp_provenance.write_observation(
                                    inputs["workspace"]
                                    / ("progress-" + event["worker"]),
                                    b"go",
                                )
                        except Exception as error:
                            errors.append(error)

                with OwnedProcess.launch(
                    inputs["argv"],
                    deadline_seconds=20,
                    grace_seconds=2,
                    popen_kwargs={
                        "cwd": str(inputs["repository"]),
                        "env": inputs["env"],
                    },
                ) as owner:
                    owners.append(owner)
                    results.append(owner.stream(chunk))
                    if errors:
                        raise errors[0]
                    if case == "nested-operation-close":
                        raise primary

        if case in {
            "nested-operation-close",
            "nested-sole-close",
            "nested-interruption",
        }:
            with pytest.raises(BaseException) as caught:
                run()
            assert caught.value is (release if case == "nested-sole-close" else primary)
            secondary = getattr(caught.value, "_transaction_secondary", ())
            assert any(error is parent_release for error in secondary)
            if case != "nested-sole-close":
                assert any(error is release for error in secondary)
        else:
            run()
        for owner in owners:
            expected = (
                (
                    FailureInfo(
                        primary,
                        FailureKind.INTERRUPTION,
                        FailurePhase.CALLBACK,
                        ProcessStream.STDOUT,
                    ),
                )
                if case == "nested-interruption"
                else ()
            )
            assert owner.wait_cleanup(0).complete and owner.shutdown().complete
            assert owner.cleanup_status.failures == expected
        for stream in ProcessStream:
            mcp_provenance.write(
                tmp_path / ("child-" + stream.name.lower()), bytes(data[stream])
            )
        mcp_provenance.write(
            tmp_path / "child-observation.json",
            mcp_provenance.canonical(
                {
                    "argv": inputs["argv"],
                    "cwd": str(inputs["repository"]),
                    "W": str(inputs["workspace"]),
                    "I": str(inputs["ini"]),
                    "B": str(inputs["base"]),
                    "storage": str(storage),
                    "events": events,
                    "statuses": [result.returncode for result in results],
                    "hold_seconds": elapsed,
                    "execution_seconds": 20,
                    "cleanup_seconds": 2,
                    "gate_seconds": 5,
                    "timeout_seconds": 0.2,
                    "progress_seconds": 15,
                    "interrupted_capture": case == "nested-interruption",
                    "cleanup_complete": all(
                        owner.cleanup_status.complete for owner in owners
                    ),
                }
            ),
        )
        for context, ops in zip(contexts, ops_list, strict=True):
            assert_released(ops)
            previous = list(ops.closed)
            context.__exit__(None, None, None)
            context.__exit__(None, None, None)
            assert ops.closed == previous
            ops.release = ops.parent_release = None
            with AdmissionPool(
                directory=inputs["base"],
                capacity=1,
                system=RecordingSystem(descriptors=ops),
            ).acquire(deadline=time.monotonic() + 5):
                pass
            assert_released(ops)
        assert len(owners) == len(contexts) == 1 and not errors
        for result in results:
            assert result.returncode == 0, bytes(data[ProcessStream.STDOUT]).decode(
                "utf-8", "replace"
            )
            assert (
                not result.timed_out
                and result.lifecycle.complete
                and not result.lifecycle.failures
            )
            assert all(
                outcome is StreamOutcome.EOF for _, outcome in result.stream_outcomes
            )
        assert results or case == "nested-interruption"
        if case == "nested":
            assert len(elapsed) == 1 and 0 < elapsed[0] < 22
            assert (
                mcp_provenance.read(inputs["workspace"] / "entered-gw0") == b"installed"
            )
        return

    prepared = support.prepare_inputs(
        Path(__file__).resolve().parents[1], storage / "inputs"
    )
    evidence = storage / "install"
    evidence.mkdir()
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = RecordingDescriptors(namespace)
    pool = AdmissionPool(
        directory=namespace, capacity=1, system=RecordingSystem(descriptors=ops)
    )
    caller = mcp_wheel_support.build_and_install_contract_wheel
    if case == "missing":
        with pytest.raises(ValueError):
            caller(prepared, admission=None, evidence_directory=evidence)
        assert (
            not ops.events
            and not (prepared / "dist").exists()
            and not list(evidence.iterdir())
        )
        return
    if case == "attribution-only":
        from tests import mcp_attribution_support as attribution
        from tests.wheel_attribution_cases import case as scripted_case

        scenario = scripted_case("success")
        executable, record = attribution.scripted_uv(storage, scenario)
        env = mcp_wheel_support.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        )
        with pool.acquire(deadline=time.monotonic() + 5):
            result = caller(
                prepared,
                admission=None,
                evidence_directory=evidence,
                attribution_only=True,
                env=env,
            )
            assert result.command["status"] == 0 and record.is_file()
            mcp_provenance.write(
                storage / "attribution.json",
                mcp_provenance.canonical(installation.commands((result.command,))),
            )
            assert sum(token[2] == "slot" for token in ops.opened) == 1
        assert not (evidence / "venv").exists()
        assert_released(ops)
        return
    if case in {"direct", "repeat"}:
        with pool.acquire(deadline=time.monotonic() + 5):
            with pytest.raises(TimeoutError):
                caller(
                    prepared,
                    admission=pool,
                    evidence_directory=evidence,
                    timeout_seconds=0.2,
                )
            assert ("busy", "slot") in ops.events
            assert not (prepared / "dist").exists() and not list(evidence.iterdir())
        assert_released(ops)
        result = caller(prepared, admission=pool, evidence_directory=evidence)
        assert result.command["status"] == 0 and result.environment == evidence / "venv"
        assert result.environment.is_dir() and result.support_root.is_dir()
        mcp_provenance.write(
            storage / "installation.json",
            mcp_provenance.canonical(installation.observation(result)),
        )
        assert_released(ops)
    else:
        if case.startswith("metadata"):
            ops.point, ops.primary = "metadata", primary
        elif case.startswith("flock"):
            ops.point, ops.primary = "flock", primary
        ops.release, ops.parent_release = release, parent_release
        original_command = mcp_wheel_support.command
        completed = []

        def work(argv, **kwargs):
            if case == "body-operation-close":
                raise primary
            result = original_command(argv, **kwargs)
            completed.append(result)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(mcp_wheel_support, "command", work)
            with pytest.raises(BaseException) as caught:
                caller(prepared, admission=pool, evidence_directory=evidence)
        assert caught.value is (release if case == "sole-close" else primary)
        secondary = getattr(caught.value, "_transaction_secondary", ())
        assert any(error is parent_release for error in secondary)
        if case != "sole-close":
            assert any(error is release for error in secondary)
        if case.startswith(("metadata", "flock")):
            assert not (prepared / "dist").exists() and not list(evidence.iterdir())
        mcp_provenance.write(
            storage / "commands.json",
            mcp_provenance.canonical(installation.commands(completed)),
        )
        assert_released(ops)
    ops.point = ops.primary = ops.release = ops.parent_release = None
    context = pool.acquire(deadline=time.monotonic() + 5)
    with context:
        pass
    previous = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == previous
    assert_released(ops)
