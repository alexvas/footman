"""C06 actual collector bodies and public-fixture caller wiring."""

SERVER_SOURCE = r"""
import time
from pathlib import Path
import pytest
from tests import mcp_provenance as p, mcp_wheel_support as wheel
from tests import mcp_contract_support as support
from tests import test_mcp_server as collector
from tests.conftest import _mcp_worker_wheel

ROOT = Path(__file__).parent

@pytest.fixture(scope='session')
def execution_inputs():
    return None

def gate(name):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if (ROOT / name).exists() and p.read(ROOT / name) == b'go':
            return
        time.sleep(0.01)
    raise AssertionError('server gate exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, tmp_path, monkeypatch, request):
    original = wheel.build_and_install_contract_wheel
    calls = []
    def observe(root, **kwargs):
        assert kwargs['admission'] is admission.shared_pool
        assert (root / 'pyproject.toml').is_file()
        p.write_observation(ROOT / 'target-ready', b'ready')
        gate('attempt-' + initialized)
        result = original(root, **kwargs)
        assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
        assert result.command['status'] == 0
        calls.append({'region': 'server-worker-installation', 'wheel': str(result.wheel)})
        return result
    monkeypatch.setattr(wheel, 'build_and_install_contract_wheel', observe)
    monkeypatch.setattr(support, 'WHEEL_PROVIDER', lambda: request.getfixturevalue('_mcp_worker_wheel'))
    collector.test_real_server_initialization_and_exact_phase_only_discovery(tmp_path, 'wheel', admission)
    assert len(calls) == 1
    gate('released-' + initialized)
    p.write_observation(ROOT / 'execution.json', p.canonical({'commands': calls}))
    p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""


def run_collector(tmp_path, tmp_path_factory, monkeypatch, admission, collector):
    from tests import admission_execution_support as execution
    from tests import admission_private_support as private
    from tests import admission_workspace_support as workspace
    from tests import test_admission_pool as suite

    kind = "collector"
    if collector == "test_mcp_transport.py":
        source = private.SOURCE
    elif collector == "test_mcp_server.py":
        source = SERVER_SOURCE
    elif collector == "test_host_acceptance_contracts.py":
        source, kind = execution.SOURCE, "invoke"
    elif collector == "test_host_build_tooling.py":
        source, kind = execution.SOURCE, "prepared"
    elif collector == "test_host_workspace.py":
        source = workspace.SOURCE
    elif collector == "test_host_provenance.py":
        source = workspace.SOURCE.replace(
            "from tests import test_host_workspace as collector",
            "from tests import test_host_provenance as collector",
        ).replace(
            "collector.test_fresh_wheel_environment_policy_and_copied_imports(\n"
            "        tmp_path, module, bound, 'pass', 0, monkeypatch, admission=admission)",
            "value = collector.workspace.__wrapped__(tmp_path, module, bound, admission)\n"
            "    collector.test_archive_expectations_do_not_trust_stale_installation(value)",
        )
    else:
        raise AssertionError("unregistered heavy collector: " + collector)
    with monkeypatch.context() as patch:
        patch.setattr(execution, "SOURCE", source)
        suite.test_host_wrapper_uses_session_admission(
            tmp_path, tmp_path_factory, patch, admission, kind, "nested"
        )
