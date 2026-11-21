"""Run the complete permanent green gate without narrowing source/test scope.

All child ownership and output forwarding delegate to python_checks.run_streaming
and its existing OwnedProcess path. This module acquires no descriptors or pipes.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_checks import run_streaming  # noqa: E402 -- CLI support path is set first.

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = (
    ("scripts/check-python-lint",),
    ("scripts/check-python-format",),
    ("scripts/check-python-compile",),
    ("scripts/check-types",),
    *(
        (
            ".venv/bin/python",
            "-m",
            "pytest",
            "tests",
            "-m",
            marker,
            "-n",
            workers,
            "--dist",
            "worksteal",
            "--tb=short",
            "-q",
        )
        for marker, workers in (
            ("not sleepy and not auto_stress", "auto"),
            ("auto_stress", "8"),
            ("sleepy and not auto_stress", "8"),
        )
    ),
)


def main(*, run=run_streaming) -> int:
    """Run all fast checks; run full pytest only after a successful fast block."""
    status = 0
    for index, argv in enumerate(COMMANDS):
        if index >= 4 and status:
            print("Fast checks failed; pytest was not run.", file=sys.stderr)
            break
        command = shlex.join(argv)
        record = {
            "check": index + 1,
            "group": index - 3 if index >= 4 else None,
            "command": command,
        }
        print("\n$ " + command, flush=True)
        print("G-record " + json.dumps({**record, "event": "start"}), flush=True)
        try:
            result = run(argv, root=ROOT)
        except BaseException as exc:
            print(
                "G-record "
                + json.dumps(
                    {
                        **record,
                        "event": "end",
                        "raw_status": None,
                        "clean": None,
                        "lifecycle_failures": [],
                        "exception": repr(exc),
                    }
                ),
                flush=True,
            )
            raise
        print(
            "G-record "
            + json.dumps(
                {
                    **record,
                    "event": "end",
                    "raw_status": result.returncode,
                    "clean": result.clean,
                    "lifecycle_failures": [
                        repr(failure) for failure in result.failures
                    ],
                }
            ),
            flush=True,
        )
        code = result.returncode
        if not result.clean or code is None:
            code = code or 1
            for failure in result.failures:
                print(f"check lifecycle failure: {failure}", file=sys.stderr)
        if code and not status:
            status = code if code > 0 else 128 - code
        if index >= 4 and status:
            break
    return status


if __name__ == "__main__":
    if len(sys.argv) != 1:
        sys.exit("check-green does not support narrowing or mutation arguments")
    sys.exit(main())
