"""Docker CLI simulation only: never execute exported code or contact a daemon.

The integration driver embeds the recorded state path in its private executable;
there is no production runner environment/control switch. Observations here are
scripted Docker responses, not evidence of container semantics.
"""

import base64
import os
import sys
import time
import uuid
from pathlib import Path

from tests import mcp_provenance as p

IMAGE = "sha256:" + "a" * 64


def main(directory, argv, *, observed_uid=0):
    root = Path(directory)
    record = {"argv": argv, "simulation": True}
    status, stdout, stderr = 0, b"", b""
    selected = None
    state = None
    if argv == ["--version"]:
        stdout = b"Docker CLI simulation (not host proof)\n"
    elif argv[:2] == ["image", "inspect"]:
        p.require(
            argv[2:] == ["--format", "{{.Id}}", "footman-sandbox:latest"],
            "unexpected image inspection",
        )
        stdout = (IMAGE + "\n").encode()
    elif argv[:1] == ["run"]:
        p.require(
            argv[1:4] == ["--detach=true", "--pull=never", "--entrypoint=sleep"],
            "unexpected launch",
        )
        p.require(argv[4:] == ["--", IMAGE, "infinity"], "literal image changed")
        selected = uuid.uuid4().hex + uuid.uuid4().hex
        state = {"active": True, "destination": None, "copied": None}
        stdout = (selected + "\n").encode()
    elif argv[:2] == ["container", "inspect"]:
        selected = argv[-1]
        state = p.load(root / (selected + ".json"))
        p.require(state["active"], "inspection of removed simulated container")
        stdout = (selected + "|running|" + IMAGE + "\n").encode()
    elif argv[:2] == ["container", "ls"]:
        p.require(
            argv[:5] == ["container", "ls", "--all", "--no-trunc", "--filter"],
            "unexpected confirmation",
        )
        selected = argv[5].removeprefix("id=")
        state = p.load(root / (selected + ".json"))
        stdout = (selected + "\n").encode() if state["active"] else b""
    elif argv[:2] == ["rm", "-f"]:
        selected = argv[2]
        state = p.load(root / (selected + ".json"))
        state["active"] = False
        stdout = (selected + "\n").encode()
    elif argv[:1] == ["cp"]:
        source, target = argv[1:]
        selected, destination = target.split(":", 1)
        state = p.load(root / (selected + ".json"))
        p.require(
            source.endswith("/.") and destination == state["destination"] + "/",
            "copy operands changed",
        )
        source = Path(source[:-2])
        state["copied"] = {
            "payload": base64.b64encode(p.read(source / "payload")).decode(),
            "binary": base64.b64encode(p.read(source / "binary")).decode(),
            "payload_mode": (source / "payload").stat().st_mode & 0o777,
            "wrapper_mode": (source / "probe-wrapper").stat().st_mode & 0o777,
            "link": (source / "internal-link").is_symlink(),
            "target": os.readlink(source / "internal-link"),
            "wrapper": p.read(source / "probe-wrapper").decode(),
        }
    elif argv[:1] == ["exec"]:
        p.require(argv[1:3] == ["-u", "root"], "execution identity changed")
        if argv[3] != "-w":
            selected, command = argv[3], argv[4:]
            state = p.load(root / (selected + ".json"))
            p.require(command[:2] == ["mkdir", "--"], "unexpected preparation")
            destination = command[2]
            if destination == "/tmp" or destination.endswith("/source"):
                status, stderr = (
                    1,
                    b"simulated mkdir: existing destination or missing parent\n",
                )
            else:
                state["destination"] = destination
        else:
            destination, selected, command = argv[4], argv[5], argv[6:]
            state = p.load(root / (selected + ".json"))
            p.require(
                state["active"]
                and state["destination"] == destination
                and state["copied"] is not None,
                "command before successful copy",
            )
            copied = state["copied"]
            program = (
                command[2] if command[:2] == ["python3", "-c"] else copied["wrapper"]
            )
            if "v.update(alive=" in program:
                value = {
                    "pid": 42,
                    "start_ticks": "100",
                    "uid": 0,
                    "alive": True,
                    "file_uid": 0,
                    "file_regular": True,
                    "file_bytes": base64.b64encode(
                        b"container-only root bytes\x00\xff\n"
                    ).decode(),
                    "link_uid": 0,
                    "link_is_symlink": True,
                    "link_target": "root-owned-file",
                    "payload": base64.b64encode(
                        b"container changed payload\n"
                    ).decode(),
                }
                stdout = p.canonical(value) + b"\n"
            elif "root-owned-file" in program:
                stdout = (
                    p.canonical({"pid": 42, "start_ticks": "100", "uid": 0}) + b"\n"
                )
                if command[-1] == "timeout":
                    # This real local CLI child is terminated by its sole owner;
                    # survivor presence is separately scripted by OBSERVE above.
                    p.write(root / (uuid.uuid4().hex + ".event"), p.canonical(record))
                    sys.stdout.buffer.write(stdout)
                    sys.stdout.buffer.flush()
                    time.sleep(60)
            else:
                arguments = (
                    command[3:] if command[:2] == ["python3", "-c"] else command[1:]
                )
                value = {
                    key: value for key, value in copied.items() if key != "wrapper"
                }
                value.update(
                    uid=observed_uid,
                    cwd=destination,
                    argv=arguments,
                    entries=["binary", "internal-link", "payload", "probe-wrapper"],
                )
                stdout = p.canonical(value) + b"\nSTDOUT\x00\xff\n"
                stderr = b"STDERR\x00\x80\n"
                status = int(arguments[-1])
    else:
        raise AssertionError("unexpected Docker CLI: " + repr(argv))
    if selected is not None and state is not None:
        path = root / (selected + ".json")
        if path.exists():
            path.unlink()  # Only this recorded simulation state, never a host resource.
        p.write(path, p.canonical(state))
    record.update(status=status, stdout_hex=stdout.hex(), stderr_hex=stderr.hex())
    p.write(root / (uuid.uuid4().hex + ".event"), p.canonical(record))
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return status
