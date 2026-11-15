"""Prepared-root RED drivers for the future single-file orchestrator.

Only the named prepared-leg boundary is doubled. Docker inspection and the
controlled build run as real OwnedProcess children against recorded inert tools.
Their synthetic wheel bytes are never installed. File/process ownership is
entirely delegated to existing tested support primitives.
"""

import hashlib
import json
import os
import shlex
import shutil
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor
from tests import host_acceptance_contract_support as h
from tests import host_expectation_support as expectations
from tests import mcp_attribution_support as attribution
from tests import mcp_contract_support as service
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel

IMAGE = "sha256:" + "a" * 64
TAG = "footman-sandbox:latest"
CASES = ("staged_bytes", "argv", "command_status", "cleanup")


@contextmanager
def accounting_scope(account):
    """Always account durable receipts, preserving an active failure."""
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(account, ordinary=(Exception,))
        selected = failures.complete()
        if selected is not None and selected is not primary:
            raise selected


def new_reservation_journal(prepared, directory):
    directory.mkdir()
    prepared.setdefault("reservation_journals", []).append(directory)
    return directory


def reservation_receipts(prepared, start=0):
    receipts = []
    for directory in prepared.get("reservation_journals", [])[start:]:
        for name in h.root_names(directory):
            value = p.load(directory / name)
            h.check(
                value.get("operation") == "exclusive-mkdir-success",
                "invalid reservation journal",
            )
            h.check(
                all(type(value.get(key)) is int for key in ("device", "inode")),
                "invalid receipt identity",
            )
            receipts.append(value)
    return receipts


def account_reservations(prepared, ledger, start=0):
    failures = CleanupFailures(None)

    def consume(path):
        receipt = p.load(path)
        h.check(
            receipt.get("operation") == "exclusive-mkdir-success",
            "invalid reservation journal",
        )
        if Path(receipt["path"]) == ledger.base:
            h.check(
                (receipt["device"], receipt["inode"]) == ledger.base_identity,
                "created base receipt changed",
            )
        else:
            ledger.claim_receipt(receipt)

    for directory in prepared.get("reservation_journals", [])[start:]:
        for name in h.root_names(directory):
            failures.run(
                lambda path=directory / name: consume(path), ordinary=(Exception,)
            )
    selected = failures.complete()
    if selected is not None:
        raise selected


def reconcile_reservations(prepared, ledger, start=0):
    if ledger.reconciliation_failure is not None:
        raise ledger.reconciliation_failure
    failures = CleanupFailures(None)
    if not ledger.attempted:
        failures.run(
            lambda: account_reservations(prepared, ledger, start), ordinary=(Exception,)
        )
    failures.run(ledger.finish, ordinary=(Exception,))
    selected = failures.complete()
    if selected is not None:
        ledger.reconciliation_failure = selected
        raise selected


# Loaded by a normal, isolated interpreter's .pth processing. No checkout
# import roots, test modules or Footman imports are supplied by this observer.
# Only after the script has itself imported the foundation may we borrow its
# already loaded capability classes to own receipt-writing descriptors.
DIRECT_RECORDER = """import json
import os
from pathlib import Path
import stat
import sys
import uuid


def install(base, journal):
    base = Path(base)
    journal = Path(journal)
    mkdir = os.mkdir

    def recorded(path, mode=0o777, *, dir_fd=None):
        result = mkdir(path, mode, dir_fd=dir_fd)
        descriptors = sys.modules.get("footman.filesystem.descriptors")
        operations = sys.modules.get("footman.filesystem.operations")
        if descriptors is None or operations is None:
            # Observation must not establish the script's missing bootstrap.
            # Such a path remains unknown and cannot be automatically deleted.
            return result
        if dir_fd is None:
            absolute = Path(os.path.abspath(path))
        else:
            absolute = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
        if absolute != base and absolute.parent != base:
            return result
        ops = operations.PosixDescriptorOps()
        with descriptors.DirectoryDescriptor.open_secure_path(ops, absolute.parent) as parent:
            info = parent.stat_child(absolute.name, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("reserved directory changed type")
            data = json.dumps({"operation": "exclusive-mkdir-success",
                "path": str(absolute), "device": info.st_dev, "inode": info.st_ino},
                sort_keys=True, separators=(",", ":")).encode()
            with descriptors.DirectoryDescriptor.open_secure_path(ops, journal) as output:
                name = output.child_basename(uuid.uuid4().hex + ".json")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                label = "direct reservation receipt"
                fd = os.open(name, flags, 0o600, dir_fd=output.fd)
                with descriptors.OwnedDescriptor(ops, fd, label=label) as owner:
                    remaining = memoryview(data)
                    while remaining:
                        count = os.write(owner.fd, remaining)
                        if count <= 0:
                            raise OSError("receipt write made no progress")
                        remaining = remaining[count:]
                    os.fsync(owner.fd)
                os.fsync(output.fd)
        return result

    os.mkdir = recorded
"""


def prepare_direct_recorder(prepared, evidence, environment):
    """Observe genuine shebang execution without supplying bootstrap authority."""
    journal = new_reservation_journal(prepared, evidence / "reservations")
    interpreter = evidence / "observer-python"
    original_path = prepared.setdefault("_direct_tool_path", environment["PATH"])
    python = shutil.which("python3.14", path=original_path)
    h.check(python is not None, "isolated Python prerequisite missing")
    setup = wheel.command(
        [python, "-I", "-m", "venv", "--without-pip", interpreter],
        cwd=prepared["foreign"],
        env=environment,
        timeout_seconds=30,
    )
    p.write(evidence / "interpreter-setup.json", p.canonical(h.operation_record(setup)))
    h.check(setup["status"] == 0, "isolated observer interpreter setup failed")
    site = interpreter / "lib/python3.14/site-packages"
    # site can process a venv's .pth files twice. Install in the module body,
    # not in the .pth statement: ordinary import caching makes this once-only.
    module = (
        DIRECT_RECORDER + f"\ninstall({str(prepared['base'])!r}, {str(journal)!r})\n"
    )
    p.write(site / "_reservation_observer.py", module.encode())
    p.write(site / "reservation-observer.pth", b"import _reservation_observer\n")
    environment["PATH"] = str(interpreter / "bin") + os.pathsep + original_path


def require_direct_success(value):
    h.check(value["status"] == 0, f"direct executable failed: {value['stderr']!r}")


def prepare_default_results_base(base):
    """Shared driver setup; local readiness calls do not redirect production."""
    with DirectoryDescriptor.open_secure_path(p.OPS, base.parent) as parent:
        try:
            parent.stat_child(base.name)
        except FileNotFoundError:
            with parent.create_directory(base.name, mode=0o700) as created_base:
                info = os.fstat(created_base.fd)
                return info.st_dev, info.st_ino
    return None


def isolated_environment(prepared, tmp):
    """Explicit approved tools only; no ambient Docker/PATH fallback."""
    bootstrap = h.isolated_path(tmp, uv=False)
    return wheel.clean_environment(
        {"PATH": str(prepared["tools"]) + os.pathsep + str(bootstrap)}
    )


def check_synthetic_leg_report(report):
    """Core-double evidence only, never acceptance of actual host results."""
    h.check(report.get("state") == "final", "synthetic report is not final")
    h.check(report.get("success") is True, "synthetic report is not successful")
    h.check(report["first_unexpected_status"] is None, "unexpected synthetic failure")
    legs = report.get("legs")
    h.check(isinstance(legs, list) and len(legs) == 5, "incomplete synthetic leg set")
    for index, (leg, case) in enumerate(zip(legs, (*CASES, None), strict=True)):
        h.check(isinstance(leg, dict), "invalid synthetic leg")
        h.check(leg["case_id"] == case, "wrong synthetic case order")
        status = leg["pytest_status"]
        h.check(
            type(status) is int and status == (0 if case is None else 1),
            "wrong raw synthetic pytest status",
        )
        counts = leg["counts"]
        expected = {
            "collected": 2 if case is None else 1,
            "passed": 2 if case is None else 0,
            "failed": 0 if case is None else 1,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
        }
        h.check(
            isinstance(counts, dict)
            and counts == expected
            and all(type(count) is int for count in counts.values()),
            f"wrong synthetic counts for leg {index}",
        )
        if case is not None:
            h.check(leg["detected"] is True, "synthetic mismatch was not detected")


def snapshot_candidate():
    index = os.fsdecode(h.git(h.ROOT, "rev-parse", "--git-path", "index")).rstrip("\n")
    if not os.path.isabs(index):
        index = os.path.join(h.ROOT, index)
    return {
        "repository_state": {
            "revision": h.git(h.ROOT, "rev-parse", "HEAD").decode().strip(),
            "refs_sha256": hashlib.sha256(h.git(h.ROOT, "show-ref")).hexdigest(),
            "index_sha256": hashlib.sha256(p.read(index)).hexdigest(),
        },
        "source": h.inventory(
            h.ROOT, ("src/", "tests/", "scripts/", "pyproject.toml", "uv.lock")
        )[0],
        "build_input": wheel.inventory(h.ROOT)[0],
        "dist": attribution.dist_identity(h.ROOT),
    }


def tool_wrapper(path, root, control, mode):
    # This is an explicitly recorded *test-tool* copied-support bootstrap in a
    # separate child, never the orchestrator bootstrap or an installed service.
    program = (
        "import sys; "
        f"sys.path.insert(0, {str(root)!r}); "
        "from tests.host_orchestration_tool import main; "
        f"raise SystemExit(main({mode!r}, {str(control)!r}))"
    )
    data = (
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(root / 'src'))} "
        f'exec {shlex.quote(sys.executable)} -P -c {shlex.quote(program)} "$@"\n'
    ).encode()
    p.write(path, data)
    path.chmod(0o700)


def prepare(
    tmp,
    *,
    fixture_source=None,
    inspect_status=0,
    inspect_output=None,
    build_status=0,
    **faults,
):
    """Missing real orchestrator is RED, outside fixture/setup rejection."""
    if fixture_source is None:
        script = h.required_surface("verify_host_acceptance.py")
        source = p.read(script)
    else:
        # Only fixture-readiness tests supply inert source, never candidate RED
        # execution or evidence of delivered orchestrator behavior.
        source = fixture_source
    candidate = snapshot_candidate()
    root = service.prepare_inputs(h.ROOT, tmp / "prepared")
    (root / "tests").mkdir()
    (root / "host_tests").mkdir()
    p.write(root / "host_tests/__init__.py", b'"""Case-owned host profile marker."""\n')
    service.copy_tree(h.ROOT / "scripts", root / "scripts")
    path = root / "scripts/verify_host_acceptance.py"
    if path.exists():
        h.replace(path, source)
    else:
        p.write(path, source)
    path.chmod(0o700)
    support = (*expectations.ASSERTION_SUPPORT, "tests/host_orchestration_tool.py")
    for relative in support:
        p.write(root / relative, p.read(h.ROOT / relative))
    p.write(root / "uv.lock", p.read(h.ROOT / "uv.lock"))
    # No dependency on the later real runner/build implementation. These are
    # labelled disposable loader/build doubles in the complete prepared profile.
    for name in ("run_host_acceptance.py", "build_acceptance_wheel.py"):
        target = root / "scripts" / name
        data = b'"""Core-only inert tooling double; no import-time acquisitions."""\n'
        if name.startswith("run_"):
            data += b'def _execute_prepared_host_leg(*args, **kwargs):\n    raise RuntimeError("EXECUTION_BOUNDARY_NOT_SUBSTITUTED")\n'
        if target.exists():
            h.replace(target, data)
        else:
            p.write(target, data)
    controls = tmp / "controls"
    controls.mkdir()
    (controls / "calls").mkdir()
    tools = tmp / "tools"
    tools.mkdir()
    control_path = controls / "input.json"
    build_wrapper = root / "scripts/build-acceptance-wheel"
    if build_wrapper.exists():
        build_wrapper.unlink()  # Fixture-owned copy, never candidate artifacts.
    tool_wrapper(build_wrapper, root, control_path, "build")
    tool_wrapper(tools / "docker", root, control_path, "docker")
    # The core checks uv availability but the build boundary double never runs
    # uv or installs anything. A real tool path satisfies that prerequisite.
    (tools / "uv").symlink_to(shutil.which("uv") or "/missing-uv")
    (root / "dist").mkdir()
    p.write(root / "dist/unrelated-stale.whl", b"never select this wheel")
    revision = h.synthetic_head(root, tmp)
    source_identity, inventory = h.inventory(root)
    build_input, build_inventory = wheel.inventory(root)
    manifest = {
        "schema_version": 1,
        "repository_root": str(root),
        "wrapper_path": str(build_wrapper),
        "caller_cwd": str(root),
        "revision": revision,
        "local_changes": True,
        "source_identity": source_identity,
        "source_inventory": inventory,
        "build_input_identity": build_input,
        "build_input_inventory": build_inventory,
        "wheel_path": str(root / "dist/footman-0.1.0-py3-none-any.whl"),
        "wheel_sha256": hashlib.sha256(b"wheel").hexdigest(),
        "sdist_path": str(root / "dist/footman-0.1.0.tar.gz"),
        "sdist_sha256": hashlib.sha256(b"sdist").hexdigest(),
    }
    control = {
        "tag": TAG,
        "inspect": {
            "status": inspect_status,
            "stdout_hex": (
                (IMAGE + "\n").encode() if inspect_output is None else inspect_output
            ).hex(),
            "stderr_hex": b"inspection diagnostic\x00\xff\n".hex(),
        },
        "build": {
            "status": build_status,
            "stdout_hex": b"synthetic controlled build\x00\xff\n".hex(),
            "stderr_hex": b"synthetic build diagnostic\x80\n".hex(),
        },
        "manifest": manifest,
        **faults,
    }
    p.write(control_path, p.canonical(control))
    base = tmp / "fixture-results"
    base.mkdir()
    foreign = tmp / "foreign-cwd"
    foreign.mkdir()
    profile = {
        "root": str(root),
        "candidate_revision": h.git(h.ROOT, "rev-parse", "HEAD").decode().strip(),
        "synthetic_revision": revision,
        "source_identity": source_identity,
        "source_inventory": inventory,
        "build_input_identity": build_input,
        "build_input_inventory": build_inventory,
        "dist_before": attribution.dist_identity(root),
        "support": list(support),
        "tool_sources": {
            str(path): hashlib.sha256(p.read(path)).hexdigest()
            for path in (build_wrapper, tools / "docker")
        },
        "candidate_before": candidate,
        "simulation": True,
    }
    p.write(tmp / "prepared-profile.json", p.canonical(profile))
    diagnostics = tmp / "probe-invocations"
    diagnostics.mkdir()
    h.check(
        wheel.inventory(root)[0] == wheel.inventory(h.ROOT)[0],
        "prepared common inputs differ",
    )
    h.check(
        h.root_names(root) == sorted((*h.IDENTITY_ROOTS, "README.md", ".git", "dist")),
        "incomplete core prepared profile",
    )
    return {
        "root": root,
        "base": base,
        "foreign": foreign,
        "tools": tools,
        "control": control_path,
        "profile": profile,
        "tmp": tmp,
        "diagnostics": diagnostics,
        "invocations": [],
    }


READINESS_SOURCE = '''#!/usr/bin/env python3.14
"""Inert prepared-leg probe exerciser, NOT the production orchestrator."""
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys

_RESULTS_BASE = None

@dataclass(frozen=True)
class Leg:
    wheel: Path
    source_identity: Path
    image: str
    evidence: Path
    workspace: Path
    profile: str
    suite_paths: tuple
    case_id: str | None

def main(argv):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    spec = importlib.util.spec_from_file_location("_readiness_io", root / "tests/mcp_provenance.py")
    io = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(io)
    for case in ("cleanup", None):
        workspace = _RESULTS_BASE / (case or "normal")
        workspace.mkdir()
        host = workspace / "host_tests"
        host.mkdir()
        if case:
            io.write(host / "expectation_input.json", io.canonical({"schema_version": 1, "case_id": case, "expected_override": False}))
        result = _execute_prepared_leg(Leg(
            root / "dist/inert.whl", root.parent / "controls/input.json",
            "sha256:" + "a" * 64, workspace, workspace,
            "negative" if case else "normal",
            (host / "test_negative_expectations.py",) if case else (host,), case,
        ))
        if result.pytest_status != (1 if case else 0):
            raise RuntimeError("prepared-leg double readiness failed")
    return 0
'''


PROBE = """import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

root = Path(ROOT)
launch_requests = []
exec_requests = []
reservations = []
base_creations = []
support_path = list(sys.path)
sys.path[:0] = [str(root), str(root / "src")]
from tests.host_orchestration_tool import install_reservation_recorder
install_reservation_recorder(BASE, RECEIPTS)
sys.path[:] = support_path
original_mkdir = os.mkdir

def tracked_mkdir(path, mode=0o777, *, dir_fd=None):
    result = original_mkdir(path, mode, dir_fd=dir_fd)
    parent = Path(os.readlink('/proc/self/fd/' + str(dir_fd))) if dir_fd is not None else Path.cwd()
    absolute = Path(path) if Path(path).is_absolute() else parent / path
    if absolute == Path(BASE) or absolute.parent == Path(BASE):
        info = absolute.lstat()
        receipt = {'path': str(absolute), 'device': info.st_dev, 'inode': info.st_ino}
        if absolute == Path(BASE):
            base_creations.append(receipt)
        else:
            reservations.append(receipt)
    return result

os.mkdir = tracked_mkdir

def audit(event, arguments):
    if event == "os.system":
        raise RuntimeError("unowned shell execution is forbidden")
    if event == "os.exec":
        exec_requests.append(str(arguments[0]))
        raise RuntimeError("self-re-exec is forbidden by the core contract")
    if event == "subprocess.Popen":
        argv = list(map(str, arguments[1]))
        launch_requests.append(argv)
        if any(Path(arg).name in {"verify_host_acceptance.py", "run-host-acceptance", "run_host_acceptance.py"} for arg in argv):
            raise RuntimeError("orchestrator self-re-exec/runner CLI subprocess is forbidden")
        if Path(argv[0]).name == "docker":
            environment = arguments[3]
            resolved = None if environment is None else shutil.which(argv[0], path=environment.get("PATH", ""))
            if resolved is None or Path(resolved).resolve() != Path(TOOLS) / "docker":
                raise RuntimeError("real Docker or ambient Docker PATH is forbidden")
            if argv[1:] != ["image", "inspect", "--format", "{{.Id}}", TAG]:
                raise RuntimeError("only one literal image inspection is allowed; no build/pull")

sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location("_core_orchestrator_contract", root / "scripts/verify_host_acceptance.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
before_path = list(sys.path)
if PRELOAD:
    import footman.process
spec.loader.exec_module(module)
if sys.path != before_path:
    raise RuntimeError("orchestrator acquired bootstrap authority at import time")
if PATCH_BASE:
    module._RESULTS_BASE = Path(BASE)
legs = []

def execute(leg):
    if not dataclasses.is_dataclass(leg):
        raise RuntimeError("prepared leg must be a typed dataclass value")
    values = dataclasses.asdict(leg)
    required = {"wheel", "source_identity", "image", "evidence", "workspace", "profile", "suite_paths", "case_id"}
    if not required <= values.keys():
        raise RuntimeError("prepared leg omitted typed execution inputs")
    if values["image"] != IMAGE:
        raise RuntimeError("configured image ID was rewritten")
    workspace = Path(values["workspace"])
    evidence = Path(values["evidence"])
    if not workspace.is_absolute() or not evidence.is_absolute() or not evidence.is_relative_to(Path(BASE)):
        raise RuntimeError("prepared leg is outside its external evidence domain")
    if workspace.is_relative_to(root):
        raise RuntimeError("prepared leg uses source checkout as workspace")
    if any(str(workspace) == item["workspace"] for item in legs):
        raise RuntimeError("prepared legs share mutable workspaces")
    # Import copied sole-owner JSON support by path *after* production bootstrap;
    # this test harness adds no sys.path root or production API.
    io_spec = importlib.util.spec_from_file_location("_core_probe_io", root / "tests/mcp_provenance.py")
    io = importlib.util.module_from_spec(io_spec)
    io_spec.loader.exec_module(io)
    expectation_path = workspace / "host_tests/expectation_input.json"
    case = values["case_id"]
    if values["profile"] == "negative":
        expectation = io.load(expectation_path)
        assertion_spec = importlib.util.spec_from_file_location("_core_probe_assertions", root / "tests/host_acceptance_assertions.py")
        assertions = importlib.util.module_from_spec(assertion_spec)
        assertion_spec.loader.exec_module(assertions)
        assertions.validate_expectation_input(expectation)
        if expectation["case_id"] != case:
            raise RuntimeError("negative fixture input/profile mismatch")
        if list(map(str, values["suite_paths"])) != [str(workspace / "host_tests/test_negative_expectations.py")]:
            raise RuntimeError("negative leg did not select its dedicated suite")
    else:
        expectation = None
        if case is not None or expectation_path.exists():
            raise RuntimeError("negative input leaked into normal execution")
        if not values["suite_paths"] or any("negative" in str(path) for path in values["suite_paths"]):
            raise RuntimeError("normal suite selection omitted or filtered")
    import footman.process
    if Path(footman.process.__file__).resolve() != root / "src/footman/process.py":
        raise RuntimeError("ambient process foundation selected")
    if sys.path.count(str(root / "src")) != 1:
        raise RuntimeError("bootstrap did not insert exactly one root/src")
    if root in map(Path, sys.path) or root / "scripts" in map(Path, sys.path):
        raise RuntimeError("orchestrator added a second import root")
    snapshot = {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}
    snapshot["suite_paths"] = list(map(str, values["suite_paths"]))
    snapshot["expectation"] = expectation
    legs.append(snapshot)
    chosen = OUTCOMES.get(case or "normal", {})
    if chosen.get("raise"):
        if chosen["raise"] == "interrupt":
            raise KeyboardInterrupt("synthetic prepared-leg interruption")
        raise RuntimeError("synthetic prepared-leg exception")
    negative = values["profile"] == "negative"
    diagnostics = {"staged_bytes": "staged bytes mismatch", "argv": "configured argv mismatch", "command_status": "command status mismatch", "cleanup": "container cleanup mismatch"}
    stdout = chosen.get("diagnostic", diagnostics.get(case, "normal suite passed"))
    if negative and chosen.get("baseline_verified", True):
        stdout = "FIXTURE_BASELINE_VERIFIED\\n" + stdout
    return SimpleNamespace(
        pytest_status=chosen.get("status", 1 if negative else 0),
        stdout=(stdout + "\\n").encode(), stderr=b"synthetic boundary diagnostic\\x00\\xff",
        counts=chosen.get("counts", {"collected": 1 if negative else 2, "passed": 0 if negative else 2, "failed": 1 if negative else 0, "skipped": 0, "xfailed": 0, "xpassed": 0}),
        failure_kind=chosen.get("failure_kind", "assertion" if negative else None),
        cleanup_complete=chosen.get("cleanup_complete", True),
        evidence_complete=chosen.get("evidence_complete", True),
    )

if SUBSTITUTE:
    module._execute_prepared_leg = execute
exec(PROBE_SETUP)
before_cwd = os.getcwd()
before_env = dict(os.environ)
status = None
error = None
caught = None
try:
    status = module.main(ARGV)
except BaseException as failure:
    caught = failure
    error = {"type": type(failure).__name__, "message": str(failure)}
finally:
    observation = {"legs": legs, "call_status": status, "error": error,
                   "cwd_restored": os.getcwd() == before_cwd,
                   "environment_restored": dict(os.environ) == before_env,
                   "bootstrap_import_root": str(getattr(module, "bootstrap_import_root", "")),
                   "launch_requests": launch_requests, "exec_requests": exec_requests,
                   "exclusive_reservations": reservations,
                   "exclusive_base_creations": base_creations,
                   "simulation": True}
    print("CORE_PROBE " + json.dumps(observation, sort_keys=True))
if caught is not None:
    raise caught
raise SystemExit(status)
"""


def results_base_inventory(base):
    try:
        base.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    return h.inventory(base.parent, (base.name,))[1]


def invoke(
    prepared,
    *,
    outcomes=None,
    environment=None,
    preload=False,
    substitute=True,
    argv=None,
    patch_base=True,
    probe_setup="",
):
    substitutions = {
        "ROOT": str(prepared["root"]),
        "TOOLS": str(prepared["tools"]),
        "BASE": str(prepared["base"]),
        "IMAGE": IMAGE,
        "TAG": TAG,
        "OUTCOMES": {} if outcomes is None else outcomes,
        "PRELOAD": preload,
        "SUBSTITUTE": substitute,
        "ARGV": [TAG] if argv is None else argv,
        "PATCH_BASE": patch_base,
        "PROBE_SETUP": probe_setup,
    }
    diagnostic = prepared["diagnostics"] / uuid.uuid4().hex
    diagnostic.mkdir(mode=0o700)
    journal = new_reservation_journal(prepared, diagnostic / "reservations")
    substitutions["RECEIPTS"] = str(journal)
    program = (
        "\n".join(f"{name} = {value!r}" for name, value in substitutions.items())
        + "\n"
        + PROBE
    )
    env = wheel.clean_environment(
        {"PATH": str(prepared["tools"]) + os.pathsep + os.environ["PATH"]}
    )
    if environment:
        env.update(environment)
    prepared["invocations"].append(str(diagnostic))
    p.write(
        diagnostic / "profile.json",
        p.canonical(
            {
                "prepared_root": str(prepared["root"]),
                "source": h.inventory(prepared["root"]),
                "build_input": wheel.inventory(prepared["root"]),
                "dist": attribution.dist_identity(prepared["root"]),
                "candidate": snapshot_candidate(),
                "PATH": env.get("PATH"),
                "PYTHONPATH": env.get("PYTHONPATH"),
                "simulation": True,
            }
        ),
    )
    before = results_base_inventory(prepared["base"])
    try:
        result = wheel.command(
            [sys.executable, "-P", "-c", program],
            cwd=prepared["foreign"],
            env=env,
            timeout_seconds=90,
        )
    finally:
        completion = {
            "source": h.inventory(prepared["root"]),
            "build_input": wheel.inventory(prepared["root"]),
            "dist": attribution.dist_identity(prepared["root"]),
            "candidate": snapshot_candidate(),
        }
        p.write(diagnostic / "completion-profile.json", p.canonical(completion))
        h.check(
            completion["candidate"] == prepared["profile"]["candidate_before"],
            "orchestration changed candidate inputs, index, refs or dist",
        )
        h.check(
            completion["source"][0] == prepared["profile"]["source_identity"],
            "prepared source changed during orchestration",
        )
        h.check(
            completion["build_input"][0] == prepared["profile"]["build_input_identity"],
            "prepared build inputs changed",
        )
    p.write(
        diagnostic / "orchestration-operation.json",
        p.canonical(h.operation_record(result)),
    )
    calls = [
        p.load(prepared["control"].parent / "calls" / name)
        for name in h.root_names(prepared["control"].parent / "calls")
    ]
    after = results_base_inventory(prepared["base"])
    names = (
        h.root_names(prepared["base"])
        if prepared["base"].is_dir() and not prepared["base"].is_symlink()
        else []
    )
    records = [
        line.removeprefix(b"CORE_PROBE ")
        for line in result["stdout"].splitlines()
        if line.startswith(b"CORE_PROBE ")
    ]
    h.check(
        len(records) == 1,
        "orchestrator probe did not report a complete observation: " + repr(result),
    )
    observed = json.loads(records[0])
    p.write(
        diagnostic / "results-inventory.json",
        p.canonical(
            {
                "before": before,
                "after": after,
                "exclusive_reservations": observed["exclusive_reservations"],
                "exclusive_base_creations": observed["exclusive_base_creations"],
                "owned_run_paths": [
                    item["path"] for item in observed["exclusive_reservations"]
                ],
                "retained": "test evidence preserved outside run cleanup targets; operator runs remain unowned",
            }
        ),
    )
    return result, observed, calls, names
