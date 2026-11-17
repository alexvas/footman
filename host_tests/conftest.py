"""Explicit policy registration, independent of development conftest."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from host_tests.support import HOST_MARKERS

pytest_plugins = ("tests.green_skip_policy",)
_EXPECTATION = pytest.StashKey[Any]()


def pytest_sessionstart(session: pytest.Session) -> None:
    root = Path(__file__).parent
    selected = [Path(arg).absolute() for arg in session.config.args]
    negative = selected == [root / "test_negative_expectations.py"]
    path = root / "expectation_input.json"
    present = os.path.lexists(path)
    if not negative:
        if present or any(p.name == "test_negative_expectations.py" for p in selected):
            raise pytest.UsageError(
                "unexpected normal expectation input or mixed negative selection"
            )
        session.config.stash[_EXPECTATION] = None
        return
    from tests import host_acceptance_assertions as assertions
    from tests import mcp_provenance as p

    for module in (assertions, p):
        assertions.require(
            Path(module.__file__) == root.parent / "tests" / Path(module.__file__).name,
            "ambient assertion/support fallback",
        )
    session.config.stash[_EXPECTATION] = assertions.validate_expectation_input(
        p.load(path)
    )
    # Resource-free controller/worker observations, not another process owner.
    from host_tests.support import Outcomes

    session.config.pluginmanager.register(Outcomes(), "host-negative-outcomes")


@pytest.fixture(scope="session")
def expectation(request: pytest.FixtureRequest) -> Any:
    return request.config.stash[_EXPECTATION]


@pytest.fixture(scope="session")
def host_flow() -> Iterator[Any]:
    from host_tests.execution import HostFlow
    from tests import mcp_provenance as p

    path = os.environ.get("FOOTMAN_HOST_CONTEXT")
    if path is None:
        raise ValueError("missing runner-generated host context")
    flow = HostFlow(p.load(Path(path)))
    try:
        yield flow
    finally:
        flow.finish()


def pytest_configure(config: pytest.Config) -> None:
    for marker in HOST_MARKERS:
        config.addinivalue_line("markers", marker)
