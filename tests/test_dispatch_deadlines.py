"""Shared deadlines and independent request ownership contracts."""

import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.dispatch_support import api, removal, run_case
from tests.launcher_case_support import ID, config, source, steps
from tests.launcher_support import DockerSequence

__all__ = ["source"]


def _phase3_support():
    support = importlib.import_module("tests.dispatch_deadline_support")
    assert callable(getattr(support, "validate_timeout_record", None)), (
        "required per-attempt timeout record validation is absent"
    )
    return support


def _synthetic_timeout_attempt(**changes):
    """Classifier input only: never evidence of real child execution."""
    from footman.process import LifecycleResult, ProcessResult
    from tests.dispatch_deadline_support import ChildRecordObservation, LaunchAttempt

    status = LifecycleResult(True)
    values = dict(
        ordinal=0,
        phase="working",
        argv=("docker", "run", "fixture"),
        deadline_seconds=0.3,
        grace_seconds=1,
        acquisition="acquired",
        owner=SimpleNamespace(cleanup_status=status),
        result=ProcessResult(b"", b"", -15, True, status),
        record=ChildRecordObservation("absent"),
    )
    values.update(changes)
    return LaunchAttempt(**values)


@pytest.mark.parametrize(
    "defect",
    [None, "requested", "failed", "cleanup", "success", "incomplete", "release"],
)
def test_timeout_record_eligibility_is_per_attempt(defect):
    """Synthetic observations must not lend eligibility to another helper."""
    from dataclasses import replace

    from footman.process import FailureInfo, FailureKind, FailurePhase, LifecycleResult

    support = _phase3_support()
    attempt = _synthetic_timeout_attempt()
    if defect in ("requested", "failed"):
        attempt = replace(attempt, acquisition=defect, owner=None)
    elif defect == "cleanup":
        attempt = replace(attempt, phase="cleanup")
    elif defect == "success":
        attempt = replace(
            attempt, result=replace(attempt.result, timed_out=False, returncode=0)
        )
    elif defect in ("incomplete", "release"):
        failures = (
            ()
            if defect == "incomplete"
            else (
                FailureInfo(
                    OSError("synthetic close"),
                    FailureKind.ORDINARY,
                    FailurePhase.RELEASE,
                ),
            )
        )
        attempt = replace(
            attempt,
            owner=SimpleNamespace(
                cleanup_status=LifecycleResult(defect != "incomplete", failures)
            ),
        )
    expected = json.dumps(
        {
            "argv": list(attempt.argv[1:]),
            "container_id": None,
            "status": 0,
            "stdin_eof": True,
        }
    ).encode()
    if defect is None:
        assert support.validate_timeout_record(attempt, expected=expected) == "absent"
    else:
        with pytest.raises(AssertionError):
            support.validate_timeout_record(attempt, expected=expected)


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "prefix",
        "complete",
        "wrong-argv",
        "schema",
        "stdin",
        "corrupt",
        "read",
        "close",
    ],
)
def test_timeout_record_classifies_only_expected_prefixes(kind):
    """Explicit synthetic bytes, not an assertion of an actual interrupted write."""
    from dataclasses import replace

    from tests.dispatch_deadline_support import ChildRecordObservation

    support = _phase3_support()
    payload = {
        "argv": ["run", "fixture"],
        "container_id": None,
        "status": 0,
        "stdin_eof": True,
    }
    expected = json.dumps(payload).encode()
    data = expected
    if kind == "empty":
        data = b""
    elif kind == "prefix":
        data = expected[: len(expected) // 2]
    elif kind == "corrupt":
        data = b'{"arbitrary":broken'
    elif kind in ("wrong-argv", "schema", "stdin"):
        payload.update(
            {
                "wrong-argv": {"argv": ["rm", "guessed"]},
                "schema": {"extra": 1},
                "stdin": {"stdin_eof": False},
            }[kind]
        )
        data = json.dumps(payload).encode()
    error = (
        OSError("synthetic independent record " + kind)
        if kind in ("read", "close")
        else None
    )
    record = ChildRecordObservation(
        "read-failed" if error else "available", data, error
    )
    attempt = replace(_synthetic_timeout_attempt(), record=record)
    if kind in ("empty", "prefix", "complete"):
        assert support.validate_timeout_record(attempt, expected=expected) == (
            "complete" if kind == "complete" else "incomplete"
        )
    else:
        with pytest.raises(AssertionError):
            support.validate_timeout_record(attempt, expected=expected)


@pytest.mark.parametrize(
    "defect",
    [None, "acquisition", "import", "ordinary-exit", "release", "later-working"],
)
def test_timeout_verdict_requires_positive_retained_result(defect):
    """Synthetic rejection controls cannot establish a real timeout."""
    from dataclasses import replace

    from footman.process import FailureInfo, FailureKind, FailurePhase, LifecycleResult

    support = _phase3_support()
    assert callable(getattr(support, "assert_positive_timeout", None)), (
        "required positive timeout verdict is absent"
    )
    attempt = _synthetic_timeout_attempt()
    if defect == "acquisition":
        attempt = replace(
            attempt,
            acquisition="failed",
            owner=None,
            result=None,
            error=OSError("synthetic acquisition"),
        )
    elif defect in ("import", "ordinary-exit"):
        attempt = replace(
            attempt, result=replace(attempt.result, timed_out=False, returncode=1)
        )
    elif defect == "release":
        status = LifecycleResult(
            True,
            (
                FailureInfo(
                    OSError("synthetic release"),
                    FailureKind.ORDINARY,
                    FailurePhase.RELEASE,
                ),
            ),
        )
        attempt = replace(attempt, owner=SimpleNamespace(cleanup_status=status))
    attempts = (attempt,)
    if defect == "later-working":
        attempts += (replace(_synthetic_timeout_attempt(), ordinal=1),)
    if defect is None:
        assert support.assert_positive_timeout(attempts) is attempt
    else:
        with pytest.raises(AssertionError):
            support.assert_positive_timeout(attempts)


@pytest.mark.parametrize(
    "defect",
    [None, "order", "nonempty", "failed", "incomplete", "unresolved", "snapshot"],
)
def test_known_identity_timeout_cleanup_verdict(defect):
    """Synthetic cleanup verdict controls require independent confirmation."""
    from dataclasses import replace

    support = _phase3_support()
    assert callable(getattr(support, "assert_identity_cleanup", None)), (
        "required identity-aware cleanup verdict is absent"
    )
    cleanup = tuple(tuple(step["argv"]) for step in removal())
    attempts = tuple(
        replace(_synthetic_timeout_attempt(), ordinal=i + 1, phase="cleanup", argv=argv)
        for i, argv in enumerate(cleanup)
    )
    attempts = tuple(
        replace(a, result=replace(a.result, timed_out=False, returncode=0))
        for a in attempts
    )
    run = SimpleNamespace(
        execution=SimpleNamespace(container_id=ID),
        unresolved_resources=False,
        snapshot_deleted=True,
        snapshot=SimpleNamespace(path=SimpleNamespace(exists=lambda: False)),
    )
    if defect == "order":
        attempts = tuple(reversed(attempts))
    elif defect in ("nonempty", "failed", "incomplete"):
        result = replace(
            attempts[-1].result,
            stdout=b"unexpected-id\n" if defect == "nonempty" else b"",
            returncode=1 if defect == "failed" else 0,
            stream_outcomes=(),
        )
        attempts = (*attempts[:-1], replace(attempts[-1], result=result))
    if defect != "incomplete":
        from footman.process import ProcessStream, StreamOutcome

        attempts = tuple(
            replace(
                a,
                result=replace(
                    a.result,
                    stream_outcomes=tuple(
                        (s, StreamOutcome.EOF) for s in ProcessStream
                    ),
                ),
            )
            for a in attempts
        )
    if defect == "unresolved":
        run.unresolved_resources = True
    if defect == "snapshot":
        run.snapshot_deleted = False
    if defect is None:
        support.assert_identity_cleanup(run, attempts, cleanup=cleanup)
    else:
        with pytest.raises(AssertionError):
            support.assert_identity_cleanup(run, attempts, cleanup=cleanup)


@pytest.mark.parametrize(
    "defect", [None, "guessed", "resolved", "deleted", "incomplete"]
)
def test_unknown_identity_timeout_cleanup_verdict(defect):
    """Unknown identity is an unresolved negative contract, not external success."""
    from dataclasses import replace

    from footman.process import LifecycleResult

    support = _phase3_support()
    assert callable(getattr(support, "assert_identity_cleanup", None)), (
        "required unknown-identity verdict is absent"
    )
    attempt = _synthetic_timeout_attempt()
    if defect == "incomplete":
        attempt = replace(
            attempt, owner=SimpleNamespace(cleanup_status=LifecycleResult(False))
        )
    attempts = (attempt,)
    if defect == "guessed":
        attempts += (replace(attempt, ordinal=1, phase="cleanup"),)
    run = SimpleNamespace(
        execution=SimpleNamespace(container_id=None),
        unresolved_resources=defect != "resolved",
        snapshot_deleted=defect == "deleted",
        snapshot=SimpleNamespace(
            path=SimpleNamespace(exists=lambda: defect != "deleted")
        ),
    )
    if defect is None:
        support.assert_identity_cleanup(
            run, attempts, cleanup=tuple(tuple(s["argv"]) for s in removal())
        )
    else:
        with pytest.raises(AssertionError):
            support.assert_identity_cleanup(
                run, attempts, cleanup=tuple(tuple(s["argv"]) for s in removal())
            )


def test_real_timeout_is_stage_independent(tmp_path, source):
    """Actual sole-owned children; no synthetic records or forced timeout stage."""
    from tests.dispatch_deadline_support import (
        LaunchJournal,
        assert_identity_cleanup,
        assert_positive_timeout,
        read_child_record,
        validate_timeout_record,
    )

    cfg = config(timeout_seconds=0.3, cleanup_grace_seconds=1)
    working = [dict(step, container_id=ID) for step in steps(source, cfg)]
    working[-1]["delay"] = 2
    cleanup = removal()
    expected_working = tuple(tuple(s["argv"]) for s in working)
    expected_cleanup = tuple(tuple(s["argv"]) for s in cleanup)
    journal = LaunchJournal(working=expected_working, cleanup=expected_cleanup)
    sequences = {}
    for phase, script in (("working", working), ("cleanup", cleanup)):
        directory = tmp_path / phase
        directory.mkdir()
        sequences[phase] = DockerSequence(directory, script)

    def launch(argv, **options):
        def acquire(actual, **passed):
            phase = "cleanup" if tuple(actual) in expected_cleanup else "working"
            return sequences[phase].launch(actual, **passed)

        return journal.launch(argv, acquire=acquire, **options)

    run = api().RunSupervisor(cfg, source, launch=launch)
    run.execute(lambda *_: None, lambda *_: None)
    results = [
        run.execution.startup_result,
        *(op.result for op in run.execution.infrastructure_operations),
        run.execution.command_result,
    ]
    mapped = dict(zip(expected_working, results, strict=True))
    mapped.update(
        zip(expected_cleanup, (op.result for op in run.cleanup.operations), strict=True)
    )
    try:
        assert run.failure is not None
        for attempt in journal.snapshot():
            assert attempt.acquisition == "acquired"
            journal.observe_result(attempt.ordinal, mapped[attempt.argv])
        attempts = journal.snapshot()
        timeout = assert_positive_timeout(attempts)
        actual_working = tuple(a.argv for a in attempts if a.phase == "working")
        assert actual_working == expected_working[: len(actual_working)]
        assert timeout.argv == actual_working[-1]
        assert all(
            0 < a.deadline_seconds <= 0.3 for a in attempts if a.phase == "working"
        )
        for attempt in attempts:
            if attempt.phase == "working":
                expected_grace = (cfg.cleanup_grace_seconds / 10) * (
                    6 if attempt.argv == expected_working[-1] else 1
                )
                assert attempt.grace_seconds == expected_grace
        for phase, sequence in sequences.items():
            sequence.assert_owners_settled()
            phase_attempts = tuple(a for a in attempts if a.phase == phase)
            for attempt, path, step in zip(
                phase_attempts,
                sequence.stub.records,
                sequence.steps[: len(phase_attempts)],
                strict=True,
            ):
                observation = read_child_record(path)
                journal.observe_record(
                    attempt.ordinal,
                    availability=observation.availability,
                    data=observation.data,
                )
                expected = json.dumps(
                    {
                        "argv": step["argv"][1:],
                        "container_id": ID,
                        "status": step.get("status", 0),
                        "stdin_eof": True,
                    }
                ).encode()
                validate_timeout_record(
                    journal.snapshot()[attempt.ordinal], expected=expected
                )
        assert_identity_cleanup(run, journal.snapshot(), cleanup=expected_cleanup)
    except BaseException as primary:
        journal.annotate_failure(primary)
        raise


@pytest.mark.parametrize("phase", ["working", "cleanup"])
@pytest.mark.parametrize("kind", ["absent", "prefix", "corrupt"])
def test_successful_helper_cannot_inherit_timeout_record_tolerance(phase, kind):
    """Synthetic neighboring timeout cannot excuse successful helper evidence."""
    from dataclasses import replace

    from tests.dispatch_deadline_support import ChildRecordObservation

    support = _phase3_support()
    timeout = _synthetic_timeout_attempt()
    successful = replace(
        timeout,
        ordinal=1,
        phase=phase,
        result=replace(timeout.result, timed_out=False, returncode=0),
    )
    expected = json.dumps(
        {
            "argv": ["run", "fixture"],
            "container_id": None,
            "status": 0,
            "stdin_eof": True,
        }
    ).encode()
    record = (
        ChildRecordObservation("absent")
        if kind == "absent"
        else ChildRecordObservation(
            "available", expected[:10] if kind == "prefix" else b"corrupt"
        )
    )
    successful = replace(successful, record=record)
    assert support.validate_timeout_record(timeout, expected=expected) == "absent"
    with pytest.raises(AssertionError):
        support.validate_timeout_record(successful, expected=expected)


@pytest.mark.parametrize("index", range(4), ids=["startup", "inspect", "mkdir", "cp"])
def test_controlled_timeout_identity_branches_use_real_supervisor(source, index):
    """Synthetic early timeouts retain production results and identity handling."""
    from tests.dispatch_deadline_support import (
        LogicalScenario,
        assert_identity_cleanup,
        assert_positive_timeout,
    )

    cfg = config(timeout_seconds=0.3, cleanup_grace_seconds=1)
    scenario = LogicalScenario(source, cfg)
    known = index > 0
    argv = tuple(scenario.working[index]["argv"])
    scenario._script[argv] = (
        index,
        dict(scenario.working[index], stdout=b"", status=-15, timed_out=True),
    )
    with scenario:
        run = scenario.execute()
        assert run.failure is not None
        attempts = scenario.journal.snapshot()
        timeout = assert_positive_timeout(attempts)
        assert timeout.argv == argv
        # Production _run retains the delivered result before rejecting timeout;
        # its caller's assignment is never reached on this failure path.
        results = (
            run.execution.startup_result,
            *(op.result for op in run.execution.infrastructure_operations),
            run.execution.command_result,
        )
        assert results[index] is timeout.result is timeout.owner.result
        assert results[index].timed_out is True
        assert results[index].returncode == -15
        assert all(
            result is not None and not result.timed_out for result in results[:index]
        )
        assert all(result is None for result in results[index + 1 :])
        assert tuple(a.argv for a in attempts if a.phase == "working") == tuple(
            tuple(s["argv"]) for s in scenario.working[: index + 1]
        )
        assert run.execution.container_id == (ID if known else None)
        assert_identity_cleanup(
            run, attempts, cleanup=tuple(tuple(s["argv"]) for s in scenario.cleanup)
        )
        if known:
            assert run.cleanup.confirmed_absent
        else:
            assert not run.cleanup.confirmed_absent
            assert run.cleanup.observe().failures
        scenario.assert_helpers_settled()


@pytest.mark.parametrize(
    "mode",
    [
        "absent",
        "open-denied",
        "read",
        "read-enoent",
        "close",
        "close-enoent",
        "both",
        "interrupt",
        "close-interrupt",
        "success",
    ],
)
def test_deadline_record_reader_sole_owner_fault_matrix(monkeypatch, mode):
    """Synthetic fd number only; distinguish open absence from read/release ENOENT."""
    import footman.filesystem.descriptors as descriptors
    from tests.dispatch_deadline_support import read_child_record

    owners = []
    constructor = descriptors.OwnedDescriptor

    def adopt(*args, **kwargs):
        owner = constructor(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(descriptors, "OwnedDescriptor", adopt)
    primary = (
        KeyboardInterrupt("synthetic read control")
        if mode == "interrupt"
        else FileNotFoundError("synthetic read ENOENT")
        if mode == "read-enoent"
        else OSError("synthetic read failure")
    )
    close = (
        KeyboardInterrupt("synthetic close control")
        if mode == "close-interrupt"
        else FileNotFoundError("synthetic close ENOENT")
        if mode == "close-enoent"
        else OSError("synthetic close failure")
    )
    attempts = []

    class Ops:
        def close(self, fd):
            attempts.append(fd)
            if mode in ("close", "close-enoent", "both", "close-interrupt"):
                raise close

    def acquire(path, flags):
        if mode == "absent":
            raise FileNotFoundError("synthetic open ENOENT")
        if mode == "open-denied":
            raise PermissionError("synthetic no-follow/permission refusal")
        return 123456  # No actual OS fd acquired by this synthetic fixture.

    def read(fd, size):
        if mode in ("read", "read-enoent", "both", "interrupt"):
            raise primary
        return b""

    def invoke():
        return read_child_record(
            "synthetic", open_record=acquire, read_record=read, ops=Ops()
        )

    if mode in ("absent", "success"):
        observation = invoke()
        assert observation.availability == (
            "absent" if mode == "absent" else "available"
        )
    else:
        expected = (
            PermissionError
            if mode == "open-denied"
            else type(primary)
            if mode in ("read", "read-enoent", "both", "interrupt")
            else type(close)
        )
        with pytest.raises(expected) as caught:
            invoke()
        if mode != "open-denied":
            assert caught.value is (
                primary
                if mode in ("read", "read-enoent", "both", "interrupt")
                else close
            )
        if mode == "both":
            assert close in getattr(primary, "_transaction_secondary", ())
    assert attempts == ([] if mode in ("absent", "open-denied") else [123456])
    for owner in owners:
        owner.close()
        owner.close()
    assert len(attempts) == len(owners)
    # Independent record reads remain independently releasable after a fault.
    other = read_child_record(
        "sibling",
        open_record=lambda *a: 654321,
        read_record=lambda *a: b"",
        ops=SimpleNamespace(close=lambda fd: attempts.append(fd)),
    )
    assert other.availability == "available"
    assert attempts[-1] == 654321


def test_request_is_journaled_before_acquisition(tmp_path):
    """Parent admission is observable even when no child can write a record."""
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), (
        "required parent-side deadline launch journal is absent"
    )
    support = importlib.import_module("tests.dispatch_deadline_support")
    argv = ("docker", "run", "--pull=never", "fixture-image")
    failure = OSError("synthetic acquisition failure; no child or fd acquired")
    observed = []
    journal = support.LaunchJournal(working=(argv,), cleanup=())

    def acquire(actual, **options):
        snapshot = journal.snapshot()
        observed.append(snapshot)
        assert len(snapshot) == 1
        attempt = snapshot[0]
        assert attempt.ordinal == 0
        assert attempt.phase == "working"
        assert attempt.argv == argv == tuple(actual)
        assert attempt.deadline_seconds == options["deadline_seconds"] == 0.3
        assert attempt.grace_seconds == options["grace_seconds"] == 1
        assert attempt.acquisition == "requested"
        assert attempt.owner is None and attempt.error is None
        assert list(tmp_path.iterdir()) == []
        raise failure

    with pytest.raises(OSError) as caught:
        journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    assert caught.value is failure
    assert observed[0][0].acquisition == "requested", "snapshots must not mutate"
    attempt = journal.snapshot()[0]
    assert attempt.acquisition == "failed"
    assert attempt.error is failure and attempt.owner is None


@pytest.mark.parametrize(
    "failure", [OSError("synthetic acquisition"), ValueError("synthetic setup")]
)
def test_failed_acquisition_is_not_an_owner(failure):
    """Synthetic acquisition raises before any descriptor or child exists."""
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required acquisition classification is absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    argv = ("docker", "run", "fixture")
    journal = support.LaunchJournal(working=(argv,), cleanup=())

    def acquire(*args, **kwargs):
        raise failure

    with pytest.raises(type(failure)) as caught:
        journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    assert caught.value is failure
    (attempt,) = journal.snapshot()
    assert attempt.acquisition == "failed"
    assert attempt.error is failure and attempt.owner is None


@pytest.mark.parametrize(
    "control", [KeyboardInterrupt("synthetic"), SystemExit("synthetic")]
)
def test_acquisition_interruption_keeps_identity(control):
    """Synthetic controls acquire no resource and retain interruption authority."""
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required interruption classification is absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    argv = ("docker", "run", "fixture")
    journal = support.LaunchJournal(working=(argv,), cleanup=())

    def acquire(*args, **kwargs):
        raise control

    with pytest.raises(type(control)) as caught:
        journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    assert caught.value is control
    (attempt,) = journal.snapshot()
    assert attempt.acquisition == "interrupted"
    assert attempt.error is control and attempt.owner is None


@pytest.mark.parametrize("prefix", range(6))
def test_independent_script_cursors_accept_every_working_prefix(source, prefix):
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required independent script cursors are absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    working = tuple(tuple(step["argv"]) for step in steps(source))
    cleanup = tuple(tuple(step["argv"]) for step in removal())
    journal = support.LaunchJournal(working=working, cleanup=cleanup)
    owners = []  # Explicitly synthetic no-fd tokens, not real child evidence.

    def acquire(*args, **kwargs):
        owner = object()
        owners.append(owner)
        return owner

    expected = (*working[:prefix], *cleanup)
    for argv in expected:
        journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    observations = journal.snapshot()
    assert tuple(a.argv for a in observations) == expected
    assert (
        tuple(a.phase for a in observations) == ("working",) * prefix + ("cleanup",) * 2
    )
    assert tuple(a.owner for a in observations) == tuple(owners)
    assert all(a.acquisition == "acquired" for a in observations)


@pytest.mark.parametrize(
    "bad", ["reordered", "unknown", "operand", "extra", "late-working"]
)
def test_independent_script_cursors_reject_nonliteral_requests(source, bad):
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required strict script rejection is absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    working = tuple(tuple(step["argv"]) for step in steps(source))
    cleanup = tuple(tuple(step["argv"]) for step in removal())
    journal = support.LaunchJournal(working=working, cleanup=cleanup)
    acquired = []

    def acquire(argv, **kwargs):
        acquired.append(tuple(argv))
        return object()  # Synthetic; no descriptor or child.

    if bad == "extra":
        for argv in (*working, *cleanup):
            journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
        invalid = cleanup[-1]
    elif bad == "late-working":
        journal.launch(
            cleanup[0], acquire=acquire, deadline_seconds=0.3, grace_seconds=1
        )
        invalid = working[0]
    else:
        invalid = {
            "reordered": working[1],
            "unknown": ("docker", "unknown"),
            "operand": (*cleanup[0][:-1], "guessed-id"),
        }[bad]
    before = tuple(acquired)
    with pytest.raises(AssertionError):
        journal.launch(invalid, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    assert tuple(acquired) == before
    rejected = journal.snapshot()[-1]
    assert rejected.argv == invalid and rejected.owner is None
    assert rejected.acquisition == "failed"


def test_journal_interleaving_keeps_stable_ordinals_and_results():
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required synchronized launch journal is absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    working = ("docker", "run", "fixture")
    cleanup = ("docker", "rm", "literal-id")
    journal = support.LaunchJournal(working=(working,), cleanup=(cleanup,))
    entered = threading.Event()
    release = threading.Event()
    work_owner, cleanup_owner = object(), object()  # Synthetic no-fd owners.
    work_result, cleanup_result = object(), object()  # Synthetic retained values.

    def acquire_work(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test failed to release acquisition"
        return work_owner

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            journal.launch,
            working,
            acquire=acquire_work,
            deadline_seconds=0.3,
            grace_seconds=1,
        )
        try:
            assert entered.wait(5), "working acquisition never entered"
            before = journal.snapshot()
            assert before[0].acquisition == "requested"
            assert (
                journal.launch(
                    cleanup,
                    acquire=lambda *a, **k: cleanup_owner,
                    deadline_seconds=1,
                    grace_seconds=1,
                )
                is cleanup_owner
            )
            journal.observe_result(1, cleanup_result)
            middle = journal.snapshot()
            assert tuple(a.ordinal for a in middle) == (0, 1)
            assert middle[0].owner is None and middle[0].result is None
            assert middle[1].owner is cleanup_owner
            assert middle[1].result is cleanup_result
        finally:
            release.set()
        assert pending.result(timeout=5) is work_owner
    journal.observe_result(0, work_result)
    final = journal.snapshot()
    assert final[0].owner is work_owner and final[0].result is work_result
    assert final[1].owner is cleanup_owner and final[1].result is cleanup_result
    assert before[0].acquisition == "requested" and before[0].result is None
    assert middle[0].owner is None
    with pytest.raises((AttributeError, TypeError)):
        final[0].ordinal = 99


@pytest.mark.parametrize(
    "defect", ["missing", "wrong-argv", "stdin", "schema", "incomplete", "release"]
)
def test_existing_sequence_keeps_strict_records_and_settlement(tmp_path, defect):
    """Synthetic record/owner fixtures do not establish real child execution."""
    from footman.process import FailureInfo, FailureKind, FailurePhase, LifecycleResult
    from tests.dispatcher_harness import write_bytes

    sequence = DockerSequence(tmp_path, [])
    argv = ["docker", "run", "fixture"]
    record = tmp_path / "synthetic-record.json"
    sequence.parameters.append((argv, {}))
    sequence.stub.records.append(record)
    release = OSError("synthetic close failure")
    failures = (
        (FailureInfo(release, FailureKind.ORDINARY, FailurePhase.RELEASE),)
        if defect == "release"
        else ()
    )
    sequence.owners.append(
        SimpleNamespace(
            cleanup_status=LifecycleResult(defect != "incomplete", failures)
        )
    )
    if defect != "missing":
        data = {"argv": argv[1:], "stdin_eof": True}
        if defect == "wrong-argv":
            data["argv"] = ["wrong"]
        if defect == "stdin":
            data["stdin_eof"] = False
        if defect == "schema":
            data = []
        write_bytes(record, json.dumps(data).encode())
    if defect == "release":
        # Settlement and valid records do not certify failure-free cleanup.
        sequence.assert_settled()
        support = importlib.import_module("tests.dispatch_deadline_support")
        assert callable(getattr(support, "assert_lifecycle_failures", None))
        with pytest.raises(AssertionError):
            support.assert_lifecycle_failures(sequence.owners[0].cleanup_status)
    else:
        with pytest.raises((AssertionError, OSError, TypeError, KeyError)):
            sequence.assert_settled()


@pytest.mark.parametrize("operation", ["domain", "interruption"])
def test_settlement_does_not_reclassify_expected_operation_failure(tmp_path, operation):
    """Synthetic settled owners retain errors without acquiring any resource."""
    from footman.process import (
        FailureInfo,
        FailureKind,
        FailurePhase,
        LifecycleResult,
        ProcessStream,
    )

    support = importlib.import_module("tests.dispatch_deadline_support")
    assert callable(getattr(support, "assert_lifecycle_failures", None)), (
        "missing explicit failure-policy check"
    )
    error = (
        OSError("synthetic expected operation")
        if operation == "domain"
        else KeyboardInterrupt("synthetic expected callback")
    )
    info = FailureInfo(
        error,
        FailureKind.ORDINARY if operation == "domain" else FailureKind.INTERRUPTION,
        FailurePhase.ACTION if operation == "domain" else FailurePhase.CALLBACK,
        None if operation == "domain" else ProcessStream.STDOUT,
    )
    status = LifecycleResult(True, (info,))
    sequence = DockerSequence(tmp_path, [])
    sequence.owners.append(SimpleNamespace(cleanup_status=status))
    observations = sequence.assert_owners_settled()
    assert observations == (status,)
    sequence.assert_settled()
    with pytest.raises(AssertionError):
        support.assert_lifecycle_failures(status)
    support.assert_lifecycle_failures(status, expected=(info,))
    support.assert_lifecycle_failures(status, expected=(info,))
    assert status.failures[0].error is error
    # An expected operation failure cannot excuse a sibling release defect.
    close = OSError("synthetic close after expected operation")
    combined = LifecycleResult(
        True, (info, FailureInfo(close, FailureKind.ORDINARY, FailurePhase.RELEASE))
    )
    with pytest.raises(AssertionError):
        support.assert_lifecycle_failures(combined, expected=(info,))
    assert combined.failures[0].error is error and combined.failures[1].error is close
    # Nor can it make a mandatory child record optional.
    sequence.parameters.append((["docker", "run", "fixture"], {}))
    sequence.stub.records.append(tmp_path / "absent-expected-operation.json")
    with pytest.raises(FileNotFoundError):
        sequence.assert_settled()


@pytest.mark.parametrize(
    "defect",
    [
        "other-identity",
        "other-kind",
        "other-phase",
        "other-stream",
        "extra",
        "missing",
        "duplicate",
        "release",
        "setup",
        "unexpected",
    ],
)
def test_deadline_failure_policy_rejects_unexpected_or_cleanup_failures(defect):
    """Synthetic typed observations; matching text cannot authorize a failure."""
    from footman.process import (
        FailureInfo,
        FailureKind,
        FailurePhase,
        LifecycleResult,
        ProcessStream,
    )

    support = importlib.import_module("tests.dispatch_deadline_support")
    assert callable(getattr(support, "assert_lifecycle_failures", None)), (
        "missing explicit failure-policy check"
    )
    error = KeyboardInterrupt("synthetic expected callback")
    expected = FailureInfo(
        error, FailureKind.INTERRUPTION, FailurePhase.CALLBACK, ProcessStream.STDOUT
    )
    actual = (expected,)
    if defect == "other-identity":
        actual = (
            FailureInfo(
                KeyboardInterrupt(str(error)),
                expected.kind,
                expected.phase,
                expected.stream,
            ),
        )
    elif defect == "other-kind":
        expected = FailureInfo(
            OSError("synthetic operation"), FailureKind.ORDINARY, FailurePhase.ACTION
        )
        actual = (FailureInfo(expected.error, FailureKind.UNEXPECTED, expected.phase),)
    elif defect == "other-phase":
        actual = (
            FailureInfo(error, expected.kind, FailurePhase.READ, expected.stream),
        )
    elif defect == "other-stream":
        actual = (
            FailureInfo(error, expected.kind, expected.phase, ProcessStream.STDERR),
        )
    elif defect in ("release", "setup", "unexpected"):
        expected = FailureInfo(
            OSError("synthetic failure"),
            FailureKind.UNEXPECTED if defect == "unexpected" else FailureKind.ORDINARY,
            FailurePhase.RELEASE
            if defect == "release"
            else FailurePhase.SETUP
            if defect == "setup"
            else FailurePhase.ACTION,
        )
        actual = (
            expected,
        )  # Even exact identity cannot whitelist a cleanup/defect failure.
    elif defect == "extra":
        actual = (
            expected,
            FailureInfo(
                OSError("synthetic close"), FailureKind.ORDINARY, FailurePhase.RELEASE
            ),
        )
    elif defect == "missing":
        actual = ()
    elif defect == "duplicate":
        actual = (expected, expected)
    status = LifecycleResult(True, actual)
    with pytest.raises(AssertionError):
        support.assert_lifecycle_failures(status, expected=(expected,))
    assert status.failures is actual


def test_settlement_and_failure_policy_are_independent(tmp_path):
    from footman.process import LifecycleResult

    support = importlib.import_module("tests.dispatch_deadline_support")
    assert callable(getattr(support, "assert_lifecycle_failures", None)), (
        "missing explicit failure-policy check"
    )
    status = LifecycleResult(False)
    sequence = DockerSequence(tmp_path, [])
    sequence.owners.append(SimpleNamespace(cleanup_status=status))
    support.assert_lifecycle_failures(status)  # No failures is not proof of settlement.
    with pytest.raises(AssertionError):
        sequence.assert_owners_settled()
    with pytest.raises(AssertionError):
        sequence.assert_settled()
    assert sequence.owners[0].cleanup_status is status


def test_sequence_settlement_and_record_checks_are_separate(tmp_path):
    """Separation must not make the existing combined API permissive."""
    sequence = DockerSequence(tmp_path, [])
    assert callable(getattr(sequence, "assert_owners_settled", None)), (
        "missing settlement-only check"
    )
    assert callable(getattr(sequence, "assert_records", None)), (
        "missing strict record-only check"
    )
    sequence.parameters.append((["docker", "run", "fixture"], {}))
    sequence.stub.records.append(tmp_path / "absent.json")
    sequence.assert_owners_settled()
    with pytest.raises(FileNotFoundError):
        sequence.assert_records()
    with pytest.raises(FileNotFoundError):
        sequence.assert_settled()


def test_journal_diagnostics_are_bounded_without_truncating_observations():
    from footman.process import (
        LifecycleResult,
        ProcessResult,
        ProcessStream,
        StreamOutcome,
    )

    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required bounded deadline diagnostics are absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    argv = ("docker", "exec", "literal-id", "界" * 100000)
    journal = support.LaunchJournal(working=(argv,), cleanup=())
    owner = SimpleNamespace(
        cleanup_status=LifecycleResult(True)
    )  # Synthetic no-fd owner.
    journal.launch(
        argv, acquire=lambda *a, **k: owner, deadline_seconds=0.3, grace_seconds=1
    )
    result = ProcessResult(
        b"out",
        b"err",
        -15,
        True,
        LifecycleResult(True),
        (
            (ProcessStream.STDOUT, StreamOutcome.EOF),
            (ProcessStream.STDERR, StreamOutcome.ABORTED),
        ),
    )
    journal.observe_result(0, result)
    text = journal.render_diagnostics()
    assert isinstance(text, str)
    assert len(text.encode("utf-8")) <= 65536
    for required in (
        "working",
        "ordinal",
        "argv",
        "deadline_seconds",
        "grace_seconds",
        "acquired",
        "timed_out",
        "returncode",
        "stream",
        "record",
        "cleanup",
        "truncated",
    ):
        assert required in text
    (attempt,) = journal.snapshot()
    assert attempt.argv == argv and attempt.result is result and attempt.owner is owner


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("synthetic render"),
        OSError("synthetic read"),
        KeyboardInterrupt("synthetic render"),
    ],
)
def test_diagnostic_failure_preserves_primary_authority(monkeypatch, failure):
    support_path = Path(__file__).with_name("dispatch_deadline_support.py")
    assert support_path.is_file(), "required diagnostic precedence is absent"
    support = importlib.import_module("tests.dispatch_deadline_support")
    journal = support.LaunchJournal(working=(), cleanup=())
    primary = AssertionError("original admission failure")

    def broken():
        raise failure

    monkeypatch.setattr(journal, "render_diagnostics", broken)
    if isinstance(failure, Exception):
        with pytest.raises(AssertionError) as caught:
            try:
                raise primary
            except AssertionError:
                journal.annotate_failure(primary)
                raise
        assert caught.value is primary
    else:
        with pytest.raises(KeyboardInterrupt) as caught:
            journal.annotate_failure(primary)
        assert caught.value is failure


@pytest.mark.parametrize(
    "mode",
    [
        "operation-close",
        "sole-close",
        "interruption",
        "repeat",
        "independent",
        "handoff",
    ],
)
@pytest.mark.parametrize("handoff", ["publication", "caller"])
def test_journal_handoff_fault_matrix(mode, handoff):
    """Synthetic no-fd lifecycle controls, not evidence of real child execution."""
    from footman.filesystem.cleanup import CleanupFailures
    from footman.process import LifecycleResult

    support = importlib.import_module("tests.dispatch_deadline_support")
    argv = ("docker", "run", "fixture")
    journal = support.LaunchJournal(working=(argv,), cleanup=())
    primary = (
        KeyboardInterrupt("synthetic publication")
        if mode == "interruption"
        else OSError("synthetic publication")
    )
    close = OSError("synthetic close")
    releases = []

    class SyntheticOwner:
        def __init__(self):
            self.exits = 0
            self.complete = mode != "repeat"
            self.failures = []
            self.continuation = object()  # Synthetic retained continuation token.
            self.budget = object()  # Re-observation must not replace the allowance.

        def __enter__(self):
            return self

        def __exit__(self, exc_type, error, traceback):
            if self.exits:
                return
            self.exits += 1
            failures = CleanupFailures(error)

            def first():
                releases.append("first")
                if mode in ("operation-close", "sole-close", "independent"):
                    self.failures.append(close)
                    raise close

            failures.run(first, ordinary=(OSError,))
            failures.run(lambda: releases.append("second"), ordinary=(OSError,))
            close_failure = failures.complete()
            if close_failure is not None:
                raise close_failure

        @property
        def cleanup_status(self):
            return LifecycleResult(self.complete)

    owner = SyntheticOwner()

    class PublicationLock:
        def __init__(self):
            self.failed = False

        def __enter__(self):
            if not self.failed:
                self.failed = True
                raise primary

        def __exit__(self, *args):
            return False

    def acquire(*args, **kwargs):
        if mode != "sole-close" and handoff == "publication":
            journal._lock = PublicationLock()
        return owner

    expected = close if mode == "sole-close" else primary
    with pytest.raises(type(expected)) as caught:
        returned = journal.launch(
            argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1
        )
        with returned:
            if mode != "sole-close":
                raise primary
    assert caught.value is expected
    assert releases == ["first", "second"] and owner.exits == 1
    if mode in ("operation-close", "sole-close", "independent"):
        assert owner.failures == [close]
        if mode != "sole-close":
            assert close in primary._transaction_secondary
    (attempt,) = journal.snapshot()
    assert attempt.owner is owner, "a failed publication must retain the acquired owner"
    assert attempt.acquisition == "acquired"
    before = owner.cleanup_status
    continuation, budget = owner.continuation, owner.budget
    owner.__exit__(None, None, None)
    assert owner.cleanup_status == before
    assert owner.continuation is continuation and owner.budget is budget
    assert releases == ["first", "second"] and owner.exits == 1
    if mode == "repeat":
        assert not before.complete, (
            "incomplete owner must remain observable without a fresh budget"
        )


@pytest.mark.parametrize("primary_control", [False, True])
@pytest.mark.parametrize("publication_control", [False, True])
def test_acquisition_failure_survives_failed_journal_publication(
    primary_control, publication_control
):
    """Synthetic failures before acquisition: no resource exists to release."""
    from tests.dispatch_deadline_support import LaunchJournal

    argv = ("docker", "run", "fixture")
    journal = LaunchJournal(working=(argv,), cleanup=())
    primary = (
        KeyboardInterrupt("synthetic acquisition")
        if primary_control
        else OSError("synthetic acquisition")
    )
    secondary = (
        SystemExit("synthetic publication")
        if publication_control
        else OSError("synthetic publication")
    )

    class FailOnce:
        failed = False

        def __enter__(self):
            if not self.failed:
                self.failed = True
                raise secondary

        def __exit__(self, *args):
            return False

    def acquire(*args, **kwargs):
        journal._lock = FailOnce()
        raise primary

    expected = primary if primary_control or not publication_control else secondary
    with pytest.raises(BaseException) as caught:
        journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    assert caught.value is expected
    (attempt,) = journal.snapshot()
    assert attempt.owner is None and attempt.error is primary
    assert attempt.acquisition == ("interrupted" if primary_control else "failed")
    displaced = secondary if expected is primary else primary
    assert displaced in expected._transaction_secondary


@pytest.mark.parametrize("control", [False, True])
def test_failed_handoff_keeps_primary_when_owner_exit_raises(control):
    """Synthetic no-fd owner raises an ordinary error at its exit boundary."""
    from tests.dispatch_deadline_support import LaunchJournal

    argv = ("docker", "run", "fixture")
    journal = LaunchJournal(working=(argv,), cleanup=())
    primary = (
        KeyboardInterrupt("synthetic handoff")
        if control
        else OSError("synthetic handoff")
    )
    close = OSError("synthetic exit release")
    calls = []

    class Owner:
        def __exit__(self, *args):
            calls.append(args)
            raise close

    owner = Owner()

    class FailOnce:
        failed = False

        def __enter__(self):
            if not self.failed:
                self.failed = True
                raise primary

        def __exit__(self, *args):
            return False

    def acquire(*args, **kwargs):
        journal._lock = FailOnce()
        return owner

    with pytest.raises(BaseException) as caught:
        journal.launch(argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1)
    assert caught.value is primary
    assert close in primary._transaction_secondary
    (attempt,) = journal.snapshot()
    assert attempt.owner is owner and attempt.error is primary
    assert attempt.acquisition == "acquired"
    assert len(calls) == 1 and calls[0][1] is primary
    journal.snapshot()
    assert len(calls) == 1


def test_failed_handoff_snapshots_remain_coherent_during_cleanup():
    """Real synchronization around synthetic no-fd cleanup, never sleeps."""
    from tests.dispatch_deadline_support import LaunchJournal

    argv = ("docker", "run", "fixture")
    journal = LaunchJournal(working=(argv,), cleanup=())
    primary = OSError("synthetic publication")
    entered, release = threading.Event(), threading.Event()
    exits = []

    class Owner:
        def __exit__(self, *args):
            exits.append(args)
            entered.set()
            assert release.wait(5)

    owner = Owner()

    class FailOnce:
        failed = False

        def __enter__(self):
            if not self.failed:
                self.failed = True
                raise primary

        def __exit__(self, *args):
            return False

    def acquire(*args, **kwargs):
        journal._lock = FailOnce()
        return owner

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            journal.launch, argv, acquire=acquire, deadline_seconds=0.3, grace_seconds=1
        )
        try:
            assert entered.wait(5)
            (attempt,) = journal.snapshot()
            assert attempt.owner is owner and attempt.error is primary
            assert attempt.acquisition == "acquired"
        finally:
            release.set()
        with pytest.raises(OSError) as caught:
            pending.result(timeout=5)
        assert caught.value is primary
    assert len(exits) == 1


@pytest.mark.parametrize(
    "control", [KeyboardInterrupt("synthetic primary"), SystemExit("synthetic primary")]
)
def test_diagnostics_preserve_first_interruption(monkeypatch, control):
    from tests.dispatch_deadline_support import LaunchJournal

    journal = LaunchJournal(working=(), cleanup=())

    def render():
        raise KeyboardInterrupt("synthetic secondary renderer interruption")

    monkeypatch.setattr(journal, "render_diagnostics", render)
    with pytest.raises(BaseException) as caught:
        try:
            raise control
        except BaseException:
            journal.annotate_failure(control)
            raise
    assert caught.value is control


def test_journal_retains_record_observations_without_reading_or_tolerating_them():
    """Explicit synthetic bytes are diagnostics, not real child execution proof."""
    from tests.dispatch_deadline_support import LaunchJournal

    argv = ("docker", "run", "fixture")
    journal = LaunchJournal(working=(argv,), cleanup=())
    journal.launch(
        argv, acquire=lambda *a, **k: object(), deadline_seconds=0.3, grace_seconds=1
    )
    assert callable(getattr(journal, "observe_record", None)), (
        "missing retained record diagnostics"
    )
    initial = journal.snapshot()[0]
    journal.observe_record(0, availability="absent")
    absent = journal.snapshot()[0]
    assert absent.record.availability == "absent"
    assert "absent" in journal.render_diagnostics()
    payload = b"synthetic complete or invalid bytes" * 5000
    journal.observe_record(0, availability="available", data=payload)
    observed = journal.snapshot()[0]
    assert observed.record.data is payload
    text = journal.render_diagnostics()
    assert "available" in text and "truncated" in text
    assert len(text.encode("utf-8")) <= 65536
    assert absent.record.availability == "absent"
    assert initial.record.availability == "unobserved"
    assert observed.record.data == payload
    read_error = PermissionError("synthetic record read failure")
    journal.observe_record(0, availability="read-failed", error=read_error)
    assert journal.snapshot()[0].record.error is read_error
    assert "read-failed" in journal.render_diagnostics()
    assert observed.record.data is payload


@pytest.mark.parametrize("ordinal", [-1, 1, True, False, "0"])
@pytest.mark.parametrize("kind", ["result", "record"])
def test_journal_rejects_invalid_observation_ordinals(ordinal, kind):
    from tests.dispatch_deadline_support import LaunchJournal

    argv = ("docker", "run", "fixture")
    journal = LaunchJournal(working=(argv,), cleanup=())
    journal.launch(
        argv, acquire=lambda *a, **k: object(), deadline_seconds=0.3, grace_seconds=1
    )
    with pytest.raises((AssertionError, TypeError, IndexError)):
        if kind == "result":
            journal.observe_result(ordinal, object())
        else:
            journal.observe_record(ordinal, availability="absent")
    assert journal.snapshot()[0].result is None
    assert journal.snapshot()[0].record.availability == "unobserved"


def _logical_support():
    support = importlib.import_module("tests.dispatch_deadline_support")
    assert hasattr(support, "LogicalScenario"), (
        "required no-fd logical scenario support is absent"
    )
    return support


def test_logical_helper_streams_counts_and_expenditure():
    from footman.process import ProcessStream, StreamOutcome
    from tests.clock_support import ManualClock

    support = _logical_support()
    clock = ManualClock()
    owner = support.ScriptedHelper(
        {"stdout": b"output", "stderr": b"error"},
        clock,
        expenditure=(0.125, "startup complete"),
    )
    chunks, endings = [], []
    with owner:
        result = owner.stream(
            lambda *pair: chunks.append(pair),
            on_stream_end=lambda *pair: endings.append(pair),
        )
    assert chunks == [
        (ProcessStream.STDOUT, b"output"),
        (ProcessStream.STDERR, b"error"),
    ]
    assert endings == [(s, StreamOutcome.EOF) for s in ProcessStream]
    assert result.returncode == 0 and not result.timed_out
    assert result.lifecycle.complete and result.lifecycle.failures == ()
    assert result.stream_outcomes == tuple(endings)
    assert owner.collections == owner.context_exits == 1
    assert not hasattr(owner, "fileno") and not hasattr(owner, "process")
    before = clock.journal()
    assert [(a.seconds, a.label) for a in before] == [(0.125, "startup complete")]
    for _ in range(10):
        assert clock.monotonic() == 0.125
    assert clock.journal() == before
    with pytest.raises(RuntimeError):
        owner.stream(lambda *_: None)
    assert owner.collections == 1


@pytest.mark.parametrize("position", [1, 2, 3])
def test_logical_boundary_oracle_rejects_forbidden_request(source, position):
    support = _logical_support()
    working = tuple(tuple(s["argv"]) for s in steps(source))
    cleanup = tuple(tuple(s["argv"]) for s in removal())
    valid = (*working[:position], *cleanup)
    support.assert_policy_trace(
        valid, working=working, cleanup=cleanup, prefix=position
    )
    with pytest.raises(AssertionError):
        support.assert_policy_trace(
            (*working[: position + 1], *cleanup),
            working=working,
            cleanup=cleanup,
            prefix=position,
        )


def test_logical_cumulative_oracle_rejects_renewed_budget():
    support = _logical_support()
    support.assert_allowances((0.5, 0.375, 0.25), (0.5, 0.375, 0.25))
    with pytest.raises(AssertionError):
        support.assert_allowances((0.5, 0.5, 0.5), (0.5, 0.375, 0.25))
    with pytest.raises(AssertionError):
        support.assert_allowances((0.0,), (0.0,))


@pytest.mark.parametrize("exit_kind", ["normal", "assertion", "interruption"])
def test_logical_scenario_restores_only_container_clock(source, exit_kind):
    import footman.container as container
    import footman.process as process
    import footman.supervision as supervision

    support = _logical_support()
    originals = (container.time, process.time, supervision.time)
    failure = {
        "normal": None,
        "assertion": AssertionError("synthetic"),
        "interruption": KeyboardInterrupt("synthetic"),
    }[exit_kind]
    caught = None
    try:
        with support.LogicalScenario(
            source, config(timeout_seconds=0.3, cleanup_grace_seconds=1)
        ) as scenario:
            assert container.time is not originals[0]
            assert process.time is originals[1] and supervision.time is originals[2]
            assert scenario.clock.monotonic() == 0
            assert scenario.run.execution is not None
            if failure is not None:
                raise failure
            scenario.execute()
    except BaseException as error:
        caught = error
    assert caught is failure
    assert (container.time, process.time, supervision.time) == originals


@pytest.mark.parametrize("position", [1, 2, 3])
@pytest.mark.parametrize("spent", [0.3, 0.4, 0.125], ids=["exact", "past", "positive"])
def test_shared_deadline_suppresses_downstream_work(source, position, spent):
    support = _logical_support()
    label = f"before working {position}"
    with support.LogicalScenario(
        source,
        config(timeout_seconds=0.3, cleanup_grace_seconds=1),
        expenditures=((position - 1, (spent, label)),),
    ) as scenario:
        run = scenario.execute()
        attempts = scenario.journal.snapshot()
        working = tuple(tuple(s["argv"]) for s in scenario.working)
        cleanup = tuple(tuple(s["argv"]) for s in scenario.cleanup)
        prefix = 5 if spent < 0.3 else position
        support.assert_policy_trace(
            tuple(a.argv for a in attempts),
            working=working,
            cleanup=cleanup,
            prefix=prefix,
        )
        assert all(a.acquisition == "acquired" for a in attempts)
        assert (
            tuple(a.phase for a in attempts) == ("working",) * prefix + ("cleanup",) * 2
        )
        assert run.cleanup.confirmed_absent and run.snapshot_deleted
        assert not run.unresolved_resources
        if spent < 0.3:
            assert run.failure is None
            assert attempts[position].argv == working[position]
            support.assert_allowances(
                (attempts[position].deadline_seconds,), (0.3 - spent,)
            )
        else:
            assert run.failure is not None
            assert working[position] not in tuple(a.argv for a in attempts)
        assert [(a.seconds, a.label) for a in scenario.clock.journal()] == [
            (spent, label)
        ]
        before = scenario.clock.journal()
        for _ in range(10):
            scenario.clock.monotonic()
        assert scenario.clock.journal() == before
        scenario.assert_helpers_settled()


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
def test_nonzero_terminal_path_removes_only_recorded_id(tmp_path, source, stage):
    script = steps(source)[: stage + 1]
    script[stage]["status"] = 125
    script.extend(removal())
    run, sequence = run_case(tmp_path, source, script=script)
    assert run.cleanup.confirmed_absent and run.snapshot_deleted
    if stage < 4:
        assert run.failure is not None
        assert run.execution.command_result is None
    else:
        assert run.execution.command_result.returncode == 125
    assert len(sequence.calls()) == stage + 3
    sequence.assert_settled()


def test_cumulative_deadline_not_fresh_per_child(source):
    support = _logical_support()
    charges = (
        (0, (0.125, "startup")),
        (1, (0.125, "inspection")),
        (2, (0.25, "mkdir")),
    )
    with support.LogicalScenario(
        source,
        config(timeout_seconds=0.5, cleanup_grace_seconds=2),
        expenditures=charges,
    ) as scenario:
        run = scenario.execute()
        attempts = scenario.journal.snapshot()
        working = tuple(tuple(s["argv"]) for s in scenario.working)
        cleanup = tuple(tuple(s["argv"]) for s in scenario.cleanup)
        support.assert_policy_trace(
            tuple(a.argv for a in attempts), working=working, cleanup=cleanup, prefix=3
        )
        support.assert_allowances(
            tuple(a.deadline_seconds for a in attempts if a.phase == "working"),
            (0.5, 0.375, 0.25),
        )
        assert run.failure is not None and run.cleanup.confirmed_absent
        assert run.snapshot_deleted and not run.unresolved_resources
        assert [(a.seconds, a.label) for a in scenario.clock.journal()] == [
            charge for _, charge in charges
        ]
        before = scenario.clock.journal()
        for _ in range(10):
            assert scenario.clock.monotonic() == 0.5
        assert scenario.clock.journal() == before
        scenario.assert_helpers_settled()


@pytest.mark.parametrize("stage", [0, 4])
def test_interruption_retains_authority_and_known_identity_cleanup(
    tmp_path, source, stage
):
    module = api()
    control = KeyboardInterrupt("caller")
    script = steps(source)[: stage + 1]
    if stage:
        script.extend(removal())
    directory = tmp_path / "interrupt-stub"
    directory.mkdir()
    sequence = DockerSequence(directory, script)
    run = module.RunSupervisor(config(), source, launch=sequence.launch)

    def interrupt(*args):
        raise control

    # Supply data so the domain callback is actually entered.
    if stage:
        script[stage]["stdout"] = b"ready"
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute(
            interrupt if stage == 0 else lambda *_: None,
            interrupt if stage else lambda *_: None,
        )
    assert caught.value is control
    assert run.failure is control
    assert run.wait_cleanup(2).complete
    if stage:
        assert run.cleanup.confirmed_absent and run.snapshot_deleted
    else:
        assert run.unresolved_resources and source.path.exists()
    sequence.assert_settled()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_cleanup_observation_requires_bounded_timeout(source, timeout):
    run = api().RunSupervisor(config(), source)
    with pytest.raises(ValueError):
        run.wait_cleanup(timeout)


def test_command_setup_failure_still_removes_known_container(tmp_path, source):
    from footman.process import OwnedProcess

    directory = tmp_path / "setup-stub"
    directory.mkdir()
    sequence = DockerSequence(directory, [*steps(source)[:4], *removal()])
    attempted = False

    def launch(argv, **kwargs):
        nonlocal attempted
        if len(sequence.parameters) == 4 and not attempted:
            attempted = True

            def unavailable(*args, **options):
                raise OSError("command client unavailable")

            return OwnedProcess.launch(argv, factory=unavailable, **kwargs)
        return sequence.launch(argv, **kwargs)

    run = api().RunSupervisor(config(), source, launch=launch)
    run.execute(lambda *_: None, lambda *_: None)
    assert attempted and run.failure is not None
    assert run.cleanup.confirmed_absent and run.snapshot_deleted
    assert run.cleanup_status.complete
    sequence.assert_settled()


def test_independent_requests_do_not_share_cleanup_state(tmp_path):
    from footman.snapshot import reserve_snapshot
    from tests.dispatcher_harness import GitFixture

    # Separate fixture-owned sequence records, no real or guessed containers.
    module = api()
    repo = GitFixture.create(tmp_path / "independent-repo")
    root = tmp_path / "independent-snapshots"
    with (
        reserve_snapshot(root, "0" * 26) as first,
        reserve_snapshot(root, "0" * 25 + "1") as second,
    ):
        snapshots = [first, second]
        sequences = []
        for i, snapshot in enumerate(snapshots):
            tree = snapshot.capture(repo.path)
            snapshot.export(tree)
            directory = tmp_path / str(i)
            directory.mkdir()
            identity = ("a" if i == 0 else "c") * 64
            script = steps(snapshot)
            for entry in script:
                entry["argv"] = [arg.replace(ID, identity) for arg in entry["argv"]]
                if "stdout" in entry:
                    entry["stdout"] = entry["stdout"].replace(
                        ID.encode(), identity.encode()
                    )
            sequences.append(DockerSequence(directory, [*script, *removal(identity)]))
        runs = [
            module.RunSupervisor(config(), snapshot, launch=sequence.launch)
            for snapshot, sequence in zip(snapshots, sequences, strict=True)
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda r: r.execute(lambda *_: None, lambda *_: None), runs))
        for run, sequence in zip(runs, sequences, strict=True):
            assert run.cleanup.confirmed_absent
            assert len(sequence.calls()) == 7
            assert run.cleanup_status.complete
            sequence.assert_settled()
        assert runs[0].cleanup.operations is not runs[1].cleanup.operations
        assert all(run.snapshot_deleted for run in runs)
        assert runs[0].execution.container_id != runs[1].execution.container_id
