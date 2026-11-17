"""Fixture-only no-follow witnesses and once-only, allowlisted reconciliation."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from tests.host_acceptance_assertions import require

OPS = PosixDescriptorOps()


def identity(info: Any) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def file_digest(parent: DirectoryDescriptor, name: str, expected: Any) -> str:
    label = "host cleanup witness file"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(parent.child_basename(name), flags, dir_fd=parent.fd)
    with OwnedDescriptor(OPS, fd, label=label) as owner:
        info = os.fstat(owner.fd)
        require(
            stat.S_ISREG(info.st_mode) and identity(info) == identity(expected),
            "cleanup witness file identity changed",
        )
        digest = hashlib.sha256()
        while chunk := os.read(owner.fd, 65536):
            digest.update(chunk)
        after = os.fstat(owner.fd)
        require(
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            == (info.st_size, info.st_mtime_ns, info.st_ctime_ns),
            "cleanup witness file changed during read",
        )
        return digest.hexdigest()


def entry(parent: DirectoryDescriptor, name: str) -> dict[str, Any]:
    info = parent.stat_child(name)
    result: dict[str, Any] = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
    }
    if stat.S_ISDIR(info.st_mode):
        result["kind"] = "directory"
    elif stat.S_ISREG(info.st_mode):
        result.update(kind="file", sha256=file_digest(parent, name, info))
    elif stat.S_ISLNK(info.st_mode):
        result.update(kind="link", target=os.readlink(name, dir_fd=parent.fd))
    else:
        raise AssertionError("unsupported cleanup witness entry")
    return result


def inventory(parent: DirectoryDescriptor, name: str) -> dict[str, Any]:
    result = entry(parent, name)
    require(result["kind"] == "directory", "cleanup witness root is not a directory")
    with parent.open_directory(name) as child:
        require(
            identity(os.fstat(child.fd)) == (result["device"], result["inode"]),
            "cleanup witness directory identity changed",
        )
        entries = {}
        for leaf in sorted(child.list_names()):
            observed = entry(child, leaf)
            if observed["kind"] == "directory":
                observed = inventory(child, leaf)
            entries[leaf] = observed
        result["entries"] = entries
    return result


def remove(parent: DirectoryDescriptor, name: str, expected: dict[str, Any]) -> None:
    require(
        entry(parent, name) == {k: v for k, v in expected.items() if k != "entries"},
        "cleanup witness entry changed",
    )
    if expected["kind"] != "directory":
        parent.unlink_child(name)
        return
    with parent.open_directory(name) as child:
        require(
            identity(os.fstat(child.fd)) == (expected["device"], expected["inode"]),
            "cleanup witness directory identity changed",
        )
        require(
            sorted(child.list_names()) == sorted(expected["entries"]),
            "unrecorded fixture descendants",
        )
        for leaf, value in expected["entries"].items():
            remove(child, leaf, value)
        require(not child.list_names(), "unrecorded fixture descendants")
    require(
        entry(parent, name) == {k: v for k, v in expected.items() if k != "entries"},
        "cleanup witness directory replaced",
    )
    parent.remove_child_directory(name)


class RecordedTree:
    """A successful reservation supplies identity; enumeration grants no ownership.

    This is explicit fixture reconciliation after a failed runtime deletion, not
    a retry of SnapshotReservation.remove or its terminal descriptor releases.
    Evidence/witnesses are kept outside the deletion target by the caller.
    """

    def __init__(self, path: Path, reserved_identity: tuple[int, int]):
        self.path = path
        self.parent_identity: tuple[int, int]
        self.attempted = False
        self.failure: BaseException | None = None
        self.removed = False
        with DirectoryDescriptor.open_secure_path(OPS, path.parent) as parent:
            self.parent_identity = identity(os.fstat(parent.fd))
            self.witness = inventory(parent, path.name)
            require(
                (self.witness["device"], self.witness["inode"]) == reserved_identity,
                "fixture reservation identity mismatch",
            )

    def check(self) -> None:
        with DirectoryDescriptor.open_secure_path(OPS, self.path.parent) as parent:
            require(
                identity(os.fstat(parent.fd)) == self.parent_identity,
                "fixture parent replaced",
            )
            require(
                inventory(parent, self.path.name) == self.witness,
                "fixture tree changed",
            )

    def finish(self) -> None:
        if self.attempted:
            if self.failure is not None:
                raise self.failure
            return
        self.attempted = True
        try:
            self.check()
            with DirectoryDescriptor.open_secure_path(OPS, self.path.parent) as parent:
                require(
                    identity(os.fstat(parent.fd)) == self.parent_identity,
                    "fixture parent replaced",
                )
                remove(parent, self.path.name, self.witness)
            require(not os.path.lexists(self.path), "fixture tree still present")
            self.removed = True
        except BaseException as error:
            self.failure = error
            raise
