"""P13 generated caller scenario; admission itself comes from the real plugin."""

import base64


def commands(operations):
    """Serialize actual completed external calls without acquiring resources."""
    return [
        {
            "argv": list(op["argv"]),
            "cwd": op["cwd"],
            "status": op["status"],
            "PATH": op.get("PATH"),
            "started_monotonic": op["started_monotonic"],
            "ended_monotonic": op["ended_monotonic"],
            "stdout_base64": base64.b64encode(op["stdout"]).decode("ascii"),
            "stderr_base64": base64.b64encode(op["stderr"]).decode("ascii"),
        }
        for op in operations
    ]


def observation(bound):
    return {
        "repository_root": str(bound.repository_root),
        "wheel": str(bound.wheel),
        "wheel_sha256": bound.wheel_sha256,
        "environment": str(bound.environment),
        "support_root": str(bound.support_root),
        "commands": commands((bound.command, *bound.setup_operations)),
    }


SOURCE = r"""
import time
from pathlib import Path
import pytest
from tests import mcp_provenance as p, mcp_wheel_support as wheel
from tests.mcp_contract_support import prepare_inputs
from tests.admission_installation_support import observation

ROOT = Path(__file__).parent

@pytest.fixture(scope='session')
def installation_inputs():
    storage = Path('__INSTALLATION_ROOT__')
    prepared = prepare_inputs(Path('__REPOSITORY__'), storage / 'inputs')
    evidence = storage / 'install'
    evidence.mkdir()
    return prepared, evidence

def gate(name):
    deadline = time.monotonic() + 5
    while not (ROOT / name).exists():
        if time.monotonic() >= deadline:
            raise AssertionError('installation handshake exhausted: ' + name)
        time.sleep(0.01)
    assert p.read(ROOT / name) == b'go'

def test_ready(initialized):
    pass

def test_timeout(admission, initialized, installation_inputs):
    prepared, evidence = installation_inputs
    gate('attempt-' + initialized)
    with pytest.raises(TimeoutError):
        wheel.build_and_install_contract_wheel(prepared, admission=admission.shared_pool,
            evidence_directory=evidence, timeout_seconds=0.2)
    assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
    assert not (prepared / 'dist').exists()
    assert list(evidence.iterdir()) == []

def test_progress(admission, initialized, installation_inputs):
    prepared, evidence = installation_inputs
    gate('progress-' + initialized)
    bound = wheel.build_and_install_contract_wheel(prepared, admission=admission.shared_pool,
        evidence_directory=evidence, timeout_seconds=15)
    assert bound.command['status'] == 0
    assert bound.environment == evidence / 'venv' and bound.environment.is_dir()
    assert bound.wheel.is_file() and bound.support_root.is_dir()
    p.write_observation(ROOT / 'installation.json', p.canonical(observation(bound)))
    p.write_observation(ROOT / ('entered-' + initialized), b'installed')
"""
