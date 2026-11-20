"""P19 actual private-support collector wiring scenario."""

SOURCE = r"""
import time
from pathlib import Path
import pytest
from tests import mcp_provenance as p, mcp_wheel_support as wheel
from tests import mcp_contract_support as support
from tests import test_mcp_transport as collector
from tests.conftest import _mcp_worker_wheel

ROOT = Path(__file__).parent

@pytest.fixture(scope='session')
def execution_inputs(_mcp_worker_wheel):
    return _mcp_worker_wheel

def gate(name):
    limit = time.monotonic() + 5
    while time.monotonic() < limit:
        path = ROOT / name
        if path.exists():
            value = p.read(path)
            assert b'go'.startswith(value)
            if value == b'go':
                return
        time.sleep(0.01)
    raise AssertionError('private support gate exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, execution_inputs, tmp_path, monkeypatch):
    original = wheel.reuse_for_test
    calls = []
    def observe(base, destination, **kwargs):
        assert base is execution_inputs and kwargs['admission'] is admission.shared_pool
        if kwargs.get('private_support'):
            p.write_observation(ROOT / 'target-ready', b'ready')
            gate('attempt-' + initialized)
        result = original(base, destination, **kwargs)
        if kwargs.get('private_support'):
            assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
            assert result.support_root != base.support_root
            for item in base.support_inventory:
                assert p.read(result.support_root / item['path']) == p.read(base.support_root / item['path'])
                assert not (result.support_root / item['path']).samefile(base.support_root / item['path'])
            calls.append({'region': 'private-support', 'root': str(result.support_root)})
        return result
    monkeypatch.setattr(support, 'WHEEL_PROVIDER', lambda: execution_inputs)
    monkeypatch.setattr(wheel, 'reuse_for_test', observe)
    collector.test_worker_wheel_is_reused_with_immutable_support_and_private_manifests(
        tmp_path, admission=admission)
    assert len(calls) == 1
    gate('released-' + initialized)
    p.write_observation(ROOT / 'execution.json', p.canonical({'commands': calls}))
    p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""
