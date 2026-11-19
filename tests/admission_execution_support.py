"""P15 generated actual-collector wiring; no private execution replacements."""

SOURCE = r"""
import time
from pathlib import Path
import pytest
from tests import host_acceptance_contract_support as h, mcp_provenance as p
from tests.admission_installation_support import commands

ROOT = Path(__file__).parent
STORAGE = Path('__EXECUTION_ROOT__')
KIND = '__EXECUTION_KIND__'

@pytest.fixture(scope='session')
def execution_inputs():
    for name in (*h.SURFACES, *h.IMPLEMENTATIONS):
        if not (h.ROOT / 'scripts' / name).is_file():
            raise AssertionError('missing execution interface: ' + name)

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
    raise AssertionError('execution handshake exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, monkeypatch):
    target = 'invoke' if KIND == 'invoke' else 'prepared_command'
    original = getattr(h, target)
    completed = []
    def observed(*args, **kwargs):
        p.write_observation(ROOT / 'target-ready', b'ready')
        gate('attempt-' + initialized)
        assert kwargs.get('admission') is admission.shared_pool, 'collector must forward its real shared pool'
        value = original(*args, **kwargs)
        completed.append(value)
        return value
    with monkeypatch.context() as patch:
        patch.setattr(h, target, observed)
        if KIND == 'invoke':
            from tests.test_host_acceptance_contracts import test_bootstrap_help_works_without_checkout_venv_from_unrelated_cwd as collector
            collector(admission, STORAGE, 'build-acceptance-wheel')
        else:
            from tests.test_host_build_tooling import test_real_sibling_loader_calls_prepared_function_without_cli_or_global_leak as collector
            collector(admission, STORAGE, 'normal')
    assert len(completed) == 1 and completed[0]['status'] == 0
    assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
    gate('released-' + initialized)
    p.write_observation(ROOT / 'execution.json', p.canonical({'kind': KIND, 'commands': commands(completed)}))
    p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""

FINISHED_HOOK = """if hasattr(CONFIG, 'workerinput'):
        if report.when == 'call' and report.nodeid.endswith('::test_work'):
            p.write_observation(ROOT / 'call-finished', b'done')
        return"""
