"""Real SDK output backpressure must not trap transport cleanup."""

import fcntl
import os
import select
import threading
from types import SimpleNamespace
from typing import BinaryIO, cast

import anyio
import pytest
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.server import BorrowedText


@pytest.mark.parametrize("failure", ["read", "cancel"])
@pytest.mark.parametrize("size", [1, 32768])
@pytest.mark.sleepy
def test_sdk_input_failure_joins_backpressured_output_without_drain(failure, size):
    ops = PosixDescriptorOps()
    read_fd, write_fd = os.pipe()
    reader = OwnedDescriptor(ops, read_fd, label="test client stdout")
    writer = OwnedDescriptor(ops, write_fd, label="test inherited stdout")
    entered = threading.Event()
    rescued = threading.Event()
    finished = threading.Event()

    def rescue():
        # Negative-control safety only: successful cleanup never drains stdout.
        rescued.set()
        while not finished.is_set():
            if select.select([reader.fd], [], [], 0.05)[0]:
                os.read(reader.fd, 65536)

    timer = threading.Timer(2, rescue)
    try:
        capacity = fcntl.fcntl(writer.fd, fcntl.F_GETPIPE_SZ)
        filler = b"x" * capacity
        assert os.write(writer.fd, filler) == capacity
        stream = os.fdopen(writer.fd, "wb", closefd=False)
        try:

            class Buffer:
                def fileno(self):
                    entered.set()
                    return stream.fileno()

                def write(self, data):
                    entered.set()
                    return stream.write(data)

                def flush(self):
                    entered.set()
                    stream.flush()

            output = BorrowedText(cast(BinaryIO, Buffer()))

            async def exercise():
                with anyio.CancelScope() as scope:

                    class Input:
                        def __aiter__(self):
                            return self

                        async def __anext__(self):
                            while not entered.is_set():
                                await anyio.sleep(0.001)
                            # Exercise several blocked polls before failing.
                            await anyio.sleep(0.15)
                            if failure == "read":
                                raise OSError("stdin read failure")
                            scope.cancel()
                            await anyio.sleep_forever()

                    async with stdio_server(
                        stdin=cast(anyio.AsyncFile[str], Input()),
                        stdout=cast(anyio.AsyncFile[str], output),
                    ) as (_, outgoing):
                        await outgoing.send(
                            SessionMessage(
                                types.JSONRPCMessage(
                                    types.JSONRPCNotification(
                                        jsonrpc="2.0", method="x" * size
                                    )
                                )
                            )
                        )
                        await anyio.sleep_forever()

            timer.start()
            if failure == "read":
                with pytest.raises(ExceptionGroup) as caught:
                    anyio.run(exercise)
                assert "stdin read failure" in repr(caught.value)
            else:
                anyio.run(exercise)
            assert not rescued.is_set(), "cleanup required the client to drain stdout"
            assert not stream.closed
            assert os.read(reader.fd, capacity) == filler
            os.fstat(writer.fd)
        finally:
            finished.set()
            timer.cancel()
            timer.join()
            stream.close()  # closefd=False; writer alone releases its descriptor.
    finally:
        try:
            reader.close()
        finally:
            writer.close()


@pytest.mark.parametrize("fault", [None, "zero", "write", "interrupt"])
def test_borrowed_output_bounded_raw_writes_and_unbuffered_flush(monkeypatch, fault):
    from footman import server

    captured = bytearray()
    polls = []
    writes = []
    primary = (
        KeyboardInterrupt("write interrupted")
        if fault == "interrupt"
        else OSError("write failed")
    )

    def poll(readable, writable, exceptional, timeout):
        polls.append(timeout)
        return [], writable if len(polls) > 2 else [], []

    def write(fd, data):
        assert fd == 123
        writes.append(bytes(data))
        assert len(data) <= 4
        if fault == "zero":
            return 0
        if fault is not None:
            raise primary
        captured.extend(data[:2])
        return min(2, len(data))

    def forbidden(*args):
        raise AssertionError("borrowed buffer write/flush/close must not be called")

    # Synthetic fd; only the stubbed readiness and write operations see it.
    buffer = SimpleNamespace(
        fileno=lambda: 123, write=forbidden, flush=forbidden, close=forbidden
    )
    monkeypatch.setattr(server, "select", SimpleNamespace(select=poll, PIPE_BUF=4))
    monkeypatch.setattr(server, "os", SimpleNamespace(write=write))
    output = BorrowedText(cast(BinaryIO, buffer))
    text = "split UTF-8: \u2603\n"

    async def exercise():
        try:
            await output.write(text)
            await output.flush()
        finally:
            await output.aclose()
            await output.aclose()

    if fault is None:
        anyio.run(exercise)
        assert captured == text.encode("utf-8")
    elif fault == "zero":
        with pytest.raises(OSError, match="made no progress"):
            anyio.run(exercise)
    else:
        with pytest.raises(type(primary)) as caught:
            anyio.run(exercise)
        assert caught.value is primary
    assert all(timeout == 0.05 for timeout in polls)
    expected_writes = (len(text.encode("utf-8")) + 1) // 2 if fault is None else 1
    assert len(writes) == expected_writes


def test_borrowed_descriptor_flush_observes_cancellation_without_buffer_flush():
    def forbidden():
        raise AssertionError("must not flush runtime-owned buffering")

    output = BorrowedText(
        cast(BinaryIO, SimpleNamespace(fileno=lambda: 123, flush=forbidden))
    )

    async def exercise():
        with anyio.CancelScope() as scope:
            scope.cancel()
            await output.flush()
            raise AssertionError("flush ignored cancellation")
        assert scope.cancelled_caught
        await output.aclose()

    anyio.run(exercise)
