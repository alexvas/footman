"""Literal dedicated-container execution; no Docker policy in the owners.

This adapter retains observations and the same local process owners. Container
removal/snapshot deletion and durable result publication belong to the run
supervisor, not this execution boundary. It exposes no public CLI.
"""

from __future__ import annotations

import base64
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from footman.configuration import Configuration
from footman.filesystem.cleanup import CleanupFailures
from footman.process import (
    FailureKind,
    LifecycleResult,
    OwnedProcess,
    ProcessResult,
    ProcessStream,
    StopPolicy,
    StopStage,
    StreamOutcome,
)
from footman.snapshot import SnapshotReservation

_PREFIX_LIMIT = 4096
_CONTAINER_ID = re.compile(rb"[0-9A-Fa-f]{64}")
_IMAGE_ID = re.compile(rb"sha256:[0-9A-Fa-f]{64}")
INSPECT_FORMAT = "{{.Id}}|{{.State.Status}}|{{.Image}}"
type ChunkSink = Callable[[ProcessStream, bytes], None]


class ContainerError(Exception):
    """Dispatch failed; retained adapter state supplies reconciliation identity."""


@dataclass
class StreamSummary:
    observed_bytes: int = 0
    prefix: bytearray = field(default_factory=bytearray, repr=False)
    outcome: StreamOutcome = StreamOutcome.PENDING

    @property
    def excerpt_base64(self) -> str:
        return base64.b64encode(self.prefix).decode("ascii")

    @property
    def retained_bytes(self) -> int:
        return len(self.prefix)

    @property
    def truncated(self) -> bool:
        return self.observed_bytes > _PREFIX_LIMIT


class InfrastructureCollector:
    """Leading raw bytes, independent of generic tails and protocol parsing."""

    def __init__(self) -> None:
        self.summaries = {stream: StreamSummary() for stream in ProcessStream}
        self.failure: Exception | None = None

    def _retain(self, summary: StreamSummary, data: bytes) -> None:
        summary.prefix.extend(data[: max(0, _PREFIX_LIMIT - len(summary.prefix))])

    def chunk(self, stream: ProcessStream, data: bytes) -> None:
        summary = self.summaries[stream]
        summary.observed_bytes += len(data)
        if self.failure is None:
            try:
                self._retain(summary, data)
            except Exception as error:
                # Ordinary process callbacks are diagnostic-only. This explicit
                # domain failure remains authoritative even after a zero exit.
                self.failure = error

    def end(self, stream: ProcessStream, outcome: StreamOutcome) -> None:
        self.summaries[stream].outcome = outcome

    def finish(self, result: ProcessResult) -> None:
        for stream, outcome in result.stream_outcomes:
            self.end(stream, outcome)


class _Record:
    """Bounded parser state, separate from excerpts; track the entire record."""

    def __init__(self) -> None:
        self.prefix = bytearray()
        self.size = 0
        self.newlines = 0
        self.last_newline = -1

    def feed(self, data: bytes) -> None:
        self.prefix.extend(data[: max(0, 256 - len(self.prefix))])
        self.newlines += data.count(b"\n")
        if b"\n" in data:
            self.last_newline = self.size + data.rfind(b"\n")
        self.size += len(data)

    def record(self) -> bytes:
        if self.newlines and (self.newlines != 1 or self.last_newline != self.size - 1):
            raise ContainerError("expected one record with at most one trailing LF")
        return (
            bytes(self.prefix).removesuffix(b"\n")
            if self.size <= 256
            else bytes(self.prefix)
        )

    def container_id(self) -> str:
        record = self.record()
        if self.size not in (64, 65) or _CONTAINER_ID.fullmatch(record) is None:
            raise ContainerError(
                "startup stdout must contain only one full container ID"
            )
        return record.decode("ascii")


@dataclass
class InfrastructureOperation:
    name: str
    argv: tuple[str, ...] = ()
    started_at: str | None = None
    ended_at: str | None = None
    result: ProcessResult | None = None
    collector: InfrastructureCollector = field(default_factory=InfrastructureCollector)
    diagnostics: list[str] = field(default_factory=list)
    failure: BaseException | None = None


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class ContainerExecution:
    """One attempt, retained identity and once-collected local child owners.

    Callers supply separate startup/command sinks; no persistence or aggregate
    success claim is made here. The Phase 6 supervisor consumes retained owners
    and known container identity on every terminal path.
    """

    def __init__(
        self,
        config: Configuration,
        snapshot: SnapshotReservation,
        *,
        launch: Callable[..., Any] = OwnedProcess.launch,
    ) -> None:
        self.config = config
        self.snapshot = snapshot
        self._launch = launch
        self.processes: list[OwnedProcess] = []
        self.container_id: str | None = None
        self.observed_image_id: str | None = None
        self.startup_result: ProcessResult | None = None
        self.startup_operation = InfrastructureOperation("startup")
        self.command_result: ProcessResult | None = None
        self.startup_argv: tuple[str, ...] = ()
        self.command_argv: tuple[str, ...] = ()
        self.infrastructure_operations = [
            InfrastructureOperation(name) for name in ("inspect", "mkdir", "cp")
        ]
        self._claim = threading.Lock()
        self._started = False
        self._deadline = time.monotonic() + config.timeout_seconds
        # Five local children may each spend at most this share. Reserve half
        # of the ONE configured allowance for the later domain cleanup plan.
        self.local_grace_seconds = config.cleanup_grace_seconds / 10
        self.cleanup_allowance_reserved = config.cleanup_grace_seconds / 2
        self.failure: BaseException | None = None
        self.command_cleanup_action: Any = None

    @property
    def cleanup_status(self) -> tuple[LifecycleResult, ...]:
        return tuple(owner.cleanup_status for owner in self.processes)

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ContainerError("container execution deadline exhausted")
        return remaining

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        sink: ChunkSink | None = None,
        parser: _Record | None = None,
        operation: InfrastructureOperation | None = None,
        configured_command: bool = False,
    ) -> ProcessResult:
        collector = operation.collector if operation else InfrastructureCollector()
        sink_failure: Exception | None = None
        if operation is not None:
            operation.argv = argv

        def chunk(stream: ProcessStream, data: bytes) -> None:
            nonlocal sink_failure
            collector.chunk(stream, data)
            # Protocol attribution must continue after an independent sink
            # failure. Parser state never comes from an excerpt or raw tail.
            try:
                if parser is not None and stream is ProcessStream.STDOUT:
                    parser.feed(data)
            except Exception as error:
                if sink_failure is None:
                    sink_failure = error
            if sink_failure is not None:
                return
            try:
                if sink is not None:
                    sink(stream, data)
            except Exception as error:
                sink_failure = error

        try:
            remaining = self._remaining()
            if operation is not None:
                operation.started_at = _timestamp()
            action = self.command_cleanup_action if configured_command else None
            grace = self.local_grace_seconds
            policy = StopPolicy((), grace)
            total = grace
            if action is not None:
                action.identity = self.container_id
                policy = StopPolicy(
                    (
                        StopStage(
                            grace * 5,
                            action=action,
                            run_on_normal=True,
                            local_stop_before=True,
                        ),
                    ),
                    grace,
                )
                total = grace * 6
            with self._launch(
                argv,
                deadline_seconds=remaining,
                grace_seconds=total,
                stop_policy=policy,
                capture_limit=0,
            ) as owner:
                self.processes.append(owner)
                result = owner.stream(chunk, on_stream_end=collector.end)
                if configured_command:
                    # An observation, not a success claim: retain before owner
                    # exit or any sink/collector/lifecycle failure can raise.
                    self.command_result = result
            collector.finish(result)
            if operation is not None:
                operation.result = result
            if operation is self.startup_operation:
                self.startup_result = result
            failures = CleanupFailures(sink_failure or collector.failure)
            for info in result.lifecycle.failures:

                def propagate(error: BaseException = info.error) -> None:
                    raise error

                failures.run(
                    propagate,
                    ordinary=(Exception,) if info.kind is FailureKind.ORDINARY else (),
                )
            if (
                not result.lifecycle.complete
                or result.timed_out
                or any(
                    collector.summaries[s].outcome is not StreamOutcome.EOF
                    for s in ProcessStream
                )
            ):

                def incomplete() -> None:
                    raise ContainerError("local child or stream observation incomplete")

                failures.run(incomplete, ordinary=(ContainerError,))
            selected = failures.complete()
            if selected is not None:
                raise selected
            if sink_failure is not None:
                raise sink_failure
            if collector.failure is not None:
                raise collector.failure
            return result
        except BaseException as error:
            if operation is not None:
                operation.failure = error
            raise
        finally:
            if operation is not None and operation.started_at is not None:
                operation.ended_at = _timestamp()

    def _inspect(self, operation: InfrastructureOperation, identity: str) -> None:
        parser = _Record()
        result = self._run(
            ("docker", "container", "inspect", "--format", INSPECT_FORMAT, identity),
            parser=parser,
            operation=operation,
        )
        try:
            if result.returncode != 0:
                raise ContainerError("container inspection failed")
            fields = parser.record().split(b"|", 2)
            if (
                len(fields) < 2
                or _CONTAINER_ID.fullmatch(fields[0]) is None
                or fields[0].decode("ascii").lower() != identity.lower()
                or fields[1] != b"running"
            ):
                raise ContainerError(
                    "inspection requires the exact full container ID and running state"
                )
            image = fields[2] if len(fields) == 3 else b""
            if _IMAGE_ID.fullmatch(image) is not None and parser.size <= 256:
                self.observed_image_id = image.decode("ascii")
            else:
                operation.diagnostics.append(
                    "observed image identity unknown: unavailable or unusable metadata"
                )
        except ContainerError as error:
            operation.diagnostics.append(str(error))
            operation.failure = error
            raise

    def execute(self, startup: ChunkSink, command: ChunkSink) -> None:
        with self._claim:
            if self._started:
                raise RuntimeError("container execution already attempted")
            self._started = True
        parser = _Record()
        try:
            source = self.snapshot.copy_source()
            self.startup_argv = tuple(
                self.config.image_id if arg == "{image_id}" else arg
                for arg in self.config.container_start_argv
            )
            self.startup_result = self._run(
                self.startup_argv,
                sink=startup,
                parser=parser,
                operation=self.startup_operation,
            )
            if self.startup_result.returncode != 0:
                raise ContainerError("container startup failed")
            self.container_id = parser.container_id()
            identity = self.container_id
            inspect, mkdir, copy = self.infrastructure_operations
            self._inspect(inspect, identity)
            destination = self.config.container_snapshot_directory
            # Revalidate the host capability at the transfer boundary too.
            if self.snapshot.copy_source() != source:
                raise ContainerError("snapshot source identity changed")
            for op, argv in (
                (
                    mkdir,
                    (
                        "docker",
                        "exec",
                        "-u",
                        "root",
                        identity,
                        "mkdir",
                        "--",
                        destination,
                    ),
                ),
                (
                    copy,
                    (
                        "docker",
                        "cp",
                        str(source) + "/.",
                        identity + ":" + destination.rstrip("/") + "/",
                    ),
                ),
            ):
                if op is copy and self.snapshot.copy_source() != source:
                    raise ContainerError("snapshot source identity changed")
                result = self._run(argv, operation=op)
                if result.returncode != 0:
                    error = ContainerError(
                        op.name + " failed; configured command not started"
                    )
                    op.failure = error
                    op.diagnostics.append(str(error))
                    raise error
            self.command_argv = (
                "docker",
                "exec",
                "-u",
                self.config.user,
                "-w",
                self.config.container_working_directory,
                identity,
                *self.config.command_argv,
            )
            self._run(self.command_argv, sink=command, configured_command=True)
        except BaseException as error:
            # A valid zero-status, fully observed ID remains attributable even
            # if persistence or sole pipe release failed. Never infer an ID
            # from a nonzero/unknown startup status or incomplete stdout.
            if (
                self.container_id is None
                and self.startup_result is not None
                and self.startup_result.returncode == 0
                and self.startup_operation.collector.summaries[
                    ProcessStream.STDOUT
                ].outcome
                is StreamOutcome.EOF
            ):
                try:
                    self.container_id = parser.container_id()
                except ContainerError:
                    pass
            self.failure = error
            raise
