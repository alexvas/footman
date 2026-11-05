"""Lossless output and result publication policy over the shared owners.

No stream or child receives a second owner. FileSinks borrows reservation fds;
Manifest borrows its directory capability and solely owns each temporary fd.
RetainedRun is an internal domain boundary, not a command-execution CLI.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

from footman.configuration import Configuration
from footman.container import InfrastructureOperation, _timestamp
from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.output import OutputReservation
from footman.process import OwnedProcess, ProcessResult, ProcessStream, StreamOutcome
from footman.snapshot import CapturedTree, SnapshotReservation, reserve_snapshot
from footman.supervision import RunSupervisor

_PATH_FIELDS = {
    "stdout.txt": "stdoutPath",
    "stderr.txt": "stderrPath",
    "startup-stdout.txt": "startupStdoutPath",
    "startup-stderr.txt": "startupStderrPath",
    "result.json": "resultPath",
}


def _write_all(fd: int, data: bytes) -> int:
    remaining = memoryview(data)
    total = 0
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0 or written > len(remaining):
            raise OSError("output write made invalid progress")
        total += written
        remaining = remaining[written:]
    return total


def _error(error: BaseException | None) -> dict[str, Any] | None:
    if error is None:
        return None
    # Diagnostic messages are sensitive too. Bound source bytes before Base64
    # exactly as helper excerpts; do not redact or split arbitrary text safely.
    import base64

    formatting_failure: str | None = None
    try:
        raw = str(error).encode("utf-8", errors="backslashreplace")
    except Exception as formatting:
        formatting_failure = type(formatting).__name__
        raw = b"diagnostic message unavailable"
    return {
        "formatting_failure": formatting_failure,
        "type": type(error).__name__,
        "message_base64": base64.b64encode(raw[:4096]).decode("ascii"),
        "observed_bytes": len(raw),
        "retained_bytes": min(len(raw), 4096),
        "truncated": len(raw) > 4096,
    }


def operation_record(operation: InfrastructureOperation) -> dict[str, Any]:
    result = operation.result
    status = None if result is None else result.returncode
    record: dict[str, Any] = {
        "name": operation.name,
        "state": "not_started"
        if operation.started_at is None
        else "pending"
        if operation.ended_at is None
        else "finished",
        "argv": list(operation.argv),
        "started_at": operation.started_at,
        "ended_at": operation.ended_at,
        "return_code": status,
        "signal": -status if status is not None and status < 0 else None,
        "timed_out": False if result is None else result.timed_out,
        "lifecycle_complete": None if result is None else result.lifecycle.complete,
        "lifecycle_failures": []
        if result is None
        else [
            {
                "kind": f.kind.name.lower(),
                "phase": f.phase.name.lower(),
                "stream": None if f.stream is None else f.stream.name.lower(),
                "error": _error(f.error),
            }
            for f in result.lifecycle.failures
        ],
        "failure": _error(operation.failure),
        "collector_failure": _error(operation.collector.failure),
        "parse_diagnostics": list(operation.diagnostics),
    }
    for stream, summary in operation.collector.summaries.items():
        record[stream.name.lower()] = {
            "excerpt_base64": summary.excerpt_base64,
            "observed_bytes": summary.observed_bytes,
            "retained_bytes": summary.retained_bytes,
            "truncated": summary.truncated,
            "completeness": summary.outcome.value,
            "completeness_name": summary.outcome.name,
        }
    return record


class FileSinks:
    """Unbuffered bounded-memory writers; borrowed fds never grant release.

    Errors are explicit domain observations, not diagnostic-only process callback
    exceptions. After one stream fails it discards subsequent bytes while its
    independent sibling continues to retain output. finish flushes each once.
    """

    def __init__(self, output: OutputReservation, *, startup: bool) -> None:
        self.startup = startup
        prefix = "startup-" if startup else ""
        self._files = {
            ProcessStream.STDOUT: output.files[prefix + "stdout.txt"],
            ProcessStream.STDERR: output.files[prefix + "stderr.txt"],
        }
        self.observed = {s: 0 for s in ProcessStream}
        self.retained = {s: 0 for s in ProcessStream}
        self.outcomes = {s: StreamOutcome.PENDING for s in ProcessStream}
        self.failures: dict[ProcessStream, BaseException] = {}
        self.failure: BaseException | None = None
        self._finished = False
        self._lock = threading.RLock()

    def _record(self, stream: ProcessStream, error: BaseException) -> None:
        self.failures.setdefault(stream, error)
        if self.failure is None or (
            isinstance(self.failure, Exception) and not isinstance(error, Exception)
        ):
            self.failure = error

    def chunk(self, stream: ProcessStream, data: bytes) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("output sink already finished")
            self.observed[stream] += len(data)
            if stream in self.failures:
                return
            try:
                # Count successful partial writes even when a later write fails.
                remaining = memoryview(data)
                while remaining:
                    written = os.write(self._files[stream].fd, remaining)
                    if written <= 0 or written > len(remaining):
                        raise OSError("output write made invalid progress")
                    self.retained[stream] += written
                    remaining = remaining[written:]
            except BaseException as error:
                self._record(stream, error)
                if not isinstance(error, Exception):
                    raise

    def end(self, stream: ProcessStream, outcome: StreamOutcome) -> None:
        with self._lock:
            self.outcomes[stream] = outcome

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            primary = self.failure
            failures = CleanupFailures(primary)
            for stream, view in self._files.items():

                def flush(stream: ProcessStream = stream, view: Any = view) -> None:
                    try:
                        os.fsync(view.fd)
                    except BaseException as error:
                        self._record(stream, error)
                        raise

                failures.run(flush, ordinary=(OSError,))
            try:
                selected = failures.complete()
            except BaseException as error:
                if error is primary:
                    # chunk already propagated this interruption. Keep it primary
                    # without reporting it a second time during finalization.
                    return
                self.failure = error
                raise
            if selected is not None:
                self.failure = selected

    @property
    def complete(self) -> bool:
        return (
            self._finished
            and self.failure is None
            and all(
                self.outcomes[s] is StreamOutcome.EOF
                and self.observed[s] == self.retained[s]
                for s in ProcessStream
            )
        )

    def record(self) -> dict[str, Any]:
        return {
            s.name.lower(): {
                "observed_bytes": self.observed[s],
                "retained_bytes": self.retained[s],
                "completeness": self.outcomes[s].value,
                "completeness_name": self.outcomes[s].name,
                "failure": _error(self.failures.get(s)),
            }
            for s in ProcessStream
        }


class Manifest:
    """One initial write, atomic checkpoints and one final-publication attempt.

    Every temporary fd is immediately adopted, flushed and released before
    replacement and directory sync. Failure before replacement leaves the previous
    manifest; failure after replacement leaves visible but unconfirmed bytes.
    Neither permits a retry or corrective write.
    """

    def __init__(self, output: OutputReservation, config: Configuration) -> None:
        self.output = output
        self.config = config
        self.failure: BaseException | None = None
        self.finalized = False
        self._initial = False
        self._attempted = False
        self._lock = threading.RLock()
        self._ops = PosixDescriptorOps()
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": output.identity.run_id,
            "utc_timestamp": output.identity.utc_timestamp,
            "phase": output.phase,
            "output_template": config.output_dir,
            "output_directory": str(output.directory),
            "configuration_identity": config.identity,
            "command_argv": list(config.command_argv),
            "docker_command_argv": [],
            "startup": operation_record(InfrastructureOperation("startup")),
            "command_started_at": None,
            "command_ended_at": None,
            "cwd": config.container_working_directory,
            "user": config.user,
            "configured_image_id": config.image_id,
            "observed_image_id": None,
            "container_id": None,
            "snapshot": {
                "path": str(config.snapshot_directory / output.identity.run_id),
                "reserved": False,
                "tree_id": None,
                "object_format": None,
                "head": None,
            },
            "started_at": _timestamp(),
            "ended_at": None,
            "return_code": None,
            "signal": None,
            "outcome": "running",
            "paths": {
                key: str(path)
                for name, path in output.paths.items()
                if (key := _PATH_FIELDS.get(name))
            },
            "cleanup": {
                "confirmed_absent": False,
                "snapshot_deleted": False,
                "unresolved_resources": True,
            },
            "persistence": {},
            "warnings": [],
            "limitations": [
                "Only the captured staged snapshot was executed; no verification-sufficiency claim.",
                "Raw evidence can contain secrets; Base64 offers no secrecy.",
            ],
            "infrastructure_operations": [
                operation_record(InfrastructureOperation(n))
                for n in ("inspect", "mkdir", "cp", "rm", "confirmation")
            ],
        }

    @staticmethod
    def _encode(data: Mapping[str, Any]) -> bytes:
        return (
            json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")

    def initial(self) -> None:
        with self._lock:
            if self._initial:
                raise RuntimeError("initial manifest already attempted")
            self._initial = True
            try:
                _write_all(self.output.files["result.json"].fd, self._encode(self.data))
                os.fsync(self.output.files["result.json"].fd)
                os.fsync(self.output._directories[-1].fd)
            except BaseException as error:
                self.failure = error
                raise

    def _publish(self, values: Mapping[str, Any]) -> None:
        leaf = self.output._directories[-1]  # Borrowed; never close it here.
        temporary = ".result.tmp"
        label = "result publication temporary"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        try:
            data = self.data | dict(values)
            encoded = self._encode(data)
            fd = os.open(temporary, flags, 0o600, dir_fd=leaf.fd)
            owner = OwnedDescriptor(self._ops, fd, label=label)
            created = True
            primary: BaseException | None = None
            try:
                _write_all(owner.fd, encoded)
                os.fsync(owner.fd)
            except BaseException as error:
                primary = error
            failures = CleanupFailures(primary)
            failures.run(owner.close, ordinary=(OSError,))
            selected = failures.complete() or primary
            if selected is not None:
                raise selected
            os.replace(temporary, "result.json", src_dir_fd=leaf.fd, dst_dir_fd=leaf.fd)
            created = False
            os.fsync(leaf.fd)
            self.data = data
        except BaseException as error:
            self.failure = error
            failures = CleanupFailures(error)
            if created:
                failures.run(lambda: leaf.unlink_child(temporary), ordinary=(OSError,))
            selected = failures.complete() or error
            if selected is error:
                raise
            raise selected from error

    def checkpoint(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            if self._attempted:
                raise RuntimeError("final manifest already attempted")
            if self.failure is not None:
                raise RuntimeError("manifest publication already failed")
            self._publish(values)

    def finalize(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            if self._attempted:
                raise RuntimeError("final manifest already attempted")
            if self.failure is not None:
                raise RuntimeError("manifest publication already failed")
            self._attempted = True
            try:
                self._publish(values)
            except BaseException as error:
                self.failure = error
                raise
            self.finalized = True


def aggregate_outcome(
    return_code: int | None,
    *,
    interrupted: bool = False,
    timed_out: bool = False,
    output_failed: bool = False,
    dispatch_failed: bool = False,
    unresolved: bool = False,
) -> str:
    """First matching row of docs/results.md; never interpret command output."""
    if interrupted:
        return "interrupted"
    if timed_out:
        return "timed_out"
    if output_failed:
        return "output_failed"
    if dispatch_failed or unresolved or return_code is None:
        return "dispatch_failed"
    return "succeeded" if return_code == 0 else "command_failed"


class PublicationError(Exception):
    """Application boundary for an unconfirmed manifest publication.

    response carries available observations for later MCP error translation; it
    is not a successful internal run return. failure retains selected operation
    authority independently of publication_failure. Neither grants retry rights.
    """

    def __init__(
        self,
        response: dict[str, Any],
        publication_failure: BaseException,
        failure: BaseException | None,
    ) -> None:
        super().__init__("manifest publication failed; invocation unsuccessful")
        self.response = response
        self.publication_failure = publication_failure
        self.failure = failure


class RetainedRun:
    """One accepted reserved request, retained even when execution is interrupted.

    The caller reserves output using reserve_request first. This boundary writes
    the initial manifest BEFORE snapshot reservation/capture and owns downstream
    capabilities until settled. The instance retains the supervisor/continuation
    for observation; execute never restarts work or renews cleanup budgets.
    """

    def __init__(
        self,
        config: Configuration,
        output: OutputReservation,
        *,
        launch: Callable[..., Any] = OwnedProcess.launch,
    ) -> None:
        self.config = config
        self.output = output
        self.launch = launch
        self.manifest = Manifest(output, config)
        self.startup = FileSinks(output, startup=True)
        self.command = FileSinks(output, startup=False)
        self.snapshot: SnapshotReservation | None = None
        self.tree: CapturedTree | None = None
        self.supervisor: RunSupervisor | None = None
        self.response: dict[str, Any] = {}
        self.failure: BaseException | None = None
        self._lock = threading.Lock()
        self._started = False
        self._interrupted = False

    def _cleanup_record(self) -> dict[str, Any]:
        supervisor = self.supervisor
        return {
            "confirmed_absent": False
            if supervisor is None
            else supervisor.cleanup.confirmed_absent,
            "snapshot_deleted": self.snapshot is not None
            and not self.snapshot.path.exists()
            if supervisor is None
            else supervisor.snapshot_deleted,
            "unresolved_resources": self.snapshot is not None
            and self.snapshot.path.exists()
            if supervisor is None
            else supervisor.unresolved_resources,
            "failure": _error(None if supervisor is None else supervisor.failure),
            "resources_complete": True
            if supervisor is None
            else supervisor.cleanup_status.complete,
        }

    def _values(self) -> dict[str, Any]:
        supervisor = self.supervisor
        execution = None if supervisor is None else supervisor.execution
        command = None if execution is None else execution.command_result
        operations = (
            []
            if supervisor is None
            else supervisor.execution.infrastructure_operations
            + supervisor.cleanup.operations
        )
        return {
            "snapshot": {
                "path": str(
                    self.config.snapshot_directory / self.output.identity.run_id
                ),
                "reserved": self.snapshot is not None,
                "tree_id": None if self.tree is None else self.tree.tree_id,
                "object_format": None if self.tree is None else self.tree.object_format,
                "head": None if self.tree is None else self.tree.head,
            },
            "container_id": None if execution is None else execution.container_id,
            "observed_image_id": None
            if execution is None
            else execution.observed_image_id,
            "docker_command_argv": []
            if execution is None
            else list(execution.command_argv),
            "startup": self.manifest.data["startup"]
            if execution is None
            else operation_record(execution.startup_operation),
            "command_started_at": None
            if execution is None
            else execution.command_operation.started_at,
            "command_ended_at": None
            if execution is None
            else execution.command_operation.ended_at,
            "return_code": None if command is None else command.returncode,
            "signal": None
            if command is None or command.returncode is None or command.returncode >= 0
            else -command.returncode,
            "infrastructure_operations": self.manifest.data["infrastructure_operations"]
            if not operations
            else [operation_record(op) for op in operations],
            "cleanup": self._cleanup_record(),
            "persistence": {
                "startup": self.startup.record(),
                "command": self.command.record(),
                "failure": _error(
                    self.output.file_failure or self.output.cleanup_failure
                ),
            },
            "failure": _error(self.failure),
        }

    def _checkpoint(self) -> None:
        try:
            self.manifest.checkpoint(self._values())
        except BaseException as error:
            if self.manifest.failure is None:
                self.manifest.failure = error
            raise

    def _outcome(self) -> str:
        supervisor = self.supervisor
        execution = None if supervisor is None else supervisor.execution
        command = None if execution is None else execution.command_result
        results: list[ProcessResult] = (
            []
            if execution is None
            else [
                op.result
                for op in (
                    execution.startup_operation,
                    *execution.infrastructure_operations,
                    execution.command_operation,
                )
                if op.result is not None
            ]
        )
        started = (
            execution is not None and execution.command_operation.result is not None
        )
        startup_started = (
            execution is not None and execution.startup_operation.result is not None
        )
        return aggregate_outcome(
            None if command is None else command.returncode,
            interrupted=self._interrupted,
            timed_out=any(r.timed_out for r in results)
            or (False if execution is None else execution.deadline_expired),
            output_failed=self.manifest.failure is not None
            or self.output.cleanup_failure is not None
            or self.output.file_failure is not None
            or (started and not self.command.complete)
            or (startup_started and not self.startup.complete),
            dispatch_failed=self.failure is not None
            or (supervisor is not None and supervisor.failure is not None),
            unresolved=supervisor is not None
            and (
                supervisor.unresolved_resources
                or not supervisor.snapshot_deleted
                or not supervisor.cleanup_status.complete
            ),
        )

    def execute(self) -> dict[str, Any]:
        with self._lock:
            if self._started:
                raise RuntimeError("retained run already attempted")
            self._started = True
        primary: BaseException | None = None
        try:
            self.manifest.initial()
            self.snapshot = reserve_snapshot(
                self.config.snapshot_directory, self.output.identity.run_id
            )
            self._checkpoint()
            self.tree = self.snapshot.capture(self.config.repository)
            self.snapshot.export(self.tree)
            self.supervisor = RunSupervisor(
                self.config, self.snapshot, launch=self.launch
            )
            self.supervisor.execution.observer = self._checkpoint
            self._checkpoint()
            self.supervisor.execute(
                self.startup.chunk,
                self.command.chunk,
                startup_end=self.startup.end,
                command_end=self.command.end,
            )
            self.failure = self.supervisor.failure
            primary = self.failure
        except BaseException as error:
            primary = error
            self.failure = error
            self._interrupted = not isinstance(error, Exception)
        failures = CleanupFailures(primary)

        def attempt(action: Callable[[], Any]) -> None:
            def perform() -> None:
                try:
                    action()
                except BaseException as error:
                    if not isinstance(error, Exception):
                        self._interrupted = True
                    raise

            failures.run(perform, ordinary=(Exception,))

        attempt(self.startup.finish)
        attempt(self.command.finish)
        if self.snapshot is not None:
            if self.supervisor is None:
                attempt(self.snapshot.remove)
            attempt(self.snapshot.close)
        attempt(self.output.close_files)
        try:
            selected = failures.complete() or primary
            if selected is not None:
                self.failure = selected
        except BaseException as error:
            self.failure = error
            self._interrupted = not isinstance(error, Exception)
        command_result = (
            None
            if self.supervisor is None
            else self.supervisor.execution.command_result
        )
        values: dict[str, Any] = {
            "return_code": None
            if command_result is None
            else command_result.returncode,
            "cleanup": self.manifest.data["cleanup"],
        }
        publication = CleanupFailures(self.failure)

        def publish() -> None:
            try:
                # Formatting is part of publication, not an unprotected gap
                # before independent directory-capability release.
                values.update(self._values())
                values.update(outcome=self._outcome(), ended_at=_timestamp())
                self.manifest.finalize(values)
            except BaseException as error:
                self.manifest.failure = error
                if not isinstance(error, Exception):
                    self._interrupted = True
                raise

        # An initial/checkpoint failure is terminal for publication. Retain
        # current cleanup observations without rewriting the manifest bytes.
        def observe_cleanup() -> None:
            values["cleanup"] = self._cleanup_record()

        publication.run(observe_cleanup, ordinary=(Exception,))
        if self.manifest.failure is None:
            publication.run(publish, ordinary=(Exception,))
        publication.run(self.output.close, ordinary=(OSError,))
        try:
            self.failure = publication.complete() or self.failure
        except BaseException as error:
            self.failure = error
            self._interrupted = not isinstance(error, Exception)
        self.response = {
            "outcome": self._outcome(),
            "return_code": values["return_code"],
            **self.manifest.data["paths"],
            "diagnostics": {
                "failure": _error(self.failure),
                "publication_failure": _error(self.manifest.failure),
                "manifest_finalized": self.manifest.finalized,
                "cleanup": values["cleanup"],
            },
        }
        if self.failure is not None and not isinstance(self.failure, Exception):
            raise self.failure
        if self.manifest.failure is not None:
            raise PublicationError(
                self.response, self.manifest.failure, self.failure
            ) from self.failure
        return self.response
