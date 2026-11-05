"""Phase 7 retained bytes, manifest publication and aggregate status contracts."""

import base64
import dataclasses
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from footman.container import InfrastructureOperation
from footman.process import ProcessStream, StreamOutcome
from tests.dispatcher_harness import GitFixture, read_bytes
from tests.launcher_support import DockerSequence
from tests.test_dispatch_lifecycle import removal
from tests.test_launcher_execution import ID, IMAGE, source, steps
from tests.test_output_reservation import FILES, config

__all__ = ["config", "source"]


def api():
    import footman

    assert (Path(footman.__file__).parent / "results.py").is_file(), (
        "required lossless sinks, durable manifests and outcome aggregation are absent"
    )
    return importlib.import_module("footman.results")


@pytest.mark.parametrize("status", [0, 1, 127, -9, None])
@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            {"interrupted": True, "timed_out": True, "output_failed": True},
            "interrupted",
        ),
        ({"timed_out": True, "output_failed": True}, "timed_out"),
        ({"output_failed": True, "dispatch_failed": True}, "output_failed"),
        ({"dispatch_failed": True}, "dispatch_failed"),
        ({"unresolved": True}, "dispatch_failed"),
        ({}, None),
    ],
)
def test_outcome_precedence(status, flags, expected):
    expected = expected or (
        "dispatch_failed"
        if status is None
        else "succeeded"
        if status == 0
        else "command_failed"
    )
    assert api().aggregate_outcome(status, **flags) == expected


@pytest.mark.parametrize("terminal", list(StreamOutcome))
def test_sink_terminal_observation_is_not_child_success(config, terminal):
    from footman.output import reserve_request

    with reserve_request(config, {"phase": "retention"}) as output:
        sink = api().FileSinks(output, startup=False)
        sink.chunk(ProcessStream.STDOUT, b"\x00warning\xff\r\n")
        sink.end(ProcessStream.STDOUT, terminal)
        sink.end(ProcessStream.STDERR, StreamOutcome.EOF)
        sink.finish()
        assert sink.complete is (terminal is StreamOutcome.EOF)
        assert read_bytes(output.paths["stdout.txt"]) == b"\x00warning\xff\r\n"
        assert read_bytes(output.paths["stderr.txt"]) == b""
        sink.finish()  # No second flush or release.


@pytest.mark.parametrize("boundary", ["write", "flush"])
def test_sink_failure_is_explicit_and_other_stream_remains_retained(
    config, monkeypatch, boundary
):
    from footman.output import reserve_request

    module = api()
    failure = OSError("storage exhausted")
    with reserve_request(config, {"phase": "fault"}) as output:
        sink = module.FileSinks(output, startup=False)
        if boundary == "write":
            original = module.os.write
            failed_fd = output.files["stdout.txt"].fd

            def write(fd, data):
                if fd == failed_fd:
                    raise failure
                return original(fd, data)

            monkeypatch.setattr(module.os, "write", write)
        else:
            original = module.os.fsync
            failed_fd = output.files["stdout.txt"].fd

            def flush(fd):
                if fd == failed_fd:
                    raise failure
                return original(fd)

            monkeypatch.setattr(module.os, "fsync", flush)
        # An ordinary callback error must not be the only failure signal.
        sink.chunk(ProcessStream.STDOUT, b"lost")
        sink.chunk(ProcessStream.STDERR, b"independent warning\xff")
        for stream in ProcessStream:
            sink.end(stream, StreamOutcome.EOF)
        sink.finish()
        assert sink.failure is failure
        assert not sink.complete
        assert read_bytes(output.paths["stderr.txt"]) == b"independent warning\xff"
        sink.finish()


def test_sink_short_writes_and_large_separate_bytes(config, monkeypatch):
    from footman.output import reserve_request

    module = api()
    original = module.os.write

    def short(fd, data):
        return original(fd, data[:17])

    monkeypatch.setattr(module.os, "write", short)
    stdout = bytes(range(256)) * 8192
    stderr = b"warning\x00\xff\r\n" * 190000
    with reserve_request(config, {"phase": "large"}) as output:
        sink = module.FileSinks(output, startup=False)
        for position in range(0, max(len(stdout), len(stderr)), 8191):
            sink.chunk(ProcessStream.STDOUT, stdout[position : position + 8191])
            sink.chunk(ProcessStream.STDERR, stderr[position : position + 8191])
        for stream in ProcessStream:
            sink.end(stream, StreamOutcome.EOF)
        sink.finish()
        assert sink.complete
        assert read_bytes(output.paths["stdout.txt"]) == stdout
        assert read_bytes(output.paths["stderr.txt"]) == stderr
        assert read_bytes(output.paths["startup-stdout.txt"]) == b""
        assert read_bytes(output.paths["startup-stderr.txt"]) == b""


@pytest.mark.parametrize("split", [1, 7, 4095, 4096, 4097])
@pytest.mark.parametrize("terminal", list(StreamOutcome))
def test_manifest_raw_infrastructure_prefix(config, split, terminal):
    from footman.output import reserve_request

    module = api()
    data = b"https://user:password@example.invalid/?token=secret\xff\x00" * 100
    operation = InfrastructureOperation("inspect")
    for offset in range(0, len(data), split):
        operation.collector.chunk(ProcessStream.STDERR, data[offset : offset + split])
    operation.collector.end(ProcessStream.STDERR, terminal)
    with reserve_request(config, {"phase": "summary"}) as output:
        manifest = module.Manifest(output, config)
        manifest.initial()
        manifest.finalize(
            {"infrastructure_operations": [module.operation_record(operation)]}
        )
        record = json.loads(read_bytes(output.paths["result.json"]))[
            "infrastructure_operations"
        ][0]
        assert record["name"] == "inspect"
        assert record["started_at"] is None and record["ended_at"] is None
        assert record["return_code"] is None
        summary = record["stderr"]
        assert base64.b64decode(summary["excerpt_base64"]) == data[:4096]
        assert summary["observed_bytes"] == len(data)
        assert summary["retained_bytes"] == 4096
        assert summary["truncated"] is True
        assert summary["completeness"] == terminal.value
        assert record["stdout"]["completeness"] == StreamOutcome.PENDING.value
        assert set(p.name for p in output.directory.iterdir()) == FILES


def test_manifest_syncs_directory_after_replacement(config, monkeypatch):
    import os

    from footman.filesystem.descriptors import OwnedDescriptor
    from footman.output import reserve_request

    module = api()
    with reserve_request(config, {"phase": "directory-sync"}) as output:
        manifest = module.Manifest(output, config)
        manifest.initial()
        directory_fd = output._directories[-1].fd
        original = module.os.fsync
        original_close = OwnedDescriptor.close
        original_replace = module.os.replace
        events = []
        synced = []

        def close(owner):
            if owner.label == "result publication temporary":
                events.append("close")
            return original_close(owner)

        def replace(*args, **kwargs):
            events.append("replace")
            return original_replace(*args, **kwargs)

        def fsync(fd):
            synced.append(fd)
            events.append("directory_sync" if fd == directory_fd else "file_sync")
            if fd == directory_fd:
                assert (
                    json.loads(read_bytes(output.paths["result.json"]))["outcome"]
                    == "succeeded"
                )
                assert not (output.directory / ".result.tmp").exists()
            return original(fd)

        monkeypatch.setattr(module.os, "fsync", fsync)
        monkeypatch.setattr(module.os, "replace", replace)
        monkeypatch.setattr(OwnedDescriptor, "close", close)
        manifest.finalize({"outcome": "succeeded"})
        expected = ["file_sync", "close", "replace", "directory_sync"]
        if os.environ.get("FOOTMAN_RESULTS_CONTROL") == "publication-order":
            expected[-2:] = reversed(expected[-2:])
        assert events == expected
        assert len(synced) == 2
        assert synced[-1] == directory_fd
        assert manifest.finalized


@pytest.mark.parametrize("fault_type", [OSError, KeyboardInterrupt])
def test_directory_sync_failure_is_publication_failure(
    config, tmp_path, monkeypatch, fault_type
):
    run, sequence, output = prepared(config, tmp_path)
    module = api()
    directory_fd = output._directories[-1].fd
    original = module.os.fsync
    failure = fault_type("directory sync failed")
    attempts = []

    def fsync(fd):
        if fd == directory_fd and run.manifest._attempted:
            attempts.append(fd)
            assert not (output.directory / ".result.tmp").exists()
            raise failure
        return original(fd)

    monkeypatch.setattr(module.os, "fsync", fsync)
    if fault_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            run.execute()
        assert caught.value is failure
        response = run.response
    else:
        with pytest.raises(Exception) as caught:
            run.execute()
        assert type(caught.value).__name__ == "PublicationError"
        assert caught.value.publication_failure is failure
        assert caught.value.response is run.response
        response = run.response
    sequence.assert_settled()
    assert response["outcome"] == (
        "interrupted" if fault_type is KeyboardInterrupt else "output_failed"
    )
    assert response["return_code"] == 0
    assert run.manifest.failure is failure
    assert not run.manifest.finalized
    assert not response["diagnostics"]["manifest_finalized"]
    assert all(view.released for view in output.files.values())
    assert set(p.name for p in output.directory.iterdir()) == FILES
    # Visible observations are not confirmation of publication or invocation.
    visible = read_bytes(output.paths["result.json"])
    assert json.loads(visible)["return_code"] == 0
    assert json.loads(visible)["outcome"] == "succeeded"
    assert run.manifest.data["outcome"] == "running"
    assert run.manifest.data["return_code"] is None
    assert response["diagnostics"]["publication_failure"]["type"] == fault_type.__name__
    with pytest.raises(RuntimeError, match="attempted"):
        run.manifest.finalize({"outcome": "succeeded"})
    output.close()
    assert read_bytes(output.paths["result.json"]) == visible
    assert attempts == [directory_fd]


def test_atomic_finalization_failure_preserves_readable_initial(config, monkeypatch):
    from footman.output import reserve_request

    module = api()
    with reserve_request(config, {"phase": "atomic"}) as output:
        manifest = module.Manifest(output, config)
        manifest.initial()
        before = read_bytes(output.paths["result.json"])
        assert json.loads(before)["outcome"] == "running"
        failure = OSError("publication failed")

        def reject(*args, **kwargs):
            assert (
                json.loads(read_bytes(output.paths["result.json"]))["outcome"]
                == "running"
            )
            raise failure

        monkeypatch.setattr(module.os, "replace", reject)
        with pytest.raises(OSError) as caught:
            manifest.finalize({"outcome": "succeeded", "return_code": 0})
        assert caught.value is failure
        assert read_bytes(output.paths["result.json"]) == before
        assert set(p.name for p in output.directory.iterdir()) == FILES
        assert manifest.failure is failure
        assert not manifest.finalized


@pytest.mark.parametrize(
    "fault", [OSError("write failed"), KeyboardInterrupt("interrupted")]
)
def test_manifest_operation_and_close_failure_once(config, monkeypatch, fault):
    from footman.filesystem.descriptors import OwnedDescriptor
    from footman.output import reserve_request

    module = api()
    with reserve_request(config, {"phase": "publication-fault"}) as output:
        manifest = module.Manifest(output, config)
        manifest.initial()
        original_close = OwnedDescriptor.close
        closes = []
        original_write = module.os.write

        def write(fd, data):
            if fd not in {view.fd for view in output.files.values()}:
                raise fault
            return original_write(fd, data)

        def close(owner):
            if owner.label == "result publication temporary":
                closes.append(owner)
                original_close(owner)
                raise OSError("temporary close failed")
            return original_close(owner)

        monkeypatch.setattr(module.os, "write", write)
        monkeypatch.setattr(OwnedDescriptor, "close", close)
        with pytest.raises(type(fault)) as caught:
            manifest.finalize({"outcome": "succeeded"})
        assert caught.value is fault
        assert len(closes) == 1
        assert (
            json.loads(read_bytes(output.paths["result.json"]))["outcome"] == "running"
        )
        with pytest.raises(RuntimeError, match="attempted"):
            manifest.finalize({"outcome": "succeeded"})
        assert len(closes) == 1


@pytest.mark.parametrize("size", [0, 4095, 4096, 4097])
def test_summary_boundaries_and_collector_failure_are_explicit(size):
    from footman.process import (
        FailureInfo,
        FailureKind,
        FailurePhase,
        LifecycleResult,
        ProcessResult,
    )

    op = InfrastructureOperation("confirmation")
    failure = OSError("diagnostic collector failed")
    op.collector.chunk(ProcessStream.STDOUT, b"x" * size)
    op.collector.failure = failure
    op.failure = failure
    op.diagnostics.append("invalid confirmation output")
    op.result = ProcessResult(
        b"",
        b"",
        None,
        False,
        LifecycleResult(
            False,
            (
                FailureInfo(
                    failure,
                    FailureKind.ORDINARY,
                    FailurePhase.READ,
                    ProcessStream.STDOUT,
                ),
            ),
        ),
    )
    record = api().operation_record(op)
    assert record["return_code"] is None
    assert record["collector_failure"]
    assert record["failure"]
    assert record["lifecycle_failures"][0]["phase"] == "read"
    assert record["parse_diagnostics"] == ["invalid confirmation output"]
    assert record["stdout"]["observed_bytes"] == size
    assert record["stdout"]["retained_bytes"] == min(size, 4096)
    assert record["stdout"]["truncated"] is (size > 4096)
    assert record["stdout"]["completeness"] == StreamOutcome.PENDING.value


def test_sink_interruption_retains_authority_and_finish_is_once_only(
    config, monkeypatch
):
    from footman.output import reserve_request

    module = api()
    with reserve_request(config, {"phase": "sink-interruption"}) as output:
        sink = module.FileSinks(output, startup=False)
        original = module.os.write
        control = KeyboardInterrupt("sink interrupted")

        def write(fd, data):
            if fd == output.files["stdout.txt"].fd:
                raise control
            return original(fd, data)

        monkeypatch.setattr(module.os, "write", write)
        with pytest.raises(KeyboardInterrupt) as caught:
            sink.chunk(ProcessStream.STDOUT, b"not retained")
        assert caught.value is control
        assert sink.failure is control
        sink.chunk(ProcessStream.STDERR, b"still retained")
        sink.finish()
        assert not sink.complete
        assert read_bytes(output.paths["stderr.txt"]) == b"still retained"
        monkeypatch.setattr(module.os, "fsync", lambda _: pytest.fail("flush repeated"))
        sink.finish()


def prepared(config, tmp_path, *, status=0, output=None, fault=None):
    from footman.output import reserve_request

    repository = GitFixture.create(tmp_path / "staged-repo")
    cfg = dataclasses.replace(
        config,
        repository=repository.path,
        container_start_argv=(*config.container_start_argv, "sleep", "infinity"),
    )
    output = output or reserve_request(cfg, {"phase": "delivery"})
    planned_source = SimpleNamespace(
        path=cfg.snapshot_directory / output.identity.run_id
    )
    script = steps(planned_source, cfg)
    script[-1].update(status=status, stdout=b"command\x00\xff\n", stderr=b"warning\r\n")
    script[0]["stderr"] = b"startup warning\xff"
    script[1]["stderr"] = b"https://u:pw@example.invalid token=secret\xff" * 150
    if fault == "copy":
        script[3]["status"] = 125
        script = script[:4]
    script += removal()
    stub = tmp_path / "docker-script"
    stub.mkdir()
    sequence = DockerSequence(stub, script)
    run = api().RetainedRun(cfg, output, launch=sequence.launch)
    return run, sequence, output


@pytest.mark.parametrize("status", [0, 7, 127])
def test_dev_exec_status_and_streams_persisted_manifest(config, tmp_path, status):
    run, sequence, output = prepared(config, tmp_path, status=status)
    response = run.execute()
    sequence.assert_settled()
    assert response["outcome"] == ("succeeded" if status == 0 else "command_failed")
    assert response["return_code"] == status
    expected = {
        "stdoutPath": "stdout.txt",
        "stderrPath": "stderr.txt",
        "startupStdoutPath": "startup-stdout.txt",
        "startupStderrPath": "startup-stderr.txt",
        "resultPath": "result.json",
    }
    for key, name in expected.items():
        assert Path(response[key]).is_absolute()
        assert Path(response[key]) == output.directory / name
        assert Path(response[key]).is_file()
    assert read_bytes(output.paths["stdout.txt"]) == b"command\x00\xff\n"
    assert read_bytes(output.paths["stderr.txt"]) == b"warning\r\n"
    assert read_bytes(output.paths["startup-stdout.txt"]) == (ID + "\n").encode()
    assert read_bytes(output.paths["startup-stderr.txt"]) == b"startup warning\xff"
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    required = {
        "schema_version",
        "run_id",
        "utc_timestamp",
        "phase",
        "output_template",
        "output_directory",
        "snapshot",
        "configuration_identity",
        "command_argv",
        "startup",
        "docker_command_argv",
        "cwd",
        "user",
        "container_id",
        "configured_image_id",
        "observed_image_id",
        "started_at",
        "ended_at",
        "return_code",
        "signal",
        "outcome",
        "paths",
        "cleanup",
        "warnings",
        "limitations",
        "infrastructure_operations",
        "persistence",
    }
    assert required <= manifest.keys()
    assert manifest["run_id"] == output.identity.run_id
    assert manifest["utc_timestamp"] == output.identity.utc_timestamp
    assert manifest["phase"] == "delivery"
    assert manifest["configuration_identity"] == run.config.identity
    assert "environment" not in manifest
    assert manifest["snapshot"]["tree_id"]
    assert manifest["snapshot"]["object_format"] == "sha1"
    assert manifest["snapshot"]["head"]
    assert manifest["configured_image_id"] == run.config.image_id
    assert manifest["observed_image_id"] == IMAGE
    assert manifest["container_id"] == ID
    assert manifest["return_code"] == status
    assert manifest["outcome"] == response["outcome"]
    assert manifest["cleanup"]["confirmed_absent"]
    assert manifest["cleanup"]["snapshot_deleted"]
    assert not manifest["cleanup"]["unresolved_resources"]
    assert manifest["command_argv"] == list(run.config.command_argv)
    assert manifest["command_started_at"] and manifest["command_ended_at"]
    assert manifest["snapshot"]["reserved"] is True
    operations = manifest["infrastructure_operations"]
    assert [op["name"] for op in operations] == [
        "inspect",
        "mkdir",
        "cp",
        "rm",
        "confirmation",
    ]
    for op, call in zip(
        operations, sequence.calls()[1:4] + sequence.calls()[5:], strict=True
    ):
        assert op["argv"][1:] == call["argv"]
        assert op["return_code"] == call["status"]
        assert op["started_at"] and op["ended_at"]
        assert op["stdout"]["completeness"] == StreamOutcome.EOF.value
    raw = b"https://u:pw@example.invalid token=secret\xff" * 150
    assert base64.b64decode(operations[0]["stderr"]["excerpt_base64"]) == raw[:4096]
    assert operations[0]["stderr"]["observed_bytes"] == len(raw)
    assert set(p.name for p in output.directory.iterdir()) == FILES


@pytest.mark.parametrize("fault", ["timeout", "removal", "storage", "interruption"])
def test_code_zero_cannot_mask_other_failures(config, tmp_path, monkeypatch, fault):
    run, sequence, output = prepared(config, tmp_path)
    if fault == "removal":
        sequence.steps[-2]["status"] = 1
    elif fault == "timeout":
        original = sequence.launch

        def launch(argv, **kwargs):
            owner = original(argv, **kwargs)
            if len(sequence.parameters) == 5:
                stream = owner.stream

                def timed_out(*args, **kwargs):
                    result = stream(*args, **kwargs)
                    return dataclasses.replace(result, timed_out=True)

                monkeypatch.setattr(owner, "stream", timed_out)
            return owner

        run.launch = launch
    elif fault == "storage":
        module = api()
        original = module.os.write

        def write(fd, data):
            if (
                not output.files["stdout.txt"].released
                and fd == output.files["stdout.txt"].fd
            ):
                raise OSError("retention failed")
            return original(fd, data)

        monkeypatch.setattr(module.os, "write", write)
    else:
        original = api().FileSinks.finish
        control = KeyboardInterrupt("after code zero")
        triggered = False

        def finish(sink):
            nonlocal triggered
            original(sink)
            if not sink.startup and not triggered:
                triggered = True
                raise control

        monkeypatch.setattr(api().FileSinks, "finish", finish)
    expected = {
        "timeout": "timed_out",
        "removal": "dispatch_failed",
        "storage": "output_failed",
        "interruption": "interrupted",
    }[fault]
    if fault == "interruption":
        with pytest.raises(KeyboardInterrupt) as caught:
            run.execute()
        assert caught.value is control
        response = run.response
    else:
        response = run.execute()
    sequence.assert_settled()
    assert response["outcome"] == expected
    assert response["return_code"] == 0
    assert json.loads(read_bytes(output.paths["result.json"]))["outcome"] == expected


@pytest.mark.parametrize("stage", ["startup", "command"])
def test_launch_acquisition_failure_is_dispatch_failure(config, tmp_path, stage):
    run, sequence, output = prepared(config, tmp_path)
    failure = OSError("docker executable unavailable")
    original = sequence.launch
    failed_index = 0 if stage == "startup" else 4
    sequence.steps.pop(failed_index)
    attempted = False

    def launch(argv, **kwargs):
        nonlocal attempted
        if not attempted and len(sequence.parameters) == failed_index:
            attempted = True
            raise failure
        return original(argv, **kwargs)

    run.launch = launch
    response = run.execute()
    sequence.assert_settled()
    assert attempted
    assert run.failure is failure
    assert response["outcome"] == "dispatch_failed"
    assert response["return_code"] is None
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["outcome"] == "dispatch_failed"
    assert manifest["return_code"] is None


@pytest.mark.parametrize("flush_stream", ["stdout.txt", "stderr.txt"])
def test_write_interruption_survives_flush_failure(
    config, tmp_path, monkeypatch, flush_stream
):
    run, sequence, output = prepared(config, tmp_path)
    module = api()
    control = KeyboardInterrupt("write interrupted")
    flush_failure = OSError("flush failed")
    write = module.os.write
    fsync = module.os.fsync
    stdout_fd = output.files["stdout.txt"].fd
    flush_fd = output.files[flush_stream].fd

    def interrupted_write(fd, data):
        if not output.files["stdout.txt"].released and fd == stdout_fd:
            raise control
        return write(fd, data)

    def failed_flush(fd):
        if not output.files[flush_stream].released and fd == flush_fd:
            raise flush_failure
        return fsync(fd)

    monkeypatch.setattr(module.os, "write", interrupted_write)
    monkeypatch.setattr(module.os, "fsync", failed_flush)
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute()
    assert caught.value is control
    sequence.assert_settled()
    assert run.command.failure is control
    assert flush_failure in getattr(control, "_transaction_secondary", [])
    assert run.response["outcome"] == "interrupted"
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["outcome"] == "interrupted"
    assert manifest["persistence"]["command"]["stdout"]["failure"]["type"] == (
        "KeyboardInterrupt"
    )
    run.command.finish()
    assert run.command.failure is control


def test_copy_failure_has_null_command_status_and_pending_records(config, tmp_path):
    run, sequence, output = prepared(config, tmp_path, fault="copy")
    response = run.execute()
    sequence.assert_settled()
    assert response["outcome"] == "dispatch_failed"
    assert response["return_code"] is None
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["return_code"] is None
    assert manifest["infrastructure_operations"][2]["return_code"] == 125
    assert manifest["infrastructure_operations"][2]["parse_diagnostics"]
    assert read_bytes(output.paths["stdout.txt"]) == b""


def test_initial_manifest_exists_before_capture_and_interruption_is_unsuccessful(
    config, tmp_path, monkeypatch
):
    from footman.snapshot import SnapshotReservation

    run, sequence, output = prepared(config, tmp_path)
    control = KeyboardInterrupt("capture interrupted")

    def interrupt(*args, **kwargs):
        initial = json.loads(read_bytes(output.paths["result.json"]))
        assert initial["outcome"] == "running"
        assert initial["return_code"] is None
        assert initial["snapshot"]["tree_id"] is None
        assert all(
            op["state"] == "not_started" for op in initial["infrastructure_operations"]
        )
        raise control

    monkeypatch.setattr(SnapshotReservation, "capture", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute()
    assert caught.value is control
    assert not sequence.parameters
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["outcome"] == "interrupted"
    assert manifest["return_code"] is None
    assert run.response["outcome"] == "interrupted"


@pytest.mark.parametrize("boundary", ["replace", "write", "close"])
def test_finalization_failure_cannot_report_success(
    config, tmp_path, monkeypatch, boundary
):
    run, sequence, output = prepared(config, tmp_path)
    module = api()
    original = module.Manifest.finalize

    def fail_finalization(manifest, values):
        def fail(*args, **kwargs):
            raise OSError("final " + boundary + " failed")

        with monkeypatch.context() as patch:
            if boundary == "close":
                from footman.filesystem.descriptors import OwnedDescriptor

                real = OwnedDescriptor.close

                def close(owner):
                    real(owner)
                    raise OSError("final close failed")

                patch.setattr(OwnedDescriptor, "close", close)
            else:
                patch.setattr(module.os, boundary, fail)
            return original(manifest, values)

    monkeypatch.setattr(module.Manifest, "finalize", fail_finalization)
    with pytest.raises(Exception) as caught:
        run.execute()
    assert type(caught.value).__name__ == "PublicationError"
    assert caught.value.publication_failure is run.manifest.failure
    assert caught.value.response is run.response
    response = run.response
    sequence.assert_settled()
    assert response["outcome"] == "output_failed"
    assert response["return_code"] == 0
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["outcome"] == "running"
    assert run.manifest.failure is not None


@pytest.mark.parametrize("close_kind", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("prior", [False, True])
def test_retained_output_close_failure_is_once_only_and_unsuccessful(
    config, tmp_path, monkeypatch, close_kind, prior
):
    from footman.filesystem.operations import PosixDescriptorOps

    run, sequence, output = prepared(config, tmp_path)
    if prior:
        sequence.steps[3]["status"] = 125
        sequence.steps = sequence.steps[:4] + sequence.steps[5:]
    real = PosixDescriptorOps.close
    targets = {o.fd for o in output._files.values()}
    closed = []
    fault = close_kind("retained file close failed")

    def close(ops, fd):
        real(ops, fd)
        if fd in targets and not output._files["stdout.txt"].released:
            # Trigger only for the reserved file owner, not a reused temp fd.
            closed.append(fd)
            if len(closed) == 1:
                raise fault

    monkeypatch.setattr(PosixDescriptorOps, "close", close)
    if close_kind is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            run.execute()
        assert caught.value is fault
        response = run.response
    else:
        response = run.execute()
    sequence.assert_settled()
    assert response["outcome"] == (
        "interrupted" if close_kind is KeyboardInterrupt else "output_failed"
    )
    assert response["return_code"] == (None if prior else 0)
    assert output.file_failure is fault
    before = list(closed)
    output.close()
    assert closed == before
    assert all(o.released for o in output._files.values())
    if prior and close_kind is OSError:
        assert fault in getattr(run.failure, "_transaction_secondary", [])


def test_writer_failure_preserved_over_flush_failure(config, monkeypatch):
    from footman.output import reserve_request

    module = api()
    with reserve_request(config, {"phase": "write-flush"}) as output:
        sink = module.FileSinks(output, startup=False)
        write_failure = OSError("write failed first")
        flush_failure = OSError("flush failed second")

        def write(*args):
            raise write_failure

        def flush(*args):
            raise flush_failure

        monkeypatch.setattr(module.os, "write", write)
        monkeypatch.setattr(module.os, "fsync", flush)
        sink.chunk(ProcessStream.STDOUT, b"lost")
        sink.finish()
        assert sink.failure is write_failure
        assert flush_failure in getattr(write_failure, "_transaction_secondary", [])


def test_initial_storage_failure_suppresses_snapshot_and_docker(
    config, tmp_path, monkeypatch
):
    run, sequence, output = prepared(config, tmp_path)
    original = api().os.fsync
    target = output.files["result.json"].fd

    def fail(fd):
        if not output.files["result.json"].released and fd == target:
            raise OSError("initial manifest flush failed")
        return original(fd)

    monkeypatch.setattr(api().os, "fsync", fail)
    with pytest.raises(Exception) as caught:
        run.execute()
    assert type(caught.value).__name__ == "PublicationError"
    assert run.response["outcome"] == "output_failed"
    assert run.snapshot is None
    assert sequence.parameters == []
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["snapshot"]["reserved"] is False
    assert manifest["startup"]["return_code"] is None


def test_publication_sole_close_failure_and_independent_release(config, monkeypatch):
    from footman.filesystem.descriptors import OwnedDescriptor
    from footman.output import reserve_request

    module = api()
    with reserve_request(config, {"phase": "sole-close"}) as output:
        manifest = module.Manifest(output, config)
        manifest.initial()
        real = OwnedDescriptor.close
        closed = []

        def close(owner):
            closed.append(owner)
            real(owner)
            raise OSError("sole temporary close failure")

        with monkeypatch.context() as patch:
            patch.setattr(OwnedDescriptor, "close", close)
            with pytest.raises(OSError, match="sole temporary close"):
                manifest.finalize({"outcome": "succeeded"})
        assert len(closed) == 1
        assert (
            json.loads(read_bytes(output.paths["result.json"]))["outcome"] == "running"
        )
        assert set(p.name for p in output.directory.iterdir()) == FILES


def test_manifest_serialization_failure_is_recorded_and_not_retried(config):
    from footman.output import reserve_request

    with reserve_request(config, {"phase": "serialization"}) as output:
        manifest = api().Manifest(output, config)
        manifest.initial()
        with pytest.raises(TypeError) as caught:
            manifest.finalize({"invalid": object()})
        assert manifest.failure is caught.value
        assert not manifest.finalized
        assert (
            json.loads(read_bytes(output.paths["result.json"]))["outcome"] == "running"
        )
        with pytest.raises(RuntimeError, match="attempted"):
            manifest.finalize({"outcome": "succeeded"})


@pytest.mark.parametrize(
    "stage", ["initial", "initial_directory", "checkpoint", "known_id", "final"]
)
@pytest.mark.parametrize("fault_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("close_failure", [False, True])
def test_publication_escalation_preserves_cleanup_and_never_retries(
    config, tmp_path, monkeypatch, stage, fault_type, close_failure
):
    from footman.filesystem.operations import PosixDescriptorOps

    run, sequence, output = prepared(config, tmp_path)
    module = api()
    failure = fault_type("publication boundary failed")
    release_failure = OSError("independent directory close failed")
    directory = output._directories[-1]
    directory_fd = directory.fd
    initial_fd = output.files["result.json"].fd
    original_sync = module.os.fsync
    original_replace = module.os.replace
    original_close = PosixDescriptorOps.close
    original_write = module.os.write
    failed = False
    attempts_after_failure = []
    releases = []
    confirmed = None
    visible = None
    if stage == "known_id":
        sequence.steps = sequence.steps[:1] + sequence.steps[5:]

    def fsync(fd):
        nonlocal failed, confirmed, visible
        target = (
            stage == "initial"
            and not output.files["result.json"].released
            and fd == initial_fd
        ) or (
            fd == directory_fd
            and (
                (stage == "initial_directory" and run.snapshot is None)
                or (
                    stage == "checkpoint"
                    and run.snapshot is not None
                    and run.supervisor is None
                )
                or (
                    stage == "known_id"
                    and run.supervisor is not None
                    and run.supervisor.execution.container_id is not None
                )
                or (stage == "final" and run.manifest._attempted)
            )
        )
        if target and not failed:
            confirmed = dict(run.manifest.data)
            visible = read_bytes(output.paths["result.json"])
            failed = True
            raise failure
        return original_sync(fd)

    def replace(*args, **kwargs):
        if failed:
            attempts_after_failure.append("replace")
        return original_replace(*args, **kwargs)

    def write(fd, data):
        if failed:
            attempts_after_failure.append("write")
        return original_write(fd, data)

    def close(ops, fd):
        target = fd == directory_fd
        if target:
            releases.append(fd)
        original_close(ops, fd)
        if target and close_failure:
            raise release_failure

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(PosixDescriptorOps, "close", close)
    with pytest.raises(
        fault_type if fault_type is KeyboardInterrupt else Exception
    ) as caught:
        run.execute()
    if fault_type is KeyboardInterrupt:
        assert caught.value is failure
    else:
        assert type(caught.value).__name__ == "PublicationError"
        assert caught.value.publication_failure is failure
        assert caught.value.failure is failure
        assert caught.value.response is run.response
    assert failed
    assert run.failure is failure
    assert run.manifest.failure is failure
    assert not run.manifest.finalized
    assert run.manifest.data == confirmed
    assert read_bytes(output.paths["result.json"]) == visible
    assert run.response["return_code"] == (0 if stage == "final" else None)
    assert run.response["outcome"] == (
        "interrupted" if fault_type is KeyboardInterrupt else "output_failed"
    )
    assert (
        run.response["diagnostics"]["publication_failure"]["type"]
        == fault_type.__name__
    )
    assert Path(run.response["resultPath"]) == output.paths["result.json"]
    assert all(owner.released for owner in output._files.values())
    assert all(owner.released for owner in output._directories)
    if close_failure:
        assert release_failure in getattr(failure, "_transaction_secondary", [])
    sequence.assert_settled()
    if stage in {"initial", "initial_directory", "checkpoint"}:
        assert not sequence.parameters
    elif stage == "known_id":
        assert len(sequence.parameters) == 3
        assert run.supervisor.cleanup.confirmed_absent
        assert run.supervisor.snapshot_deleted
        assert run.response["diagnostics"]["cleanup"]["confirmed_absent"]
        assert run.response["diagnostics"]["cleanup"]["snapshot_deleted"]
        assert not run.response["diagnostics"]["cleanup"]["unresolved_resources"]
    with pytest.raises(RuntimeError, match="attempted|failed"):
        run.manifest.finalize({"outcome": "succeeded"})
    with pytest.raises(RuntimeError, match="attempted|failed"):
        run.manifest.checkpoint({"outcome": "succeeded"})
    with pytest.raises(RuntimeError, match="attempted"):
        run.execute()
    output.close()
    assert releases == [directory_fd]
    assert not attempts_after_failure
    assert read_bytes(output.paths["result.json"]) == visible


@pytest.mark.parametrize("primary_type", [OSError, KeyboardInterrupt])
def test_publication_error_retains_prior_operation_authority(
    config, tmp_path, monkeypatch, primary_type
):
    from footman.snapshot import SnapshotReservation

    run, sequence, output = prepared(config, tmp_path)
    primary = primary_type("snapshot operation failed first")
    publication = OSError("final publication failed second")

    def capture(*args):
        raise primary

    def replace(*args, **kwargs):
        raise publication

    original_finalize = api().Manifest.finalize

    def finalize(manifest, values):
        with monkeypatch.context() as patch:
            patch.setattr(api().os, "replace", replace)
            return original_finalize(manifest, values)

    monkeypatch.setattr(SnapshotReservation, "capture", capture)
    monkeypatch.setattr(api().Manifest, "finalize", finalize)
    with pytest.raises(
        primary_type if primary_type is KeyboardInterrupt else Exception
    ) as caught:
        run.execute()
    if primary_type is KeyboardInterrupt:
        assert caught.value is primary
    else:
        assert type(caught.value).__name__ == "PublicationError"
        assert caught.value.failure is primary
        assert caught.value.publication_failure is publication
        assert caught.value.__cause__ is primary
    assert run.failure is primary
    assert publication in getattr(primary, "_transaction_secondary", [])
    assert run.response["outcome"] == (
        "interrupted" if primary_type is KeyboardInterrupt else "output_failed"
    )
    assert run.response["return_code"] is None
    assert run.response["diagnostics"]["publication_failure"]["type"] == "OSError"
    assert all(owner.released for owner in output._directories)
    assert not sequence.parameters


def test_publication_failure_retained_when_directory_release_interrupts(
    config, tmp_path, monkeypatch
):
    from footman.filesystem.operations import PosixDescriptorOps

    run, sequence, output = prepared(config, tmp_path)
    module = api()
    directory_fd = output._directories[-1].fd
    original_sync = module.os.fsync
    original_close = PosixDescriptorOps.close
    publication = OSError("directory sync failed")
    control = KeyboardInterrupt("directory release interrupted")
    closes = []

    def fsync(fd):
        if fd == directory_fd and run.manifest._attempted:
            raise publication
        return original_sync(fd)

    def close(ops, fd):
        original_close(ops, fd)
        if fd == directory_fd:
            closes.append(fd)
            raise control

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(PosixDescriptorOps, "close", close)
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute()
    assert caught.value is control
    assert run.failure is control
    assert run.manifest.failure is publication
    assert publication in getattr(control, "_transaction_secondary", [])
    assert run.response["outcome"] == "interrupted"
    assert run.response["return_code"] == 0
    assert run.response["diagnostics"]["publication_failure"]["type"] == "OSError"
    assert all(owner.released for owner in output._directories)
    sequence.assert_settled()
    output.close()
    assert closes == [directory_fd]


def test_checkpoint_serialization_failure_blocks_all_later_publication(config):
    from footman.output import reserve_request

    with reserve_request(config, {"phase": "checkpoint-serialization"}) as output:
        manifest = api().Manifest(output, config)
        manifest.initial()
        before = read_bytes(output.paths["result.json"])
        confirmed = dict(manifest.data)
        with pytest.raises(TypeError) as caught:
            manifest.checkpoint({"invalid": object()})
        assert manifest.failure is caught.value
        assert manifest.data == confirmed
        for action in (manifest.checkpoint, manifest.finalize):
            with pytest.raises(RuntimeError, match="failed"):
                action({"outcome": "succeeded"})
        assert not manifest.finalized
        assert read_bytes(output.paths["result.json"]) == before
        assert set(p.name for p in output.directory.iterdir()) == FILES


def test_generated_diagnostics_cannot_abandon_output_on_stringification_error(
    config, tmp_path, monkeypatch
):
    run, sequence, output = prepared(config, tmp_path)

    class BrokenDiagnostic(OSError):
        def __str__(self):
            raise RuntimeError("diagnostic formatting failed")

    def broken(*args):
        raise BrokenDiagnostic()

    from footman.snapshot import SnapshotReservation

    monkeypatch.setattr(SnapshotReservation, "capture", broken)
    response = run.execute()
    assert response["outcome"] == "dispatch_failed"
    assert not sequence.parameters
    assert all(owner.released for owner in output._files.values())
    assert all(owner.released for owner in output._directories)
    manifest = json.loads(read_bytes(output.paths["result.json"]))
    assert manifest["failure"]["type"] == "BrokenDiagnostic"
    assert manifest["failure"]["formatting_failure"]


def test_interrupted_diagnostic_publication_still_releases_every_output_owner(
    config, tmp_path, monkeypatch
):
    from footman.snapshot import SnapshotReservation

    run, sequence, output = prepared(config, tmp_path)
    control = KeyboardInterrupt("diagnostic interruption")

    class InterruptingDiagnostic(OSError):
        def __str__(self):
            raise control

    def interrupt(*args):
        raise InterruptingDiagnostic()

    monkeypatch.setattr(SnapshotReservation, "capture", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        run.execute()
    assert caught.value is control
    assert run.response["outcome"] == "interrupted"
    assert all(owner.released for owner in output._files.values())
    assert all(owner.released for owner in output._directories)
    assert not sequence.parameters


def test_publication_collision_does_not_delete_unowned_entry(config):
    from footman.output import reserve_request
    from tests.dispatcher_harness import write_bytes

    with reserve_request(config, {"phase": "temporary-collision"}) as output:
        manifest = api().Manifest(output, config)
        manifest.initial()
        sentinel = output.directory / ".result.tmp"
        write_bytes(sentinel, b"unrelated temporary sentinel")
        with pytest.raises(FileExistsError):
            manifest.finalize({"outcome": "succeeded"})
        assert read_bytes(sentinel) == b"unrelated temporary sentinel"
        assert (
            json.loads(read_bytes(output.paths["result.json"]))["outcome"] == "running"
        )


def test_publication_concurrent_finalizers_have_one_winner(config):
    from footman.output import reserve_request

    with reserve_request(config, {"phase": "concurrency"}) as output:
        manifest = api().Manifest(output, config)
        manifest.initial()

        def finalize():
            try:
                manifest.finalize({"outcome": "command_failed", "return_code": 1})
                return "published"
            except RuntimeError:
                return "rejected"

        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(lambda _: finalize(), range(2))) == [
                "published",
                "rejected",
            ]
        assert json.loads(read_bytes(output.paths["result.json"]))["return_code"] == 1


def test_real_simultaneous_large_streams_complete_with_warnings(config):
    import sys

    from footman.output import reserve_request
    from footman.process import OwnedProcess

    code = """import sys, threading
out = bytes(range(256)) * 16384
err = b"warning\\x00\\xff\\r\\n" * 400000
def emit(stream, data):
    stream.write(data)
    stream.flush()
a = threading.Thread(target=emit, args=(sys.stdout.buffer, out))
b = threading.Thread(target=emit, args=(sys.stderr.buffer, err))
a.start(); b.start(); a.join(); b.join()
"""
    with reserve_request(config, {"phase": "parallel-output"}) as output:
        sink = api().FileSinks(output, startup=False)
        with OwnedProcess.launch(
            [sys.executable, "-c", code], capture_limit=0, deadline_seconds=15
        ) as owner:
            result = owner.stream(sink.chunk, on_stream_end=sink.end)
        sink.finish()
        assert result.returncode == 0 and not result.timed_out
        assert result.lifecycle.complete and not result.lifecycle.failures
        assert sink.complete
        assert result.stdout == result.stderr == b""
        assert read_bytes(output.paths["stdout.txt"]) == bytes(range(256)) * 16384
        assert read_bytes(output.paths["stderr.txt"]) == b"warning\x00\xff\r\n" * 400000
