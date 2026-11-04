"""Phase 5 adapter contracts; a scripted CLI cannot prove Docker semantics."""

import base64
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from footman.process import ProcessStream, StreamOutcome
from tests.dispatcher_harness import GitFixture, read_bytes, write_bytes
from tests.launcher_support import DockerSequence

ID = "a" * 64
IMAGE = "sha256:" + "b" * 64
START = [
    "docker",
    "run",
    "--detach=true",
    "--pull=never",
    "--",
    "d9e853e87e55",
    "sleep",
    "infinity",
]
INSPECT = [
    "docker",
    "container",
    "inspect",
    "--format",
    "{{.Id}}|{{.State.Status}}|{{.Image}}",
    ID,
]
RUN_ID = "01k8w00n007008000000000000"


def api():
    import footman

    assert (Path(footman.__file__).parent / "container.py").is_file(), (
        "required container adapter is absent"
    )
    return importlib.import_module("footman.container")


@pytest.fixture
def source(tmp_path):
    from footman.snapshot import reserve_snapshot

    repo = GitFixture.create(tmp_path / "repo")
    write_bytes(repo.path / "wrapper", b"#!/bin/sh\necho staged\n", mode=0o755)
    os.symlink("payload", repo.path / "link")
    repo.git("add", "wrapper", "link")
    with reserve_snapshot(tmp_path / "snap shots:line\n", RUN_ID) as snapshot:
        tree = snapshot.capture(repo.path)
        snapshot.export(tree)
        write_bytes(repo.path / "payload", b"later workdir")
        yield snapshot


def config(**overrides):
    values = dict(
        image_id="d9e853e87e55",
        container_start_argv=tuple([*START[:5], "{image_id}", *START[6:]]),
        container_snapshot_directory="/workspace/source",
        container_working_directory="/workspace/source/subdir",
        user="root",
        command_argv=("tool", "two words", "; $literal", "{phase}"),
        timeout_seconds=20,
        cleanup_grace_seconds=2,
    )
    return SimpleNamespace(**(values | overrides))


def steps(source, cfg=None):
    cfg = cfg or config()
    return [
        {"argv": START, "stdout": (ID + "\n").encode()},
        {"argv": INSPECT, "stdout": f"{ID}|running|{IMAGE}\n".encode()},
        {
            "argv": [
                "docker",
                "exec",
                "-u",
                "root",
                ID,
                "mkdir",
                "--",
                cfg.container_snapshot_directory,
            ]
        },
        {
            "argv": [
                "docker",
                "cp",
                str(source.path) + "/.",
                ID + ":" + cfg.container_snapshot_directory + "/",
            ]
        },
        {
            "argv": [
                "docker",
                "exec",
                "-u",
                cfg.user,
                "-w",
                cfg.container_working_directory,
                ID,
                *cfg.command_argv,
            ]
        },
    ]


def dispatch(tmp_path, source, script=None, cfg=None, **kwargs):
    module = api()
    cfg = cfg or config()
    sequence = DockerSequence(
        tmp_path, steps(source, cfg) if script is None else script
    )
    run = module.ContainerExecution(cfg, source, launch=sequence.launch)
    startup, command = (
        {s: bytearray() for s in ProcessStream},
        {s: bytearray() for s in ProcessStream},
    )
    run.execute(
        lambda s, b: startup[s].extend(b), lambda s, b: command[s].extend(b), **kwargs
    )
    sequence.assert_settled()
    return run, sequence, startup, command


@pytest.mark.parametrize("executable", ["tool", "./wrapper"])
def test_dev_exec_image_and_wrapper_argv(tmp_path, source, executable):
    cfg = config(command_argv=(executable, "two words", "; $literal"))
    run, sequence, _, _ = dispatch(tmp_path, source, cfg=cfg)
    assert run.container_id == ID
    assert run.observed_image_id == IMAGE
    assert run.config.image_id == "d9e853e87e55"
    assert len(sequence.calls()) == 5
    assert all(p[1]["capture_limit"] == 0 for p in sequence.parameters)
    deadlines = [p[1]["deadline_seconds"] for p in sequence.parameters]
    assert all(a > b for a, b in zip(deadlines, deadlines[1:], strict=False))
    assert (
        sum(p[1]["grace_seconds"] for p in sequence.parameters)
        <= cfg.cleanup_grace_seconds
    )
    assert (
        "-i" not in sequence.parameters[-1][0]
        and "-t" not in sequence.parameters[-1][0]
    )


@pytest.mark.parametrize("status", [0, 1, 126, 127])
def test_dev_exec_status_and_streams(tmp_path, source, status):
    script = steps(source)
    script[0]["stderr"] = b"startup diagnostic\xff"
    script[-1].update(
        status=status,
        stdout=b"command out\x00",
        stderr=b"command err\xff",
        stdout_repeat=20000,
        stderr_repeat=20000,
    )
    run, _, startup, command = dispatch(tmp_path, source, script)
    assert run.command_result.returncode == status
    assert bytes(command[ProcessStream.STDOUT]) == script[-1]["stdout"] * 20000
    assert bytes(command[ProcessStream.STDERR]) == script[-1]["stderr"] * 20000
    assert bytes(startup[ProcessStream.STDERR]) == script[0]["stderr"]
    assert bytes(startup[ProcessStream.STDOUT]) == (ID + "\n").encode()


@pytest.mark.parametrize(
    "reason",
    [
        "user unavailable",
        "cwd unavailable",
        "executable unavailable",
        "permission denied",
    ],
)
def test_nonroot_prerequisite_error_status(tmp_path, source, reason):
    cfg = config(user="worker")
    script = steps(source, cfg)
    script[-1].update(status=126, stderr=reason.encode())
    run, seq, _, command = dispatch(tmp_path, source, script, cfg)
    assert run.command_result.returncode == 126
    assert bytes(command[ProcessStream.STDERR]) == reason.encode()
    assert seq.parameters[-1][0][3] == "worker"


def test_dev_snapshot_copy_identity(tmp_path, source):
    assert read_bytes(source.path / "payload") == b"staged\n"
    assert (source.path / "wrapper").stat().st_mode & 0o777 == 0o755
    assert (source.path / "link").is_symlink()
    assert os.readlink(source.path / "link") == "payload"
    assert not (source.path / ".git").exists()
    run, sequence, _, _ = dispatch(tmp_path, source)
    assert [op.name for op in run.infrastructure_operations] == [
        "inspect",
        "mkdir",
        "cp",
    ]
    assert all(op.result.returncode == 0 for op in run.infrastructure_operations)
    assert sequence.calls()[3]["argv"][1] == str(source.path) + "/."
    assert all(call["container_id"] == ID for call in sequence.calls())


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize("status", [1, 125])
def test_failure_suppresses_downstream_calls(tmp_path, source, stage, status):
    module = api()
    script = steps(source)
    script[stage].update(
        status=status, stderr=b"missing parent/existing destination/partial copy"
    )
    sequence = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(config(), source, launch=sequence.launch)
    with pytest.raises(module.ContainerError):
        run.execute(lambda *a: None, lambda *a: pytest.fail("command must not run"))
    assert len(sequence.calls()) == stage + 1
    assert run.command_result is None
    assert run.container_id == (None if stage == 0 else ID)
    sequence.assert_settled()


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"a" * 12,
        b"z" * 64,
        (ID + "\n\n").encode(),
        (ID + " extra").encode(),
        (ID + "\r\n").encode(),
        (ID + "\n" + ID).encode(),
    ],
)
def test_invalid_startup_forbids_inspection(tmp_path, source, output):
    module = api()
    script = steps(source)
    script[0]["stdout"] = output
    seq = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(config(), source, launch=seq.launch)
    with pytest.raises(module.ContainerError):
        run.execute(lambda *a: None, lambda *a: pytest.fail("command"))
    assert len(seq.calls()) == 1
    assert run.container_id is None
    seq.assert_settled()


@pytest.mark.parametrize(
    "record",
    [
        b"",
        b"bad|running",
        ("c" * 64 + "|running").encode(),
        (ID + "|exited").encode(),
        (ID + "|Running").encode(),
        (ID + "|running\n\n").encode(),
        (ID + "|running\r\n").encode(),
        (ID + "\n|running").encode(),
        ID.encode(),
    ],
)
def test_invalid_inspection_forbids_copy(tmp_path, source, record):
    module = api()
    script = steps(source)
    script[1]["stdout"] = record
    seq = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(config(), source, launch=seq.launch)
    with pytest.raises(module.ContainerError):
        run.execute(lambda *a: None, lambda *a: pytest.fail("command"))
    assert len(seq.calls()) == 2
    assert run.container_id == ID
    assert run.infrastructure_operations[0].diagnostics
    seq.assert_settled()


@pytest.mark.parametrize("metadata", ["", "|", "|bad", "|sha256:123", "|" + IMAGE])
@pytest.mark.parametrize("newline", ["", "\n"])
def test_inspection_unknown_image_is_diagnostic_only(
    tmp_path, source, metadata, newline
):
    script = steps(source)
    script[1]["stdout"] = (ID.upper() + "|running" + metadata + newline).encode()
    run, seq, _, _ = dispatch(tmp_path, source, script)
    assert len(seq.calls()) == 5
    assert run.observed_image_id == (IMAGE if metadata == "|" + IMAGE else None)
    assert bool(run.infrastructure_operations[0].diagnostics) == (
        metadata != "|" + IMAGE
    )


@pytest.mark.parametrize("image", ["d9e853e87e55", IMAGE])
@pytest.mark.parametrize("resolution", ["error", "hex-name-collision", "retagged"])
def test_dev_image_id_forms(tmp_path, source, image, resolution):
    module = api()
    cfg = config(image_id=image)
    script = steps(source, cfg)
    script[0]["argv"] = [*START[:5], image, *START[6:]]
    if resolution == "error":
        script[0].update(
            status=125,
            stderr=b"No such local image; hexadecimal operand interpreted as name",
        )
    seq = DockerSequence(tmp_path, script)
    run = module.ContainerExecution(cfg, source, launch=seq.launch)
    if resolution == "error":
        with pytest.raises(module.ContainerError):
            run.execute(lambda *a: None, lambda *a: pytest.fail("command"))
        assert len(seq.calls()) == 1
    else:
        run.execute(lambda *a: None, lambda *a: None)
        assert run.observed_image_id == IMAGE
        assert len(seq.calls()) == 5
    assert seq.parameters[0][0] == script[0]["argv"]
    assert seq.parameters[0][0].count("--pull=never") == 1
    seq.assert_settled()


@pytest.mark.parametrize(
    "destination", ["/work space/source", "/work:space/source", "/work\nspace/source"]
)
def test_copy_paths_are_literal(tmp_path, source, destination):
    cfg = config(
        container_snapshot_directory=destination,
        container_working_directory=destination,
    )
    run, seq, _, _ = dispatch(tmp_path, source, cfg=cfg)
    assert seq.parameters[3][0][-1] == ID + ":" + destination + "/"
    assert run.command_result.returncode == 0


@pytest.mark.parametrize(
    "ending",
    [
        StreamOutcome.EOF,
        StreamOutcome.READ_FAILED,
        StreamOutcome.ABORTED,
        StreamOutcome.PENDING,
    ],
)
@pytest.mark.parametrize("width", [1, 13, 4095, 65536])
def test_summary_raw_prefix_chunk_independent(ending, width):
    module = api()
    collector = module.InfrastructureCollector()
    raw = (
        b"https://user:password@host/?token=secret\xff"
        + b"x" * 4050
        + b"\xfeTOKEN=trailing" * 1000
    )
    for i in range(0, len(raw), width):
        collector.chunk(ProcessStream.STDERR, raw[i : i + width])
    if ending is not StreamOutcome.PENDING:
        collector.end(ProcessStream.STDERR, ending)
    summary = collector.summaries[ProcessStream.STDERR]
    assert base64.b64decode(summary.excerpt_base64) == raw[:4096]
    assert summary.observed_bytes == len(raw)
    assert summary.retained_bytes == 4096
    assert summary.truncated
    assert summary.outcome is ending


def test_summary_continues_real_drain(tmp_path, source):
    script = steps(source)
    raw = b"https://credential@host\xff" + b"x" * 20000 + b"TRAILING"
    script[1]["stderr"] = raw
    # Large optional image metadata must not truncate mandatory-field parsing.
    script[1]["stdout"] = (ID + "|running|").encode() + b"x" * 20000
    run, _, _, _ = dispatch(tmp_path, source, script)
    op = run.infrastructure_operations[0]
    assert op.started_at is not None and op.ended_at >= op.started_at
    assert op.argv == tuple(INSPECT)
    for stream, data in [
        (ProcessStream.STDOUT, script[1]["stdout"]),
        (ProcessStream.STDERR, raw),
    ]:
        summary = op.collector.summaries[stream]
        assert base64.b64decode(summary.excerpt_base64) == data[:4096]
        assert summary.observed_bytes == len(data)
        assert summary.outcome is StreamOutcome.EOF
    assert run.observed_image_id is None


def test_documentation_copy_contract():
    root = Path(__file__).resolve().parents[1]
    config_doc = (root / "docs/configuration.md").read_text()
    example = (root / "examples/footman.toml").read_text()
    for text in (config_doc, example):
        for phrase in (
            "image_id",
            "{image_id}",
            "--pull=never",
            "/workspace/source",
            "/workspace",
            "mkdir",
            "--",
            "root",
            "non-root",
        ):
            assert phrase in text
        assert "{snapshot_path}" not in text
    for phrase in (
        "docker image ls",
        "docker image inspect",
        "d9e853e87e55",
        "hexadecimal",
        "Base64",
        "no secrecy",
        "4096",
        "symlink-free",
        "without -p",
        "restart",
        "no content redaction",
    ):
        assert phrase in config_doc
