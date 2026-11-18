"""Resource-free descriptions for session build admission.

Each acquire context owns its resources independently; system adapters only
borrow descriptors. Legacy callers remain operational during the migration.
"""

import errno
import fcntl
import math
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from footman.filesystem.descriptors import (
    DirectoryDescriptor,
    OwnedDescriptor,
    UnsafeDescriptorError,
)
from footman.filesystem.operations import DescriptorOps, PosixDescriptorOps


class AdmissionSystem(Protocol):
    """External descriptor, lock and time observations, not admission policy."""

    @property
    def descriptors(self) -> DescriptorOps: ...

    def try_lock_exclusive(self, fd: int) -> bool: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, kw_only=True)
class PosixAdmissionSystem:
    """Resource-free POSIX adapter; locking borrows the supplied descriptor."""

    descriptors: DescriptorOps = field(default_factory=PosixDescriptorOps)

    def try_lock_exclusive(self, fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True, kw_only=True)
class AdmissionPool:
    """Immutable configuration; filesystem work is deferred to acquisition."""

    directory: Path
    capacity: int
    system: AdmissionSystem | None = None

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int):
            raise TypeError("admission capacity must be an integer, not boolean")
        if self.capacity <= 0:
            raise ValueError("admission capacity must be positive")

    @contextmanager
    def acquire(self, *, deadline: int | float) -> Iterator[None]:
        """Acquire one slot within the caller's absolute monotonic deadline."""
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("admission deadline must be numeric, not boolean")
        # Integers are finite without converting arbitrarily large values to float.
        if isinstance(deadline, float) and not math.isfinite(deadline):
            raise ValueError("admission deadline must be finite")
        system = self.system if self.system is not None else PosixAdmissionSystem()
        ops = system.descriptors
        label = "session admission slot"
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        with DirectoryDescriptor.open_secure_path(ops, self.directory) as directory:
            while True:
                for index in range(self.capacity):
                    if system.monotonic() >= deadline:
                        raise TimeoutError("admission deadline exhausted")
                    name = f"admission-slot-{index}.lock"
                    try:
                        fd = ops.openat(directory.fd, name, flags, 0o600)
                    except OSError as error:
                        unsafe = error.errno in {errno.ELOOP, errno.EISDIR}
                        if error.errno == errno.ENXIO:
                            # Sockets can fail before an fd exists. Diagnose only
                            # through the held directory, never following a link.
                            # This observation never authorizes entry or a retry.
                            try:
                                info = directory.stat_child(name, follow_symlinks=False)
                            except OSError as lookup_error:
                                # Keep the open failure primary and retain the
                                # inconclusive diagnostic. Interruptions propagate.
                                raise error from lookup_error
                            unsafe = not stat.S_ISREG(info.st_mode)
                        if unsafe:
                            raise UnsafeDescriptorError(
                                "open-admission-slot",
                                "unsafe admission slot",
                                cause=error,
                            ) from error
                        raise
                    with OwnedDescriptor(ops, fd, label=label) as slot:
                        info = ops.fstat(slot.fd)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.geteuid()
                        ):
                            raise UnsafeDescriptorError(
                                "validate-admission-slot",
                                "admission slot must be regular and owned",
                            )
                        if system.try_lock_exclusive(slot.fd):
                            if system.monotonic() >= deadline:
                                raise TimeoutError(
                                    "admission deadline exhausted before entry"
                                )
                            yield None
                            return
                now = system.monotonic()
                if now >= deadline:
                    raise TimeoutError("admission deadline exhausted")
                delay = 0.01 if deadline >= now + 0.01 else deadline - now
                system.sleep(delay)


class CapacityObservationUnavailable(Exception):
    """An expected missing/inaccessible raw CPU observation, with its cause."""

    def __init__(self, message: str, *, cause: BaseException | None = None):
        super().__init__(message)
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class CapacitySystem(Protocol):
    """Raw observations; discovery and parsing belong to the resolver."""

    def affinity(self) -> frozenset[int]: ...

    def read_text(self, path: Path) -> str: ...


class PosixCapacitySystem:
    """Resource-free default CPU observation adapter."""

    def affinity(self) -> frozenset[int]:
        return frozenset(os.sched_getaffinity(0))

    def read_text(self, path: Path) -> str:
        raise NotImplementedError("P08 owned quota observation not implemented")


@dataclass(frozen=True, kw_only=True)
class CapacityDecision:
    """Immutable resolved capacity and human-readable diagnostic."""

    capacity: int
    reason: str


def resolve_admission_capacity(
    *, workers: int, system: CapacitySystem | None = None
) -> CapacityDecision:
    """Resolve admission policy from raw CPU observations."""
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise TypeError("worker count must be an integer, not boolean")
    if workers <= 0:
        raise ValueError("worker count must be positive")
    observations = system if system is not None else PosixCapacitySystem()
    affinity = observations.affinity()
    return CapacityDecision(
        capacity=max(1, min(workers, len(affinity))),
        reason=f"provisional worker/affinity bound: workers={workers}, affinity={len(affinity)}",
    )
