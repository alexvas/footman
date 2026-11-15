"""Recorded Docker/build boundary doubles for core-only orchestration contracts.

Never copied into normal host support or used as an installation source. All
acquisitions delegate to mcp_provenance's tested sole-owner read/write helpers.
"""

import os
import shutil
import stat
import sys
import uuid
from pathlib import Path

from footman.filesystem.descriptors import DirectoryDescriptor
from tests import mcp_provenance as p


def install_reservation_recorder(base, journal):
    """Fixture interpreter probe; persist successes before returning to caller.

    Not a production hook or ownership inferred from a report/directory diff.
    A failed mkdir grants nothing; an incomplete receipt leaves unknown resources
    unowned. The parent consumes these files independently of probe stdout.
    """
    base = Path(base)
    journal = Path(journal)
    mkdir = os.mkdir

    def recorded(path, mode=0o777, *, dir_fd=None):
        result = mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is None:
            absolute = Path(os.path.abspath(path))
        else:
            # Resolve the borrowed parent for attribution only, then securely
            # reopen it below; no ownership of dir_fd is acquired or released.
            absolute = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
        if absolute == base or absolute.parent == base:
            with DirectoryDescriptor.open_secure_path(p.OPS, absolute.parent) as parent:
                info = parent.stat_child(absolute.name, follow_symlinks=False)
                p.require(stat.S_ISDIR(info.st_mode), "reserved directory changed type")
                p.write(
                    journal / (uuid.uuid4().hex + ".json"),
                    p.canonical(
                        {
                            "operation": "exclusive-mkdir-success",
                            "path": str(absolute),
                            "device": info.st_dev,
                            "inode": info.st_ino,
                        }
                    ),
                )
        return result

    os.mkdir = recorded


def main(mode, control_path):
    control_path = Path(control_path)
    control = p.load(control_path)
    record = {
        "mode": mode,
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "environment_keys": sorted(os.environ),
        "resolved_docker": shutil.which("docker"),
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
