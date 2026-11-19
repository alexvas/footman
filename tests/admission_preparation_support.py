"""Generated P14 caller; actual preparation and provider stay active."""

SOURCE = r"""
import shutil
import time
from pathlib import Path
import pytest
from tests import host_acceptance_contract_support as h, mcp_provenance as p
from tests import mcp_wheel_support as wheel
from tests.admission_installation_support import commands

ROOT = Path(__file__).parent
STORAGE = Path('__PREPARATION_ROOT__')

@pytest.fixture(scope='session')
def preparation_inputs():
    for name in (*h.SURFACES, *h.IMPLEMENTATIONS):
        if not (h.ROOT / 'scripts' / name).is_file():
            raise AssertionError('missing preparation interface: ' + name)
    if shutil.which('git') is None:
        raise AssertionError('missing native Git prerequisite')

def gate(name):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (ROOT / name).exists():
            value = p.read(ROOT / name)
            assert b'go'.startswith(value)
            if value == b'go':
                return
        time.sleep(0.01)
    raise AssertionError('preparation handshake exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, monkeypatch):
    gate('attempt-' + initialized)
    original = wheel.command
    completed = []
    def command(argv, **kwargs):
        gate('released-' + initialized)
        assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
        value = original(argv, **kwargs)
        completed.append(value)
        return value
    with monkeypatch.context() as patch:
        patch.setattr(wheel, 'command', command)
        root, revision = h.prepared_root(STORAGE, admission=admission.shared_pool)
    assert root == STORAGE / 'prepared' and len(revision) == 40
    assert not (root / '.git/index').exists()
    assert p.read(root / '.git/HEAD') == b'ref: refs/heads/fixture\n'
    assert len(completed) == 7 and all(op['status'] == 0 for op in completed)
    p.write_observation(ROOT / 'preparation.json', p.canonical({
        'root': str(root), 'revision': revision, 'commands': commands(completed)}))
    p.write_observation(ROOT / ('entered-' + initialized), b'prepared')
"""
