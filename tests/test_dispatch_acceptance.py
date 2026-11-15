"""Docker-free integrated acceptance; stub observations are not Docker proofs."""

import os
import time
from pathlib import Path

import anyio
import pytest

from tests import acceptance_attribution_support as attribution
from tests import mcp_attribution_support as reference_attribution
from tests import mcp_contract_support as service
from tests import mcp_provenance as provenance
from tests.dispatcher_harness import write_bytes
from tests.test_green_gate import assert_documented_suite
from tests.wheel_attribution_cases import CASES


@pytest.mark.parametrize(
    "boundary", ["construct", "start", "start-after", "body", "success"]
)
@pytest.mark.parametrize("cleanup_fault", ["none", "shutdown", "join"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_loss_collector_failure_authority_and_independent_cleanup(
    tmp_path, monkeypatch, boundary, cleanup_fault, interrupted
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    support = portability()
    primary = (
        KeyboardInterrupt("collector interrupted")
        if interrupted
        else OSError("collector failed")
    )
    release = OSError("helper shutdown failed")
    joining = OSError("collector join failed")
    owner = SimpleNamespace(shutdown=Mock())
    owner.shutdown.side_effect = release if cleanup_fault == "shutdown" else None
    owner.shutdown.return_value = SimpleNamespace(complete=True, failures=())
    thread = SimpleNamespace(ident=None, join=Mock(), is_alive=lambda: False)
    thread.join.side_effect = joining if cleanup_fault == "join" else None

    def start():
        if boundary == "start":
            raise primary
        thread.ident = 1
        if boundary == "start-after":
            raise primary

    thread.start = start
    factory = (
        Mock(side_effect=primary)
        if boundary == "construct"
        else Mock(return_value=thread)
    )
    launch = Mock(return_value=owner)
    monkeypatch.setattr(support, "threading", SimpleNamespace(Thread=factory))
    monkeypatch.setattr(support, "OwnedProcess", SimpleNamespace(launch=launch))
    scope = support.loss_collection(
        {"argv": [], "neutral": tmp_path, "env": {}}, Mock()
    )
    caught = None
    try:
        with scope:
            if boundary == "body":
                raise primary
    except BaseException as error:
        caught = error
    joined = boundary in {"start-after", "body", "success"}
    expected = (
        primary
        if boundary != "success"
        else (
            release
            if cleanup_fault == "shutdown"
            else joining
            if cleanup_fault == "join" and joined
            else None
        )
    )
    assert caught is expected
    owner.shutdown.assert_called_once_with()
    assert thread.join.call_count == int(joined)
    if joined:
        thread.join.assert_called_once_with(timeout=2)
    if caught is primary:
        secondary = getattr(caught, "_transaction_secondary", ())
        if cleanup_fault == "shutdown":
            assert release in secondary
        if cleanup_fault == "join" and joined:
            assert joining in secondary
    # Re-entering cleanup on the exhausted context must not repeat either action.
    scope.__exit__(None, None, None)
    owner.shutdown.assert_called_once_with()
    assert thread.join.call_count == int(joined)


@pytest.mark.parametrize("setup", ["construct", "start"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_exercise_loss_protects_owner_at_collector_setup(
    tmp_path, monkeypatch, setup, interrupted
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    support = portability()
    primary = (
        KeyboardInterrupt("setup interrupted")
        if interrupted
        else OSError("setup failed")
    )
    owner = SimpleNamespace(
        shutdown=Mock(return_value=SimpleNamespace(complete=True, failures=()))
    )
    thread = SimpleNamespace(ident=None, start=Mock(side_effect=primary))
    factory = (
        Mock(side_effect=primary) if setup == "construct" else Mock(return_value=thread)
    )
    case = {"argv": [], "neutral": tmp_path, "env": {}}
    monkeypatch.setattr(support, "prepare_case", lambda *_: case)
    monkeypatch.setattr(support, "threading", SimpleNamespace(Thread=factory))
    monkeypatch.setattr(
        support, "OwnedProcess", SimpleNamespace(launch=lambda *_, **__: owner)
    )
    with pytest.raises(type(primary)) as caught:
        support.exercise_loss(None, tmp_path, "before-launch")
    assert caught.value is primary
    owner.shutdown.assert_called_once_with()


@pytest.mark.parametrize("boundary", ["normal", "operation", "interruption"])
@pytest.mark.parametrize(
    "fault", ["incomplete-owner", "failed-owner", "live-collector"]
)
def test_loss_collector_incomplete_cleanup_retains_original_objects(
    tmp_path, monkeypatch, boundary, fault
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    support = portability()
    primary = (
        KeyboardInterrupt("interrupted")
        if boundary == "interruption"
        else OSError("operation")
    )
    cleanup = SimpleNamespace(
        complete=fault != "incomplete-owner",
        failures=(OSError("close"),) if fault == "failed-owner" else (),
    )
    owner = SimpleNamespace(shutdown=Mock(return_value=cleanup))
    thread = SimpleNamespace(
        ident=1, start=Mock(), join=Mock(), is_alive=lambda: fault == "live-collector"
    )
    monkeypatch.setattr(
        support, "threading", SimpleNamespace(Thread=lambda **_: thread)
    )
    monkeypatch.setattr(
        support, "OwnedProcess", SimpleNamespace(launch=lambda *_, **__: owner)
    )
    scope = support.loss_collection(
        {"argv": [], "neutral": tmp_path, "env": {}}, Mock()
    )
    with pytest.raises(
        AssertionError if boundary == "normal" else type(primary)
    ) as caught:
        with scope:
            if boundary != "normal":
                raise primary
    failure = (
        caught.value if boundary == "normal" else caught.value._transaction_secondary[0]
    )
    if boundary != "normal":
        assert caught.value is primary
    assert isinstance(failure, AssertionError)
    assert failure.args[1] is owner
    assert failure.args[2] is (thread if fault == "live-collector" else cleanup)
    owner.shutdown.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=2)
    scope.__exit__(None, None, None)
    owner.shutdown.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=2)


@pytest.mark.parametrize("after_start", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_loss_collector_start_failure_settles_real_owner(
    tmp_path, monkeypatch, after_start, interrupted
):
    import sys
    import threading
    from types import SimpleNamespace

    from footman.process import OwnedProcess
    from tests import acceptance_build_support as build

    support = portability()
    primary = (
        KeyboardInterrupt("start interrupted")
        if interrupted
        else OSError("start failed")
    )
    acquired, threads, collected_errors = [], [], []
    entered = threading.Event()
    real_launch = OwnedProcess.launch

    def launch(*args, **kwargs):
        owner = real_launch(*args, **kwargs)
        acquired.append(owner)
        return owner

    class Collector(threading.Thread):
        def start(self):
            threads.append(self)
            if after_start:
                super().start()
                if not entered.wait(5):
                    raise AssertionError("real collector never entered")
            raise primary

    def collect(owner):
        entered.set()
        try:
            owner.stream(lambda *_: None)
        except BaseException as error:
            collected_errors.append(error)

    monkeypatch.setattr(support, "threading", SimpleNamespace(Thread=Collector))
    monkeypatch.setattr(support, "OwnedProcess", SimpleNamespace(launch=launch))
    case = {
        "argv": [sys.executable, "-P", "-c", "import time; time.sleep(60)"],
        "neutral": tmp_path,
        "env": build.clean_environment(),
    }
    try:
        with pytest.raises(type(primary)) as caught:
            with support.loss_collection(case, collect):
                raise AssertionError("failed start yielded its owner")
        assert caught.value is primary
        assert len(acquired) == len(threads) == 1
        owner = acquired[0]
        assert owner.cleanup_status.complete and not owner.cleanup_status.failures
        assert not threads[0].is_alive()
        before = owner.cleanup_status
        assert owner.shutdown() == before
        assert owner.wait_cleanup(0.1) == before
    finally:
        # Contain a failing regression, using only the same process owner.
        for owner in acquired:
            if not owner.cleanup_status.complete:
                owner.shutdown()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2)


@pytest.mark.parametrize(
    "mode",
    [
        "attribution-id",
        "attribution-inventory",
        "attribution-status",
        "attribution-stability",
        "build-identity",
        "unresolved-worker",
        "journal-token",
        "observed-token",
        "missing-remove",
        "unsuccessful-call",
        "oversized-result",
        "incomplete-cleanup",
        "reserved-helper-output",
        "wrong-id",
        "duplicate-cleanup",
        "mismatched-filter",
    ],
)
def test_verification_helpers_reject_invalid_evidence_under_optimization(
    tmp_path, mode
):
    import sys

    from tests import acceptance_build_support as build

    outcome = build.command(
        [
            sys.executable,
            "-O",
            "-m",
            "tests.acceptance_verification_probe",
            mode,
            str(tmp_path),
        ],
        cwd=ROOT,
        env=build.clean_environment(),
        timeout_seconds=15,
    )
    assert outcome["status"] == 0, outcome
    assert outcome["stdout"] == (mode + ": rejected under -O\n").encode()
    assert outcome["stderr"] == b""


def test_phase10_verification_modules_have_no_optimizable_assert_statements():
    import ast

    for module in (
        "acceptance_portability_support",
        "acceptance_attribution_support",
        "build_failure_support",
        "acceptance_verification_probe",
    ):
        tree = ast.parse(provenance.read(ROOT / "tests" / (module + ".py")))
        assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree)), module


def _recorded_cleanup_case(tmp_path):
    records = tmp_path / "stub-records"
    records.mkdir()
    case = {
        "tmp": tmp_path,
        "request": {
            "journal_path": str(tmp_path / "journal.json"),
            "transport_deadline_monotonic": time.monotonic() + 5,
            "lifecycle_binding": {"instance_token": "bound"},
        },
    }
    provenance.write(
        tmp_path / "journal.json",
        provenance.canonical({"instance_token": "bound"}) + b"\n",
    )
    for index, identity in enumerate(("a" * 64, "b" * 64)):
        for operation, argv in (
            ("launch", ["run", "--detach=true", "--pull=never", "--", "image"]),
            ("remove", ["rm", "-f", identity]),
            (
                "confirm",
                [
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "id=" + identity,
                    "--format",
                    "{{.ID}}",
                ],
            ),
        ):
            provenance.write(
                records / f"{index}-{operation}-call.json",
                provenance.canonical(
                    {
                        "argv": argv,
                        "container_id": identity if operation != "confirm" else None,
                        "status": 0,
                        "stdin_eof": True,
                    }
                ),
            )
        result = tmp_path / "results" / str(index)
        result.mkdir(parents=True)
        provenance.write(
            result / "result.json",
            provenance.canonical(
                {
                    "container_id": identity,
                    "cleanup": {
                        "confirmed_absent": True,
                        "snapshot_deleted": True,
                        "unresolved_resources": False,
                    },
                    "snapshot": {"path": str(tmp_path / "snapshots" / str(index))},
                }
            ),
        )
    return case


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-id",
        "duplicate-cleanup",
        "mismatched-filter",
        "manifest-id",
        "duplicate-manifest",
        "duplicate-launch-id",
        "extra-remove-argument",
        "extra-confirm-argument",
    ],
)
def test_reconciliation_requires_exact_recorded_id_cleanup_and_manifest(
    tmp_path, monkeypatch, fault
):
    support = portability()
    case = _recorded_cleanup_case(tmp_path)
    monkeypatch.setattr(support, "incarnation", lambda _: "absent")
    assert support.reconcile(case)["resources_reconciled"]
    name = (
        "1-launch-call.json"
        if fault == "duplicate-launch-id"
        else (
            "1-confirm-call.json"
            if fault in {"mismatched-filter", "extra-confirm-argument"}
            else "1-remove-call.json"
        )
    )
    path = tmp_path / "stub-records" / name
    if fault in {"manifest-id", "duplicate-manifest"}:
        path = tmp_path / "results/1/result.json"
    value = provenance.load(path)
    if fault in {"wrong-id", "duplicate-cleanup"}:
        target = ("c" if fault == "wrong-id" else "a") * 64
        value["argv"][-1] = target
        # Reproduce the circular stub: metadata agrees with the wrong received
        # argv, not with this invocation's independently recorded launch ID.
        value["container_id"] = target
    elif fault == "mismatched-filter":
        value["argv"][5] = "id=" + "c" * 64
    elif fault in {"manifest-id", "duplicate-manifest", "duplicate-launch-id"}:
        value["container_id"] = ("c" if fault == "manifest-id" else "a") * 64
    else:
        value["argv"].append("--unexpected")
    write_bytes(path, provenance.canonical(value))
    # All controls preserve the former count-only success criteria.
    calls = [
        provenance.load(tmp_path / "stub-records" / f"{index}-{operation}-call.json")
        for index in range(2)
        for operation in ("launch", "remove", "confirm")
    ]
    assert sum(item["argv"][0] == "run" for item in calls) == 2
    assert sum(item["argv"][:2] == ["rm", "-f"] for item in calls) == 2
    assert sum(item["argv"][:2] == ["container", "ls"] for item in calls) == 2
    assert len(support.run_resources(tmp_path)) == 2
    assert all(item["status"] == 0 and item["stdin_eof"] for item in calls)
    with pytest.raises(AssertionError, match="recorded-ID|launch identity"):
        support.reconcile(case)


def portability():
    import importlib

    return importlib.import_module("tests.acceptance_portability_support")


@pytest.fixture(scope="session")
def relocated_worker_provider(tmp_path_factory, worker_id):
    with portability().worker_scope(
        ROOT, lambda: tmp_path_factory.mktemp("phase10-" + worker_id), worker_id
    ) as obtain:
        yield obtain


@pytest.fixture(scope="module")
def relocated_worker(relocated_worker_provider):
    return relocated_worker_provider()


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "clone-success",
        "developer-path",
        "stale-main",
        "swapped-install",
        "unbound-wheel",
        "stale-container",
        "stale-supervision",
        "missing-source",
        "extra-source",
        "symlink-source",
        "wrong-root",
        "shadow-support",
        "shadow-stdlib",
        "cwd-shadow",
        "script-shadow",
        "wrong-client-interpreter",
        "wrong-stub-interpreter",
        "poison-environment",
        "missing-result",
        "partial-result",
        "oversized-result",
        "duplicate-result",
        "helper-before-evidence",
        "helper-after-evidence",
        "stderr-read-failure",
        "stdout-aborted",
        "evidence-unwritable",
        "evidence-truncated",
        "evidence-inconsistent",
        "duplicate-attestation",
        "partial-attestation",
    ],
)
def test_relocated_exact_wheel_acceptance(relocated_worker, tmp_path, case):
    record = portability().exercise_relocated(relocated_worker, tmp_path, case)
    assert record["case_id"] == case
    assert record["candidate_dist_pre"] == record["candidate_dist_post"]
    assert record["build_input_pre"] == record["build_input_post"]
    assert record["helper_stream_calls"] == int(record["helper_attempted"])
    assert set(record["helper_stream_outcomes"]) == (
        {"STDOUT", "STDERR"} if record["helper_attempted"] else set()
    )
    assert record["resources_reconciled"]
    if case == "swapped-install":
        assert record["self_consistent_install_record"]
    assert record["accepted"] is (
        case in {"success", "clone-success", "poison-environment", "cwd-shadow"}
    )
    if record["accepted"]:
        assert record["helper_status"] == 0
        result = record["result"]
        assert result["client_attribution_timing"] == "post-import"
        assert result["service_context"] == "wheel"
        assert result["wheel_sha256"] == relocated_worker.wheel_sha256
        assert result["service_stream_calls"] == 1
        assert result["service_stream_outcomes"] == {"STDOUT": "EOF", "STDERR": "EOF"}
        assert result["service_exit_code"] == 0
        assert result["evidence_readback"]
        assert result["provenance_final"] and result["eof_delivered"]
        assert [call["return_code"] for call in result["calls"]] == [0, 0]
        assert result["service_argv"][-5:] == [
            "--lifecycle",
            result["lifecycle_input"],
            "--",
            "--config",
            result["config_path"],
        ]
        assert result["service_argv"][:2] == [record["expected_interpreter"], "-P"]
        if case == "clone-success":
            assert Path(record["expected_interpreter"]).is_relative_to(
                tmp_path / "venv"
            )
        assert (
            result["service_environment"]["PATH"].split(os.pathsep)[0]
            == result["docker_wrapper_directory"]
        )
        service_env = result["service_environment"]
        assert service_env["filtered_keys"] == ["PYTHONNOUSERSITE"]
        assert service_env["PYTHONNOUSERSITE"] == "1"
        assert service_env["prefix_counts"] == {
            "PYTHON": 1,
            "PYTEST": 0,
            "FOOTMAN_RELOCATED_": 0,
        }
        assert service_env["key_count"] >= len(service_env["filtered_keys"])
        assert len(service_env["key_set_sha256"]) == 64
        if case == "poison-environment":
            assert {
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTHONUSERBASE",
                "PYTEST_RELOCATED_CANARY",
                "FOOTMAN_RELOCATED_CANARY",
            } <= set(result["helper_environment_filtered_keys"])
        evidence = provenance.load(result["evidence_path"])
        _, expected_set = provenance.manifest_set(record["expected_origin"])
        assert evidence["sources"] == expected_set
        assert evidence["source_count"] == result["source_count"]
        assert evidence["set_sha256"] == result["set_sha256"]
        assert evidence["wheel_sha256"] == relocated_worker.wheel_sha256
        assert (
            result["stub_attribution"]["interpreter"] == record["expected_interpreter"]
        )
        assert result["stub_attribution"]["context"] == "wheel"


@pytest.mark.parametrize(
    "stage",
    [
        "before-launch",
        "before-journal",
        "before-provenance",
        "after-provenance",
        "after-acquire",
        "after-journal",
        "idle",
        "accepted-work",
        "before-evidence",
        "after-evidence",
        "interruption",
        "lifetime",
        "unknown-incarnation",
        "observer-failure",
        "surviving-incarnation",
        "unsettled-resource",
    ],
)
def test_relocated_helper_loss_reconciliation(relocated_worker, tmp_path, stage):
    record = portability().exercise_loss(relocated_worker, tmp_path, stage)
    assert record["stage"] == stage
    if stage == "lifetime":
        assert record["helper_live_at_lifetime_exit"]
    assert record["helper_cleanup_complete"]
    if stage in {
        "unknown-incarnation",
        "observer-failure",
        "surviving-incarnation",
        "unsettled-resource",
    }:
        assert not record["accepted"] and record["retained_scratch"]
        assert record["failure"]
    elif stage == "before-launch":
        assert not record["service_acquired"]
        assert record["resources_reconciled"]
    else:
        assert record["service_incarnation_absent"]
        assert record["resources_reconciled"]


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate-key",
        "missing",
        "unknown",
        "schema-bool",
        "budget-bool",
        "budget-nonfinite",
        "expired",
        "extended",
        "token",
        "root",
        "path-escape",
        "symlink",
        "alias",
        "wrong-position",
        "duplicate-option",
        "production-forward",
        "wrong-leg",
        "missing-lifecycle",
        "internal-alias",
        "directory-input",
    ],
)
def test_relocated_manifest_rejects_before_service_acquisition(tmp_path, fault):
    observation = portability().exercise_request_rejection(ROOT, tmp_path, fault)
    assert observation["rejected"]
    assert observation["service_factory_calls"] == 0
    assert not observation["service_acquired"]


@pytest.mark.parametrize(
    "branch",
    [
        "build-input",
        "build-child",
        "archive",
        "copy",
        "request",
        "lifecycle-input",
        "journal-observer",
        "incarnation-observer",
        "evidence-writer",
        "run-observer",
    ],
)
@pytest.mark.parametrize(
    "fault", ["operation-close", "sole-close", "interruption", "repeat", "independent"]
)
def test_acceptance_resource_fault_matrix(tmp_path, branch, fault):
    observation = portability().exercise_resource_fault(ROOT, tmp_path, branch, fault)
    assert observation["authority_preserved"]
    assert observation["sole_release_authority"]
    assert observation["independent_releases_attempted"]
    assert not observation["repeated_release_count"]


def test_acceptance_multiworker_module_fixture_ownership(tmp_path):
    observation = portability().exercise_worker_distribution(ROOT, tmp_path)
    assert observation["status"] == 0
    assert len(observation["workers"]) == 2
    assert len({item["build_root"] for item in observation["workers"]}) == 2
    assert len({item["venv"] for item in observation["workers"]}) == 2
    assert len({item["scratch"] for item in observation["workers"]}) == 2
    for item in observation["workers"]:
        assert len(item["modules_seen"]) == 2
        assert item["build_count"] == item["venv_count"] == item["cleanup_count"] == 1
        assert item["reuse_count"] >= 1
        assert item["resources_reconciled"]


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_independent_phase10_build_attribution_and_phase9_parity(tmp_path, case):
    baseline = tmp_path / "phase9"
    baseline.mkdir()
    reference_attribution.exercise(ROOT, baseline, case)
    earlier = provenance.load(baseline / "evidence/observation.json")
    candidate = tmp_path / "phase10"
    candidate.mkdir()
    current = attribution.exercise(ROOT, candidate, case)
    attribution.compare(current, earlier)


@pytest.mark.parametrize(
    "fault",
    ["nonzero-build", "successful-build", "post-interruption", "collection-failure"],
)
def test_phase10_build_failure_precedes_postcheck_failure(tmp_path, fault):
    from tests import acceptance_build_support as build
    from tests import build_failure_support as faults

    faults.exercise(
        ROOT,
        tmp_path,
        build,
        lambda root, evidence: build.build_and_bind_wheel(
            root, evidence_directory=evidence, timeout_seconds=30
        ),
        fault,
    )


def test_build_status_survives_post_build_input_read_failure(tmp_path):
    from unittest.mock import patch

    from tests import acceptance_build_support as build
    from tests.wheel_attribution_cases import case as attribution_case

    prepared = service.prepare_inputs(ROOT, tmp_path / "prepared")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    scenario = attribution_case("build-status-failure")
    executable, call = reference_attribution.scripted_uv(tmp_path, scenario)
    original = provenance.read
    post_error = OSError("post-build README read failed")

    def read(path, **kwargs):
        if Path(path) == prepared / "README.md" and call.exists():
            raise post_error
        return original(path, **kwargs)

    with (
        patch.dict(
            os.environ,
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]},
        ),
        patch.object(provenance, "read", read),
        pytest.raises(build.BuildFailure) as caught,
    ):
        build.build_and_bind_wheel(
            prepared, evidence_directory=evidence, timeout_seconds=30
        )
    assert caught.value.observation["status"] == 17
    assert caught.value.observation["stdout"] == scenario.stdout_bytes
    assert caught.value.observation["stderr"] == scenario.stderr_bytes
    assert str(post_error) in str(caught.value) + str(
        getattr(caught.value, "__notes__", ())
    )


@pytest.mark.parametrize(
    "fault", ["missing-readme", "missing-src", "extra-root", "different-bytes"]
)
def test_phase10_prepared_profile_fault_is_setup_error(tmp_path, monkeypatch, fault):
    from unittest.mock import Mock

    from tests import acceptance_build_support as build
    from tests.wheel_attribution_cases import case

    original = attribution.prepare_inputs

    def prepare(root, destination):
        result = original(root, destination)
        if fault == "missing-readme":
            (result / "README.md").unlink()
        elif fault == "missing-src":
            (result / "src").rename(result / "wrong-src")
        elif fault == "extra-root":
            (result / "tests").mkdir()
        else:
            write_bytes(result / "README.md", b"not the declared common bytes")
        return result

    factory = Mock(side_effect=AssertionError("invalid profile invoked build"))
    monkeypatch.setattr(attribution, "prepare_inputs", prepare)
    monkeypatch.setattr(build, "build_and_bind_wheel", factory)
    with pytest.raises(AssertionError) as caught:
        attribution.exercise(ROOT, tmp_path, case("success"))
    assert "invalid profile invoked build" not in str(caught.value)
    factory.assert_not_called()


@pytest.mark.parametrize("artifact", ["sdist", "wheel"])
@pytest.mark.parametrize("kind", ["file", "directory", "dangling-link"])
def test_phase10_both_destinations_guarded_without_tool_or_deletion(
    tmp_path, monkeypatch, artifact, kind
):
    from tests import acceptance_build_support as build
    from tests.wheel_attribution_cases import case

    root = service.prepare_inputs(ROOT, tmp_path / "prepared")
    output = tmp_path / "evidence"
    output.mkdir()
    (root / "dist").mkdir()
    name = (
        "footman-0.1.0.tar.gz"
        if artifact == "sdist"
        else "footman-0.1.0-py3-none-any.whl"
    )
    conflict = root / "dist" / name
    if kind == "file":
        provenance.write(conflict, b"unowned prior artifact")
    elif kind == "directory":
        conflict.mkdir()
    else:
        conflict.symlink_to("absent-target")
    before = reference_attribution.dist_identity(root)
    tool, call = reference_attribution.scripted_uv(tmp_path, case("success"))
    monkeypatch.setenv("PATH", str(tool.parent) + os.pathsep + os.environ["PATH"])
    with pytest.raises(build.BuildFailure) as caught:
        build.build_and_bind_wheel(root, evidence_directory=output, timeout_seconds=30)
    assert not call.exists()
    assert caught.value.observation["attempted"] is False
    assert caught.value.observation["status"] is None
    assert str(root) in str(caught.value) and str(conflict) in str(caught.value)
    assert "no build/deletion/replacement" in str(caught.value)
    assert reference_attribution.dist_identity(root) == before


@pytest.mark.parametrize(
    "stage", ["before-journal", "before-provenance", "after-provenance"]
)
@pytest.mark.parametrize("interrupted", [False, True])
def test_publication_loss_gate_borrows_input_without_close(
    tmp_path, monkeypatch, stage, interrupted
):
    import time

    from tests import mcp_lifecycle as lifecycle
    from tests import mcp_server_child as bootstrap

    marker = KeyboardInterrupt("gate read interrupted")

    class Borrowed(lifecycle.DeadlineReader):
        def __init__(self):
            self.reads = 0
            self.closes = 0

        def readline(self):
            self.reads += 1
            if interrupted:
                raise marker
            return b""

        def close(self):
            self.closes += 1
            raise AssertionError("borrowed input closed")

    borrowed = Borrowed()
    value = {
        "scratch_root": str(tmp_path),
        "instance_token": "a" * 32,
        "service_deadline_monotonic": time.monotonic() + 60,
    }
    monkeypatch.setenv("FOOTMAN_BOOTSTRAP_TEST_GATE", stage)
    with pytest.raises(KeyboardInterrupt if interrupted else RuntimeError) as caught:
        bootstrap.publication_gate(provenance, lifecycle, value, stage, borrowed)
    if interrupted:
        assert caught.value is marker
    assert borrowed.reads == 1 and borrowed.closes == 0
    record = provenance.load(tmp_path / ("bootstrap-gate-" + stage + ".json"))
    assert record["stage"] == stage and record["pid"] == os.getpid()
    assert record["instance_token"] == value["instance_token"]


def test_build_surface_import_has_no_fixture_side_effects(monkeypatch):
    import importlib.util
    import sys
    from unittest.mock import Mock

    from footman.process import OwnedProcess

    factory = Mock(side_effect=AssertionError("import acquired a process"))
    writer = Mock(side_effect=AssertionError("import wrote fixture evidence"))
    monkeypatch.setattr(OwnedProcess, "launch", factory)
    monkeypatch.setattr(provenance, "write", writer)
    name = "phase10_isolated_build_surface"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "tests/acceptance_build_support.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert callable(module.build_and_bind_wheel)
    assert "tests.test_dispatch_acceptance" not in module.__dict__
    factory.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize("missing", ["proc", "12345", "stat"])
def test_incarnation_observer_distinguishes_missing_process_from_unavailable_proc(
    tmp_path, monkeypatch, missing
):
    import errno

    from footman.filesystem.descriptors import DescriptorError

    observer = portability()
    cause = FileNotFoundError(errno.ENOENT, "injected missing component", missing)
    error = (
        cause
        if missing == "stat"
        else DescriptorError("open-descriptor", "missing component", cause=cause)
    )

    def read(pid):
        assert pid == 12345
        if missing == "stat":
            return None
        raise error

    monkeypatch.setattr(provenance, "read_proc_stat", read)
    if missing == "proc":
        with pytest.raises(DescriptorError) as caught:
            observer.incarnation({"pid": 12345, "incarnation": "123"})
        assert caught.value is error
    else:
        assert observer.incarnation({"pid": 12345, "incarnation": "123"}) == "absent"


@pytest.mark.parametrize("kind", ["owner", "io"])
def test_incarnation_observer_unknown_failure_keeps_identity_and_adds_context(
    monkeypatch, kind
):
    from unittest.mock import Mock

    from footman.filesystem.descriptors import DescriptorError, UnsafeDescriptorError

    observer = portability()
    error = (
        UnsafeDescriptorError("validate-descriptor", "foreign directory owner")
        if kind == "owner"
        else DescriptorError(
            "open-descriptor", "observer I/O failure", cause=PermissionError("denied")
        )
    )
    read = Mock(side_effect=error)
    monkeypatch.setattr(provenance, "read_proc_stat", read)
    monkeypatch.setattr(observer.os, "getpid", lambda: 4321)
    monkeypatch.setattr(observer.time, "monotonic_ns", lambda: 123456789)
    with pytest.raises(DescriptorError) as caught:
        observer.incarnation({"pid": 12345, "incarnation": "98765"})
    assert caught.value is error
    read.assert_called_once_with(12345)
    assert error.__notes__ == [
        "incarnation observer: pid=12345 expected_starttime_ticks=98765 "
        "observer_pid=4321 monotonic_ns=123456789 "
        "operation=read-proc-stat errno=None "
        + f"stage={getattr(error, 'stage', None)}"
    ]


def test_phase10_build_binding_is_defensively_immutable(tmp_path):
    from tests.acceptance_build_support import BuildObservation
    from tests.build_observation_values import check_binding_values

    check_binding_values(BuildObservation, tmp_path)


@pytest.mark.parametrize(
    "fault", ["repeat", "operation-close", "sole-close", "interruption", "independent"]
)
def test_worker_scope_once_only_cleanup_and_failure_authority(
    tmp_path, monkeypatch, fault
):
    from unittest.mock import Mock

    from footman.filesystem.cleanup import CleanupFailures

    support = portability()
    value = object()
    preparation = Mock(return_value=value)
    factory = Mock(return_value=tmp_path)
    attempts = []
    operation = (
        KeyboardInterrupt("worker interrupted")
        if fault == "interruption"
        else OSError("worker operation failed")
    )
    close = OSError("worker completion failed")
    second = OSError("independent worker completion failed")

    def finish(worker):
        assert worker is value
        attempts.append("first")
        if fault == "independent":
            failures = CleanupFailures(None)

            def fail(error, label):
                attempts.append(label)
                raise error

            failures.run(lambda: fail(close, "second"), ordinary=(OSError,))
            failures.run(lambda: fail(second, "third"), ordinary=(OSError,))
            raise failures.complete()
        if fault != "repeat":
            raise close

    monkeypatch.setattr(support, "prepare_worker", preparation)
    monkeypatch.setattr(support, "finish_worker", finish)
    caught = None
    try:
        with support.worker_scope(ROOT, factory, "worker") as obtain:
            assert obtain() is obtain() is value
            if fault in {"operation-close", "interruption"}:
                raise operation
    except BaseException as error:
        caught = error
    assert caught is (
        None
        if fault == "repeat"
        else operation
        if fault in {"operation-close", "interruption"}
        else close
    )
    if fault in {"operation-close", "interruption"}:
        assert close in getattr(operation, "_transaction_secondary", ())
    if fault == "independent":
        assert attempts == ["first", "second", "third"]
        assert second in getattr(close, "_transaction_secondary", ())
    else:
        assert attempts == ["first"]
    with pytest.raises(RuntimeError, match="closed"):
        obtain()
    assert preparation.call_count == factory.call_count == 1
    assert attempts.count("first") == 1


@pytest.mark.parametrize("boundary", ["normal", "operation", "interruption"])
def test_worker_scope_without_obtain_acquires_and_finishes_nothing(
    tmp_path, monkeypatch, boundary
):
    from unittest.mock import Mock

    support = portability()
    factory = Mock(return_value=tmp_path)
    preparation = Mock()
    finish = Mock()
    monkeypatch.setattr(support, "prepare_worker", preparation)
    monkeypatch.setattr(support, "finish_worker", finish)
    marker = (
        KeyboardInterrupt("unused worker interrupted")
        if boundary == "interruption"
        else OSError("unused worker operation failed")
    )
    scope = support.worker_scope(ROOT, factory, "non-participant")
    caught = None
    try:
        with scope as obtain:
            if boundary != "normal":
                raise marker
    except BaseException as error:
        caught = error
    assert caught is (None if boundary == "normal" else marker)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="closed"):
            obtain()
        assert not scope.__exit__(None, None, None)
    factory.assert_not_called()
    preparation.assert_not_called()
    finish.assert_not_called()


@pytest.mark.parametrize("source", ["ambient", "extra"])
def test_acceptance_clean_environment_removes_import_redirection(monkeypatch, source):
    from tests import acceptance_build_support as build

    poison = {
        name: "poison"
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
            "PYTHONINSPECT",
            "PYTHONSAFEPATH",
            "PYTHON_FUTURE_CANARY",
            "FOOTMAN_BOOTSTRAP_CANARY",
        )
    }
    poison["PYTHONNOUSERSITE"] = "0"
    monkeypatch.setenv("FOOTMAN_CLEANER_KEEP", "keep")
    if source == "ambient":
        for key, value in poison.items():
            monkeypatch.setenv(key, value)
    before = dict(os.environ)
    extra = poison if source == "extra" else None
    result = build.clean_environment(extra)
    assert result["PYTHONNOUSERSITE"] == "1"
    assert {key for key in result if key.startswith("PYTHON")} == {"PYTHONNOUSERSITE"}
    assert not any(key.startswith("FOOTMAN_BOOTSTRAP_") for key in result)
    assert result["FOOTMAN_CLEANER_KEEP"] == "keep"
    assert result.get("PATH") == before.get("PATH")
    assert dict(os.environ) == before
    assert build.clean_environment(extra) == result


def test_worker_scope_preparation_failure_is_retained_without_retry(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    support = portability()
    marker = OSError("worker preparation failed")
    prepare = Mock(side_effect=marker)
    finish = Mock()
    monkeypatch.setattr(support, "prepare_worker", prepare)
    monkeypatch.setattr(support, "finish_worker", finish)
    with support.worker_scope(ROOT, lambda: tmp_path, "worker") as obtain:
        for _ in range(2):
            with pytest.raises(OSError) as caught:
                obtain()
            assert caught.value is marker
    assert prepare.call_count == 1
    finish.assert_not_called()


def test_acceptance_registration_and_documented_commands():
    for name in ("docs/testing.md", "docs/python-checks.md", "README.md"):
        assert_documented_suite(provenance.read(ROOT / name).decode())
    assert (
        ".venv/bin/python -m pytest tests/test_dispatch_acceptance.py "
        "-n 2 --dist worksteal --tb=short -q"
    ) in provenance.read(ROOT / "docs/testing.md").decode()


@pytest.mark.parametrize("phase", [None, "review_2"])
@pytest.mark.parametrize("status", [0, 7])
def test_staged_fixture_real_mcp_manifest_flow(tmp_path, phase, status):
    world = service.world(ROOT, tmp_path)
    repo = world["repo"]
    write_bytes(repo.path / "payload", b"staged acceptance\x00\xff\n")
    provenance.write(repo.path / "wrapper", b"#!/bin/sh\nexit 0\n")
    os.chmod(repo.path / "wrapper", 0o755)
    os.symlink("payload", repo.path / "internal-link")
    repo.git("add", "payload", "wrapper", "internal-link")
    tree = repo.git("write-tree").stdout.decode().strip()
    write_bytes(repo.path / "payload", b"unstaged before capture\n")
    provenance.write(repo.path / "untracked", b"not an input\n")
    initial = repo.identity()
    records = service.install_docker(world, status=status, delay=1)
    observed = {}

    async def scenario():
        async with service.client(world) as (session, observation):

            async def request():
                arguments = {} if phase is None else {"phase": phase}
                observed["response"] = await session.call_tool(
                    "run_privileged_command", arguments
                )

            async with anyio.create_task_group() as group:
                group.start_soon(request)
                with anyio.fail_after(10):
                    while True:
                        calls = service.records(records)
                        if any(call["operation"] == "command" for call in calls):
                            break
                        await anyio.sleep(0.01)
                repo.assert_unchanged(initial)
                copy = next(call for call in calls if call["operation"] == "copy")
                source = Path(copy["argv"][1].removesuffix("/."))
                assert (
                    provenance.read(source / "payload")
                    == b"staged acceptance\x00\xff\n"
                )
                assert provenance.read(source / "wrapper") == b"#!/bin/sh\nexit 0\n"
                assert (source / "wrapper").stat().st_mode & 0o111 == 0o111
                assert (source / "internal-link").is_symlink()
                assert os.readlink(source / "internal-link") == "payload"
                assert not (source / ".git").exists()
                assert not (source / "untracked").exists()
                # Only this disposable fixture is deliberately staged later. The
                # developer checkout/index is never selected by these commands.
                write_bytes(repo.path / "payload", b"later staged content\n")
                repo.git("add", "payload")
                later_tree = repo.git("write-tree").stdout.decode().strip()
                assert later_tree != tree
                write_bytes(repo.path / "payload", b"later workdir content\n")
                observed.update(source=source, later=repo.identity())
            assert observation.owner is not None

    anyio.run(scenario)
    value = service.response_check(
        observed["response"], status=status, succeeded=status == 0
    )
    manifest = provenance.load(value["resultPath"])
    calls = service.records(records)
    identity = manifest["container_id"]
    control = os.environ.get("FOOTMAN_ACCEPTANCE_CONTROL")
    expected_tree = "0" * len(tree) if control == "tree" else tree
    expected_argv = (
        ["wrong-command"]
        if control == "argv"
        else [
            "exec",
            "-u",
            "root",
            "-w",
            "/workspace/source",
            identity,
            "image-tool",
            "two words",
            "; $literal",
        ]
    )
    expected_path = (
        tmp_path / "wrong-output"
        if control == "path"
        else Path(value["resultPath"]).parent
    )
    assert manifest["snapshot"]["tree_id"] == expected_tree
    assert manifest["snapshot"]["path"] == str(observed["source"])
    assert manifest["return_code"] == (99 if control == "status" else status)
    assert manifest["phase"] == phase
    assert manifest["output_directory"] == str(expected_path)
    assert [call["operation"] for call in calls] == [
        "launch",
        "inspect",
        "mkdir",
        "copy",
        "command",
        "remove",
        "confirm",
    ]
    assert calls[4]["argv"] == expected_argv
    assert calls[3]["argv"] == [
        "cp",
        str(observed["source"]) + "/.",
        identity + ":/workspace/source/",
    ]
    assert calls[0]["argv"] == [
        "run",
        "--detach=true",
        "--pull=never",
        "--",
        "d9e853e87e55",
        "sleep",
        "infinity",
    ]
    assert calls[-2]["argv"] == ["rm", "-f", identity]
    assert calls[-1]["argv"] == [
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        "id=" + identity,
        "--format",
        "{{.ID}}",
    ]
    assert provenance.read(value["stdoutPath"]) == b"command stdout\x00\xff\n"
    assert provenance.read(value["stderrPath"]) == b"command stderr\x00\xfe\n"
    assert provenance.read(value["startupStdoutPath"]) == (identity + "\n").encode()
    assert provenance.read(value["startupStderrPath"]) == b""
    assert manifest["cleanup"]["confirmed_absent"]
    assert manifest["cleanup"]["snapshot_deleted"]
    assert not manifest["cleanup"]["unresolved_resources"]
    assert not observed["source"].exists()
    repo.assert_unchanged(observed["later"])
    retained = provenance.load(tmp_path / "verified-set.json")
    assert retained["context"] == "development"
    assert retained["wheel_sha256"] is None


@pytest.mark.parametrize("kind", ["esrch", "permission", "interruption"])
def test_incarnation_raw_read_failure_is_annotated_without_retry(monkeypatch, kind):
    import errno
    from unittest.mock import Mock

    error = {
        "esrch": ProcessLookupError(errno.ESRCH, "gone", "stat"),
        "permission": PermissionError(errno.EACCES, "denied", "stat"),
        "interruption": KeyboardInterrupt("interrupted read"),
    }[kind]
    read = Mock(side_effect=error)
    monkeypatch.setattr(provenance, "read_proc_stat", read)
    with pytest.raises(type(error)) as caught:
        portability().incarnation({"pid": 12345, "incarnation": "98765"})
    assert caught.value is error
    read.assert_called_once_with(12345)
    note = error.__notes__[0]
    assert "pid=12345 expected_starttime_ticks=98765" in note
    assert "operation=read-proc-stat" in note
    assert f"errno={getattr(error, 'errno', None)}" in note


def test_proc_stat_after_reaped_child_retains_esrch(tmp_path, monkeypatch):
    """Real kernel control: pinned proc directory does not pin the task."""
    import sys
    from contextlib import ExitStack, nullcontext

    from footman.filesystem.descriptors import DirectoryDescriptor
    from footman.process import OwnedProcess

    observer = portability()
    record = {}
    observation = {
        "observer_uid": os.geteuid(),
        "observer_gid": os.getegid(),
        "observer_uid_map": provenance.read(
            Path("/proc") / str(os.getpid()) / "uid_map"
        ).decode(),
        "observer_gid_map": provenance.read(
            Path("/proc") / str(os.getpid()) / "gid_map"
        ).decode(),
        "observer_user_namespace": os.readlink("/proc/self/ns/user"),
    }

    def snapshot(directory):
        info = os.fstat(directory.fd)
        return {
            "fd": directory.fd,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": info.st_mode,
            "monotonic_ns": time.monotonic_ns(),
        }

    data = bytearray()
    with ExitStack() as directories:
        with OwnedProcess.launch(
            [
                sys.executable,
                "-P",
                "-c",
                "import os,sys; print(os.getpid(), flush=True); sys.stdin.buffer.read()",
            ],
            stdin_mode="pipe",
            deadline_seconds=10,
            grace_seconds=1,
        ) as owner:

            def chunk(stream, payload):
                data.extend(payload)
                if record or b"\n" not in data:
                    return
                pid = int(bytes(data).splitlines()[0])
                directory = directories.enter_context(
                    DirectoryDescriptor.open_secure_path(
                        provenance.OPS, Path("/proc") / str(pid)
                    )
                )
                raw = provenance.read(Path("/proc") / str(pid) / "stat")
                record.update(
                    pid=pid, incarnation=raw.rsplit(b")", 1)[1].split()[19].decode()
                )
                record["directory"] = directory
                observation.update(
                    pid=pid,
                    expected_starttime_ticks=record["incarnation"],
                    before=snapshot(directory),
                    child_uid_map=provenance.read(
                        Path("/proc") / str(pid) / "uid_map"
                    ).decode(),
                    child_gid_map=provenance.read(
                        Path("/proc") / str(pid) / "gid_map"
                    ).decode(),
                    child_user_namespace=os.readlink(f"/proc/{pid}/ns/user"),
                )
                owner.close_stdin(timeout_seconds=1)

            result = owner.stream(chunk)
            assert result.returncode == 0
            assert owner.cleanup_status.complete
            assert record
            directory = record.pop("directory")
            observation.update(
                after=snapshot(directory),
                child_returncode=result.returncode,
                cleanup_complete=owner.cleanup_status.complete,
            )
            before, after = observation["before"], observation["after"]
            observation["ownership_changed"] = (before["uid"], before["gid"]) != (
                after["uid"],
                after["gid"],
            )
            provenance.write(
                tmp_path / "proc-ownership-observation.json",
                provenance.canonical(observation),
            )
            print(
                "PROC_OWNERSHIP_OBSERVATION "
                + provenance.canonical(observation).decode()
            )
            assert (before["fd"], before["device"], before["inode"]) == (
                after["fd"],
                after["device"],
                after["inode"],
            )

            def pinned_directory(ops, path, *, require_owner):
                assert Path(path) == Path("/proc") / str(record["pid"])
                assert require_owner is False
                # Borrow the sole ExitStack-owned capability; do not re-adopt.
                return nullcontext(directory)

            monkeypatch.setattr(
                DirectoryDescriptor, "open_secure_path", pinned_directory
            )
            assert observer.incarnation(record) == "absent"
            directory.close()
            directory.close()  # owner cleanup stays idempotent


@pytest.mark.parametrize("argv", [["run"], ["rm", "-f", "b" * 64], ["container", "ls"]])
def test_reconcile_mismatch_reports_counts_ids_and_raw_records(
    tmp_path, monkeypatch, argv
):
    import json

    observer = portability()
    (tmp_path / "stub-records").mkdir()
    item = {"argv": argv, "container_id": "a" * 64, "status": 0, "stdin_eof": True}
    provenance.write(
        tmp_path / "stub-records" / "one-call.json", provenance.canonical(item)
    )
    monkeypatch.setattr(
        observer, "journal", lambda case: {"pid": 12345, "incarnation": "123"}
    )
    monkeypatch.setattr(observer, "incarnation", lambda record: "absent")
    case = {
        "tmp": tmp_path,
        "request": {"transport_deadline_monotonic": time.monotonic() + 10},
    }
    with pytest.raises(
        AssertionError, match="stub resource not reconciled: "
    ) as caught:
        observer.reconcile(case)
    details = json.loads(str(caught.value).split(": ", 1)[1])
    assert details["counts"] == {
        "launches": int(argv[0] == "run"),
        "removes": int(argv[:2] == ["rm", "-f"]),
        "confirms": int(argv[:2] == ["container", "ls"]),
    }
    assert details["launch_ids"] == (["a" * 64] if argv[0] == "run" else [])
    assert details["observations"] == [item]
    assert details["scratch_root"] == str(tmp_path)
