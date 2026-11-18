"""Public pool contracts; faults delegate real OS release through sole owners."""

import errno
import json
import os
import socket
import stat
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from footman.filesystem.descriptors import DescriptorError, UnsafeDescriptorError
from footman.filesystem.operations import PosixDescriptorOps
from footman.filesystem.streams import OwnedStream
from footman.process import OwnedProcess, ProcessStream, StreamOutcome
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
