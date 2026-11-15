"""Recorded Docker/build boundary doubles for core-only orchestration contracts.

Never copied into normal host support or used as an installation source. All
acquisitions delegate to mcp_provenance's tested sole-owner read/write helpers.
"""

import os
import sys
import uuid
from pathlib import Path

from tests import mcp_provenance as p


def main(mode, control_path):
    control_path = Path(control_path)
    control = p.load(control_path)
    record = {
        "mode": mode,
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "environment_keys": sorted(os.environ),
        "support_origin": str(Path(p.__file__).resolve()),
        "simulation": True,
    }
    p.write(
        control_path.parent / "calls" / (uuid.uuid4().hex + ".json"),
        p.canonical(record),
    )
    if mode == "docker":
        p.require(
            sys.argv[1:] == ["image", "inspect", "--format", "{{.Id}}", control["tag"]],
            "unexpected Docker call; build/pull is forbidden",
        )
        outcome = control["inspect"]
    else:
        p.require(mode == "build", "unknown orchestration boundary double")
        p.require(
            len(sys.argv) == 3 and sys.argv[1] == "--manifest",
            "unexpected controlled build invocation",
        )
        outcome = control["build"]
        if outcome["status"] == 0:
            manifest = dict(control["manifest"])
            for artifact in ("wheel", "sdist"):
                p.write(Path(manifest[artifact + "_path"]), artifact.encode("ascii"))
            if control.get("swapped_wheel"):
                manifest["wheel_sha256"] = "0" * 64
            if control.get("manifest_fault") == "missing_wheel":
                manifest.pop("wheel_path")
            if control.get("manifest_fault") == "stale_source":
                manifest["source_identity"] = {
                    **manifest["source_identity"],
                    "sha256": "0" * 64,
                }
            p.write(Path(sys.argv[2]), p.canonical(manifest))
    # Borrow inherited output; no handle adoption/release, no output filtering.
    sys.stdout.buffer.write(bytes.fromhex(outcome["stdout_hex"]))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(bytes.fromhex(outcome["stderr_hex"]))
    sys.stderr.buffer.flush()
    return outcome["status"]
