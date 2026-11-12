"""Stdio MCP integration; inherited OS streams remain runtime-owned."""

from __future__ import annotations

import json
import logging
import os
import select
import sys
from typing import Any, BinaryIO, cast

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from footman.configuration import ConfiguredApplication, validate_request
from footman.filesystem.cleanup import CleanupFailures
from footman.output import OutputError, reserve_request
from footman.results import PublicationError, RetainedRun, _error

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"phase": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"}},
    "additionalProperties": False,
}


def diagnostic(message: str) -> str:
    """Prefix every physical generated diagnostic line, never raw retained data."""
    return "\n".join("footman: " + line for line in message.split("\n"))


class DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return diagnostic(super().format(record))


class BorrowedText:
    """UTF-8 async facade with no handle acquisition or release authority."""

    def __init__(self, buffer: BinaryIO) -> None:
        self.buffer = buffer
        self.eof_seen = False
        self._pending = bytearray()

    def __aiter__(self) -> BorrowedText:
        return self

    async def __anext__(self) -> str:
        try:
            fd = self.buffer.fileno()
        except OSError, AttributeError:
            # Non-descriptor adapters (including test deadline readers) retain
            # their own read semantics. Actual inherited input uses polling.
            data = await anyio.to_thread.run_sync(self.buffer.readline)
        else:
            data = await anyio.to_thread.run_sync(self._readline, fd)
        if not data:
            self.eof_seen = True
            raise StopAsyncIteration
        return data.decode("utf-8")

    def _readline(self, fd: int) -> bytes:
        # No acquisition, flag changes or release: the runtime owns fd. Poll
        # before raw reads so buffered readline cannot wait for a missing LF.
        # Keep the worker joined; cancellation is observed within one poll.
        while True:
            anyio.from_thread.check_cancelled()
            newline = self._pending.find(b"\n")
            if newline >= 0:
                data = bytes(self._pending[: newline + 1])
                del self._pending[: newline + 1]
                return data
            if not select.select([fd], [], [], 0.05)[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                data = bytes(self._pending)
                self._pending.clear()
                return data
            self._pending.extend(chunk)

    async def write(self, text: str) -> None:
        data = text.encode("utf-8")

        try:
            fd = self.buffer.fileno()
        except OSError, AttributeError:
            fd = None

        def perform() -> None:
            remaining = memoryview(data)
            while remaining:
                anyio.from_thread.check_cancelled()
                if fd is None:
                    written = self.buffer.write(remaining)
                else:
                    # Sole protocol writer; a writable pipe can accept up to
                    # PIPE_BUF bytes without blocking. Never alter inherited
                    # descriptor flags or enqueue bytes in a buffered stream.
                    if not select.select([], [fd], [], 0.05)[1]:
                        continue
                    written = os.write(fd, remaining[: select.PIPE_BUF])
                if written is None or written <= 0:
                    raise OSError("inherited protocol stdout made no progress")
                remaining = remaining[written:]

        await anyio.to_thread.run_sync(perform)

    async def flush(self) -> None:
        try:
            self.buffer.fileno()
        except OSError, AttributeError:
            await anyio.to_thread.run_sync(self.buffer.flush)
        else:
            # Descriptor writes above are unbuffered; there is nothing owned
            # by this facade to flush. Do not flush a borrowed runtime buffer.
            await anyio.lowlevel.checkpoint()

    async def aclose(self) -> None:
        # Scoped SDK/memory tasks are closed by their own contexts, not here.
        return None


def execute(
    application: ConfiguredApplication, arguments: dict[str, Any]
) -> dict[str, Any]:
    validate_request(application.config, arguments)
    try:
        output = reserve_request(application.config, arguments)
    except OutputError as error:
        names = {
            "stdout.txt": "stdoutPath",
            "stderr.txt": "stderrPath",
            "startup-stdout.txt": "startupStdoutPath",
            "startup-stderr.txt": "startupStderrPath",
            "result.json": "resultPath",
        }
        return {
            "outcome": "output_failed",
            "return_code": None,
            **{names[name]: str(path) for name, path in error.paths.items()},
            "diagnostics": {
                "failure": _error(error),
                "cause": _error(error.__cause__),
                "manifest_finalized": False,
            },
        }
    try:
        run = RetainedRun(application.config, output)
    except BaseException as error:
        failures = CleanupFailures(error)
        failures.run(output.close, ordinary=(OSError,))
        selected = failures.complete() or error
        # complete() propagates interruptions before this response boundary.
        names = {
            "stdout.txt": "stdoutPath",
            "stderr.txt": "stderrPath",
            "startup-stdout.txt": "startupStdoutPath",
            "startup-stderr.txt": "startupStderrPath",
            "result.json": "resultPath",
        }
        return {
            "outcome": "output_failed",
            "return_code": None,
            **{names[name]: str(path) for name, path in output.paths.items()},
            "diagnostics": {
                "failure": _error(selected),
                "manifest_finalized": False,
                "cleanup_failure": _error(output.cleanup_failure),
            },
        }
    try:
        return run.execute()
    except PublicationError as error:
        return error.response


def create_server(application: ConfiguredApplication) -> Server:
    server: Server = Server("footman")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="run_privileged_command",
                description="Execute the operator-configured command against a fixed staged snapshot; report execution, not test sufficiency.",
                inputSchema=TOOL_SCHEMA,
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name != "run_privileged_command":
            raise ValueError("unknown tool")
        validate_request(application.config, arguments)
        # Accepted work outlives request cancellation and client EOF. The worker
        # retains all run owners and its configured action/cleanup deadlines.
        with anyio.CancelScope(shield=True):
            response = await anyio.to_thread.run_sync(execute, application, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(response))],
            structuredContent=response,
            isError=response["outcome"] != "succeeded",
        )

    return server


async def run(application: ConfiguredApplication) -> None:
    server = create_server(application)
    borrowed_input = BorrowedText(sys.stdin.buffer)
    stdin = cast(anyio.AsyncFile[str], borrowed_input)
    stdout = cast(anyio.AsyncFile[str], BorrowedText(sys.stdout.buffer))
    try:
        async with stdio_server(stdin=stdin, stdout=stdout) as (incoming, outgoing):
            await server.run(incoming, outgoing, server.create_initialization_options())
    except* anyio.ClosedResourceError, anyio.BrokenResourceError:
        # The SDK may finish a shielded accepted request after its session's
        # response memory stream has closed. EOF is independently observed;
        # unrelated failures and process-control interruptions still propagate.
        if not borrowed_input.eof_seen:
            raise


def serve(application: ConfiguredApplication) -> int:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DiagnosticFormatter("%(levelname)s: %(message)s"))
    root = logging.getLogger()
    previous = root.handlers[:]
    root.handlers = [handler]
    primary: BaseException | None = None
    try:
        anyio.run(run, application)
    except BaseException as error:
        primary = error
    finally:
        root.handlers = previous
        failures = CleanupFailures(primary)
        # This releases only logging bookkeeping, never borrowed stderr.
        failures.run(handler.close, ordinary=(OSError,))
        selected = failures.complete() or primary
    if selected is not None:
        raise selected
    return 0
