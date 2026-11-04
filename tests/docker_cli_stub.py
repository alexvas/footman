"""Executable-boundary simulation only: never talks to a Docker daemon."""

import base64
import json
import os
import sys
import time

from tests.dispatcher_harness import write_bytes


def main():
    scenario = json.loads(os.environ["FOOTMAN_STUB_SCENARIO"])
    argv = sys.argv[1:]
    identity = scenario["container_id"]
    if identity is None:
        identity = next(
            (
                arg
                for arg in argv
                if len(arg) == 64 and all(c in "0123456789abcdefABCDEF" for c in arg)
            ),
            None,
        )
    if identity is None and "expected_argv" in scenario:
        # Startup has no ID operand; cp embeds its ID before the colon.
        # Attribute only a full scripted stdout record or full target prefix.
        raw = base64.b64decode(scenario["stdout"]).removesuffix(b"\n")
        candidates = [arg.partition(":")[0] for arg in argv]
        try:
            candidates.append(raw.decode("ascii"))
        except UnicodeDecodeError:
            pass
        identity = next(
            (
                value
                for value in candidates
                if len(value) == 64
                and all(c in "0123456789abcdefABCDEF" for c in value)
            ),
            None,
        )
    unexpected = "expected_argv" in scenario and argv != scenario["expected_argv"]
    status = 97 if unexpected else scenario["status"]
    write_bytes(
        scenario["record"],
        json.dumps(
            {"argv": argv, "container_id": identity}
            | (
                {
                    "status": status,
                    "stdin_eof": sys.stdin.buffer.read(1) == b"",
                }
                if "expected_argv" in scenario
                else {}
            )
        ).encode(),
    )
    if unexpected:
        sys.stderr.write("unexpected sequenced Docker argv\n")
        return 97
    time.sleep(scenario["delay"])
    # Standard streams are borrowed from the interpreter, not acquired here.
    sys.stdout.buffer.write(
        base64.b64decode(scenario["stdout"]) * scenario.get("stdout_repeat", 1)
    )
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(
        base64.b64decode(scenario["stderr"]) * scenario.get("stderr_repeat", 1)
    )
    sys.stderr.buffer.flush()
    return scenario["status"]


if __name__ == "__main__":
    raise SystemExit(main())
