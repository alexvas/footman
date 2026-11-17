"""Reusable host assertions; callers supply observations, never release authority.

Docker-free controls exercise these checks with real MCP/retained manifests and
scripted Docker records. Only a future real-host producer can establish actual
container bytes, execution and removal. This module acquires no resources.
"""

import base64
import re
from collections import Counter
from pathlib import PurePosixPath


def require(condition, message):
    if not condition:
        raise AssertionError(message)


EXPECTATION_CASES = ("staged_bytes", "argv", "command_status", "cleanup")
EXPECTATION_DIAGNOSTICS = {
    "staged_bytes": "staged bytes mismatch",
    "argv": "configured argv mismatch",
    "command_status": "command status mismatch",
    "cleanup": "container cleanup mismatch",
}


def validate_expectation_input(value):
    """Validate fixture-only JSON values without acquiring resources.

    Staged bytes use a UTF-8 string, argv a nonempty string array, command
    status an integer, and cleanup a boolean confirmed-absence
    expectation. None of these values selects work or changes observations.
    Callers must reject unexpected input in normal profiles before calling us.
    """
    require(type(value) is dict, "expectation input must be an object")
    require(
        set(value) == {"schema_version", "case_id", "expected_override"},
        "expectation input fields mismatch",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "unsupported expectation schema",
    )
    case = value["case_id"]
    require(type(case) is str and case in EXPECTATION_CASES, "unknown expectation case")
    expected = value["expected_override"]
    if case == "staged_bytes":
        require(
            type(expected) is str, "staged bytes expectation must be a UTF-8 string"
        )
        try:
            expected.encode("utf-8")
        except UnicodeEncodeError as error:
            raise AssertionError(
                "staged bytes expectation must be a UTF-8 string"
            ) from error
    elif case == "argv":
        require(
            type(expected) is list
            and bool(expected)
            and all(type(arg) is str and arg and "\x00" not in arg for arg in expected),
            "argv expectation must be a nonempty NUL-free string array",
        )
        expected = list(expected)
    elif case == "command_status":
        require(
            type(expected) is int,
            "command status expectation must be an integer",
        )
    else:
        require(type(expected) is bool, "cleanup expectation must be a boolean")
    return {"schema_version": 1, "case_id": case, "expected_override": expected}


def verify_expectation(case_id, actual, expected):
    """The same assertions serve normal observations and isolated mismatches."""
    validated = validate_expectation_input(
        {"schema_version": 1, "case_id": case_id, "expected_override": expected}
    )
    expected = validated["expected_override"]
    if case_id == "staged_bytes":
        expected = expected.encode("utf-8")
    require(
        type(actual) is type(expected) and actual == expected,
        EXPECTATION_DIAGNOSTICS[case_id],
    )


def verify_copy(manifest, destination):
    """Match actual launch validation and transfer to the same recorded ID."""
    identity = manifest["container_id"]
    startup = manifest["startup"]
    require(
        startup["return_code"] == 0 and startup["lifecycle_complete"] is True,
        "launch unsuccessful",
    )
    launch = startup["argv"]
    separator = launch.index("--")
    require(
        launch[:4] == ["docker", "run", "--detach=true", "--pull=never"]
        and launch[separator + 1] == manifest["configured_image_id"],
        "literal configured image changed",
    )
    inspect, mkdir, copy = manifest["infrastructure_operations"][:3]
    require(
        inspect["argv"]
        == [
            "docker",
            "container",
            "inspect",
            "--format",
            "{{.Id}}|{{.State.Status}}|{{.Image}}",
            identity,
        ],
        "inspection identity mismatch",
    )
    inspected = (
        base64.b64decode(inspect["stdout"]["excerpt_base64"], validate=True)
        .rstrip(b"\n")
        .split(b"|")
    )
    require(
        len(inspected) >= 2
        and inspected[0].decode().lower() == identity.lower()
        and inspected[1] == b"running",
        "container was not inspected running",
    )
    require(
        mkdir["argv"]
        == ["docker", "exec", "-u", "root", identity, "mkdir", "--", destination],
        "fresh directory protocol mismatch",
    )
    if copy["argv"]:
        require(
            copy["argv"]
            == [
                "docker",
                "cp",
                manifest["snapshot"]["path"] + "/.",
                identity + ":" + destination + "/",
            ],
            "contents copy identity mismatch",
        )
    observed = manifest["observed_image_id"]
    require(
        type(observed) is str and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", observed),
        "invalid observed image attribution",
    )
    require(
        len(inspected) == 3 and inspected[2] == observed.encode("ascii"),
        "observed image attribution mismatch with inspection",
    )


def verify_preparation_failure(value, manifest, streams, destination):
    """An existing image directory must never become a stale-content merge."""
    verify_copy(manifest, destination)
    operations = manifest["infrastructure_operations"]
    require(
        value["outcome"] == manifest["outcome"] == "dispatch_failed",
        "preparation failure outcome mismatch",
    )
    require(
        value["return_code"] is None and manifest["return_code"] is None,
        "preparation failure ran command",
    )
    require(
        type(operations[1]["return_code"]) is int and operations[1]["return_code"] != 0,
        "mkdir failure unobserved",
    )
    require(
        operations[2]["argv"] == [] and operations[2]["return_code"] is None,
        "copy ran after mkdir failure",
    )
    require(
        manifest["docker_command_argv"] == []
        and streams["stdout"] == streams["stderr"] == b"",
        "command ran after mkdir failure",
    )
    verify_expectation("cleanup", manifest["cleanup"]["confirmed_absent"], True)
    require(
        manifest["cleanup"]["snapshot_deleted"] is True
        and not manifest["cleanup"]["unresolved_resources"],
        "preparation resources unresolved",
    )


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
    staged_bytes,
    expected_staged_bytes,
):
    """Verify a completed command and mandatory staged evidence at any status."""
    verify_expectation("staged_bytes", staged_bytes, expected_staged_bytes)
    require(type(status) is int, "expected status must be an integer")
    require(manifest["snapshot"]["tree_id"] == tree_id, "staged tree mismatch")
    verify_expectation("command_status", manifest["return_code"], status)
    verify_expectation("command_status", value["return_code"], status)
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
    verify_expectation("argv", manifest["command_argv"], list(command_argv))
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
    verify_expectation("cleanup", cleanup["confirmed_absent"], True)
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
