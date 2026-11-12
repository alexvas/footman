"""Optional bootstrap-only accounting; observing an incarnation grants no release authority."""

# Loaded by the standalone bootstrap without inserting its directory into sys.path.
import importlib.util
import math
import os
import re
import select
import sys
import time
from pathlib import Path

import anyio

from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps

_spec = importlib.util.spec_from_file_location(
    "mcp_lifecycle_provenance", Path(__file__).with_name("mcp_provenance.py")
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("missing lifecycle support")
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)
OPS = PosixDescriptorOps()
FIELDS = {
    "schema_version",
    "scratch_root",
    "journal_path",
    "instance_token",
    "service_deadline_monotonic",
    "settlement_allowance_seconds",
}


def validate_input(path, *, forbidden=(), binding=None):
    value = p.load(path)
    p.require(
        isinstance(value, dict) and set(value) == FIELDS, "invalid lifecycle fields"
    )
    p.require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "invalid lifecycle schema",
    )
    p.require(
        isinstance(value["instance_token"], str)
        and re.fullmatch(r"[0-9a-f]{32}", value["instance_token"]),
        "invalid instance token",
    )
    for name in ("service_deadline_monotonic", "settlement_allowance_seconds"):
        item = value[name]
        p.require(
            type(item) in {int, float} and math.isfinite(item) and item > 0,
            "invalid lifecycle budget",
        )
    now = time.monotonic()
    p.require(
        now < value["service_deadline_monotonic"] <= now + 60
        and value["settlement_allowance_seconds"] <= 5,
        "expired or extended lifecycle budget",
    )
    for name in ("scratch_root", "journal_path"):
        p.require(
            isinstance(value[name], str) and Path(value[name]).is_absolute(),
            "lifecycle paths must be absolute",
        )
    root, journal = Path(value["scratch_root"]), Path(value["journal_path"])
    p.require(
        root == root.resolve()
        and journal.is_relative_to(root)
        and journal != root
        and ".." not in journal.parts,
        "journal outside scratch",
    )
    p.require(
        journal not in {Path(path), *(Path(item) for item in forbidden)},
        "journal aliases an input",
    )
    with DirectoryDescriptor.open_secure_path(OPS, journal.parent) as parent:
        try:
            parent.stat_child(journal.name)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("journal destination already exists")
    if binding is not None:
        p.require(
            value == binding, "lifecycle input differs from original launch binding"
        )
    return value


class Journal:
    def __init__(self, value):
        self.value = value
        self.owner = None
        self.parent = None
        self.finished = False
        self.failure = None

    def publish(self, status):
        record = {
            "schema_version": 1,
            "instance_token": self.value["instance_token"],
            "pid": os.getpid(),
            "incarnation": p.read(Path(f"/proc/{os.getpid()}/stat"))
            .rsplit(b")", 1)[1]
            .split()[19]
            .decode("ascii"),
            "status": status,
            "service_deadline_monotonic": self.value["service_deadline_monotonic"],
            "settlement_allowance_seconds": self.value["settlement_allowance_seconds"],
        }
        data = p.canonical(record) + b"\n"
        remaining = memoryview(data)
        while remaining:
            count = os.write(self.owner.fd, remaining)
            p.require(count > 0, "journal write made no progress")
            remaining = remaining[count:]
        os.fsync(self.owner.fd)
        os.fsync(self.parent.fd)
        p.require(
            p.read(Path(self.value["journal_path"])).endswith(data),
            "journal readback mismatch",
        )

    def finish(self, primary=None):
        if self.finished:
            if self.failure is not None:
                raise self.failure
            return
        self.finished = True
        failures = CleanupFailures(primary)
        if primary is None and self.owner is not None:
            failures.run(
                lambda: self.publish("service-returned"), ordinary=(Exception,)
            )
        if self.owner is not None:
            failures.run(self.owner.close, ordinary=(OSError,))
        if self.parent is not None:
            failures.run(self.parent.close, ordinary=(OSError,))
        try:
            self.failure = failures.complete()
        except BaseException as error:
            self.failure = error
            raise
        if self.failure is not None:
            raise self.failure


def start(path, *, forbidden=()):
    value = validate_input(path, forbidden=forbidden)
    journal = Journal(value)
    destination = Path(value["journal_path"])
    try:
        journal.parent = DirectoryDescriptor.open_secure_path(OPS, destination.parent)
        label = "test bootstrap lifecycle journal"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(
            journal.parent.child_basename(destination.name),
            flags,
            0o600,
            dir_fd=journal.parent.fd,
        )
        journal.owner = OwnedDescriptor(OPS, fd, label=label)
        journal.publish("started-before-dispatch")
    except BaseException as error:
        journal.finish(error)
        raise
    return journal


class BorrowedInput:
    def __init__(self, original, journal):
        self.buffer = DeadlineReader(
            original.buffer, journal.value["service_deadline_monotonic"]
        )


class DeadlineReader:
    def __init__(self, borrowed, deadline):
        self.borrowed = borrowed
        self.deadline = deadline
        self.pending = bytearray()

    def readline(self):
        if time.monotonic() >= self.deadline:
            return b""
        while b"\n" not in self.pending:
            anyio.from_thread.check_cancelled()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                # The same server input reader observes EOF; no competing reader.
                return b""
            # Keep the shielded worker joined, but let SDK task cancellation
            # stop it without EOF or a renewed lifecycle budget.
            if not select.select(
                [self.borrowed.fileno()], [], [], min(remaining, 0.05)
            )[0]:
                continue
            chunk = os.read(self.borrowed.fileno(), 65536)
            if not chunk:
                data, self.pending = bytes(self.pending), bytearray()
                return data
            self.pending.extend(chunk)
        line, _, rest = self.pending.partition(b"\n")
        self.pending[:] = rest
        return bytes(line) + b"\n"


def borrowed_input(original, journal):
    return BorrowedInput(original, journal)
