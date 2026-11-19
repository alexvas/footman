"""Isolated session inputs; all I/O uses existing sole-owner helpers."""

from pathlib import Path

from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel

CONFTEST = r"""
import json
from pathlib import Path
import pytest
from tests import mcp_provenance as p
from tests.admission_support import AdmissionPool, PosixAdmissionSystem, SessionAdmission

ROOT = Path(__file__).parent
CONFIG = None
SEEN = set()

def emit(value):
    print('\nADMISSION_EVENT ' + json.dumps(value, sort_keys=True), flush=True)

def pytest_configure(config):
    global CONFIG
    CONFIG = config

def pytest_xdist_auto_num_workers(config):
    return 3

@pytest.hookimpl(trylast=True)
def pytest_xdist_setupnodes(config, specs):
    emit({'kind': 'planned', 'workers': len(specs)})

@pytest.hookimpl(trylast=True)
def pytest_configure_node(node):
    emit({'kind': 'delivery', 'worker': node.workerinput['workerid'],
          'payload': node.workerinput.get('footman_admission')})

def pytest_testnodedown(node, error):
    emit({'kind': 'down', 'worker': node.workerinput['workerid'], 'error': str(error) if error is not None else None})

@pytest.fixture(scope='session')
def admission(admission, request):
    worker = getattr(request.config, 'workerinput', {}).get('workerid', 'master')
    busy = []
    class ObservedSystem(PosixAdmissionSystem):
        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if not acquired and not busy:
                p.write_observation(ROOT / ('busy-' + worker), b'busy')
                busy.append(True)
            return acquired
    pool = admission.shared_pool
    return SessionAdmission(shared_pool=AdmissionPool(directory=pool.directory, capacity=pool.capacity, system=ObservedSystem()))

@pytest.fixture(scope='session', autouse=True)
def initialized(admission, tmp_path_factory, request):
    worker = getattr(request.config, 'workerinput', {}).get('workerid', 'master')
    pool = admission.shared_pool
    value = {'kind': 'ready', 'worker': worker, 'base': str(tmp_path_factory.getbasetemp()),
             'directory': str(pool.directory), 'capacity': pool.capacity}
    if pool is not admission.shared_pool:
        raise AssertionError('provider must return a stable pool')
    p.write_observation(ROOT / ('ready-' + worker + '.json'), p.canonical(value))
    return worker

def pytest_runtest_logreport(report):
    if hasattr(CONFIG, 'workerinput'):
        return
    worker = report.node.workerinput['workerid'] if hasattr(report, 'node') else 'master'
    if report.when == 'setup' and report.passed and worker not in SEEN:
        SEEN.add(worker)
        emit(json.loads(p.read(ROOT / ('ready-' + worker + '.json'))))
    if report.when == 'call':
        emit({'kind': 'report', 'worker': worker, 'nodeid': report.nodeid, 'outcome': report.outcome})
"""

TRACER = r"""
import os
import time
from pathlib import Path
import pytest
from tests import mcp_provenance as p

ROOT = Path(__file__).parent

def gate(name):
    deadline = time.monotonic() + 5
    while not (ROOT / name).exists():
        if time.monotonic() >= deadline:
            raise AssertionError('handshake deadline exhausted: ' + name)
        time.sleep(0.01)
    assert p.read(ROOT / name) == b'go'

def timeout(admission, initialized):
    gate('attempt-' + initialized)
    with pytest.raises(TimeoutError):
        with admission.shared_pool.acquire(deadline=time.monotonic() + 0.2):
            raise AssertionError('entered saturated session pool')
    assert p.read(ROOT / ('busy-' + initialized)) == b'busy'

def progress(admission, initialized):
    gate('progress-' + initialized)
    with admission.shared_pool.acquire(deadline=time.monotonic() + 5):
        p.write_observation(ROOT / ('entered-' + initialized), b'entered')

def test_ready_initial(initialized):
    pass

def test_timeout_initial(admission, initialized):
    timeout(admission, initialized)

def test_progress_initial(admission, initialized):
    progress(admission, initialized)
"""

REPLACEMENT = r"""
# The initial worker has released its acquisition before this separate item.
def test_crash(initialized):
    assert initialized == 'gw0'
    assert p.read(ROOT / ('entered-' + initialized)) == b'entered'
    os._exit(23)

def test_ready_replacement(initialized):
    assert initialized != 'gw0'

def test_timeout_replacement(admission, initialized):
    timeout(admission, initialized)

def test_progress_replacement(admission, initialized):
    progress(admission, initialized)
"""

MODES = r"""
import time
import pytest

@pytest.mark.parametrize('index', range(6))
def test_mode(index, admission):
    with admission.shared_pool.acquire(deadline=time.monotonic() + 5):
        pass
"""

READY = "def test_ready(initialized):\n    pass\n"

WORK = r"""
import time
from pathlib import Path
from tests import mcp_provenance as p

def test_work(admission):
    with admission.shared_pool.acquire(deadline=time.monotonic() + 5):
        p.write_observation(Path(__file__).parent / 'protected-work', b'entered')
"""


def prepare(root, *, mode, scenario="tracer"):
    repository = Path(__file__).resolve().parents[1]
    workspace = root / "inner"
    workspace.mkdir()
    base = root / "base"
    ini = workspace / "pytest.ini"
    test = workspace / "test_session.py"
    p.write(
        ini, f"[pytest]\naddopts =\ntestpaths =\npythonpath = {repository}\n".encode()
    )
    p.write(workspace / "conftest.py", CONFTEST.encode())
    source = {
        "modes": MODES,
        "ready": READY,
        "tracer": TRACER,
        "replacement": TRACER + REPLACEMENT,
        "work": WORK,
    }[scenario]
    p.write(test, source.encode())
    argv = [
        str(repository / ".venv/bin/python"),
        "-m",
        "pytest",
        "-c",
        str(ini),
        "--rootdir",
        str(workspace),
        "--confcutdir",
        str(workspace),
        "--basetemp",
        str(base),
        "-p",
        "xdist",
        "-p",
        "tests.green_skip_policy",
        "-p",
        "tests.admission_plugin",
        str(test),
        *mode,
        "--dist",
        "worksteal",
        "--tb=short",
        "-q",
    ]
    if scenario == "replacement":
        argv += ["--max-worker-restart", "1"]
    env = wheel.clean_environment()
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSAFEPATH"):
        env.pop(key, None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return {
        "workspace": workspace,
        "base": base,
        "ini": ini,
        "argv": argv,
        "env": env,
        "repository": repository,
    }
