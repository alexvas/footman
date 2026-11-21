"""Task-11.7 archive authority and immediate-parent provenance contracts."""

import hashlib
import importlib.util
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from tests import mcp_contract_support as service
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "_provenance_runner", ROOT / "scripts/run_host_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "fault", [None, "interpreter", "arguments", "extra-shell", "extra-python"]
)
@pytest.mark.parametrize(
    "interpreter",
    [
        "/external/very-long-" + "x" * 160 + "/bin/python",
        "/external/space and 'quote/venv/bin/python",
    ],
)
def test_bound_uv_long_shebang_trampoline(runner, fault, interpreter):
    import ast

    from tests import mcp_server_child as bootstrap

    quoted = "'" + interpreter.replace("'", "'\"'\"'") + "'"
    prefix = ("#!/bin/sh\n'''exec' " + quoted + ' "$0" "$@"\n' + "' '''\n").encode()
    body = runner._LAUNCHER.encode()
    data = prefix + body
    if fault == "interpreter":
        data = data.replace(quoted.encode(), b"'/ambient/python'")
    elif fault == "arguments":
        data = data.replace(b'"$@"', b'"$*"')
    elif fault == "extra-shell":
        data = data.replace(b"#!/bin/sh\n", b"#!/bin/sh\necho injected\n")
    elif fault == "extra-python":
        data += b"print('injected')\n"
    expected = ast.dump(ast.parse(body))
    for normalize in (runner._launcher_body, bootstrap.launcher_body):
        if fault in {"interpreter", "arguments", "extra-shell"}:
            with pytest.raises(ValueError, match="launcher semantics"):
                normalize(data, interpreter)
        else:
            assert (ast.dump(ast.parse(normalize(data, interpreter))) == expected) is (
                fault is None
            )
        assert normalize(body, interpreter) == body


@pytest.mark.parametrize(
    "fault", [None, "operation-close", "close", "interruption-close"]
)
def test_worker_artifact_discovery_sole_owner_matrix(
    tmp_path, runner, monkeypatch, fault
):
    from types import SimpleNamespace

    for token in ("a", "b"):
        (tmp_path / ("host-runs-" + token * 32)).mkdir()
    workspace = SimpleNamespace(
        directory=tmp_path,
        ops=runner.PosixDescriptorOps(),
        host_context={"pytest_workers": 2},
    )
    opened, closed = [], []
    original_open = runner.DirectoryDescriptor.open_secure_path
    original_close = runner.DirectoryDescriptor.close
    original_names = runner.DirectoryDescriptor.list_names
    primary = (
        KeyboardInterrupt("discovery interrupted")
        if fault == "interruption-close"
        else OSError("discovery failed")
    )
    secondary = OSError("discovery close failed")

    def opening(ops, path):
        owner = original_open(ops, path)
        opened.append(owner)
        return owner

    def names(owner):
        if fault in {"operation-close", "interruption-close"}:
            raise primary
        return original_names(owner)

    def closing(owner):
        if owner in opened and not owner.released:
            closed.append(owner)
            original_close(owner)
            if fault is not None:
                raise secondary
        else:
            original_close(owner)

    monkeypatch.setattr(
        runner.DirectoryDescriptor, "open_secure_path", staticmethod(opening)
    )
    monkeypatch.setattr(runner.DirectoryDescriptor, "list_names", names)
    monkeypatch.setattr(runner.DirectoryDescriptor, "close", closing)
    if fault is None:
        paths = runner._flow_artifacts(workspace, "provenance.json")
        assert len(paths) == len(set(paths)) == 2
    else:
        with pytest.raises(BaseException) as caught:
            runner._flow_artifacts(workspace, "provenance.json")
        assert caught.value is (secondary if fault == "close" else primary)
    assert len(opened) == len(closed) == 1
    opened[0].close()
    assert len(closed) == 1


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink"])
def test_each_worker_provenance_is_required_and_unsafe_roots_fail_closed(
    tmp_path, runner, monkeypatch, fault
):
    from types import SimpleNamespace

    roots = [tmp_path / ("host-runs-" + token * 32) for token in ("a", "b")]
    for root in roots:
        root.mkdir()
    if fault == "symlink":
        roots[1].rmdir()
        roots[1].symlink_to(roots[0])
    workspace = SimpleNamespace(
        directory=tmp_path,
        ops=runner.PosixDescriptorOps(),
        host_context={"pytest_workers": 2},
    )
    calls = []

    def verify(owner, path):
        calls.append(path)
        if path.parent == roots[1]:
            raise ValueError(
                "missing worker evidence"
                if fault == "missing"
                else "changed worker evidence"
            )

    monkeypatch.setattr(runner, "_verified_evidence_file", verify)
    with pytest.raises(ValueError, match="worker evidence|unsafe host flow root"):
        runner._verified_evidence(workspace)
    assert not hasattr(workspace, "verified_evidence_paths")
    assert len(calls) == (0 if fault == "symlink" else 2)


@pytest.fixture
def workspace(tmp_path, runner, _mcp_worker_wheel, admission):
    root = tmp_path / "source"
    root.mkdir()
    (root / "host_tests").mkdir()
    (root / "tests").mkdir()
    for relative in (
        *runner._SUPPORT_FILES,
        "host_tests/__init__.py",
        "host_tests/conftest.py",
        "host_tests/support.py",
        "host_tests/provenance.py",
        "uv.lock",
        "pyproject.toml",
    ):
        shutil.copy2(ROOT / relative, root / relative)
    external = tmp_path / "external"
    external.mkdir()
    value = runner._Workspace(root, external).prepare()
    wheel.provision_workspace(
        value,
        _mcp_worker_wheel.wheel,
        _mcp_worker_wheel.wheel_sha256,
        admission=admission.shared_pool,
        interpreter=sys.executable,
    )
    binding = tmp_path / "build-manifest.json"
    p.write(
        binding,
        p.canonical(
            {
                "schema_version": 1,
                "wheel_path": str(_mcp_worker_wheel.wheel),
                "wheel_sha256": _mcp_worker_wheel.wheel_sha256,
            }
        ),
    )
    return value, _mcp_worker_wheel, binding


def test_archive_expectations_do_not_trust_stale_installation(workspace):
    value, bound, binding = workspace
    site = next((value.venv / "lib").glob("python*/site-packages"))
    stale = site / "footman/__main__.py"
    stale.unlink()
    p.write(stale, b"raise RuntimeError('STALE_ENTRY_MUST_NOT_EXECUTE')\n")
    manifest = value.prepare_provenance(bound.wheel, binding)
    with zipfile.ZipFile(io.BytesIO(p.read(bound.wheel))) as archive:
        assert {m["source_path"] for m in manifest["modules"]} == {
            n
            for n in archive.namelist()
            if n.startswith("footman/") and n.endswith(".py")
        }
        for module in manifest["modules"]:
            assert (
                module["sha256"]
                == hashlib.sha256(archive.read(module["source_path"])).hexdigest()
            )
    entry = next(m for m in manifest["modules"] if m["name"] == "footman.__main__")
    assert entry["sha256"] != hashlib.sha256(p.read(stale)).hexdigest()
    assert {r["path"] for r in value.generated} >= {
        "expected-origin.json",
        "host-context.json",
    }
    assert not {"expected-origin.json", "host-context.json"} & {
        r["path"] for r in value.inventory
    }
    assert value.host_context["wheel_sha256"] == bound.wheel_sha256
    assert value.host_context["service_deadline_monotonic"] == value.deadline - 10


@pytest.mark.parametrize("change", ["digest", "path", "version", "duplicate-key"])
def test_build_binding_rejected_before_layout_query(workspace, change):
    value, bound, binding = workspace
    data = p.load(binding)
    if change == "duplicate-key":
        encoded = p.canonical(data).replace(
            b'"schema_version":1', b'"schema_version":1,"schema_version":1'
        )
    else:
        data[
            {
                "digest": "wheel_sha256",
                "path": "wheel_path",
                "version": "schema_version",
            }[change]
        ] = {"digest": "0" * 64, "path": "/other.whl", "version": True}[change]
        encoded = p.canonical(data)
    binding.unlink()
    p.write(binding, encoded)
    before = len(value.commands)
    with pytest.raises(ValueError):
        value.prepare_provenance(bound.wheel, binding)
    assert len(value.commands) == before
    assert not (value.directory / "expected-origin.json").exists()


def archive_bytes(change=None):
    files = [
        ("footman/" + name, b"# source\n")
        for name in (
            "__init__.py",
            "startup.py",
            "process.py",
            "__main__.py",
            "filesystem/__init__.py",
        )
    ]
    files.append(
        (
            "footman-0.1.0.dist-info/entry_points.txt",
            b"[console_scripts]\nfootman = footman.startup:main\n",
        )
    )
    if change == "missing":
        files = [(n, b) for n, b in files if n != "footman/__main__.py"]
    elif change == "mapping":
        files.append(("footman/process/__init__.py", b"# alias\n"))
    elif change == "duplicate":
        files.append(files[0])
    elif change == "distribution":
        files.append(("footman-9.0.0.dist-info/RECORD", b"other distribution"))
    elif change == "node-type":
        files.append(("footman/directory.py", b"not a regular source"))
    elif change in {
        "absolute",
        "traversal",
        "normalized",
        "backslash",
        "scheme",
        "symlink",
    }:
        files.append(
            (
                {
                    "absolute": "/footman/bad.py",
                    "traversal": "footman/../bad.py",
                    "normalized": "footman//bad.py",
                    "backslash": "footman/xx\\bad.py",
                    "scheme": "alternate/bad.py",
                    "symlink": "footman/link.py",
                }[change],
                b"unsafe",
            )
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files:
            info = zipfile.ZipInfo(name)
            if change == "symlink" and name.endswith("link.py"):
                info.external_attr = 0o120777 << 16
            if change == "node-type" and name.endswith("directory.py"):
                info.external_attr = 0o40700 << 16
            archive.writestr(info, data)
    return output.getvalue()


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "mapping",
        "absolute",
        "traversal",
        "normalized",
        "backslash",
        "scheme",
        "symlink",
        "distribution",
        "node-type",
    ],
)
def test_archive_member_and_mapping_controls(runner, change):
    baseline, target = runner._archive_modules(archive_bytes())
    assert len(baseline) == 5 and target == "footman.startup:main"
    with pytest.raises(ValueError):
        runner._archive_modules(archive_bytes(change))


def test_duplicate_archive_members_are_rejected(runner):
    # The fixture builder warns about its intentionally duplicated member;
    # assert that diagnostic explicitly rather than globally filtering it.
    with pytest.warns(UserWarning, match="Duplicate name"):
        data = archive_bytes("duplicate")
    with pytest.raises(ValueError, match="duplicate wheel members"):
        runner._archive_modules(data)


@pytest.mark.parametrize("change", ["traversal", "symlink", "mapping"])
def test_invalid_archive_never_reaches_installer(workspace, runner, tmp_path, change):
    base, _, _ = workspace
    directory = tmp_path / "unsafe-install"
    directory.mkdir()
    value = runner._Workspace(base.root, directory).prepare()
    artifact = tmp_path / "unsafe.whl"
    data = archive_bytes(change)
    p.write(artifact, data)
    with pytest.raises(ValueError):
        value.provision(
            artifact, hashlib.sha256(data).hexdigest(), interpreter=sys.executable
        )
    assert not value.commands and not value.owners
    assert not value.venv.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "stale-entry",
        "stale-container",
        "stale-supervision",
        "missing-supervision",
        "self-consistent-stale",
        "swapped-copy",
        "missing",
        "extra",
        "symlink",
        "wrong-root",
        "launcher",
        "shadow-script",
        "shadow-cwd",
        "assertion-missing",
        "assertion-mutated",
        "assertion-mutated-during",
        "child-after-attestation",
    ],
)
def test_copied_parent_and_same_child_service_provenance(workspace, tmp_path, fault):
    value, bound, binding = workspace
    value.prepare_provenance(bound.wheel, binding)
    _, config = service.configuration(tmp_path)
    test = value.directory / "host_tests/test_provenance.py"
    # This disposable probe is test input, not production fault configuration.
    program = """
import base64
import csv
import io
import json
import os
import shutil
from pathlib import Path
import anyio
from tests import mcp_provenance as p, mcp_transport as transport
from host_tests.provenance import verified_session

context = json.loads(CONTEXT)
fault = FAULT
original_transport = transport.stdio_transport

def injected(*args, **kwargs):
    manifest = p.load(Path(context['expected_origin_path']))
    package = Path(next(m['expected_path'] for m in manifest['modules'] if m['name'] == 'footman')).parent
    if fault in {'stale-entry', 'stale-container', 'stale-supervision', 'self-consistent-stale', 'swapped-copy'}:
        if fault == 'swapped-copy':
            original = package.with_name('original-footman')
            package.rename(original)
            shutil.copytree(original, package)
        target = package / {'stale-entry': '__main__.py', 'stale-supervision': 'supervision.py'}.get(fault, 'container.py')
        target.unlink()
        p.write(target, b"raise RuntimeError('STALE_SOURCE_EXECUTED')\\n")
        if fault in {'self-consistent-stale', 'swapped-copy'}:
            record = package.parent / 'footman-0.1.0.dist-info/RECORD'
            rows = list(csv.reader(io.StringIO(p.read(record).decode())))
            for row in rows:
                if row[0] == 'footman/container.py':
                    data = p.read(target)
                    row[1:] = ['sha256=' + base64.urlsafe_b64encode(__import__('hashlib').sha256(data).digest()).rstrip(b'=').decode(), str(len(data))]
            encoded = io.StringIO()
            csv.writer(encoded).writerows(rows)
            record.unlink()
            p.write(record, encoded.getvalue().encode())
    elif fault in {'missing', 'missing-supervision'}:
        (package / ('supervision.py' if fault == 'missing-supervision' else 'container.py')).unlink()
    elif fault == 'extra':
        p.write(package / 'extra.py', b'# extra source\\n')
    elif fault == 'symlink':
        (package / 'container.py').unlink()
        (package / 'container.py').symlink_to('startup.py')
    elif fault == 'launcher':
        target = Path(context['server_script'])
        data = p.read(target)
        target.unlink()
        p.write(target, data + b'print("BAD_LAUNCHER")\\n')
    elif fault == 'wrong-root':
        manifest['modules'][0]['expected_path'] = '/wrong/footman/__init__.py'
        target = Path(context['expected_origin_path'])
        target.unlink()
        p.write(target, p.canonical(manifest))
    elif fault == 'child-after-attestation':
        kwargs['env']['FOOTMAN_BOOTSTRAP_TEST_FAULT'] = 'after-attestation'
    if fault in {'stale-supervision', 'missing-supervision'}:
        assert any(m['name'] == 'footman.supervision' for m in manifest['modules'])
        for module in manifest['modules']:
            if module['name'] in {'footman', 'footman.startup', 'footman.process', 'footman.__main__'}:
                assert __import__('hashlib').sha256(p.read(Path(module['expected_path']))).hexdigest() == module['sha256']
    return original_transport(*args, **kwargs)

transport.stdio_transport = injected

async def scenario(observed):
    async with verified_session(context, Path(CONFIG), observed) as session:
        assert (await session.list_tools()).tools[0].name == 'run_privileged_command'
        if fault == 'assertion-mutated-during':
            target = Path(context['support_root']) / 'tests/host_acceptance_assertions.py'
            target.unlink()
            p.write(target, b'# changed during service\\n')


def test_probe():
    if fault in {'assertion-missing', 'assertion-mutated'}:
        target = Path(context['support_root']) / 'tests/host_acceptance_assertions.py'
        target.unlink()
        if fault == 'assertion-mutated':
            p.write(target, b'# mutated\\n')
    if fault in {'shadow-script', 'shadow-cwd'}:
        parent = Path(context['support_root']) / 'tests' if fault == 'shadow-script' else Path(context['neutral_cwd'])
        for name in ('json.py', 'footman.py'):
            p.write(parent / name, b"raise RuntimeError('SHADOW_SOURCE_EXECUTED')\\n")
    os.environ['PYTHONPATH'] = CHECKOUT
    os.environ['PYTHONHOME'] = '/invalid-home'
    os.environ['PYTHONUSERBASE'] = '/invalid-user-base'
    observed = transport.TransportObservation()
    primary = None
    try:
        anyio.run(scenario, observed)
    except BaseException as error:
        primary = error
    finally:
        p.write(Path(context['support_root']) / 'service-observation.json', p.canonical({
            'acquired': observed.owner is not None,
            'cleanup_complete': None if observed.owner is None else observed.owner.cleanup_status.complete,
            'returncode': None if observed.result is None else observed.result.returncode,
            'eof': observed.eof_delivered,
            'stderr': bytes(observed.stderr_tail).decode('utf-8', 'replace'),
        }))
    if primary is not None:
        raise primary
"""
    program = (
        program.replace(
            "json.loads(CONTEXT)",
            "json.loads(" + repr(json.dumps(value.host_context)) + ")",
        )
        .replace("fault = FAULT", "fault = " + repr(fault))
        .replace("Path(CONFIG)", "Path(" + repr(str(config)) + ")")
        .replace("= CHECKOUT", "= " + repr(str(ROOT / "src")))
    )
    p.write(test, program.encode())
    result = value._command(
        [
            value.python,
            "-P",
            "-m",
            "pytest",
            "-c",
            value.ini,
            test,
            "-n",
            "1",
            "--dist",
            "worksteal",
            "--tb=short",
            "-q",
        ]
    )
    success = fault in {"none", "shadow-script", "shadow-cwd"}
    assert result["status"] == (0 if success else 1), result
    observed = p.load(value.directory / "service-observation.json")
    evidence = value.directory / "provenance.json"
    assert (
        observed["cleanup_complete"] is True
        if observed["acquired"]
        else fault.startswith("assertion-")
    )
    if success:
        retained = p.load(evidence)
        assert retained["wheel_sha256"] == bound.wheel_sha256
        assert retained["source_count"] == len(retained["sources"])
        assert (
            retained["set_sha256"]
            == hashlib.sha256(p.canonical(retained["sources"])).hexdigest()
        )
        assert retained["argv"][:2] == [str(value.python), "-P"]
        assert "--lifecycle" not in retained["argv"]
        assert "SHADOW_SOURCE_EXECUTED" not in observed["stderr"]
        assert observed["eof"] and observed["returncode"] == 0
    elif fault in {"child-after-attestation", "assertion-mutated-during"}:
        assert (observed["returncode"] != 0) == (fault == "child-after-attestation")
        assert evidence.is_file(), result
    else:
        assert not evidence.exists(), result
        assert "STALE_SOURCE_EXECUTED" not in observed["stderr"]
        if fault in {"stale-supervision", "missing-supervision"}:
            assert observed["acquired"] and observed["cleanup_complete"]
            assert observed["returncode"] is not None and observed["returncode"] != 0
            assert observed["eof"]
            assert "full package source-set mismatch" in observed["stderr"]
            assert p.WIRE.decode() not in observed["stderr"]


@pytest.mark.parametrize("remaining", [60, 6])
def test_command_execution_and_cleanup_fit_original_envelope(
    tmp_path, runner, monkeypatch, remaining
):
    directory = tmp_path / "external"
    directory.mkdir()
    value = runner._Workspace(tmp_path / "source", directory)
    monkeypatch.setattr(
        runner,
        "time",
        type(
            "Clock", (), {"monotonic": staticmethod(lambda: value.deadline - remaining)}
        ),
    )
    observed = []

    def launch(argv, **kwargs):
        observed.append(kwargs)
        raise OSError("launch boundary control")

    monkeypatch.setattr(
        runner, "OwnedProcess", type("Process", (), {"launch": staticmethod(launch)})
    )
    with pytest.raises(OSError, match="launch boundary"):
        value._command(["synthetic"])
    assert len(observed) == 1 and not value.owners
    plan = observed[0]
    assert plan["deadline_seconds"] + plan["grace_seconds"] == remaining
    assert 0 < plan["grace_seconds"] <= 5
    if remaining > 10:
        assert plan["deadline_seconds"] > remaining - 10


def test_missing_python_prerequisite_is_a_real_failure(
    workspace, runner, tmp_path, admission
):
    base, bound, _ = workspace
    directory = tmp_path / "missing-prerequisite"
    directory.mkdir()
    value = runner._Workspace(base.root, directory).prepare()
    with pytest.raises(ValueError, match="provisioning failed"):
        wheel.provision_workspace(
            value,
            bound.wheel,
            bound.wheel_sha256,
            admission=admission.shared_pool,
            interpreter=tmp_path / "absent-python",
        )
    assert len(value.commands) == 1
    assert value.commands[0]["status"] != 0
    assert value.commands[0]["stderr"]
    assert not value.provisioned
    assert all(owner.cleanup_status.complete for owner in value.owners)


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_origin_generation_owner_faults_stop_before_service_context(
    workspace, runner, monkeypatch, fault
):
    value, bound, binding = workspace
    target = value.directory / "expected-origin.json"
    original_owner = runner.OwnedDescriptor
    original_write = runner.os.write
    owners, released, fds = [], [], set()
    primary = (
        KeyboardInterrupt("origin interrupted")
        if fault == "interruption-close"
        else OSError("origin write failed")
    )
    release = OSError("origin close failed")

    class Ops:
        def close(self, fd):
            released.append(fd)
            value.ops.close(fd)
            raise release

    def owner(ops, fd, *, label):
        if runner.os.readlink(f"/proc/self/fd/{fd}") != str(target):
            return original_owner(ops, fd, label=label)
        result = original_owner(Ops(), fd, label=label)
        owners.append(result)
        fds.add(fd)
        return result

    def write(fd, data):
        if fd in fds and fault != "close":
            raise primary
        return original_write(fd, data)

    monkeypatch.setattr(runner, "OwnedDescriptor", owner)
    monkeypatch.setattr(runner.os, "write", write)
    with pytest.raises(BaseException) as caught:
        value.prepare_provenance(bound.wheel, binding)
    assert caught.value is (release if fault == "close" else primary)
    assert not (value.directory / "host-context.json").exists()
    assert len(owners) == len(released) == 1
    owners[0].close()
    assert len(released) == 1
    record = next(r for r in value.generated if r["path"] == "expected-origin.json")
    assert record["state"] == "failed"
    before = len(value.commands)
    with pytest.raises(ValueError, match="once-only"):
        value.prepare_provenance(bound.wheel, binding)
    assert len(value.commands) == before


@pytest.mark.parametrize("fault", ["truncated", "missing"])
def test_origin_readback_failure_prevents_context(workspace, monkeypatch, fault):
    value, bound, binding = workspace
    original = value._read

    def read(path):
        if path == value.directory / "expected-origin.json":
            if fault == "missing":
                raise FileNotFoundError("origin readback missing")
            return b'{"truncated":'
        return original(path)

    monkeypatch.setattr(value, "_read", read)
    with pytest.raises((OSError, ValueError)):
        value.prepare_provenance(bound.wheel, binding)
    assert not (value.directory / "host-context.json").exists()


def test_unsupported_launcher_is_not_attested(workspace):
    value, bound, binding = workspace
    script = value.venv / "bin/footman"
    payload = p.read(script)
    script.unlink()
    p.write(script, payload + b'print("UNSUPPORTED_LAUNCHER_EXECUTED")\n')
    with pytest.raises(ValueError, match="launcher semantics"):
        value.prepare_provenance(bound.wheel, binding)
    assert not (value.directory / "expected-origin.json").exists()


@pytest.mark.parametrize(
    "fault",
    ["none", "entry", "container", "supervision", "missing", "extra", "symlink"],
)
def test_full_installed_set_is_checked_without_import_before_help(
    workspace, runner, fault
):
    value, bound, binding = workspace
    manifest = value.prepare_provenance(bound.wheel, binding)
    package = Path(
        next(
            item["expected_path"]
            for item in manifest["modules"]
            if item["name"] == "footman"
        )
    ).parent
    if fault in {"entry", "container", "supervision"}:
        target = (
            package
            / {
                "entry": "__main__.py",
                "container": "container.py",
                "supervision": "supervision.py",
            }[fault]
        )
        target.unlink()
        p.write(target, b"raise RuntimeError('UNVERIFIED_SOURCE_EXECUTED')\n")
    elif fault == "missing":
        (package / "supervision.py").unlink()
    elif fault == "extra":
        p.write(package / "unexpected.py", b"# unexpected source\n")
    elif fault == "symlink":
        (package / "supervision.py").unlink()
        (package / "supervision.py").symlink_to("startup.py")
    before = len(value.commands)
    if fault == "none":
        runner._verify_installed(value, manifest)
    else:
        with pytest.raises(ValueError):
            runner._verify_installed(value, manifest)
    assert len(value.commands) == before
    assert not (value.directory / "provenance.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "schema-bool",
        "attestation-bool",
        "extra",
        "wrong-argv",
        "digest",
        "source",
        "missing",
    ],
)
def test_persisted_set_validation_is_not_satisfied_by_malformed_evidence(
    workspace, runner, fault
):
    # Explicitly synthetic file-format control, never service-child proof.
    value, bound, binding = workspace
    manifest = value.prepare_provenance(bound.wheel, binding)
    _, sources = p.manifest_set(manifest)
    attestation = {
        "schema_version": 1,
        "source_count": len(sources),
        "set_sha256": hashlib.sha256(p.canonical(sources)).hexdigest(),
    }
    payload = {
        **attestation,
        "sources": sources,
        "attestation": dict(attestation),
        "context": "wheel",
        "wheel_sha256": bound.wheel_sha256,
        "argv": [
            str(value.python),
            "-P",
            str(value.directory / "tests/mcp_server_child.py"),
            "--expected-origin",
            str(value.directory / "expected-origin.json"),
            "--entrypoint",
            str(value.venv / "bin/footman"),
            "--",
            "--config",
            "/external/config.toml",
        ],
    }
    if fault == "schema-bool":
        payload["schema_version"] = True
    elif fault == "attestation-bool":
        payload["attestation"]["schema_version"] = True
    elif fault == "extra":
        payload["unexpected"] = 1
    elif fault == "wrong-argv":
        payload["argv"].insert(-3, "--lifecycle")
    elif fault == "digest":
        payload["wheel_sha256"] = "0" * 64
    elif fault == "source":
        payload["sources"] = sources[:-1]
    if fault != "missing":
        p.write(value.directory / "provenance.json", p.canonical(payload))
    if fault == "none":
        assert runner._verified_evidence(value)
    else:
        with pytest.raises((OSError, ValueError)):
            runner._verified_evidence(value)
