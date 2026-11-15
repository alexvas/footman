"""Reusable host assertions; callers supply observations, never release authority.

Docker-free controls exercise these checks with real MCP/retained manifests and
scripted Docker records. Only a future real-host producer can establish actual
container bytes, execution and removal. This module acquires no resources.
"""

import re
from collections import Counter
from pathlib import PurePosixPath


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def verify_run(
    value,
    manifest,
    streams,
    *,
    tree_id,
    status,
    phase,
    command_argv,
    user,
    cwd,
    configured_image,
    expected_stdout,
    expected_stderr,
    snapshot_present,
):
    """Verify a normally completed command, independently of status zero."""
    require(type(status) is int, "expected status must be an integer")
    require(manifest["snapshot"]["tree_id"] == tree_id, "staged tree mismatch")
    require(
        type(manifest["return_code"]) is int
        and manifest["return_code"] == status
        and type(value["return_code"]) is int
        and value["return_code"] == status,
        "command status mismatch",
    )
    outcome = "succeeded" if status == 0 else "command_failed"
    require(
        manifest["outcome"] == value["outcome"] == outcome, "command outcome mismatch"
    )
    require(manifest["phase"] == phase, "phase mismatch")
    directory = PurePosixPath(manifest["output_directory"])
    require(directory.is_absolute(), "output directory is not absolute")
    filenames = {
        "stdoutPath": "stdout.txt",
        "stderrPath": "stderr.txt",
        "startupStdoutPath": "startup-stdout.txt",
        "startupStderrPath": "startup-stderr.txt",
        "resultPath": "result.json",
    }
    require(set(manifest["paths"]) == set(filenames), "retained path set mismatch")
    for field, name in filenames.items():
        require(
            value[field] == manifest["paths"][field] == str(directory / name),
            "retained output path mismatch",
        )
    require(manifest["command_argv"] == list(command_argv), "configured argv mismatch")
    require(manifest["user"] == user and manifest["cwd"] == cwd, "user/cwd mismatch")
    require(
        manifest["configured_image_id"] == configured_image, "configured image changed"
    )
    identity = manifest["container_id"]
    require(
        isinstance(identity, str) and re.fullmatch(r"[0-9a-fA-F]{64}", identity),
        "invalid container identity",
    )
    expected_exec = ["docker", "exec", "-u", user, "-w", cwd, identity, *command_argv]
    require(
        manifest["docker_command_argv"] == expected_exec, "Docker exec argv mismatch"
    )
    require(streams["stdout"] == expected_stdout, "command stdout mismatch")
    require(streams["stderr"] == expected_stderr, "command stderr mismatch")
    require(
        streams["startup_stdout"] == (identity + "\n").encode(),
        "startup identity mismatch",
    )
    require(streams["startup_stderr"] == b"", "unexpected startup diagnostic")
    cleanup = manifest["cleanup"]
    require(cleanup["confirmed_absent"] is True, "container removal unconfirmed")
    require(cleanup["snapshot_deleted"] is True, "snapshot deletion unconfirmed")
    require(not cleanup["unresolved_resources"], "unresolved resources")
    require(snapshot_present is False, "snapshot still present")
    operations = manifest["infrastructure_operations"]
    require(
        [op["name"] for op in operations]
        == ["inspect", "mkdir", "cp", "rm", "confirmation"],
        "infrastructure operation order mismatch",
    )
    removal = ["docker", "rm", "-f", identity]
    confirmation = [
        "docker",
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        "id=" + identity,
        "--format",
        "{{.ID}}",
    ]
    expected = Counter((tuple(removal), tuple(confirmation)))
    observed = Counter(
        tuple(op["argv"]) for op in operations if op["name"] in {"rm", "confirmation"}
    )
    require(observed == expected, "recorded-ID cleanup argv mismatch")
    for op in operations:
        require(
            type(op["return_code"]) is int
            and op["return_code"] == 0
            and op["lifecycle_complete"] is True
            and not op["lifecycle_failures"]
            and op["failure"] is None
            and op["collector_failure"] is None,
            "infrastructure operation unsuccessful",
        )
        for stream in ("stdout", "stderr"):
            require(
                op[stream]["completeness_name"] == "EOF",
                "infrastructure stream incomplete",
            )
    require(
        operations[-1]["stdout"]["observed_bytes"] == 0,
        "recorded container still listed",
    )
