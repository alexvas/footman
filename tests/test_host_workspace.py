"""Task-11.6 isolated workspace contracts, without Docker or future wrappers."""

import ast
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "_workspace_contract", ROOT / "scripts/run_host_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_support_closure_matches_declared_transport_closure(runner):
    assert set(runner._SUPPORT_FILES) == set(p.SUPPORT_FILES) | {
        "tests/host_acceptance_assertions.py"
    }
    assert len(runner._SUPPORT_FILES) == len(set(runner._SUPPORT_FILES))


def test_delivered_runner_passes_orchestrator_target_and_import_preflight(
    tmp_path, runner
):
    root, _ = source(tmp_path, runner)
    (root / "src").mkdir()
    (root / "scripts").mkdir()
    for name in ("verify_host_acceptance.py", "run_host_acceptance.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    spec = importlib.util.spec_from_file_location(
        "_workspace_orchestrator_contract", root / "scripts/verify_host_acceptance.py"
    )
    orchestrator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orchestrator)
    core = orchestrator._Core(
        (
            root,
            runner.DirectoryDescriptor,
            runner.OwnedDescriptor,
            runner.PosixDescriptorOps(),
            runner.OwnedProcess,
            runner.ProcessStream,
            runner.StreamOutcome,
        )
    )
    path, digest = orchestrator._runner_target(core)
    delivered = p.read(ROOT / "scripts/run_host_acceptance.py")
    assert path == root / "scripts/run_host_acceptance.py"
    assert p.read(path) == delivered
    assert digest == hashlib.sha256(delivered).hexdigest()
    orchestrator._import_safe(ast.parse(delivered).body)


@pytest.mark.parametrize(
    "collector,admitted",
    [
        (name, True)
        for name in (
            "test_mcp_transport.py",
            "test_mcp_server.py",
            "test_host_acceptance_contracts.py",
            "test_host_build_tooling.py",
            "test_host_provenance.py",
            "test_host_workspace.py",
        )
    ]
    + [("test_host_provenance_faults.py", False), ("test_scaffold.py", False)],
)
def test_shared_admission_covers_actual_provisioning_collectors(
    collector, admitted, _wheel_build_slot_path, monkeypatch
):
    from tests.conftest import _mcp_build_admission

    request = SimpleNamespace(node=SimpleNamespace(path=Path(collector)))
    with monkeypatch.context() as patch:
        patch.setattr(wheel, "BUILD_SLOT_PATH", None)
        _mcp_build_admission.__wrapped__(request, _wheel_build_slot_path, patch)
        assert wheel.BUILD_SLOT_PATH == (_wheel_build_slot_path if admitted else None)


def test_workspace_admission_cannot_silently_bypass_shared_slot(monkeypatch):
    from tests.admission_support import AdmissionPool

    value = SimpleNamespace(deadline=120, provision=Mock())
    slot = Mock(side_effect=AssertionError("must refuse before admission/acquisition"))
    monkeypatch.setattr(AdmissionPool, "acquire", slot)
    with pytest.raises(
        ValueError, match="missing shared workspace provisioning admission"
    ):
        wheel.provision_workspace(
            value, "exact.whl", "digest", admission=None, interpreter="python"
        )
    slot.assert_not_called()
    value.provision.assert_not_called()


def test_workspace_admission_wait_charges_original_command_deadline(
    tmp_path, runner, monkeypatch
):
    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock, module_clock
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    clock, plans, ends, contexts = ManualClock(origin=100), [], [], []
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = RecordingDescriptors(namespace)

    class System(RecordingSystem):
        def monotonic(self):
            return clock.monotonic()

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if acquired:
                clock.advance(7, label="workspace admission consumes original budget")
            return acquired

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            ends.append(deadline)
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(directory=namespace, capacity=1, system=System(descriptors=ops))
    primary = OSError("command launch boundary")

    def launch(argv, **kwargs):
        plans.append(kwargs)
        raise primary

    with module_clock(runner, clock):
        value = runner._Workspace(tmp_path / "source", tmp_path)
        original_deadline = value.deadline

        def provision(artifact, digest, *, interpreter):
            assert (artifact, digest, interpreter) == ("exact.whl", "digest", "python")
            assert any(token[2] == "slot" for token in ops.live.values())
            return value._command(["synthetic"])

        monkeypatch.setattr(value, "provision", provision)
        monkeypatch.setattr(runner, "OwnedProcess", SimpleNamespace(launch=launch))
        with pytest.raises(OSError) as caught:
            wheel.provision_workspace(
                value, "exact.whl", "digest", admission=pool, interpreter="python"
            )
    assert caught.value is primary and value.deadline == original_deadline == 220
    assert ends == [220] and clock.monotonic() == 107
    assert len(plans) == 1 and not value.owners
    assert plans[0]["deadline_seconds"] + plans[0]["grace_seconds"] == 113
    assert_released(ops)
    before = list(ops.closed)
    contexts[0].__exit__(None, None, None)
    contexts[0].__exit__(None, None, None)
    assert ops.closed == before


@pytest.mark.parametrize(
    "fault", ["none", "operation-close", "close", "interruption-close"]
)
def test_workspace_admission_owner_faults_and_independent_release(tmp_path, fault):
    from tests.admission_support import AdmissionPool
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    primary = (
        KeyboardInterrupt("provision interrupted")
        if fault == "interruption-close"
        else ValueError("provision failed")
    )
    secondary, parent_release, result = (
        OSError("admission close failed"),
        OSError("directory close failed"),
        object(),
    )
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = RecordingDescriptors(
        namespace, release=None if fault == "none" else secondary
    )
    ops.parent_release = None if fault == "none" else parent_release
    contexts = []

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(
        directory=namespace, capacity=1, system=RecordingSystem(descriptors=ops)
    )

    def provision(*args, **kwargs):
        assert any(token[2] == "slot" for token in ops.live.values())
        if fault in {"operation-close", "interruption-close"}:
            raise primary
        return result

    value = SimpleNamespace(
        deadline=wheel.time.monotonic() + 120, provision=Mock(side_effect=provision)
    )
    if fault == "none":
        assert (
            wheel.provision_workspace(
                value, "exact.whl", "digest", admission=pool, interpreter="python"
            )
            is result
        )
    else:
        with pytest.raises(BaseException) as caught:
            wheel.provision_workspace(
                value, "exact.whl", "digest", admission=pool, interpreter="python"
            )
        assert caught.value is (secondary if fault == "close" else primary)
        cleanup = getattr(caught.value, "_transaction_secondary", ())
        assert any(error is parent_release for error in cleanup)
        if fault != "close":
            assert any(error is secondary for error in cleanup)
    value.provision.assert_called_once_with("exact.whl", "digest", interpreter="python")
    assert len(contexts) == 1
    assert sum(token[2] == "slot" for token in ops.closed) == 1
    assert sum(token[2] == "namespace" for token in ops.closed) == 1
    assert_released(ops)
    before = list(ops.closed)
    contexts[0].__exit__(None, None, None)
    contexts[0].__exit__(None, None, None)
    assert ops.closed == before
    ops.release = ops.parent_release = None
    with pool.acquire(deadline=wheel.time.monotonic() + 5):
        pass
    assert_released(ops)


def test_workspace_admission_contention_exhausts_without_provisioning(
    tmp_path, monkeypatch
):
    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = RecordingDescriptors(namespace)
    clock, waits, busy = ManualClock(origin=100), [], []

    class System(RecordingSystem):
        def monotonic(self):
            return clock.monotonic()

        def sleep(self, seconds):
            waits.append(seconds)
            clock.advance(1, label="workspace contention exhausts deadline")

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if not acquired:
                busy.append(True)
            return acquired

    pool = AdmissionPool(
        directory=namespace, capacity=1, system=System(descriptors=ops)
    )
    context = pool.acquire(deadline=101)
    with context:
        value = SimpleNamespace(deadline=101.0, provision=Mock())
        with pytest.raises(TimeoutError, match="admission deadline exhausted"):
            wheel.provision_workspace(
                value, "exact.whl", "digest", admission=pool, interpreter="python"
            )
        assert waits == [0.01] and busy and value.deadline == 101.0
        value.provision.assert_not_called()
    assert_released(ops)
    before = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == before
    with pool.acquire(deadline=102):
        assert clock.monotonic() == 101
    assert_released(ops)


@pytest.mark.parametrize("elapsed", [0, 1])
def test_exhausted_workspace_budget_refuses_command_without_acquisition(
    tmp_path, runner, monkeypatch, elapsed
):
    _, workspace = source(tmp_path, runner)
    deadline = workspace.deadline
    monkeypatch.setattr(
        runner, "time", SimpleNamespace(monotonic=lambda: deadline + elapsed)
    )
    launches = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("exhausted workspace must not launch")

    monkeypatch.setattr(runner, "OwnedProcess", SimpleNamespace(launch=launch))
    for _ in range(2):
        with pytest.raises(ValueError, match="original workspace deadline exhausted"):
            workspace._command(["must-not-launch"])
        assert not launches and not workspace.commands and not workspace.owners
        assert workspace.deadline == deadline


def source(tmp_path, runner, body="pass"):
    root = tmp_path / "source"
    (root / "host_tests").mkdir(parents=True)
    (root / "tests").mkdir()
    for relative in (
        *runner._SUPPORT_FILES,
        "host_tests/__init__.py",
        "host_tests/conftest.py",
        "host_tests/support.py",
        "pyproject.toml",
        "uv.lock",
    ):
        shutil.copy2(ROOT / relative, root / relative)
    p.write(
        root / "host_tests/test_normal.py",
        ("import pytest\ndef test_probe():\n    " + body + "\n").encode(),
    )
    p.write(root / runner._NEGATIVE_SUITE, b"def test_negative():\n    pass\n")
    external = tmp_path / "external"
    external.mkdir()
    return root, runner._Workspace(root, external)


def test_normal_and_negative_copy_profiles_are_explicit(tmp_path, runner):
    root, normal = source(tmp_path, runner)
    normal.prepare()
    assert normal.suite_paths == ("host_tests",)
    assert not (normal.directory / runner._NEGATIVE_SUITE).exists()
    assert not (normal.directory / "pyproject.toml").exists()
    assert not (normal.directory / "uv.lock").exists()
    assert not (normal.directory / "src").exists()
    assert set(runner._SUPPORT_FILES) <= {r["path"] for r in normal.inventory}
    assert "tests/mcp_protocol_child.py" not in {r["path"] for r in normal.inventory}
    extra = tmp_path / "negative"
    extra.mkdir()
    negative = runner._Workspace(root, extra).prepare(
        profile="negative",
        expectation={
            "schema_version": 1,
            "case_id": "cleanup",
            "expected_override": False,
        },
    )
    assert negative.suite_paths == (runner._NEGATIVE_SUITE,)
    assert (extra / runner._NEGATIVE_SUITE).is_file()
    assert not (extra / "host_tests/test_normal.py").exists()
    assert {record["path"] for record in negative.generated} == {
        "host.ini",
        "host_tests/expectation_input.json",
    }
    for record in negative.generated:
        assert (
            record["sha256"]
            == hashlib.sha256(p.read(negative.directory / record["path"])).hexdigest()
        )
    assert "host_tests/expectation_input.json" not in {
        r["path"] for r in negative.inventory
    }
    with pytest.raises(ValueError, match="once-only"):
        normal.prepare()


@pytest.mark.parametrize("side", ["source", "copy"])
@pytest.mark.parametrize("change", ["missing", "mutated"])
def test_assertion_support_is_rechecked(tmp_path, runner, side, change):
    root, workspace = source(tmp_path, runner)
    workspace.prepare()
    target = (
        root if side == "source" else workspace.directory
    ) / "tests/host_acceptance_assertions.py"
    target.unlink()
    if change == "mutated":
        p.write(target, b"raise RuntimeError('mutated assertion')\n")
    with pytest.raises((ValueError, FileNotFoundError)):
        workspace.verify_copy()


@pytest.mark.parametrize("case", ["input", "protocol", "symlink", "nonempty"])
def test_unsafe_preparation_fails_closed_and_cannot_retry(tmp_path, runner, case):
    root, workspace = source(tmp_path, runner)
    if case == "input":
        p.write(root / "host_tests/expectation_input.json", b"{}")
    elif case == "protocol":
        p.write(root / "host_tests/bad.py", b"from tests import mcp_protocol_child\n")
    elif case == "symlink":
        (root / "host_tests/link.py").symlink_to("test_normal.py")
    else:
        p.write(workspace.directory / "sentinel", b"unrelated")
    with pytest.raises(ValueError):
        workspace.prepare()
    assert not workspace.ready and not workspace.commands
    with pytest.raises(ValueError, match="once-only"):
        workspace.prepare()


def test_clean_environment_rejects_inherited_injection(monkeypatch, runner):
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTEST_ADDOPTS",
        "FOOTMAN_HOST_CONTEXT",
    ):
        monkeypatch.setenv(key, "poison")
    env = runner._environment()
    assert [key for key in env if key.startswith("PYTHON")] == ["PYTHONNOUSERSITE"]
    assert not any(key.startswith(("PYTEST", "FOOTMAN_")) for key in env)


@pytest.fixture(scope="module")
def real_bound(_mcp_worker_wheel):
    return _mcp_worker_wheel


@pytest.mark.parametrize(
    "case,status", [("pass", 0), ("skip", 1), ("xfail", 1), ("xpass", 1), ("empty", 5)]
)
def test_fresh_wheel_environment_policy_and_copied_imports(
    tmp_path, runner, real_bound, case, status, monkeypatch, admission
):
    root, workspace = source(tmp_path, runner)
    probe = root / "host_tests/test_normal.py"
    imports = "import pytest\nimport footman.process\nfrom tests import host_acceptance_assertions, mcp_transport, mcp_server_child, mcp_lifecycle, mcp_provenance\n"
    body = {
        "pass": "assert host_acceptance_assertions.__file__.startswith("
        + repr(str(workspace.directory))
        + ")\n    assert not footman.process.__file__.startswith("
        + repr(str(ROOT))
        + ")\n    assert mcp_transport.OwnedProcess is footman.process.OwnedProcess",
        "skip": "pytest.skip('policy control')",
        "xfail": "pytest.xfail('policy control')",
        "xpass": "pass",
    }
    probe.unlink()
    content = (
        "# no items\n"
        if case == "empty"
        else imports
        + ("@pytest.mark.xfail\n" if case == "xpass" else "")
        + "def test_probe():\n    "
        + body[case]
        + "\n"
    )
    p.write(probe, content.encode())
    workspace.prepare()
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    wheel.provision_workspace(
        workspace,
        real_bound.wheel,
        real_bound.wheel_sha256,
        admission=admission.shared_pool,
        interpreter=sys.executable,
    )
    generated = {record["path"]: record["sha256"] for record in workspace.generated}
    assert set(generated) == {"host.ini", "locked-constraints.txt"}
    assert "locked-constraints.txt" not in {r["path"] for r in workspace.inventory}
    for path, digest in generated.items():
        assert digest == hashlib.sha256(p.read(workspace.directory / path)).hexdigest()
    result = workspace.run_pytest()
    assert result["status"] == status, result
    assert all(c["cleanup_complete"] for c in workspace.commands)
    assert all("PYTHONPATH" not in c["environment_keys"] for c in workspace.commands)
    assert all(owner.cleanup_status.complete for owner in workspace.owners)
    assert workspace.commands[1]["argv"][-3:] == [
        str(real_bound.wheel),
        "pytest==8.4.2",
        "pytest-xdist==3.8.0",
    ]
    assert result["argv"].count("-n") == 1 and "worksteal" in result["argv"]
    assert str(workspace.ini) in result["argv"]
    assert str(workspace.directory / "host_tests") in result["argv"]
    assert (
        b"BLOCKED" in result["stdout"] if case in {"skip", "xfail", "xpass"} else True
    )
    assert b"1 passed" in result["stdout"] if case == "pass" else True
    with pytest.raises(ValueError, match="once-only"):
        workspace.provision(
            real_bound.wheel, real_bound.wheel_sha256, interpreter=sys.executable
        )


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_file_owner_faults_release_once_and_preserve_authority(
    tmp_path, runner, monkeypatch, operation, fault
):
    _, workspace = source(tmp_path, runner)
    target = tmp_path / "payload"
    if operation == "read":
        p.write(target, b"payload")
    original_owner = runner.OwnedDescriptor
    original_io = getattr(os, operation)
    owners = []
    closes = []
    primary = (
        KeyboardInterrupt("operation interruption")
        if fault == "interruption-close"
        else OSError("operation failure")
    )
    close_error = OSError("sole close failure")

    class FailingOps:
        def close(self, fd):
            closes.append(fd)
            workspace.ops.close(fd)
            raise close_error

    def owner(ops, fd, *, label):
        result = original_owner(FailingOps(), fd, label=label)
        owners.append(result)
        return result

    def io(fd, *args):
        if fault != "close":
            raise primary
        return original_io(fd, *args)

    monkeypatch.setattr(runner, "OwnedDescriptor", owner)
    monkeypatch.setattr(runner.os, operation, io)
    expected = close_error if fault == "close" else primary
    with pytest.raises(BaseException) as caught:
        getattr(workspace, "_" + operation)(
            target, b"data"
        ) if operation == "write" else workspace._read(target)
    assert caught.value is expected
    assert len(owners) == len(closes) == 1
    owners[0].close()
    assert len(closes) == 1


@pytest.mark.parametrize("status", [1, 127])
def test_provisioning_failure_keeps_actual_status_and_stops(
    tmp_path, runner, monkeypatch, status
):
    _, workspace = source(tmp_path, runner)
    workspace.prepare()
    artifact = tmp_path / "exact.whl"
    # Archive-valid attribution-only bytes; the mocked venv command fails
    # before installation, so these are never treated as a usable wheel.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("__init__.py", "startup.py", "process.py", "__main__.py"):
            archive.writestr("footman/" + name, b"# inert\n")
        archive.writestr(
            "footman-0.1.0.dist-info/entry_points.txt",
            b"[console_scripts]\nfootman = footman.startup:main\n",
        )
    payload = buffer.getvalue()
    p.write(artifact, payload)
    digest = hashlib.sha256(payload).hexdigest()
    called = []

    def command(argv):
        record = {
            "argv": argv,
            "status": status,
            "stdout": b"actual out",
            "stderr": b"actual err",
        }
        called.append(record)
        workspace.commands.append(record)
        return record

    monkeypatch.setattr(workspace, "_command", command)
    with pytest.raises(ValueError, match="provisioning failed"):
        workspace.provision(artifact, digest, interpreter=sys.executable)
    assert len(called) == 1 and called[0]["status"] == status
    constraints = next(
        record
        for record in workspace.generated
        if record["path"] == "locked-constraints.txt"
    )
    assert (
        constraints["sha256"]
        == hashlib.sha256(p.read(workspace.directory / constraints["path"])).hexdigest()
    )
    assert constraints["path"] not in {r["path"] for r in workspace.inventory}
    assert not workspace.provisioned
    with pytest.raises(ValueError, match="once-only"):
        workspace.provision(artifact, digest, interpreter=sys.executable)


def test_invalid_binding_never_installs(tmp_path, runner):
    _, workspace = source(tmp_path, runner)
    workspace.prepare()
    artifact = tmp_path / "exact.whl"
    p.write(artifact, b"inert")
    with pytest.raises(ValueError, match="binding mismatch"):
        workspace.provision(artifact, "0" * 64, interpreter=sys.executable)
    assert not workspace.commands


@pytest.mark.parametrize(
    "fault",
    ["operation-close", "close", "interruption-close", "pending", "read-failed"],
)
def test_command_sole_owner_faults_and_single_collection(
    tmp_path, runner, monkeypatch, fault
):
    from footman.filesystem.cleanup import CleanupFailures

    _, workspace = source(tmp_path, runner)
    primary = (
        KeyboardInterrupt("stream interruption")
        if fault == "interruption-close"
        else OSError("stream failure")
    )
    release = OSError("pipe release")
    collections = []
    releases = []

    class Owner:
        cleanup_status = SimpleNamespace(complete=fault != "pending")

        def __enter__(self):
            return self

        def stream(self, chunk, *, on_stream_end):
            collections.append(1)
            chunk(runner.ProcessStream.STDOUT, b"actual output")
            if fault == "interruption-close":
                raise primary
            for stream in runner.ProcessStream:
                on_stream_end(
                    stream,
                    runner.StreamOutcome.READ_FAILED
                    if fault == "read-failed"
                    else runner.StreamOutcome.EOF,
                )
            failures = (
                (SimpleNamespace(error=primary),) if fault == "operation-close" else ()
            )
            if fault == "close":
                failures = (SimpleNamespace(error=release),)
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                lifecycle=SimpleNamespace(
                    complete=fault != "pending", failures=failures
                ),
            )

        def __exit__(self, kind, error, tb):
            failures = CleanupFailures(error)
            failures.run(self.close, ordinary=(Exception,))
            selected = failures.complete()
            if selected is not None:
                raise selected

        def close(self):
            if releases:
                return
            releases.append(1)
            if fault.endswith("close"):
                raise release

    owner = Owner()
    launches = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return owner

    monkeypatch.setattr(runner, "OwnedProcess", SimpleNamespace(launch=launch))
    with pytest.raises(BaseException) as caught:
        workspace._command(["synthetic-fixture"])
    if fault in {"operation-close", "interruption-close"}:
        assert caught.value is primary
    elif fault == "close":
        assert caught.value is release
    else:
        assert isinstance(caught.value, ValueError)
    assert len(collections) == len(releases) == len(launches) == 1
    assert workspace.owners == [owner]
    assert workspace.commands[0]["stdout"] == b"actual output"
    assert launches[0][1]["capture_limit"] == 0
    assert "PYTHONPATH" not in launches[0][1]["popen_kwargs"]["env"]
    owner.close()
    assert len(releases) == 1


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_directory_creation_independent_release_matrix(
    tmp_path, runner, monkeypatch, fault
):
    _, workspace = source(tmp_path, runner)
    parent = runner.DirectoryDescriptor.open_secure_path(workspace.ops, tmp_path)
    original_close = runner.DirectoryDescriptor.close
    primary = (
        KeyboardInterrupt("directory interruption")
        if fault == "interruption-close"
        else OSError("directory operation")
    )
    release = OSError("directory release")
    closed = []

    def close(owner):
        if owner.released:
            return original_close(owner)
        closed.append(owner)
        original_close(owner)
        if owner is not parent:
            raise release

    class ChildScope:
        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            return self.owner

        def __exit__(self, kind, error, tb):
            return self.owner.__exit__(kind, error, tb)

    create = parent.create_directory

    def create_with_fault(owner, name, *, mode):
        assert owner is parent
        child = create(name, mode=mode)

        # The real acquired child remains sole owner during injected body work.
        class Scope(ChildScope):
            def __enter__(self):
                if fault != "close":
                    try:
                        raise primary
                    except BaseException:
                        self.owner.__exit__(
                            type(primary), primary, primary.__traceback__
                        )
                        raise
                return self.owner

        return Scope(child)

    monkeypatch.setattr(
        runner.DirectoryDescriptor, "open_secure_path", lambda *a, **kw: parent
    )
    monkeypatch.setattr(runner.DirectoryDescriptor, "close", close)
    monkeypatch.setattr(
        runner.DirectoryDescriptor, "create_directory", create_with_fault
    )
    with pytest.raises(BaseException) as caught:
        workspace._mkdir(tmp_path / "created")
    assert caught.value is (release if fault == "close" else primary)
    assert len(closed) == 2 and parent in closed
    for owner in closed:
        owner.close()
    assert len(closed) == 2
