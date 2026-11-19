"""Lightweight Docker-free CLI peer; no MCP SDK startup cost in cleanup budgets."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

from tests import mcp_provenance as p


def main():
    value = json.loads(os.environ["FOOTMAN_MCP_STUB"])
    argv = sys.argv[1:]
    operation = (
        "launch"
        if argv[0] == "run"
        else "inspect"
        if argv[:2] == ["container", "inspect"]
        else "copy"
        if argv[0] == "cp"
        else "remove"
        if argv[0] == "rm"
        else "confirm"
        if argv[:2] == ["container", "ls"]
        else "mkdir"
        if "mkdir" in argv
        else "command"
    )
    record = Path(value["records"]) / f"{time.time_ns()}-{os.getpid()}.json"
    # Call records are ephemeral observations, not durable provenance. File and
    # directory fsync must not consume the native helper's cleanup allowance.
    p.write_observation(record, p.canonical({"operation": operation, "argv": argv}))
    status = value["status"] if value["failure"] == operation else 0
    if operation == "launch":
        data = (uuid.uuid7().hex * 2 + "\n").encode()
    elif operation == "inspect":
        data = (argv[-1] + "|running|sha256:" + "b" * 64 + "\n").encode()
    elif operation == "command":
        time.sleep(value["delay"])
        data = b"command stdout\x00\xff\n"
        sys.stderr.buffer.write(b"command stderr\x00\xfe\n")
    else:
        data = b""
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.flush()
    raise SystemExit(status)
