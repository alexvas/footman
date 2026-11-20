"""P18 separate policy provisioning probes through existing callers and O."""

SOURCE = r"""
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from tests import mcp_provenance as p, mcp_wheel_support as wheel
from tests import host_acceptance_contract_support as h
from tests import test_host_acceptance_contracts as collector
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
    raise AssertionError('policy gate exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, execution_inputs, tmp_path, monkeypatch):
    # The scenario session already loaded explicit plugins. Its real policy
    # grandchild must use the same fresh-venv plugin discovery as normal callers.
    monkeypatch.delenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', raising=False)
    reuse = wheel.reuse_for_test
    clones = []
    def observe(base, destination, **kwargs):
        assert base is execution_inputs and kwargs['admission'] is admission.shared_pool
        result = reuse(base, destination, **kwargs)
        assert result.environment != base.environment
        assert len(result.setup_operations) == len(base.setup_operations) + 2
        clones.append(str(result.environment))
        p.write_observation(ROOT / 'target-ready', b'ready')
        gate('attempt-' + initialized)
        return result
    monkeypatch.setattr(wheel, 'reuse_for_test', observe)
    request = SimpleNamespace(getfixturevalue=lambda name: execution_inputs)
    collector.test_isolated_policy_fixture_executes_with_locked_wheel_dependencies(
        request, tmp_path, 'pass', 0, admission=admission)
    assert clones and p.read(ROOT / ('busy-' + initialized)) == b'busy'
    gate('released-' + initialized)
    operation = p.load(tmp_path / 'pytest-provisioning.json')
    assert operation['status'] == 0
    p.write_observation(ROOT / 'execution.json', p.canonical({'commands': [operation]}))
    p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""


def run_case(tmp_path, tmp_path_factory, monkeypatch, admission, base, case):
    import sys
    import time
    from contextlib import ExitStack

    import pytest

    from tests import admission_execution_support as execution
    from tests import host_acceptance_contract_support as h
    from tests import mcp_wheel_support as wheel
    from tests import test_admission_pool as suite
    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock, module_clock

    if case.startswith("nested"):
        with monkeypatch.context() as patch:
            patch.setattr(execution, "SOURCE", SOURCE)
            suite.test_host_wrapper_uses_session_admission(
                tmp_path, tmp_path_factory, patch, admission, "policy", case
            )
        return
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = suite.RecordingDescriptors(namespace)
    ends, contexts, busy, work, clones, policy_allowances = [], [], [], [], [], []
    clock = ManualClock(origin=100) if case == "elapsed" else None
    holder = None
    primary = (
        KeyboardInterrupt("policy interrupted")
        if "interruption" in case
        else OSError("policy operation")
    )
    release, parent_release = (
        OSError("policy slot close"),
        OSError("policy directory close"),
    )

    class System(suite.RecordingSystem):
        def monotonic(self):
            return super().monotonic() if clock is None else clock.monotonic()

        def sleep(self, seconds):
            if clock is None:
                super().sleep(seconds)

        def try_lock_exclusive(self, fd):
            acquired = super().try_lock_exclusive(fd)
            if not acquired:
                busy.append(True)
                if holder is not None:
                    if clock is not None:
                        clock.advance(7, label="policy provisioning admission wait")
                    holder.__exit__(None, None, None)
            return acquired

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            ends.append(deadline)
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(directory=namespace, capacity=1, system=System(descriptors=ops))
    reuse, native = wheel.reuse_for_test, wheel.command
    cross_argv = [sys.executable, "-c", "print('cross-policy')"]
    with ExitStack() as stack:
        patch = stack.enter_context(monkeypatch.context())
        if clock is not None:
            stack.enter_context(module_clock(wheel, clock))
            stack.enter_context(module_clock(h, clock))

        def observe(base, destination, **kwargs):
            nonlocal holder
            assert kwargs["admission"] is pool
            result = reuse(base, destination, **kwargs)
            suite.assert_released(ops)
            assert len(ends) == 1
            clones.append(result)
            if clock is not None:
                clock.advance(
                    20, label="completed clone precedes separate policy envelope"
                )
            if case in {"direct", "elapsed", "repeat"}:
                holder = pool.acquire(deadline=1000 if clock else time.monotonic() + 5)
                stack.enter_context(holder)
            if case.startswith(("metadata", "flock")):
                ops.point = case.split("-")[0]
                ops.primary = primary
            if case == "sole-close" or case.startswith(("metadata", "flock", "body")):
                ops.release, ops.parent_release = release, parent_release
            return result

        def command(argv, **kwargs):
            if argv[1:3] == ["-m", "pytest"]:
                policy_allowances.append(kwargs["timeout_seconds"])
            if argv[:3] == ["uv", "pip", "install"]:
                assert len(clones) == 1
                assert any(token[2] == "slot" for token in ops.live.values())
                work.append(kwargs["timeout_seconds"])
                if case.startswith("body"):
                    raise primary
                if case == "cross-execution":
                    with pytest.raises(TimeoutError):
                        h.prepared_command(
                            cross_argv,
                            admission=pool,
                            cwd=tmp_path,
                            env=wheel.clean_environment(),
                            deadline=time.monotonic() + 0.2,
                        )
                    assert busy
            return native(argv, **kwargs)

        patch.setattr(wheel, "reuse_for_test", observe)
        patch.setattr(wheel, "command", command)
        fault = case == "sole-close" or case.startswith(("metadata", "flock", "body"))
        if fault:
            with pytest.raises(BaseException) as caught:
                h.standalone_policy(base, tmp_path, "pass", admission=pool)
            assert caught.value is (release if case == "sole-close" else primary)
            secondary = getattr(caught.value, "_transaction_secondary", ())
            assert any(error is parent_release for error in secondary)
            if case != "sole-close":
                assert any(error is release for error in secondary)
            assert not (tmp_path / "pytest-provisioning.json").exists()
            assert not (tmp_path / "isolated-policy-operation.json").exists()
            if case.startswith(("metadata", "flock")):
                assert not work
        else:
            result, _, bound = h.standalone_policy(
                base, tmp_path, "pass", admission=pool
            )
            assert result["status"] == 0 and bound is clones[0]
            assert len(work) == 1
            if case in {"direct", "elapsed", "repeat", "cross-execution"}:
                assert busy
            if case == "elapsed":
                assert ends == [220, 1000, 240] and work == [113]
                assert policy_allowances == [113]
                assert clock is not None and clock.monotonic() == 127
            if case == "cross-execution":
                suite.assert_released(ops)
                progress = h.prepared_command(
                    cross_argv,
                    admission=pool,
                    cwd=tmp_path,
                    env=wheel.clean_environment(),
                    deadline=time.monotonic() + 5,
                )
                assert (
                    progress["status"] == 0 and progress["stdout"] == b"cross-policy\n"
                )
    suite.assert_released(ops)
    before = list(ops.closed)
    for context in contexts:
        context.__exit__(None, None, None)
        context.__exit__(None, None, None)
    assert ops.closed == before
    ops.point = ops.primary = ops.release = ops.parent_release = None
    with pool.acquire(deadline=time.monotonic() + 5):
        pass
    suite.assert_released(ops)
