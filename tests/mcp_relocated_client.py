"""Fresh-wheel SDK client; owns the service and its provenance evidence exclusively."""

import argparse
import importlib.util
import math
import os
import signal
import stat
import sys
import time
from pathlib import Path

REQUEST_FIELDS = {
    "schema_version",
    "scratch_root",
    "support_root",
    "expected_origin_manifest",
    "provenance_evidence_path",
    "config_path",
    "installed_server_script",
    "docker_wrapper_directory",
    "neutral_service_cwd",
    "lifecycle_input",
    "journal_path",
    "lifecycle_binding",
    "calls",
    "transport_deadline_monotonic",
}


def load_support(root):
    root = Path(root)
    if not root.is_absolute() or root != root.resolve() or not sys.flags.safe_path:
        raise ValueError("invalid recorded support root or missing -P")
    spec = importlib.util.spec_from_file_location(
        "tests",
        root / "tests/__init__.py",
        submodule_search_locations=[str(root / "tests")],
    )
    if spec is None or spec.loader is None:
        raise ValueError("missing copied support")
    package = importlib.util.module_from_spec(spec)
    sys.modules["tests"] = package
    spec.loader.exec_module(package)
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import mcp_provenance as p

    with DirectoryDescriptor.open_secure_path(p.OPS, root) as directory:
        p.require(
            set(directory.list_names()) == {"tests"},
            "copy-root shadow or undeclared entry",
        )
    with DirectoryDescriptor.open_secure_path(p.OPS, root / "tests") as directory:
        declared = {
            Path(name).name
            for name in (
                *p.SUPPORT_FILES,
                "tests/mcp_relocated_client.py",
                "tests/relocated_docker_child.py",
                "tests/docker_cli_stub.py",
                "tests/dispatcher_harness.py",
            )
        }
        p.require(
            set(directory.list_names()) - {"__pycache__"} == declared,
            "undeclared copied support",
        )
    p.require(
        Path(package.__file__) == root / "tests/__init__.py", "wrong tests origin"
    )
    return p


def validate_request(p, path, support_root):
    request = p.load(path)
    p.require(
        isinstance(request, dict) and set(request) == REQUEST_FIELDS,
        "invalid request fields",
    )
    p.require(
        type(request["schema_version"]) is int and request["schema_version"] == 1,
        "invalid request version",
    )
    for key in REQUEST_FIELDS - {
        "schema_version",
        "lifecycle_binding",
        "calls",
        "transport_deadline_monotonic",
    }:
        p.require(
            isinstance(request[key], str) and Path(request[key]).is_absolute(),
            "request paths must be absolute",
        )
    scratch = Path(request["scratch_root"])
    p.require(
        scratch == scratch.resolve() and ".." not in scratch.parts,
        "noncanonical scratch",
    )
    p.require(request["support_root"] == str(support_root), "wrong support binding")
    scratch_paths = [
        "expected_origin_manifest",
        "provenance_evidence_path",
        "config_path",
        "docker_wrapper_directory",
        "neutral_service_cwd",
        "lifecycle_input",
        "journal_path",
    ]
    from footman.filesystem.descriptors import DirectoryDescriptor

    for key in scratch_paths:
        child = Path(request[key])
        p.require(
            child.is_relative_to(scratch)
            and child != scratch
            and child == child.resolve()
            and ".." not in child.parts,
            "path outside recorded scratch",
        )
        with DirectoryDescriptor.open_secure_path(p.OPS, child.parent) as parent:
            if key in {"expected_origin_manifest", "config_path", "lifecycle_input"}:
                p.require(
                    stat.S_ISREG(parent.stat_child(child.name).st_mode),
                    "request input must be a regular file",
                )
            elif key in {"docker_wrapper_directory", "neutral_service_cwd"}:
                p.require(
                    stat.S_ISDIR(parent.stat_child(child.name).st_mode),
                    "request directory must be a real directory",
                )
    paths = [request[key] for key in scratch_paths] + [str(path)]
    p.require(len(paths) == len(set(paths)), "request path alias")
    reserved = {scratch / "launch-intent.json", scratch / "helper-service-cleanup.json"}
    reserved.update(
        scratch / ("stage-" + stage)
        for stage in (
            "before-launch",
            "after-acquire",
            "after-journal",
            "idle",
            "accepted-work",
            "before-evidence",
            "after-evidence",
        )
    )
    reserved.update(
        scratch / ("bootstrap-gate-" + stage + ".json")
        for stage in ("before-journal", "before-provenance", "after-provenance")
    )
    p.require(
        not any(Path(item) in reserved for item in paths),
        "request aliases internal accounting",
    )
    p.require(
        not os.path.lexists(request["provenance_evidence_path"]),
        "evidence destination exists",
    )
    deadline = request["transport_deadline_monotonic"]
    p.require(
        type(deadline) in {int, float}
        and math.isfinite(deadline)
        and 0 < deadline - time.monotonic() <= 65,
        "invalid transport deadline",
    )
    calls = request["calls"]
    p.require(isinstance(calls, list) and len(calls) <= 2, "invalid calls")
    for call in calls:
        p.require(
            isinstance(call, dict)
            and set(call) <= {"phase"}
            and (not call or isinstance(call["phase"], str)),
            "invalid tool call",
        )
    from tests import mcp_lifecycle

    binding = mcp_lifecycle.validate_input(
        request["lifecycle_input"],
        forbidden=[item for item in paths if item != request["journal_path"]],
        binding=request["lifecycle_binding"],
    )
    p.require(
        binding["scratch_root"] == str(scratch)
        and binding["journal_path"] == request["journal_path"]
        and deadline == binding["service_deadline_monotonic"] + 5
        and binding["settlement_allowance_seconds"] == 5,
        "unbound lifecycle envelope",
    )
    return request


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--support-root", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    root = Path(args.support_root)
    p = load_support(root)
    request = validate_request(p, Path(args.request), root)
    import anyio
    from mcp import ClientSession

    import footman.process
    from tests import mcp_transport as transport

    manifest = p.load(request["expected_origin_manifest"])
    p.require(
        manifest["context"] == "wheel", "relocated helper requires wheel expectations"
    )
    # The transport import already imports the foundation: attribution is POST-import.
    import hashlib

    _, entries = p.manifest_set(manifest)
    for item in manifest["modules"]:
        loaded = sys.modules.get(item["name"])
        if loaded is not None:
            p.require(
                getattr(loaded, "__file__", None) == item["expected_path"]
                and hashlib.sha256(p.read(item["expected_path"])).hexdigest()
                == item["sha256"],
                "post-import helper origin/digest mismatch",
            )
    p.require(
        Path(footman.process.__file__).is_relative_to(Path(sys.prefix)),
        "checkout foundation in helper",
    )
    p.require(
        Path(transport.__file__) == root / "tests/mcp_transport.py",
        "wrong transport copy",
    )
    p.require(
        Path(request["installed_server_script"]) == Path(sys.prefix) / "bin/footman",
        "wrong installed script",
    )
    filtered_prefixes = ("PYTHON", "PYTEST", "FOOTMAN_RELOCATED_")
    helper_filtered_keys = sorted(
        key for key in os.environ if key.startswith(filtered_prefixes)
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(filtered_prefixes)
    }
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = request["docker_wrapper_directory"] + os.pathsep + env.get("PATH", "")
    argv = [
        sys.executable,
        "-P",
        str(root / "tests/mcp_server_child.py"),
        "--expected-origin",
        request["expected_origin_manifest"],
        "--entrypoint",
        request["installed_server_script"],
        "--lifecycle",
        request["lifecycle_input"],
        "--",
        "--config",
        request["config_path"],
    ]
    scratch = Path(request["scratch_root"])
    fault = os.environ.get("FOOTMAN_RELOCATED_FAULT")
    hold = os.environ.get("FOOTMAN_RELOCATED_STAGE")

    async def checkpoint(stage):
        p.write(
            scratch / ("stage-" + stage),
            p.canonical({"stage": stage, "monotonic": time.monotonic()}),
        )
        if stage == hold:
            while time.monotonic() < request["transport_deadline_monotonic"]:
                await anyio.sleep(0.02)

    parser = p.AttestationParser(entries)
    observed = transport.TransportObservation()
    calls = []

    def interrupt(signum, frame):
        raise KeyboardInterrupt("relocated helper interrupted")

    signal.signal(signal.SIGINT, interrupt)
    if fault == "interruption":
        signal.signal(signal.SIGTERM, interrupt)

    async def run():
        await checkpoint("before-launch")
        p.write(
            scratch / "launch-intent.json",
            p.canonical(
                {
                    "attempted": True,
                    "helper_pid": os.getpid(),
                    "helper_incarnation": p.read(Path(f"/proc/{os.getpid()}/stat"))
                    .rsplit(b")", 1)[1]
                    .split()[19]
                    .decode(),
                    "instance_token": request["lifecycle_binding"]["instance_token"],
                    "deadline": request["transport_deadline_monotonic"],
                }
            ),
        )
        async with transport.stdio_transport(
            argv,
            cwd=request["neutral_service_cwd"],
            env=env,
            timeout_seconds=request["transport_deadline_monotonic"] - time.monotonic(),
            parser=parser,
            observation=observed,
        ) as streams:
            await checkpoint("after-acquire")
            async with ClientSession(*streams) as session:
                await session.initialize()
                await checkpoint("after-journal")
                await checkpoint("idle")
                for arguments in request["calls"]:

                    async def invoke(arguments=arguments):
                        result = await session.call_tool(
                            "run_privileged_command", arguments
                        )
                        calls.append(result.structuredContent)
                        p.require(
                            not result.isError, "relocated tool request unsuccessful"
                        )

                    async with anyio.create_task_group() as group:
                        group.start_soon(invoke)
                        if hold == "accepted-work":
                            from footman.filesystem.descriptors import (
                                DirectoryDescriptor,
                            )

                            records = Path(env["FOOTMAN_STUB_RECORDS"])
                            while True:
                                with DirectoryDescriptor.open_secure_path(
                                    p.OPS, records
                                ) as directory:
                                    seen = [
                                        p.load(records / name)
                                        for name in directory.list_names()
                                        if name.endswith("-call.json")
                                    ]
                                if any(
                                    item["argv"][:2] == ["exec", "-u"]
                                    and "mkdir" not in item["argv"]
                                    for item in seen
                                ):
                                    break
                                await anyio.sleep(0.01)
                            await checkpoint("accepted-work")
                if hold != "accepted-work":
                    await checkpoint("accepted-work")
        from footman.process import StreamOutcome

        # This existing checkpoint is a receipt of successful real calls AND
        # clean transport/provenance completion, before any synthetic mutation.
        # Observe final settlement, not an earlier immutable result snapshot.
        settled = None if observed.owner is None else observed.owner.cleanup_status
        p.require(
            parser.confirmed
            and observed.eof_delivered
            and set(observed.terminals) == set(footman.process.ProcessStream)
            and all(
                outcome is StreamOutcome.EOF for outcome in observed.terminals.values()
            )
            and observed.result is not None
            and observed.result.returncode == 0
            and not observed.result.timed_out
            and not observed.result.lifecycle.failures
            and settled is not None
            and settled.complete
            and not settled.failures,
            "pre-fault service baseline incomplete",
        )
        await checkpoint("before-evidence")
        if fault == "stderr-read-failure":
            parser.end(StreamOutcome.READ_FAILED)
        elif fault == "stdout-aborted":
            observed.terminals[footman.process.ProcessStream.STDOUT] = (
                StreamOutcome.ABORTED
            )
        elif fault == "duplicate-attestation":
            parser.feed(p.attestation(entries))
        elif fault == "partial-attestation":
            parser.feed(p.PREFIX)
            parser.end(StreamOutcome.EOF)
        p.require(
            parser.confirmed
            and observed.eof_delivered
            and set(observed.terminals) == set(footman.process.ProcessStream)
            and all(
                outcome is StreamOutcome.EOF for outcome in observed.terminals.values()
            ),
            "service attestation/EOF incomplete",
        )
        if fault == "helper-before-evidence":
            raise RuntimeError("injected helper failure before evidence")
        if fault == "evidence-unwritable":
            Path(request["provenance_evidence_path"]).mkdir()
        if fault in {"evidence-truncated", "evidence-inconsistent"}:
            original_write = p.write

            def faulty_write(path, data):
                if str(path) == request["provenance_evidence_path"]:
                    data = (
                        data[: len(data) // 2]
                        if fault == "evidence-truncated"
                        else data.replace(b'"source_count":', b'"invalid_count":')
                    )
                return original_write(path, data)

            p.write = faulty_write
        retained = p.retain_evidence(
            request["provenance_evidence_path"], manifest, parser, argv
        )
        await checkpoint("after-evidence")
        if fault == "helper-after-evidence":
            raise RuntimeError("injected helper failure after evidence")
        return retained

    from unittest.mock import patch

    stream_calls = [0]
    original_stream = footman.process.OwnedProcess.stream

    def counted_stream(owner, *args, **kwargs):
        stream_calls[0] += 1
        return original_stream(owner, *args, **kwargs)

    try:
        with patch.object(footman.process.OwnedProcess, "stream", counted_stream):
            retained = anyio.run(run)
    finally:
        from footman.filesystem.cleanup import CleanupFailures

        primary = sys.exception()
        failures = CleanupFailures(primary)
        if observed.owner is not None:
            status = observed.owner.cleanup_status
            failures.run(
                lambda: p.write(
                    scratch / "helper-service-cleanup.json",
                    p.canonical(
                        {
                            "complete": status.complete,
                            "failures": [
                                str(failure.error) for failure in status.failures
                            ],
                            "service_exit_code": None
                            if observed.result is None
                            else observed.result.returncode,
                        }
                    ),
                ),
                ordinary=(Exception,),
            )
        selected = failures.complete()
        if selected is not None and selected is not primary:
            raise selected
    result = {
        "schema_version": 1,
        "client_attribution_timing": "post-import",
        "foundation": str(footman.process.__file__),
        "transport": str(transport.__file__),
        "service_context": "wheel",
        "wheel_sha256": manifest["wheel_sha256"],
        "service_argv": argv,
        "helper_environment_filtered_keys": helper_filtered_keys,
        "service_environment": {
            "PATH": env["PATH"],
            "PYTHONNOUSERSITE": env.get("PYTHONNOUSERSITE"),
            "key_count": len(env),
            "prefix_counts": {
                prefix: sum(key.startswith(prefix) for key in env)
                for prefix in filtered_prefixes
            },
            "key_set_sha256": hashlib.sha256(p.canonical(sorted(env))).hexdigest(),
            "filtered_keys": sorted(
                key for key in env if key.startswith(filtered_prefixes)
            ),
        },
        "service_stream_calls": stream_calls[0],
        "service_stream_outcomes": {
            s.name: outcome.name for s, outcome in observed.terminals.items()
        },
        "service_exit_code": observed.result.returncode,
        "eof_delivered": observed.eof_delivered,
        "provenance_final": parser.confirmed,
        "evidence_readback": True,
        "evidence_path": request["provenance_evidence_path"],
        "source_count": retained["source_count"],
        "set_sha256": retained["set_sha256"],
        "lifecycle_input": request["lifecycle_input"],
        "config_path": request["config_path"],
        "docker_wrapper_directory": request["docker_wrapper_directory"],
        "calls": calls,
    }
    import json

    data = json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    p.require(
        len(data) <= 65536 and b"\nFOOTMAN_PROVENANCE_" not in data,
        "helper result exceeds bound or reserved namespace",
    )
    if fault == "missing-result":
        return
    if fault == "partial-result":
        data = data[: len(data) // 2]
    elif fault == "oversized-result":
        data = b"x" * 65537
    elif fault == "duplicate-result":
        data += data
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        for line in str(error).splitlines():
            print("relocated-client: " + line[:512], file=sys.stderr)
        raise SystemExit(1) from error
