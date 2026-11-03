"""Request identity and exclusive retained-output reservation.

This domain adapter owns only output handles. It never captures a snapshot,
launches a child, deletes retained evidence or interprets command output.
"""

from __future__ import annotations

import errno
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

from footman.configuration import Configuration, validate_request
from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
)
from footman.filesystem.operations import PosixDescriptorOps

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_PLACEHOLDER = re.compile(r"\{(phase|utc_datetime)\}")
_FILENAMES = (
    "stdout.txt",
    "stderr.txt",
    "startup-stdout.txt",
    "startup-stderr.txt",
    "result.json",
)


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    uuid: uuid.UUID
    run_id: str
    utc_timestamp: str
    utc_datetime: str

    @classmethod
    def create(cls) -> Self:
        value = uuid.uuid7()
        integer = value.int
        digits = []
        for _ in range(26):
            digits.append(_ALPHABET[integer & 31])
            integer >>= 5
        # Integer epoch arithmetic preserves millisecond boundaries exactly.
        instant = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value.time)
        return cls(
            value,
            "".join(reversed(digits)),
            instant.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            instant.strftime("%Y%m%dT%H%M%S") + f"{value.time % 1000:03d}Z",
        )


def expand_output(
    config: Configuration, identity: RequestIdentity, phase: str | None
) -> Path:
    """Expand parsed operator components once; canonical root names are literal."""
    values = {"utc_datetime": identity.utc_datetime, "phase": phase or ""}
    parts = [
        _PLACEHOLDER.sub(lambda match: values[match[1]], component)
        for component in config.output_suffix
    ]
    return Path(os.path.normpath(config.output_root.joinpath(*parts)))


class OutputError(Exception):
    """Reservation failed; only actually created files are exposed as evidence."""

    outcome = "output_failed"

    def __init__(self, reservation: OutputReservation) -> None:
        super().__init__(f"output reservation failed at {reservation.directory}")
        self.directory = reservation.directory
        self.paths = dict(reservation.paths)
        self.identity = reservation.identity


class OutputReservation:
    """Sole handle owners retained through setup and once-only independent release.

    All directory capabilities remain tracked until release, avoiding a parent
    handoff gap. Existing shared ancestors are never chmod'd. Closing handles
    does not remove files or directories, even on partial setup failure.
    """

    def __init__(
        self, directory: Path, identity: RequestIdentity, phase: str | None
    ) -> None:
        self.directory = directory
        self.identity = identity
        self.phase = phase
        self.paths: dict[str, Path] = {}
        self.files: dict[str, OwnedDescriptor] = {}
        self._directories: list[DirectoryDescriptor] = []
        self._lock = threading.RLock()
        self._closed = False
        self.cleanup_failure: BaseException | None = None

    def close(self, primary: BaseException | None = None) -> None:
        # Serialize the complete release, not merely the claim. A concurrent
        # observer cannot return while a sibling is still releasing handles.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            failures = CleanupFailures(primary)
            for owner in reversed(tuple(self.files.values())):
                failures.run(owner.close, ordinary=(OSError,))
            for directory in reversed(self._directories):
                failures.run(directory.close, ordinary=(OSError,))
            try:
                result = failures.complete()
                self.cleanup_failure = result
                if result is not None:
                    raise result
            except BaseException as error:
                self.cleanup_failure = error
                raise

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("output handles have already been released")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(exc)

    def _setup(self, config: Configuration) -> None:
        root = config.output_root
        destination = self.directory
        # Startup canonicalization is not a lease on filesystem state. Reject
        # changed ancestors/overlap before mutation, then walk without following
        # links so replacements between this check and open fail closed too.
        if root.resolve() != root or destination.resolve() != destination:
            raise OSError("output storage ancestor was replaced by a symlink")
        if not destination.is_relative_to(root):
            raise OSError("expanded output escaped configured storage")
        for forbidden in (
            config.repository.resolve(),
            config.snapshot_directory.resolve(),
        ):
            if destination.is_relative_to(forbidden) or forbidden.is_relative_to(
                destination
            ):
                raise OSError("output overlaps repository or snapshot storage")
        ops = PosixDescriptorOps()
        parent = DirectoryDescriptor.open_secure_path(
            ops, Path(destination.anchor), label="output path root", require_owner=False
        )
        self._directories.append(parent)
        for component in destination.parts[1:-1]:
            try:
                child = parent.open_directory(component, require_owner=False)
            except DescriptorError as error:
                if (
                    error.stage != "open-descriptor"
                    or not isinstance(error.cause, OSError)
                    or error.cause.errno != errno.ENOENT
                ):
                    raise
                try:
                    child = parent.create_directory(component, mode=0o700)
                except DescriptorError as collision:
                    # A shared parent may be concurrently created; only an
                    # exclusive mkdir EEXIST permits one no-follow reopen.
                    if (
                        collision.stage != "create-descriptor"
                        or not isinstance(collision.cause, OSError)
                        or collision.cause.errno != errno.EEXIST
                    ):
                        raise
                    child = parent.open_directory(component, require_owner=False)
            self._directories.append(child)
            parent = child
        leaf = parent.create_directory(destination.name, mode=0o700)
        self._directories.append(leaf)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        for name in _FILENAMES:
            label = f"retained output {name}"
            fd = os.open(name, flags, 0o600, dir_fd=leaf.fd)
            owner = OwnedDescriptor(ops, fd, label=label)
            self.files[name] = owner
            self.paths[name] = destination / name
            ops.fchmod(owner.fd, 0o600)


def reserve_request(config: Configuration, value: object) -> OutputReservation:
    """Validate input, generate once, then finish storage before downstream work.

    Internal dispatch boundary, not a public execution API. The caller retains
    the returned reservation until downstream work has settled, then closes it
    (normally with its context manager). Setup failure releases every acquired
    handle and never grants authority to delete existing destinations.
    """
    phase = validate_request(config, value)
    identity = RequestIdentity.create()
    reserved = OutputReservation(
        expand_output(config, identity, phase), identity, phase
    )
    try:
        reserved._setup(config)
    except (OSError, DescriptorError, ValueError, RuntimeError) as error:
        failure = OutputError(reserved)
        # Bind provenance before cleanup: an interruption there can displace
        # this domain error before the subsequent `raise ... from` executes.
        failure.__cause__ = error
        reserved.close(failure)
        raise failure from error
    except BaseException as error:
        reserved.close(error)
        raise
    return reserved
