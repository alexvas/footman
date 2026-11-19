"""Phase-9 transport, attribution and bootstrap contracts (Docker-free)."""

import importlib
import io
import json
from pathlib import Path

import pytest

from footman.process import StreamOutcome
from tests.dispatcher_harness import read_bytes
from tests.test_green_gate import assert_documented_suite, assert_registered_suite

ROOT = Path(__file__).resolve().parents[1]


def support(name):
    path = ROOT / "tests" / f"{name}.py"
    assert path.is_file(), f"required Phase-9 support is absent: {name}"
    return importlib.import_module(f"tests.{name}")


@pytest.mark.parametrize(
    "fault",
    ["nonzero-build", "successful-build", "post-interruption", "collection-failure"],
)
def test_phase9_build_failure_precedes_postcheck_failure(tmp_path, fault):
    from tests import build_failure_support as faults
    from tests import mcp_wheel_support as wheel

    faults.exercise(
        ROOT,
        tmp_path,
        wheel,
        lambda root, evidence: wheel.build_and_install_contract_wheel(
            root,
            admission=None,
            evidence_directory=evidence,
            timeout_seconds=30,
            attribution_only=True,
        ),
        fault,
    )


def test_phase9_build_binding_is_defensively_immutable(tmp_path):
    from tests.build_observation_values import check_binding_values
    from tests.mcp_wheel_support import BuildObservation

    check_binding_values(BuildObservation, tmp_path)


def test_registered_suite():
    assert_registered_suite(
        read_bytes(ROOT / "docs/testing.md").decode(),
        ("tests/test_mcp_transport.py", "tests/test_mcp_server.py"),
    )
    for name in ("docs/testing.md", "docs/python-checks.md", "README.md"):
        assert_documented_suite(read_bytes(ROOT / name).decode())


@pytest.mark.parametrize("split", range(1, 24))
def test_attestation_incremental_provisional_then_eof(split):
    p = support("mcp_provenance")
    sources = [{"path": "__init__.py", "sha256": "a" * 64}]
    record = p.attestation(sources)
    parser = p.AttestationParser(sources)
    data = b"ordinary diagnostic\n" + record
    for start in range(0, len(data), split):
        parser.feed(data[start : start + split])
        assert parser.buffered_bytes <= 512
    assert parser.provisional and not parser.confirmed
    parser.end(StreamOutcome.EOF)
    assert parser.confirmed


@pytest.mark.parametrize("terminal", [None, *StreamOutcome])
def test_attestation_terminal_outcome_independent_of_valid_line(terminal):
    p = support("mcp_provenance")
    parser = p.AttestationParser([])
    parser.feed(p.attestation([]))
    if terminal is not None:
        parser.end(terminal)
    assert parser.provisional
    assert parser.confirmed is (terminal is StreamOutcome.EOF)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "partial",
        "duplicate",
        "unknown",
        "overflow",
        "cr",
        "bom",
        "bool",
        "noncanonical",
        "digest",
        "count",
        "extra",
    ],
)
def test_synthetic_attestation_faults_never_confirm(fault):
    p = support("mcp_provenance")
    line = p.attestation([])
    obj = json.loads(line.split(b" ", 1)[1])
    cases = {
        "missing": b"diagnostic\n",
        "partial": line[:-1],
        "duplicate": line + b"FOOTMAN_PROVENANCE_UNKNOWN\n",
        "unknown": line.replace(b"V1 ", b"V2 "),
        "overflow": b"FOOTMAN_PROVENANCE_V1 " + b"x" * 1000 + b"\n",
        "cr": line[:-1] + b"\r\n",
        "bom": b"FOOTMAN_PROVENANCE_V1 \xef\xbb\xbf" + line.split(b" ", 1)[1],
        "bool": b"FOOTMAN_PROVENANCE_V1 "
        + json.dumps(obj | {"schema_version": True}).encode()
        + b"\n",
        "noncanonical": b"FOOTMAN_PROVENANCE_V1 "
        + json.dumps(obj, indent=2).encode()
        + b"\n",
        "digest": line.replace(obj["set_sha256"].encode(), b"0" * 64),
        "count": line.replace(b'"source_count":0', b'"source_count":1'),
        "extra": b"FOOTMAN_PROVENANCE_V1 "
        + json.dumps(obj | {"extra": 1}).encode()
        + b"\n",
    }
    parser = p.AttestationParser([])
    for byte in cases[fault]:
        parser.feed(bytes([byte]))
        assert parser.buffered_bytes <= 512
    parser.end(StreamOutcome.EOF)
    assert not parser.confirmed and parser.failure


def test_huge_diagnostics_do_not_grow_parser_or_hide_late_duplicate():
    p = support("mcp_provenance")
    parser = p.AttestationParser([])
    parser.feed(p.attestation([]))
    for _ in range(100):
        parser.feed(b"a" * 65536)
        assert parser.buffered_bytes <= 512
    parser.feed(b"\n" + p.attestation([]))
    parser.end(StreamOutcome.EOF)
    assert not parser.confirmed and parser.failure


@pytest.mark.parametrize("fault", ["short", "write", "flush", "interrupt", None])
def test_borrowed_attestation_one_write_flush_no_close_or_retry(fault):
    p = support("mcp_provenance")
    calls = []
    error = (
        KeyboardInterrupt("emission") if fault == "interrupt" else OSError("emission")
    )

    class Borrowed:
        def write(self, data):
            calls.append(("write", data))
            if fault in {"write", "interrupt"}:
                raise error
            return len(data) - (fault == "short")

        def flush(self):
            calls.append(("flush",))
            if fault == "flush":
                raise error

        def close(self):
            raise AssertionError("borrowed stderr must never be closed")

    if fault:
        with pytest.raises((OSError, KeyboardInterrupt)) as caught:
            p.emit([], Borrowed())
        if fault != "short":
            assert caught.value is error
    else:
        p.emit([], Borrowed())
    assert sum(c[0] == "write" for c in calls) == 1
    assert sum(c[0] == "flush" for c in calls) == (
        fault not in {"short", "write", "interrupt"}
    )


@pytest.mark.parametrize(
    "fault",
    [
        "container.py",
        "supervision.py",
        "__main__.py",
        "missing",
        "extra",
        "symlink",
        "wrong-root",
    ],
)
def test_full_package_pre_dispatch_controls(tmp_path, fault):
    support("mcp_contract_support").exercise_package_control(ROOT, tmp_path, fault)


@pytest.mark.parametrize("context", ["development", "wheel"])
def test_real_sdk_protocol_peer_and_bootstrap_discovery(tmp_path, context):
    support("mcp_contract_support").exercise_discovery(ROOT, tmp_path, context)


@pytest.mark.parametrize(
    "scenario",
    [
        "split",
        "utf8",
        "malformed",
        "partial",
        "stderr",
        "eof",
        "delay",
        "blocked",
        "cancel",
    ],
)
def test_actual_protocol_peer_transport_scenarios(tmp_path, scenario):
    support("mcp_contract_support").exercise_peer(ROOT, tmp_path, scenario)


@pytest.mark.parametrize(
    "fault",
    [
        "operation-close",
        "sole-close",
        "interruption",
        "repeat",
        "independent",
        "handoff",
        "concurrent",
        "partial-frame",
        "broken-pipe",
        "eof-pending",
    ],
)
def test_transport_ownership_and_stdin_accounting(tmp_path, fault):
    support("mcp_contract_support").exercise_transport_fault(tmp_path, fault)


@pytest.mark.parametrize(
    "branch",
    [
        "read",
        "write",
        "archive",
        "copy",
        "input",
        "journal",
        "evidence",
        "build-inventory",
        "package-enumeration",
        "dist-inventory",
    ],
)
@pytest.mark.parametrize(
    "fault", ["operation-close", "sole-close", "interruption", "repeat", "independent"]
)
def test_support_resource_fault_matrix(tmp_path, branch, fault):
    support("mcp_contract_support").exercise_resource_fault(tmp_path, branch, fault)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "truncated",
        "unwritable",
        "inconsistent",
        "before-attestation",
        "after-attestation",
    ],
)
def test_parent_owned_evidence_faults(tmp_path, fault):
    support("mcp_contract_support").exercise_evidence_fault(tmp_path, fault)


@pytest.mark.parametrize(
    "case_id",
    [
        "success",
        "build-status-failure",
        "ambiguous",
        "missing",
        "stale-output",
        "unowned-output-collision",
        "same-root-double-invocation",
        "readme-only-mutation",
        "concurrent-input-change",
    ],
)
def test_independent_wheel_attribution_public_surface(tmp_path, case_id):
    cases = support("wheel_attribution_cases")
    support("mcp_contract_support").exercise_attribution(
        ROOT, tmp_path, cases.case(case_id)
    )


@pytest.mark.parametrize(
    "fault",
    [
        "sdist-file",
        "sdist-directory",
        "sdist-link",
        "wheel-file",
        "wheel-directory",
        "wheel-link",
        "unsafe-dist",
        "unsupported-profile",
        "ambient-setting",
        "ambient-config",
        "unsafe-readme",
        "missing-readme",
        "changed-readme",
    ],
)
def test_build_profile_collision_and_identity_controls(tmp_path, fault):
    support("mcp_contract_support").exercise_build_control(ROOT, tmp_path, fault)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate-key",
        "unknown",
        "missing",
        "bool",
        "nonfinite",
        "expired",
        "extended",
        "token",
        "escape",
        "symlink",
        "alias",
    ],
)
def test_lifecycle_input_rejects_before_launch(tmp_path, fault):
    support("mcp_contract_support").exercise_lifecycle_validation(tmp_path, fault)


@pytest.mark.parametrize(
    "case",
    [
        "direct",
        "lifecycle",
        "eof",
        "lifetime",
        "journal-fault",
        "invalid-input",
        "wrong-position",
        "duplicate",
        "production-forward",
        "wrong-leg",
        "missing-option",
    ],
)
def test_exact_wheel_bootstrap_lifecycle_contract(tmp_path, case):
    support("mcp_contract_support").exercise_wheel_lifecycle(ROOT, tmp_path, case)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "stale",
        "swapped-install",
        "wheel-binding",
        "launcher",
        "script-shadow",
        "cwd-shadow",
        "poisoned-environment",
    ],
)
def test_expected_origin_archive_authority_controls(tmp_path, fault, admission):
    support("mcp_contract_support").exercise_origin_control(
        ROOT, tmp_path, fault, admission=admission.shared_pool
    )


def test_server_borrowed_stdio_facades_never_own_inherited_handles():
    support("mcp_contract_support").exercise_borrowed_stdio(io.BytesIO)


def test_write_bridge_does_not_attribute_prior_worker_record_to_state_rejection(
    tmp_path,
):
    import sys

    from footman.process import OwnedProcess, StdinStateError
    from tests.mcp_transport import write_frame
    from tests.mcp_wheel_support import clean_environment

    owner = OwnedProcess.launch(
        [sys.executable, "-P", str(ROOT / "tests/mcp_protocol_child.py"), "split"],
        stdin_mode="pipe",
        deadline_seconds=3,
        grace_seconds=1,
        popen_kwargs={"cwd": str(tmp_path), "env": clean_environment()},
    )
    try:
        frame = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        prior, error = write_frame(owner, frame, 1)
        assert error is None and prior.accepted_bytes == len(frame)
        owner.close_stdin(timeout_seconds=1)
        rejected, error = write_frame(owner, frame, 1)
        assert isinstance(error, StdinStateError)
        assert rejected is None, "a state rejection is not a new write attempt"
        assert prior.accepted_bytes == len(frame)
        result = owner.capture()
        assert result.returncode == 0
    finally:
        assert owner.shutdown().complete


@pytest.mark.parametrize(
    "fault", ["operation-close", "sole-close", "interruption", "repeat", "independent"]
)
def test_build_install_process_owner_faults(tmp_path, fault):
    support("mcp_contract_support").exercise_build_process_fault(tmp_path, fault)


@pytest.mark.parametrize("stream", ["STDOUT", "STDERR"])
@pytest.mark.parametrize("terminal", ["READ_FAILED", "ABORTED"])
def test_transport_terminal_failure_never_becomes_success(tmp_path, stream, terminal):
    support("mcp_contract_support").exercise_terminal_fault(tmp_path, stream, terminal)


@pytest.mark.parametrize("fault", ["feed", "end"])
def test_transport_parser_callback_failure_is_not_diagnostic_only(tmp_path, fault):
    support("mcp_contract_support").exercise_parser_callback_fault(tmp_path, fault)


@pytest.mark.parametrize(
    "fault",
    [
        "unsafe-path",
        "symlink",
        "special",
        "missing-module",
        "entrypoint",
        "ambiguous-module",
        "corrupt-close",
    ],
)
def test_archive_preinstall_validation_and_close_authority(tmp_path, fault):
    support("mcp_contract_support").exercise_archive_fault(tmp_path, fault)


@pytest.mark.parametrize("fault", ["missing-input", "extra-input"])
def test_scripted_profile_setup_errors_do_not_count_as_attribution_rejection(
    tmp_path, fault
):
    support("mcp_contract_support").exercise_profile_setup_error(ROOT, tmp_path, fault)


def test_real_write_bridge_reports_os_prefix_under_backpressure(tmp_path):
    import sys

    from footman.process import OwnedProcess, StdinWriteError
    from tests.mcp_transport import write_frame
    from tests.mcp_wheel_support import clean_environment

    owner = OwnedProcess.launch(
        [sys.executable, "-P", str(ROOT / "tests/mcp_protocol_child.py"), "blocked"],
        stdin_mode="pipe",
        deadline_seconds=1,
        grace_seconds=0.2,
        popen_kwargs={"cwd": str(tmp_path), "env": clean_environment()},
    )
    try:
        snapshot, error = write_frame(owner, b"x" * (3 * 1048576), 0.05)
        assert isinstance(error, StdinWriteError) and error.timed_out
        assert snapshot.timed_out and snapshot.error is error.cause
        assert 0 < snapshot.accepted_bytes < snapshot.requested_bytes == 3 * 1048576
        assert snapshot.accepted_bytes == error.accepted_bytes
        owner.close_stdin(timeout_seconds=0.1)
        owner.capture()
    finally:
        status = owner.shutdown()
        assert status.complete
    assert any(f.error is error.cause for f in status.failures)


@pytest.mark.parametrize("mode", ["oversized", "many-frames"])
def test_transport_framing_and_queue_backpressure_are_bounded(
    tmp_path, monkeypatch, mode
):
    import sys

    import anyio

    from tests import mcp_transport as transport
    from tests.mcp_wheel_support import clean_environment

    queues = []
    real_queue = transport.queue.Queue

    def queue(*args, **kwargs):
        item = real_queue(*args, **kwargs)
        if item.maxsize == 32:
            queues.append(item)
        return item

    from types import SimpleNamespace

    monkeypatch.setattr(
        transport,
        "queue",
        SimpleNamespace(
            Queue=queue, Empty=transport.queue.Empty, Full=transport.queue.Full
        ),
    )
    observed = transport.TransportObservation()

    async def run():
        async with transport.stdio_transport(
            [sys.executable, "-P", str(ROOT / "tests/mcp_protocol_child.py"), mode],
            cwd=tmp_path,
            env=clean_environment(),
            timeout_seconds=4,
            observation=observed,
        ) as (incoming, outgoing):
            assert (
                incoming.statistics().max_buffer_size
                == outgoing.statistics().max_buffer_size
                == 8
            )
            with anyio.fail_after(2):
                if mode == "many-frames":
                    while not queues[0].full():
                        await anyio.sleep(0.01)
                    assert queues[0].qsize() == 32
                else:
                    while observed.failure is None:
                        await anyio.sleep(0.01)

    if mode == "oversized":
        with pytest.raises(ValueError, match="frame exceeds limit"):
            anyio.run(run)
    else:
        anyio.run(run)
    assert observed.owner.cleanup_status.complete
    assert len(queues) == 1 and queues[0].maxsize == 32


def test_build_input_identity_preserves_exact_executable_bits(tmp_path):
    from tests.mcp_wheel_support import inventory

    prepared = support("mcp_contract_support").prepare_inputs(
        ROOT, tmp_path / "prepared"
    )
    path = prepared / "src/footman/__init__.py"
    original, _ = inventory(prepared)
    data = read_bytes(path)
    path.chmod(0o744)
    owner_exec, _ = inventory(prepared)
    path.chmod(0o754)
    group_exec, _ = inventory(prepared)
    (prepared / "src").chmod(0o700)
    directory_exec, _ = inventory(prepared)
    assert (
        len(
            {
                value["sha256"]
                for value in (original, owner_exec, group_exec, directory_exec)
            }
        )
        == 4
    )
    assert read_bytes(path) == data


def test_lifecycle_deadline_rejects_already_buffered_unaccepted_frames():
    import time

    from tests.mcp_lifecycle import DeadlineReader

    class Borrowed:
        def fileno(self):
            raise AssertionError("expired input must not acquire/read another frame")

        def close(self):
            raise AssertionError("borrowed input must not be closed")

    reader = DeadlineReader(Borrowed(), time.monotonic() - 1)
    reader.pending.extend(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
    assert reader.readline() == b"", (
        "absolute lifetime must precede buffered unaccepted requests"
    )


@pytest.mark.parametrize("point", ["second-pair", "launch"])
@pytest.mark.parametrize("interruption", [False, True])
def test_transport_partial_setup_closes_all_memory_without_child(
    tmp_path, monkeypatch, point, interruption
):
    import anyio

    from tests import mcp_transport as transport

    original = transport.anyio.create_memory_object_stream
    pairs = []
    attempts = {}
    primary = (
        KeyboardInterrupt("setup interruption")
        if interruption
        else OSError("setup operation")
    )
    close_error = OSError("memory close")
    launched = []

    def factory(*args):
        if pairs and point == "second-pair":
            raise primary
        pair = original(*args)
        if not pairs:
            real_close = type(pair[0]).close
            target = pair[0]

            def close(stream):
                attempts[id(stream)] = attempts.get(id(stream), 0) + 1
                real_close(stream)
                if stream is target:
                    raise close_error

            monkeypatch.setattr(type(pair[0]), "close", close)
        pairs.append(pair)
        return pair

    def launch(*args, **kwargs):
        launched.append(kwargs)
        raise primary

    monkeypatch.setattr(transport.anyio, "create_memory_object_stream", factory)

    async def scenario():
        context = transport.stdio_transport(
            [], cwd=tmp_path, env={}, timeout_seconds=2, launch=launch
        )
        with pytest.raises(BaseException) as caught:
            await context.__aenter__()
        assert caught.value is primary
        assert all(stream._closed for pair in pairs for stream in pair)
        assert any(
            error is close_error
            for error in getattr(primary, "_transaction_secondary", ())
        )
        before = dict(attempts)
        await context.__aexit__(None, None, None)
        assert attempts == before and all(count == 1 for count in attempts.values())

    anyio.run(scenario)
    assert len(launched) == int(point == "launch")


@pytest.mark.parametrize("primary_kind", [None, "ordinary", "interruption"])
@pytest.mark.parametrize("release_kind", [None, "ordinary", "interruption"])
@pytest.mark.parametrize("memory_interruption", [False, True])
def test_transport_memory_cleanup_fault_precedence_and_independence(
    primary_kind, release_kind, memory_interruption
):
    from tests.mcp_transport import _close_memory

    def error(kind, message):
        return (
            KeyboardInterrupt(message)
            if kind == "interruption"
            else OSError(message)
            if kind
            else None
        )

    primary = error(primary_kind, "active operation")
    release = error(release_kind, "process release")
    memory_error = error(
        "interruption" if memory_interruption else "ordinary", "memory release"
    )
    attempts = []

    class Memory:
        def __init__(self, name, failure):
            self.name, self.failure = name, failure

        def close(self):
            attempts.append(self.name)
            if self.failure is not None:
                raise self.failure

    resources = [Memory("first", memory_error), Memory("independent", None)]
    candidates = [item for item in (primary, release, memory_error) if item is not None]
    expected = next(
        (item for item in candidates if isinstance(item, KeyboardInterrupt)),
        candidates[0],
    )
    with pytest.raises(BaseException) as caught:
        try:
            if primary is not None:
                raise primary
        finally:
            _close_memory(resources, primary=primary, release_error=release)
    assert caught.value is expected
    assert attempts == ["first", "independent"]
    secondary = getattr(expected, "_transaction_secondary", ())
    assert all(
        any(item is extra for item in secondary)
        for extra in candidates
        if extra is not expected
    )


@pytest.mark.parametrize("context", ["service", "peer"])
def test_real_child_hostile_ascii_locale(tmp_path, context):
    support("mcp_contract_support").exercise_hostile_locale(ROOT, tmp_path, context)


@pytest.mark.parametrize(
    "fault", ["operation-close", "sole-close", "interruption", "repeat"]
)
@pytest.mark.parametrize("point", ["metadata", "flock", "body"])
def test_build_admission_sole_owner_faults(tmp_path, fault, point):
    import time

    from tests.admission_support import AdmissionPool
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    primary = (
        KeyboardInterrupt("admission interrupted")
        if fault == "interruption"
        else OSError("admission operation")
        if fault == "operation-close"
        else None
    )
    release, parent_error = (
        OSError("admission close"),
        OSError("independent parent close"),
    )
    ops = RecordingDescriptors(
        tmp_path,
        point=point if primary is not None else None,
        primary=primary,
        release=release,
    )
    ops.parent_release = parent_error
    pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=RecordingSystem(descriptors=ops)
    )
    context = pool.acquire(deadline=time.monotonic() + 5)
    entered = []
    with pytest.raises(BaseException) as caught:
        with context:
            entered.append(True)
            if primary is not None:
                raise primary
    assert caught.value is (primary or release)
    assert entered == ([] if primary is not None and point != "body" else [True])
    secondary = getattr(caught.value, "_transaction_secondary", ())
    assert any(item is parent_error for item in secondary)
    if primary is not None:
        assert any(item is release for item in secondary)
    assert sum(token[2] == "slot" for token in ops.closed) == 1
    assert sum(token[2] == "namespace" for token in ops.closed) == 1
    assert_released(ops)
    before = list(ops.closed)
    context.__exit__(None, None, None)
    context.__exit__(None, None, None)
    assert ops.closed == before
    ops.point = ops.primary = ops.release = ops.parent_release = None
    with pool.acquire(deadline=time.monotonic() + 5):
        pass
    assert ops.closed[: len(before)] == before
    assert_released(ops)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_build_admission_rejects_unsafe_nodes(tmp_path, kind):
    import os
    import time

    from footman.filesystem.descriptors import UnsafeDescriptorError
    from tests.admission_support import AdmissionPool
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    ops = RecordingDescriptors(tmp_path)
    pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=RecordingSystem(descriptors=ops)
    )
    with pool.acquire(deadline=time.monotonic() + 5):
        pass
    assert_released(ops)
    path = tmp_path / ops.slot_names[-1]
    path.unlink()
    if kind == "symlink":
        path.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = len(ops.events)
    with pytest.raises(UnsafeDescriptorError):
        with pool.acquire(deadline=time.monotonic() + 5):
            raise AssertionError("unsafe admission node entered")
    assert not any(event[0] == "attempt" for event in ops.events[before:])
    assert not (tmp_path / "absent").exists()
    assert_released(ops)


def test_build_admission_serializes_and_preserves_original_timeout(tmp_path):
    import concurrent.futures
    import threading

    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock
    from tests.test_admission_pool import (
        RecordingDescriptors,
        RecordingSystem,
        assert_released,
    )

    clock = ManualClock(origin=0.0)
    entered, release, blocked = threading.Event(), threading.Event(), threading.Event()
    order = []

    class System(RecordingSystem):
        def monotonic(self):
            return clock.monotonic()

        def sleep(self, seconds):
            pass

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if not acquired:
                clock.advance(
                    1, label="admission contention exhausts original allowance"
                )
                blocked.set()
            return acquired

    first_ops, second_ops = (
        RecordingDescriptors(tmp_path),
        RecordingDescriptors(tmp_path),
    )
    first_pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=System(descriptors=first_ops)
    )
    second_pool = AdmissionPool(
        directory=tmp_path, capacity=1, system=System(descriptors=second_ops)
    )

    def first():
        with first_pool.acquire(deadline=10):
            order.append("first-enter")
            entered.set()
            if not release.wait(5):
                raise AssertionError("test holder not released")
            order.append("first-exit")

    def second():
        with second_pool.acquire(deadline=1):
            raise AssertionError("concurrent build admitted")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as threads:
        one = threads.submit(first)
        try:
            assert entered.wait(5)
            two = threads.submit(second)
            assert blocked.wait(5)
            with pytest.raises(TimeoutError, match="original|admission"):
                two.result(timeout=5)
        finally:
            release.set()
        one.result(timeout=5)
    assert order == ["first-enter", "first-exit"] and clock.elapsed() == 1
    assert ("busy", "slot") in second_ops.events
    with second_pool.acquire(deadline=2):
        order.append("after-release")
    assert order == ["first-enter", "first-exit", "after-release"]
    assert_released(first_ops)
    assert_released(second_ops)


def test_build_admission_does_not_renew_setup_deadline(monkeypatch, tmp_path):
    import os
    import time

    from tests import mcp_attribution_support as attribution
    from tests import mcp_contract_support as support
    from tests import mcp_wheel_support as wheel
    from tests.admission_support import AdmissionPool, PosixAdmissionSystem
    from tests.clock_support import ManualClock, module_clock
    from tests.test_admission_pool import RecordingDescriptors, assert_released
    from tests.wheel_attribution_cases import case

    prepared = support.prepare_inputs(ROOT, tmp_path / "inputs")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    namespace = tmp_path / "pool"
    namespace.mkdir()
    scenario = case("build-status-failure")
    executable, call = attribution.scripted_uv(tmp_path, scenario)
    env = wheel.clean_environment(
        {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    )
    clock = ManualClock(origin=100.0)
    holder = AdmissionPool(directory=namespace, capacity=1).acquire(
        deadline=time.monotonic() + 5
    )
    ops = RecordingDescriptors(namespace)
    busy = []

    class System(PosixAdmissionSystem):
        def monotonic(self):
            return clock.monotonic()

        def sleep(self, seconds):
            pass

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if not acquired:
                assert not busy
                busy.append(True)
                clock.advance(7, label="original installation admission wait")
                holder.__exit__(None, None, None)
            return acquired

    pool = AdmissionPool(
        directory=namespace, capacity=1, system=System(descriptors=ops)
    )
    real_command, allowances = wheel.command, []

    def command(argv, **kwargs):
        allowances.append(kwargs["timeout_seconds"])
        return real_command(argv, **kwargs)

    with holder, module_clock(wheel, clock), monkeypatch.context() as patch:
        patch.setattr(wheel, "command", command)
        with pytest.raises(wheel.BuildFailure) as caught:
            wheel.build_and_install_contract_wheel(
                prepared,
                admission=pool,
                evidence_directory=evidence,
                timeout_seconds=10,
                env=env,
            )
    assert busy == [True] and allowances == [3.0]
    assert clock.monotonic() == 107 and clock.elapsed() == 7
    # Origin100 + original duration10 is end110, not a renewed end117:
    # public external work receives110-107=3 after the real busy probe releases.
    assert caught.value.observation["status"] == 17 and call.is_file()
    assert caught.value.observation["stdout"] == scenario.stdout_bytes
    assert caught.value.observation["stderr"] == scenario.stderr_bytes
    assert not (evidence / "venv").exists()
    assert_released(ops)


def test_real_builds_use_invocation_shared_admission(request, tmp_path_factory):
    from tests import mcp_wheel_support as wheel

    base = tmp_path_factory.getbasetemp()
    expected = base.parent if hasattr(request.config, "workerinput") else base
    assert wheel.BUILD_SLOT_PATH == expected / "mcp-build.lock"


@pytest.mark.parametrize(
    "operation", ["launch", "inspect", "mkdir", "copy", "command", "remove", "confirm"]
)
def test_mcp_stub_observations_do_not_wait_for_durability(tmp_path, operation):
    import sys

    from tests import mcp_provenance as p
    from tests.mcp_wheel_support import clean_environment, command

    identity = "d" * 64
    calls = {
        "launch": [
            "run",
            "--detach=true",
            "--pull=never",
            "--",
            "image-id",
            "sleep",
            "infinity",
        ],
        "inspect": [
            "container",
            "inspect",
            "--format",
            "{{.Id}}|{{.State.Status}}|{{.Image}}",
            identity,
        ],
        "mkdir": ["exec", "-u", "root", identity, "mkdir", "--", "/workspace/source"],
        "copy": ["cp", "/prepared/.", identity + ":/workspace/source/"],
        "command": [
            "exec",
            "-u",
            "root",
            "-w",
            "/workspace/source",
            identity,
            "image-tool",
            "two words",
            "; $literal",
        ],
        "remove": ["rm", "-f", identity],
        "confirm": [
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            "id=" + identity,
            "--format",
            "{{.ID}}",
        ],
    }
    records = tmp_path / "records"
    records.mkdir()
    code = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
from tests.mcp_docker_child import main
def barrier(fd):
    raise OSError('synchronous diagnostic barrier on MCP CLI critical path')
os.fsync = barrier
sys.argv = ['docker', *{calls[operation]!r}]
main()
"""
    result = command(
        [sys.executable, "-P", "-c", code],
        cwd=tmp_path,
        env=clean_environment(
            {
                "FOOTMAN_MCP_STUB": json.dumps(
                    {
                        "records": str(records),
                        "failure": operation,
                        "status": 17,
                        "delay": 0,
                    }
                )
            }
        ),
        timeout_seconds=5,
    )
    assert result["status"] == 17, result["stderr"]
    observed = list(records.iterdir())
    assert len(observed) == 1
    assert p.load(observed[0]) == {"operation": operation, "argv": calls[operation]}
    if operation == "command":
        assert result["stdout"] == b"command stdout\x00\xff\n"
        assert result["stderr"] == b"command stderr\x00\xfe\n"
    else:
        assert result["stderr"] == b""
        if operation == "launch":
            assert len(result["stdout"]) == 65 and result["stdout"][-1:] == b"\n"
            assert all(byte in b"0123456789abcdef" for byte in result["stdout"][:-1])
        elif operation == "inspect":
            assert (
                result["stdout"]
                == (identity + "|running|sha256:" + "b" * 64 + "\n").encode()
            )
        else:
            assert result["stdout"] == b""


@pytest.mark.parametrize(
    "fault", ["operation-close", "sole-close", "interruption-close", "repeat"]
)
def test_mcp_stub_observation_failures_remain_authoritative(
    tmp_path, monkeypatch, capsysbinary, fault
):
    import os
    import stat
    import sys

    from tests import mcp_docker_child as child
    from tests import mcp_provenance as p

    original_ops, original_write = p.OPS, os.write
    namespace = tmp_path.stat()
    slot_closes, directory_closes = [], []
    primary = (
        KeyboardInterrupt("stub record interrupted")
        if fault == "interruption-close"
        else OSError("stub record write failed")
    )
    release, parent_release = (
        OSError("stub record close failed"),
        OSError("stub record directory close failed"),
    )

    class Ops:
        def __getattr__(self, name):
            return getattr(original_ops, name)

        def close(self, fd):
            info = os.fstat(fd)
            regular = stat.S_ISREG(info.st_mode)
            parent = (info.st_dev, info.st_ino) == (namespace.st_dev, namespace.st_ino)
            original_ops.close(fd)
            if regular:
                slot_closes.append((fd, info.st_dev, info.st_ino))
                if fault != "repeat":
                    raise release
            if parent:
                directory_closes.append(fd)
                if fault != "repeat":
                    raise parent_release

    def write(fd, data):
        if fault in {"operation-close", "interruption-close"}:
            raise primary
        return original_write(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(p, "OPS", Ops())
        patch.setattr(os, "write", write)
        patch.setenv(
            "FOOTMAN_MCP_STUB",
            json.dumps(
                {
                    "records": str(tmp_path),
                    "failure": "command",
                    "status": 17,
                    "delay": 0,
                }
            ),
        )
        patch.setattr(sys, "argv", ["docker", "exec", "d" * 64, "image-tool"])
        if fault == "repeat":
            for count in (1, 2):
                with pytest.raises(SystemExit) as caught:
                    child.main()
                assert caught.value.code == 17
                assert len(slot_closes) == len(directory_closes) == count
        else:
            with pytest.raises(BaseException) as caught:
                child.main()
            assert caught.value is (release if fault == "sole-close" else primary)
            secondary = getattr(caught.value, "_transaction_secondary", ())
            assert any(error is parent_release for error in secondary)
            if fault != "sole-close":
                assert any(error is release for error in secondary)
            assert len(slot_closes) == len(directory_closes) == 1
    output = capsysbinary.readouterr()
    if fault == "repeat":
        assert len(set(slot_closes)) == len(slot_closes) == 2
        assert len(list(tmp_path.iterdir())) == 2
        for path in tmp_path.iterdir():
            assert p.load(path) == {
                "operation": "command",
                "argv": ["exec", "d" * 64, "image-tool"],
            }
        assert output.out == b"command stdout\x00\xff\n" * 2
        assert output.err == b"command stderr\x00\xfe\n" * 2
    else:
        assert output.out == output.err == b""


def test_docker_stub_recording_imports_no_process_or_sdk(tmp_path):
    import sys

    from tests.mcp_wheel_support import clean_environment, command

    code = f"import sys, runpy; sys.path.insert(0, {str(ROOT)!r}); runpy.run_path({str(ROOT / 'tests/mcp_docker_child.py')!r}, run_name='import_probe'); missing = [name for name in ('footman.process', 'mcp', 'anyio') if name in sys.modules]; print(missing); raise SystemExit(bool(missing))"
    result = command(
        [sys.executable, "-P", "-c", code],
        cwd=tmp_path,
        env=clean_environment(),
        timeout_seconds=5,
    )
    assert result["status"] == 0 and result["stdout"].strip() == b"[]"


def test_worker_wheel_is_reused_with_immutable_support_and_private_manifests(tmp_path):
    from tests import mcp_contract_support as s
    from tests import mcp_provenance as p

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    one = s.world(ROOT, first, "wheel")
    two = s.world(ROOT, second, "wheel")
    assert one["bound"].environment == two["bound"].environment
    assert one["bound"].wheel == two["bound"].wheel
    assert one["bound"].support_root == two["bound"].support_root
    private = tmp_path / "private-support"
    private.mkdir()
    from tests.mcp_wheel_support import reuse_for_test

    isolated = reuse_for_test(s.WHEEL_PROVIDER(), private, private_support=True)
    assert isolated.support_root != one["bound"].support_root
    s.rewrite(
        isolated.support_root / "tests/mcp_server_child.py", b"# isolated shadow\n"
    )
    assert (
        p.read(one["bound"].support_root / "tests/mcp_server_child.py")
        != b"# isolated shadow\n"
    )
    one["manifest"]["modules"].pop()
    assert len(two["manifest"]["modules"]) == len(
        s.WHEEL_PROVIDER().expected_origin["modules"]
    )
    record = p.load(one["bound"].repository_root.parent / "setup.json")
    assert record["build_count"] == record["venv_count"] == 1
    assert record["environment"] == str(one["bound"].environment)


def test_mutable_installation_clone_has_private_inodes_and_relocated_launcher(tmp_path):
    from tests import mcp_contract_support as s
    from tests import mcp_provenance as p
    from tests import mcp_wheel_support as wheel

    base = s.WHEEL_PROVIDER()
    root, _ = p.manifest_set(base.expected_origin)
    before = p.sources(root)
    original_script = p.read(base.environment / "bin/footman")
    copied = wheel.reuse_for_test(base, tmp_path, mutable=True)
    clone, _ = p.manifest_set(copied.expected_origin)
    assert clone.is_relative_to(copied.environment) and not clone.is_relative_to(
        base.environment
    )
    assert not (clone / "startup.py").samefile(root / "startup.py")
    s.rewrite(
        clone / "startup.py", p.read(clone / "startup.py") + b"\n# private mutation\n"
    )
    assert p.sources(root) == before
    assert p.read(base.environment / "bin/footman") == original_script
    assert p.read(copied.environment / "bin/footman").startswith(
        b"#!" + str(copied.environment / "bin/python").encode() + b"\n"
    )


@pytest.mark.parametrize(
    "fault", ["operation-close", "sole-close", "interruption", "repeat", "independent"]
)
@pytest.mark.parametrize("after_successes", [0, 1], ids=["copy", "layout"])
def test_clone_process_owner_failure_matrix(tmp_path, fault, after_successes):
    from tests import mcp_contract_support as s
    from tests import mcp_wheel_support as wheel

    base = s.WHEEL_PROVIDER()

    def action():
        clone = wheel.reuse_for_test(base, tmp_path, mutable=True)
        assert clone.environment != base.environment

    s.exercise_build_process_fault(
        tmp_path, fault, action=action, after_successes=after_successes
    )
