"""P17 real-clone probes and collector/provider scenario."""

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
    raise AssertionError('clone gate exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, execution_inputs, tmp_path, monkeypatch):
    original = wheel.reuse_for_test
    calls = []
    def observe(base, destination, **kwargs):
        assert base is execution_inputs
        assert kwargs['admission'] is admission.shared_pool
        p.write_observation(ROOT / 'target-ready', b'ready')
        gate('attempt-' + initialized)
        result = original(base, destination, **kwargs)
        assert p.read(ROOT / ('busy-' + initialized)) == b'busy'
        assert result.environment != base.environment
        assert len(result.setup_operations) == len(base.setup_operations) + 2
        # Native wheel.command refuses incomplete/failed cleanup before returning.
        assert all(op['status'] == 0 for op in result.setup_operations)
        calls.append({'region': 'clone', 'environment': str(result.environment)})
        return result
    monkeypatch.setattr(support, 'WHEEL_PROVIDER', lambda: execution_inputs)
    monkeypatch.setattr(wheel, 'reuse_for_test', observe)
    collector.test_mutable_installation_clone_has_private_inodes_and_relocated_launcher(
        tmp_path, admission=admission)
    assert len(calls) == 1
    gate('released-' + initialized)
    p.write_observation(ROOT / 'execution.json', p.canonical({'commands': calls}))
    p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""


def run_case(
    tmp_path,
    tmp_path_factory,
    monkeypatch,
    admission,
    base,
    case,
    *,
    private_support=False,
):
    import time
    from contextlib import ExitStack
    from typing import cast

    import pytest

    from tests import admission_execution_support as execution
    from tests import host_acceptance_contract_support as h
    from tests import mcp_provenance as p
    from tests import mcp_wheel_support as wheel
    from tests import test_admission_pool as suite
    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock, module_clock

    if case.startswith("nested"):
        with monkeypatch.context() as patch:
            if private_support:
                from tests.admission_private_support import SOURCE as private_source

                patch.setattr(execution, "SOURCE", private_source)
            else:
                patch.setattr(execution, "SOURCE", SOURCE)
            suite.test_host_wrapper_uses_session_admission(
                tmp_path, tmp_path_factory, patch, admission, "clone", case
            )
        return
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = suite.RecordingDescriptors(namespace)
    contexts, ends, busy, working, support_work = [], [], [], [], []
    clock = ManualClock(origin=100) if case == "elapsed" else None
    holder = None
    reject_busy = False
    primary = (
        KeyboardInterrupt("clone interrupted")
        if "interruption" in case
        else OSError("clone operation")
    )
    release, parent_release = (
        OSError("clone slot close"),
        OSError("clone directory close"),
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
                if reject_busy:
                    raise primary
                assert holder is not None
                if clock is not None:
                    clock.advance(7, label="clone admission wait")
                holder.__exit__(None, None, None)
            return acquired

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            ends.append(deadline)
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(directory=namespace, capacity=1, system=System(descriptors=ops))
    destination = tmp_path / "clone"
    destination.mkdir()
    native = wheel.command
    native_copy = wheel.copy_support_file
    original_script = p.read(base.environment / "bin/footman")
    package, _ = p.manifest_set(base.expected_origin)
    original_sources = p.sources(package)

    def command(argv, **kwargs):
        if argv[0] == "cp":
            assert any(token[2] == "slot" for token in ops.live.values())
            working.append(kwargs["timeout_seconds"])
            if case.startswith("body"):
                raise primary
        return native(argv, **kwargs)

    def copy_support(source, target):
        assert any(token[2] == "slot" for token in ops.live.values())
        support_work.append(clock.monotonic() if clock else time.monotonic())
        if case.startswith("body"):
            raise primary
        return native_copy(source, target)

    def clone(selected=pool):
        if case == "forwarding":
            result, _, copied = h.standalone_policy(
                base, destination, "pass", admission=selected
            )
            assert result["status"] == 0
            return copied
        return wheel.reuse_for_test(
            base,
            destination,
            admission=selected,
            mutable=not private_support,
            private_support=private_support,
        )

    with ExitStack() as stack:
        patch = stack.enter_context(monkeypatch.context())
        patch.setattr(wheel, "command", command)
        if private_support:
            patch.setattr(wheel, "copy_support_file", copy_support)
        if case == "forwarding":
            reuse = wheel.reuse_for_test
            forwarded = []

            def observe(base, destination, **kwargs):
                assert kwargs["admission"] is pool
                forwarded.append(True)
                return reuse(base, destination, **kwargs)

            patch.setattr(wheel, "reuse_for_test", observe)
        if clock is not None:
            stack.enter_context(module_clock(wheel, clock))
        if case == "missing":
            with pytest.raises(
                ValueError, match="clone reuse requires an AdmissionPool"
            ):
                clone(cast(AdmissionPool, None))
            assert (
                not ops.events
                and not working
                and not support_work
                and not (destination / "venv").exists()
            )
            return
        if case in {"direct", "elapsed", "repeat"}:
            holder = pool.acquire(deadline=1000 if clock else time.monotonic() + 5)
            stack.enter_context(holder)
        if case == "cross-preparation":
            reject_busy = True
            preparation = tmp_path / "preparation"
            preparation.mkdir()

            def prepare_command(argv, **kwargs):
                if not busy:
                    with pytest.raises(OSError) as caught:
                        clone()
                    assert (
                        caught.value is primary
                        and busy
                        and not working
                        and not support_work
                    )
                return native(argv, **kwargs)

            with monkeypatch.context() as cross:
                cross.setattr(wheel, "command", prepare_command)
                root, revision = h.prepared_root(preparation, admission=pool)
            assert root.is_dir() and len(revision) == 40
            suite.assert_released(ops)
            reject_busy = False
            destination = tmp_path / "fresh-clone"
            destination.mkdir()
        if case.startswith(("metadata", "flock")):
            ops.point = case.split("-")[0]
            ops.primary = primary
        fault = case == "sole-close" or case.startswith(("metadata", "flock", "body"))
        if fault:
            ops.release, ops.parent_release = release, parent_release
            with pytest.raises(BaseException) as caught:
                clone()
            assert caught.value is (release if case == "sole-close" else primary)
            secondary = getattr(caught.value, "_transaction_secondary", ())
            assert any(error is parent_release for error in secondary)
            if case != "sole-close":
                assert any(error is release for error in secondary)
            if case.startswith(("metadata", "flock")):
                assert not working and not support_work
        else:
            result = clone()
            if private_support:
                assert result.environment == base.environment
                assert result.setup_operations == base.setup_operations and not working
                assert result.support_root != base.support_root
                assert len(support_work) == len(base.support_inventory)
                for item in base.support_inventory:
                    target = result.support_root / item["path"]
                    source = base.support_root / item["path"]
                    assert p.read(target) == p.read(source) and not target.samefile(
                        source
                    )
            copied, _ = p.manifest_set(result.expected_origin)
            if not private_support:
                assert result.environment != base.environment
                assert copied.is_relative_to(result.environment)
                assert not (copied / "startup.py").samefile(package / "startup.py")
                assert p.read(result.environment / "bin/footman").startswith(
                    b"#!" + str(result.environment / "bin/python").encode() + b"\n"
                )
                assert len(result.setup_operations) == len(base.setup_operations) + 2
            # Native wheel.command refuses incomplete/failed cleanup before returning.
            assert all(op["status"] == 0 for op in result.setup_operations)
            assert len(working) == (0 if private_support else 1)
            if case == "forwarding":
                assert forwarded == [True] and len(ends) == 2
            if case in {"direct", "elapsed", "repeat", "cross-preparation"}:
                assert busy
            if case == "elapsed":
                assert ends == [1000, 220]
                if private_support:
                    assert support_work == [107] * len(base.support_inventory)
                else:
                    assert working == [113]
                assert clock is not None and clock.monotonic() == 107
    assert p.read(base.environment / "bin/footman") == original_script
    assert p.sources(package) == original_sources
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
