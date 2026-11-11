"""Execution argv, retained stream bytes and staged-source copy contracts."""

import os

import pytest

from footman.process import ProcessStream
from tests.dispatcher_harness import read_bytes
from tests.launcher_case_support import ID, IMAGE, config, dispatch, source, steps

__all__ = ["source"]


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
