"""Copied Docker CLI simulation: literal fresh-wheel interpreter and support origins."""

import argparse
import base64
import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--support-root", required=True)
    prefix, argv = sys.argv[1:].index("--"), sys.argv[1:]
    args = parser.parse_args(argv[:prefix])
    root = Path(args.support_root)
    if not sys.flags.safe_path or root != root.resolve():
        raise ValueError("unsafe relocated stub loader")
    spec = importlib.util.spec_from_file_location(
        "tests",
        root / "tests/__init__.py",
        submodule_search_locations=[str(root / "tests")],
    )
    if spec is None or spec.loader is None:
        raise ValueError("missing copied stub package")
    package = importlib.util.module_from_spec(spec)
    sys.modules["tests"] = package
    spec.loader.exec_module(package)
    import footman.process
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import dispatcher_harness, docker_cli_stub
    from tests import mcp_provenance as p

    with DirectoryDescriptor.open_secure_path(p.OPS, root) as directory:
        p.require(set(directory.list_names()) == {"tests"}, "stub support root shadow")
    p.require(
        Path(footman.process.__file__).is_relative_to(sys.prefix),
        "stub loaded checkout foundation",
    )
    p.require(
        all(
            Path(module.__file__).parent == root / "tests"
            for module in (package, docker_cli_stub, dispatcher_harness, p)
        ),
        "stub support origin mismatch",
    )
    arguments = argv[prefix + 1 :]
    operation = (
        "launch"
        if arguments[0] == "run"
        else "inspect"
        if arguments[:2] == ["container", "inspect"]
        else "copy"
        if arguments[0] == "cp"
        else "remove"
        if arguments[0] == "rm"
        else "confirm"
        if arguments[:2] == ["container", "ls"]
        else "mkdir"
        if "mkdir" in arguments
        else "command"
    )
    directory = Path(os.environ["FOOTMAN_STUB_RECORDS"])
    identifier = str(time.time_ns()) + "-" + str(os.getpid())
    # This is an ephemeral stub observation, not confirmed provenance evidence.
    # Complete write/close is required; power-loss durability must not stall the
    # real CLI inside its original short execution/cleanup allowance.
    p.write_observation(
        directory / (identifier + "-origin.json"),
        p.canonical(
            {
                "interpreter": sys.executable,
                "pid": os.getpid(),
                "incarnation": p.read(Path(f"/proc/{os.getpid()}/stat"))
                .rsplit(b")", 1)[1]
                .split()[19]
                .decode(),
                "context": "wheel",
                "foundation": str(footman.process.__file__),
                "support_root": str(root),
                "stub": str(docker_cli_stub.__file__),
                "harness": str(dispatcher_harness.__file__),
            }
        ),
    )
    if operation == "launch":
        output = (uuid.uuid4().hex * 2 + "\n").encode()
    elif operation == "inspect":
        output = (arguments[-1] + "|running|sha256:" + "b" * 64 + "\n").encode()
    elif operation == "command":
        output = b"relocated stdout\x00\xff\n"
    else:
        output = b""
    scenario = {
        "record": str(directory / (identifier + "-call.json")),
        "stdout": base64.b64encode(output).decode(),
        "stderr": base64.b64encode(
            b"relocated stderr\x00\xfe\n" if operation == "command" else b""
        ).decode(),
        "status": 0,
        "delay": float(os.environ.get("FOOTMAN_STUB_DELAY", "0"))
        if operation == "command"
        else 0,
        "container_id": None,
        "expected_argv": arguments,
    }
    os.environ["FOOTMAN_STUB_SCENARIO"] = json.dumps(scenario)
    sys.argv = [sys.argv[0], *arguments]
    return docker_cli_stub.main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        for line in str(error).splitlines():
            print("relocated-stub: " + line[:512], file=sys.stderr)
        raise SystemExit(1) from error
