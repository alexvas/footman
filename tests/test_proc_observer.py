"""Narrow proc observation: synthetic owners are not liveness evidence."""

import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from footman.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
)
from tests import mcp_provenance as p
from tests.acceptance_portability_support import incarnation


def test_proc_observer_is_registered_as_standalone_suite():
    command = (
        ".venv/bin/python -m pytest tests/test_proc_observer.py "
        "-n 2 --dist worksteal --tb=short -q"
    )
    documentation = p.read(
        Path(__file__).resolve().parents[1] / "docs/testing.md"
    ).decode()
    assert documentation.count(command) == 1


@pytest.fixture
def proc_io(monkeypatch):
    closed = []
    owners = []
    directories = []
    failures = {}
    requirements = []

    def close(fd):
        closed.append(fd)
        if fd in failures:
            raise failures[fd]

    ops = SimpleNamespace(
        close=close,
        fstat=lambda fd: os.stat_result(
            (stat.S_IFDIR | 0o555, 1, 2, 1, 65534, 65534, 0, 0, 0, 0)
        ),
    )

    def directory(operations, path, *, require_owner):
        assert operations is ops and Path(path) == Path("/proc/999")
        requirements.append(require_owner)
        owner = DirectoryDescriptor.adopt(
            ops, 100, label=str(path), require_owner=require_owner
        )
        directories.append(owner)
        return owner

    def owned(operations, fd, *, label):
        owner = OwnedDescriptor(operations, fd, label=label)
        owners.append(owner)
        return owner

    monkeypatch.setattr(p, "OPS", ops)
    monkeypatch.setattr(DirectoryDescriptor, "open_secure_path", directory)
    monkeypatch.setattr(p, "OwnedDescriptor", owned)
    opened = Mock(return_value=101)
    read = Mock(side_effect=[b"999 (name) S " + b"0 " * 18 + b"123 0", b""])
    monkeypatch.setattr(p.os, "open", opened)
    monkeypatch.setattr(
        p.os,
        "fstat",
        lambda fd: os.stat_result(
            (stat.S_IFREG | 0o444, 3, 2, 1, 65534, 65534, 0, 0, 0, 0)
        ),
    )
    monkeypatch.setattr(p.os, "read", read)
    yield SimpleNamespace(
        closed=closed,
        failures=failures,
        opened=opened,
        read=read,
        requirements=requirements,
    )
    # Every independent owner was already released, including on operation error.
    prior = list(closed)
    for owner in owners + directories:
        owner.close()
        owner.close()
    assert closed == prior


@pytest.mark.parametrize("ticks,expected", [("123", "S"), ("456", "absent")])
def test_foreign_proc_owner_does_not_mean_absence(proc_io, ticks, expected):
    assert incarnation({"pid": 999, "incarnation": ticks}) == expected
    assert proc_io.requirements == [False]
    assert proc_io.closed == [101, 100]


def test_normal_read_keeps_ownership_guard(proc_io):
    with pytest.raises(DescriptorError, match="not owned"):
        p.read(Path("/proc/999/stat"))
    proc_io.opened.assert_not_called()
    assert proc_io.requirements == [True]
    assert proc_io.closed == [100]


@pytest.mark.parametrize("number", [errno.ENOENT, errno.ESRCH])
def test_only_leaf_open_disappearance_is_absence(proc_io, number):
    proc_io.opened.side_effect = OSError(number, "gone", "stat")
    assert incarnation({"pid": 999, "incarnation": "123"}) == "absent"
    assert proc_io.closed == [100]
    proc_io.read.assert_not_called()


@pytest.mark.parametrize("phase", ["open", "read"])
@pytest.mark.parametrize(
    "number,filename",
    [
        (errno.EACCES, "stat"),
        (errno.ESRCH, "other"),
        (errno.EIO, "stat"),
        (errno.ESRCH, "stat"),
    ],
)
def test_unknown_errors_stay_unknown(proc_io, phase, number, filename):
    if phase == "open" and number == errno.ESRCH and filename == "stat":
        # An ESRCH read is unknown even though an ESRCH leaf open is absent.
        phase = "read"
    error = OSError(number, "unknown", filename)
    (proc_io.opened if phase == "open" else proc_io.read).side_effect = error
    with pytest.raises(OSError) as caught:
        incarnation({"pid": 999, "incarnation": "123"})
    assert caught.value is error
    assert proc_io.closed == ([100] if phase == "open" else [101, 100])


@pytest.mark.parametrize(
    "kind", ["operation-close", "sole-close", "interruption", "absent-close"]
)
def test_proc_read_cleanup_authority_and_failure_precedence(proc_io, kind):
    proc_io.failures.update(
        {
            100: ProcessLookupError(errno.ESRCH, "directory close", "stat"),
            101: ProcessLookupError(errno.ESRCH, "file close", "stat"),
        }
    )
    if kind == "absent-close":
        proc_io.opened.side_effect = ProcessLookupError(errno.ESRCH, "gone", "stat")
    primary = (
        KeyboardInterrupt("interrupted")
        if kind == "interruption"
        else OSError("read failed")
    )
    if kind in {"operation-close", "interruption"}:
        proc_io.read.side_effect = primary
        with pytest.raises(type(primary)) as caught:
            incarnation({"pid": 999, "incarnation": "123"})
        assert caught.value is primary
    else:
        with pytest.raises(OSError) as caught:
            incarnation({"pid": 999, "incarnation": "123"})
        assert caught.value is proc_io.failures[100 if kind == "absent-close" else 101]
    assert proc_io.closed == ([100] if kind == "absent-close" else [101, 100])
