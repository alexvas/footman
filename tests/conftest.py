"""Apply the permanent skip-review policy and opt-in process time fixture."""

from __future__ import annotations

import importlib

import pytest

from tests.clock_support import ManualClock, module_clock, stage_pause

pytest_plugins = ("tests.green_skip_policy", "tests.admission_plugin")


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


@pytest.fixture(scope="session")
def _mcp_worker_wheel(request, tmp_path_factory, admission):
    from pathlib import Path

    from tests import mcp_contract_support as support
    from tests import mcp_provenance as p
    from tests import mcp_wheel_support as wheel

    root = tmp_path_factory.mktemp("mcp-worker-wheel")
    prepared = support.prepare_inputs(
        Path(__file__).resolve().parents[1], root / "inputs"
    )
    evidence = root / "installation"
    evidence.mkdir(mode=0o700)
    # Session setup can precede function-scoped autouse fixtures. Admit it too,
    # without an outer lock (the build/install routine takes its own slot).
    bound = wheel.build_and_install_contract_wheel(
        prepared, admission=admission.shared_pool, evidence_directory=evidence
    )
    p.write(
        root / "setup.json",
        p.canonical(
            {
                "schema_version": 1,
                "worker": getattr(request.config, "workerinput", {}).get(
                    "workerid", "master"
                ),
                "build_count": 1,
                "venv_count": 1,
                "wheel_sha256": bound.wheel_sha256,
                "environment": str(bound.environment),
                "support_root": str(bound.support_root),
                "build_started_monotonic": bound.command["started_monotonic"],
                "build_status": bound.command["status"],
                "build_root": str(bound.repository_root),
            }
        ),
    )
    return bound


@pytest.fixture(autouse=True)
def _mcp_worker_provider(request, monkeypatch):
    """Keep the lazy wheel factory available independently of collector names.

    Installing this factory acquires nothing. The session fixture explicitly
    depends on admission; callers select heavy work by invoking the factory.
    """
    from tests import mcp_contract_support

    monkeypatch.setattr(
        mcp_contract_support,
        "WHEEL_PROVIDER",
        lambda: request.getfixturevalue("_mcp_worker_wheel"),
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
