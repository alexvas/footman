"""C05/C11 real-provider timeout/progress guards with controlled O time."""

SOURCE = r"""
from pathlib import Path
import pytest
from tests import host_acceptance_contract_support as h, mcp_provenance as p, mcp_wheel_support as wheel
from tests.mcp_contract_support import prepare_inputs
from tests.clock_support import module_clock
from tests.admission_installation_support import observation

ROOT = Path(__file__).parent
STORAGE = Path('__EXECUTION_ROOT__')
KIND = '__GUARD_KIND__'

@pytest.fixture(scope='session')
def execution_inputs():
    prepared = prepare_inputs(h.ROOT, STORAGE / 'inputs')
    evidence = STORAGE / 'installation'
    evidence.mkdir()
    return prepared, evidence

def gate(name):
    import time
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if (ROOT / name).exists() and p.read(ROOT / name) == b'go':
            return
        time.sleep(0.01)
    raise AssertionError('guard gate exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, execution_inputs, admission_clock):
    prepared, evidence = execution_inputs
    p.write_observation(ROOT / 'target-ready', b'ready')
    gate('attempt-' + initialized)
    with module_clock(wheel, admission_clock), module_clock(h, admission_clock):
        assert admission_clock.monotonic() == 100
        with pytest.raises(TimeoutError):
            if KIND == 'installation':
                wheel.build_and_install_contract_wheel(prepared, admission=admission.shared_pool,
                    evidence_directory=evidence, timeout_seconds=1)
            else:
                h.invoke(h.ROOT, 'build-acceptance-wheel', ['--help'], admission=admission.shared_pool,
                    cwd=STORAGE, evidence=STORAGE, deadline=101)
        assert admission_clock.monotonic() == 101
        assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
        assert not (prepared / 'dist').exists() and not list(evidence.iterdir())
        gate('released-' + initialized)
        bound = wheel.build_and_install_contract_wheel(prepared, admission=admission.shared_pool,
            evidence_directory=evidence, timeout_seconds=15)
        assert bound.command['status'] == 0 and bound.environment.is_dir()
        p.write_observation(ROOT / 'execution.json', p.canonical({'commands': [observation(bound)]}))
        p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""


def run_guard(tmp_path, tmp_path_factory, monkeypatch, admission, kind):
    from tests import admission_execution_support as execution
    from tests import admission_session_support as session
    from tests import test_admission_pool as suite

    conftest = (
        session.CONFTEST.replace(
            "import pytest\n",
            "import pytest\nfrom tests.clock_support import ManualClock\n"
            "CLOCK = ManualClock(origin=100)\n"
            "@pytest.fixture(scope='session')\ndef admission_clock():\n    return CLOCK\n",
        )
        .replace(
            "class ObservedSystem(PosixAdmissionSystem):\n",
            "class ObservedSystem(PosixAdmissionSystem):\n"
            "        def monotonic(self):\n            return CLOCK.monotonic()\n",
        )
        .replace(
            "if not acquired and not busy:\n",
            "if not acquired and not busy:\n"
            "                CLOCK.advance(1, label='guard original deadline exhaustion')\n",
        )
    )
    with monkeypatch.context() as patch:
        patch.setattr(session, "CONFTEST", conftest)
        patch.setattr(
            execution, "SOURCE", SOURCE.replace("'__GUARD_KIND__'", repr(kind))
        )
        suite.test_host_wrapper_uses_session_admission(
            tmp_path, tmp_path_factory, patch, admission, kind, "nested"
        )
