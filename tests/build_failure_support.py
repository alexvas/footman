"""Shared public-surface fault driver; never imports either build implementation."""

import os
from pathlib import Path
from unittest.mock import patch

from tests import mcp_attribution_support as attribution
from tests import mcp_contract_support as fixtures
from tests import mcp_provenance as p
from tests.wheel_attribution_cases import case


def exercise(root, tmp, surface, invoke, fault):
    prepared = fixtures.prepare_inputs(root, tmp / "prepared")
    evidence = tmp / "evidence"
    evidence.mkdir()
    scenario = case(
        "success" if fault == "successful-build" else "build-status-failure"
    )
    executable, call = attribution.scripted_uv(tmp, scenario)
    original_read, original_command = p.read, surface.command
    interruption = KeyboardInterrupt("post-build interruption")
    post_error = (
        interruption
        if fault == "post-interruption"
        else OSError("post-build input read failed")
    )
    operations = []
    collection_error = None

    def read(path, **kwargs):
        if Path(path) == prepared / "README.md" and call.exists():
            raise post_error
        return original_read(path, **kwargs)

    def command(*args, **kwargs):
        nonlocal collection_error
        observation = original_command(*args, **kwargs)
        operations.append(observation)
        if fault == "collection-failure":
            collection_error = surface.BuildFailure(
                "injected collection failure", observation
            )
            raise collection_error
        return observation

    caught = None
    with (
        patch.dict(
            os.environ,
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]},
        ),
        patch.object(p, "read", read),
        patch.object(surface, "command", command),
    ):
        try:
            invoke(prepared, evidence)
        except BaseException as error:
            caught = error
    if not (len(operations) == 1 and operations[0]["status"] == scenario.build_status):
        raise AssertionError(
            "verification failed: len(operations) == 1 and operations[0]['status'] == scenario.build_status"
        )
    if not (operations[0]["stdout"] == scenario.stdout_bytes):
        raise AssertionError(
            "verification failed: operations[0]['stdout'] == scenario.stdout_bytes"
        )
    if not (operations[0]["stderr"] == scenario.stderr_bytes):
        raise AssertionError(
            "verification failed: operations[0]['stderr'] == scenario.stderr_bytes"
        )
    if fault == "successful-build":
        if caught is not post_error:
            raise AssertionError("verification failed: caught is post_error")
        if not (caught.observation["status"] == 0):
            raise AssertionError(
                "verification failed: caught.observation['status'] == 0"
            )
    elif fault == "post-interruption":
        if caught is not interruption:
            raise AssertionError("verification failed: caught is interruption")
        if not (isinstance(caught.__cause__, surface.BuildFailure)):
            raise AssertionError(
                "verification failed: isinstance(caught.__cause__, surface.BuildFailure)"
            )
        if not (caught.__cause__.observation["status"] == 17):
            raise AssertionError(
                "verification failed: caught.__cause__.observation['status'] == 17"
            )
    else:
        if not (isinstance(caught, surface.BuildFailure)):
            raise AssertionError(
                "verification failed: isinstance(caught, surface.BuildFailure)"
            )
        if caught.post_check_error is not post_error:
            raise AssertionError(
                "verification failed: caught.post_check_error is post_error"
            )
        if fault == "collection-failure":
            if caught is not collection_error:
                raise AssertionError("verification failed: caught is collection_error")
        if not (caught.observation["status"] == 17):
            raise AssertionError(
                "verification failed: caught.observation['status'] == 17"
            )
        if not (caught.observation["stdout"] == scenario.stdout_bytes):
            raise AssertionError(
                "verification failed: caught.observation['stdout'] == scenario.stdout_bytes"
            )
        if not (caught.observation["stderr"] == scenario.stderr_bytes):
            raise AssertionError(
                "verification failed: caught.observation['stderr'] == scenario.stderr_bytes"
            )
        if caught.observation["attempted"] is not True:
            raise AssertionError(
                "verification failed: caught.observation['attempted'] is True"
            )
        if caught.observation["post_identity"] is not None:
            raise AssertionError(
                "verification failed: caught.observation['post_identity'] is None"
            )
        if str(post_error) not in str(getattr(caught, "__notes__", ())):
            raise AssertionError(
                "verification failed: str(post_error) in str(getattr(caught, '__notes__', ()))"
            )
    if not (not (evidence / "venv").exists()):
        raise AssertionError("verification failed: not (evidence / 'venv').exists()")
    if not (not (evidence / "copied").exists()):
        raise AssertionError("verification failed: not (evidence / 'copied').exists()")
