"""Shared supervision configuration, cleanup script and execution helpers."""

import importlib
from pathlib import Path

from tests.launcher_case_support import ID, config, steps
from tests.launcher_support import DockerSequence


def api():
    import footman

    assert (Path(footman.__file__).parent / "supervision.py").is_file(), (
        "required bounded run supervision and recorded-ID cleanup are absent"
    )
    return importlib.import_module("footman.supervision")


def removal(identity=ID, **outcomes):
    return [
        {
            "argv": ["docker", "rm", "-f", identity],
            "container_id": identity,
            **outcomes.get("rm", {}),
        },
        {
            "argv": [
                "docker",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "id=" + identity,
                "--format",
                "{{.ID}}",
            ],
            "container_id": identity,
            **outcomes.get("listing", {}),
        },
    ]


def run_case(tmp_path, source, *, script=None, cfg=None):
    cfg = cfg or config()
    stub_directory = tmp_path / "stub"
    stub_directory.mkdir()
    sequence = DockerSequence(
        stub_directory, script or [*steps(source, cfg), *removal()]
    )
    run = api().RunSupervisor(cfg, source, launch=sequence.launch)
    run.execute(lambda *_: None, lambda *_: None)
    return run, sequence
