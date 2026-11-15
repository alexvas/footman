"""Copied isolated-runner probe, not normal real-host support or a Docker proof.

Only disposable prepared roots copy this module as host_tests/probe_support.py.
All verification remains active under -O. Each actual stream/file acquisition is
performed by the existing copied transport/provenance sole-owner primitives.
"""

import base64
import hashlib
import os
import shlex
import sys
import uuid
from pathlib import Path

import anyio
from mcp import ClientSession

import footman
import footman.process
from footman.filesystem.cleanup import CleanupFailures
from footman.process import StreamOutcome
from tests import mcp_provenance as p
from tests import mcp_transport as transport


def record_installation(session=None):
    directory = Path(os.environ["FOOTMAN_HOST_CONTRACT_RECORDS"])
    p.write(
        directory
        / ("installation-" + str(os.getpid()) + "-" + uuid.uuid4().hex + ".json"),
        p.canonical(
            {
                "interpreter": sys.executable,
                "prefix": sys.prefix,
                "optimize": sys.flags.optimize,
                "foundation": footman.process.__file__,
                "package": footman.__file__,
                "transport": transport.__file__,
                "support": p.__file__,
                "environment_keys": sorted(os.environ),
                "pythonpath": os.environ.get("PYTHONPATH"),
                "cwd": str(Path.cwd()),
                "pytest_config_path": None
                if session is None
                else str(session.config.inipath),
                "pytest_configuration": None
                if session is None
                else p.read(Path(session.config.inipath)).decode(),
                "source_set": p.sources(Path(footman.__file__).parent),
            }
        ),
    )


def mutate_installation(fault, origin):
    package = Path(footman.__file__).parent
    selected = package / ("__main__.py" if fault == "stale-entry" else "container.py")
    if fault in {"stale-module", "stale-entry", "self-consistent-stale"}:
        original = p.read(selected)
        selected.unlink()
        changed = (
            b"raise RuntimeError('STALE_ENTRY_EXECUTED')\n" + original
            if fault == "stale-entry"
            else original + b"\n# isolated runner stale-install control\n"
        )
        p.write(selected, changed)
        if fault == "self-consistent-stale":
            # Change the installed RECORD too: it cannot be expectation authority.
            import csv
            import io

            record = package.parent / "footman-0.1.0.dist-info/RECORD"
            rows = list(csv.reader(io.StringIO(p.read(record).decode())))
            for row in rows:
                if row[0] == "footman/container.py":
                    row[1] = "sha256=" + base64.urlsafe_b64encode(
                        hashlib.sha256(p.read(selected)).digest()
                    ).decode().rstrip("=")
                    row[2] = str(len(p.read(selected)))
            data = io.StringIO()
            csv.writer(data).writerows(rows)
            record.unlink()
            p.write(record, data.getvalue().encode())
    elif fault == "missing-module":
        selected.unlink()
    elif fault == "extra-module":
        p.write(package / "unexpected_source.py", b"# isolated extra source\n")
    elif fault == "symlink-module":
        target = Path(os.environ["FOOTMAN_HOST_CONTRACT_RECORDS"]) / "source-target"
        p.write(target, p.read(selected))
        selected.unlink()
        selected.symlink_to(target)
    elif fault in {"origin-missing", "origin-schema", "origin-wheel-binding"}:
        changed = p.load(origin)
        origin.unlink()
        if fault != "origin-missing":
            changed[
                "schema_version" if fault == "origin-schema" else "wheel_sha256"
            ] = True if fault == "origin-schema" else "0" * 64
            p.write(origin, p.canonical(changed))
    elif fault == "wrong-root":
        manifest = p.load(origin)
        for entry in manifest["modules"]:
            entry["expected_path"] = str(
                Path("/not-the-installation") / entry["source_path"]
            )
        origin.unlink()
        p.write(origin, p.canonical(manifest))


def parser_for(entries, fault):
    class Parser(p.AttestationParser):
        def feed(self, data):
            if fault == "attestation-missing":
                return
            if fault == "attestation-duplicate":
                super().feed(p.attestation(entries))
            elif fault == "attestation-malformed":
                super().feed(b"FOOTMAN_PROVENANCE_V1 {}\n")
            elif fault == "attestation-partial":
                super().feed(p.attestation(entries).removesuffix(b"\n"))
                return
            elif fault == "attestation-overlong":
                super().feed(p.PREFIX + b"x" * 512 + b"\n")
            super().feed(data)

        def end(self, terminal):
            # Explicit synthetic parser control; actual transport terminal state
            # is independently retained below, never mislabeled READ_FAILED.
            if fault == "attestation-incomplete":
                terminal = StreamOutcome.READ_FAILED
            super().end(terminal)

    return Parser(entries)


def retain(path, manifest, parser, argv, fault):
    original_read, original_write = p.read, p.write
    original_os_write, original_close = os.write, os.close
    counts = {"writes": 0, "closes": 0}

    def target(fd):
        try:
            return os.readlink(f"/proc/self/fd/{fd}") == str(path)
        except OSError:
            return False

    def write_bytes(fd, data):
        if target(fd):
            counts["writes"] += 1
            if fault == "evidence-operation-close":
                raise OSError("contract evidence operation failure")
            if fault == "evidence-interruption":
                raise KeyboardInterrupt("contract evidence interruption")
        return original_os_write(fd, data)

    def close(fd):
        selected = target(fd)
        original_close(fd)
        if selected:
            counts["closes"] += 1
            if fault in {"evidence-operation-close", "evidence-close"}:
                raise OSError("contract evidence sole close failure")

    def write(destination, data):
        if Path(destination) == path and fault == "evidence-unwritable":
            raise OSError("contract evidence unwritable")
        return original_write(destination, data)

    def read(source, *, transform=lambda data: data):
        if Path(source) == path:
            if fault == "evidence-missing":
                raise FileNotFoundError("contract evidence missing on readback")
            if fault == "evidence-truncated":
                return transform(b'{"schema_version":')
            if fault == "evidence-inconsistent":
                data = p.unique(__import__("json").loads(original_read(source)).items())
                data["source_count"] += 1
                return transform(p.canonical(data))
        return original_read(source, transform=transform)

    try:
        os.write, os.close = write_bytes, close
        p.read, p.write = read, write
        return p.retain_evidence(path, manifest, parser, argv)
    finally:
        p.read, p.write = original_read, original_write
        os.write, os.close = original_os_write, original_close
        record = Path(os.environ["FOOTMAN_HOST_CONTRACT_RECORDS"]) / (
            "evidence-attempt-" + uuid.uuid4().hex + ".json"
        )
        failures = CleanupFailures(sys.exception())
        failures.run(
            lambda: p.write(record, p.canonical(counts)), ordinary=(OSError, ValueError)
        )
        error = failures.complete()
        if error is not None:
            raise error


def run():
    fault = os.environ.get("FOOTMAN_HOST_CONTRACT_FAULT", "none")
    context = p.load(Path(os.environ["FOOTMAN_HOST_CONTEXT"]))
    origin = Path(context["expected_origin_path"])
    manifest = p.load(origin)
    p.require(
        manifest["context"] == "wheel", "runner did not generate wheel expectations"
    )
    p.require(
        context["venv_python"] == sys.executable, "runner selected another interpreter"
    )
    p.require(
        "PYTHONPATH" not in os.environ, "bootstrap PYTHONPATH reached host pytest"
    )
    support = Path(context["support_root"])
    p.require(
        Path(transport.__file__) == support / "tests/mcp_transport.py",
        "transport is not the declared copy",
    )
    p.require(
        Path(footman.process.__file__).is_relative_to(sys.prefix),
        "host client foundation is not installed-wheel derived",
    )
    # Ordinary module checks are post-import attribution. Child bootstrap below
    # independently verifies the full package before service dispatch.
    _, entries = p.manifest_set(manifest)
    mutate_installation(fault, origin)
    if fault in {
        "source-readme-during",
        "source-support-during",
        "copied-support-during",
    }:
        source_root = Path(os.environ["FOOTMAN_HOST_CONTRACT_SOURCE_ROOT"])
        changed = (
            source_root / "README.md"
            if fault == "source-readme-during"
            else source_root / "tests/mcp_transport.py"
            if fault == "source-support-during"
            else support / "tests/mcp_transport.py"
        )
        original = p.read(changed)
        changed.unlink()
        p.write(changed, original + b"\n# isolated runner completion mutation\n")
    config = Path(os.environ["FOOTMAN_HOST_CONTRACT_CONFIG"])
    configured = p.read(config).decode().splitlines()
    configured = [
        "image_id = " + __import__("json").dumps(context["configured_image_id"])
        if line.startswith("image_id = ")
        else line
        for line in configured
    ]
    config.unlink()
    p.write(config, ("\n".join(configured) + "\n").encode())
    parser = parser_for(entries, fault)
    records = Path(os.environ["FOOTMAN_HOST_CONTRACT_RECORDS"])
    tools = records / "service-tools"
    tools.mkdir()
    bundle = Path(__file__).parent / "contract_support"
    wrapper = tools / "docker"
    if fault == "image-launch-failure":
        loader = bundle / "tests/relocated_docker_child.py"
        data = p.read(loader).decode()
        anchor = '    os.environ["FOOTMAN_STUB_SCENARIO"] = json.dumps(scenario)'
        p.require(data.count(anchor) == 1, "image-error probe anchor changed")
        injected = '    if operation == "launch":\n        scenario.update(status=23, stdout="", stderr=base64.b64encode(b"controlled Docker resolution error\\n").decode())\n'
        loader.unlink()
        p.write(loader, data.replace(anchor, injected + anchor).encode())
    argv_stub = [
        sys.executable,
        "-P",
        str(bundle / "tests/relocated_docker_child.py"),
        "--support-root",
        str(bundle),
        "--",
    ]
    p.write(
        wrapper,
        (
            "#!/bin/sh\nexec " + " ".join(map(shlex.quote, argv_stub)) + ' "$@"\n'
        ).encode(),
    )
    wrapper.chmod(0o700)
    stub_records = records / "service-stub-records"
    stub_records.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "FOOTMAN_BOOTSTRAP_", "FOOTMAN_HOST_"))
    }
    if fault in {"shadow-script", "shadow-cwd"}:
        shadow = (
            support / "tests"
            if fault == "shadow-script"
            else Path(context["neutral_cwd"])
        )
        for name in ("footman.py", "json.py"):
            p.write(shadow / name, b"raise RuntimeError('SHADOW_SOURCE_EXECUTED')\n")
    environment.update(
        PYTHONNOUSERSITE="1",
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        FOOTMAN_STUB_RECORDS=str(stub_records),
    )
    if fault == "retag-image":
        configured = context["configured_image_id"]
        full = "sha256:" + configured.removeprefix("sha256:").ljust(64, "b")
        before = {configured: full, "operator-owned:latest": full}
        after = {configured: full, "operator-owned:latest": "sha256:" + "c" * 64}
        p.write(records / "images-before.json", p.canonical(before))
        p.write(records / "images-after.json", p.canonical(after))
        environment["FOOTMAN_STUB_IMAGE_MAP"] = str(records / "images-after.json")
    if fault == "child-after-attestation":
        environment["FOOTMAN_BOOTSTRAP_TEST_FAULT"] = "after-attestation"
    argv = [
        sys.executable,
        "-P",
        str(support / "tests/mcp_server_child.py"),
        "--expected-origin",
        str(origin),
        "--entrypoint",
        context["server_script"],
        "--",
        "--config",
        os.environ["FOOTMAN_HOST_CONTRACT_CONFIG"],
    ]
    evidence = Path(context["provenance_evidence_path"])
    p.require("--lifecycle" not in argv, "host service used nested-relocated argv")
    observation = transport.TransportObservation()
    primary = None

    async def scenario():
        async with transport.stdio_transport(
            argv,
            cwd=context["neutral_cwd"],
            env=environment,
            timeout_seconds=20,
            parser=parser,
            observation=observation,
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                p.require(
                    (await session.list_tools()).tools[0].name
                    == "run_privileged_command",
                    "real SDK tool discovery failed",
                )
                result = await session.call_tool("run_privileged_command", {})
                p.write(
                    records / "mcp-result.json", p.canonical(result.structuredContent)
                )
                p.require(not result.isError, "stub command unsuccessful")

    try:
        anyio.run(scenario)
    except BaseException as error:
        primary = error
    failures = CleanupFailures(primary)

    def attempt(operation):
        failures.run(operation, ordinary=(OSError, ValueError))

    attempt(
        lambda: p.require(
            parser.confirmed, "contract attestation incomplete: " + str(parser.failure)
        )
    )
    attempt(
        lambda: p.require(
            p.load(origin) == manifest, "expected-origin input changed during service"
        )
    )
    if parser.confirmed:
        attempt(lambda: retain(evidence, manifest, parser, argv, fault))
    if observation.owner is not None:
        status = observation.owner.cleanup_status
        attempt(
            lambda: p.require(
                status.complete and not status.failures,
                "contract child cleanup incomplete",
            )
        )
        attempt(
            lambda: p.require(
                observation.owner.shutdown() == observation.owner.cleanup_status,
                "repeat child cleanup changed observations",
            )
        )
    else:
        status = None
    attempt(
        lambda: p.write(
            records / "service-observation.json",
            p.canonical(
                {
                    "argv": argv,
                    "cwd": context["neutral_cwd"],
                    "environment_keys": sorted(environment),
                    "fault_input": fault,
                    "parser_confirmed": parser.confirmed,
                    "parser_terminal": None
                    if parser.terminal is None
                    else parser.terminal.name,
                    "parser_failure": parser.failure,
                    "child_cleanup_complete": None
                    if status is None
                    else status.complete,
                    "child_return_code": None
                    if observation.result is None
                    else observation.result.returncode,
                    "actual_stream_terminals": {}
                    if observation.result is None
                    else {
                        key.name: value.name
                        for key, value in observation.result.stream_outcomes
                    },
                    "primary_type": None if primary is None else type(primary).__name__,
                    "stderr_tail_base64": base64.b64encode(
                        observation.stderr_tail
                    ).decode("ascii"),
                    "foundation": footman.process.__file__,
                    "transport": transport.__file__,
                }
            ),
        )
    )
    error = failures.complete()
    if error is not None:
        raise error
    if primary is not None:
        raise primary
