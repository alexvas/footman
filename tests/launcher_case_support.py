"""Shared staged-source fixture and launcher scenario construction helpers."""

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from footman.process import ProcessStream
from tests.dispatcher_harness import GitFixture, write_bytes
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
