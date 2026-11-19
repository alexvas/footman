"""Resource-free descriptions for session build admission.

Each acquire context owns its resources independently; system adapters only
borrow descriptors. Legacy callers remain operational during the migration.
"""

import errno
import fcntl
import math
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from footman.filesystem.cleanup import CleanupFailures
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


@dataclass(frozen=True, kw_only=True)
class SessionAdmission:
    """Resource-free, immutable session provider with one stable pool description."""

    shared_pool: AdmissionPool


class CapacityObservationUnavailable(Exception):
    """An expected missing/inaccessible raw CPU observation, with its cause."""

    def __init__(self, message: str, *, cause: BaseException | None = None):
        super().__init__(message)
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class _AbsentCapacityFile(CapacityObservationUnavailable):
    """A clean ENOENT at leaf open, not a metadata/read/cleanup failure."""


class CapacitySystem(Protocol):
    """Raw observations; discovery and parsing belong to the resolver."""

    def affinity(self) -> frozenset[int]: ...

    def read_text(self, path: Path) -> str: ...


@dataclass(frozen=True, kw_only=True)
class PosixCapacitySystem:
    """Resource-free default CPU observation adapter."""

    descriptors: DescriptorOps = field(default_factory=PosixDescriptorOps)

    def affinity(self) -> frozenset[int]:
        try:
            observe = getattr(os, "sched_getaffinity", None)
            if observe is None:
                raise NotImplementedError("CPU affinity is unsupported")
            return frozenset(observe(0))
        except (OSError, NotImplementedError) as error:
            _raise_expected_observation(error)
            raise

    def read_text(self, path: Path) -> str:
        ops = self.descriptors
        name = str(path)
        label = "CPU capacity observation"
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = ops.openat(None, name, flags)
        except OSError as error:
            if error.errno == errno.ENOENT:
                raise _AbsentCapacityFile(
                    "CPU observation leaf absent", cause=error
                ) from error
            _raise_expected_observation(error)
            raise
        owner = OwnedDescriptor(ops, fd, label=label)
        try:
            if not stat.S_ISREG(ops.fstat(owner.fd).st_mode):
                raise UnsafeDescriptorError(
                    "read-capacity", "CPU observation must be regular"
                )
            chunks = []
            while chunk := os.read(owner.fd, 65536):
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
        except BaseException as error:
            cleanup = CleanupFailures(error)
            cleanup.run(owner.close, ordinary=(OSError,))
            cleanup.complete()
            # Only successful cleanup permits converting an expected observation
            # error. Use the public accumulator, not exception-private evidence.
            if not cleanup.ordinary_failures:
                _raise_expected_observation(error)
            raise
        # A sole-close failure is never an unavailable observation.
        owner.close()
        return text


@dataclass(frozen=True, kw_only=True)
class CapacityDecision:
    """Immutable positive capacity and nonblank human-readable diagnostic."""

    capacity: int
    reason: str

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int):
            raise TypeError("decision capacity must be an integer, not boolean")
        if self.capacity <= 0:
            raise ValueError("decision capacity must be positive")
        if not isinstance(self.reason, str):
            raise TypeError("decision reason must be a string")
        if not self.reason.strip():
            raise ValueError("decision reason must not be blank")


def resolve_admission_capacity(
    *, workers: int, system: CapacitySystem | None = None
) -> CapacityDecision:
    """Resolve admission policy from raw CPU observations."""
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise TypeError("worker count must be an integer, not boolean")
    if workers <= 0:
        raise ValueError("worker count must be positive")
    observations = system if system is not None else PosixCapacitySystem()
    try:
        affinity = observations.affinity()
        if not isinstance(affinity, frozenset):
            raise TypeError("affinity adapter must return a frozenset")
        if not affinity or any(
            isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0
            for cpu in affinity
        ):
            raise _InvalidCapacityObservation("empty or invalid CPU affinity")
        quota, hierarchy = _effective_quota(observations)
    except (CapacityObservationUnavailable, _InvalidCapacityObservation) as error:
        return CapacityDecision(capacity=1, reason=f"CPU capacity unknown: {error}")
    bound = min(workers, len(affinity))
    capacity = max(1, int(min(bound, quota))) if quota is not None else max(1, bound)
    return CapacityDecision(
        capacity=capacity,
        reason=f"workers={workers}, affinity={len(affinity)}, visible {hierarchy} quota={quota if quota is not None else 'unlimited'}",
    )


class _InvalidCapacityObservation(ValueError):
    """Recognized malformed/unresolved raw discovery, not known unlimited."""


def _raise_expected_observation(error: BaseException) -> None:
    if isinstance(error, UnicodeDecodeError):
        raise _InvalidCapacityObservation(
            "CPU observation is not valid UTF-8"
        ) from error
    if isinstance(error, NotImplementedError) or (
        isinstance(error, OSError)
        and error.errno
        in {
            errno.ENOENT,
            errno.ENOTDIR,
            errno.EACCES,
            errno.EPERM,
            errno.ENOSYS,
            errno.ENODEV,
            errno.EOPNOTSUPP,
        }
    ):
        raise CapacityObservationUnavailable(
            "CPU observation unavailable", cause=error
        ) from error


def _observation_path(value: str) -> Path:
    if (
        not value.startswith("/")
        or "\0" in value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise _InvalidCapacityObservation(
            "CPU hierarchy path is not absolute/canonical"
        )
    return Path(value)


def _mount_path(value: str) -> Path:
    decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
    return _observation_path(decoded)


def _positive_observation(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise _InvalidCapacityObservation("CPU quota/period must be positive integers")
    try:
        number = int(value)
    except ValueError as error:
        raise _InvalidCapacityObservation("CPU quota integer is unsupported") from error
    if number <= 0:
        raise _InvalidCapacityObservation("CPU quota/period must be positive")
    return number


def _effective_quota(system: CapacitySystem) -> tuple[Fraction | None, str]:
    membership = system.read_text(Path("/proc/self/cgroup"))
    mounts = system.read_text(Path("/proc/self/mountinfo"))
    v1_members = []
    v2_members = []
    for line in membership.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            raise _InvalidCapacityObservation("malformed cgroup membership")
        if fields[0] == "0" and fields[1] == "":
            v2_members.append(_observation_path(fields[2]))
        elif "cpu" in fields[1].split(","):
            _positive_observation(fields[0])
            v1_members.append(_observation_path(fields[2]))
    # A v1 CPU membership identifies its controller even in hybrid layouts;
    # unified membership then does not imply a v2 CPU controller.
    v1 = bool(v1_members)
    members = v1_members if v1 else v2_members
    hierarchy = "v1" if v1 else "v2"
    if len(members) != 1:
        raise _InvalidCapacityObservation("unresolved CPU cgroup membership")
    member = members[0]
    candidates = []
    for line in mounts.splitlines():
        left, separator, right = line.partition(" - ")
        before, after = left.split(), right.split()
        if not separator or len(before) < 6 or len(after) < 3:
            raise _InvalidCapacityObservation("malformed mount discovery")
        applicable = (
            (after[0] == "cgroup" and "cpu" in after[2].split(","))
            if v1
            else after[0] == "cgroup2"
        )
        if applicable:
            root, mount = _mount_path(before[3]), _mount_path(before[4])
            if member.is_relative_to(root):
                candidates.append((root, mount))
    if not candidates:
        raise _InvalidCapacityObservation("unresolved CPU cgroup mount root")
    # Prefer the widest visible hierarchy, including restrictions above a
    # narrower bind mount; path components, not textual prefixes, define scope.
    root, mount = min(candidates, key=lambda pair: (len(pair[0].parts), str(pair[1])))
    current = mount / member.relative_to(root)
    quotas = []
    while True:
        if v1:
            quota_text = system.read_text(current / "cpu.cfs_quota_us").split()
            period_text = system.read_text(current / "cpu.cfs_period_us").split()
            if len(quota_text) != 1 or len(period_text) != 1:
                raise _InvalidCapacityObservation("malformed v1 quota/period")
            fields = [quota_text[0], period_text[0]]
            unlimited = fields[0] == "-1"
        else:
            try:
                fields = system.read_text(current / "cpu.max").split()
            except _AbsentCapacityFile:
                # Only the actual v2 hierarchy root lacks CPU limit files.
                # mount root '/' alone is insufficient in a cgroup namespace.
                if (
                    current == mount
                    and root == Path("/")
                    and _is_v2_hierarchy_root(system, mount)
                ):
                    return (min(quotas) if quotas else None), hierarchy
                raise
            if len(fields) != 2:
                raise _InvalidCapacityObservation("malformed cpu.max")
            unlimited = fields[0] == "max"
        period = _positive_observation(fields[1])
        if not unlimited:
            quotas.append(Fraction(_positive_observation(fields[0]), period))
        if current == mount:
            return (min(quotas) if quotas else None), hierarchy
        current = current.parent


def _is_v2_hierarchy_root(system: CapacitySystem, mount: Path) -> bool:
    # cgroup.type exists on every non-root cgroup, including namespace roots.
    # Only clean leaf absence is evidence: read errors and close failures must
    # not erase restrictions or turn unknown observations into unlimited quota.
    try:
        system.read_text(mount / "cgroup.type")
    except _AbsentCapacityFile:
        return "cpu" in system.read_text(mount / "cgroup.controllers").split()
    return False
