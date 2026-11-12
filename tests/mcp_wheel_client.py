"""Copied Phase-9 client bootstrap; no sys.path insertion or subprocess SDK owner."""

import importlib.util
import os
import sys
from pathlib import Path


def main():
    if not sys.flags.safe_path:
        raise RuntimeError("wheel client requires interpreter -P")
    # Resolve only the recorded sibling support package. Unlike a script-dir
    # fallback, this does not insert the copy root (or cwd) into sys.path.
    package = Path(__file__).absolute().parent
    spec = importlib.util.spec_from_file_location(
        "tests", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("missing copied test-support package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tests"] = module
    spec.loader.exec_module(module)
    import anyio
    from mcp import ClientSession

    from tests import mcp_provenance as p
    from tests import mcp_transport as transport

    p.require(
        importlib.util.find_spec("pytest") is None
        and importlib.util.find_spec("xdist") is None,
        "service venv unexpectedly contains pytest tooling",
    )
    request = p.load(Path(os.environ["FOOTMAN_WHEEL_REQUEST"]))
    p.require(
        set(request)
        == {
            "argv",
            "origin",
            "cwd",
            "evidence",
            "report",
            "wait_seconds",
            "lifecycle_binding",
        },
        "invalid wheel-client request",
    )
    for name in ("origin", "cwd", "evidence", "report"):
        p.require(
            isinstance(request[name], str) and Path(request[name]).is_absolute(),
            "wheel-client paths must be absolute",
        )
    manifest = p.load(request["origin"])
    p.require(manifest["context"] == "wheel", "wheel client requires archive authority")
    argv = request["argv"]
    p.require(
        isinstance(argv, list)
        and all(isinstance(arg, str) for arg in argv)
        and len(argv) >= 10,
        "invalid service argv",
    )
    expected = [
        sys.executable,
        "-P",
        str(package / "mcp_server_child.py"),
        "--expected-origin",
        request["origin"],
        "--entrypoint",
        manifest["entrypoint"]["script_path"],
    ]
    if "--lifecycle" in argv:
        from tests import mcp_lifecycle

        position = argv.index("--lifecycle")
        p.require(position + 1 < len(argv), "missing lifecycle input")
        path = argv[position + 1]
        p.require(
            request["lifecycle_binding"] is not None,
            "missing original lifecycle binding",
        )
        mcp_lifecycle.validate_input(
            path,
            binding=request["lifecycle_binding"],
            forbidden=[
                request["origin"],
                request["evidence"],
                request["report"],
                argv[-1],
            ],
        )
        expected += ["--lifecycle", path]
    else:
        p.require(request["lifecycle_binding"] is None, "unexpected lifecycle binding")
    p.require(
        Path(argv[-1]).is_absolute(), "service configuration path must be absolute"
    )
    expected += ["--", "--config", argv[-1]]
    p.require(argv == expected, "noncanonical service argv")
    p.require(
        type(request["wait_seconds"]) in {int, float}
        and 0 <= request["wait_seconds"] <= 5,
        "invalid client wait budget",
    )
    # POST-IMPORT attribution: copied transport already imported the installed
    # foundation. This is not a guarantee that no earlier imports executed.
    entries = p.verify(manifest)
    import footman.process

    p.require(
        Path(footman.process.__file__).is_relative_to(Path(sys.prefix)),
        "wheel client imported checkout foundation",
    )
    p.require(
        package == Path(p.__file__).parent
        and Path(transport.__file__) == package / "mcp_transport.py",
        "client/support copy roots differ",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST", "FOOTMAN_WHEEL_REQUEST"))
    }
    env["PYTHONNOUSERSITE"] = "1"
    parser = p.AttestationParser(entries)
    observation = transport.TransportObservation()
    state = {"exited_before_client_eof": False}

    async def run():
        async with transport.stdio_transport(
            request["argv"],
            cwd=request["cwd"],
            env=env,
            timeout_seconds=20,
            parser=parser,
            observation=observation,
        ) as streams:
            async with ClientSession(*streams) as session:
                with anyio.fail_after(10):
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    p.require(
                        len(tools) == 1 and tools[0].name == "run_privileged_command",
                        "wheel tool discovery mismatch",
                    )
                    p.require(
                        tools[0].inputSchema
                        == {
                            "type": "object",
                            "properties": {
                                "phase": {
                                    "type": "string",
                                    "pattern": "^[A-Za-z0-9_-]+$",
                                }
                            },
                            "additionalProperties": False,
                        },
                        "wheel tool schema mismatch",
                    )
                if request["wait_seconds"]:
                    await anyio.sleep(request["wait_seconds"])
                state["exited_before_client_eof"] = observation.result is not None

    anyio.run(run)
    p.require(parser.confirmed, "wheel service attestation incomplete")
    retained = p.retain_evidence(request["evidence"], manifest, parser, request["argv"])
    p.write(
        request["report"],
        p.canonical(
            {
                "schema_version": 1,
                "foundation": str(footman.process.__file__),
                "transport": str(Path(transport.__file__)),
                "attribution_timing": "post-import",
                "context": "wheel",
                "return_code": observation.result.returncode,
                "eof_delivered": observation.eof_delivered,
                "source_count": retained["source_count"],
                "set_sha256": retained["set_sha256"],
                "wheel_sha256": manifest["wheel_sha256"],
                **state,
            }
        ),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        for line in str(error).splitlines():
            print("wheel-client: " + line, file=sys.stderr)
        raise SystemExit(1) from error
