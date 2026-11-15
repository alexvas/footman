"""Phase-10 orchestration, independently building its exact wheel before relocation."""

import base64
import csv
import hashlib
import importlib
import io
import json
import os
import shlex
import stat
import sys
import threading
import time
import tomllib
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DescriptorError, DirectoryDescriptor
from footman.process import OwnedProcess, ProcessStream
from tests import acceptance_build_support as build
from tests import mcp_contract_support as primitives
from tests import mcp_provenance as p
from tests.mcp_attribution_support import dist_identity

SUPPORT = (
    *p.SUPPORT_FILES,
    "tests/mcp_relocated_client.py",
    "tests/relocated_docker_child.py",
    "tests/docker_cli_stub.py",
    "tests/dispatcher_harness.py",
)


@dataclass
class Worker:
    candidate: Path
    root: Path
    worker_id: str
    bound: object
    python: Path
    wheel_sha256: str
    site: Path
    support: Path
    operations: list
    inventory: tuple
    dist_pre: object
    dependency_identity: str
    reuse_count: int = 0
    cleanup_count: int = 0
    tests: list = field(default_factory=list)


def operation_record(operation):
    return {
        **operation,
        "stdout": base64.b64encode(operation["stdout"]).decode(),
        "stderr": base64.b64encode(operation["stderr"]).decode(),
    }


def prepare_worker(candidate, root, *, worker_id):
    before = dist_identity(candidate)
    initial_support = {
        name: hashlib.sha256(p.read(candidate / name)).hexdigest() for name in SUPPORT
    }
    lock_bytes = p.read(candidate / "uv.lock")
    dependency_identity = hashlib.sha256(lock_bytes).hexdigest()
    prepared = primitives.prepare_inputs(candidate, root / "build-root")
    bound = build.build_and_bind_wheel(
        prepared, evidence_directory=root, timeout_seconds=120
    )
    if not (dist_identity(candidate) == before):
        raise AssertionError("verification failed: dist_identity(candidate) == before")
    # Validate digest/member mapping before any venv or install acquisition.
    build.archive_expectations(
        bound.wheel, bound.wheel_sha256, Path("/archive-validation-only")
    )
    env = build.clean_environment()
    constraints = root / "constraints.txt"
    packages = tomllib.loads(lock_bytes.decode())["package"]
    p.write(
        constraints,
        (
            "\n".join(
                sorted(
                    {
                        f"{item['name']}=={item['version']}"
                        for item in packages
                        if item["name"] != "footman"
                    }
                )
            )
            + "\n"
        ).encode(),
    )
    venv = root / "venv"
    operations = []
    deadline = time.monotonic() + 120
    for argv in (
        ["uv", "venv", "--python", sys.executable, str(venv)],
        [
            "uv",
            "pip",
            "install",
            "--constraint",
            str(constraints),
            "--python",
            str(venv / "bin/python"),
            str(bound.wheel),
            "anyio==4.16.0",
        ],
    ):
        if not (hashlib.sha256(p.read(bound.wheel)).hexdigest() == bound.wheel_sha256):
            raise AssertionError(
                "verification failed: hashlib.sha256(p.read(bound.wheel)).hexdigest() == bound.wheel_sha256"
            )
        observed = build.command(
            argv, cwd=root, env=env, timeout_seconds=deadline - time.monotonic()
        )
        operations.append(observed)
        if not (observed["status"] == 0):
            raise AssertionError(observed)
    scheme = build.command(
        [
            str(venv / "bin/python"),
            "-P",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        cwd=root,
        env=env,
        timeout_seconds=deadline - time.monotonic(),
    )
    operations.append(scheme)
    if not (scheme["status"] == 0):
        raise AssertionError("verification failed: scheme['status'] == 0")
    site = Path(scheme["stdout"].decode().strip())
    if not (site.is_relative_to(venv)):
        raise AssertionError("verification failed: site.is_relative_to(venv)")
    support = root / "copied"
    (support / "tests").mkdir(parents=True)
    inventory = []
    for name in SUPPORT:
        build.copy_support_file(candidate / name, support / name)
        inventory.append(
            {
                "path": name,
                "sha256": hashlib.sha256(p.read(candidate / name)).hexdigest(),
            }
        )
    if not (all(item["sha256"] == initial_support[item["path"]] for item in inventory)):
        raise AssertionError(
            "verification failed: all((item['sha256'] == initial_support[item['path']] for item in inventory))"
        )
    if not (
        hashlib.sha256(p.read(candidate / "uv.lock")).hexdigest() == dependency_identity
    ):
        raise AssertionError(
            "verification failed: hashlib.sha256(p.read(candidate / 'uv.lock')).hexdigest() == dependency_identity"
        )
    worker = Worker(
        candidate,
        root,
        worker_id,
        bound,
        venv / "bin/python",
        bound.wheel_sha256,
        site,
        support,
        operations,
        tuple(inventory),
        before,
        dependency_identity,
    )
    if not (build.inventory(prepared)[0] == bound.build_input_identity):
        raise AssertionError(
            "verification failed: build.inventory(prepared)[0] == bound.build_input_identity"
        )
    p.write(
        root / "worker-setup.json",
        p.canonical(
            {
                "worker": worker_id,
                "build_root": str(prepared),
                "venv": str(venv),
                "scratch": str(root),
                "build_count": 1,
                "venv_count": 1,
                "wheel": str(bound.wheel),
                "wheel_sha256": bound.wheel_sha256,
                "build_input_identity": bound.build_input_identity,
                "input_inventory": bound.inventory,
                "build_operation": operation_record(bound.command),
                "setup_operations": [operation_record(op) for op in operations],
                "support_inventory": inventory,
                "dependency_identity": dependency_identity,
            }
        ),
    )
    return worker


def finish_worker(worker):
    if not (worker.cleanup_count == 0):
        raise AssertionError("verification failed: worker.cleanup_count == 0")
    if not (
        build.inventory(worker.bound.repository_root)[0]
        == worker.bound.build_input_identity
    ):
        raise AssertionError(
            "verification failed: build.inventory(worker.bound.repository_root)[0] == worker.bound.build_input_identity"
        )
    if not (dist_identity(worker.candidate) == worker.dist_pre):
        raise AssertionError(
            "verification failed: dist_identity(worker.candidate) == worker.dist_pre"
        )
    if not (
        hashlib.sha256(p.read(worker.candidate / "uv.lock")).hexdigest()
        == worker.dependency_identity
    ):
        raise AssertionError(
            "verification failed: hashlib.sha256(p.read(worker.candidate / 'uv.lock')).hexdigest() == worker.dependency_identity"
        )
    for item in worker.inventory:
        if not (
            hashlib.sha256(p.read(worker.candidate / item["path"])).hexdigest()
            == item["sha256"]
        ):
            raise AssertionError(
                "verification failed: hashlib.sha256(p.read(worker.candidate / item['path'])).hexdigest() == item['sha256']"
            )
        if not (
            hashlib.sha256(p.read(worker.support / item["path"])).hexdigest()
            == item["sha256"]
        ):
            raise AssertionError(
                "verification failed: hashlib.sha256(p.read(worker.support / item['path'])).hexdigest() == item['sha256']"
            )
    modules, _ = build.archive_expectations(
        worker.bound.wheel, worker.wheel_sha256, worker.site
    )
    expected = p.source_set(
        [
            {"path": item["source_path"][len("footman/") :], "sha256": item["sha256"]}
            for item in modules
        ]
    )
    if not (p.sources(worker.site / "footman") == expected):
        raise AssertionError("worker base installation was mutated")
    if not (all(item["resources_reconciled"] for item in worker.tests)):
        raise AssertionError(
            "verification failed: all((item['resources_reconciled'] for item in worker.tests))"
        )
    worker.cleanup_count += 1
    p.write(
        worker.root / "worker-completion.json",
        p.canonical(
            {
                "worker": worker.worker_id,
                "build_root": str(worker.bound.repository_root),
                "venv": str(worker.python.parent.parent),
                "scratch": str(worker.root),
                "build_count": 1,
                "venv_count": 1,
                "reuse_count": worker.reuse_count,
                "clone_count": sum(op["argv"][0] == "cp" for op in worker.operations),
                "operations": [operation_record(op) for op in worker.operations],
                "cleanup_count": worker.cleanup_count,
                "resources_reconciled": True,
            }
        ),
    )


@contextmanager
def worker_scope(candidate, root_factory, worker_id):
    """Lazy worker-session lifetime, reused across module fixture re-entry."""
    worker = None
    preparation_error = None
    closed = False

    def obtain():
        nonlocal worker, preparation_error
        if closed:
            raise RuntimeError("worker resource scope is closed")
        if preparation_error is not None:
            raise preparation_error
        if worker is None:
            try:
                worker = prepare_worker(candidate, root_factory(), worker_id=worker_id)
            except BaseException as error:
                preparation_error = error
                raise
        return worker

    try:
        yield obtain
    finally:
        closed = True
        if worker is not None:
            failures = CleanupFailures(sys.exception())
            failures.run(lambda: finish_worker(worker), ordinary=(Exception,))
            selected = failures.complete()
            if selected is not None:
                raise selected


def clone_install(worker, tmp):
    target = tmp / "venv"
    target.mkdir()
    copied = build.command(
        [
            "cp",
            "--archive",
            "--reflink=auto",
            "--no-preserve=ownership",
            str(worker.python.parent.parent) + "/.",
            str(target),
        ],
        cwd=tmp,
        env=build.clean_environment(),
        timeout_seconds=30,
    )
    if not (copied["status"] == 0):
        raise AssertionError("verification failed: copied['status'] == 0")
    worker.operations.append(copied)
    relative_site = worker.site.relative_to(worker.python.parent.parent)
    script = target / "bin/footman"
    data = p.read(script)
    script.unlink()
    p.write(
        script,
        b"#!" + os.fsencode(target / "bin/python") + b"\n" + data.split(b"\n", 1)[1],
    )
    script.chmod(0o700)
    return target / "bin/python", target / relative_site


def prepare_case(worker, tmp, *, mutable=False):
    worker.reuse_count += 1
    python, site = (
        clone_install(worker, tmp) if mutable else (worker.python, worker.site)
    )
    support = tmp / "copied"
    (support / "tests").mkdir(parents=True)
    for name in SUPPORT:
        build.copy_support_file(worker.support / name, support / name)
    repo, config = primitives.configuration(tmp)
    # Instantiate the annotated configuration, retaining its structure and action
    # bounds while replacing operator-owned paths/argv for this disposable leg.
    example_bytes = p.read(worker.candidate / "examples/footman.toml")
    example = tomllib.loads(example_bytes.decode())
    relocated_paths = tomllib.loads(p.read(config).decode())
    actual = dict(example)
    for name in ("repository", "snapshot_directory", "output_dir"):
        actual[name] = relocated_paths[name]
    actual.update(timeout_seconds=10, cleanup_grace_seconds=2)
    if not (actual["container_start_argv"] == example["container_start_argv"]):
        raise AssertionError(
            "verification failed: actual['container_start_argv'] == example['container_start_argv']"
        )
    if not (actual["command_argv"] == example["command_argv"]):
        raise AssertionError(
            "verification failed: actual['command_argv'] == example['command_argv']"
        )
    config.unlink()
    p.write(
        config,
        (
            "\n".join(f"{key} = {json.dumps(value)}" for key, value in actual.items())
            + "\n"
        ).encode(),
    )
    manifest_modules, target = build.archive_expectations(
        worker.bound.wheel, worker.wheel_sha256, site
    )
    script = python.parent / "footman"
    manifest = {
        "schema_version": 1,
        "context": "wheel",
        "wheel_sha256": worker.wheel_sha256,
        "modules": manifest_modules,
        "entrypoint": {
            "script_path": str(script),
            "target": target,
            "observed_script_sha256": hashlib.sha256(p.read(script)).hexdigest(),
        },
    }
    origin = tmp / "expected-origin.json"
    p.write(origin, p.canonical(manifest))
    neutral = tmp / "neutral"
    neutral.mkdir()
    tools = tmp / "docker-tools"
    tools.mkdir()
    wrapper = (
        "#!/bin/sh\nexec "
        + " ".join(
            shlex.quote(item)
            for item in (
                str(python),
                "-P",
                str(support / "tests/relocated_docker_child.py"),
                "--support-root",
                str(support),
                "--",
            )
        )
        + ' "$@"\n'
    )
    p.write(tools / "docker", wrapper.encode())
    (tools / "docker").chmod(0o700)
    records = tmp / "stub-records"
    records.mkdir()
    launched = time.monotonic()
    lifecycle = {
        "schema_version": 1,
        "scratch_root": str(tmp),
        "journal_path": str(tmp / "service-journal.jsonl"),
        "instance_token": uuid.uuid4().hex,
        "service_deadline_monotonic": launched + 60,
        "settlement_allowance_seconds": 5,
    }
    lifecycle_path = tmp / "lifecycle.json"
    p.write(lifecycle_path, p.canonical(lifecycle))
    request = {
        "schema_version": 1,
        "scratch_root": str(tmp),
        "support_root": str(support),
        "expected_origin_manifest": str(origin),
        "provenance_evidence_path": str(tmp / "provenance-evidence.json"),
        "config_path": str(config),
        "installed_server_script": str(script),
        "docker_wrapper_directory": str(tools),
        "neutral_service_cwd": str(neutral),
        "lifecycle_input": str(lifecycle_path),
        "journal_path": lifecycle["journal_path"],
        "lifecycle_binding": lifecycle,
        "calls": [{}, {"phase": "relocated_2"}],
        "transport_deadline_monotonic": launched + 65,
    }
    request_path = tmp / "request.json"
    p.write(request_path, p.canonical(request))
    env = build.clean_environment({"FOOTMAN_STUB_RECORDS": str(records)})
    return {
        "worker": worker,
        "tmp": tmp,
        "python": python,
        "site": site,
        "support": support,
        "support_inventory": {
            name: hashlib.sha256(p.read(support / name)).hexdigest() for name in SUPPORT
        },
        "repo": repo,
        "origin": origin,
        "manifest": manifest,
        "annotated_example_sha256": hashlib.sha256(example_bytes).hexdigest(),
        "configuration": actual,
        "docker_wrapper_bytes_base64": base64.b64encode(wrapper.encode()).decode(),
        "docker_wrapper_sha256": hashlib.sha256(wrapper.encode()).hexdigest(),
        "request": request,
        "request_path": request_path,
        "env": env,
        "neutral": neutral,
        "argv": [
            str(python),
            "-P",
            str(support / "tests/mcp_relocated_client.py"),
            "--support-root",
            str(support),
            "--request",
            str(request_path),
        ],
    }


def incarnation(record):
    """Read-only Linux identity observation; never signal, wait/reap or adopt."""
    p.require(
        type(record.get("pid")) is int
        and record["pid"] > 0
        and isinstance(record.get("incarnation"), str)
        and record["incarnation"].isascii()
        and record["incarnation"].isdecimal(),
        "unknown service incarnation",
    )
    try:
        try:
            raw = p.read_proc_stat(record["pid"])
        except BaseException as error:
            cause = error.__cause__ if isinstance(error, DescriptorError) else error
            error.add_note(
                "incarnation observer: "
                f"pid={record['pid']} expected_starttime_ticks={record['incarnation']} "
                f"observer_pid={os.getpid()} monotonic_ns={time.monotonic_ns()} "
                f"operation=read-proc-stat errno={getattr(cause, 'errno', None)} "
                f"stage={getattr(error, 'stage', None)}"
            )
            raise
    except DescriptorError as error:
        # Only ENOENT of the pid directory establishes absence. Other open,
        # permission/read/close failures stay unknown and fail closed.
        if (
            error.stage == "open-descriptor"
            and isinstance(error.__cause__, FileNotFoundError)
            and error.__cause__.filename == str(record["pid"])
        ):
            return "absent"
        raise
    if raw is None:
        return "absent"
    fields = raw.rsplit(b")", 1)[1].split()
    p.require(len(fields) > 19, "malformed process identity")
    return (
        "absent" if fields[19].decode() != record["incarnation"] else fields[0].decode()
    )


def journal(case):
    path = Path(case["request"]["journal_path"])
    try:
        data = p.read(path)
    except FileNotFoundError:
        return None
    if b"\n" not in data:
        return None
    value = json.loads(data.splitlines()[0])
    if not (
        value["instance_token"]
        == case["request"]["lifecycle_binding"]["instance_token"]
    ):
        raise AssertionError(
            "verification failed: value['instance_token'] == case['request']['lifecycle_binding']['instance_token']"
        )
    return value


def run_resources(scratch):
    """Read-only retained-run reconciliation, separate from child liveness."""
    root = scratch / "results"
    observed = []
    if not os.path.lexists(root):
        return observed

    def visit(directory, path):
        for name in directory.list_names():
            mode = directory.stat_child(name).st_mode
            if stat.S_ISDIR(mode):
                with directory.open_directory(name) as child:
                    visit(child, path / name)
            else:
                p.require(stat.S_ISREG(mode), "unsafe retained-run resource")
                if name != "result.json":
                    continue
                value = p.load(path / name)
                cleanup = value["cleanup"]
                p.require(
                    cleanup.get("confirmed_absent") is True
                    and cleanup.get("snapshot_deleted") is True
                    and cleanup.get("unresolved_resources") is False,
                    "retained run reports unconfirmed resource cleanup",
                )
                snapshot = Path(value["snapshot"]["path"])
                p.require(
                    snapshot.is_relative_to(scratch / "snapshots")
                    and ".." not in snapshot.parts,
                    "retained snapshot outside fixture",
                )
                p.require(
                    not os.path.lexists(snapshot), "retained snapshot still exists"
                )
                observed.append(
                    {
                        "manifest": str(path / name),
                        "container_id": value.get("container_id"),
                        "snapshot": str(snapshot),
                        "snapshot_absent": True,
                    }
                )

    with DirectoryDescriptor.open_secure_path(p.OPS, root) as directory:
        visit(directory, root)
    return observed


def reconcile(case, *, no_launch=False, observed_incarnation=None):
    deadline = case["request"]["transport_deadline_monotonic"]
    record = journal(case)
    journal_record = record
    if record is None and observed_incarnation is not None:
        if not (
            observed_incarnation["instance_token"]
            == case["request"]["lifecycle_binding"]["instance_token"]
        ):
            raise AssertionError(
                "verification failed: observed_incarnation['instance_token'] == case['request']['lifecycle_binding']['instance_token']"
            )
        record = observed_incarnation
    if record is None:
        if no_launch or not (case["tmp"] / "launch-intent.json").exists():
            return {"resources_reconciled": True, "service_acquired": False}
        while record is None and time.monotonic() < deadline:
            time.sleep(0.02)
            record = journal(case)
    if record is None:
        return {
            "resources_reconciled": False,
            "failure": "unknown launch identity",
            "retained_scratch": True,
        }
    if observed_incarnation is None:
        journal_record = record
    while time.monotonic() < deadline:
        state = incarnation(record)
        if state == "absent":
            break
        time.sleep(0.02)
    else:
        return {
            "resources_reconciled": False,
            "failure": "service incarnation not absent",
            "retained_scratch": True,
        }
    # Stub children do not own external containers. Each call record is a
    # completed CLI observation; known fake IDs require matching remove/listing.
    records = []
    descendants = []
    with DirectoryDescriptor.open_secure_path(
        p.OPS, case["tmp"] / "stub-records"
    ) as directory:
        for name in directory.list_names():
            if name.endswith("-call.json"):
                records.append(p.load(case["tmp"] / "stub-records" / name))
            elif name.endswith("-origin.json"):
                descendants.append(p.load(case["tmp"] / "stub-records" / name))
    for descendant in descendants:
        while incarnation(descendant) != "absent":
            if time.monotonic() >= deadline:
                return {
                    "resources_reconciled": False,
                    "failure": "stub descendant remains",
                    "retained_scratch": True,
                }
            time.sleep(0.02)
    launches = [item for item in records if item["argv"][0] == "run"]
    removes = [item for item in records if item["argv"][:2] == ["rm", "-f"]]
    confirms = [item for item in records if item["argv"][:2] == ["container", "ls"]]
    if not (len(removes) == len(confirms) == len(launches)):
        raise AssertionError(
            "stub resource not reconciled: "
            + p.canonical(
                {
                    "counts": {
                        "launches": len(launches),
                        "removes": len(removes),
                        "confirms": len(confirms),
                    },
                    "launch_ids": [item.get("container_id") for item in launches],
                    "observations": records,
                    "scratch_root": str(case["tmp"]),
                }
            ).decode("ascii")
        )
    if not (all(item["status"] == 0 and item["stdin_eof"] for item in records)):
        raise AssertionError("stub resource observation unsuccessful")
    # The relocated stub's expected_argv is derived from received argv. Its
    # successful status (and balanced counts) cannot authorize a cleanup target.
    # Launch IDs are separately attributed from startup stdout by the stub.
    identities = [item.get("container_id") for item in launches]
    if not all(
        isinstance(identity, str)
        and len(identity) == 64
        and all(character in "0123456789abcdefABCDEF" for character in identity)
        for identity in identities
    ):
        raise AssertionError("invalid launch identity")
    if len(set(identities)) != len(identities):
        raise AssertionError("duplicate launch identity")
    if Counter(tuple(item["argv"]) for item in removes) != Counter(
        ("rm", "-f", identity) for identity in identities
    ):
        raise AssertionError("recorded-ID removal mismatch")
    if Counter(tuple(item["argv"]) for item in confirms) != Counter(
        (
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            "id=" + identity,
            "--format",
            "{{.ID}}",
        )
        for identity in identities
    ):
        raise AssertionError("recorded-ID confirmation mismatch")
    retained_runs = run_resources(case["tmp"])
    p.require(
        len(retained_runs) == len(launches),
        "stub launch/retained-run accounting mismatch",
    )
    manifest_identities = [item["container_id"] for item in retained_runs]
    if not all(isinstance(identity, str) for identity in manifest_identities) or (
        Counter(manifest_identities) != Counter(identities)
    ):
        raise AssertionError("recorded-ID retained manifest mismatch")
    return {
        "resources_reconciled": True,
        "retained_runs": retained_runs,
        "service_acquired": True,
        "service_incarnation_absent": True,
        "service_journal": journal_record,
        "service_identity": record,
        "identity_authority": "bootstrap-loss-gate"
        if observed_incarnation is not None
        else "bootstrap-journal",
    }


def run_helper(case):
    data = {stream: bytearray() for stream in ProcessStream}
    outcomes = {}
    stream_calls = 0
    with OwnedProcess.launch(
        case["argv"],
        deadline_seconds=case["request"]["transport_deadline_monotonic"]
        - time.monotonic(),
        grace_seconds=2,
        capture_limit=0,
        popen_kwargs={"cwd": str(case["neutral"]), "env": case["env"]},
    ) as owner:
        stream_calls += 1
        result = owner.stream(
            lambda stream, payload: data[stream].extend(
                payload[: max(0, 65537 - len(data[stream]))]
            ),
            on_stream_end=lambda stream, outcome: outcomes.__setitem__(
                stream.name, outcome.name
            ),
        )
    if not (result.lifecycle.complete and not result.lifecycle.failures):
        raise AssertionError(
            "verification failed: result.lifecycle.complete and (not result.lifecycle.failures)"
        )
    accepted = False
    value = None
    if result.returncode == 0 and all(
        outcomes.get(s.name) == "EOF" for s in ProcessStream
    ):
        try:
            raw = bytes(data[ProcessStream.STDOUT])
            if not (
                len(raw) <= 65536 and raw.endswith(b"\n") and raw.count(b"\n") == 1
            ):
                raise AssertionError(
                    "verification failed: len(raw) <= 65536 and raw.endswith(b'\\n') and (raw.count(b'\\n') == 1)"
                )
            value = json.loads(raw, object_pairs_hook=p.unique)
            accepted = (
                type(value["schema_version"]) is int
                and value["schema_version"] == 1
                and value["provenance_final"] is True
                and value["evidence_readback"] is True
                and type(value["service_exit_code"]) is int
                and value["service_exit_code"] == 0
                and value["wheel_sha256"] == case["worker"].wheel_sha256
                and value["evidence_path"]
                == case["request"]["provenance_evidence_path"]
                and value["service_context"] == "wheel"
            )
        except AssertionError, ValueError, KeyError:
            pass
    if not (
        not any(
            line.startswith(b"FOOTMAN_PROVENANCE_")
            for stream in data.values()
            for line in bytes(stream).splitlines()
        )
    ):
        raise AssertionError(
            "verification failed: not any((line.startswith(b'FOOTMAN_PROVENANCE_') for stream in data.values() for line in bytes(stream).splitlines()))"
        )
    return {
        "accepted": bool(accepted),
        "helper_attempted": True,
        "result": value,
        "helper_status": result.returncode,
        "helper_stream_calls": stream_calls,
        "helper_stream_outcomes": outcomes,
        "helper_stdout_base64": base64.b64encode(data[ProcessStream.STDOUT]).decode(),
        "helper_stderr_base64": base64.b64encode(data[ProcessStream.STDERR]).decode(),
    }


def exercise_relocated(worker, tmp, case_id):
    mutable = case_id in {
        "clone-success",
        "stale-main",
        "swapped-install",
        "stale-container",
        "stale-supervision",
        "missing-source",
        "extra-source",
        "symlink-source",
    }
    case = prepare_case(worker, tmp, mutable=mutable)
    before = dist_identity(worker.candidate)
    identity = build.inventory(worker.bound.repository_root)[0]
    if case_id in {
        "stale-main",
        "swapped-install",
        "stale-container",
        "stale-supervision",
        "missing-source",
        "extra-source",
        "symlink-source",
    }:
        name = {
            "stale-main": "__main__.py",
            "swapped-install": "startup.py",
            "stale-container": "container.py",
            "stale-supervision": "supervision.py",
            "missing-source": "container.py",
            "symlink-source": "container.py",
            "extra-source": "unexpected.py",
        }[case_id]
        path = case["site"] / "footman" / name
        if path.exists():
            data = p.read(path)
            path.unlink()
        else:
            data = b""
        if case_id == "symlink-source":
            target = tmp / "untrusted-source.py"
            p.write(target, data)
            path.symlink_to(target)
        elif case_id != "missing-source":
            p.write(path, data + b"\n# deliberately stale source\n")
        if case_id == "swapped-install":
            # Deliberately make installed RECORD self-consistent. It must not
            # become expected authority in place of the bound archive bytes.
            distribution = (
                worker.bound.wheel.name.removesuffix("-py3-none-any.whl") + ".dist-info"
            )
            record_path = case["site"] / distribution / "RECORD"
            rows = list(csv.reader(io.StringIO(p.read(record_path).decode())))
            for row in rows:
                if row[0] == distribution + "/RECORD":
                    row[1:] = ["", ""]
                    continue
                target = (case["site"] / row[0]).resolve()
                if not (target.is_relative_to(case["python"].parent.parent)):
                    raise AssertionError(
                        "verification failed: target.is_relative_to(case['python'].parent.parent)"
                    )
                content = p.read(target)
                row[1:] = [
                    "sha256="
                    + base64.urlsafe_b64encode(hashlib.sha256(content).digest())
                    .rstrip(b"=")
                    .decode(),
                    str(len(content)),
                ]
            encoded = io.StringIO()
            csv.writer(encoded, lineterminator="\n").writerows(rows)
            record_path.unlink()
            p.write(record_path, encoded.getvalue().encode())
            if not (p.read(record_path).decode() == encoded.getvalue()):
                raise AssertionError(
                    "verification failed: p.read(record_path).decode() == encoded.getvalue()"
                )
            case["self_consistent_install_record"] = True
    elif case_id in {"developer-path", "wrong-root", "unbound-wheel"}:
        manifest = case["manifest"]
        if case_id == "unbound-wheel":
            manifest["wheel_sha256"] = "0" * 64
        else:
            manifest["modules"][0]["expected_path"] = str(
                worker.candidate / "src/footman/__init__.py"
            )
        case["origin"].unlink()
        p.write(case["origin"], p.canonical(manifest))
    elif case_id == "shadow-support":
        (case["support"] / "footman").mkdir()
    elif case_id == "shadow-stdlib":
        p.write(
            case["support"] / "json.py",
            b'raise RuntimeError("copy-root stdlib shadow executed")\n',
        )
    elif case_id in {"cwd-shadow", "script-shadow"}:
        directory = (
            case["neutral"] if case_id == "cwd-shadow" else case["support"] / "tests"
        )
        p.write(
            directory / "footman.py",
            b'raise RuntimeError("implicit import shadow executed")\n',
        )
    elif case_id == "wrong-client-interpreter":
        case["argv"][0] = sys.executable
    elif case_id == "wrong-stub-interpreter":
        wrapper = Path(case["request"]["docker_wrapper_directory"]) / "docker"
        data = p.read(wrapper).replace(
            os.fsencode(case["python"]), os.fsencode(sys.executable)
        )
        wrapper.unlink()
        p.write(wrapper, data)
        wrapper.chmod(0o700)
    elif case_id == "poison-environment":
        # -E ignores these inputs during helper interpreter startup but leaves
        # them in os.environ for the real helper-to-service filter to remove.
        # -P alone would not protect startup from the invalid PYTHONHOME.
        case["argv"].insert(1, "-E")
        case["env"].update(
            PYTHONPATH=str(worker.candidate / "src"),
            PYTHONHOME="/nonexistent-import-home",
            PYTHONUSERBASE="/nonexistent-user-base",
            PYTEST_RELOCATED_CANARY="poison",
            FOOTMAN_RELOCATED_CANARY="poison",
        )
    else:
        case["env"]["FOOTMAN_RELOCATED_FAULT"] = case_id
    if case_id == "wrong-stub-interpreter":
        probe = build.command(
            [
                sys.executable,
                "-P",
                str(case["support"] / "tests/relocated_docker_child.py"),
                "--support-root",
                str(case["support"]),
                "--",
                "run",
            ],
            cwd=case["neutral"],
            env=case["env"],
            timeout_seconds=5,
        )
        if not (
            probe["status"] != 0
            and b"stub loaded checkout foundation" in probe["stderr"]
        ):
            raise AssertionError(
                "verification failed: probe['status'] != 0 and b'stub loaded checkout foundation' in probe['stderr']"
            )
        observation = {
            "accepted": False,
            "helper_attempted": False,
            "helper_stream_calls": 0,
            "helper_stream_outcomes": {},
            "helper_status": None,
            "result": None,
            "failure": "Docker wrapper interpreter binding mismatch",
            "stub_negative_probe": operation_record(probe),
        }
    elif case_id == "unbound-wheel":
        supplied = tmp / "unbound.whl"
        p.write(supplied, p.read(worker.bound.wheel) + b"swapped artifact")
        try:
            build.archive_expectations(supplied, worker.wheel_sha256, case["site"])
        except ValueError as error:
            observation = {
                "accepted": False,
                "helper_attempted": False,
                "helper_stream_calls": 0,
                "helper_stream_outcomes": {},
                "helper_status": None,
                "result": None,
                "failure": str(error),
            }
        else:
            raise AssertionError("unbound artifact was accepted")
    else:
        observation = run_helper(case)
    settled = reconcile(case)
    observation.update(
        settled,
        expected_interpreter=str(case["python"]),
        self_consistent_install_record=case.get(
            "self_consistent_install_record", False
        ),
        case_id=case_id,
        candidate_dist_pre=before,
        candidate_dist_post=dist_identity(worker.candidate),
        build_input_pre=identity,
        build_input_post=build.inventory(worker.bound.repository_root)[0],
        expected_origin=case["manifest"],
    )
    if observation["accepted"]:
        with DirectoryDescriptor.open_secure_path(
            p.OPS, tmp / "stub-records"
        ) as directory:
            origins = [
                p.load(tmp / "stub-records" / name)
                for name in directory.list_names()
                if name.endswith("-origin.json")
            ]
        if not (origins):
            raise AssertionError("verification failed: origins")
        if not (
            all(
                item["interpreter"] == str(case["python"])
                and item["support_root"] == str(case["support"])
                and Path(item["foundation"]).is_relative_to(
                    case["python"].parent.parent
                )
                for item in origins
            )
        ):
            raise AssertionError(
                "verification failed: all((item['interpreter'] == str(case['python']) and item['support_root'] == str(case['support']) and Path(item['foundation']).is_relative_to(case['python'].parent.parent) for item in origins))"
            )
        observation["result"]["stub_attribution"] = origins[0]
        for response in observation["result"]["calls"]:
            manifest = p.load(response["resultPath"])
            if not (manifest["command_argv"] == case["configuration"]["command_argv"]):
                raise AssertionError(
                    "verification failed: manifest['command_argv'] == case['configuration']['command_argv']"
                )
            if not (
                manifest["configured_image_id"] == case["configuration"]["image_id"]
            ):
                raise AssertionError(
                    "verification failed: manifest['configured_image_id'] == case['configuration']['image_id']"
                )
            expected_launch = [
                case["configuration"]["image_id"] if arg == "{image_id}" else arg
                for arg in case["configuration"]["container_start_argv"]
            ]
            if not (manifest["startup"]["argv"] == expected_launch):
                raise AssertionError(
                    "verification failed: manifest['startup']['argv'] == expected_launch"
                )
            if not (
                manifest["return_code"] == 0 and manifest["cleanup"]["snapshot_deleted"]
            ):
                raise AssertionError(
                    "verification failed: manifest['return_code'] == 0 and manifest['cleanup']['snapshot_deleted']"
                )
            if not (p.read(response["stdoutPath"]) == b"relocated stdout\x00\xff\n"):
                raise AssertionError(
                    "verification failed: p.read(response['stdoutPath']) == b'relocated stdout\\x00\\xff\\n'"
                )
            if not (p.read(response["stderrPath"]) == b"relocated stderr\x00\xfe\n"):
                raise AssertionError(
                    "verification failed: p.read(response['stderrPath']) == b'relocated stderr\\x00\\xfe\\n'"
                )
    completion_support = {
        name: hashlib.sha256(p.read(case["support"] / name)).hexdigest()
        for name in SUPPORT
    }
    if not (completion_support == case["support_inventory"]):
        raise AssertionError(
            "verification failed: completion_support == case['support_inventory']"
        )
    if not (
        hashlib.sha256(p.read(worker.candidate / "examples/footman.toml")).hexdigest()
        == case["annotated_example_sha256"]
    ):
        raise AssertionError(
            "verification failed: hashlib.sha256(p.read(worker.candidate / 'examples/footman.toml')).hexdigest() == case['annotated_example_sha256']"
        )
    observation["annotated_example_sha256"] = case["annotated_example_sha256"]
    observation["instantiated_configuration"] = case["configuration"]
    observation["support_inventory"] = case["support_inventory"]
    observation["completion_support_inventory"] = completion_support
    wrapper_bytes = p.read(Path(case["request"]["docker_wrapper_directory"]) / "docker")
    observation["docker_wrapper_bytes_base64"] = base64.b64encode(
        wrapper_bytes
    ).decode()
    observation["docker_wrapper_sha256"] = hashlib.sha256(wrapper_bytes).hexdigest()
    observation["original_docker_wrapper_sha256"] = case["docker_wrapper_sha256"]
    if case_id != "wrong-stub-interpreter":
        if not (observation["docker_wrapper_sha256"] == case["docker_wrapper_sha256"]):
            raise AssertionError(
                "verification failed: observation['docker_wrapper_sha256'] == case['docker_wrapper_sha256']"
            )
    worker.tests.append(observation)
    p.write(tmp / "orchestration-observation.json", p.canonical(observation))
    return observation


@contextmanager
def loss_collection(case, collect):
    """One helper owner, protected before any collector setup can fail."""
    thread = None
    owner = OwnedProcess.launch(
        case["argv"],
        deadline_seconds=70,
        grace_seconds=2,
        capture_limit=0,
        popen_kwargs={"cwd": str(case["neutral"]), "env": case["env"]},
    )
    try:
        thread = threading.Thread(target=lambda: collect(owner))
        thread.start()
        yield owner
    finally:
        failures = CleanupFailures(sys.exception())

        def shutdown():
            cleanup = owner.shutdown()
            if not cleanup.complete or cleanup.failures:
                # Retain this same owner/continuation in the diagnostic, never
                # retry shutdown or give an incomplete owner a fresh budget.
                raise AssertionError(
                    "helper cleanup incomplete or failed", owner, cleanup
                )

        def join():
            # start() can raise after actually starting the collector. Joining
            # only on a successfully returned start() would abandon that thread.
            if thread is not None:
                if thread.ident is None:
                    # An interrupted start may have handed off to the OS before
                    # publishing ident. Do not call this settled or discard its
                    # continuation; retain both objects with the primary error.
                    raise AssertionError(
                        "helper collector start unresolved", owner, thread
                    )
                thread.join(timeout=2)
                if thread.is_alive():
                    raise AssertionError(
                        "helper collector did not settle", owner, thread
                    )

        failures.run(shutdown, ordinary=(Exception,))
        failures.run(join, ordinary=(Exception,))
        selected = failures.complete()
        if selected is not None:
            raise selected


def exercise_loss(worker, tmp, stage):
    case = prepare_case(worker, tmp)
    target = (
        stage
        if stage
        not in {
            "interruption",
            "lifetime",
            "unknown-incarnation",
            "observer-failure",
            "surviving-incarnation",
            "unsettled-resource",
        }
        else "idle"
    )
    publication_stage = stage in {
        "before-journal",
        "before-provenance",
        "after-provenance",
    }
    if publication_stage:
        target = "after-acquire"
        case["env"]["FOOTMAN_BOOTSTRAP_TEST_GATE"] = stage
    if stage in {"accepted-work", "unsettled-resource"}:
        target = "accepted-work"
        case["env"]["FOOTMAN_STUB_DELAY"] = "1"
    if stage == "interruption":
        case["env"]["FOOTMAN_RELOCATED_FAULT"] = "interruption"
    case["env"]["FOOTMAN_RELOCATED_STAGE"] = target
    absence_before_shutdown = False
    helper_live_at_exit = False
    data, ends, result = {s: bytearray() for s in ProcessStream}, {}, {}
    observed_bytes = {s: 0 for s in ProcessStream}
    collection_errors = []

    def chunk(stream, payload):
        observed_bytes[stream] += len(payload)
        try:
            data[stream].extend(payload[: max(0, 65537 - len(data[stream]))])
        except BaseException as error:
            collection_errors.append(error)
            if not isinstance(error, Exception):
                raise

    def collect(owner):
        try:
            result["value"] = owner.stream(
                chunk,
                on_stream_end=lambda stream, outcome: ends.__setitem__(
                    stream.name, outcome.name
                ),
            )
        except BaseException as error:
            collection_errors.append(error)

    ready = tmp / (
        "bootstrap-gate-" + stage + ".json" if publication_stage else "stage-" + target
    )
    with loss_collection(case, collect) as owner:
        until = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.02)
        if not (ready.exists()):
            raise AssertionError({s.name: bytes(v) for s, v in data.items()})
        if stage == "lifetime":
            record = journal(case)
            if not (record is not None):
                raise AssertionError("verification failed: record is not None")
            while time.monotonic() < case["request"]["transport_deadline_monotonic"]:
                if incarnation(record) == "absent":
                    intent = p.load(tmp / "launch-intent.json")
                    helper_state = incarnation(
                        {
                            "pid": intent["helper_pid"],
                            "incarnation": intent["helper_incarnation"],
                        }
                    )
                    helper_live_at_exit = helper_state in {"R", "S", "D"}
                    if not (
                        time.monotonic()
                        < case["request"]["transport_deadline_monotonic"]
                    ):
                        raise AssertionError(
                            "verification failed: time.monotonic() < case['request']['transport_deadline_monotonic']"
                        )
                    if not (helper_live_at_exit):
                        raise AssertionError(
                            "helper exited before lifetime absence was observed"
                        )
                    absence_before_shutdown = True
                    break
                time.sleep(0.02)
            if not (absence_before_shutdown):
                raise AssertionError(
                    "original service lifetime failed with helper stdin still open"
                )
    if collection_errors:
        raise collection_errors[0]
    gate_identity = p.load(ready) if publication_stage else None
    settled = reconcile(
        case, no_launch=target == "before-launch", observed_incarnation=gate_identity
    )
    observation = {
        **settled,
        "stage": stage,
        "helper_cleanup_complete": owner.cleanup_status.complete,
        "helper_status": result["value"].returncode,
        "helper_stream_outcomes": ends,
        "helper_observed_bytes": {s.name: count for s, count in observed_bytes.items()},
        "helper_retained_bytes": {s.name: len(value) for s, value in data.items()},
        "accepted": bool(settled["resources_reconciled"]),
    }
    observation["absence_before_helper_shutdown"] = absence_before_shutdown
    observation["helper_live_at_lifetime_exit"] = helper_live_at_exit
    if stage == "before-journal":
        if not (not Path(case["request"]["journal_path"]).exists()):
            raise AssertionError(
                "verification failed: not Path(case['request']['journal_path']).exists()"
            )
        observation.update(
            accepted=False,
            retained_scratch=True,
            failure="controlled stop before required journal publication",
        )
    if publication_stage:
        observation["publication_gate_identity"] = gate_identity
    if stage == "interruption":
        retained_cleanup = p.load(tmp / "helper-service-cleanup.json")
        if not (retained_cleanup["complete"] and not retained_cleanup["failures"]):
            raise AssertionError(
                "verification failed: retained_cleanup['complete'] and (not retained_cleanup['failures'])"
            )
        observation["helper_owned_service_cleanup"] = retained_cleanup
    if stage in {
        "unknown-incarnation",
        "observer-failure",
        "surviving-incarnation",
        "unsettled-resource",
    }:
        actual_record = journal(case)
        if not (actual_record is not None):
            raise AssertionError("verification failed: actual_record is not None")
        try:
            if stage == "unknown-incarnation":
                with patch(
                    __name__ + ".journal",
                    return_value={"pid": actual_record["pid"], "incarnation": None},
                ):
                    reconcile(case)
            elif stage == "surviving-incarnation":
                # A bounded substage cap, never an extension of the inherited
                # service/settlement budget. The actual fixture is already absent.
                probe = {
                    **case,
                    "request": {
                        **case["request"],
                        "transport_deadline_monotonic": min(
                            case["request"]["transport_deadline_monotonic"],
                            time.monotonic() + 0.02,
                        ),
                    },
                }
                with patch(
                    __name__ + ".incarnation", return_value="R"
                ) as observed_live:
                    unresolved = reconcile(probe)
                if not (observed_live.called):
                    raise AssertionError("verification failed: observed_live.called")
                observation["injected_observation"] = unresolved
                if not (not unresolved["resources_reconciled"]):
                    raise AssertionError(
                        "verification failed: not unresolved['resources_reconciled']"
                    )
                raise RuntimeError(unresolved["failure"])
            elif stage == "observer-failure":
                with patch(
                    __name__ + ".incarnation",
                    side_effect=PermissionError("injected read-only observer failure"),
                ):
                    reconcile(case)
            else:
                original_load = p.load

                def failed_resource(path):
                    value = original_load(path)
                    return (
                        {**value, "status": 1}
                        if str(path).endswith("-call.json")
                        else value
                    )

                with patch.object(p, "load", failed_resource):
                    reconcile(case)
        except (ValueError, OSError, RuntimeError, AssertionError) as error:
            observation.update(
                accepted=False,
                retained_scratch=True,
                failure=type(error).__name__ + ": " + str(error),
            )
        else:
            raise AssertionError("unresolved identity/observer/resource was accepted")
        if not (reconcile(case)["resources_reconciled"]):
            raise AssertionError(
                "verification failed: reconcile(case)['resources_reconciled']"
            )
    if not (
        hashlib.sha256(
            p.read(Path(case["request"]["docker_wrapper_directory"]) / "docker")
        ).hexdigest()
        == case["docker_wrapper_sha256"]
    ):
        raise AssertionError(
            "verification failed: hashlib.sha256(p.read(Path(case['request']['docker_wrapper_directory']) / 'docker')).hexdigest() == case['docker_wrapper_sha256']"
        )
    observation["docker_wrapper_bytes_base64"] = case["docker_wrapper_bytes_base64"]
    observation["docker_wrapper_sha256"] = case["docker_wrapper_sha256"]
    worker.tests.append(settled)
    p.write(tmp / "loss-observation.json", p.canonical(observation))
    return observation


def exercise_request_rejection(root, tmp, fault):
    client = importlib.import_module("tests.mcp_relocated_client")
    # Independent invalid-input contracts must reject before invoking any service
    # factory; constructing an installed environment is not required for rejection.
    request = tmp / "request.json"
    support = tmp / "copied"
    support.mkdir()
    for name in ("docker-tools", "neutral"):
        (tmp / name).mkdir()
    for name in ("origin.json", "operator.toml", "installed-script"):
        p.write(tmp / name, b"{}")
    now = time.monotonic()
    binding = {
        "schema_version": 1,
        "scratch_root": str(tmp),
        "journal_path": str(tmp / "journal.jsonl"),
        "instance_token": uuid.uuid4().hex,
        "service_deadline_monotonic": now + 60,
        "settlement_allowance_seconds": 5,
    }
    value = {
        "schema_version": 1,
        "scratch_root": str(tmp),
        "support_root": str(support),
        "expected_origin_manifest": str(tmp / "origin.json"),
        "provenance_evidence_path": str(tmp / "evidence.json"),
        "config_path": str(tmp / "operator.toml"),
        "installed_server_script": str(tmp / "installed-script"),
        "docker_wrapper_directory": str(tmp / "docker-tools"),
        "neutral_service_cwd": str(tmp / "neutral"),
        "lifecycle_input": str(tmp / "lifecycle.json"),
        "journal_path": binding["journal_path"],
        "lifecycle_binding": dict(binding),
        "calls": [],
        "transport_deadline_monotonic": now + 65,
    }
    if fault == "missing":
        value.pop("config_path")
    elif fault == "unknown":
        value["unknown"] = True
    elif fault == "schema-bool":
        value["schema_version"] = True
    elif fault == "budget-bool":
        value["transport_deadline_monotonic"] = True
    elif fault == "budget-nonfinite":
        value["transport_deadline_monotonic"] = float("inf")
    elif fault == "expired":
        binding["service_deadline_monotonic"] = now - 1
    elif fault == "extended":
        binding["service_deadline_monotonic"] += 1
    elif fault == "token":
        binding["instance_token"] = "0" * 32
    elif fault == "root":
        binding["scratch_root"] = str(tmp.parent)
    elif fault == "path-escape":
        value["config_path"] = str(tmp.parent / "outside")
    elif fault == "symlink":
        alias = tmp / "linked-config"
        alias.symlink_to(tmp / "operator.toml")
        value["config_path"] = str(alias)
    elif fault == "alias":
        value["provenance_evidence_path"] = value["config_path"]
    elif fault == "internal-alias":
        value["journal_path"] = str(tmp / "launch-intent.json")
        binding["journal_path"] = value["journal_path"]
        value["lifecycle_binding"]["journal_path"] = value["journal_path"]
    elif fault == "directory-input":
        invalid = tmp / "invalid-config-directory"
        invalid.mkdir()
        value["config_path"] = str(invalid)
    elif fault in {
        "wrong-position",
        "duplicate-option",
        "production-forward",
        "wrong-leg",
    }:
        value["argv"] = {
            "wrong-position": ["--lifecycle", "--expected-origin"],
            "duplicate-option": ["--lifecycle", "input", "--lifecycle", "input"],
            "production-forward": ["--", "--lifecycle", "input"],
            "wrong-leg": ["--module", "footman"],
        }[fault]
    p.write(tmp / "lifecycle.json", p.canonical(binding))
    if fault == "missing-lifecycle":
        (tmp / "lifecycle.json").unlink()
    raw = p.canonical(value)
    if fault == "duplicate-key":
        raw = raw[:-1] + b',"schema_version":1}'
    p.write(request, raw)
    from unittest.mock import Mock

    factory = Mock(side_effect=AssertionError("invalid request acquired service"))
    from tests import mcp_transport

    with (
        patch.object(OwnedProcess, "launch", factory),
        patch.object(mcp_transport, "stdio_transport", factory),
        patch.object(client, "load_support", return_value=p),
        patch.object(
            sys,
            "argv",
            [
                "mcp_relocated_client.py",
                "--support-root",
                str(support),
                "--request",
                str(request),
            ],
        ),
    ):
        try:
            client.main()
        except ValueError, TypeError, OSError:
            pass
        else:
            raise AssertionError("invalid relocated request accepted")
    factory.assert_not_called()
    return {
        "rejected": True,
        "service_factory_calls": factory.call_count,
        "service_acquired": False,
    }


def exercise_resource_fault(root, tmp, branch, fault):
    mapped = {
        "build-input": "build-inventory",
        "build-child": "read",
        "archive": "archive",
        "copy": "copy",
        "request": "input",
        "lifecycle-input": "input",
        "journal-observer": "journal",
        "incarnation-observer": "read",
        "evidence-writer": "evidence",
        "run-observer": "read",
    }[branch]
    if branch == "build-child":
        _process_matrix(tmp, fault)
    elif branch == "incarnation-observer":
        record = {
            "pid": os.getpid(),
            "incarnation": p.read(Path(f"/proc/{os.getpid()}/stat"))
            .rsplit(b")", 1)[1]
            .split()[19]
            .decode(),
        }
        _resource_matrix(
            tmp,
            "read",
            fault,
            read_operation=lambda source: incarnation(record),
            branch_root_override=Path(f"/proc/{os.getpid()}"),
        )
    elif branch == "run-observer":
        result_root = tmp / "results"
        (result_root / "one").mkdir(parents=True)
        p.write(
            result_root / "one/result.json",
            p.canonical(
                {
                    "cleanup": {
                        "confirmed_absent": True,
                        "snapshot_deleted": True,
                        "unresolved_resources": False,
                    },
                    "snapshot": {"path": str(tmp / "snapshots/fixture")},
                }
            ),
        )
        _resource_matrix(
            tmp,
            "read",
            fault,
            read_operation=lambda source: run_resources(tmp),
            branch_root_override=result_root,
        )
    elif branch == "request":
        _resource_matrix(tmp, "read", fault)
    else:
        _resource_matrix(tmp, mapped, fault)
    return {
        "authority_preserved": True,
        "sole_release_authority": True,
        "independent_releases_attempted": True,
        "repeated_release_count": 0,
    }


def exercise_worker_distribution(root, tmp):
    probes = [tmp / "test_worker_probe_a.py", tmp / "test_worker_probe_b.py"]
    output = tmp / "worker-results"
    output.mkdir()
    source = """import pytest
from pathlib import Path
from tests import acceptance_portability_support as support
from tests import mcp_provenance as p

pytest_plugins = ["tests.green_skip_policy"]

def pytest_collection_modifyitems(items):
    # Preserve all eight tests, but interleave modules. With the pinned
    # worksteal scheduler each initial worker queue starts A, B, A, B.
    items.sort(key=lambda item: (item.callspec.params["case"], item.path.name))

@pytest.fixture(scope="session")
def provider(tmp_path_factory, worker_id):
    with support.worker_scope(Path(CANDIDATE), lambda: tmp_path_factory.mktemp("phase10-worker-"+worker_id), worker_id) as obtain:
        yield obtain
        worker = obtain()
    record = p.load(worker.root / "worker-completion.json")
    record["modules_seen"] = sorted({item["probe_module"] for item in worker.tests})
    p.write(Path(OUTPUT) / (worker_id + ".json"), p.canonical(record))

@pytest.fixture(scope="module")
def prepared(provider):
    return provider()
"""
    source = source.replace("CANDIDATE", repr(str(root))).replace(
        "OUTPUT", repr(str(output))
    )
    p.write(tmp / "conftest.py", source.encode())
    probe_source = """import pytest
from tests import acceptance_portability_support as support

@pytest.mark.parametrize("case", range(4))
def test_reuse(prepared, case, request):
    prepared.reuse_count += 1
    prepared.tests.append({"resources_reconciled": True, "probe_module": request.module.__name__})
    assert support.build.inventory(prepared.bound.repository_root)[0] == prepared.bound.build_input_identity
"""
    for probe in probes:
        p.write(probe, probe_source.encode())
    command = [
        str(Path(sys.executable).absolute()),
        "-m",
        "pytest",
        "-c",
        str(root / "pyproject.toml"),
        *(str(probe) for probe in probes),
        "-n",
        "2",
        "--dist",
        "worksteal",
        "--tb=short",
        "-q",
    ]
    result = build.command(
        command, cwd=root, env=build.clean_environment(), timeout_seconds=180
    )
    if not (result["status"] == 0):
        raise AssertionError(result)
    with DirectoryDescriptor.open_secure_path(p.OPS, output) as directory:
        workers = [p.load(output / name) for name in directory.list_names()]
    p.write(
        tmp / "worker-distribution.json",
        p.canonical({"command": operation_record(result), "workers": workers}),
    )
    return {"status": result["status"], "workers": workers}


def _resource_matrix(
    tmp, branch, fault, read_operation=None, branch_root_override=None
):
    import hashlib
    import io
    import zipfile
    from unittest.mock import patch

    from footman.filesystem.descriptors import OwnedDescriptor
    from tests import acceptance_build_support as wheel
    from tests import mcp_lifecycle as lifecycle

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
    branch_root = tmp if branch_root_override is None else branch_root_override
    if branch == "build-inventory":
        branch_root = primitives.prepare_inputs(
            Path(__file__).resolve().parents[1], tmp / "build-inputs"
        )
    elif branch == "package-enumeration":
        branch_root = Path(__file__).resolve().parents[1] / "src/footman"
    elif branch == "dist-inventory":
        branch_root = (
            tmp if branch_root_override is None else branch_root_override / "dist"
        )
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
        primitives.rewrite(source, data.getvalue())
    if branch in {"input", "journal"}:
        primitives.rewrite(source, p.canonical(primitives.lifecycle_input(tmp)))
    manifest = parser = argv = None
    if branch == "evidence":
        setup = tmp / "attested-child"
        setup.mkdir()
        w, parser = primitives.actual_attestation(setup)
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
                p.read(source) if read_operation is None else read_operation(source)
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
                primitives.check(
                    error is caught, "repeat journal cleanup changed retained failure"
                )
        for owner in owners:
            owner.close()
    if inject_operation:
        primitives.check(
            caught is operation_error, "operation/interruption authority lost to close"
        )
    elif inject_close:
        primitives.check(caught is release_error, "sole release failure lost")
    else:
        primitives.check(caught is None, "successful/repeat resource operation failed")

    def live(owner):
        try:
            _ = owner.fd
        except Exception:
            return False
        return True

    primitives.check(
        not any(live(owner) for owner in owners),
        "independent resource retained after settled operation",
    )
    primitives.check(len(attempts) == len(set(map(id, attempts))), "release retried")
    primitives.check(
        any(owner.label == label for owner in attempts),
        "matrix never exercised the owned branch",
    )
    if fault == "independent":
        primitives.check(
            any(
                error is parent_release_error
                for error in getattr(caught, "_transaction_secondary", ())
            ),
            "independent parent release diagnostic lost",
        )
        primitives.check(
            any(owner.label == str(branch_root) for owner in attempts),
            "parent release not attempted independently",
        )


def _process_matrix(tmp, fault, action=None, after_successes=0):
    from unittest.mock import patch

    from footman.filesystem.streams import OwnedStream
    from tests import acceptance_build_support as wheel

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
                    env=build.clean_environment(),
                    timeout_seconds=3,
                )
                primitives.check(
                    result["status"] == 0 and result["stdout"] == b"helper\n",
                    "successful command output/status drift",
                )
            else:
                action()
        except BaseException as error:
            caught = error
        for owner in acquired:
            primitives.check(
                owner.cleanup_status.complete, "build/install process owner not settled"
            )
            before = len(releases)
            owner.shutdown()
            owner.wait_cleanup(0.1)
            primitives.check(
                len(releases) == before, "build/install helper close retried"
            )
    primitives.check(acquired, "process acquisition branch not exercised")
    primitives.check(
        len(releases) == len(set(map(id, releases))),
        "one stream had multiple release attempts",
    )
    primitives.check(
        {"stdout", "stderr"} <= {owner.label for owner in releases},
        "independent sibling release omitted",
    )
    ledger = acquired[after_successes].cleanup_status.failures
    if fault == "repeat":
        primitives.check(caught is None, "repeat cleanup changed success")
    elif fault == "interruption":
        primitives.check(
            caught is operation, "helper interruption authority/identity changed"
        )
    else:
        primitives.check(
            caught is not None, "helper failed release/operation reported success"
        )
    if fault in {"interruption", "operation-close"}:
        primitives.check(
            any(item.error is operation for item in ledger),
            "original helper operation absent from retained ledger",
        )
    if fault != "repeat":
        primitives.check(
            any(item.error is failures["stdout"] for item in ledger),
            "sole helper close lost",
        )
    if fault == "independent":
        primitives.check(
            any(item.error is failures["stderr"] for item in ledger),
            "independent helper close lost",
        )
