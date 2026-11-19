"""Task-11.8 owned attribution branches and actual by-path prepared-leg calls."""

import hashlib
import io
import os
import sys
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import _footman_acceptance as build
from tests import host_acceptance_contract_support as h
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel


def _implementation(name):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_failure_evidence_" + name, h.ROOT / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status", [0, 17])
@pytest.mark.parametrize("post_fault", ["none", "source", "readme", "interruption"])
def test_public_build_retains_pre_and_post_artifacts_on_every_outcome(
    admission, tmp_path, status, post_fault
):
    from dataclasses import replace

    from tests.wheel_attribution_cases import CASES

    candidate = build.Tooling(h.ROOT).dist_inventory()
    root, _ = h.prepared_root(tmp_path, admission=admission.shared_pool)
    (root / "dist").mkdir()
    p.write(root / "dist/unrelated", b"unowned and unchanged")
    mutation = {"source": "src/footman/__init__.py", "readme": "README.md"}.get(
        post_fault
    )
    case = replace(
        CASES[0],
        build_status=status,
        mutation=mutation,
        stdout_bytes=b"raw\0\xff" * 25000,
    )
    executable, call, attempts = h.scripted_uv(tmp_path, case)
    if post_fault == "interruption":
        h.install_readme_guard(root, "interruption", after=call)
    before = build.Tooling(root).dist_inventory()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = evidence / "build.json"
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(manifest)],
        admission=admission.shared_pool,
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    record = h.build_operation(manifest, root)
    assert record["pre_existing_dist"] == before
    assert record["predicted_destinations"] == [
        str(root / "dist/footman-0.1.0.tar.gz"),
        str(root / "dist/footman-0.1.0-py3-none-any.whl"),
    ]
    assert record["produced_dist"] == build.Tooling(root).dist_inventory()
    assert len(record["produced_dist"]) == 3
    assert record["status"] == status and record["attempted"]
    assert (
        record["stdout_bytes"] == case.stdout_bytes
        and record["stderr_bytes"] == case.stderr_bytes
    )
    assert len(h.root_names(attempts)) == 1
    assert p.read(root / "dist/unrelated") == b"unowned and unchanged"
    assert manifest.exists() == (status == 0 and post_fault == "none")
    if post_fault == "interruption":
        assert result["status"] in {-2, 130}
    else:
        assert result["status"] == (status if status else int(post_fault != "none"))
    if post_fault == "none":
        assert record["post_check_error"] is None
    else:
        assert record["post_check_error"] is not None
    if status and post_fault != "interruption":
        assert record["error"] == "ValueError: uv build failed"
    assert build.Tooling(h.ROOT).dist_inventory() == candidate


@pytest.mark.parametrize("status", [0, 17])
@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_post_build_dist_inventory_failure_is_independent_and_once_owned(
    admission, tmp_path, monkeypatch, status, fault
):
    from dataclasses import replace

    from tests.wheel_attribution_cases import CASES

    module = _implementation("build_acceptance_wheel")
    root, _ = h.prepared_root(tmp_path, admission=admission.shared_pool)
    executable, _, _ = h.scripted_uv(tmp_path, replace(CASES[0], build_status=status))
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = evidence / "build.json"
    tool = build.Tooling(root)
    original_ops, original_command = tool.ops, tool.command
    original_names, original_child = (
        build.DirectoryDescriptor.list_names,
        build.DirectoryDescriptor.open_directory,
    )
    active, owners, releases, parents = [False], [], [], []
    in_dist = [False]
    primary = (
        KeyboardInterrupt("post inventory interrupted")
        if fault == "interruption-close"
        else OSError("post inventory failed")
    )
    close = OSError("post dist close failed")

    class Ops:
        def __getattr__(self, name):
            return getattr(original_ops, name)

        def close(self, fd):
            path = os.readlink(f"/proc/self/fd/{fd}")
            original_ops.close(fd)
            if active[0] and path == str(root / "dist"):
                releases.append(fd)
                raise close
            if in_dist[0] and path == str(root):
                parents.append(fd)
                in_dist[0] = False

    def command(argv, **kwargs):
        try:
            return original_command(argv, **kwargs)
        finally:
            if argv == ["uv", "build"]:
                active[0] = True

    def child(owner, name, **kwargs):
        result = original_child(owner, name, **kwargs)
        if active[0] and name == "dist":
            in_dist[0] = True
            owners.append(result)
        return result

    def names(owner):
        if (
            active[0]
            and os.readlink(f"/proc/self/fd/{owner.fd}") == str(root / "dist")
            and fault != "close"
        ):
            raise primary
        return original_names(owner)

    tool.ops = Ops()
    monkeypatch.setattr(tool, "command", command)
    monkeypatch.setattr(build.DirectoryDescriptor, "open_directory", child)
    monkeypatch.setattr(build.DirectoryDescriptor, "list_names", names)
    if fault == "interruption-close":
        with pytest.raises(KeyboardInterrupt) as caught:
            module.build(tool, manifest, root / "scripts/build-acceptance-wheel")
        assert caught.value is primary
    else:
        assert module.build(
            tool, manifest, root / "scripts/build-acceptance-wheel"
        ) == (status or 1)
    record = h.build_operation(manifest, root)
    assert record["pre_existing_dist"] == []
    assert len(record["predicted_destinations"]) == 2
    assert record["status"] == status
    assert record["produced_dist"] is None
    assert record["post_check_error"]["check"] == "dist"
    assert record["source_identity"] and record["build_input_identity"]
    if fault != "close":
        assert close in getattr(primary, "_transaction_secondary", [])
    assert len(owners) == len(releases) == len(parents) == 1
    owners[0].close()
    assert len(releases) == 1
    assert not manifest.exists()


@pytest.mark.parametrize(
    ("failure", "publication"),
    [
        (failure, publication)
        for failure in ("which", "load", "binding", "archive", "git", "docker")
        for publication in ("none", "failure", "interruption")
    ]
    + [
        ("docker", "retain-" + stream + "-" + fault)
        for stream in ("stdout", "stderr")
        for fault in ("failure", "interruption")
    ],
)
def test_runner_preflight_failure_finalizes_outputs_status_and_report(
    tmp_path, monkeypatch, failure, publication
):
    module = _implementation("run_host_acceptance")
    root, directory, evidence, tools = (
        tmp_path / name for name in ("root", "workspace", "evidence", "tools")
    )
    for path in (root, directory, evidence, tools):
        path.mkdir()
    out, err = b"out\0\xff" * 25000, b"err\0\x80" * 25000
    for name in ("git", "docker"):
        status = (
            17
            if (name == "git" and failure in {"git", "binding"})
            or (name == "docker" and failure == "docker")
            else 0
        )
        script = f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({out!r})\nsys.stderr.buffer.write({err!r})\nraise SystemExit({status})\n"
        p.write(tools / name, script.encode())
        (tools / name).chmod(0o700)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    which = module.shutil.which
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: None if failure == "which" and name == "docker" else which(name),
    )
    tool = build.Tooling(root)
    deadline = tool.deadline
    manifest = {
        "schema_version": 1,
        "source_identity": {"sha256": "a" * 64},
        "build_input_identity": {"sha256": "b" * 64},
        "revision": "c" * 40,
        "wheel_sha256": "d" * 64,
    }
    primary = (
        KeyboardInterrupt("binding interrupted")
        if failure == "binding" and publication == "none"
        else ValueError("preflight " + failure)
    )
    secondary = (
        KeyboardInterrupt("report interrupted")
        if publication.endswith("interruption")
        else OSError("report publication failed")
    )
    loaded, comparisons, writes = [], [], []
    real_write = tool.write

    def load(path):
        loaded.append(path)
        if failure == "load":
            raise primary
        return manifest

    def compare(*args):
        comparisons.append(True)
        if failure == "binding":
            if len(comparisons) == 1:
                tool.command(["git", "rev-parse", "HEAD"])
            raise primary

    def archive(*args):
        if failure == "archive":
            raise primary

    def write(path, data):
        writes.append(path)
        if (
            publication.startswith("retain-")
            and path.name == "command-0-" + publication.split("-")[1] + ".txt"
        ):
            raise secondary
        real_write(path, data)
        if path == evidence / "report.json" and publication in {
            "failure",
            "interruption",
        }:
            raise secondary

    monkeypatch.setattr(module, "checked_root", lambda *args: root)
    monkeypatch.setattr(module, "Tooling", lambda *args, **kwargs: tool)
    monkeypatch.setattr(tool, "load", load)
    monkeypatch.setattr(tool, "write", write)
    monkeypatch.setattr(module, "compare_binding", compare)
    monkeypatch.setattr(module, "_archive_modules", archive)
    prepare = Mock(side_effect=AssertionError("preflight must not prepare/install"))
    monkeypatch.setattr(module._Workspace, "prepare", prepare)
    artifact = tmp_path / "wheel.whl"
    p.write(
        artifact,
        b"inert: archive authority is explicitly doubled in this preflight control",
    )
    leg = SimpleNamespace(
        profile="normal",
        image="d9e853e87e55",
        workspace=directory,
        evidence=evidence,
        suite_paths=(directory / "host_tests",),
        case_id=None,
        wheel=artifact,
        source_identity=tmp_path / "manifest.json",
    )
    with pytest.raises(BaseException) as caught:
        module._execute_prepared_host_leg(leg)
    assert writes.count(evidence / "report.json") == 1
    report = p.load(evidence / "report.json")
    assert report["success"] is False and report["pytest_status"] is None
    assert (
        report["cleanup_complete"]
        and not report["inventory"]
        and not report["generated"]
    )
    assert report["counts"]["collected"] == 0 and report["warnings"]
    assert report["wheel_path"] == str(artifact)
    assert report["build_manifest_path"] == str(leg.source_identity)
    for key in ("source_identity", "build_input_identity", "revision", "wheel_sha256"):
        assert report[key] == (
            None if failure in {"which", "load", "binding"} else manifest[key]
        )
    if (
        publication == "failure"
        or publication.startswith("retain-")
        and publication.endswith("failure")
    ):
        assert secondary in getattr(caught.value, "_transaction_secondary", [])
    assert (
        len(report["commands"])
        == len(tool.commands)
        == (2 if failure == "docker" else 1 if failure in {"git", "binding"} else 0)
    )
    for observed, original in zip(report["commands"], tool.commands, strict=True):
        assert observed["argv"] == original["argv"]
        assert observed["status"] == original["status"]
        for stream, expected in (("stdout", out), ("stderr", err)):
            path = evidence / (
                "command-"
                + str(report["commands"].index(observed))
                + "-"
                + stream
                + ".txt"
            )
            assert str(path) == observed[stream + "_path"]
            assert writes.count(path) == 1
            if (
                publication.startswith("retain-" + stream)
                and path.name == "command-0-" + stream + ".txt"
            ):
                assert not path.exists()
            else:
                assert p.read(path) == expected == original[stream]
    if tool.commands:
        assert tool.commands[-1]["status"] == 17
    if publication.endswith("interruption"):
        assert caught.value is secondary
    elif failure in {"load", "binding", "archive"}:
        assert caught.value is primary
    else:
        assert type(caught.value) is ValueError and "prerequisite" in str(caught.value)
    prepare.assert_not_called()
    assert tool.deadline == deadline
    for owner in tool.owners:
        before = owner.cleanup_status
        assert before.complete and not before.failures
        assert owner.shutdown() == before


@pytest.mark.parametrize("status", [0, 17])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_build_stream_failure_still_publishes_operation_once(
    admission, tmp_path, monkeypatch, status, stream, interrupted
):
    from dataclasses import replace

    from tests.wheel_attribution_cases import CASES

    module = _implementation("build_acceptance_wheel")
    root, _ = h.prepared_root(tmp_path, admission=admission.shared_pool)
    executable, _, _ = h.scripted_uv(tmp_path, replace(CASES[0], build_status=status))
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = evidence / "build.json"
    tool = build.Tooling(root)
    primary, secondary = (
        ValueError("uv build failed"),
        KeyboardInterrupt("stream interrupted")
        if interrupted
        else OSError("stream write failed"),
    )
    original_write, original_require = tool.write, module.require
    writes = []

    def require(condition, message):
        if not condition and message == "uv build failed":
            raise primary
        original_require(condition, message)

    def write(path, data):
        writes.append(path.name)
        if path.name == "build-build-" + stream + ".txt":
            raise secondary
        original_write(path, data)

    monkeypatch.setattr(module, "require", require)
    monkeypatch.setattr(tool, "write", write)
    if interrupted:
        with pytest.raises(KeyboardInterrupt) as caught:
            module.build(tool, manifest, root / "scripts/build-acceptance-wheel")
        assert caught.value is secondary
    else:
        assert module.build(
            tool, manifest, root / "scripts/build-acceptance-wheel"
        ) == (status or 1)
        if status:
            assert secondary in getattr(primary, "_transaction_secondary", [])
    assert writes == [
        "build-build-stdout.txt",
        "build-build-stderr.txt",
        "build-operation.json",
    ]
    record = p.load(evidence / "build-operation.json")
    assert record["status"] == status
    assert record["error"] == ("ValueError: uv build failed" if status else None)
    for name in ("stdout", "stderr"):
        path = evidence / ("build-build-" + name + ".txt")
        assert record[name + "_path"] == str(path)
        if name != stream:
            assert p.read(path) == tool.commands[-1][name]
        else:
            assert not path.exists()
    assert not manifest.exists()


def test_host_and_mcp_use_one_nonvacuous_admission_path(_wheel_build_slot_path):
    assert wheel.BUILD_SLOT_PATH == _wheel_build_slot_path
    assert wheel.BUILD_SLOT_PATH.name == "mcp-build.lock"


def test_prepared_root_and_call_admission_charge_original_envelope(
    tmp_path, monkeypatch
):
    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock, module_clock
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    events, contexts, completed = [], [], []
    clock = ManualClock(origin=10)
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = RecordingDescriptors(namespace)

    class System(RecordingSystem):
        def monotonic(self):
            return clock.monotonic()

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if acquired:
                clock.advance(3, label="preparation admission spends original budget")
            return acquired

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            # Observe the supported A argument without replacing its policy/owners.
            events.append(deadline)
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(directory=namespace, capacity=1, system=System(descriptors=ops))
    original_command = wheel.command

    def preparation_command(argv, **kwargs):
        assert any(token[2] == "slot" for token in ops.live.values())
        assert clock.monotonic() == 13 and kwargs["timeout_seconds"] == 20
        value = original_command(argv, **kwargs)
        completed.append(value)
        return value

    with module_clock(h, clock), monkeypatch.context() as patch:
        patch.setattr(wheel, "command", preparation_command)
        root, revision = h.prepared_root(tmp_path, admission=pool)
    assert root == tmp_path / "prepared" and len(revision) == 40
    assert not (root / ".git/index").exists()
    assert len(completed) == 7 and all(value["status"] == 0 for value in completed)
    assert events == [130] and clock.elapsed() == 3
    assert_released(ops)
    before = list(ops.closed)
    contexts[0].__exit__(None, None, None)
    contexts[0].__exit__(None, None, None)
    assert ops.closed == before

    def command(*args, **kwargs):
        assert any(token[2] == "slot" for token in ops.live.values())
        assert kwargs["timeout_seconds"] == 17
        return "collected"

    with module_clock(h, clock), monkeypatch.context() as patch:
        patch.setattr(wheel, "command", command)
        assert (
            h.prepared_command(
                ["unused"], admission=pool, cwd=tmp_path, env={}, deadline=33
            )
            == "collected"
        )
    assert events == [130, 33] and clock.monotonic() == 16
    assert_released(ops)
    before = list(ops.closed)
    contexts[1].__exit__(None, None, None)
    contexts[1].__exit__(None, None, None)
    assert ops.closed == before


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_wrapper_admission_release_preserves_primary_and_refuses_publication(
    tmp_path, monkeypatch, fault
):
    from tests.admission_support import AdmissionPool
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    (tmp_path / "scripts").mkdir()
    wrapper = tmp_path / "scripts/run-host-acceptance"
    p.write(wrapper, b"#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o700)
    namespace = tmp_path / "pool"
    namespace.mkdir()
    primary = (
        KeyboardInterrupt("wrapper interrupted")
        if fault == "interruption-close"
        else ValueError("wrapper operation failed")
    )
    secondary, parent_release = (
        OSError("admission close failed"),
        OSError("independent directory close failed"),
    )
    ops = RecordingDescriptors(namespace, release=secondary)
    ops.parent_release = parent_release
    contexts, work = [], []

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(
        directory=namespace, capacity=1, system=RecordingSystem(descriptors=ops)
    )

    def command(*args, **kwargs):
        assert any(token[2] == "slot" for token in ops.live.values())
        work.append(True)
        if fault != "close":
            raise primary
        return {"status": 0, "stdout": b"out", "stderr": b"err"}

    publish = Mock()
    monkeypatch.setattr(wheel, "command", command)
    monkeypatch.setattr(p, "write", publish)
    with pytest.raises(BaseException) as caught:
        h.invoke(
            tmp_path,
            "run-host-acceptance",
            [],
            admission=pool,
            cwd=tmp_path,
            evidence=tmp_path,
        )
    assert caught.value is (secondary if fault == "close" else primary)
    assert len(contexts) == 1 and work == [True]
    assert sum(token[2] == "slot" for token in ops.closed) == 1
    assert sum(token[2] == "namespace" for token in ops.closed) == 1
    cleanup = getattr(caught.value, "_transaction_secondary", ())
    assert any(error is parent_release for error in cleanup)
    if fault != "close":
        assert any(error is secondary for error in cleanup)
    assert_released(ops)
    before = list(ops.closed)
    contexts[0].__exit__(None, None, None)
    contexts[0].__exit__(None, None, None)
    assert ops.closed == before
    ops.release = ops.parent_release = None
    with pool.acquire(deadline=time.monotonic() + 5):
        pass
    assert_released(ops)
    publish.assert_not_called()


def test_attribution_environment_cannot_refresh_or_redirect_the_developer_index(
    monkeypatch,
):
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("GIT_DIR", "/wrong/repository")
    monkeypatch.setenv("GIT_INDEX_FILE", "/wrong/index")
    monkeypatch.setenv("PYTHONPATH", "/wrong/imports")
    environment = build.clean_environment()
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_DIR" not in environment and "GIT_INDEX_FILE" not in environment
    assert "PYTHONPATH" not in environment


@pytest.mark.parametrize("operation", ["read", "write", "inventory"])
@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_attribution_file_owner_failure_and_repeat(
    tmp_path, monkeypatch, operation, fault
):
    target = tmp_path / "README.md"
    if operation != "write":
        p.write(target, b"fixture input")
    tool = build.Tooling(tmp_path)
    original_owner = build.OwnedDescriptor
    original_read, original_write = build.os.read, build.os.write
    owners, released, selected_fds = [], [], set()
    primary = (
        KeyboardInterrupt("input interrupted")
        if fault == "interruption-close"
        else OSError("input operation failed")
    )
    secondary = OSError("input close failed")

    class Ops:
        def close(self, fd):
            released.append(fd)
            tool.ops.close(fd)
            raise secondary

    def descriptor(ops, fd, *, label):
        owner = original_owner(Ops(), fd, label=label)
        owners.append(owner)
        selected_fds.add(fd)
        return owner

    def read(fd, size):
        if fd in selected_fds and fault != "close":
            raise primary
        return original_read(fd, size)

    def write(fd, data):
        if fd in selected_fds and fault != "close":
            raise primary
        return original_write(fd, data)

    monkeypatch.setattr(build, "OwnedDescriptor", descriptor)
    monkeypatch.setattr(build.os, "read", read)
    monkeypatch.setattr(build.os, "write", write)
    with pytest.raises(BaseException) as caught:
        if operation == "read":
            tool.read(target)
        elif operation == "write":
            tool.write(target, b"generated input")
        else:
            tool.inventory(("README.md",))
    assert caught.value is (secondary if fault == "close" else primary)
    assert len(owners) == len(released) == 1
    for owner in owners:
        owner.close()
    assert len(released) == 1
    assert not tool.commands


@pytest.mark.parametrize(
    "primary", [None, OSError("operation"), KeyboardInterrupt("interrupted")]
)
def test_independent_stream_retention_preserves_authority(
    tmp_path, monkeypatch, primary
):
    tool = build.Tooling(tmp_path)
    original = tool.write
    failed = OSError("stdout retention failed") if primary is None else primary
    calls = []

    def write(path, data):
        calls.append(path.name)
        if path.name == "stdout.txt":
            raise failed
        return original(path, data)

    monkeypatch.setattr(tool, "write", write)
    with pytest.raises(BaseException) as caught:
        tool.retain_command(
            {"stdout": b"out", "stderr": b"err", "status": 17}, tmp_path, ""
        )
    assert caught.value is failed
    assert calls == ["stdout.txt", "stderr.txt"]
    assert p.read(tmp_path / "stderr.txt") == b"err"


@pytest.mark.parametrize(
    "operation", ["read", "write", "inventory", "dist", "dist-child"]
)
@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_attribution_directory_owner_failure_and_repeat(
    tmp_path, monkeypatch, operation, fault
):
    tool = build.Tooling(tmp_path)
    original_ops = tool.ops
    if operation == "dist-child":
        (tmp_path / "dist").mkdir()
        p.write(tmp_path / "dist/payload", b"unowned recorded artifact")
    chosen_directory = tmp_path / "dist" if operation == "dist-child" else tmp_path
    p.write(tmp_path / "README.md", b"input")
    target = tmp_path / "output" if operation == "write" else tmp_path / "README.md"
    primary = (
        KeyboardInterrupt("directory operation interrupted")
        if fault == "interruption-close"
        else OSError("directory operation failed")
    )
    release = OSError("directory close failed")
    closes, owners = [], []
    original_open = build.DirectoryDescriptor.open_secure_path
    original_read, original_write = build.os.read, build.os.write
    original_names = build.DirectoryDescriptor.list_names
    original_child = build.DirectoryDescriptor.open_directory
    parent_closes = []

    class Ops:
        def __getattr__(self, name):
            return getattr(original_ops, name)

        def close(self, fd):
            path = os.readlink(f"/proc/self/fd/{fd}")
            chosen = path == str(chosen_directory)
            if path == str(tmp_path):
                parent_closes.append(fd)
            original_ops.close(fd)
            if chosen:
                closes.append(fd)
                raise release

    def opened(ops, path, **kwargs):
        owner = original_open(ops, path, **kwargs)
        owners.append(owner)
        return owner

    def child(owner, name, **kwargs):
        result = original_child(owner, name, **kwargs)
        owners.append(result)
        return result

    def read(fd, count):
        if fault != "close" and os.readlink(f"/proc/self/fd/{fd}") == str(target):
            raise primary
        return original_read(fd, count)

    def write(fd, data):
        if fault != "close":
            raise primary
        return original_write(fd, data)

    def names(owner):
        if (
            operation.startswith("dist")
            and fault != "close"
            and os.readlink(f"/proc/self/fd/{owner.fd}") == str(chosen_directory)
        ):
            raise primary
        return original_names(owner)

    tool.ops = Ops()
    monkeypatch.setattr(
        build.DirectoryDescriptor, "open_secure_path", staticmethod(opened)
    )
    monkeypatch.setattr(build.DirectoryDescriptor, "list_names", names)
    monkeypatch.setattr(build.DirectoryDescriptor, "open_directory", child)
    monkeypatch.setattr(build.os, "read", read)
    monkeypatch.setattr(build.os, "write", write)
    with pytest.raises(BaseException) as caught:
        if operation == "read":
            tool.read(target)
        elif operation == "write":
            tool.write(target, b"output")
        elif operation == "inventory":
            tool.inventory(("README.md",))
        else:
            tool.dist_inventory()
    assert caught.value is (release if fault == "close" else primary)
    assert len(closes) == 1
    if operation == "dist-child":
        assert len(parent_closes) == 1
    for owner in owners:
        owner.close()
    assert len(closes) == 1


def test_original_tooling_deadline_refuses_launch_without_renewal(
    tmp_path, monkeypatch
):
    tool = build.Tooling(tmp_path)
    tool.deadline = 0

    def forbidden(*args, **kwargs):
        raise AssertionError("launch after deadline")

    monkeypatch.setattr(build.OwnedProcess, "launch", forbidden)
    for _ in range(2):
        with pytest.raises(ValueError, match="original tooling deadline exhausted"):
            tool.command(["never-launched"])
    assert tool.deadline == 0 and tool.commands == [] and tool.owners == []


def test_incomplete_tooling_cleanup_is_unsuccessful_with_retained_status(
    tmp_path, monkeypatch
):
    # Synthetic incomplete observation over a genuinely settled real child.
    # The facade borrows the sole OwnedProcess; it never owns/releases pipes.
    launch = build.OwnedProcess.launch
    owners, exits = [], []

    class Observation:
        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            self.owner.__enter__()
            return self

        def __exit__(self, *args):
            exits.append(self.owner)
            return self.owner.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.owner, name)

        @property
        def cleanup_status(self):
            return SimpleNamespace(complete=False)

    def observed(*args, **kwargs):
        owner = launch(*args, **kwargs)
        owners.append(owner)
        return Observation(owner)

    monkeypatch.setattr(build.OwnedProcess, "launch", observed)
    tool = build.Tooling(tmp_path)
    with pytest.raises(ValueError, match="incomplete tooling cleanup"):
        tool.command([sys.executable, "-P", "-c", "print('retained')"])
    assert len(owners) == len(exits) == 1
    assert owners[0].cleanup_status.complete
    record = tool.commands[0]
    assert record["status"] == 0 and record["stdout"] == b"retained\n"
    assert record["terminals"] == {"STDOUT": "EOF", "STDERR": "EOF"}
    assert not record["cleanup_complete"]
    before = owners[0].cleanup_status
    assert owners[0].shutdown() == before
    assert len(exits) == 1


@pytest.mark.parametrize("status", [0, 17])
def test_real_command_retains_binary_streams_status_and_settled_owner(tmp_path, status):
    tool = build.Tooling(tmp_path)
    result = tool.command(
        [
            sys.executable,
            "-P",
            "-c",
            f"import sys; sys.stdout.buffer.write(b'out\\x00\\xff'); sys.stderr.buffer.write(b'err\\x00'); raise SystemExit({status})",
        ]
    )
    assert result["status"] == status
    assert result["stdout"] == b"out\x00\xff" and result["stderr"] == b"err\x00"
    assert result["cleanup_complete"] and result["terminals"] == {
        "STDOUT": "EOF",
        "STDERR": "EOF",
    }
    assert "PYTHONPATH" not in result["environment_keys"]
    for owner in tool.owners:
        before = owner.cleanup_status
        assert owner.shutdown() == before
        assert before.complete and not before.failures


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_command_sink_and_stream_close_failures_keep_owner_and_primary(
    tmp_path, monkeypatch, fault
):
    from footman.filesystem.streams import OwnedStream

    tool = build.Tooling(tmp_path)
    original_close = OwnedStream.close
    releases = []
    primary = (
        KeyboardInterrupt("collector interrupted")
        if fault == "interruption-close"
        else OSError("collector failed")
    )
    release = OSError("pipe close failed")

    class Buffer(bytearray):
        def extend(self, data):
            if fault != "close":
                raise primary
            return super().extend(data)

    def close(owner):
        was_released = owner.released
        original_close(owner)
        if not was_released:
            releases.append(owner.label)
            if owner.label == "stdout":
                raise release

    monkeypatch.setattr(build, "bytearray", Buffer, raising=False)
    monkeypatch.setattr(OwnedStream, "close", close)
    with pytest.raises(BaseException) as caught:
        tool.command(
            [
                sys.executable,
                "-P",
                "-c",
                "import sys; print('out'); sys.stderr.write('err')",
            ]
        )
    assert caught.value is (release if fault == "close" else primary)
    assert releases.count("stdout") == releases.count("stderr") == 1
    before = list(releases)
    assert len(tool.owners) == 1 and tool.owners[0].cleanup_status.complete
    tool.owners[0].shutdown()
    assert releases == before
    assert tool.commands[0]["cleanup_complete"]
    assert type(tool.commands[0]["status"]) is int


@pytest.mark.parametrize(
    "fault,mutate",
    [
        ("value", False),
        ("interrupt", False),
        ("none", True),
        ("value", True),
        ("interrupt", True),
    ],
)
def test_real_prepared_failure_and_completion_recheck_authority(
    admission, tmp_path, fault, mutate
):
    # No execution/loader double: real build/install, copied fixture, SDK and
    # subprocess owners run. Faults enter only after real report publication.
    candidate = (
        h.inventory(h.ROOT)[0],
        wheel.inventory(h.ROOT)[0],
        build.Tooling(h.ROOT).dist_inventory(),
    )
    root, environment, _ = h.policy_root(
        tmp_path, "pass", admission=admission.shared_pool
    )
    deadline = time.monotonic() + 120
    manifest, binding = h.bind(
        root,
        tmp_path / "build-evidence",
        admission=admission.shared_pool,
        deadline=deadline,
    )
    workspace, evidence = tmp_path / "workspace", tmp_path / "evidence"
    workspace.mkdir()
    (workspace / "host_tests").mkdir()
    evidence.mkdir()
    observation = tmp_path / "failure-observation.json"
    source = f"""import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("orchestrator", {str(root / "scripts/verify_host_acceptance.py")!r})
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
m._bootstrap()
import _footman_acceptance as tooling
core = m._Core(m._bootstrap())
path = core.root / "scripts/run_host_acceptance.py"
original_source = core.read(path)
primary = ValueError("leg publication failed") if {fault!r} == "value" else KeyboardInterrupt("leg publication interrupted") if {fault!r} == "interrupt" else None
original_write = tooling.Tooling.write
target = m._runner_target
calls = []
publications = []
def checked(core):
    calls.append(str(path))
    return target(core)
m._runner_target = checked
def write(self, destination, data):
    result = original_write(self, destination, data)
    if Path(destination) == Path({str(evidence / "report.json")!r}):
        publications.append(str(destination))
        if {mutate!r}:
            path.unlink()
            original_write(self, path, original_source + b"\\n# changed mid-leg\\n")
        if primary is not None:
            raise primary
    return result
tooling.Tooling.write = write
workspace, evidence = Path({str(workspace)!r}), Path({str(evidence)!r})
leg = m.PreparedLeg(Path({binding["wheel_path"]!r}), Path({str(manifest)!r}), "d9e853e87e55", evidence, workspace, "normal", (workspace / "host_tests",), None)
before = (list(sys.path), dict(os.environ), os.getcwd())
try:
    m._execute_prepared_leg(leg)
except BaseException as error:
    secondary = getattr(error, "_transaction_secondary", [])
    assert len(calls) == 3, calls  # two load checks, exactly one completion check
    assert len(publications) == 1
    assert before == (list(sys.path), dict(os.environ), os.getcwd())
    if primary is not None:
        assert error is primary
        assert len(secondary) == int({mutate!r})
        if secondary:
            assert str(secondary[0]) == "runner changed during prepared leg"
    else:
        assert type(error) is ValueError and str(error) == "runner changed during prepared leg"
    core.write(Path({str(observation)!r}), m._canonical({{"exception": type(error).__name__, "diagnostic": str(error), "same_primary": error is primary, "checks": len(calls), "secondary": [str(item) for item in secondary], "published": len(publications)}}))
    raise
else:
    raise AssertionError("prepared failure was accepted")
"""
    result = h.prepared_command(
        [sys.executable, "-P", "-c", source],
        admission=admission.shared_pool,
        cwd=tmp_path,
        env=environment,
        deadline=deadline,
    )
    p.write(
        tmp_path / "failure-operation.json", p.canonical(h.operation_record(result))
    )
    retained = p.load(observation)
    assert result["status"] == (-2 if fault == "interrupt" else 1), result
    # wheel.command returns only after complete cleanup, clean EOF and no timeout.
    assert retained["checks"] == 3 and retained["published"] == 1
    assert retained["exception"] == (
        "KeyboardInterrupt" if fault == "interrupt" else "ValueError"
    )
    assert retained["same_primary"] == (fault != "none")
    assert retained["secondary"] == (
        ["runner changed during prepared leg"] if mutate and fault != "none" else []
    )
    assert p.load(evidence / "report.json")["cleanup_complete"]
    assert (
        h.inventory(h.ROOT)[0],
        wheel.inventory(h.ROOT)[0],
        build.Tooling(h.ROOT).dist_inventory(),
    ) == candidate


@pytest.mark.parametrize("profile", ["normal", "negative"])
def test_real_sibling_loader_calls_prepared_function_without_cli_or_global_leak(
    admission, tmp_path, profile
):
    root, environment, _ = h.policy_root(
        tmp_path, "pass", admission=admission.shared_pool
    )
    if profile == "negative":
        p.write(
            root / "host_tests/test_negative_expectations.py",
            b"""from host_tests.probe_support import run
from tests.host_acceptance_assertions import verify_expectation

def test_negative():
    run()
    verify_expectation("argv", ["actual"], ["wrong"])
""",
        )
    deadline = time.monotonic() + 120
    manifest, binding = h.bind(
        root,
        tmp_path / "build-evidence",
        admission=admission.shared_pool,
        deadline=deadline,
    )
    with zipfile.ZipFile(io.BytesIO(p.read(binding["wheel_path"]))) as archive:
        assert "footman/acceptance_build.py" not in archive.namelist()
        assert not any("_footman_acceptance" in name for name in archive.namelist())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "host_tests").mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    if profile == "negative":
        p.write(
            workspace / "host_tests/expectation_input.json",
            p.canonical(
                {"schema_version": 1, "case_id": "argv", "expected_override": ["wrong"]}
            ),
        )
    observation = tmp_path / "function-result.json"
    source = """import importlib.util, json, os, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("orchestrator", Path(ROOT) / "scripts/verify_host_acceptance.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._bootstrap()
before = (list(sys.path), dict(os.environ), os.getcwd())
workspace, evidence = Path(WORKSPACE), Path(EVIDENCE)
leg = module.PreparedLeg(Path(WHEEL), Path(MANIFEST), "d9e853e87e55", evidence, workspace, PROFILE, (workspace / "host_tests/test_negative_expectations.py",) if PROFILE == "negative" else (workspace / "host_tests",), "argv" if PROFILE == "negative" else None)
started = time.monotonic()
result = module._execute_prepared_leg(leg)
ended = time.monotonic()
if before != (list(sys.path), dict(os.environ), os.getcwd()):
    raise RuntimeError("prepared call leaked process globals")
core = module._Core(module._bootstrap())
core.write(Path(OBSERVATION), module._canonical({"status": result.pytest_status, "counts": result.counts, "cleanup": result.cleanup_complete, "evidence": result.evidence_complete, "loader": result.loader, "started": started, "ended": ended}))
"""
    values = {
        "ROOT": str(root),
        "WORKSPACE": str(workspace),
        "EVIDENCE": str(evidence),
        "WHEEL": binding["wheel_path"],
        "MANIFEST": str(manifest),
        "PROFILE": profile,
        "OBSERVATION": str(observation),
    }
    for key, value in values.items():
        source = source.replace(key, repr(value))
    result = h.prepared_command(
        [sys.executable, "-P", "-c", source],
        admission=admission.shared_pool,
        cwd=tmp_path,
        env=environment,
        deadline=deadline,
    )
    p.write(tmp_path / "call-operation.json", p.canonical(h.operation_record(result)))
    assert result["status"] == 0, result
    retained = p.load(observation)
    assert retained["status"] == (0 if profile == "normal" else 1)
    assert retained["counts"]["collected"] == 1
    assert retained["cleanup"] and retained["evidence"]
    loader = retained["loader"]
    path = root / "scripts/run_host_acceptance.py"
    assert loader["module_path"] == loader["spec_origin"] == str(path)
    assert loader["source_sha256"] == hashlib.sha256(p.read(path)).hexdigest()
    assert h.inventory(root)[0] == binding["source_identity"]
    report = p.load(evidence / "report.json")
    budget = report["budget"]
    assert budget == {
        "schema_version": 1,
        "profile": profile,
        "provisioning_seconds": 600,
        "preflight_and_smokes_seconds": 120,
        "dispatch_timeout_seconds": 60,
        "dispatch_cleanup_seconds": 10,
        "dispatch_windows": 32 if profile == "normal" else 1,
        "finalization_seconds": 30,
        "total_seconds": 2990 if profile == "normal" else 820,
    }
    context = p.load(workspace / "execution-context.json")
    assert context["leg_budget"] == budget
    original_deadline = context["service_deadline_monotonic"] + 10
    assert (
        retained["started"] + budget["total_seconds"]
        <= original_deadline
        <= retained["ended"] + budget["total_seconds"]
    )
    assert report["configured_image_id"] == "d9e853e87e55"
    assert report["observed_image_id"] == "sha256:" + "b" * 64
    pytest_command = next(
        command for command in report["commands"] if "pytest" in command["argv"]
    )
    assert (
        str(
            workspace
            / (
                "host_tests"
                if profile == "normal"
                else "host_tests/test_negative_expectations.py"
            )
        )
        in pytest_command["argv"]
    )
    assert "PYTHONPATH" not in pytest_command["environment_keys"]
