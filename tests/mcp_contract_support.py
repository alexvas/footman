"""Assertion-bearing Phase-9 fixture drivers, never imported by copied service support."""

import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from mcp import ClientSession

from footman.filesystem.descriptors import DirectoryDescriptor
from tests import mcp_provenance as p
from tests.dispatcher_harness import GitFixture
from tests.mcp_transport import TransportObservation, stdio_transport
from tests.mcp_wheel_support import (
    build_and_install_contract_wheel,
    clean_environment,
    command,
)

WHEEL_PROVIDER = None  # pytest supplies a lazy per-worker session fixture.


def check(value, message):
    if not value:
        raise AssertionError(message)


def copy_tree(source, destination):
    destination.mkdir(mode=0o700)
    with DirectoryDescriptor.open_secure_path(p.OPS, source) as directory:
        for name in directory.list_names():
            if name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
                continue
            child = source / name
            if child.is_symlink():
                os.symlink(os.readlink(child), destination / name)
            elif child.is_dir():
                copy_tree(child, destination / name)
            else:
                p.write(destination / name, p.read(child))
                os.chmod(destination / name, child.stat().st_mode & 0o777)
    os.chmod(destination, source.stat().st_mode & 0o777, follow_symlinks=False)


def prepare_inputs(root, tmp):
    tmp.mkdir(mode=0o700)
    copy_tree(root / "src", tmp / "src")
    for name in ("pyproject.toml", "README.md"):
        p.write(tmp / name, p.read(root / name))
        os.chmod(tmp / name, (root / name).stat().st_mode & 0o777)
    from tests.mcp_wheel_support import inventory

    check(
        inventory(root)[0] == inventory(tmp)[0],
        "prepared real build inputs differ from candidate",
    )
    return tmp


def configuration(tmp, *, require_phase=False):
    repo = GitFixture.create(tmp / "repository")
    fields = {
        "version": 1,
        "repository": str(repo.path),
        "container_start_argv": [
            "docker",
            "run",
            "--detach=true",
            "--pull=never",
            "--",
            "{image_id}",
            "sleep",
            "infinity",
        ],
        "image_id": "d9e853e87e55",
        "command_argv": ["image-tool", "two words", "; $literal"],
        "working_directory": ".",
        "container_snapshot_directory": "/workspace/source",
        "snapshot_directory": str(tmp / "snapshots"),
        "output_dir": str(
            tmp
            / "results"
            / ("{phase}/{utc_datetime}" if require_phase else "{utc_datetime}")
        ),
        "timeout_seconds": 10,
        "cleanup_grace_seconds": 2,
    }
    path = tmp / "operator.toml"
    p.write(
        path,
        (
            "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items())
            + "\n"
        ).encode(),
    )
    return repo, path


def world(root, tmp, context="development", **options):
    repo, config = configuration(tmp, require_phase=options.get("require_phase", False))
    if context == "wheel":
        import base64
        import hashlib

        from tests.mcp_attribution_support import dist_identity

        candidate_dist_before = dist_identity(root)
        install = tmp / "installation"
        install.mkdir(mode=0o700)
        if WHEEL_PROVIDER is None:
            prepared = prepare_inputs(root, tmp / "build-root")
            bound = build_and_install_contract_wheel(
                prepared, admission=options.get("admission"), evidence_directory=install
            )
        else:
            from tests.mcp_wheel_support import inventory, reuse_for_test

            base = WHEEL_PROVIDER()
            check(
                inventory(root)[0] == base.build_input_identity,
                "worker wheel inputs changed",
            )
            bound = reuse_for_test(
                base,
                install,
                admission=options.get("admission"),
                mutable=options.get("mutable_wheel", False),
                private_support=options.get("private_support", False),
            )
            prepared = base.repository_root
        candidate_dist_after = dist_identity(root)
        check(
            candidate_dist_after == candidate_dist_before,
            "disposable real build changed candidate dist",
        )
        check(
            hashlib.sha256(p.read(bound.wheel)).hexdigest() == bound.wheel_sha256,
            "independent installed-wheel binding mismatch",
        )

        def operation(value):
            return {
                **value,
                "stdout": base64.b64encode(value["stdout"]).decode(),
                "stderr": base64.b64encode(value["stderr"]).decode(),
            }

        record = {
            "schema_version": 1,
            "reused_worker_install": WHEEL_PROVIDER is not None,
            "mutable_clone": options.get("mutable_wheel", False),
            "build_root": str(prepared),
            "candidate_dist_pre": candidate_dist_before,
            "candidate_dist_post": candidate_dist_after,
            "predicted": [str(bound.sdist), str(bound.wheel)],
            "produced_dist": dist_identity(prepared),
            "build_input_identity": bound.build_input_identity,
            "inventory": bound.inventory,
            "wheel_path": str(bound.wheel),
            "wheel_sha256": bound.wheel_sha256,
            "sdist_path": str(bound.sdist),
            "sdist_sha256": bound.sdist_sha256,
            "build_operation": operation(bound.command),
            "setup_operations": [operation(op) for op in bound.setup_operations],
            "support_inventory": bound.support_inventory,
            "dependency_identity": bound.dependency_identity,
        }
        p.write(install / "build-record.json", p.canonical(record))
        manifest = bound.expected_origin
        python = bound.environment / "bin/python"
        bootstrap = bound.support_root / "tests/mcp_server_child.py"
        selected = ["--entrypoint", str(bound.environment / "bin/footman")]
    else:
        bound = None
        manifest = p.development_manifest(root / "src/footman")
        python = Path(sys.executable).absolute()
        bootstrap = root / "tests/mcp_server_child.py"
        selected = ["--module", "footman"]
    origin = tmp / "expected-origin.json"
    p.write(origin, p.canonical(manifest))
    neutral = tmp / "neutral"
    neutral.mkdir(mode=0o700)
    env = clean_environment()
    argv = [
        str(python),
        "-P",
        str(bootstrap),
        "--expected-origin",
        str(origin),
        *selected,
        "--",
        "--config",
        str(config),
    ]
    return {
        "repo": repo,
        "config": config,
        "manifest": manifest,
        "origin": origin,
        "python": python,
        "bootstrap": bootstrap,
        "argv": argv,
        "cwd": neutral,
        "env": env,
        "bound": bound,
        "tmp": tmp,
    }


@asynccontextmanager
async def client(w, *, timeout=20):
    _, entries = p.manifest_set(w["manifest"])
    parser = p.AttestationParser(entries)
    observation = TransportObservation()
    async with stdio_transport(
        w["argv"],
        cwd=w["cwd"],
        env=w["env"],
        timeout_seconds=timeout,
        parser=parser,
        observation=observation,
    ) as streams:
        async with ClientSession(*streams) as session:
            with anyio.fail_after(min(10, timeout)):
                await session.initialize()
            yield session, observation
    check(
        parser.confirmed,
        f"service attestation failed: {parser.failure}; {observation.stderr_tail!r}",
    )
    p.retain_evidence(w["tmp"] / "verified-set.json", w["manifest"], parser, w["argv"])
    check(
        observation.eof_delivered, "explicit EOF not delivered before child settlement"
    )
    check(
        observation.owner.cleanup_status.complete, "sole process owner did not settle"
    )


def validate_support(w):
    bound = w.get("bound")
    if bound is None:
        return
    import hashlib

    root = Path(__file__).resolve().parents[1]
    for entry in bound.support_inventory:
        for path in (root / entry["path"], bound.support_root / entry["path"]):
            check(
                hashlib.sha256(p.read(path)).hexdigest() == entry["sha256"]
                and path.lstat().st_mode & 0o111 == entry["executable"],
                "support source/copy changed during fixture lifetime",
            )
    check(
        hashlib.sha256(p.read(root / "uv.lock")).hexdigest()
        == bound.dependency_identity,
        "dependency lock changed during fixture lifetime",
    )


def run_wheel_client(w, *, wait_seconds=0):
    validate_support(w)
    root = w["bound"].support_root
    request = w["tmp"] / "wheel-client-request.json"
    report = w["tmp"] / "wheel-client-report.json"
    evidence = w["tmp"] / "wheel-client-evidence.json"
    p.write(
        request,
        p.canonical(
            {
                "argv": w["argv"],
                "origin": str(w["origin"]),
                "cwd": str(w["cwd"]),
                "evidence": str(evidence),
                "report": str(report),
                "wait_seconds": wait_seconds,
                "lifecycle_binding": w.get("lifecycle_binding"),
            }
        ),
    )
    env = dict(w["env"], FOOTMAN_WHEEL_REQUEST=str(request))
    operation = command(
        [str(w["python"]), "-P", str(root / "tests/mcp_wheel_client.py")],
        cwd=w["cwd"],
        env=env,
        timeout_seconds=30,
    )
    check(operation["status"] == 0, f"fresh-wheel SDK client failed: {operation}")
    value = p.load(report)
    _, entries = p.manifest_set(w["manifest"])
    retained = p.load(evidence)
    check(
        value["return_code"] == 0
        and value["wheel_sha256"] == w["bound"].wheel_sha256
        and value["attribution_timing"] == "post-import",
        "wheel client origin/status binding mismatch",
    )
    check(
        retained["sources"] == entries
        and retained["source_count"] == value["source_count"]
        and retained["set_sha256"] == value["set_sha256"],
        "wheel helper failed complete-set evidence linkage",
    )
    check(
        Path(value["foundation"]).is_relative_to(w["bound"].environment),
        "wheel client loaded development foundation",
    )
    check(
        value["transport"] == str(root / "tests/mcp_transport.py"),
        "wheel client did not use copied transport",
    )
    validate_support(w)
    return value


def exercise_discovery(root, tmp, context, *, admission):
    w = world(root, tmp, context, admission=admission)
    if context == "wheel":
        run_wheel_client(w)
        check(
            not (tmp / "results").exists() and not (tmp / "snapshots").exists(),
            "wheel discovery acquired run resources",
        )
        return

    async def scenario():
        async with client(w) as (session, observation):
            tools = (await session.list_tools()).tools
            check(
                len(tools) == 1 and tools[0].name == "run_privileged_command",
                "wrong MCP capability",
            )
            check(
                tools[0].inputSchema
                == {
                    "type": "object",
                    "properties": {
                        "phase": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"}
                    },
                    "additionalProperties": False,
                },
                "wrong phase-only input schema",
            )
            check(observation.owner is not None, "missing sole child owner")

    anyio.run(scenario)
    check(
        not (tmp / "results").exists() and not (tmp / "snapshots").exists(),
        "discovery acquired run resources",
    )


def exercise_peer(root, tmp, scenario):
    observation = TransportObservation()

    async def run():
        async with stdio_transport(
            [sys.executable, "-P", str(root / "tests/mcp_protocol_child.py"), scenario],
            cwd=tmp,
            env=clean_environment(),
            timeout_seconds=3,
            observation=observation,
        ) as streams:
            async with ClientSession(*streams) as session:
                with anyio.fail_after(0.3 if scenario in {"blocked", "cancel"} else 2):
                    await session.initialize()
                    tools = await session.list_tools()
                    check(tools.tools[0].name == "echo", "fixture discovery failed")
                    value = await session.call_tool("echo", {})
                    check(value.content[0].text == "привет", "UTF-8 framing corrupted")
                    if scenario == "cancel":
                        await anyio.sleep(2)

    expected_failure = scenario in {"malformed", "partial", "eof", "blocked", "cancel"}
    try:
        anyio.run(run)
    except Exception:
        check(expected_failure, "ordinary peer communication unexpectedly failed")
    else:
        check(not expected_failure, "invalid/blocked peer was accepted")
    check(observation.owner.cleanup_status.complete, "peer owner incomplete")
    check(
        b"FOOTMAN_PROVENANCE_" not in observation.stderr_tail,
        "ordinary peer impersonated provenance",
    )
    if scenario == "stderr":
        check(b"peer diagnostic" in observation.stderr_tail, "stderr lost")


def exercise_invalid_request(root, tmp, arguments):
    w = world(root, tmp, require_phase=True)

    async def scenario():
        async with client(w) as (session, _):
            result = await session.call_tool("run_privileged_command", arguments)
            check(result.isError, "invalid tool arguments accepted")

    anyio.run(scenario)
    check(
        not (tmp / "results").exists() and not (tmp / "snapshots").exists(),
        "invalid arguments acquired resources",
    )


def install_docker(w, *, failure="command", status=0, delay=0):
    directory = w["tmp"] / "docker-tools"
    directory.mkdir(mode=0o700)
    record = w["tmp"] / "docker-records"
    record.mkdir(mode=0o700)
    root = Path(__file__).resolve().parents[1]
    script = f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(root)!r})\nfrom tests.mcp_docker_child import main\nmain()\n"
    p.write(directory / "docker", script.encode())
    os.chmod(directory / "docker", 0o700)
    w["env"].update(
        PATH=str(directory) + os.pathsep + w["env"].get("PATH", ""),
        FOOTMAN_MCP_STUB=json.dumps(
            {
                "records": str(record),
                "failure": failure,
                "status": status,
                "delay": delay,
            }
        ),
    )
    return record


def records(directory):
    return [p.load(path) for path in sorted(directory.iterdir())]


def response_check(result, *, status, succeeded):
    check(
        result.isError is not succeeded,
        f"MCP aggregate error mismatch: {result.structuredContent!r}",
    )
    value = result.structuredContent
    check(
        value is not None and value["return_code"] == status,
        "command status provenance mismatch",
    )
    check((value["outcome"] == "succeeded") is succeeded, "aggregate outcome mismatch")
    for key in (
        "stdoutPath",
        "stderrPath",
        "startupStdoutPath",
        "startupStderrPath",
        "resultPath",
    ):
        check(
            key in value
            and Path(value[key]).is_absolute()
            and Path(value[key]).is_file(),
            "retained path absent or relative",
        )
    check(
        set(k for k in value if k.endswith("Path"))
        == {
            "stdoutPath",
            "stderrPath",
            "startupStdoutPath",
            "startupStderrPath",
            "resultPath",
        },
        "extra helper-output paths",
    )
    return value


def exercise_response(root, tmp, failure, status):
    w = world(root, tmp)
    before = w["repo"].identity()
    directory = install_docker(w, failure=failure, status=status)

    async def scenario():
        async with client(w) as (session, _):
            result = await session.call_tool("run_privileged_command", {})
            value = response_check(
                result,
                status=status if failure == "command" else None,
                succeeded=failure == "command" and status == 0,
            )
            manifest = p.load(value["resultPath"])
            check(
                manifest["return_code"] == value["return_code"],
                "manifest/MCP status mismatch",
            )
            if failure == "command":
                check(
                    p.read(value["stdoutPath"]) == b"command stdout\x00\xff\n"
                    and p.read(value["stderrPath"]) == b"command stderr\x00\xfe\n",
                    "raw command streams changed/mixed",
                )

    anyio.run(scenario)
    calls = records(directory)
    check(
        sum(item["operation"] == "launch" for item in calls) == 1,
        "accepted request retried",
    )
    if failure != "command":
        check(
            not any(item["operation"] == "command" for item in calls),
            "command started after infrastructure failure",
        )
    w["repo"].assert_unchanged(before)


def exercise_production_rejection(root, tmp, option):
    _, config = configuration(tmp)
    result = command(
        [sys.executable, "-P", "-m", "footman", "--config", str(config), option],
        cwd=tmp,
        env=clean_environment(),
        timeout_seconds=10,
    )
    check(
        result["status"] != 0 and not result["stdout"] and result["stderr"],
        "unknown production option reached service",
    )
    check(not (tmp / "results").exists(), "unknown option dispatched")


def exercise_publication(root, tmp, stage):
    w = world(root, tmp, require_phase=True)
    directory = install_docker(w)
    w["env"]["FOOTMAN_BOOTSTRAP_TEST_FAULT"] = stage

    async def scenario():
        async with client(w) as (session, _):
            first = await session.call_tool(
                "run_privileged_command", {"phase": "first"}
            )
            value = response_check(
                first,
                status=0 if stage == "final-post-rename" else None,
                succeeded=False,
            )
            check(
                value["outcome"] == "output_failed",
                "publication failure masked by command success",
            )
            diagnostic = value["diagnostics"]
            check(
                not diagnostic["manifest_finalized"]
                and diagnostic["publication_failure"] is not None,
                "publication confirmation fabricated",
            )
            check(
                "injected"
                in __import__("base64")
                .b64decode(diagnostic["publication_failure"]["message_base64"])
                .decode(),
                "original publication diagnostic lost",
            )
            if stage == "final-post-rename":
                check(
                    p.load(value["resultPath"])["outcome"] == "succeeded",
                    "post-rename manifest corrected or rolled back",
                )
            second = await session.call_tool(
                "run_privileged_command", {"phase": "second"}
            )
            response_check(second, status=0, succeeded=True)
            check(
                second.structuredContent["resultPath"] != value["resultPath"],
                "independent request reused failed output",
            )
            check(
                Path(value["stdoutPath"]).parent.parent.name == "first"
                and Path(second.structuredContent["stdoutPath"]).parent.parent.name
                == "second",
                "fast requests did not use disjoint phase namespaces",
            )

    anyio.run(scenario)
    expected = 2 if stage == "final-post-rename" else 1
    check(
        sum(r["operation"] == "launch" for r in records(directory)) == expected,
        "failed run retried",
    )


def exercise_configuration_lifetime(root, tmp):
    w = world(root, tmp)
    directory = install_docker(w)

    async def scenario():
        async with client(w) as (session, _):
            first = await session.call_tool("run_privileged_command", {})
            response_check(first, status=0, succeeded=True)
            # Operator edit is fixture-owned; no running process reloads it.
            w["config"].unlink()
            p.write(w["config"], b"version = 999\n")
            second = await session.call_tool("run_privileged_command", {})
            response_check(second, status=0, succeeded=True)
            check(
                p.load(first.structuredContent["resultPath"])["configuration_identity"]
                == p.load(second.structuredContent["resultPath"])[
                    "configuration_identity"
                ],
                "effective config identity changed without restart",
            )

    anyio.run(scenario)
    result = command(w["argv"], cwd=w["cwd"], env=w["env"], timeout_seconds=10)
    check(
        result["status"] != 0
        and not result["stdout"]
        and b"version" in result["stderr"],
        "restart ignored invalid new config",
    )
    check(
        sum(r["operation"] == "launch" for r in records(directory)) == 2,
        "configuration reload launched a run",
    )


def exercise_borrowed_stdio(factory):
    from footman.server import BorrowedText

    class Borrowed:
        def __init__(self, data=b""):
            self.inner = factory(data)

        def readline(self):
            return self.inner.readline()

        def write(self, data):
            return self.inner.write(data)

        def flush(self):
            return self.inner.flush()

        def getvalue(self):
            return self.inner.getvalue()

        def close(self):
            raise AssertionError("inherited stream was closed")

    async def scenario():
        reader = Borrowed("привет\n".encode())
        writer = Borrowed()
        facade = BorrowedText(reader)
        check(await anext(facade) == "привет\n", "locale-dependent inherited framing")
        output = BorrowedText(writer)
        await output.write("ответ\n")
        await output.flush()
        await output.aclose()
        await output.aclose()
        check(
            writer.getvalue() == "ответ\n".encode(), "UTF-8 inherited output corrupted"
        )
        error = OSError("borrowed read")

        class Broken(Borrowed):
            def readline(self):
                raise error

        try:
            await anext(BorrowedText(Broken()))
        except OSError as caught:
            check(caught is error, "borrowed read failure identity changed")
        else:
            raise AssertionError("read failure swallowed")

    anyio.run(scenario)


def exercise_diagnostics(root, tmp, scenario):
    w = world(root, tmp)
    if scenario in {"startup", "configuration"}:
        w["config"].unlink()
        p.write(w["config"], b'version = 999\n"FOOTMAN_PROVENANCE_FORGED" = true\n')
        result = command(w["argv"], cwd=w["cwd"], env=w["env"], timeout_seconds=10)
        check(
            result["status"] != 0 and not result["stdout"],
            "invalid config served protocol",
        )
        lines = result["stderr"].splitlines()
        check(
            sum(line.startswith(p.PREFIX) for line in lines) == 1,
            "startup diagnostic collided with bootstrap namespace",
        )
        return
    if scenario == "direct":
        w["config"].unlink()
        p.write(w["config"], b"version = 999\n")
        result = command(
            [sys.executable, "-P", "-m", "footman", "--config", str(w["config"])],
            cwd=w["cwd"],
            env=w["env"],
            timeout_seconds=10,
        )
        check(
            result["status"] != 0
            and not result["stdout"]
            and not any(
                line.startswith(p.PREFIX) for line in result["stderr"].splitlines()
            ),
            "direct startup emitted reserved record",
        )
        return
    # Real generated logging is injected only through the verified test bootstrap.
    w["env"]["FOOTMAN_BOOTSTRAP_TEST_DIAGNOSTIC"] = scenario
    install_docker(
        w,
        failure="launch" if scenario == "helper-failure" else "command",
        status=9 if scenario == "helper-failure" else 0,
    )

    async def run():
        async with client(w) as (session, observation):
            result = await session.call_tool("run_privileged_command", {})
            check(
                result.isError is (scenario == "helper-failure"),
                "diagnostic scenario changed command result",
            )
        lines = bytes(observation.stderr_tail).splitlines()
        check(
            sum(line.startswith(p.PREFIX) for line in lines) == 1,
            "ordinary diagnostic impersonated bootstrap",
        )
        if scenario in {"logging", "multiline"}:
            check(
                any(
                    b"FOOTMAN_PROVENANCE_FORGED" in line
                    and not line.startswith(p.PREFIX)
                    for line in lines
                ),
                "generated multiline diagnostic control did not execute",
            )

    anyio.run(run)


def exercise_supervision(root, tmp, scenario):
    if scenario == "partial-frame":
        return exercise_transport_fault(tmp, "partial-frame")
    if scenario == "eof-timeout":
        return exercise_transport_fault(tmp, "eof-pending")
    if scenario == "envelope-expired":
        # A real child exceeding the original envelope cannot report success.
        from tests.mcp_transport import TransportObservation, stdio_transport

        observed = TransportObservation()

        async def expired():
            async with stdio_transport(
                [
                    sys.executable,
                    "-P",
                    str(root / "tests/mcp_protocol_child.py"),
                    "blocked",
                ],
                cwd=tmp,
                env=clean_environment(),
                timeout_seconds=0.2,
                observation=observed,
            ):
                await anyio.sleep(0.3)

        try:
            anyio.run(expired)
        except Exception:
            check(
                observed.owner.cleanup_status.complete,
                "expired transport lost child owner",
            )
        else:
            raise AssertionError("exhausted envelope reported success")
        return
    w = world(root, tmp, require_phase=scenario == "concurrent")
    directory = install_docker(w, delay=1.25)
    before = w["repo"].identity()

    async def run():
        async with client(w) as (session, observation):
            async with anyio.create_task_group() as group:

                async def invoke(phase):
                    arguments = {} if phase is None else {"phase": phase}
                    result = await session.call_tool(
                        "run_privileged_command", arguments
                    )
                    check(
                        not result.isError,
                        f"accepted request failed: {result.structuredContent!r}",
                    )
                    response_check(result, status=0, succeeded=True)

                # Millisecond timestamps alone are not unique output namespaces.
                group.start_soon(invoke, "first" if scenario == "concurrent" else None)
                if scenario == "concurrent":
                    group.start_soon(invoke, "second")
                else:
                    with anyio.fail_after(8):
                        while not any(
                            r["operation"] == "command" for r in records(directory)
                        ):
                            await anyio.sleep(0.02)
                    group.cancel_scope.cancel()
        check(
            observation.result.returncode == 0 and observation.eof_delivered,
            "disconnect did not deliver EOF then normal exit",
        )

    anyio.run(run)
    manifests = [p.load(path) for path in (tmp / "results").rglob("result.json")]
    count = 2 if scenario == "concurrent" else 1
    check(
        len(manifests) == count and all(m["outcome"] == "succeeded" for m in manifests),
        "accepted work abandoned or mixed",
    )
    if scenario == "concurrent":
        check(
            {manifest["phase"] for manifest in manifests} == {"first", "second"},
            "concurrent request phases mixed",
        )
    check(
        sum(r["operation"] == "launch" for r in records(directory)) == count,
        "accepted work retried",
    )
    w["repo"].assert_unchanged(before)


def rewrite(path, data):
    # Explicit destructive fixture mutation, only on this driver's recorded copy.
    path.unlink()
    p.write(path, data)


def reject_bootstrap(w):
    result = command(w["argv"], cwd=w["cwd"], env=w["env"], timeout_seconds=10)
    check(
        result["status"] != 0 and not result["stdout"] and result["stderr"],
        "bootstrap fault did not fail before service",
    )
    check(
        not any(line.startswith(p.PREFIX) for line in result["stderr"].splitlines()),
        "failed verification emitted attestation",
    )
    check(not (w["tmp"] / "results").exists(), "bootstrap fault dispatched a run")
    return result


def exercise_package_control(root, tmp, fault, *, admission):
    w = world(root, tmp, "wheel", admission=admission, mutable_wheel=True)
    package, _ = p.manifest_set(w["manifest"])
    if fault in {"container.py", "supervision.py", "__main__.py"}:
        path = package / fault
        rewrite(
            path,
            p.read(path) + b"\nraise RuntimeError('unverified sentinel executed')\n",
        )
    elif fault == "missing":
        (package / "container.py").unlink()
    elif fault == "extra":
        p.write(
            package / "unexpected.py",
            b"raise RuntimeError('unexpected source executed')\n",
        )
    elif fault == "symlink":
        (package / "container.py").unlink()
        os.symlink("supervision.py", package / "container.py")
    else:
        for item in w["manifest"]["modules"]:
            item["expected_path"] = str(
                tmp / "wrong-package" / Path(item["expected_path"]).relative_to(package)
            )
        rewrite(w["origin"], p.canonical(w["manifest"]))
    result = reject_bootstrap(w)
    check(
        b"sentinel executed" not in result["stderr"], "unverified source was executed"
    )


def exercise_origin_control(root, tmp, fault, *, admission):
    if fault == "wheel-binding":
        from unittest.mock import patch

        from tests import mcp_wheel_support as wheel

        build = prepare_inputs(root, tmp / "build")
        evidence = tmp / "install"
        evidence.mkdir()
        real = wheel.command
        calls = []

        def swap(argv, **kwargs):
            calls.append(list(argv))
            if list(argv[:2]) == ["uv", "venv"]:
                artifact = build / "dist/footman-0.1.0-py3-none-any.whl"
                rewrite(artifact, p.read(artifact) + b"swapped bytes")
            return real(argv, **kwargs)

        with patch.object(wheel, "command", swap):
            try:
                wheel.build_and_install_contract_wheel(
                    build, admission=admission, evidence_directory=evidence
                )
            except ValueError as error:
                check(
                    "bound wheel changed" in str(error), "wrong wheel-binding rejection"
                )
            else:
                raise AssertionError("swapped wheel installed despite original binding")
        check(
            not any(call[:3] == ["uv", "pip", "install"] for call in calls),
            "swapped wheel reached installation",
        )
        return
    w = world(
        root,
        tmp,
        "wheel",
        admission=admission,
        mutable_wheel=fault in {"stale", "swapped-install", "launcher"},
        private_support=fault == "script-shadow",
    )
    package, _ = p.manifest_set(w["manifest"])
    if fault == "missing":
        w["manifest"]["modules"] = [
            m for m in w["manifest"]["modules"] if m["name"] != "footman.__main__"
        ]
        rewrite(w["origin"], p.canonical(w["manifest"]))
    elif fault in {"stale", "swapped-install"}:
        path = package / "startup.py"
        rewrite(path, p.read(path) + b"\n# self-consistent stale installation\n")
        if fault == "swapped-install":
            # Installer metadata is deliberately self-consistent but never authority.
            record = next(package.parent.glob("footman-*.dist-info/RECORD"))
            rewrite(record, p.read(record) + b"# self-consistent replacement\n")
    elif fault == "launcher":
        script = Path(w["manifest"]["entrypoint"]["script_path"])
        rewrite(
            script,
            p.read(script) + b"\nraise RuntimeError('unexpected launcher behavior')\n",
        )
        import hashlib

        w["manifest"]["entrypoint"]["observed_script_sha256"] = hashlib.sha256(
            p.read(script)
        ).hexdigest()
        rewrite(w["origin"], p.canonical(w["manifest"]))
    else:
        shadow = w["bootstrap"].parent if fault == "script-shadow" else w["cwd"]
        if fault == "poisoned-environment":
            # A poisoned parent environment is deliberately NOT copied to child.
            from unittest.mock import patch

            with patch.dict(
                os.environ,
                {"PYTHONPATH": str(tmp / "poison"), "PYTHONHOME": str(tmp / "poison")},
            ):
                w["env"] = clean_environment()
            check(
                "PYTHONPATH" not in w["env"] and "PYTHONHOME" not in w["env"],
                "import redirection survived environment construction",
            )
        (shadow / "footman").mkdir()
        p.write(
            shadow / "footman/__init__.py",
            b"raise RuntimeError('shadow sentinel executed')\n",
        )
        p.write(
            shadow / "logging.py",
            b"raise RuntimeError('stdlib shadow sentinel executed')\n",
        )

        run_wheel_client(w)
        return
    reject_bootstrap(w)


def lifecycle_input(tmp, *, duration=10):
    return {
        "schema_version": 1,
        "scratch_root": str(tmp),
        "journal_path": str(tmp / "lifecycle-journal.jsonl"),
        "instance_token": "a" * 32,
        "service_deadline_monotonic": time.monotonic() + duration,
        "settlement_allowance_seconds": 2,
    }


def exercise_lifecycle_validation(tmp, fault):
    from unittest.mock import Mock

    from tests import mcp_lifecycle as lifecycle

    value = lifecycle_input(tmp)
    binding = dict(value)
    path = tmp / "lifecycle.json"
    if fault == "unknown":
        value["unknown"] = 1
    elif fault == "missing":
        del value["instance_token"]
    elif fault == "bool":
        value["schema_version"] = True
    elif fault == "nonfinite":
        value["settlement_allowance_seconds"] = float("inf")
    elif fault == "expired":
        value["service_deadline_monotonic"] = time.monotonic() - 1
    elif fault == "extended":
        value["service_deadline_monotonic"] += 100
    elif fault == "token":
        value["instance_token"] = "b" * 32
    elif fault == "escape":
        value["journal_path"] = str(tmp.parent / "escaped-journal")
    elif fault == "symlink":
        os.symlink(tmp, tmp / "link")
        value["journal_path"] = str(tmp / "link/journal")
    elif fault == "alias":
        value["journal_path"] = str(path)
    data = p.canonical(value)
    if fault == "duplicate-key":
        data = data[:-1] + b',"schema_version":1}'
    p.write(path, data)
    launch = Mock()
    try:
        lifecycle.validate_input(path, binding=binding)
        launch()
    except ValueError, OSError, __import__(
        "footman.filesystem.descriptors", fromlist=["DescriptorError"]
    ).DescriptorError:
        pass
    else:
        raise AssertionError("invalid lifecycle input accepted")
    launch.assert_not_called()
    check(
        not (tmp / "lifecycle-journal.jsonl").exists(),
        "prelaunch validation acquired journal",
    )


def exercise_wheel_lifecycle(root, tmp, case, *, admission):

    w = world(root, tmp, "wheel", admission=admission)
    input_path = tmp / "lifecycle.json"
    value = lifecycle_input(tmp, duration=3 if case == "lifetime" else 15)
    if case != "direct":
        w["lifecycle_binding"] = dict(value)
    p.write(input_path, p.canonical(value))
    if case != "direct":
        separator = w["argv"].index("--")
        w["argv"][separator:separator] = ["--lifecycle", str(input_path)]
    if case == "invalid-input":
        rewrite(input_path, p.canonical(value | {"schema_version": True}))
    elif case == "journal-fault":
        w["env"]["FOOTMAN_BOOTSTRAP_TEST_FAULT"] = "journal"
    elif case == "wrong-position":
        pos = w["argv"].index("--lifecycle")
        pair = w["argv"][pos : pos + 2]
        del w["argv"][pos : pos + 2]
        w["argv"][3:3] = pair
    elif case == "duplicate":
        pos = w["argv"].index("--")
        w["argv"][pos:pos] = ["--lifecycle", str(input_path)]
    elif case == "production-forward":
        pos = w["argv"].index("--lifecycle")
        del w["argv"][pos : pos + 2]
        w["argv"] += ["--lifecycle", str(input_path)]
    elif case in {"wrong-leg", "missing-option"}:
        if case == "missing-option":
            pos = w["argv"].index("--lifecycle")
            del w["argv"][pos : pos + 2]
        # Public-surface request validation rejects leg mismatch BEFORE acquisition.
        launch = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        try:
            validate_launch(
                w["argv"], nested=case == "missing-option", direct=case == "wrong-leg"
            )
            launch()
        except ValueError:
            pass
        else:
            raise AssertionError("wrong lifecycle launch leg accepted")
        launch.assert_not_called()
        return
    invalid = case in {
        "invalid-input",
        "journal-fault",
        "wrong-position",
        "duplicate",
        "production-forward",
    }
    if invalid:
        reject_bootstrap(w)
        if case != "journal-fault":
            check(
                not Path(value["journal_path"]).exists(),
                "validation acquired a journal",
            )
        return

    report = run_wheel_client(w, wait_seconds=4 if case == "lifetime" else 0)
    if case == "lifetime":
        check(
            report["exited_before_client_eof"],
            "lifetime path actually depended on parent EOF",
        )
    if case != "direct":
        lines = p.read(Path(value["journal_path"])).splitlines()
        initial = json.loads(lines[0])
        check(
            initial["instance_token"] == value["instance_token"]
            and initial["pid"] > 0
            and initial["incarnation"].isdigit()
            and initial["status"] == "started-before-dispatch",
            "journal lacks pre-dispatch incarnation",
        )
        last = json.loads(lines[-1])
        check(
            last["status"] == "service-returned",
            "journal never recorded normal service return",
        )


def validate_launch(argv, *, nested=False, direct=False):
    count = argv.count("--lifecycle")
    if (nested and count != 1) or (direct and count != 0):
        raise ValueError("lifecycle option inconsistent with launch leg")
    if count and (count != 1 or argv.index("--lifecycle") != argv.index("--") - 2):
        raise ValueError("noncanonical lifecycle placement")


def exercise_attribution(root, tmp, case):
    from tests.mcp_attribution_support import exercise

    exercise(root, tmp, case)


def exercise_build_control(root, tmp, fault):
    from tests import mcp_wheel_support as wheel
    from tests.mcp_attribution_support import dist_identity, scripted_uv
    from tests.wheel_attribution_cases import case

    if fault == "changed-readme":
        return exercise_attribution(root, tmp, case("readme-only-mutation"))
    prepared = prepare_inputs(root, tmp / "prepared")
    evidence = tmp / "evidence"
    evidence.mkdir()
    executable, call = scripted_uv(tmp, case("success"))
    env = clean_environment(
        {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    )
    if fault.startswith(("sdist-", "wheel-")):
        (prepared / "dist").mkdir()
        name = (
            "footman-0.1.0.tar.gz"
            if fault.startswith("sdist")
            else "footman-0.1.0-py3-none-any.whl"
        )
        path = prepared / "dist" / name
        if fault.endswith("file"):
            p.write(path, b"unowned output")
        elif fault.endswith("directory"):
            path.mkdir()
        else:
            os.symlink("missing-target", path)
        before = dist_identity(prepared)
    elif fault == "unsafe-dist":
        os.symlink(tmp, prepared / "dist")
    elif fault == "unsupported-profile":
        rewrite(
            prepared / "pyproject.toml",
            p.read(prepared / "pyproject.toml")
            + b"\n[tool.hatch.build.hooks.custom]\n",
        )
    elif fault == "ambient-setting":
        env["UV_BUILD_OUT_DIR"] = str(tmp / "unowned-output")
    elif fault == "ambient-config":
        config = tmp / "xdg-config/uv"
        config.mkdir(parents=True)
        p.write(config / "uv.toml", b'build-out-dir = "unowned-output"\n')
        env["XDG_CONFIG_HOME"] = str(config.parent)
    elif fault == "unsafe-readme":
        (prepared / "README.md").unlink()
        os.symlink("pyproject.toml", prepared / "README.md")
    elif fault == "missing-readme":
        (prepared / "README.md").unlink()
    try:
        wheel.build_and_install_contract_wheel(
            prepared,
            admission=None,
            evidence_directory=evidence,
            attribution_only=True,
            env=env,
        )
    except wheel.BuildFailure, ValueError, OSError:
        pass
    else:
        raise AssertionError("unsafe profile/input/output accepted")
    check(not call.exists(), "preflight rejection still invoked uv")
    if fault.startswith(("sdist-", "wheel-")):
        check(
            dist_identity(prepared) == before, "collision changed unowned bytes/kinds"
        )


def actual_attestation(tmp):
    root = Path(__file__).resolve().parents[1]
    w = world(root, tmp)
    _, entries = p.manifest_set(w["manifest"])
    parser = p.AttestationParser(entries)
    observed = TransportObservation()

    async def run():
        async with stdio_transport(
            w["argv"],
            cwd=w["cwd"],
            env=w["env"],
            timeout_seconds=10,
            parser=parser,
            observation=observed,
        ):
            await anyio.sleep(0.05)

    anyio.run(run)
    check(
        parser.confirmed and observed.result.returncode == 0,
        "actual child did not attest cleanly",
    )
    return w, parser


def exercise_evidence_fault(tmp, fault):
    from unittest.mock import patch

    root = Path(__file__).resolve().parents[1]
    if fault in {"before-attestation", "after-attestation"}:
        w = world(root, tmp)
        if fault == "before-attestation":
            w["manifest"]["modules"][0]["sha256"] = "0" * 64
            rewrite(w["origin"], p.canonical(w["manifest"]))
        else:
            w["env"]["FOOTMAN_BOOTSTRAP_TEST_FAULT"] = "after-attestation"
        _, entries = p.manifest_set(w["manifest"])
        parser = p.AttestationParser(entries)
        observed = TransportObservation()

        async def run():
            async with stdio_transport(
                w["argv"],
                cwd=w["cwd"],
                env=w["env"],
                timeout_seconds=10,
                parser=parser,
                observation=observed,
            ):
                await anyio.sleep(0.1)

        try:
            anyio.run(run)
        except Exception:
            pass
        else:
            raise AssertionError("deliberately failing child treated as successful")
        check(observed.result.returncode != 0, "child failure status lost")
        target = tmp / "retained.json"
        if fault == "after-attestation":
            check(
                parser.confirmed,
                "post-attestation failure erased independently valid provenance",
            )
            p.retain_evidence(target, w["manifest"], parser, w["argv"])
        else:
            try:
                p.retain_evidence(target, w["manifest"], parser, w["argv"])
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "pre-attestation child authorized verified evidence"
                )
            check(not target.exists(), "unconfirmed expectations labelled verified")
        return
    w, parser = actual_attestation(tmp)
    target = tmp / "retained.json"
    original = p.write

    def write(path, data):
        if fault == "missing":
            return
        if fault == "truncated":
            data = data[:10]
        elif fault == "inconsistent":
            data = p.canonical(json.loads(data) | {"source_count": 0})
        elif fault == "unwritable":
            raise PermissionError("injected evidence permission failure")
        original(path, data)

    with patch.object(p, "write", write):
        try:
            p.retain_evidence(target, w["manifest"], parser, w["argv"])
        except ValueError, OSError:
            pass
        else:
            raise AssertionError("missing/corrupt evidence reported as retained")
    check(parser.confirmed, "file failure changed actual child attribution")


def exercise_resource_fault(tmp, branch, fault):
    import hashlib
    import io
    import zipfile
    from unittest.mock import patch

    from footman.filesystem.descriptors import OwnedDescriptor
    from tests import mcp_lifecycle as lifecycle
    from tests import mcp_wheel_support as wheel

    source = tmp / "input"
    p.write(source, b"input bytes")
    destination = tmp / "output"
    label = (
        "build input"
        if branch == "build-inventory"
        else "MCP package source"
        if branch == "package-enumeration"
        else "MCP input"
        if branch in {"read", "input", "archive", "dist-inventory"}
        else "test bootstrap lifecycle journal"
        if branch == "journal"
        else "MCP exclusive evidence"
    )
    branch_root = tmp
    if branch == "build-inventory":
        branch_root = prepare_inputs(
            Path(__file__).resolve().parents[1], tmp / "build-inputs"
        )
    elif branch == "package-enumeration":
        branch_root = Path(__file__).resolve().parents[1] / "src/footman"
    elif branch == "dist-inventory":
        branch_root = tmp / "dist"
        branch_root.mkdir()
        p.write(branch_root / "unrelated.whl", b"recorded attribution bytes")
    if branch == "archive":
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name in ("__init__.py", "startup.py", "process.py", "__main__.py"):
                archive.writestr("footman/" + name, b"# source\n")
            archive.writestr(
                "footman-0.1.0.dist-info/entry_points.txt",
                "[console_scripts]\nfootman=footman.startup:main\n",
            )
        rewrite(source, data.getvalue())
    if branch in {"input", "journal"}:
        rewrite(source, p.canonical(lifecycle_input(tmp)))
    manifest = parser = argv = None
    if branch == "evidence":
        setup = tmp / "attested-child"
        setup.mkdir()
        w, parser = actual_attestation(setup)
        manifest, argv = w["manifest"], w["argv"]
    archive_digest = hashlib.sha256(p.read(source)).hexdigest()
    operation_error = (
        KeyboardInterrupt("operation interruption")
        if fault == "interruption"
        else OSError("operation failure")
    )
    release_error = OSError("sole/independent close failure")
    parent_release_error = OSError("independent parent close failure")
    owners, attempts = [], []
    original_init, original_close = OwnedDescriptor.__init__, OwnedDescriptor.close
    original_read, original_write = os.read, os.write
    inject_operation = fault in {"operation-close", "interruption"}
    inject_close = fault in {
        "operation-close",
        "interruption",
        "sole-close",
        "independent",
    }
    fired = False

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        owners.append(self)

    def close(self):
        try:
            _ = self.fd
            live = True
        except Exception:
            live = False
        original_close(self)
        if live:
            attempts.append(self)
            if inject_close and self.label == label:
                raise release_error
            if (
                fault == "independent"
                and self.label == str(branch_root)
                and any(owner.label == label and owner.released for owner in owners)
            ):
                raise parent_release_error

    def selected(fd):
        return any(
            not owner.released and owner.label == label and owner.fd == fd
            for owner in owners
        )

    def read(fd, count):
        nonlocal fired
        if inject_operation and not fired and selected(fd):
            fired = True
            raise operation_error
        return original_read(fd, count)

    def write(fd, data):
        nonlocal fired
        if inject_operation and not fired and selected(fd):
            fired = True
            raise operation_error
        return original_write(fd, data)

    caught = None
    journal = None
    with (
        patch.object(OwnedDescriptor, "__init__", initialize),
        patch.object(OwnedDescriptor, "close", close),
        patch.object(os, "read", read),
        patch.object(os, "write", write),
    ):
        try:
            if branch == "read":
                p.read(source)
            elif branch == "write":
                p.write(destination, b"retained")
            elif branch == "copy":
                wheel.copy_support_file(source, destination)
            elif branch == "archive":
                wheel.archive_expectations(source, archive_digest, tmp / "site")
            elif branch == "build-inventory":
                wheel.inventory(branch_root)
            elif branch == "package-enumeration":
                p.sources(branch_root)
            elif branch == "dist-inventory":
                from tests.mcp_attribution_support import dist_identity

                dist_identity(tmp)
            elif branch == "input":
                lifecycle.validate_input(source)
            elif branch == "journal":
                journal = lifecycle.start(source)
                journal.finish()
            else:
                p.retain_evidence(destination, manifest, parser, argv)
        except BaseException as error:
            caught = error
        if journal is not None:
            try:
                journal.finish()
            except BaseException as error:
                check(
                    error is caught, "repeat journal cleanup changed retained failure"
                )
        for owner in owners:
            owner.close()
    if inject_operation:
        check(
            caught is operation_error, "operation/interruption authority lost to close"
        )
    elif inject_close:
        check(caught is release_error, "sole release failure lost")
    else:
        check(caught is None, "successful/repeat resource operation failed")

    def live(owner):
        try:
            _ = owner.fd
        except Exception:
            return False
        return True

    check(
        not any(live(owner) for owner in owners),
        "independent resource retained after settled operation",
    )
    check(len(attempts) == len(set(map(id, attempts))), "release retried")
    check(
        any(owner.label == label for owner in attempts),
        "matrix never exercised the owned branch",
    )
    if fault == "independent":
        check(
            any(
                error is parent_release_error
                for error in getattr(caught, "_transaction_secondary", ())
            ),
            "independent parent release diagnostic lost",
        )
        check(
            any(owner.label == str(branch_root) for owner in attempts),
            "parent release not attempted independently",
        )


def exercise_transport_fault(tmp, fault):
    from unittest.mock import patch

    from footman import process
    from tests.mcp_transport import write_frame

    root = Path(__file__).resolve().parents[1]
    argv = [sys.executable, "-P", str(root / "tests/mcp_protocol_child.py"), "split"]
    observed = TransportObservation()
    write_error = (
        KeyboardInterrupt("partial stdin interruption")
        if fault == "interruption"
        else BrokenPipeError("peer input closed")
        if fault == "broken-pipe"
        else OSError("partial stdin failure")
    )
    close_error = (
        process.StdinCloseTimeout("pending EOF")
        if fault == "eof-pending"
        else OSError("stdin sole close failure")
    )
    state = {"streams": 0, "writes": 0, "closes": 0}
    real_write = os.write

    def launch(args, **kwargs):
        check(
            kwargs["stdin_mode"] == "pipe" and kwargs["capture_limit"] == 0,
            "transport changed managed stdin/output collection contract",
        )
        if fault == "handoff":

            def failed():
                raise write_error

            kwargs["on_launched"] = failed
        owner = process.OwnedProcess.launch(args, **kwargs)
        original_stream = owner.stream

        def stream(*args, **kwargs):
            state["streams"] += 1
            return original_stream(*args, **kwargs)

        owner.stream = stream
        if fault in {"partial-frame", "broken-pipe", "operation-close", "interruption"}:
            original_write = owner.write_stdin

            def write(data, **options):
                calls = 0

                def os_write(fd, payload):
                    nonlocal calls
                    if fd != owner._stdin_fd:
                        return real_write(fd, payload)
                    calls += 1
                    if calls == 1:
                        return real_write(fd, payload[:2])
                    raise write_error

                state["writes"] += 1
                with patch.object(os, "write", os_write):
                    return original_write(data, **options)

            owner.write_stdin = write
        if fault in {"operation-close", "sole-close", "independent", "eof-pending"}:
            original_close = owner.close_stdin

            def close(**options):
                state["closes"] += 1
                original_close(**options)
                raise close_error

            owner.close_stdin = close
        return owner

    if fault == "interruption":
        owner = launch(
            argv,
            stdin_mode="pipe",
            deadline_seconds=3,
            grace_seconds=1,
            capture_limit=0,
            popen_kwargs={"cwd": str(tmp), "env": clean_environment()},
        )
        try:
            snapshot, error = write_frame(owner, b'{"incomplete": true}\n', 1)
            check(
                error is write_error
                and snapshot.error is write_error
                and snapshot.accepted_bytes == 2
                and snapshot.outcome == "interrupted",
                "interruption progress/identity was lost or mutated",
            )
            check(
                not hasattr(write_error, "accepted_bytes"),
                "transport mutated control exception",
            )
            try:
                owner.close_stdin(timeout_seconds=1)
                owner.stream(lambda *_: None)
            except BaseException as error:
                check(error is write_error, "cleanup replaced write interruption")
        finally:
            status = owner.shutdown()
        check(
            status.complete and any(f.error is write_error for f in status.failures),
            "interruption owner/ledger lost",
        )
        return
    expected_failure = fault in {
        "partial-frame",
        "broken-pipe",
        "operation-close",
        "sole-close",
        "independent",
        "handoff",
        "eof-pending",
    }

    async def run():
        async with stdio_transport(
            argv,
            cwd=tmp,
            env=clean_environment(),
            timeout_seconds=4,
            observation=observed,
            launch=launch,
        ) as streams:
            async with ClientSession(*streams) as session:
                with anyio.fail_after(2):
                    await session.initialize()
                    if fault == "concurrent":
                        async with anyio.create_task_group() as group:

                            async def discover():
                                check(
                                    (await session.list_tools()).tools[0].name
                                    == "echo",
                                    "concurrent protocol responses mixed",
                                )

                            group.start_soon(discover)
                            group.start_soon(discover)
                    else:
                        await session.list_tools()

    try:
        anyio.run(run)
    except Exception:
        check(expected_failure, "normal/repeat/concurrent transport failed")
    else:
        check(
            not expected_failure,
            "transport reported success after injected operation/EOF failure",
        )
    owner = observed.owner
    check(
        owner is not None and owner.cleanup_status.complete,
        "transport discarded pending/acquired owner",
    )
    check(state["streams"] == 1, "transport collected child streams more than once")
    if fault in {"partial-frame", "broken-pipe", "operation-close"}:
        check(state["writes"] == 1, "failed frame or suffix replayed")
        snapshot = observed.write_observations[0]
        check(
            snapshot.accepted_bytes == 2
            and snapshot.error is write_error
            and observed.failure.cause is write_error,
            "same-thread write observation/cause lost",
        )
        check(
            not snapshot.timed_out
            and snapshot.outcome == "failed"
            and any(
                f.error is write_error
                and f.phase is process.FailurePhase.COORDINATION
                and f.stream is None
                for f in owner.cleanup_status.failures
            ),
            "write failure progress/ledger attribution mismatch",
        )
    if fault in {"operation-close", "sole-close", "independent", "eof-pending"}:
        check(
            observed.eof_error is close_error
            and state["closes"] == 1
            and not observed.eof_delivered,
            "failed/pending EOF fabricated delivery or was retried",
        )
    if fault == "handoff":
        check(
            any(f.error is write_error for f in owner.cleanup_status.failures),
            "handoff failure identity absent",
        )
    before = dict(state)
    owner.shutdown()
    owner.wait_cleanup(0.1)
    check(state == before, "repeat cleanup renewed transport work/collection")


def exercise_build_process_fault(tmp, fault, action=None, after_successes=0):
    from unittest.mock import patch

    from footman.filesystem.streams import OwnedStream
    from tests import mcp_wheel_support as wheel

    real_launch = wheel.OwnedProcess.launch
    real_close = OwnedStream.close
    acquired, releases = [], []
    selected = [False]
    operation = (
        KeyboardInterrupt("setup interrupted")
        if fault == "interruption"
        else OSError("setup operation failed")
    )
    failures = {name: OSError(name + " sole close") for name in ("stdout", "stderr")}

    def close(self):
        live = not self.released
        real_close(self)
        if live:
            releases.append(self)
            if (
                selected[0]
                and self.label in failures
                and (
                    fault == "independent"
                    or (self.label == "stdout" and fault != "repeat")
                )
            ):
                raise failures[self.label]

    def launch(argv, **kwargs):
        selected[0] = len(acquired) >= after_successes
        if selected[0] and fault in {"operation-close", "interruption"}:

            def fail():
                raise operation

            kwargs["on_launched"] = fail
        try:
            owner = real_launch(argv, **kwargs)
        except BaseException as error:
            if getattr(error, "cleanup", None) is not None:
                acquired.append(error.cleanup)
            raise
        acquired.append(owner)
        return owner

    caught = None
    with (
        patch.object(wheel.OwnedProcess, "launch", launch),
        patch.object(OwnedStream, "close", close),
    ):
        try:
            if action is None:
                result = wheel.command(
                    [sys.executable, "-P", "-c", "print('helper')"],
                    cwd=tmp,
                    env=clean_environment(),
                    timeout_seconds=3,
                )
                check(
                    result["status"] == 0 and result["stdout"] == b"helper\n",
                    "successful command output/status drift",
                )
            else:
                action()
        except BaseException as error:
            caught = error
        for owner in acquired:
            check(
                owner.cleanup_status.complete, "build/install process owner not settled"
            )
            before = len(releases)
            owner.shutdown()
            owner.wait_cleanup(0.1)
            check(len(releases) == before, "build/install helper close retried")
    check(acquired, "process acquisition branch not exercised")
    check(
        len(releases) == len(set(map(id, releases))),
        "one stream had multiple release attempts",
    )
    check(
        {"stdout", "stderr"} <= {owner.label for owner in releases},
        "independent sibling release omitted",
    )
    ledger = acquired[after_successes].cleanup_status.failures
    if fault == "repeat":
        check(caught is None, "repeat cleanup changed success")
    elif fault == "interruption":
        check(caught is operation, "helper interruption authority/identity changed")
    else:
        check(caught is not None, "helper failed release/operation reported success")
    if fault in {"interruption", "operation-close"}:
        check(
            any(item.error is operation for item in ledger),
            "original helper operation absent from retained ledger",
        )
    if fault != "repeat":
        check(
            any(item.error is failures["stdout"] for item in ledger),
            "sole helper close lost",
        )
    if fault == "independent":
        check(
            any(item.error is failures["stderr"] for item in ledger),
            "independent helper close lost",
        )


def exercise_terminal_fault(tmp, stream, terminal):
    from footman.process import OwnedProcess, ProcessStream, StreamOutcome

    observed = TransportObservation()

    def launch(argv, **kwargs):
        owner = OwnedProcess.launch(argv, **kwargs)
        original = owner.stream

        def collect(chunk, *, on_stream_end):
            def end(which, outcome):
                on_stream_end(
                    which,
                    StreamOutcome[terminal]
                    if which is ProcessStream[stream]
                    else outcome,
                )

            return original(chunk, on_stream_end=end)

        owner.stream = collect
        return owner

    async def run():
        async with stdio_transport(
            [
                sys.executable,
                "-P",
                str(Path(__file__).with_name("mcp_protocol_child.py")),
                "split",
            ],
            cwd=tmp,
            env=clean_environment(),
            timeout_seconds=4,
            observation=observed,
            launch=launch,
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                await session.list_tools()

    try:
        anyio.run(run)
    except RuntimeError:
        pass
    else:
        raise AssertionError("incomplete output collection became transport success")
    check(
        observed.result.returncode == 0
        and observed.terminals[ProcessStream[stream]] is StreamOutcome[terminal],
        "status and stream completeness conflated",
    )
    check(observed.owner.cleanup_status.complete, "terminal fault lost sole owner")


def exercise_parser_callback_fault(tmp, fault):
    observed = TransportObservation()
    marker = OSError("parser callback failed")

    class Parser:
        def feed(self, data):
            if fault == "feed":
                raise marker

        def end(self, outcome):
            if fault == "end":
                raise marker

    async def run():
        async with stdio_transport(
            [
                sys.executable,
                "-P",
                str(Path(__file__).with_name("mcp_protocol_child.py")),
                "stderr",
            ],
            cwd=tmp,
            env=clean_environment(),
            timeout_seconds=4,
            observation=observed,
            parser=Parser(),
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                await session.list_tools()

    try:
        anyio.run(run)
    except OSError as error:
        check(error is marker, "parser failure identity changed")
    else:
        raise AssertionError(
            "coordinator diagnostic-only callback hid domain parser failure"
        )
    check(
        observed.owner.cleanup_status.complete and observed.result.returncode == 0,
        "parser failure and process exit conflated",
    )


def exercise_archive_fault(tmp, fault):
    import hashlib
    import io
    import stat
    import zipfile
    from unittest.mock import patch

    from footman.filesystem.descriptors import OwnedDescriptor
    from tests import mcp_wheel_support as wheel

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("__init__.py", "startup.py", "process.py", "__main__.py"):
            if fault == "missing-module" and name == "__main__.py":
                continue
            archive.writestr("footman/" + name, b"# regular source\n")
        target = (
            "other.module:main" if fault == "entrypoint" else "footman.startup:main"
        )
        archive.writestr(
            "footman-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\nfootman=" + target + "\n",
        )
        if fault == "unsafe-path":
            archive.writestr("../escape.py", b"bad")
        elif fault in {"symlink", "special"}:
            info = zipfile.ZipInfo("footman/bad.py")
            info.external_attr = (
                (stat.S_IFLNK if fault == "symlink" else stat.S_IFIFO) | 0o600
            ) << 16
            archive.writestr(info, b"target")
        elif fault == "ambiguous-module":
            archive.writestr(
                "footman/startup/__init__.py", b"# duplicate logical module\n"
            )
    data = b"not an archive" if fault == "corrupt-close" else buffer.getvalue()
    path = tmp / "bound.whl"
    p.write(path, data)
    close_error = OSError("archive sole close")
    original_close = OwnedDescriptor.close
    count = 0

    def close(self):
        nonlocal count
        live = not self.released and self.label == "MCP input"
        original_close(self)
        if live:
            count += 1
            if fault == "corrupt-close":
                raise close_error

    with patch.object(OwnedDescriptor, "close", close):
        try:
            wheel.archive_expectations(
                path, hashlib.sha256(data).hexdigest(), tmp / "site"
            )
        except (ValueError, zipfile.BadZipFile) as error:
            if fault == "corrupt-close":
                check(
                    isinstance(error, zipfile.BadZipFile),
                    "archive operation masked by ordinary close",
                )
                check(
                    any(
                        item is close_error
                        for item in getattr(error, "_transaction_secondary", ())
                    ),
                    "independent archive close diagnostic lost",
                )
        else:
            raise AssertionError("unsafe/corrupt/malformed archive accepted")
    check(
        count == 1 and not (tmp / "site").exists(),
        "archive validation released twice or installed before validation",
    )


def exercise_profile_setup_error(root, tmp, fault):
    from unittest.mock import Mock, patch

    from tests import mcp_attribution_support as driver
    from tests import mcp_wheel_support as wheel
    from tests.wheel_attribution_cases import case

    original = prepare_inputs

    def broken(*args):
        result = original(*args)
        if fault == "missing-input":
            (result / "README.md").unlink()
        else:
            (result / "tests").mkdir()
        return result

    build = Mock(
        side_effect=AssertionError(
            "public build must not be called for invalid profile"
        )
    )
    with (
        patch(__name__ + ".prepare_inputs", broken),
        patch.object(wheel, "build_and_install_contract_wheel", build),
    ):
        try:
            driver.exercise(root, tmp, case("success"))
        except AssertionError as error:
            check(
                "fixture profile" in str(error),
                "setup failure mislabeled as attribution rejection",
            )
        else:
            raise AssertionError("invalid scripted profile accepted")
    build.assert_not_called()


def exercise_partial_reservation(root, tmp):
    import base64

    w = world(root, tmp, require_phase=True)
    directory = install_docker(w)
    w["env"]["FOOTMAN_BOOTSTRAP_TEST_FAULT"] = "reserve-partial"

    async def scenario():
        async with client(w) as (session, _):
            first = await session.call_tool(
                "run_privileged_command", {"phase": "first"}
            )
            value = first.structuredContent
            check(
                first.isError
                and value["outcome"] == "output_failed"
                and value["return_code"] is None,
                "partial reservation reported success/command status",
            )
            check(
                {key for key in value if key.endswith("Path")} == {"stdoutPath"},
                "partial reservation fabricated uncreated file paths",
            )
            retained = Path(value["stdoutPath"])
            check(
                retained.is_absolute() and p.read(retained) == b"",
                "created stdout evidence missing or nonempty",
            )
            check(
                "injected partial"
                in base64.b64decode(
                    value["diagnostics"]["cause"]["message_base64"]
                ).decode(),
                "original storage diagnostic lost",
            )
            check(
                not (tmp / "snapshots").exists() and not records(directory),
                "storage failure reached snapshot/Docker",
            )
            second = await session.call_tool(
                "run_privileged_command", {"phase": "second"}
            )
            response_check(second, status=0, succeeded=True)
            check(
                p.read(retained) == b""
                and second.structuredContent["stdoutPath"] != str(retained),
                "next request changed/reused partial evidence",
            )
            check(
                retained.parent.parent.name == "first"
                and Path(second.structuredContent["stdoutPath"]).parent.parent.name
                == "second",
                "partial-failure requests shared a timestamp namespace",
            )

    anyio.run(scenario)
    check(
        sum(item["operation"] == "launch" for item in records(directory)) == 1,
        "failed reservation triggered execution or retry",
    )


def exercise_hostile_locale(root, tmp, context):
    explicit = {
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONIOENCODING": "ascii",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    env = clean_environment()
    env.update(explicit)  # Deliberately AFTER cleaning: normal launches strip PYTHON*.
    observation = TransportObservation()
    text = "привет — 世界"
    if context == "service":
        w = world(root, tmp)
        w["env"] = dict(env, FOOTMAN_BOOTSTRAP_TEST_DIAGNOSTIC="locale")

        async def run():
            async with client(w) as (session, observed):
                check(
                    (await session.list_tools()).tools[0].name
                    == "run_privileged_command",
                    "locale discovery failed",
                )
                result = await session.call_tool(
                    "run_privileged_command", {"phase": text}
                )
                check(
                    result.isError and text in result.content[0].text,
                    "Unicode validation response did not round-trip",
                )
            return observed

        observation = anyio.run(run)
        env = w["env"]
        check(
            not (tmp / "results").exists(), "invalid Unicode phase reached reservation"
        )
    else:

        async def run():
            async with stdio_transport(
                [
                    sys.executable,
                    "-P",
                    str(root / "tests/mcp_protocol_child.py"),
                    "hostile-locale",
                ],
                cwd=tmp,
                env=env,
                timeout_seconds=5,
                observation=observation,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    check(
                        (await session.list_tools()).tools[0].name == "echo",
                        "locale peer discovery failed",
                    )
                    result = await session.call_tool("echo", {"text": text})
                    check(
                        not result.isError and result.content[0].text == text,
                        "non-ASCII echo corrupted",
                    )

        anyio.run(run)
    check(
        all(env[key] == value for key, value in explicit.items()),
        "launch locale overrides lost",
    )
    stderr = bytes(observation.stderr_tail)
    for key, value in explicit.items():
        check(
            f"{key}={value}".encode("ascii") in stderr,
            "child did not observe explicit locale",
        )
    check(
        b"stdin=ascii stdout=ascii utf8_mode=0" in stderr,
        "hostile locale was not effective",
    )
    reserved = [line for line in stderr.splitlines() if line.startswith(p.PREFIX)]
    check(
        len(reserved) == int(context == "service"),
        "locale diagnostics collided with provenance",
    )
    check(
        observation.failure is None
        and observation.eof_delivered
        and observation.owner.cleanup_status.complete,
        "locale transport failed or leaked",
    )
    p.write(
        tmp / "locale-launch.json",
        p.canonical(
            {
                "environment": {key: env[key] for key in explicit},
                "context": context,
                "stderr_base64": __import__("base64").b64encode(stderr).decode(),
                "return_code": observation.result.returncode,
            }
        ),
    )
