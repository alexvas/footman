"""Build and independently bind one acceptance wheel; never reuse prior output."""

import argparse
import hashlib
import os
import platform
import re
import sys
from pathlib import Path

from _footman_acceptance import (
    BUILD_ROOTS,
    SOURCE_ROOTS,
    Tooling,
    canonical,
    checked_root,
    require,
)
from footman.filesystem.cleanup import CleanupFailures


def build(tool, manifest, wrapper):
    operation_path = manifest.with_name(manifest.stem + "-operation.json")
    operation = {
        "argv": ["uv", "build"],
        "cwd": str(tool.root),
        "attempted": False,
        "status": None,
        "pre_existing_dist": None,
        "predicted_destinations": None,
    }
    binding = None
    primary = None
    status = 1
    post = {"produced_dist": None}
    try:
        require(
            not os.path.lexists(manifest) and not os.path.lexists(operation_path),
            "evidence destination already exists",
        )
        sdist, wheel = tool.predict()
        pre_dist = tool.dist_inventory()
        operation["pre_existing_dist"] = pre_dist
        operation["predicted_destinations"] = [str(sdist), str(wheel)]
        conflicts = [
            entry["path"]
            for entry in pre_dist
            if entry["path"] in {str(sdist), str(wheel)}
        ]
        require(
            not conflicts,
            f"build root {tool.root}; conflicts: {', '.join(conflicts)}; no build/deletion/replacement occurred; remove only named conflicts externally or use a clean matching root's wrapper",
        )
        source, inventory = tool.inventory(SOURCE_ROOTS)
        inputs, input_inventory = tool.inventory(BUILD_ROOTS)
        revision = tool.revision()
        changes = tool.local_changes()
        operation["attempted"] = True
        try:
            operation.update(tool.command(["uv", "build"]))
            status = operation["status"] if operation["status"] is not None else 1
            require(status == 0, "uv build failed")
        finally:
            if tool.commands and tool.commands[-1]["argv"] == ["uv", "build"]:
                # Merge collection observations; never discard preflight state.
                # Also recover raw status if collection raised after child exit.
                operation.update(tool.commands[-1])
                status = operation["status"] if operation["status"] is not None else 1
            # Postchecks are independent, including after nonzero/failed collection.
            failures = CleanupFailures(sys.exception())

            def check_source():
                post["source_identity"], post["source_inventory"] = tool.inventory(
                    SOURCE_ROOTS
                )
                require(
                    post["source_identity"] == source,
                    "source inputs changed during build",
                )

            def check_inputs():
                post["build_input_identity"], post["build_input_inventory"] = (
                    tool.inventory(BUILD_ROOTS)
                )
                require(
                    post["build_input_identity"] == inputs,
                    "build inputs changed during build",
                )

            def check_dist():
                # Observation only: includes pre-existing entries, grants no
                # artifact selection/deletion authority, even after failure.
                post["produced_dist"] = tool.dist_inventory()

            def observed_check(name, action):
                try:
                    action()
                except BaseException as error:
                    detail = {
                        "check": name,
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                    post.setdefault("post_check_error", detail)
                    post.setdefault("post_check_errors", []).append(detail)
                    raise

            for name, action in (
                ("source", check_source),
                ("build_inputs", check_inputs),
                ("dist", check_dist),
            ):
                failures.run(
                    lambda name=name, action=action: observed_check(name, action),
                    ordinary=(Exception,),
                )
            selected = failures.complete()
            if selected is not None:
                raise selected
        output = operation["stderr"] + operation["stdout"]
        selected = {}
        attributed = re.findall(rb"^Successfully built (.+)\r?$", output, re.MULTILINE)
        require(len(attributed) == 2, "missing or ambiguous build output attribution")
        for kind, path in (("source distribution", sdist), ("wheel", wheel)):
            matches = [
                item for item in attributed if item.endswith(os.fsencode(path.name))
            ]
            require(len(matches) == 1, "missing or ambiguous " + kind + " attribution")
            observed = Path(os.fsdecode(matches[0]))
            if not observed.is_absolute():
                observed = tool.root / observed
            require(
                observed == path, "build output disagrees with predicted destination"
            )
            selected[path] = hashlib.sha256(tool.read(path)).hexdigest()
        binding = {
            "schema_version": 1,
            "environment": {"python": sys.version, "os": platform.platform()},
            "repository_root": str(tool.root),
            "wrapper_path": str(wrapper),
            "caller_cwd": os.getcwd(),
            "revision": revision,
            "local_changes": changes,
            "source_identity": source,
            "source_inventory": inventory,
            "build_input_identity": inputs,
            "build_input_inventory": input_inventory,
            "wheel_path": str(wheel),
            "wheel_sha256": selected[wheel],
            "sdist_path": str(sdist),
            "sdist_sha256": selected[sdist],
            "pre_existing_dist": pre_dist,
            "predicted_destinations": [str(sdist), str(wheel)],
            "produced_dist": post["produced_dist"],
        }
    except BaseException as error:
        primary = error
    failures = CleanupFailures(primary)
    operation.update(post)
    operation.setdefault("post_check_error", None)
    operation["error"] = (
        None if primary is None else type(primary).__name__ + ": " + str(primary)
    )

    prefix = manifest.stem + "-build-"
    retained = tool.command_evidence(operation, manifest.parent, prefix)
    failures.run(
        lambda: tool.retain_command(operation, manifest.parent, prefix),
        ordinary=(Exception,),
    )
    # Summary publication is independent of either stream's retention. These
    # paths are attempted destinations, not assertions that the files exist.
    failures.run(
        lambda: tool.write(operation_path, canonical(retained)),
        ordinary=(Exception,),
    )
    selected = failures.complete()
    if selected is None:
        selected = primary
    if selected is not None:
        if not isinstance(selected, Exception):
            raise selected
        print(str(selected), file=sys.stderr)
        return status if status != 0 else 1
    require(binding is not None, "missing successful binding")
    tool.write(manifest, canonical(binding))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    root = checked_root(__file__, "build-acceptance-wheel")
    tool = Tooling(root)
    manifest = tool.external(args.manifest)
    return build(tool, manifest, root / "scripts/build-acceptance-wheel")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
