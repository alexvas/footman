"""Development tool spy: mutate a private install before runner expectations.

This is not a production dependency, wheel client, or normal host copy input.
It delegates native uv through the existing sole process owner, preserving the
original environment so a bootstrap-PYTHONPATH leak cannot be cleaned by the spy.
"""

import base64
import csv
import hashlib
import io
import os
import sys
import uuid
from pathlib import Path

from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel


def replace(path, data):
    path.unlink()
    p.write(path, data)


def mutate(site, fault, replacement):
    package = site / "footman"
    changes = []
    if fault == "swapped-copy":
        for entry in p.sources(replacement):
            target = package / entry["path"]
            replace(target, p.read(replacement / entry["path"]))
            changes.append(str(target))
    else:
        target = package / ("__main__.py" if fault == "stale-entry" else "container.py")
        original = p.read(target)
        changed = (
            b"raise RuntimeError('STALE_ENTRY_EXECUTED')\n" + original
            if fault == "stale-entry"
            else original + b"\n# stale before runner expectation generation\n"
        )
        replace(target, changed)
        changes.append(str(target))
    if fault == "self-consistent-stale":
        record = site / "footman-0.1.0.dist-info/RECORD"
        rows = list(csv.reader(io.StringIO(p.read(record).decode())))
        for row in rows:
            if row[0] == "footman/container.py":
                data = p.read(package / "container.py")
                row[1] = "sha256=" + base64.urlsafe_b64encode(
                    hashlib.sha256(data).digest()
                ).decode().rstrip("=")
                row[2] = str(len(data))
        output = io.StringIO()
        csv.writer(output).writerows(rows)
        replace(record, output.getvalue().encode())
        changes.append(str(record))
    return changes


class NativeToolFailure(Exception):
    def __init__(self, status):
        super().__init__(f"native tool exited {status}")
        self.status = status


def main():
    args = sys.argv[1:]
    directory = Path(os.environ["FOOTMAN_HOST_TOOL_RECORDS"])
    if os.environ.get("FOOTMAN_HOST_TOOL_BLOCK_INSTALL") == "1" and "install" in args:
        p.write(
            directory / (uuid.uuid4().hex + ".json"),
            p.canonical(
                {
                    "argv": args,
                    "native_attempted": False,
                    "status": None,
                    "environment_keys": sorted(os.environ),
                    "mutation_members": [],
                }
            ),
        )
        raise RuntimeError(
            "forbidden installation of a scripted archive control was blocked"
        )
    result = wheel.command(
        [os.environ["FOOTMAN_HOST_TOOL_REAL_UV"], *args],
        cwd=Path.cwd(),
        env=dict(os.environ),
        timeout_seconds=120,
    )
    value = {
        "argv": args,
        "cwd": str(Path.cwd()),
        "environment_keys": sorted(os.environ),
        "status": result["status"],
        "stdout_base64": base64.b64encode(result["stdout"]).decode(),
        "stderr_base64": base64.b64encode(result["stderr"]).decode(),
        "mutation_members": [],
    }
    from footman.filesystem.cleanup import CleanupFailures

    primary = NativeToolFailure(result["status"]) if result["status"] != 0 else None
    failures = CleanupFailures(primary)

    def inspect_installation():
        p.require(
            "PYTHONPATH" not in os.environ,
            "bootstrap PYTHONPATH reached wheel installation tool",
        )
        fault = os.environ.get("FOOTMAN_HOST_TOOL_FAULT", "none")
        if result["status"] == 0 and "install" in args and fault != "none":
            if "--python" in args:
                python = Path(args[args.index("--python") + 1])
            else:
                candidates = [
                    argument.split("=", 1)[1]
                    for argument in args
                    if argument.startswith("--python=")
                ]
                p.require(
                    len(candidates) == 1,
                    "install spy requires an explicit target interpreter",
                )
                python = Path(candidates[0])
            root = python.parent.parent
            p.require(
                python.is_absolute()
                and root.is_relative_to(Path("/tmp"))
                and not root.is_relative_to(Path(__file__).resolve().parents[1]),
                "install spy refuses a non-fixture target",
            )
            scheme = wheel.command(
                [
                    python,
                    "-P",
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                cwd=Path.cwd(),
                env=dict(os.environ),
                timeout_seconds=20,
            )
            p.require(
                scheme["status"] == 0, "install spy cannot resolve the intended scheme"
            )
            site = Path(scheme["stdout"].decode().strip())
            p.require(
                site.is_relative_to(root), "install scheme escaped the private venv"
            )
            value["mutation_members"] = mutate(
                site, fault, Path(os.environ["FOOTMAN_HOST_TOOL_REPLACEMENT"])
            )
            value["site"] = str(site)

    def deliver(stream, data):
        if stream.write(data) != len(data):
            raise OSError("short native-tool stream delivery")

    # Every independent observation/delivery is attempted even when another
    # fails. An actual interruption outranks native status; ordinary observer
    # failures cannot replace the first native nonzero status.
    failures.run(inspect_installation, ordinary=(OSError, ValueError))
    failures.run(
        lambda: p.write(directory / (uuid.uuid4().hex + ".json"), p.canonical(value)),
        ordinary=(OSError, ValueError),
    )
    failures.run(
        lambda: deliver(sys.stdout.buffer, result["stdout"]), ordinary=(OSError,)
    )
    failures.run(
        lambda: deliver(sys.stderr.buffer, result["stderr"]), ordinary=(OSError,)
    )
    error = failures.complete()
    if primary is not None:
        raise SystemExit(result["status"]) from primary
    if error is not None:
        raise error
    raise SystemExit(0)


if __name__ == "__main__":
    main()
