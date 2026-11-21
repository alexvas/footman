"""Real SDK cancellation must settle without EOF on inherited input."""

import os
import threading
import time
from types import SimpleNamespace
from typing import cast

import anyio
import pytest
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.server import BorrowedText


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("partial", [b"", b"partial frame", b"\xc3"])
@pytest.mark.sleepy
def test_sdk_stdout_failure_joins_reader_with_stdin_still_open(
    partial, lifecycle, monkeypatch
):
    ops = PosixDescriptorOps()
    read_fd, write_fd = os.pipe()
    reader = OwnedDescriptor(ops, read_fd, label="test inherited input")
    writer = OwnedDescriptor(ops, write_fd, label="test client input")
    released = threading.Event()
    entered = threading.Event()

    class Input(BorrowedText):
        def _readline(self, fd):
            entered.set()
            return super()._readline(fd)

    class Output:
        async def write(self, text):
            raise OSError("stdout failure")

        async def flush(self):
            raise AssertionError("flush after failed write")

    def rescue():
        # Keep a broken implementation from hanging the test runner. This is
        # not success: the assertion below requires cleanup before this fires.
        released.set()
        os.write(writer.fd, b"\n")

    timer = threading.Timer(2, rescue)
    try:
        if partial:
            os.write(writer.fd, partial)
        stream = os.fdopen(reader.fd, "rb", closefd=False)
        try:
            deadline = time.monotonic() + 30
            if lifecycle:
                from tests import mcp_lifecycle

                actual_reader = mcp_lifecycle.DeadlineReader

                class LifecycleReader(actual_reader):
                    def readline(self):
                        entered.set()
                        return super().readline()

                monkeypatch.setattr(mcp_lifecycle, "DeadlineReader", LifecycleReader)
                inherited = mcp_lifecycle.borrowed_input(
                    SimpleNamespace(buffer=stream),
                    SimpleNamespace(value={"service_deadline_monotonic": deadline}),
                )
                borrowed = BorrowedText(inherited.buffer)
            else:
                borrowed = Input(stream)

            async def exercise():
                async with stdio_server(
                    stdin=cast(anyio.AsyncFile[str], borrowed),
                    stdout=cast(anyio.AsyncFile[str], Output()),
                ) as (_, outgoing):
                    while not entered.is_set():
                        await anyio.sleep(0.001)
                    # Allow several empty polls before provoking SDK failure.
                    await anyio.sleep(0.12)
                    await outgoing.send(
                        SessionMessage(
                            types.JSONRPCMessage(
                                types.JSONRPCNotification(jsonrpc="2.0", method="test")
                            )
                        )
                    )
                    await anyio.sleep_forever()

            timer.start()
            with pytest.raises(ExceptionGroup, match="unhandled errors") as caught:
                anyio.run(exercise)
            assert "stdout failure" in repr(caught.value)
            assert not released.is_set(), "cleanup waited for the blocked input reader"
            assert not stream.closed
            assert not borrowed.eof_seen
            if lifecycle:
                assert inherited.buffer.deadline == deadline
                assert time.monotonic() < deadline
            os.fstat(writer.fd)
        finally:
            timer.cancel()
            timer.join()
            stream.close()  # closefd=False: the descriptor owner alone releases.
    finally:
        try:
            reader.close()
        finally:
            writer.close()


def test_lifecycle_polling_keeps_original_deadline(monkeypatch):
    from tests import mcp_lifecycle

    now = [10.0]
    polls = []

    def poll(readable, writable, exceptional, timeout):
        polls.append(timeout)
        now[0] += timeout
        return [], [], []

    monkeypatch.setattr(
        mcp_lifecycle, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    monkeypatch.setattr(mcp_lifecycle, "select", SimpleNamespace(select=poll))
    # Synthetic borrowed descriptor: the poll stub never touches the OS.
    reader = mcp_lifecycle.DeadlineReader(SimpleNamespace(fileno=lambda: 123), 10.12)
    reader.pending.extend(b"unfinished")

    async def exercise():
        assert await anyio.to_thread.run_sync(reader.readline) == b""
        assert await anyio.to_thread.run_sync(reader.readline) == b""

    anyio.run(exercise)
    assert polls == pytest.approx([0.05, 0.05, 0.02])
    assert reader.deadline == 10.12
    assert now[0] == 10.12
    assert reader.pending == b"unfinished"
