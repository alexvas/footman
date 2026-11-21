"""Copy failure boundaries and full-drain diagnostic stream contracts."""

import base64
from pathlib import Path

import pytest

from footman.process import ProcessStream, StreamOutcome
from tests.launcher_case_support import (
    ID,
    INSPECT,
    api,
    config,
    dispatch,
    source,
    steps,
)
from tests.launcher_support import DockerSequence

__all__ = ["source"]


@pytest.mark.sleepy
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


@pytest.mark.sleepy
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


@pytest.mark.sleepy
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
