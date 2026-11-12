"""Project-owned test transport; the SDK receives messages, never child control."""

import math
import queue
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial

import anyio
from mcp import types
from mcp.shared.message import SessionMessage

from footman.filesystem.cleanup import CleanupFailures
from footman.process import OwnedProcess, ProcessStream, StreamOutcome

SUPPORT_FILES = ("tests/__init__.py", "tests/mcp_transport.py")
MAX_FRAME = 1048576


@dataclass
class TransportObservation:
    owner: object = None
    result: object = None
    eof_delivered: bool = False
    eof_error: BaseException | None = None
    failure: BaseException | None = None
    write_observations: list = field(default_factory=list)
    stderr_tail: bytearray = field(default_factory=bytearray)
    terminals: dict = field(default_factory=dict)


def write_frame(owner, data, remaining, *, handoff=None):
    """Capture progress on this very worker before any subsequent operation."""
    previous = owner.stdin_write_observation()
    error = None
    try:
        count = owner.write_stdin(data, timeout_seconds=remaining)
        if count != len(data):
            raise OSError("managed stdin did not accept the entire frame")
    except BaseException as caught:
        error = caught
    observation = owner.stdin_write_observation()
    if error is not None and observation is previous:
        # State/argument rejection did not attempt a new write. A reused live
        # worker's preceding record is not progress for the rejected frame.
        observation = None
    if handoff is not None:
        handoff(observation)
    return observation, error


def _close_memory(streams, *, primary=None, release_error=None):
    failures = CleanupFailures(primary)
    if release_error is not None:

        def failed_release():
            raise release_error

        failures.run(failed_release, ordinary=(OSError,))
    for stream in streams:
        failures.run(stream.close, ordinary=(OSError,))
    selected = failures.complete()
    if selected is not None:
        raise selected


@asynccontextmanager
async def stdio_transport(
    argv,
    *,
    cwd,
    env,
    timeout_seconds,
    parser=None,
    observation=None,
    launch=OwnedProcess.launch,
):
    if type(timeout_seconds) not in {int, float}:
        raise TypeError("transport timeout must be numeric and not boolean")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("transport timeout must be finite and positive")
    observed = observation if observation is not None else TransportObservation()
    end = time.monotonic() + timeout_seconds
    reserve = min(2.0, timeout_seconds / 4)
    frames = queue.Queue(maxsize=32)
    collected = threading.Event()
    discard = threading.Event()
    done = anyio.Event()
    writer_done = anyio.Event()
    memory = []
    try:
        incoming_send, incoming = anyio.create_memory_object_stream(8)
        memory.extend((incoming_send, incoming))
        outgoing, outgoing_receive = anyio.create_memory_object_stream(8)
        memory.extend((outgoing, outgoing_receive))
        owner = launch(
            list(argv),
            stdin_mode="pipe",
            deadline_seconds=timeout_seconds - reserve,
            grace_seconds=reserve,
            capture_limit=0,
            popen_kwargs={"cwd": str(cwd), "env": dict(env)},
        )
    except BaseException as error:
        _close_memory(memory, primary=error)
        raise
    observed.owner = owner

    def remaining():
        value = end - time.monotonic()
        if value <= 0:
            raise TimeoutError("original transport envelope exhausted")
        return value

    def chunk(stream, data):
        if stream is ProcessStream.STDERR:
            observed.stderr_tail.extend(data)
            del observed.stderr_tail[:-65536]
            if parser is not None:
                try:
                    parser.feed(data)
                except BaseException as error:
                    observed.failure = observed.failure or error
                    if not isinstance(error, Exception):
                        raise
            return
        while not discard.is_set():
            try:
                frames.put(data, timeout=min(0.05, remaining()))
                return
            except queue.Full:
                continue
            except BaseException as error:
                observed.failure = observed.failure or error
                discard.set()
                return

    def terminal(stream, outcome):
        observed.terminals[stream] = outcome
        if stream is ProcessStream.STDERR and parser is not None:
            try:
                parser.end(outcome)
            except BaseException as error:
                observed.failure = observed.failure or error
                if not isinstance(error, Exception):
                    raise

    def collect():
        try:
            observed.result = owner.stream(chunk, on_stream_end=terminal)
        except BaseException as error:
            observed.failure = observed.failure or error
        finally:
            collected.set()

    async def collector():
        try:
            await anyio.to_thread.run_sync(collect)
        finally:
            done.set()

    def next_chunk():
        try:
            return frames.get(timeout=0.05)
        except queue.Empty:
            return None

    async def reader():
        pending = bytearray()
        try:
            async with incoming_send:
                while not collected.is_set() or not frames.empty():
                    data = await anyio.to_thread.run_sync(next_chunk)
                    if data is None:
                        continue
                    pending.extend(data)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending[:] = rest
                        if len(line) > MAX_FRAME:
                            raise ValueError("protocol frame exceeds limit")
                        try:
                            message = SessionMessage(
                                types.JSONRPCMessage.model_validate_json(line)
                            )
                        except Exception as error:
                            observed.failure = observed.failure or error
                            message = error
                        await incoming_send.send(message)
                    if len(pending) > MAX_FRAME:
                        raise ValueError("unterminated protocol frame exceeds limit")
                if pending:
                    await incoming_send.send(
                        ValueError("partial protocol frame at EOF")
                    )
        except anyio.BrokenResourceError, anyio.ClosedResourceError:
            discard.set()
        except Exception as error:
            observed.failure = observed.failure or error
            discard.set()

    async def writer():
        try:
            async with outgoing_receive:
                async for message in outgoing_receive:
                    data = (
                        message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        + "\n"
                    ).encode("utf-8")
                    if len(data) > MAX_FRAME:
                        raise ValueError("outbound protocol frame exceeds limit")
                    snapshot, error = await anyio.to_thread.run_sync(
                        partial(
                            write_frame,
                            owner,
                            data,
                            remaining(),
                            handoff=observed.write_observations.append,
                        )
                    )
                    if error is not None:
                        raise error
        except BaseException as error:
            observed.failure = observed.failure or error
            if not isinstance(error, Exception):
                raise
        finally:
            writer_done.set()

    primary = None
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(collector)
            group.start_soon(reader)
            group.start_soon(writer)
            try:
                yield incoming, outgoing
            finally:
                with anyio.CancelScope(shield=True):
                    await outgoing.aclose()
                    # Drain already queued frames before requesting EOF. This is
                    # the original envelope, not a new SDK teardown allowance.
                    with anyio.move_on_after(max(0, end - time.monotonic())):
                        await writer_done.wait()
                    try:
                        await anyio.to_thread.run_sync(
                            partial(owner.close_stdin, timeout_seconds=remaining())
                        )
                        observed.eof_delivered = True
                    except BaseException as error:
                        observed.eof_error = error
                        observed.failure = observed.failure or error
                        if not isinstance(error, Exception):
                            raise
                    discard.set()
                    with anyio.move_on_after(max(0, end - time.monotonic())):
                        await done.wait()
                    if not done.is_set():
                        observed.failure = observed.failure or TimeoutError(
                            "service did not settle within original envelope"
                        )
                    group.cancel_scope.cancel()
    except BaseException as error:
        primary = error
        raise
    finally:
        with anyio.CancelScope(shield=True):
            release_error = None
            try:
                status = await anyio.to_thread.run_sync(owner.shutdown)
                if not status.complete or status.failures:
                    observed.failure = observed.failure or RuntimeError(
                        f"transport cleanup incomplete or failed: {status}"
                    )
            except BaseException as error:
                release_error = error
            _close_memory(memory, primary=primary, release_error=release_error)
    if observed.failure is not None:
        raise observed.failure
    result = observed.result
    if (
        result is None
        or result.timed_out
        or result.returncode != 0
        or any(
            observed.terminals.get(s) is not StreamOutcome.EOF for s in ProcessStream
        )
    ):
        raise RuntimeError(
            f"service process/stream verification failed: {result}; stderr={bytes(observed.stderr_tail)!r}"
        )
