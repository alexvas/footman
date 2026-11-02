"""Apply the permanent skip-review policy and opt-in process time fixture."""

from __future__ import annotations

import importlib

import pytest

from tests.clock_support import ManualClock, module_clock, stage_pause

pytest_plugins = ("tests.green_skip_policy",)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "process_time: run a process scenario on the shared frozen timeline"
    )
    config.addinivalue_line(
        "markers", "real_process_time: keep operating-system process timing real"
    )
    config.addinivalue_line(
        "markers", "own_process_time: scenario installs its own shared process clock"
    )


@pytest.fixture
def process_clock():
    """One frozen process timeline for a marked process migration scenario."""
    process = importlib.import_module("footman.process")
    # A zero origin keeps exact deadline boundaries independent of host uptime.
    clock = ManualClock(origin=0.0)
    with module_clock(process, clock):
        yield clock


@pytest.fixture
def process_stage_pause(process_clock):
    """Explicit policy-pause progression for selected pending scenarios."""
    process = importlib.import_module("footman.process")
    with stage_pause(process, process_clock, label="process-stage"):
        yield process_clock


@pytest.fixture(autouse=True)
def _process_time(request):
    """Install the shared process timeline only for marked P5 scenarios.

    ``real_process_time`` keeps OS/process integration real, and
    ``own_process_time`` leaves a scenario that installs its own shared
    timeline to do so without a second timeline on the same module.
    """
    if request.node.get_closest_marker("process_time") is None:
        yield None
        return
    if (
        request.node.get_closest_marker("real_process_time") is not None
        or request.node.get_closest_marker("own_process_time") is not None
    ):
        yield None
        return
    yield request.getfixturevalue("process_clock")
