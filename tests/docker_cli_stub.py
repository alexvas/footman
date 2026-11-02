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
    write_bytes(
        scenario["record"],
        json.dumps({"argv": argv, "container_id": identity}).encode(),
    )
    time.sleep(scenario["delay"])
    # Standard streams are borrowed from the interpreter, not acquired here.
    sys.stdout.buffer.write(base64.b64decode(scenario["stdout"]))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(base64.b64decode(scenario["stderr"]))
    sys.stderr.buffer.flush()
    return scenario["status"]


if __name__ == "__main__":
    raise SystemExit(main())
