"""P16 collector scenario using the bounded P15 child/ownership protocol."""

import base64
from types import ModuleType
from typing import Protocol


class _RunnerFixture(Protocol):
    def __wrapped__(self) -> ModuleType: ...


def commands(operations):
    return [
        {
            "argv": list(op["argv"]),
            "cwd": op["cwd"],
            "status": op["status"],
            "cleanup_complete": op["cleanup_complete"],
            "stdout_base64": base64.b64encode(op["stdout"]).decode("ascii"),
            "stderr_base64": base64.b64encode(op["stderr"]).decode("ascii"),
            "terminals": op["terminals"],
        }
        for op in operations
    ]


SOURCE = r"""
import time
from pathlib import Path
import pytest
from tests import mcp_provenance as p, mcp_wheel_support as wheel
from tests import test_host_workspace as collector
from tests.conftest import _mcp_worker_wheel
from tests.admission_workspace_support import commands

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
    raise AssertionError('workspace gate exhausted: ' + name)

def test_ready(initialized):
    pass

def test_work(admission, initialized, execution_inputs, tmp_path, monkeypatch):
    bound = execution_inputs
    original = wheel.provision_workspace
    calls = []
    def observe(value, artifact, digest, **kwargs):
        assert kwargs['admission'] is admission.shared_pool
        assert artifact == bound.wheel and digest == bound.wheel_sha256
        assert not value.provisioning_started and not value.commands
        p.write_observation(ROOT / 'target-ready', b'ready')
        gate('attempt-' + initialized)
        result = original(value, artifact, digest, **kwargs)
        assert result is None and value.provisioned
        assert len(value.commands) == 2
        assert all(op['status'] == 0 and op['cleanup_complete'] for op in value.commands)
        assert all(owner.cleanup_status.complete for owner in value.owners)
        calls.append({'region': 'workspace-provision', 'native_commands': commands(value.commands)})
        return result
    monkeypatch.setattr(wheel, 'provision_workspace', observe)
    module = collector.runner.__wrapped__()
    collector.test_fresh_wheel_environment_policy_and_copied_imports(
        tmp_path, module, bound, 'pass', 0, monkeypatch, admission=admission)
    assert len(calls) == 1
    gate('released-' + initialized)
    p.write_observation(ROOT / 'execution.json', p.canonical({'commands': calls}))
    p.write_observation(ROOT / ('entered-' + initialized), b'executed')
"""


def run_case(tmp_path, tmp_path_factory, monkeypatch, admission, bound, case):
    import sys
    import time
    from contextlib import ExitStack
    from types import SimpleNamespace
    from typing import cast

    import pytest

    from tests import admission_execution_support as execution
    from tests import host_acceptance_contract_support as h
    from tests import mcp_provenance as p
    from tests import mcp_wheel_support as wheel
    from tests import test_admission_pool as suite
    from tests import test_host_workspace as collector
    from tests.admission_support import AdmissionPool
    from tests.clock_support import ManualClock, module_clock

    if case.startswith("nested"):
        with monkeypatch.context() as patch:
            patch.setattr(execution, "SOURCE", SOURCE)
            suite.test_host_wrapper_uses_session_admission(
                tmp_path, tmp_path_factory, patch, admission, "workspace", case
            )
        return
    module = cast(_RunnerFixture, collector.runner).__wrapped__()
    namespace = tmp_path / "pool"
    namespace.mkdir()
    ops = suite.RecordingDescriptors(namespace)
    clock = ManualClock(origin=100) if case == "elapsed" else None
    contexts, ends, busy = [], [], []
    holder = None

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
                if clock is not None:
                    clock.advance(
                        7, label="workspace admission consumes original envelope"
                    )
                    assert holder is not None
                    holder.__exit__(None, None, None)
            return acquired

    class Pool(AdmissionPool):
        def acquire(self, *, deadline):
            ends.append(deadline)
            context = super().acquire(deadline=deadline)
            contexts.append(context)
            return context

    pool = Pool(directory=namespace, capacity=1, system=System(descriptors=ops))
    with ExitStack() as stack:
        if clock is not None:
            stack.enter_context(module_clock(module, clock))
        root, value = collector.source(tmp_path, module)
        value.prepare()
        original = value.deadline
        primary = (
            KeyboardInterrupt("workspace interrupted")
            if "interruption" in case
            else OSError("workspace operation")
        )
        release, parent_release = (
            OSError("workspace slot release"),
            OSError("workspace directory release"),
        )

        def provision(selected=pool):
            return wheel.provision_workspace(
                value,
                bound.wheel,
                bound.wheel_sha256,
                admission=selected,
                interpreter=sys.executable,
            )

        if case == "missing":
            with pytest.raises(
                ValueError, match="missing shared workspace provisioning admission"
            ):
                provision(cast(AdmissionPool, None))
            assert (
                not ops.events and not value.provisioning_started and not value.commands
            )
            return
        if case in {"direct", "repeat", "elapsed"}:
            holder = pool.acquire(deadline=original)
            with holder:
                if case == "elapsed":
                    plans = []

                    def launch(argv, **kwargs):
                        plans.append(kwargs)
                        raise primary

                    with monkeypatch.context() as patch:
                        patch.setattr(
                            module, "OwnedProcess", SimpleNamespace(launch=launch)
                        )
                        with pytest.raises(OSError) as caught:
                            provision()
                    assert caught.value is primary and value.deadline == original == 220
                    assert len(plans) == 1 and not value.owners
                    assert (
                        plans[0]["deadline_seconds"] + plans[0]["grace_seconds"] == 113
                    )
                    assert clock is not None and clock.monotonic() == 107 and busy
                    assert ends == [220, 220]
                else:
                    value.deadline = time.monotonic() + 0.2
                    original = value.deadline
                    started = time.monotonic()
                    with pytest.raises(TimeoutError):
                        provision()
                    assert time.monotonic() - started < 2 and busy
                    assert (
                        value.deadline == original
                        and not value.provisioning_started
                        and not value.commands
                    )
            suite.assert_released(ops)
            if case == "elapsed":
                return
            external = tmp_path / "fresh"
            external.mkdir()
            value = module._Workspace(root, external).prepare()
            original = value.deadline
        elif case == "cross-preparation":
            directory = tmp_path / "preparation"
            directory.mkdir()
            native = wheel.command

            def command(argv, **kwargs):
                if not busy:
                    value.deadline = time.monotonic() + 0.2
                    with pytest.raises(TimeoutError):
                        provision()
                    assert (
                        busy and not value.provisioning_started and not value.commands
                    )
                return native(argv, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(wheel, "command", command)
                prepared, revision = h.prepared_root(directory, admission=pool)
            assert prepared.is_dir() and len(revision) == 40
            suite.assert_released(ops)
            external = tmp_path / "fresh"
            external.mkdir()
            value = module._Workspace(root, external).prepare()
            original = value.deadline
        if case.startswith(("metadata", "flock")):
            ops.point = case.split("-")[0]
            ops.primary = primary
        fault = case in {
            "sole-close",
            "body-operation-close",
            "body-interruption",
        } or case.startswith(("metadata", "flock"))
        if fault:
            ops.release, ops.parent_release = release, parent_release
            native_command = value._command

            def command(argv):
                assert any(token[2] == "slot" for token in ops.live.values())
                if case.startswith("body"):
                    raise primary
                return native_command(argv)

            with monkeypatch.context() as patch:
                patch.setattr(value, "_command", command)
                with pytest.raises(BaseException) as caught:
                    provision()
            assert caught.value is (release if case == "sole-close" else primary)
            secondary = getattr(caught.value, "_transaction_secondary", ())
            assert any(error is parent_release for error in secondary)
            if case != "sole-close":
                assert any(error is release for error in secondary)
            if case.startswith(("metadata", "flock")):
                assert not value.provisioning_started and not value.commands
        else:
            assert provision() is None and value.provisioned
        assert value.deadline == original and ends[-1] == original
        if value.provisioned:
            assert len(value.commands) == 2
            assert all(
                op["status"] == 0 and op["cleanup_complete"] for op in value.commands
            )
        assert all(owner.cleanup_status.complete for owner in value.owners)
        p.write(
            tmp_path / "workspace-observation.json",
            p.canonical(
                {
                    "case": case,
                    "deadline": value.deadline,
                    "ends": ends,
                    "provisioned": value.provisioned,
                    "commands": commands(value.commands),
                }
            ),
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
