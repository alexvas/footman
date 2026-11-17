"""Docker-free controls for authored host cleanup, never real Docker proof."""

import copy
from pathlib import Path

import pytest

from tests import mcp_provenance as p

ROOT = Path(__file__).resolve().parents[1]


def cleanup_support():
    assert (ROOT / "host_tests/cleanup.py").is_file(), "missing host cleanup producer"
    from host_tests import cleanup

    return cleanup


def test_host_cleanup_surface_is_authored_and_uses_shared_assertions():
    cleanup_support()
    source = p.read(ROOT / "host_tests/test_cleanup.py")
    assert b"tests import host_acceptance_assertions" in source
    assert b"test_timeout_survivor" in source
    assert b"test_generated_entries" in source
    assert b"test_snapshot_retention" in source


@pytest.mark.parametrize(
    "fault",
    ["none", "not-timeout", "present", "early-delete", "client-live", "survivor-dead"],
)
def test_shared_timeout_cleanup_verdict(fault):
    cleanup_support()
    from tests import host_acceptance_assertions as a

    record = {
        "outcome": "timed_out",
        "return_code": -15,
        "cleanup": {
            "confirmed_absent": True,
            "snapshot_deleted": True,
            "unresolved_resources": False,
            "resources_complete": True,
            "failure": None,
        },
    }
    observations = {
        "local_client_exited": True,
        "survivor_alive": True,
        "snapshot_before_removal": True,
        "snapshot_after_confirmation": True,
        "snapshot_after_run": False,
    }
    if fault == "not-timeout":
        record["outcome"] = "command_failed"
    elif fault == "present":
        record["cleanup"]["confirmed_absent"] = False
    elif fault == "early-delete":
        observations["snapshot_after_confirmation"] = False
    elif fault == "client-live":
        observations["local_client_exited"] = False
    elif fault == "survivor-dead":
        observations["survivor_alive"] = False
    if fault == "none":
        a.verify_timeout_cleanup(record, observations)
    else:
        with pytest.raises(AssertionError):
            a.verify_timeout_cleanup(record, observations)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "success",
        "absent",
        "no-failure",
        "unsettled",
        "status",
        "unknown",
        "empty-failure",
    ],
)
def test_shared_safe_retention_is_not_runtime_success(fault):
    cleanup_support()
    from tests import host_acceptance_assertions as a

    manifest = {
        "outcome": "dispatch_failed",
        "return_code": 0,
        "cleanup": {
            "confirmed_absent": True,
            "snapshot_deleted": False,
            "unresolved_resources": False,
            "resources_complete": True,
            "failure": {"type": "PermissionError", "message": "denied"},
        },
    }
    retained = True
    if fault == "success":
        manifest["outcome"] = "succeeded"
    elif fault == "absent":
        retained = False
    elif fault == "no-failure":
        manifest["cleanup"]["failure"] = None
    elif fault == "unsettled":
        manifest["cleanup"]["resources_complete"] = False
    elif fault == "status":
        manifest["return_code"] = 17
    elif fault == "unknown":
        manifest["outcome"] = None
    elif fault == "empty-failure":
        manifest["cleanup"]["failure"] = {}
    before = copy.deepcopy(manifest)
    if fault == "none":
        a.verify_safe_retention(manifest, snapshot_present=retained)
    else:
        with pytest.raises(AssertionError):
            a.verify_safe_retention(manifest, snapshot_present=retained)
    assert manifest == before


@pytest.mark.parametrize(
    "kind, mode, fault",
    [
        ("image", "timeout", None),
        ("wrapper", "complete", None),
        ("image", "complete", "permission"),
        ("image", "complete", "owner"),
    ],
)
def test_native_retained_run_orchestrates_cleanup_and_explicit_reconciliation(
    tmp_path, monkeypatch, kind, mode, fault
):
    from tests import host_acceptance_assertions as a
    from tests import host_cleanup_controls as controls

    producer, flow, boundary, baseline = controls.prepare(tmp_path, monkeypatch)
    assert len(baseline) == 2  # Explicit synthetic SDK baseline only.
    record = producer.run(kind, mode, fault)
    assert record["complete"] and record["reconciled"]
    if mode == "timeout":
        a.verify_timeout_cleanup(record["manifest"], record["observations"])
    elif fault is None:
        assert record["manifest"]["outcome"] == "succeeded"
    else:
        a.verify_safe_retention(
            record["manifest"],
            snapshot_present=record["observations"]["snapshot_after_run"],
        )
        assert record["manifest"]["outcome"] == "dispatch_failed"
        assert record["snapshot"].removed
    a.verify_generated_entries(record["observations"]["generated"])
    assert producer.sentinel_tree.path.exists() and boundary.live_sentinel
    assert [argv[1] for argv, _ in boundary.calls] == [
        "run",
        "container",
        "exec",
        "cp",
        "exec",
        "rm",
        "container",
    ]
    assert all(helper.collections == helper.exits == 1 for helper in boundary.helpers)
    assert all(kwargs["deadline_seconds"] > 0 for _, kwargs in boundary.calls)
    snapshot = record["snapshot"].witness["entries"]
    assert snapshot["payload"]["sha256"] != snapshot["binary"]["sha256"]
    if fault:
        assert snapshot["host-sentinel-link"]["kind"] == "link"
    saved = p.load(Path(record["scenario"]) / "cleanup-observations.json")
    assert saved["complete"] and saved["reconciled"]
    producer.finish()
    producer.finish()
    flow.finish()
    assert not boundary.live_sentinel and not producer.sentinel_tree.path.exists()
    assert not p.load(producer.root / "summary.json")["unresolved"]


@pytest.mark.parametrize(
    "fault",
    ["uid", "file-uid", "link-uid", "bytes", "regular", "symlink", "target", "payload"],
)
def test_generated_entry_assertions_reject_synthetic_mismatches(fault):
    from tests import host_acceptance_assertions as a
    from tests.host_cleanup_controls import generated

    value = generated()
    a.verify_generated_entries(value)
    key, replacement = {
        "uid": ("uid", 1000),
        "file-uid": ("file_uid", 1000),
        "link-uid": ("link_uid", 1000),
        "bytes": ("file_bytes", "eA=="),
        "regular": ("file_regular", False),
        "symlink": ("link_is_symlink", False),
        "target": ("link_target", "wrong"),
        "payload": ("payload", "eA=="),
    }[fault]
    value[key] = replacement
    with pytest.raises(AssertionError):
        a.verify_generated_entries(value)


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_witness_file_owner_failure_precedence_and_repeat(tmp_path, monkeypatch, fault):
    import os

    from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
    from tests.host_cleanup_controls import standalone_tree

    tree, sentinel = standalone_tree(tmp_path)
    primary = (
        KeyboardInterrupt("witness interrupted")
        if fault == "interruption-close"
        else OSError("witness read failed")
    )
    secondary = OSError("witness close failed")
    read, close, directory_close = (
        os.read,
        OwnedDescriptor.close,
        DirectoryDescriptor.close,
    )
    releases, directories = [], []
    target = tree.path / "file"

    def selected(fd):
        return Path(os.readlink(f"/proc/self/fd/{fd}")) == target

    def reading(fd, size):
        if selected(fd) and fault != "close":
            raise primary
        return read(fd, size)

    def releasing(owner):
        picked = not owner.released and selected(owner.fd)
        close(owner)
        if picked:
            releases.append(owner)
            raise secondary

    def close_directory(owner):
        if not owner.released:
            directories.append(owner)
        return directory_close(owner)

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", reading)
        patch.setattr(OwnedDescriptor, "close", releasing)
        patch.setattr(DirectoryDescriptor, "close", close_directory)
        with pytest.raises(BaseException) as caught:
            tree.finish()
        assert caught.value is (secondary if fault == "close" else primary)
        count = len(directories)
        with pytest.raises(BaseException) as repeated:
            tree.finish()
        assert repeated.value is caught.value
        assert len(releases) == 1 and len(directories) == count
        assert all(owner.released for owner in directories)
    assert tree.path.exists() and p.read(sentinel / "keep") == b"untouched"


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_witness_directory_release_and_repeat(tmp_path, monkeypatch, fault):
    import os

    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests.host_cleanup_controls import standalone_tree

    tree, sentinel = standalone_tree(tmp_path)
    primary = (
        KeyboardInterrupt("directory interrupted")
        if fault == "interruption-close"
        else OSError("directory list failed")
    )
    secondary = OSError("directory close failed")
    listing, close = DirectoryDescriptor.list_names, DirectoryDescriptor.close
    target = tree.path / "directory"
    releases = []

    def names(owner):
        if (
            Path(os.readlink(f"/proc/self/fd/{owner.fd}")) == target
            and fault != "close"
        ):
            raise primary
        return listing(owner)

    def release(owner):
        path = (
            None if owner.released else Path(os.readlink(f"/proc/self/fd/{owner.fd}"))
        )
        close(owner)
        if path is not None:
            releases.append((owner, path))
        if path == target:
            raise secondary

    with monkeypatch.context() as patch:
        patch.setattr(DirectoryDescriptor, "list_names", names)
        patch.setattr(DirectoryDescriptor, "close", release)
        with pytest.raises(BaseException) as caught:
            tree.finish()
        assert caught.value is (secondary if fault == "close" else primary)
        before = list(releases)
        with pytest.raises(BaseException) as repeated:
            tree.finish()
        assert repeated.value is caught.value and releases == before
        assert sum(path == target for _, path in releases) == 1
        assert any(path == tree.path.parent for _, path in releases)
    assert p.read(sentinel / "keep") == b"untouched"


@pytest.mark.parametrize("mutation", ["extra", "changed", "link", "root-replaced"])
def test_fixture_reconciliation_refuses_changed_allowlist(tmp_path, mutation):
    import os

    from tests.host_cleanup_controls import standalone_tree

    tree, sentinel = standalone_tree(tmp_path)
    if mutation == "extra":
        p.write(tree.path / "unknown", b"not owned by the witness")
    elif mutation == "changed":
        (tree.path / "file").unlink()
        p.write(tree.path / "file", b"changed")
    elif mutation == "link":
        (tree.path / "outward").unlink()
        (tree.path / "outward").symlink_to(tmp_path)
    else:
        tree.path.rename(tmp_path / "retained-original")
        tree.path.mkdir()
    with pytest.raises(AssertionError):
        tree.finish()
    assert os.path.lexists(tree.path)
    assert p.read(sentinel / "keep") == b"untouched"
    assert tree.attempted and not tree.removed


def test_fixture_witness_refuses_special_nodes_without_opening_them(tmp_path):
    import os

    from host_tests.cleanup_paths import RecordedTree, identity

    root = tmp_path / "owned"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(AssertionError, match="unsupported cleanup witness entry"):
        RecordedTree(root, identity(root.stat()))


def test_fixture_witness_rejects_replaced_parent(tmp_path):
    from host_tests import execution as e
    from tests.host_cleanup_controls import standalone_tree

    parent = tmp_path / "parent"
    e.mkdir(parent)
    tree, _ = standalone_tree(parent)
    parent.rename(tmp_path / "original-parent")
    e.mkdir(parent)
    e.mkdir(parent / "owned")
    with pytest.raises(AssertionError, match="parent replaced"):
        tree.finish()
    assert (parent / "owned").exists()
    assert p.read(tmp_path / "original-parent/unrelated/keep") == b"untouched"


def test_fixture_path_reconciliation_unlinks_not_follows_sentinel(tmp_path):
    from tests.host_cleanup_controls import standalone_tree

    tree, sentinel = standalone_tree(tmp_path)
    tree.finish()
    tree.finish()
    assert tree.removed and not tree.path.exists()
    assert p.read(sentinel / "keep") == b"untouched"


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_retained_run_output_handoff_has_one_release_authority(
    tmp_path, monkeypatch, fault
):
    from footman.filesystem.descriptors import OwnedDescriptor
    from host_tests import cleanup as c
    from tests import host_cleanup_controls as controls

    producer, flow, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    primary = (
        KeyboardInterrupt("constructor interrupted")
        if fault == "interruption-close"
        else OSError("constructor failed")
    )
    secondary = OSError("retained stdout close failed")
    released = []
    close = OwnedDescriptor.close
    constructor = c.RetainedRun

    def release(owner):
        picked = not owner.released and owner.label.startswith("retained output ")
        label = owner.label
        close(owner)
        if picked:
            released.append(owner)
            if label == "retained output stdout.txt":
                raise secondary

    def construct(*args, **kwargs):
        if fault != "close":
            raise primary
        return constructor(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(OwnedDescriptor, "close", release)
        patch.setattr(c, "RetainedRun", construct)
        with pytest.raises(BaseException) as caught:
            producer.run("image", "complete")
    if fault != "close":
        assert caught.value is primary
    else:
        assert producer.runs[0]["run"].failure is secondary
        assert "evidence retention/publication" in str(caught.value)
    assert len(released) == 5 and all(owner.released for owner in released)
    with pytest.raises(BaseException) as settled:
        producer.finish(caught.value)
    assert settled.value is caught.value
    assert not boundary.live_sentinel and not producer.sentinel_tree.path.exists()
    with pytest.raises(BaseException) as repeated:
        producer.finish()
    assert repeated.value is caught.value and len(released) == 5


@pytest.mark.parametrize("interrupt", [False, True])
def test_cleanup_observation_retention_cannot_replace_active_failure(
    tmp_path, monkeypatch, interrupt
):
    from host_tests import cleanup as c
    from tests import host_cleanup_controls as controls

    producer, _, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    primary = (
        KeyboardInterrupt("local run interrupted")
        if interrupt
        else OSError("local run failed")
    )
    secondary = OSError("observation retention failed")
    writes = []
    write = p.write

    def construct(*args, **kwargs):
        raise primary

    def retain(path, data):
        if path.name == "cleanup-observations.json":
            writes.append(path)
            raise secondary
        return write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(c, "RetainedRun", construct)
        patch.setattr(p, "write", retain)
        with pytest.raises(BaseException) as caught:
            producer.run("image", "complete")
    assert caught.value is primary and len(writes) == 1
    assert secondary in getattr(primary, "_transaction_secondary", [])
    with pytest.raises(BaseException) as finished:
        producer.finish(primary)
    assert finished.value is primary
    assert not boundary.live_sentinel


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_docker_scope_handoff_preserves_failure_and_independent_settlement(
    tmp_path, monkeypatch, fault
):
    from tests import host_cleanup_controls as controls

    producer, _, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    boundary.fail = fault
    if fault == "interruption-close":
        boundary.operation_error = KeyboardInterrupt("synthetic stream interruption")
    with pytest.raises(BaseException) as caught:
        producer.run("image", "complete")
    expected = boundary.close_error if fault == "close" else boundary.operation_error
    assert producer.runs[0]["run"].failure is expected
    if fault == "interruption-close":
        assert caught.value is expected
    assert all(helper.exits == 1 for helper in boundary.helpers)
    helpers = list(boundary.helpers)
    with pytest.raises(BaseException) as finished:
        producer.finish(caught.value)
    assert finished.value is caught.value
    assert not boundary.live_sentinel
    assert producer.sentinel_tree.removed
    summary = p.load(producer.root / "summary.json")
    assert summary["unresolved"] == [producer.runs[0]["scenario"]]
    with pytest.raises(BaseException) as repeated:
        producer.finish()
    assert repeated.value is caught.value
    assert boundary.helpers == helpers and all(helper.exits == 1 for helper in helpers)


def test_reconciliation_attempts_independent_paths_and_retains_latched_failure(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from host_tests import execution as e
    from tests import host_cleanup_controls as controls

    producer, _, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    e.mkdir(first_root)
    e.mkdir(second_root)
    first, _ = controls.standalone_tree(first_root)
    second, _ = controls.standalone_tree(second_root)
    failure = OSError("first fixture deletion failed")
    calls = []
    finish = first.finish

    def failed():
        calls.append(True)
        raise failure

    monkeypatch.setattr(first, "finish", failed)
    runtime = SimpleNamespace(
        supervisor=SimpleNamespace(
            cleanup_status=SimpleNamespace(complete=True),
            cleanup=SimpleNamespace(confirmed_absent=True),
        ),
        snapshot=SimpleNamespace(processes=[]),
    )
    for tree in (first, second):
        producer.runs.append(
            {
                "scenario": str(tree.path),
                "run": runtime,
                "owners": [],
                "commands": SimpleNamespace(owners=[]),
                "snapshot": tree,
                "complete": True,
                "reconciled": False,
                "fault": None,
            }
        )
    with pytest.raises(OSError) as caught:
        producer.finish()
    assert caught.value is failure
    assert first.path.exists() and not second.path.exists()
    assert not boundary.live_sentinel and producer.sentinel_tree.removed
    with pytest.raises(OSError) as repeated:
        producer.finish()
    assert repeated.value is failure and calls == [True]
    monkeypatch.setattr(first, "finish", finish)
    finish()  # Explicit test-only repair of the injected boundary, not a retry by the producer.
    assert not first.path.exists()


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_allowlisted_deletion_failure_has_terminal_owners(tmp_path, monkeypatch, fault):
    import os

    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests.host_cleanup_controls import standalone_tree

    tree, sentinel = standalone_tree(tmp_path)
    primary = (
        KeyboardInterrupt("unlink interrupted")
        if fault == "interruption-close"
        else OSError("unlink failed")
    )
    secondary = OSError("delete directory close failed")
    unlink, close = DirectoryDescriptor.unlink_child, DirectoryDescriptor.close
    active = False
    released = []

    def unlink_child(owner, name):
        nonlocal active
        active = True
        if fault != "close":
            raise primary
        return unlink(owner, name)

    def release(owner):
        path = (
            None if owner.released else Path(os.readlink(f"/proc/self/fd/{owner.fd}"))
        )
        close(owner)
        if path is not None:
            released.append(path)
        if active and path == tree.path / "directory":
            raise secondary

    with monkeypatch.context() as patch:
        patch.setattr(DirectoryDescriptor, "unlink_child", unlink_child)
        patch.setattr(DirectoryDescriptor, "close", release)
        with pytest.raises(BaseException) as caught:
            tree.finish()
        assert caught.value is (secondary if fault == "close" else primary)
        before = list(released)
        with pytest.raises(BaseException) as repeated:
            tree.finish()
        assert repeated.value is caught.value and released == before
    assert active and tree.path.exists() and not tree.removed
    assert p.read(sentinel / "keep") == b"untouched"


@pytest.mark.parametrize(
    "fault",
    ["wrong-rm", "wrong-list", "nonzero", "read-failed", "nonempty", "extra-field"],
)
def test_cleanup_protocol_negative_controls_use_native_manifests(
    tmp_path, monkeypatch, fault
):
    import base64

    from tests import host_acceptance_assertions as a
    from tests import host_cleanup_controls as controls

    producer, flow, _, _ = controls.prepare(tmp_path, monkeypatch)
    record = producer.run("wrapper", "complete")
    manifest = copy.deepcopy(record["manifest"])
    operations = manifest["infrastructure_operations"]
    if fault == "wrong-rm":
        operations[-2]["argv"][-1] = controls.SENTINEL
    elif fault == "wrong-list":
        operations[-1]["argv"][6] = "id=" + controls.SENTINEL
    elif fault == "nonzero":
        operations[-1]["return_code"] = 17
    elif fault == "read-failed":
        operations[-1]["stdout"]["completeness_name"] = "READ_FAILED"
    elif fault == "nonempty":
        operations[-1]["stdout"].update(
            observed_bytes=65,
            excerpt_base64=base64.b64encode((controls.ID + "\n").encode()).decode(),
        )
    else:
        operations[-1]["argv"].append("unexpected")
    with pytest.raises(AssertionError):
        a.verify_cleanup_protocol(manifest)
    producer.finish()
    flow.finish()


@pytest.mark.parametrize("fail", [False, True])
def test_copied_host_cleanup_bodies_and_failure_handling_are_docker_free(
    tmp_path, fail
):
    from tests.host_cleanup_controls import copied_cleanup_profile

    workspace, result = copied_cleanup_profile(tmp_path, fail=fail)
    assert result["status"] == (1 if fail else 0), result
    assert p.read(workspace / "host_tests/test_cleanup.py") == p.read(
        ROOT / "host_tests/test_cleanup.py"
    )
    output = result["stdout"] + result["stderr"]
    assert (b"2 failed, 1 passed" if fail else b"3 passed") in output, result
    if fail:
        assert b"generated entries not root-owned" in output, result


@pytest.mark.parametrize("state", ["unreconciled", "success", "no-failure", "valid"])
def test_host_flow_safe_retention_exception_requires_verified_reconciliation(
    tmp_path, state
):
    import time

    from host_tests.execution import HostFlow

    flow = HostFlow(
        {
            "support_root": str(tmp_path),
            "service_deadline_monotonic": time.monotonic() + 30,
        }
    )
    record = {
        "directory": str(flow.root / "case"),
        "complete": True,
        "expected_retention": True,
        "fixture_reconciled": True,
        "runtime_outcome": "dispatch_failed",
        "cleanup": {
            "confirmed_absent": True,
            "snapshot_deleted": False,
            "resources_complete": True,
            "unresolved_resources": False,
            "failure": {"type": "PermissionError"},
        },
    }
    if state == "unreconciled":
        record["fixture_reconciled"] = False
    elif state == "success":
        record["runtime_outcome"] = "succeeded"
    elif state == "no-failure":
        record["cleanup"]["failure"] = None
    flow.cases.append(record)
    if state == "valid":
        flow.finish()
    else:
        with pytest.raises(AssertionError, match="scenario incomplete"):
            flow.finish()


@pytest.mark.parametrize("publication", ["operation", "interruption"])
def test_cleanup_summary_publication_failure_remains_unsuccessful_and_once_only(
    tmp_path, monkeypatch, publication
):
    from tests import host_cleanup_controls as controls

    producer, flow, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    producer.run("wrapper", "complete")
    error = (
        KeyboardInterrupt("summary interrupted")
        if publication == "interruption"
        else OSError("summary unavailable")
    )
    writes = []
    write = p.write

    def publish(path, data):
        if path == producer.root / "summary.json":
            writes.append(path)
            raise error
        return write(path, data)

    monkeypatch.setattr(p, "write", publish)
    with pytest.raises(BaseException) as caught:
        producer.finish()
    assert (
        caught.value is error
        and not boundary.live_sentinel
        and producer.sentinel_tree.removed
    )
    with pytest.raises(BaseException) as repeated:
        producer.finish()
    assert repeated.value is error and len(writes) == 1
    flow.finish()


def test_missing_sentinel_identity_is_unresolved_and_never_guessed(
    tmp_path, monkeypatch
):
    from tests import host_cleanup_controls as controls

    producer, _, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    producer.sentinel["id"] = None  # Explicit synthetic lost-identity observation.
    before = list(boundary.command_calls)
    with pytest.raises(AssertionError, match="unknown sentinel launch"):
        producer.finish()
    assert boundary.command_calls == before
    summary = p.load(producer.root / "summary.json")
    assert summary["sentinel"]["attempted"] and not summary["sentinel"]["removed"]
    assert summary["sentinel_path_removed"]


def test_observer_spending_cannot_renew_original_helper_budget(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    from host_tests import cleanup as c
    from tests import host_cleanup_controls as controls

    producer, _, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    clock = time.monotonic
    spent = [0.0]
    monkeypatch.setattr(
        c, "time", SimpleNamespace(monotonic=lambda: clock() + spent[0])
    )
    command = boundary.command

    def observe(argv, root):
        result = command(argv, root)
        if argv[1] == "exec":
            spent[0] += 1000
        return result

    monkeypatch.setattr(boundary, "command", observe)
    with pytest.raises(AssertionError):
        producer.run("wrapper", "complete")
    assert not any(argv[1:3] == ["rm", "-f"] for argv, _ in boundary.calls)
    failure = producer.runs[0]["run"].failure
    assert "exhausted original helper deadline" in str(failure)
    with pytest.raises(AssertionError, match="fixture incomplete"):
        producer.finish()
    assert not boundary.live_sentinel


def test_cleanup_probe_programs_compile_without_execution():
    from host_tests import cleanup as c

    compile(c.PROBE, "<container-generated-entries>", "exec")
    compile(c.OBSERVE, "<container-cleanup-observation>", "exec")


def test_sentinel_must_remain_running_not_merely_listed(tmp_path, monkeypatch):
    from tests import host_cleanup_controls as controls

    producer, _, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    boundary.sentinel_status = "exited"
    with pytest.raises(AssertionError, match="sentinel container changed") as caught:
        producer.check_sentinel()
    with pytest.raises(AssertionError) as finished:
        producer.finish(caught.value)
    assert finished.value is caught.value
    assert not boundary.live_sentinel and producer.sentinel_tree.removed


@pytest.mark.parametrize("kind", ["ORDINARY", "UNEXPECTED"])
def test_settled_owner_failure_does_not_suppress_independent_domain_reconciliation(
    tmp_path, monkeypatch, kind
):
    from footman.process import FailureInfo, FailureKind, FailurePhase, LifecycleResult
    from tests import host_cleanup_controls as controls

    producer, flow, boundary, _ = controls.prepare(tmp_path, monkeypatch)
    record = producer.run("wrapper", "complete")
    error = OSError("settled action retained a failure")
    record["command_owner"].cleanup_status = LifecycleResult(
        True, (FailureInfo(error, FailureKind[kind], FailurePhase.ACTION),)
    )
    # Explicitly synthetic late-domain observation, never a real container claim.
    record["run"].supervisor.cleanup.confirmed_absent = False
    primary = OSError("active caller failure")
    with pytest.raises(OSError) as caught:
        producer.finish(primary)
    assert caught.value is (primary if kind == "ORDINARY" else error)
    assert boundary.command_calls.count(["docker", "rm", "-f", controls.ID]) == 1
    assert record["reconciled"] and not boundary.live_sentinel
    summary = p.load(producer.root / "summary.json")
    assert summary["owner_failures"] == [
        {"kind": kind, "phase": "ACTION", "type": "OSError"}
    ]
    assert summary["unresolved"] == []
    flow.finish()
