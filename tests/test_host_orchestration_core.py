"""Task-11.4 core-only branch faults, always in complete prepared copies.

No real Docker, build/install or later host suite is executed here. Independent
full host contracts remain registered in test_host_acceptance_contracts.py.
"""

import ast
import importlib.util
import json
import os
import sys
from importlib._bootstrap_external import _code_to_timestamp_pyc
from pathlib import Path

import pytest

from tests import host_acceptance_assertions as assertions
from tests import host_acceptance_contract_support as h
from tests import host_orchestration_support as core
from tests import mcp_attribution_support as attribution
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel


def isolated(prepared, program, bootstrap_setup=""):
    prefix = f"""
import importlib.util
import json
import os
from pathlib import Path
import sys
path = Path({str(prepared["root"] / "scripts/verify_host_acceptance.py")!r})
spec = importlib.util.spec_from_file_location('_core_branch_test', path)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
before = list(sys.path)
{bootstrap_setup}
c = m._Core(m._bootstrap())
"""

    def snapshot():
        return {
            "prepared_root": str(prepared["root"]),
            "synthetic_revision": prepared["profile"]["synthetic_revision"],
            "candidate_revision": prepared["profile"]["candidate_revision"],
            "source": h.inventory(prepared["root"]),
            "build_input": wheel.inventory(prepared["root"]),
            "dist": attribution.dist_identity(prepared["root"]),
            "candidate": core.snapshot_candidate(),
            "simulation": True,
        }

    before = snapshot()
    p.write(prepared["tmp"] / "branch-profile-before.json", p.canonical(before))

    def account():
        after = snapshot()
        p.write(prepared["tmp"] / "branch-profile-after.json", p.canonical(after))
        assert after == before
        assert after["candidate"] == prepared["profile"]["candidate_before"]

    with core.accounting_scope(account):
        result = wheel.command(
            [sys.executable, "-P", "-c", prefix + program],
            cwd=prepared["foreign"],
            env=wheel.clean_environment(),
            timeout_seconds=60,
        )
        p.write(
            prepared["tmp"] / "branch-operation.json",
            p.canonical(h.operation_record(result)),
        )
    assert result["status"] == 0, result
    return result


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "message",
        "node",
        "setup",
        "teardown",
        "xfail",
        "collection",
        "internal",
        "interrupt",
        "cleanup",
        "duplicate",
        "malformed",
        "missing",
    ],
)
def test_structured_negative_diagnostic_does_not_depend_on_traceback_format(
    tmp_path, fault
):
    prepared = core.prepare(tmp_path)
    value = {
        "schema_version": 1,
        "collected": 1,
        "exitstatus": 1,
        "collection_errors": [],
        "internal_errors": [],
        "interrupted": False,
        "cleanup_complete": True,
        "reports": [
            {
                "nodeid": "host_tests/test_negative_expectations.py::test_negative_expectations",
                "when": when,
                "outcome": "failed" if when == "call" else "passed",
                "wasxfail": False,
                "longrepr": "formatted traceback" if when == "call" else "",
                "message": "AssertionError: configured argv mismatch"
                if when == "call"
                else None,
            }
            for when in ("setup", "call", "teardown")
        ],
    }
    if fault == "message":
        value["reports"][1]["message"] = "AssertionError: unrelated failure"
    elif fault == "node":
        value["reports"][1]["nodeid"] = "another_test"
    elif fault in {"setup", "teardown"}:
        value["reports"][0 if fault == "setup" else 2]["outcome"] = "failed"
    elif fault == "xfail":
        value["reports"][1]["wasxfail"] = True
    elif fault in {"collection", "internal"}:
        value[fault + "_errors"] = ["failure"]
    elif fault == "interrupt":
        value["interrupted"] = True
    elif fault == "cleanup":
        value["cleanup_complete"] = False
    marker = b"FIXTURE_OUTCOMES=" + json.dumps(value).encode() + b"\n"
    if fault == "duplicate":
        marker *= 2
    elif fault == "malformed":
        marker = b"FIXTURE_OUTCOMES={bad-json}\n"
    elif fault == "missing":
        marker = b""
    # A bare diagnostic cannot rescue a missing or malformed observation.
    stdout = b"configured argv mismatch\nE   AssertionError: configured argv mismatch\n"
    isolated(
        prepared,
        f"""
assert m._negative_assertion(({stdout!r}, {marker!r}), b'configured argv mismatch') is {fault is None!r}
""",
    )


@pytest.mark.parametrize("substitute,timeout", [(True, 90), (False, 180)])
def test_outer_envelope_distinguishes_real_integration_from_core_doubles(
    tmp_path, monkeypatch, substitute, timeout
):
    prepared = core.prepare(tmp_path)
    original = wheel.command
    calls = []
    refused = RuntimeError("observed outer launch boundary; no child acquired")

    def command(argv, **kwargs):
        if argv[0] == sys.executable and "-c" in argv:
            calls.append(kwargs["timeout_seconds"])
            raise refused
        return original(argv, **kwargs)

    monkeypatch.setattr(wheel, "command", command)
    with pytest.raises(RuntimeError) as caught:
        core.invoke(prepared, substitute=substitute)
    assert caught.value is refused
    assert calls == [timeout]
    assert core.snapshot_candidate() == prepared["profile"]["candidate_before"]


def test_negative_detection_vocabulary_matches_host_fixture_and_probe(tmp_path):
    expected = assertions.EXPECTATION_DIAGNOSTICS
    assert tuple(expected) == assertions.EXPECTATION_CASES == core.CASES
    probe_maps = [
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(core.PROBE))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "diagnostics"
            for target in node.targets
        )
    ]
    assert len(probe_maps) == 1
    assert list(probe_maps[0].items()) == list(expected.items())
    prepared = core.prepare(tmp_path)
    isolated(
        prepared,
        f"""
assert m._CASES == {tuple(expected)!r}
assert list(m._DIAGNOSTICS.items()) == {list(expected.items())!r}
""",
    )


@pytest.mark.parametrize("case_id", ["unknown", ""])
@pytest.mark.parametrize("status", [0, 1])
def test_unknown_negative_case_is_an_invalid_leg_not_a_detection(
    tmp_path, case_id, status
):
    prepared = core.prepare(tmp_path)
    isolated(
        prepared,
        f"""
from types import SimpleNamespace
external = Path({str(tmp_path)!r})
leg = SimpleNamespace(case_id={case_id!r}, evidence=external, workspace=external,
    profile='negative', suite_paths=(external / 'synthetic_suite.py',))
stdout = b'FIXTURE_BASELINE_VERIFIED\\nstaged bytes mismatch\\n'
result = SimpleNamespace(stdout=stdout, stderr=b'synthetic stderr',
    counts={{'collected':1, 'passed':{1 - status}, 'failed':{status}, 'skipped':0, 'xfailed':0, 'xpassed':0}},
    pytest_status={status}, failure_kind={"assertion" if status else None!r},
    cleanup_complete=True, evidence_complete=True)
record, accepted = m._leg_result(c, leg, result)
assert not accepted and not record['detected']
assert record['failure_kind'] == 'invalid-observation/setup'
assert record['case_id'] == {case_id!r} and record['pytest_status'] == {status}
assert c.read(Path(record['stdout_path'])) == stdout
assert c.read(Path(record['stderr_path'])) == b'synthetic stderr'
""",
    )


@pytest.mark.sleepy
@pytest.mark.parametrize("branch", ["read", "write", "inventory", "publish", "mkdir"])
@pytest.mark.parametrize(
    "fault", ["operation", "operation-close", "close", "interrupt", "interrupt-close"]
)
def test_core_sole_owner_branch_failure_matrix(tmp_path, branch, fault):
    prepared = core.prepare(tmp_path)
    # All resources are in the prepared root or external fixture evidence.
    isolated(
        prepared,
        f"""
from footman.filesystem.operations import PosixDescriptorOps
from footman.filesystem.descriptors import OwnedDescriptor
branch, fault = {branch!r}, {fault!r}
external = Path({str(tmp_path)!r})
input_path = c.root / 'README.md'
output_path = external / 'branch-output'
report_dir = external / 'branch-report'
report_dir.mkdir()
operation = KeyboardInterrupt('primary interruption') if fault.startswith('interrupt') else ValueError('primary operation')
release = OSError('sole close')
closed = []
operation_started = False
class Ops(PosixDescriptorOps):
    def close(self, fd):
        closed.append(fd)
        super().close(fd)
        if 'close' in fault and (operation_started or fault == 'close'):
            raise release
c.ops = Ops()
original_read, original_write, original_replace = os.read, os.write, os.replace
original_mkdirat = Ops.mkdirat
if fault != 'close':
    def fail(*args, **kwargs):
        global operation_started
        operation_started = True
        raise operation
    if branch in ('read', 'inventory'):
        os.read = fail
    elif branch == 'write':
        os.write = fail
    elif branch == 'publish':
        os.replace = fail
    else:
        Ops.mkdirat = fail
caught = None
try:
    if branch == 'read':
        c.read(input_path)
    elif branch == 'write':
        c.write(output_path, b'evidence')
    elif branch == 'inventory':
        c.inventory(m._BUILD_ROOTS)
    elif branch == 'publish':
        c.publish(report_dir, {{'state':'incomplete'}})
    else:
        c.mkdir(external / 'new-directory')
except BaseException as error:
    caught = error
finally:
    os.read, os.write, os.replace = original_read, original_write, original_replace
    Ops.mkdirat = original_mkdirat
assert caught is not None
if fault != 'close':
    assert operation_started and caught is operation
    if 'close' in fault:
        assert release in operation._transaction_secondary
elif fault == 'close':
    assert caught is release
assert closed
# Repeat observation/release uses the same owner and never retries a close.
base_ops = PosixDescriptorOps()
flags, label = os.O_RDONLY, 'repeat branch'
ops = c.ops
fd = os.open(input_path, flags)
owner = OwnedDescriptor(ops, fd, label=label)
try:
    owner.close()
except OSError:
    pass
count = len(closed)
try:
    owner.close()
except OSError:
    pass
assert len(closed) == count
print('branch matrix settled')
""",
    )


@pytest.mark.parametrize(
    "mode",
    ["success", "missing", "symlink", "spec", "exec", "side-effect", "wrong-origin"],
)
def test_by_path_loader_preflight_and_source_attribution(tmp_path, mode):
    prepared = core.prepare(tmp_path)
    runner = prepared["root"] / "scripts/run_host_acceptance.py"
    source = b"def _execute_prepared_host_leg(leg):\n    return leg\n"
    if mode == "exec":
        source = b"raise RuntimeError\n"
    elif mode == "side-effect":
        source = b"open('/DO_NOT_ACQUIRE', 'w')\n"
    elif mode == "wrong-origin":
        source = b"from os import getpid as _execute_prepared_host_leg\n"
    h.replace(runner, source)
    if mode == "missing":
        runner.unlink()
    elif mode == "symlink":
        runner.unlink()
        runner.symlink_to(tmp_path / "absent")
    isolated(
        prepared,
        f"""
mode = {mode!r}
if mode == 'spec':
    m.importlib.util.spec_from_file_location = lambda *a: None
observed = {{}}
paths, environment, cwd = list(sys.path), dict(os.environ), os.getcwd()
try:
    loaded = m._load_host_runner(c, observed)
except Exception as error:
    assert mode != 'success', repr(error)
    print(type(error).__name__, str(error))
else:
    assert mode == 'success'
    assert loaded._execute_prepared_host_leg('sentinel') == 'sentinel'
    assert observed['module_path'] == str(c.root / 'scripts/run_host_acceptance.py')
    assert observed['spec_origin'] == observed['module_path']
    assert observed['source_sha256'] == m.hashlib.sha256(c.read(Path(observed['module_path']))).hexdigest()
assert paths == sys.path and environment == dict(os.environ) and cwd == os.getcwd()
assert not Path('/DO_NOT_ACQUIRE').exists()
""",
    )


def test_by_path_loader_ignores_timestamp_valid_stale_bytecode(tmp_path):
    prepared = core.prepare(tmp_path)
    runner = prepared["root"] / "scripts/run_host_acceptance.py"
    stale = b'def _execute_prepared_host_leg(leg):\n    return "cached"\n'
    source = stale.replace(b'"cached"', b'"source"')
    assert len(source) == len(stale)
    h.replace(runner, stale)
    stamp = runner.stat()
    cache = Path(importlib.util.cache_from_source(str(runner)))
    cache.parent.mkdir(exist_ok=True)
    bytecode = bytes(
        _code_to_timestamp_pyc(
            compile(stale, str(runner), "exec"), int(stamp.st_mtime), len(stale)
        )
    )
    p.write(cache, bytecode)
    h.replace(runner, source)
    # A same-size edit within the timestamp granularity is valid to the
    # standard timestamp-pyc loader, despite containing different code.
    os.utime(runner, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    p.write(
        prepared["tmp"] / "stale-cache.json",
        p.canonical(
            {
                "source": source.decode(),
                "stale_source": stale.decode(),
                "cache": str(cache),
                "header": bytecode[:16].hex(),
                "size": runner.stat().st_size,
                "mtime_ns": runner.stat().st_mtime_ns,
            }
        ),
    )
    isolated(
        prepared,
        f"""
runner = c.root / 'scripts/run_host_acceptance.py'
cache = Path({str(cache)!r})
header = c.read(cache)[:16]
assert header[:4] == m.importlib.util.MAGIC_NUMBER
assert int.from_bytes(header[4:8], 'little') == 0
assert int.from_bytes(header[8:12], 'little') == int(runner.stat().st_mtime) & 0xffffffff
assert int.from_bytes(header[12:16], 'little') == runner.stat().st_size
for _ in range(2):
    observed = {{}}
    loaded = m._load_host_runner(c, observed)
    assert loaded._execute_prepared_host_leg(None) == 'source'
    assert observed['source_sha256'] == m.hashlib.sha256({source!r}).hexdigest()
assert c.read(cache) == {bytecode!r}
""",
    )


@pytest.mark.sleepy
@pytest.mark.parametrize(
    "declaration",
    [
        "def helper(value=acquire()): pass",
        "def helper(*, value=acquire()): pass",
        "async def helper(value=acquire()): pass",
        "async def helper(*, value=acquire()): pass",
        "@acquire\ndef helper(): pass",
        "@acquire()\ndef helper(): pass",
        "@acquire\nasync def helper(): pass",
        "@acquire\nclass Helper: pass",
        "@acquire()\nclass Helper: pass",
        "class Helper(acquire()): pass",
        "class Helper(metaclass=acquire()): pass",
        "class Helper(metaclass=acquire): pass",
        "class Parent:\n    def __init_subclass__(cls): acquire()\nclass Helper(Parent): pass",
        "if __name__ == '__main__':\n    pass\nelse:\n    acquire()",
        "if __name__ == '__main__':\n    pass\nelse:\n    def helper(value=acquire()): pass",
        "raise acquire()",
    ],
)
def test_by_path_loader_refuses_declaration_side_effects_before_exec(
    tmp_path, declaration
):
    prepared = core.prepare(tmp_path)
    runner = prepared["root"] / "scripts/run_host_acceptance.py"
    source = (
        "import builtins\n"
        "def acquire(*args, **kwargs):\n"
        "    return builtins._footman_loader_side_effect(*args, **kwargs)\n"
        + declaration
        + "\n"
        "def _execute_prepared_host_leg(leg):\n    return leg\n"
    )
    h.replace(runner, source.encode())
    isolated(
        prepared,
        """
import builtins
assert not hasattr(builtins, '_footman_loader_side_effect')
effects = []
def side_effect(*args, **kwargs):
    effects.append((args, kwargs))
    return object
builtins._footman_loader_side_effect = side_effect
paths, env, cwd = list(sys.path), dict(os.environ), os.getcwd()
previous = sys.modules.get('_footman_host_runner')
try:
    for _ in range(2):
        try:
            m._load_host_runner(c)
        except ValueError as error:
            assert 'runner import' in str(error)
        else:
            raise AssertionError('unsafe declaration was executed')
        assert effects == [], effects
        assert sys.modules.get('_footman_host_runner') is previous
        assert paths == sys.path and env == dict(os.environ) and cwd == os.getcwd()
finally:
    del builtins._footman_loader_side_effect
""",
    )


def test_by_path_loader_accepts_inert_defaults_plain_class_and_guard_else(tmp_path):
    prepared = core.prepare(tmp_path)
    h.replace(
        prepared["root"] / "scripts/run_host_acceptance.py",
        b"""
class Plain:
    def method(self, value=3, *, label="safe"):
        return value, label
async def helper(value=None):
    return value
if __name__ == '__main__':
    raise RuntimeError('CLI must not execute')
else:
    VALUE = 7
def _execute_prepared_host_leg(leg, value=3, *, label="safe"):
    return leg, value, label, VALUE
""",
    )
    isolated(
        prepared,
        """
loaded = m._load_host_runner(c)
assert loaded._execute_prepared_host_leg('leg') == ('leg', 3, 'safe', 7)
assert loaded.Plain().method() == (3, 'safe')
""",
    )


def test_owner_leaf_operation_close_and_independent_release_identity(tmp_path):
    prepared = core.prepare(tmp_path)
    isolated(
        prepared,
        """
from footman.filesystem.operations import PosixDescriptorOps
from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.cleanup import CleanupFailures
primary, release = ValueError('operation'), OSError('release')
closed = []
class Ops(PosixDescriptorOps):
    def close(self, fd):
        closed.append(fd)
        super().close(fd)
        raise release
c.ops = Ops()
with c.Directory.open_secure_path(PosixDescriptorOps(), c.root) as parent:
    original = os.read
    def fail(fd, count):
        raise primary
    os.read = fail
    try:
        c.read_child(parent, 'README.md')
    except Exception as error:
        assert error is primary
    finally:
        os.read = original
assert len(closed) == 1
# Independent release faults do not stop sibling release; repeats use owners.
label, flags = 'independent cleanup', os.O_RDONLY
ops, input_path = c.ops, c.root / 'README.md'
fd = os.open(input_path, flags)
first = OwnedDescriptor(ops, fd, label=label)
fd = os.open(input_path, flags)
second = OwnedDescriptor(ops, fd, label=label)
failures = CleanupFailures(primary)
failures.run(first.close, ordinary=(Exception,))
failures.run(second.close, ordinary=(Exception,))
assert failures.complete() is None
assert release in primary._transaction_secondary
assert len(closed) == 3
repeated = CleanupFailures(primary)
repeated.run(first.close, ordinary=(Exception,))
repeated.run(second.close, ordinary=(Exception,))
assert repeated.complete() is None
assert len(closed) == 3
""",
    )


def test_core_inventory_matches_independent_prepared_profile(tmp_path):
    prepared = core.prepare(tmp_path)
    result = isolated(
        prepared,
        """
print(json.dumps({'source':c.inventory(m._SOURCE_ROOTS), 'build':c.inventory(m._BUILD_ROOTS)}))
""",
    )
    observed = json.loads(result["stdout"])
    assert observed["source"] == [
        prepared["profile"]["source_identity"],
        prepared["profile"]["source_inventory"],
    ]
    assert observed["build"] == [
        prepared["profile"]["build_input_identity"],
        prepared["profile"]["build_input_inventory"],
    ]


@pytest.mark.parametrize("interrupt", [False, True])
def test_independent_stream_retention_preserves_primary(tmp_path, interrupt):
    prepared = core.prepare(tmp_path)
    isolated(
        prepared,
        f"""
external = Path({str(tmp_path)!r})
primary = KeyboardInterrupt('stream interrupted') if {interrupt!r} else OSError('stream write')
original = os.write
attempts = []
def write(fd, payload):
    path = os.readlink('/proc/self/fd/' + str(fd))
    attempts.append(path)
    if path.endswith('stdout.txt'):
        raise primary
    return original(fd, payload)
os.write = write
try:
    c.retain_streams(external, '', b'stdout', b'stderr')
except BaseException as error:
    assert error is primary
else:
    raise AssertionError('stream failure hidden')
finally:
    os.write = original
assert c.read(external / 'stderr.txt') == b'stderr'
assert sum(path.endswith('stdout.txt') for path in attempts) == 1
assert sum(path.endswith('stderr.txt') for path in attempts) == 1
""",
    )


def test_finalization_failure_does_not_replace_first_raw_status(tmp_path):
    prepared = core.prepare(tmp_path)
    setup = """
original_replace = os.replace
publications = []
def replace(src, dst, *args, **kwargs):
    if str(dst) == 'report.json':
        publications.append(str(dst))
        if len(publications) == 2:
            raise OSError('final report persistence failure')
    return original_replace(src, dst, *args, **kwargs)
os.replace = replace
"""
    from tests.host_results_support import ResultsLedger

    evidence = tmp_path / "reconciliation"
    evidence.mkdir()
    ledger = ResultsLedger(prepared["base"], evidence)
    with core.accounting_scope(lambda: core.reconcile_reservations(prepared, ledger)):
        result, observed, _, names = core.invoke(
            prepared,
            outcomes={"argv": {"status": 17, "failure_kind": "prerequisite"}},
            probe_setup=setup,
        )
        assert result["status"] == observed["call_status"] == 17
        assert len(names) == 1
        report = p.load(prepared["base"] / names[0] / "report.json")
        assert report["state"] == "incomplete" and report["success"] is False
        assert b"final report persistence failure" in result["stderr"]
    assert p.load(evidence / "reconciled.json")["complete"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "success",
        "launch",
        "operation-close",
        "sink-close",
        "close",
        "interrupt-close",
        "incomplete",
        "read-failed",
    ],
)
@pytest.mark.parametrize("retain_files", [False, True])
def test_process_boundary_has_one_owner_collection_and_failure_authority(
    tmp_path, fault, retain_files
):
    prepared = core.prepare(tmp_path)
    isolated(
        prepared,
        f"""
from types import SimpleNamespace
from footman.filesystem.cleanup import attach_secondary
fault = {fault!r}
primary = KeyboardInterrupt('child interruption') if fault == 'interrupt-close' else OSError('child operation')
release = OSError('child release')
calls = {{'launch':0, 'stream':0, 'exit':0}}
class Child:
    returncode = 19
    def __enter__(self):
        return self
    def stream(self, chunk, on_stream_end):
        calls['stream'] += 1
        chunk(c.Stream.STDOUT, b'out\\x00\\xff')
        chunk(c.Stream.STDERR, b'err\\x80')
        if fault == 'sink-close':
            class BadPayload:
                def __iter__(self):
                    yield 65
                    raise primary
            chunk(c.Stream.STDOUT, BadPayload())
        if fault in ('operation-close', 'interrupt-close'):
            raise primary
        for stream in c.Stream:
            on_stream_end(stream, c.Outcome.READ_FAILED if fault == 'read-failed' else c.Outcome.EOF)
        return SimpleNamespace(returncode=19, timed_out=False,
            lifecycle=SimpleNamespace(complete=fault != 'incomplete', failures=[]))
    def __exit__(self, kind, error, tb):
        calls['exit'] += 1
        if fault in ('operation-close', 'interrupt-close', 'sink-close', 'close'):
            if error is not None:
                attach_secondary(error, [release])
            else:
                raise release
class Process:
    @staticmethod
    def launch(argv, **kwargs):
        calls['launch'] += 1
        assert kwargs['capture_limit'] == 0
        assert kwargs.get('stdin_mode', 'closed') == 'closed'
        assert kwargs['popen_kwargs']['cwd'] == str(c.root)
        assert 'PYTHONPATH' not in kwargs['popen_kwargs']['env']
        if fault == 'launch':
            raise primary
        return Child()
c.Process = Process
record = {{'commands': []}}
external = Path({str(tmp_path)!r})
try:
    result = c.command(['inert-child'], external if {retain_files!r} else None, 'child', record)
except BaseException as error:
    assert fault != 'success'
    saved = error._host_command_evidence
    assert saved['record']['status'] == (None if fault == 'launch' else 19)
    assert saved['stdout'] == (b'' if fault == 'launch' else b'out\\x00\\xff')
    assert saved['stderr'] == (b'' if fault == 'launch' else b'err\\x80')
    if fault in ('launch', 'operation-close', 'interrupt-close', 'sink-close'):
        assert error is primary
        if fault != 'launch':
            assert release in primary._transaction_secondary
    elif fault == 'close':
        assert error is release
else:
    assert fault == 'success'
    assert result == (19, b'out\\x00\\xff', b'err\\x80')
assert calls == {{'launch':1,'stream':0 if fault == 'launch' else 1,'exit':0 if fault == 'launch' else 1}}
assert len(record['commands']) == (1 if {retain_files!r} else 0)
if {retain_files!r}:
    entry = record['commands'][0]
    assert entry['status'] == (None if fault == 'launch' else 19)
    assert entry['cleanup_complete'] is (fault == 'success')
    assert c.read(Path(entry['stdout_path'])) == (b'' if fault == 'launch' else b'out\\x00\\xff')
    assert c.read(Path(entry['stderr_path'])) == (b'' if fault == 'launch' else b'err\\x80')
""",
    )


def test_bootstrap_promotes_existing_source_over_earlier_poison(tmp_path):
    prepared = core.prepare(tmp_path)
    poison = tmp_path / "ambient"
    package = poison / "footman"
    package.mkdir(parents=True)
    p.write(
        package / "__init__.py", b"raise AssertionError('ambient Footman executed')\n"
    )
    isolated(
        prepared,
        """
assert sys.path[0] == str(c.root / 'src')
assert sys.path.count(str(c.root / 'src')) == 1
for name, module in tuple(sys.modules.items()):
    if name == 'footman' or name.startswith('footman.'):
        assert Path(module.__file__).is_relative_to(c.root / 'src')
        assert module.__spec__.origin == module.__file__
""",
        bootstrap_setup=f"sys.path[:0] = [{str(poison)!r}, str(m.Path(m.__file__).resolve().parents[1] / 'src')]",
    )


def accounted_invoke(prepared, check, **kwargs):
    from tests.host_results_support import ResultsLedger

    evidence = prepared["tmp"] / "review-reconciliation"
    evidence.mkdir()
    ledger = ResultsLedger(prepared["base"], evidence)
    with core.accounting_scope(lambda: core.reconcile_reservations(prepared, ledger)):
        check(*core.invoke(prepared, **kwargs))
    assert p.load(evidence / "reconciled.json")["complete"] is True


@pytest.mark.parametrize("stage", ["inspect", "build"])
@pytest.mark.parametrize("fault", ["stream", "close"])
@pytest.mark.parametrize("status", [0, 23])
def test_command_failure_retains_output_status_and_first_status(
    tmp_path, stage, fault, status
):
    prepared = core.prepare(tmp_path, **{stage + "_status": status})
    control = p.load(prepared["control"])
    setup = f"""
from footman.process import OwnedProcess
original_stream = OwnedProcess.stream
original_exit = OwnedProcess.__exit__
stream_calls = 0
def stream(self, *args, **kwargs):
    global stream_calls
    stream_calls += 1
    result = original_stream(self, *args, **kwargs)
    if stream_calls == {3 if stage == "inspect" else 4} and {fault!r} == 'stream':
        raise OSError('after actual collection')
    return result
def exit(self, *args):
    result = original_exit(self, *args)
    if stream_calls == {3 if stage == "inspect" else 4} and {fault!r} == 'close':
        raise OSError('after actual owner cleanup')
    return result
OwnedProcess.stream = stream
OwnedProcess.__exit__ = exit
"""

    def check(result, observed, calls, names):
        assert result["status"] == observed["call_status"] == (status or 1)
        report = p.load(prepared["base"] / names[0] / "report.json")
        assert report["success"] is False
        assert report["first_unexpected_status"] == (status or 1)
        command = report["commands"][-1]
        assert command["status"] == status and command["cleanup_complete"] is False
        assert command["error"] is not None
        for stream in ("stdout", "stderr"):
            assert p.read(command[stream + "_path"]) == bytes.fromhex(
                control[stage][stream + "_hex"]
            )

    accounted_invoke(prepared, check, probe_setup=setup)


@pytest.mark.sleepy
@pytest.mark.parametrize(
    "case,stage",
    [
        (case, stage)
        for case in ("argv", "normal")
        for stage in (
            "leg-directory",
            "workspace-directory",
            "host-directory",
            "evidence-directory",
            "expectation-input",
        )
        if case != "normal" or stage != "expectation-input"
    ],
)
@pytest.mark.parametrize(
    "fault", ["operation", "close", "operation-close", "interrupt-close"]
)
@pytest.mark.parametrize("earlier_status", [None, 17])
def test_preparation_failure_is_isolated_and_finalized(
    tmp_path, case, stage, fault, earlier_status
):
    prepared = core.prepare(tmp_path)
    outcomes = (
        {}
        if earlier_status is None
        else {
            "staged_bytes": {"status": earlier_status, "failure_kind": "prerequisite"}
        }
    )
    suffix = {
        "leg-directory": (),
        "workspace-directory": ("workspace",),
        "host-directory": ("workspace", "host_tests"),
        "evidence-directory": ("evidence",),
        "expectation-input": ("workspace", "host_tests", "expectation_input.json"),
    }[stage]
    setup = f"""
preparation_mkdir, original_write, original_close = os.mkdir, os.write, os.close
active_target = None
operation_failed = False
close_failed = False
def target(path):
    try:
        parts = path.relative_to(Path(BASE)).parts
    except ValueError:
        return False
    return len(parts) >= 2 and parts[1] == {case!r} and parts[2:] == {suffix!r}
def fail_operation():
    global operation_failed
    operation_failed = True
    if {fault!r} == 'interrupt-close':
        raise KeyboardInterrupt('preparation interrupted')
    raise OSError('preparation operation failed')
def mkdir(path, *args, **kwargs):
    global active_target
    parent = Path(os.readlink('/proc/self/fd/' + str(kwargs['dir_fd']))) if kwargs.get('dir_fd') is not None else Path.cwd()
    absolute = Path(path) if Path(path).is_absolute() else parent / path
    if target(absolute):
        active_target = absolute
        if {fault!r} != 'close':
            fail_operation()
    return preparation_mkdir(path, *args, **kwargs)
def write(fd, data):
    global active_target
    absolute = Path(os.readlink('/proc/self/fd/' + str(fd)))
    if target(absolute):
        active_target = absolute
        if {fault!r} != 'close':
            fail_operation()
    return original_write(fd, data)
def close(fd):
    global close_failed
    absolute = Path(os.readlink('/proc/self/fd/' + str(fd)))
    selected = False
    if active_target is not None and not close_failed and {fault!r} != 'operation':
        close_target = active_target.parent if operation_failed and {stage!r} != 'expectation-input' else active_target
        selected = absolute == close_target
    original_close(fd)
    if selected:
        close_failed = True
        raise OSError('preparation owner close failed')
os.mkdir, os.write, os.close = mkdir, write, close
"""

    def check(result, observed, calls, names):
        interrupt = fault == "interrupt-close"
        selected_case = None if case == "normal" else case
        all_cases = [*core.CASES, None]
        prior_cases = all_cases[: all_cases.index(selected_case)]
        expected_cases = (
            prior_cases
            if interrupt
            else [item for item in all_cases if item != selected_case]
        )
        assert [leg["case_id"] for leg in observed["legs"]] == expected_cases
        assert result["status"] != 0
        if interrupt:
            assert observed["error"]["type"] == "KeyboardInterrupt"
        else:
            assert result["status"] == observed["call_status"] == (earlier_status or 1)
        run = prepared["base"] / names[0]
        report = p.load(run / "report.json")
        assert report["state"] == ("incomplete" if interrupt else "final")
        assert report["success"] is False
        assert report["first_unexpected_status"] == (earlier_status or 1)
        expected_preparations = (
            [*prior_cases, selected_case] if interrupt else all_cases
        )
        assert [
            item["case_id"] for item in report["preparation_outcomes"]
        ] == expected_preparations
        failures = [
            item
            for item in report["preparation_outcomes"]
            if item["outcome"] == "failed"
        ]
        assert len(failures) == 1
        failure = failures[0]
        assert failure["case_id"] == selected_case and failure["stage"] == stage
        expected_error = (
            "KeyboardInterrupt"
            if interrupt
            else "DescriptorError"
            if stage != "expectation-input"
            and fault in ("operation", "operation-close")
            else "OSError"
        )
        assert failure["exception_type"] == expected_error
        expected_diagnostic = (
            "preparation interrupted"
            if interrupt
            else "preparation owner close failed"
            if fault == "close"
            else "preparation operation failed"
        )
        if expected_error == "DescriptorError":
            expected_diagnostic = "cannot create directory " + repr(
                suffix[-1] if suffix else case
            )
        assert expected_diagnostic in failure["diagnostic"]
        assert failure["workspace"] == str(run / case / "workspace")
        assert "pytest_status" not in failure
        assert [call["case_id"] for call in report["call_outcomes"]] == expected_cases
        assert all(call["outcome"] == "returned" for call in report["call_outcomes"])
        for leg in report["legs"]:
            if leg["case_id"] != "staged_bytes" or earlier_status is None:
                assert leg["accepted"] is True
            assert leg["evidence_retention"] == "complete"
            assert p.read(leg["stdout_path"]) and p.read(leg["stderr_path"])
        reconciliation = report["resource_reconciliation"]
        assert reconciliation["preparation_attempts"] == len(expected_preparations)
        assert reconciliation["preparation_failures"] == 1
        assert reconciliation["prepared_calls"] == len(expected_cases)
        assert reconciliation["complete"] is False

    accounted_invoke(prepared, check, outcomes=outcomes, probe_setup=setup)


def test_multiple_preparation_failures_do_not_prevent_other_legs(tmp_path):
    prepared = core.prepare(tmp_path)
    setup = """
preparation_mkdir, original_write = module._Core.mkdir, module._Core.write
def mkdir(self, path):
    if path.name == 'workspace' and path.parent.name == 'argv':
        raise OSError('argv workspace unavailable')
    return preparation_mkdir(self, path)
def write(self, path, data):
    if path.name == 'expectation_input.json' and path.parent.parent.parent.name == 'cleanup':
        raise OSError('cleanup expectation unavailable')
    return original_write(self, path, data)
module._Core.mkdir, module._Core.write = mkdir, write
"""

    def check(result, observed, calls, names):
        assert result["status"] == observed["call_status"] == 1
        assert [leg["case_id"] for leg in observed["legs"]] == [
            "staged_bytes",
            "command_status",
            None,
        ]
        report = p.load(prepared["base"] / names[0] / "report.json")
        assert report["state"] == "final" and report["success"] is False
        assert report["first_unexpected_status"] == 1
        failures = [
            item
            for item in report["preparation_outcomes"]
            if item["outcome"] == "failed"
        ]
        assert [(item["case_id"], item["stage"]) for item in failures] == [
            ("argv", "workspace-directory"),
            ("cleanup", "expectation-input"),
        ]
        assert len(report["preparation_outcomes"]) == 5
        assert len(report["call_outcomes"]) == len(report["legs"]) == 3
        assert all(leg["accepted"] is True for leg in report["legs"])
        assert report["resource_reconciliation"]["preparation_failures"] == 2
        assert report["resource_reconciliation"]["complete"] is False

    accounted_invoke(prepared, check, probe_setup=setup)


@pytest.mark.parametrize("case", ["argv", "normal"])
@pytest.mark.parametrize("fault", ["write", "close"])
@pytest.mark.parametrize("earlier_status", [None, 31])
def test_leg_retention_failure_preserves_returned_status_and_continues(
    tmp_path, case, fault, earlier_status
):
    prepared = core.prepare(tmp_path)
    counts = {
        "collected": 3,
        "passed": 1,
        "failed": 2,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    outcomes = {case: {"status": 17, "counts": counts, "failure_kind": "prerequisite"}}
    if earlier_status is not None:
        outcomes["staged_bytes"] = {
            "status": earlier_status,
            "failure_kind": "prerequisite",
        }
    setup = f"""
original_write, original_close = os.write, os.close
injected = False
def target(fd):
    path = Path(os.readlink('/proc/self/fd/' + str(fd)))
    return path.name == 'stdout.txt' and path.parent.name == 'evidence' and path.parent.parent.name == {case!r}
def write(fd, data):
    global injected
    if {fault!r} == 'write' and not injected and target(fd):
        injected = True
        raise OSError('synthetic leg evidence write failure')
    return original_write(fd, data)
def close(fd):
    global injected
    selected = {fault!r} == 'close' and not injected and target(fd)
    original_close(fd)
    if selected:
        injected = True
        raise OSError('synthetic leg evidence close failure')
os.write, os.close = write, close
"""

    def check(result, observed, calls, names):
        expected = earlier_status or 17
        assert result["status"] == observed["call_status"] == expected
        assert [leg["case_id"] for leg in observed["legs"]] == [*core.CASES, None]
        report = p.load(prepared["base"] / names[0] / "report.json")
        assert report["state"] == "final" and report["success"] is False
        assert report["first_unexpected_status"] == expected
        assert [call["case_id"] for call in report["call_outcomes"]] == [
            *core.CASES,
            None,
        ]
        assert all(call["outcome"] == "returned" for call in report["call_outcomes"])
        selected_case = case if case != "normal" else None
        call = next(
            call for call in report["call_outcomes"] if call["case_id"] == selected_case
        )
        record = next(leg for leg in report["legs"] if leg["case_id"] == selected_case)
        assert call["pytest_status"] == record["pytest_status"] == 17
        assert call["counts"] == record["counts"] == counts
        assert record["failure_kind"] == "prerequisite"
        assert record["cleanup_complete"] is True
        assert record["evidence_complete"] is False and record["accepted"] is False
        assert record["evidence_retention"] == "failed"
        assert record["processing_error"]["stage"] == "evidence-retention"
        assert record["processing_error"]["exception_type"] == "OSError"
        assert p.read(record["stderr_path"]) == b"synthetic boundary diagnostic\x00\xff"
        if fault == "close":
            assert p.read(record["stdout_path"])
        for other in report["legs"]:
            if other is not record and not (
                earlier_status and other["case_id"] == "staged_bytes"
            ):
                assert (
                    other["accepted"] is True
                    and other["evidence_retention"] == "complete"
                )
        assert report["resource_reconciliation"]["prepared_calls"] == 5
        assert report["resource_reconciliation"]["complete"] is False

    accounted_invoke(prepared, check, outcomes=outcomes, probe_setup=setup)


def test_returned_leg_retention_interruption_preserves_status_and_identity(tmp_path):
    prepared = core.prepare(tmp_path)
    setup = """
original_write = os.write
def write(fd, data):
    path = Path(os.readlink('/proc/self/fd/' + str(fd)))
    if path.name == 'stdout.txt' and path.parent.parent.name == 'argv':
        raise KeyboardInterrupt('returned-leg evidence interrupted')
    return original_write(fd, data)
os.write = write
"""

    def check(result, observed, calls, names):
        assert result["status"] != 0
        assert observed["error"]["type"] == "KeyboardInterrupt"
        assert observed["call_status"] is None
        assert [leg["case_id"] for leg in observed["legs"]] == ["staged_bytes", "argv"]
        report = p.load(prepared["base"] / names[0] / "report.json")
        assert report["state"] == "incomplete" and report["success"] is False
        assert report["first_unexpected_status"] == 17
        assert len(report["call_outcomes"]) == 2
        call, record = report["call_outcomes"][-1], report["legs"][-1]
        assert call["outcome"] == "returned" and call["pytest_status"] == 17
        assert call["counts"] == record["counts"]
        assert (
            record["pytest_status"] == 17 and record["evidence_retention"] == "failed"
        )
        assert record["processing_error"]["exception_type"] == "KeyboardInterrupt"
        assert p.read(record["stderr_path"]) == b"synthetic boundary diagnostic\x00\xff"

    accounted_invoke(
        prepared, check, outcomes={"argv": {"status": 17}}, probe_setup=setup
    )


@pytest.mark.parametrize(
    "mode,source,error_type",
    [
        ("syntax", b"def broken(:\n    pass\n", "SyntaxError"),
        (
            "default",
            b"def acquire():\n    raise AssertionError\ndef helper(value=acquire()): pass\n",
            "ValueError",
        ),
        (
            "decorator",
            b"def acquire(value):\n    raise AssertionError\n@acquire\ndef helper(): pass\n",
            "ValueError",
        ),
        (
            "guard-else",
            b"if __name__ == '__main__':\n    pass\nelse:\n    open('/DO_NOT_ACQUIRE', 'w')\n",
            "ValueError",
        ),
        ("exec", b"raise RuntimeError\n", "RuntimeError"),
        (
            "origin",
            b"from os import getpid as _execute_prepared_host_leg\n",
            "AttributeError",
        ),
    ],
)
def test_orchestration_loader_preflight_precedes_all_helpers(
    tmp_path, mode, source, error_type
):
    prepared = core.prepare(tmp_path)
    runner = prepared["root"] / "scripts/run_host_acceptance.py"
    h.replace(runner, source)
    prepared["profile"]["source_identity"] = h.inventory(prepared["root"])[0]
    before = core.results_base_inventory(prepared["base"])

    def check(result, observed, calls, names):
        assert result["status"] != 0
        assert observed["error"]["type"] == error_type
        assert observed["launch_requests"] == []  # includes Git, not only build
        assert observed["exec_requests"] == []
        assert observed["legs"] == [] and calls == []
        assert observed["exclusive_reservations"] == [] and names == []
        assert core.results_base_inventory(prepared["base"]) == before
        assert (
            observed["cwd_restored"]
            and observed["environment_restored"]
            and observed["logging_restored"]
        )
        assert not Path("/DO_NOT_ACQUIRE").exists()

    accounted_invoke(prepared, check)


@pytest.mark.parametrize(
    "fault",
    [
        "fsync",
        "close",
        "readback",
        "mismatch",
        "interrupt",
        "quarantine-close",
        "quarantine-interrupt",
    ],
)
def test_final_report_post_replacement_fault_quarantines_success(tmp_path, fault):
    prepared = core.prepare(tmp_path)
    setup = f"""
original_replace, original_fsync, original_close, original_read = os.replace, os.fsync, os.close, os.read
publications = 0
final_run = None
injected = False
quarantined = False
quarantine_close_attempts = 0
def replace(src, dst, *args, **kwargs):
    global publications, final_run, quarantined
    result = original_replace(src, dst, *args, **kwargs)
    if str(dst) == 'report.json':
        publications += 1
        if publications == 2:
            final_run = Path(os.readlink('/proc/self/fd/' + str(kwargs['dst_dir_fd'])))
    if str(dst).endswith('.failed'):
        assert not quarantined
        quarantined = True
    return result
def fail():
    global injected
    injected = True
    if {fault!r} == 'interrupt':
        raise KeyboardInterrupt('post replace interrupt')
    raise OSError('post replace {fault}')
def fsync(fd):
    if final_run is not None and not injected and Path(os.readlink('/proc/self/fd/' + str(fd))) == final_run and {fault!r} in ('fsync', 'interrupt', 'quarantine-close', 'quarantine-interrupt'):
        fail()
    return original_fsync(fd)
def close(fd):
    global quarantine_close_attempts
    target = Path(os.readlink('/proc/self/fd/' + str(fd)))
    original_close(fd)
    if final_run is not None and not injected and target == final_run and {fault!r} == 'close':
        fail()
    if quarantined and target == final_run:
        quarantine_close_attempts += 1
        assert quarantine_close_attempts == 1
        if {fault!r} == 'quarantine-close':
            raise OSError('independent quarantine owner close')
        if {fault!r} == 'quarantine-interrupt':
            raise KeyboardInterrupt('independent quarantine owner interruption')
def read(fd, count):
    global injected
    if final_run is not None and not injected and Path(os.readlink('/proc/self/fd/' + str(fd))) == final_run / 'report.json':
        if {fault!r} == 'readback':
            fail()
        if {fault!r} == 'mismatch':
            injected = True
            return b'corrupted readback'
    return original_read(fd, count)
os.replace, os.fsync, os.close, os.read = replace, fsync, close, read
"""

    def check(result, observed, calls, names):
        assert result["status"] != 0
        run = prepared["base"] / names[0]
        assert not (run / "report.json").exists()
        failed = [name for name in h.root_names(run) if name.endswith(".failed")]
        assert len(failed) == 1
        # The optimistic bytes are retained for diagnosis, never at the
        # acceptance filename, and never accepted over a failed invocation.
        assert p.load(run / failed[0])["success"] is True

    accounted_invoke(prepared, check, probe_setup=setup)
