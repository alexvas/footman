"""Exclusive staged snapshots; Git policy stays outside lifecycle primitives.

A reservation owns capabilities, not permission to delete arbitrary paths.
Close releases handles only. Failed/partial snapshots remain for reconciliation;
container-aware deletion belongs to the later run supervisor.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
)
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import FailureKind, OwnedProcess, ProcessStream, StreamOutcome

_RUN_ID = re.compile(r"[0-7][0123456789abcdefghjkmnpqrstvwxyz]{25}\Z")
_PRIVATE_INDEX = ".footman-index"
_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW


class SnapshotError(Exception):
    """A snapshot cannot safely represent the captured staged tree."""

    cleanup: SnapshotReservation | None = None


@dataclass(frozen=True)
class CapturedTree:
    tree_id: str
    object_format: str
    head: str | None


@dataclass(frozen=True)
class TreeEntry:
    path: tuple[str, ...]
    mode: str
    oid: str


def _oid(value: str, object_format: str) -> str:
    width = {"sha1": 40, "sha256": 64}.get(object_format)
    if width is None or re.fullmatch(r"[0-9a-f]{" + str(width) + r"}", value) is None:
        raise SnapshotError("invalid Git object identity/format")
    return value


class TreeEntries:
    """Incremental NUL-delimited ls-tree parser, preserving filesystem bytes."""

    def __init__(self, object_format: str) -> None:
        self.object_format = object_format
        self.pending = bytearray()
        self.entries: dict[tuple[str, ...], TreeEntry] = {}

    def feed(self, data: bytes) -> None:
        self.pending.extend(data)
        while (end := self.pending.find(b"\0")) >= 0:
            record = bytes(self.pending[:end])
            del self.pending[: end + 1]
            try:
                header, name = record.split(b"\t", 1)
                mode, kind, oid = header.decode("ascii").split(" ")
            except (ValueError, UnicodeError) as error:
                raise SnapshotError("malformed Git tree entry") from error
            parts = tuple(os.fsdecode(part) for part in name.split(b"/"))
            if any(
                not part or part in (".", "..") or part.lower() == ".git"
                for part in parts
            ):
                raise SnapshotError("unsafe Git tree path")
            if parts in self.entries or any(
                len(os.fsencode(part)) > 255 for part in parts
            ):
                raise SnapshotError("duplicate or unsupported Git tree path")
            expected = {
                "040000": "tree",
                "100644": "blob",
                "100755": "blob",
                "120000": "blob",
            }
            if mode == "160000":
                raise SnapshotError("gitlink export is unsupported")
            if mode not in expected or kind != expected[mode]:
                raise SnapshotError("unsupported Git entry mode/type")
            self.entries[parts] = TreeEntry(parts, mode, _oid(oid, self.object_format))
        if len(self.pending) > 1048576:
            raise SnapshotError("unsupported Git tree path length")

    def finish(self) -> None:
        if self.pending:
            raise SnapshotError("unterminated Git tree entry")
        for path in self.entries:
            for depth in range(1, len(path)):
                ancestor = self.entries.get(path[:depth])
                if ancestor is None or ancestor.mode != "040000":
                    raise SnapshotError("unsafe Git tree path ancestry")


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("snapshot write made no progress")
        remaining = remaining[written:]


def _validate_index(data: bytes, object_format: str) -> None:
    """Validate checksum and parse extensions, without guessing from blob bytes."""
    width = {"sha1": 20, "sha256": 32}[object_format]
    if len(data) < 12 + width or data[:4] != b"DIRC":
        raise SnapshotError("invalid captured index header")
    checksum = data[-width:]
    # Git's index.skipHash writes an all-zero checksum as a supported format,
    # not corruption. Open-version size/time checks still cover that capture.
    if (
        checksum != bytes(width)
        and hashlib.new(object_format, data[:-width]).digest() != checksum
    ):
        raise SnapshotError("inconsistent captured index checksum")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3, 4):
        raise SnapshotError("unsupported Git index version")
    end = len(data) - width
    cursor = 12
    try:
        for _ in range(int.from_bytes(data[8:12], "big")):
            start = cursor
            flags_at = cursor + 40 + width
            flags = int.from_bytes(data[flags_at : flags_at + 2], "big")
            cursor = flags_at + 2
            if flags & 0x4000:
                cursor += 2
            if version == 4:
                while data[cursor] & 0x80:
                    cursor += 1
                cursor += 1
            cursor = data.index(b"\0", cursor) + 1
            if version != 4:
                cursor = start + ((cursor - start + 7) // 8) * 8
            if cursor > end:
                raise ValueError("index entry exceeds content")
        while cursor < end:
            signature = data[cursor : cursor + 4]
            size = int.from_bytes(data[cursor + 4 : cursor + 8], "big")
            if cursor + 8 + size > end:
                raise ValueError("index extension exceeds content")
            if signature == b"link":
                raise SnapshotError("split index format is unsupported")
            cursor += 8 + size
        if cursor != end:
            raise ValueError("index extension boundary")
    except (IndexError, ValueError) as error:
        raise SnapshotError("invalid captured index structure") from error


class SnapshotReservation:
    """One reservation's sole capabilities and retained managed Git owners."""

    def __init__(
        self, path: Path, *, timeout_seconds: float, grace_seconds: float
    ) -> None:
        self.path = path
        self._ops = PosixDescriptorOps()
        self._directories: list[DirectoryDescriptor] = []
        self._processes: list[OwnedProcess] = []
        self._root: DirectoryDescriptor | None = None
        self._repository: Path | None = None
        self._tree: CapturedTree | None = None
        self._captured = False
        self._exported = False
        self._closed = False
        self._lock = threading.RLock()
        self._deadline = time.monotonic() + timeout_seconds
        self._grace = grace_seconds

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("snapshot reservation is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(exc)

    def close(self, primary: BaseException | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            failures = CleanupFailures(primary)
            # Git collection/context exit owns local settlement. Retain owners
            # for read-only observation if their continuation is still pending.
            for process in self._processes:
                status = process.cleanup_status
                if not status.complete:
                    failures.run(self._incomplete, ordinary=(SnapshotError,))
            for directory in reversed(self._directories):
                failures.run(directory.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result

    def _incomplete(self) -> None:
        error = SnapshotError("Git helper cleanup remains incomplete")
        error.cleanup = self
        raise error

    @property
    def processes(self) -> tuple[OwnedProcess, ...]:
        """Retained owners for bounded cleanup observations; never recapture."""
        return tuple(self._processes)

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise SnapshotError("snapshot deadline expired")
        return remaining

    def _git(
        self,
        args: list[str],
        *,
        index: Path | None = None,
        sink: Callable[[bytes], None] | None = None,
        limit: int = 65536,
        allow_failure: bool = False,
    ) -> tuple[int | None, bytes]:
        if self._repository is None:
            raise RuntimeError("repository has not been selected")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_NO_LAZY_FETCH="1",
        )
        if index is not None:
            environment["GIT_INDEX_FILE"] = str(index)
        argv = [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(self._repository),
            *args,
        ]
        output = bytearray()
        failure: Exception | None = None

        def chunk(stream: ProcessStream, data: bytes) -> None:
            nonlocal failure
            if stream is not ProcessStream.STDOUT or failure is not None:
                return
            try:
                if sink is not None:
                    sink(data)
                elif len(output) + len(data) > limit:
                    raise SnapshotError("Git metadata exceeds supported bounded size")
                else:
                    output.extend(data)
            except Exception as error:
                # Generic callbacks are diagnostic-only. Domain storage/parser
                # status is independent; continue draining/discarding safely.
                failure = error

        with OwnedProcess.launch(
            argv,
            deadline_seconds=self._remaining(),
            grace_seconds=self._grace,
            capture_limit=0,
            popen_kwargs={"cwd": self._repository, "env": environment},
        ) as process:
            self._processes.append(process)
            result = process.stream(chunk)
        failures = CleanupFailures(failure)
        for info in result.lifecycle.failures:
            # Preserve typed authority, not a guess from exception type: even
            # OSError may be explicitly classified as an unexpected defect.
            def propagate(error: BaseException = info.error) -> None:
                raise error

            failures.run(
                propagate,
                ordinary=(Exception,) if info.kind is FailureKind.ORDINARY else (),
            )
        if not result.lifecycle.complete:
            failures.run(self._incomplete, ordinary=(SnapshotError,))
        selected = failures.complete()
        if selected is not None:
            raise selected
        if failure is not None:
            raise failure
        if result.timed_out or any(
            outcome is not StreamOutcome.EOF for _, outcome in result.stream_outcomes
        ):
            raise SnapshotError("Git operation timed out or stream was incomplete")
        if result.returncode != 0 and not allow_failure:
            raise SnapshotError("Git operation failed: " + args[0])
        return result.returncode, bytes(output)

    def _leaf(self) -> DirectoryDescriptor:
        if self._closed or self._root is None:
            raise RuntimeError("snapshot reservation is not live")
        if Path(os.readlink(f"/proc/self/fd/{self._root.fd}")) != self.path:
            raise SnapshotError("snapshot capability moved from its reserved path")
        return self._root

    def _new_file(self, parent: DirectoryDescriptor, name: str) -> OwnedDescriptor:
        base = parent.child_basename(name)
        label = f"snapshot file {base}"
        fd = self._ops.openat(
            parent.fd, base, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS, 0o600
        )
        return OwnedDescriptor(self._ops, fd, label=label)

    def _copy_index(
        self, index_path: Path, destination: OwnedDescriptor, object_format: str
    ) -> None:
        with DirectoryDescriptor.open_secure_path(
            self._ops, index_path.parent, label="development index directory"
        ) as parent:
            label = "opened development index"
            try:
                fd = self._ops.openat(
                    parent.fd, index_path.name, os.O_RDONLY | os.O_NONBLOCK | _FLAGS
                )
            except FileNotFoundError:
                # An unborn repository may never have had an index. Represent
                # that observed empty staged state only in our private index.
                header = b"DIRC" + (2).to_bytes(4, "big") + bytes(4)
                _write_all(
                    destination.fd, header + hashlib.new(object_format, header).digest()
                )
                return
            with OwnedDescriptor(self._ops, fd, label=label) as source:
                before = self._ops.fstat(source.fd)
                if not stat.S_ISREG(before.st_mode):
                    raise SnapshotError("development index is not a regular file")
                data = bytearray()
                while block := os.read(source.fd, 65536):
                    data.extend(block)
                    _write_all(destination.fd, block)
                after = self._ops.fstat(source.fd)
                # Atomic replacement unlinks this opened inode and changes its
                # ctime, but not its bytes. Keep the opened version; checksum
                # and size/mtime still reject inconsistent/in-place capture.
                if (
                    len(data) != before.st_size
                    or (before.st_size, before.st_mtime_ns)
                    != (after.st_size, after.st_mtime_ns)
                    or (before.st_ctime_ns != after.st_ctime_ns and after.st_nlink != 0)
                ):
                    raise SnapshotError(
                        "development index changed in place during capture"
                    )
                _validate_index(bytes(data), object_format)

    def capture(self, repository: Path) -> CapturedTree:
        with self._lock:
            root = self._leaf()
            if self._captured:
                raise RuntimeError("snapshot capture already attempted")
            self._captured = True
            self._repository = Path(repository).resolve(strict=True)
            _, fmt = self._git(["rev-parse", "--show-object-format"])
            object_format = fmt.rstrip(b"\n").decode("ascii")
            if object_format not in ("sha1", "sha256"):
                raise SnapshotError("unsupported Git object format")
            # --git-path index dereferences an index symlink in Git itself,
            # bypassing our no-follow file open. Redirection is sanitized, so
            # the actual index is the basename in this worktree's Git directory.
            _, raw_path = self._git(["rev-parse", "--absolute-git-dir"])
            index_path = Path(os.fsdecode(raw_path.removesuffix(b"\n"))) / "index"
            private = self.path / _PRIVATE_INDEX
            created = False
            primary: BaseException | None = None
            try:
                owner = self._new_file(root, _PRIVATE_INDEX)
                created = True
                with owner:
                    self._ops.fchmod(owner.fd, 0o600)
                    self._copy_index(index_path, owner, object_format)
                # Both commands use only the private index. ls-files -u can be
                # arbitrarily large, so a streaming flag is sufficient.
                unresolved = False

                def mark_unresolved(data: bytes) -> None:
                    nonlocal unresolved
                    unresolved = unresolved or bool(data)

                self._git(
                    ["ls-files", "--unmerged", "-z"],
                    index=private,
                    sink=mark_unresolved,
                )
                if unresolved:
                    raise SnapshotError("unresolved index entries")
                _, raw_tree = self._git(["write-tree"], index=private)
                tree_id = _oid(raw_tree.rstrip(b"\n").decode("ascii"), object_format)
                code, raw_head = self._git(
                    ["rev-parse", "--verify", "HEAD"], allow_failure=True
                )
                head = (
                    _oid(raw_head.rstrip(b"\n").decode("ascii"), object_format)
                    if code == 0
                    else None
                )
                self._tree = CapturedTree(tree_id, object_format, head)
            except BaseException as error:
                primary = error
                raise
            finally:
                failures = CleanupFailures(primary)
                if created:
                    if all(
                        process.cleanup_status.complete for process in self._processes
                    ):
                        failures.run(
                            lambda: root.unlink_child(_PRIVATE_INDEX),
                            ordinary=(OSError,),
                        )
                        failures.run(
                            lambda: self._remove_lock(root), ordinary=(OSError,)
                        )
                    else:
                        # A pending helper can still use/create its index/lock.
                        # Keep paths and the same process continuation for later
                        # reconciliation; never unlink beneath active work.
                        failures.run(self._incomplete, ordinary=(SnapshotError,))
                result = failures.complete()
                if result is not None:
                    raise result
            return self._tree

    @staticmethod
    def _remove_lock(root: DirectoryDescriptor) -> None:
        try:
            root.unlink_child(_PRIVATE_INDEX + ".lock")
        except FileNotFoundError:
            pass

    def _blob(self, oid: str, sink: Callable[[bytes], None]) -> None:
        self._git(["cat-file", "blob", oid], sink=sink)

    @staticmethod
    def _validate_links(
        entries: dict[tuple[str, ...], TreeEntry], links: dict[tuple[str, ...], bytes]
    ) -> None:
        for path, target in links.items():
            if not target or b"\0" in target or target.startswith(b"/"):
                raise SnapshotError("unsafe symlink target")
            pending = deque([*path[:-1], *os.fsdecode(target).split("/")])
            resolved: list[str] = []
            hops = 0
            while pending:
                part = pending.popleft()
                if part in ("", "."):
                    continue
                if part == "..":
                    if not resolved:
                        raise SnapshotError("outward symlink target")
                    resolved.pop()
                    continue
                current = (*resolved, part)
                entry = entries.get(current)
                if entry is None:
                    raise SnapshotError(
                        "unprovable symlink containment: missing target"
                    )
                if entry.mode == "120000":
                    hops += 1
                    replacement = links[current]
                    if (
                        hops > 40
                        or not replacement
                        or replacement.startswith(b"/")
                        or b"\0" in replacement
                    ):
                        raise SnapshotError("unsafe symlink chain/cycle")
                    pending.extendleft(reversed(os.fsdecode(replacement).split("/")))
                else:
                    if pending and entry.mode != "040000":
                        raise SnapshotError("symlink target traverses a non-directory")
                    resolved.append(part)

    def export(self, tree: CapturedTree) -> None:
        with self._lock:
            root = self._leaf()
            if tree is not self._tree or self._exported:
                raise RuntimeError(
                    "export requires this reservation's one captured tree"
                )
            self._exported = True
            parser = TreeEntries(tree.object_format)
            self._git(["ls-tree", "-r", "-t", "-z", tree.tree_id], sink=parser.feed)
            parser.finish()
            links: dict[tuple[str, ...], bytes] = {}
            for path, entry in parser.entries.items():
                if entry.mode == "120000":
                    target_bytes = bytearray()

                    def collect(data: bytes, buffer: bytearray = target_bytes) -> None:
                        if len(buffer) + len(data) > 4095:
                            raise SnapshotError("unsupported symlink target length")
                        buffer.extend(data)

                    self._blob(entry.oid, collect)
                    links[path] = bytes(target_bytes)
            self._validate_links(parser.entries, links)
            directories = {(): root}
            try:
                for path, entry in sorted(
                    parser.entries.items(), key=lambda item: (len(item[0]), item[0])
                ):
                    parent = directories[path[:-1]]
                    if entry.mode == "040000":
                        child = parent.create_directory(path[-1], mode=0o700)
                        self._directories.append(child)
                        directories[path] = child
                    elif entry.mode != "120000":
                        with self._new_file(parent, path[-1]) as owner:
                            self._blob(
                                entry.oid, lambda data: _write_all(owner.fd, data)
                            )
                            self._ops.fchmod(
                                owner.fd, 0o755 if entry.mode == "100755" else 0o644
                            )
                # Symlinks are last. No export write ever traverses one.
                for path, target in links.items():
                    parent = directories[path[:-1]]
                    os.symlink(
                        os.fsdecode(target),
                        parent.child_basename(path[-1]),
                        dir_fd=parent.fd,
                    )
            except DescriptorError as error:
                raise SnapshotError("unsafe snapshot export directory") from error


def reserve_snapshot(
    root: Path, run_id: str, *, timeout_seconds: float = 120, grace_seconds: float = 1
) -> SnapshotReservation:
    """Exclusively acquire root/run_id, never rename, reuse or delete a loser."""
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid canonical request run ID")
    if not 0 < timeout_seconds <= 86400 or not 0 < grace_seconds <= 3600:
        raise ValueError("invalid snapshot deadline or cleanup allowance")
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise SnapshotError(
            "snapshot root is not canonical or was replaced by a symlink"
        )
    reservation = SnapshotReservation(
        root / run_id, timeout_seconds=timeout_seconds, grace_seconds=grace_seconds
    )
    try:
        parent = DirectoryDescriptor.open_secure_path(
            reservation._ops,
            root.anchor,
            label="snapshot path root",
            require_owner=False,
        )
        reservation._directories.append(parent)
        for part in root.parts[1:]:
            try:
                child = parent.open_directory(part, require_owner=False)
            except DescriptorError as error:
                if (
                    error.stage != "open-descriptor"
                    or not isinstance(error.cause, OSError)
                    or error.cause.errno != errno.ENOENT
                ):
                    raise
                try:
                    child = parent.create_directory(part, mode=0o700)
                except DescriptorError as collision:
                    if (
                        collision.stage != "create-descriptor"
                        or not isinstance(collision.cause, OSError)
                        or collision.cause.errno != errno.EEXIST
                    ):
                        raise
                    child = parent.open_directory(part, require_owner=False)
            reservation._directories.append(child)
            parent = child
        if Path(os.readlink(f"/proc/self/fd/{parent.fd}")) != root:
            raise SnapshotError("snapshot root capability moved")
        leaf = parent.create_directory(run_id, mode=0o700)
        reservation._directories.append(leaf)
        reservation._root = leaf
        reservation._leaf()
    except (OSError, DescriptorError) as error:
        failure = SnapshotError("exclusive snapshot reservation failed")
        failure.__cause__ = error
        reservation.close(failure)
        raise failure from error
    except BaseException as error:
        reservation.close(error)
        raise
    return reservation
