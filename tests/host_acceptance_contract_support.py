"""Development-only public-wrapper drivers; never copied into host support.

Existing tested file/copy/process owners perform I/O. The sole direct acquisition
is root_names' DirectoryDescriptor; its failure/interruption/repeat-close matrix
is exercised here. Other helpers delegate to those same owners and introduce no
parallel process/stream owner or cleanup continuation. Scripted build artifacts
are inert and must never cross an installation boundary.
"""

import base64
import hashlib
import os
import shutil
import stat
import time
from pathlib import Path

from footman.filesystem.descriptors import DirectoryDescriptor
from tests import mcp_attribution_support as attribution
from tests import mcp_contract_support as service
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel
from tests.admission_support import AdmissionPool
from tests.dispatcher_harness import write_bytes

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("src", "tests", "host_tests", "scripts")
IDENTITY_ROOTS = (*SOURCE_ROOTS, "pyproject.toml", "uv.lock")
SURFACES = ("build-acceptance-wheel", "run-host-acceptance")
IMPLEMENTATIONS = ("build_acceptance_wheel.py", "run_host_acceptance.py")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def required_surface(name, root=None):
    path = (ROOT if root is None else root) / "scripts" / name
    check(path.is_file(), f"required host interface is not implemented: {path}")
    check(not path.is_symlink(), f"host interface must be a regular file: {path}")
    check(os.access(path, os.X_OK), f"host interface is not executable: {path}")
    return path


def root_names(root):
    with DirectoryDescriptor.open_secure_path(p.OPS, root) as owner:
        return sorted(owner.list_names())


def operation_record(value):
    return {
        **value,
        "stdout": base64.b64encode(value["stdout"]).decode("ascii"),
        "stderr": base64.b64encode(value["stderr"]).decode("ascii"),
    }


def invoke(
    root,
    name,
    args,
    *,
    admission: AdmissionPool,
    cwd,
    evidence,
    env=None,
    label="operation",
    deadline=None,
):
    if not isinstance(admission, AdmissionPool):
        raise ValueError("wrapper execution requires an AdmissionPool")
    deadline = time.monotonic() + 120 if deadline is None else deadline
    wrapper = required_surface(name, root)
    environment = wheel.clean_environment() if env is None else dict(env)
    # Both wrappers may provision/build. Admission waiting consumes this same
    # original envelope; no helper or subprocess receives a renewed 120 seconds.
    with admission.acquire(deadline=deadline):
        value = wheel.command(
            [wrapper, *args],
            cwd=cwd,
            env=environment,
            timeout_seconds=deadline - time.monotonic(),
        )
    p.write(
        evidence / (label + ".json"),
        p.canonical(
            {
                "operation": operation_record(value),
                "PATH": environment.get("PATH"),
                "PYTHONPATH": environment.get("PYTHONPATH"),
                "wrapper": str(wrapper),
                "wrapper_sha256": hashlib.sha256(p.read(wrapper)).hexdigest(),
                "resolved_root": str(root.resolve()),
            }
        ),
    )
    return value


def prepared_command(argv, *, admission: AdmissionPool, cwd, env, deadline):
    """Parent admission covers all work in the unmodified prepared Python leg."""
    if not isinstance(admission, AdmissionPool):
        raise ValueError("prepared execution requires an AdmissionPool")
    with admission.acquire(deadline=deadline):
        return wheel.command(
            argv, cwd=cwd, env=env, timeout_seconds=deadline - time.monotonic()
        )


def prepared_root(tmp, *, admission: AdmissionPool):
    # Keep the original preparation envelope and complete protected region.
    if not isinstance(admission, AdmissionPool):
        raise ValueError("preparation requires an AdmissionPool")
    with admission.acquire(deadline=time.monotonic() + 120):
        return _prepared_root(tmp)


def _prepared_root(tmp):
    # Missing candidate implementation is an explicit RED assertion, outside
    # attribution rejection/setup boundaries, not a broken prepared profile.
    for name in (*SURFACES, *IMPLEMENTATIONS):
        path = ROOT / "scripts" / name
        check(path.is_file(), f"required host interface is not implemented: {path}")
    root = service.prepare_inputs(ROOT, tmp / "prepared")
    (root / "tests").mkdir(mode=0o700)
    support_files = (*p.SUPPORT_FILES, "tests/host_acceptance_assertions.py")
    for relative in support_files:
        p.write(root / relative, p.read(ROOT / relative))
        (root / relative).chmod((ROOT / relative).stat().st_mode & 0o777)
    (root / "host_tests").mkdir(mode=0o700)
    p.write(root / "host_tests/__init__.py", b'"""Case-owned host marker."""\n')
    service.copy_tree(ROOT / "scripts", root / "scripts")
    p.write(root / "uv.lock", p.read(ROOT / "uv.lock"))
    (root / "uv.lock").chmod((ROOT / "uv.lock").stat().st_mode & 0o777)
    expected = inventory(ROOT, ("src/", "scripts/", "pyproject.toml", "uv.lock"))[1]
    expected.extend(
        [
            {"path": "tests", "kind": "directory", "executable": 0o100},
            {"path": "host_tests", "kind": "directory", "executable": 0o100},
            {
                "path": "host_tests/__init__.py",
                "kind": "file",
                "executable": 0,
                "sha256": hashlib.sha256(
                    b'"""Case-owned host marker."""\n'
                ).hexdigest(),
            },
        ]
    )
    expected.extend(
        {
            "path": relative,
            "kind": "file",
            "executable": (ROOT / relative).stat().st_mode & 0o111,
            "sha256": hashlib.sha256(p.read(ROOT / relative)).hexdigest(),
        }
        for relative in support_files
    )
    expected.sort(key=lambda item: item["path"])
    check(
        inventory(root)[1] == expected,
        "prepared profile has undeclared, missing or changed scoped inputs",
    )
    check(
        root_names(root) == sorted((*IDENTITY_ROOTS, "README.md")),
        "prepared profile has missing or undeclared roots",
    )
    check(wheel.inventory(root)[0] == wheel.inventory(ROOT)[0], "common bytes differ")
    for relative in (*support_files, *("scripts/" + s for s in IMPLEMENTATIONS)):
        check(
            p.read(root / relative) == p.read(ROOT / relative), "copy dependency drift"
        )
    revision = synthetic_head(root, tmp)
    check(len(revision) == 40, "unresolved synthetic fixture HEAD")
    check(
        git(root, "rev-parse", "HEAD").decode().strip() == revision,
        "unresolved synthetic fixture HEAD",
    )
    return root, revision


def synthetic_head(root, tmp):
    """Create a resolvable fixture revision with plumbing, never an index."""
    environment = {
        key: value
        for key, value in wheel.clean_environment().items()
        if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        value = wheel.command(
            ["git", "-C", root, *args],
            cwd=tmp,
            env=environment,
            timeout_seconds=20,
        )
        check(value["status"] == 0, f"fixture Git setup failed: {value!r}")
        return value["stdout"].decode("ascii").strip()

    git("init", "--quiet", "--object-format=sha1")
    empty = tmp / "empty-tree"
    p.write(empty, b"")
    tree = git("hash-object", "-t", "tree", "-w", str(empty))
    commit = tmp / "commit-input"
    p.write(
        commit,
        (
            f"tree {tree}\n"
            "author Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "committer Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "\nDeterministic attribution fixture, not the candidate revision\n"
        ).encode("ascii"),
    )
    revision = git("hash-object", "-t", "commit", "-w", str(commit))
    git("update-ref", "refs/heads/fixture", revision)
    git("symbolic-ref", "HEAD", "refs/heads/fixture")
    check(not (root / ".git/index").exists(), "fixture unexpectedly staged files")
    check(git("rev-parse", "HEAD") == revision, "fixture revision mismatch")
    return revision


def isolated_path(tmp, *, python=True, uv=True, git=True):
    tools = tmp / "bootstrap-tools"
    tools.mkdir()
    names = ["dirname", "readlink", "realpath"]
    if git:
        names.append("git")
    if python:
        names.append("python3.14")
    if uv:
        names.append("uv")
    for name in names:
        source = shutil.which(name)
        check(source is not None, f"contract fixture prerequisite missing: {name}")
        (tools / name).symlink_to(source)
    return tools


def build_case(tmp, case, *, admission):
    candidate_dist = attribution.dist_identity(ROOT)
    root, revision = prepared_root(tmp, admission=admission)
    (root / "dist").mkdir()
    p.write(root / "dist/unrelated.whl", b"unowned stale artifact")
    if not case.attempted:
        p.write(root / "dist/footman-0.1.0-py3-none-any.whl", b"unowned collision")
    before_dist = attribution.dist_identity(root)
    before, inputs = wheel.inventory(root)
    source_before, full_inputs = inventory(root)
    executable, call, attempts = scripted_uv(tmp, case)
    environment = wheel.clean_environment(
        {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    )
    evidence = tmp / "evidence"
    evidence.mkdir()
    manifest = evidence / "build.json"
    neutral = tmp / "caller-cwd"
    neutral.mkdir()
    result = invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(manifest)],
        admission=admission,
        cwd=neutral,
        env=environment,
        evidence=evidence,
    )
    after, after_inputs = wheel.inventory(root)
    source_after, full_post_inputs = inventory(root)
    check(
        (result["status"] == 0) is case.accepted,
        f"wrong public build attribution outcome for {case.case_id}: {result!r}",
    )
    check(call.exists() is case.attempted, "incorrect uv invocation observation")
    check(
        len(root_names(attempts)) == int(case.attempted),
        "incorrect uv invocation count",
    )
    if case.attempted:
        check(
            p.load(call)
            == {"argv": ["build"], "cwd": str(root), "PATH": environment["PATH"]},
            "build did not use exactly bare uv build at the wrapper-selected root",
        )
        # Wrapper stdout is not required to forward raw build output. Its
        # captured streams remain actual observations; the scripted tool's
        # configured status/bytes below are inputs, not measured child status.
    else:
        text = (result["stdout"] + result["stderr"]).decode("utf-8", "replace")
        for expected in (str(root), str(root / "dist/footman-0.1.0-py3-none-any.whl")):
            check(expected in text, "collision diagnostic omits root/exact path")
        check("no build/deletion/replacement" in text, "missing no-delete diagnostic")
        check(attribution.dist_identity(root) == before_dist, "collision changed dist")
    observed_build = build_operation(manifest, root)
    check(
        observed_build["attempted"] is case.attempted,
        "public build observation fabricated invocation",
    )
    check(
        observed_build["status"] == (case.build_status if case.attempted else None),
        "underlying build status was lost or replaced",
    )
    if case.attempted:
        check(
            observed_build["stdout_bytes"] == case.stdout_bytes
            and observed_build["stderr_bytes"] == case.stderr_bytes,
            "underlying full binary build streams were lost",
        )
    if case.accepted:
        retained = p.load(manifest)
        check(
            type(retained["schema_version"]) is int,
            "manifest version is not an integer",
        )
        check(retained["schema_version"] == 1, "unsupported build manifest version")
        check(
            retained["build_input_identity"] == before == after,
            "build input binding mismatch",
        )
        # Binding must be public and independent of archive/install self-report.
        check(
            retained["wheel_sha256"] == hashlib.sha256(case.wheel_bytes).hexdigest(),
            "wrong wheel digest",
        )
        check(
            Path(retained["wheel_path"])
            == root / "dist/footman-0.1.0-py3-none-any.whl",
            "wrong wheel path",
        )
    else:
        check(not manifest.exists(), "rejected build published a success manifest")
    if case.repeat:
        old_dist = attribution.dist_identity(root)
        old_call = p.read(call)
        again = invoke(
            root,
            "build-acceptance-wheel",
            ["--manifest", str(evidence / "second.json")],
            admission=admission,
            cwd=neutral,
            env=environment,
            evidence=evidence,
            label="repeat",
        )
        check(again["status"] != 0, "repeat build overwrote prior outputs")
        diagnostic = (again["stdout"] + again["stderr"]).decode("utf-8", "replace")
        for name in ("footman-0.1.0.tar.gz", "footman-0.1.0-py3-none-any.whl"):
            check(str(root / "dist" / name) in diagnostic, "repeat omitted conflict")
        check(str(root) in diagnostic, "repeat omitted resolved root")
        check(
            "no build/deletion/replacement" in diagnostic,
            "repeat omitted no-delete rule",
        )
        check(
            p.read(call) == old_call and len(root_names(attempts)) == 1,
            "repeat invoked uv again",
        )
        check(
            attribution.dist_identity(root) == old_dist, "repeat changed prior outputs"
        )
    check((before != after) is (case.mutation is not None), "wrong mutation relation")
    check(
        p.read(root / "dist/unrelated.whl") == b"unowned stale artifact",
        "unrelated artifact changed",
    )
    check(attribution.dist_identity(ROOT) == candidate_dist, "candidate dist changed")
    record = {
        "case_id": case.case_id,
        "profile": list(case.phase11_profile),
        "synthetic_revision": revision,
        "candidate_revision": git(ROOT, "rev-parse", "HEAD").decode().strip(),
        "source_identity_pre": source_before,
        "source_identity_post": source_after,
        "full_input_inventory": full_inputs,
        "full_post_input_inventory": full_post_inputs,
        "pre_identity": before,
        "post_identity": after,
        "inventory": inputs,
        "post_inventory": after_inputs,
        "candidate_dist_pre": candidate_dist,
        "candidate_dist_post": attribution.dist_identity(ROOT),
        "stub_path": str(executable),
        "stub_sha256": hashlib.sha256(p.read(executable)).hexdigest(),
        "stub_bytes_base64": base64.b64encode(p.read(executable)).decode("ascii"),
        "operation": operation_record(result),
        "observed_build_operation": {
            key: value
            for key, value in observed_build.items()
            if key not in {"stdout_bytes", "stderr_bytes"}
        },
        "observed_build_stdout_base64": base64.b64encode(
            observed_build["stdout_bytes"]
        ).decode()
        if observed_build["attempted"]
        else None,
        "observed_build_stderr_base64": base64.b64encode(
            observed_build["stderr_bytes"]
        ).decode()
        if observed_build["attempted"]
        else None,
        "configured_tool_status": case.build_status,
        "configured_tool_stdout_base64": base64.b64encode(case.stdout_bytes).decode(
            "ascii"
        ),
        "configured_tool_stderr_base64": base64.b64encode(case.stderr_bytes).decode(
            "ascii"
        ),
        "attempted": case.attempted,
        "accepted": case.accepted,
    }
    check(
        (source_before != source_after)
        is (case.mutation is not None and case.mutation != "README.md"),
        "source/build-input scope confusion",
    )
    p.write(evidence / "case.json", p.canonical(record))
    return record


CACHE_NAMES = frozenset(
    {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv"}
)
SOURCE_IDENTITY_ROOTS = (
    "src/",
    "tests/",
    "host_tests/",
    "scripts/",
    "pyproject.toml",
    "uv.lock",
)


def inventory(root, roots=SOURCE_IDENTITY_ROOTS):
    """Independent reference using already tested capability/read primitives."""
    records = []

    def visit(relative):
        path = root / relative
        info = path.lstat()
        value = {"path": relative.as_posix(), "executable": info.st_mode & 0o111}
        if stat.S_ISLNK(info.st_mode):
            records.append(value | {"kind": "link", "target": os.readlink(path)})
        elif stat.S_ISREG(info.st_mode):
            records.append(
                value
                | {"kind": "file", "sha256": hashlib.sha256(p.read(path)).hexdigest()}
            )
        elif stat.S_ISDIR(info.st_mode):
            records.append(value | {"kind": "directory"})
            for name in root_names(path):
                if name not in CACHE_NAMES:
                    visit(relative / name)
        else:
            raise ValueError("unsupported reference inventory node: " + str(path))

    for name in roots:
        visit(Path(name.rstrip("/")))
    records.sort(key=lambda item: item["path"])
    return {
        "schema_version": 1,
        "roots": list(roots),
        "sha256": hashlib.sha256(p.canonical(records)).hexdigest(),
    }, records


def scripted_uv(tmp, case):
    executable, call = attribution.scripted_uv(tmp, case)
    attempts = executable.parent / "attempts"
    attempts.mkdir()
    data = p.read(executable).decode()
    data = data.replace("import os, sys\n", "import os, sys, uuid\n")
    anchor = "root = Path.cwd()\n"
    check(data.count(anchor) == 1, "scripted tool instrumentation anchor changed")
    insertion = f"p.write(Path({str(attempts)!r}) / (uuid.uuid4().hex + '.json'), p.canonical({{'argv': sys.argv[1:], 'cwd': str(root), 'PATH': os.environ['PATH']}}))\n"
    data = data.replace(anchor, anchor + insertion)
    replace(executable, data.encode())
    return executable, call, attempts


def git(root, *args):
    environment = {
        key: value
        for key, value in wheel.clean_environment().items()
        if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = wheel.command(
        ["git", "-C", root, *args], cwd=root.parent, env=environment, timeout_seconds=20
    )
    check(result["status"] == 0, "fixture Git command failed: " + repr(result))
    return result["stdout"]


def bind(
    root, evidence, *, admission, cwd=None, env=None, label="binding", deadline=None
):
    """Real uv only: the returned wheel may be used by isolated runner tests."""
    deadline = time.monotonic() + 120 if deadline is None else deadline
    evidence.mkdir()
    path = evidence / "build-manifest.json"
    before, entries = inventory(root)
    inputs, input_entries = wheel.inventory(root)
    result = invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(path)],
        admission=admission,
        cwd=root if cwd is None else cwd,
        evidence=evidence,
        env=env,
        label=label,
        deadline=deadline,
    )
    check(result["status"] == 0, "controlled real build failed: " + repr(result))
    value = p.load(path)
    check(
        value["source_identity"] == before == inventory(root)[0],
        "source identity binding mismatch",
    )
    check(
        value["build_input_identity"] == inputs == wheel.inventory(root)[0],
        "build-input identity binding mismatch",
    )
    check(value["source_inventory"] == entries, "full source inventory omitted")
    check(
        value["build_input_inventory"] == input_entries, "build-input inventory omitted"
    )
    check(
        value["revision"] == git(root, "rev-parse", "HEAD").decode().strip(),
        "wrapper root revision mismatch",
    )
    for artifact in ("wheel", "sdist"):
        check(
            hashlib.sha256(p.read(Path(value[artifact + "_path"]))).hexdigest()
            == value[artifact + "_sha256"],
            "produced artifact binding mismatch",
        )
    return path, value


def build_operation(manifest, root):
    operation = p.load(manifest.with_name(manifest.stem + "-operation.json"))
    check(
        operation["argv"] == ["uv", "build"] and operation["cwd"] == str(root),
        "public build command attribution mismatch",
    )
    if operation["attempted"]:
        check(
            type(operation["status"]) is int, "observed build status is not an integer"
        )
        for stream in ("stdout", "stderr"):
            path = Path(operation[stream + "_path"])
            check(
                path.is_absolute()
                and path == path.resolve()
                and path.is_relative_to(manifest.parent),
                "build output escaped its recorded evidence domain",
            )
            operation[stream + "_bytes"] = p.read(path)
    else:
        check(operation["status"] is None, "unattempted build fabricated a status")
    return operation


def scripted_binding(root, evidence, tools, *, admission, case=None):
    evidence.mkdir()
    tools.mkdir()
    executable, _, attempts = scripted_uv(
        tools,
        __import__("tests.wheel_attribution_cases", fromlist=["CASES"]).CASES[0]
        if case is None
        else case,
    )
    manifest = evidence / "build-manifest.json"
    environment = wheel.clean_environment(
        {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    )
    result = invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(manifest)],
        admission=admission,
        cwd=evidence,
        evidence=evidence,
        env=environment,
    )
    check(result["status"] == 0, "scripted identity binding failed: " + repr(result))
    check(
        len(root_names(attempts)) == 1,
        "scripted identity control crossed the build-only boundary",
    )
    value = p.load(manifest)
    check(
        value["source_identity"] == inventory(root)[0],
        "source identity is not independently derived",
    )
    check(
        value["build_input_identity"] == wheel.inventory(root)[0],
        "build-input identity is not independently derived",
    )
    return manifest, value


def relocate(root, destination):
    """Copy a clean matching root, not a dist-selected reuse or deletion."""
    destination.mkdir()
    for name in root_names(root):
        if name in (CACHE_NAMES - {".git"}) or name == "dist":
            continue
        source, target = root / name, destination / name
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_dir():
            service.copy_tree(source, target)
        else:
            p.write(target, p.read(source))
            target.chmod(source.stat().st_mode & 0o777)
    check(inventory(root) == inventory(destination), "relocation changed scoped inputs")
    check(
        wheel.inventory(root) == wheel.inventory(destination),
        "relocation changed build inputs",
    )
    check(
        git(root, "rev-parse", "HEAD") == git(destination, "rev-parse", "HEAD"),
        "relocation changed revision",
    )
    return destination


def probe_docker(tmp):
    """Development prerequisite spy, not a daemon or container implementation."""
    import sys

    directory = tmp / "docker-prerequisite-spy"
    directory.mkdir()
    executable = directory / "docker"
    records = directory / "records"
    records.mkdir()
    source = f"""#!{sys.executable}
import json, sys, uuid
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tests import mcp_provenance as p
args = sys.argv[1:]
p.write(Path({str(records)!r}) / (uuid.uuid4().hex + '.json'), p.canonical({{"argv": args}}))
if not args or args[0] not in ('--version', 'version', 'info'):
    raise SystemExit(91)
if '--format' in args:
    fmt = args[args.index('--format') + 1]
    if 'json' in fmt:
        print(json.dumps({{"Client": {{"Version": "27.0.0"}}, "Server": {{"Version": "27.0.0"}}, "Version": "27.0.0", "ServerVersion": "27.0.0"}}))
    else:
        print('27.0.0')
else:
    print('Docker version 27.0.0, development prerequisite spy')
"""
    p.write(executable, source.encode())
    executable.chmod(0o700)
    return executable, records


def runner(
    root,
    build_manifest,
    wheel_path,
    evidence,
    *,
    admission,
    env=None,
    label="runner",
    image="d9e853e87e55",
    cwd=None,
):
    evidence.mkdir()
    return invoke(
        root,
        "run-host-acceptance",
        [
            "--wheel",
            str(wheel_path),
            "--source-identity",
            str(build_manifest),
            "--image",
            image,
            "--evidence",
            str(evidence),
        ],
        admission=admission,
        cwd=root if cwd is None else cwd,
        evidence=evidence.parent,
        env=env,
        label=label,
    )


BOOTSTRAP_ENV_GUARD = """import atexit, json, os, sys
import footman
@atexit.register
def report():
    sys.stderr.write('contract-bootstrap ' + json.dumps({'pythonpath': os.environ.get('PYTHONPATH'), 'foundation': footman.__file__}, sort_keys=True) + '\\n')
"""


ACQUISITION_GUARD = """import atexit, json, os, sys
from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.cleanup import CleanupFailures
from footman.process import OwnedProcess
counts = {"descriptor_adoptions": 0, "launch_calls": 0}
old_init = OwnedDescriptor.__init__
old_launch = OwnedProcess.launch
def initialize(self, *args, **kwargs):
    old_init(self, *args, **kwargs)
    try:
        counts['descriptor_adoptions'] += 1
    except BaseException as primary:
        failures = CleanupFailures(primary)
        failures.run(self.close, ordinary=(OSError,))
        error = failures.complete()
        if error is not None:
            raise error
        raise
def launch(cls, *args, **kwargs):
    counts['launch_calls'] += 1
    return old_launch(*args, **kwargs)
OwnedDescriptor.__init__ = initialize
OwnedProcess.launch = classmethod(launch)
@atexit.register
def report():
    sys.stderr.write('contract-acquisitions ' + json.dumps(counts, sort_keys=True) + '\\n')
"""


def install_readme_guard(root, fault, *, after=None):
    source = """import atexit, json, os, sys
original_read, original_close = os.read, os.close
counts = {'read_calls': 0, 'close_calls': 0}
target = TARGET
fault = FAULT
after = AFTER
def selected(fd):
    try:
        return os.readlink('/proc/self/fd/' + str(fd)) == target and (after is None or os.path.exists(after))
    except OSError:
        return False
def read(fd, count):
    if selected(fd):
        counts['read_calls'] += 1
        if fault in ('operation', 'operation-close'):
            raise OSError('contract README read failure')
        if fault == 'interruption':
            raise KeyboardInterrupt('contract README interruption')
    return original_read(fd, count)
def close(fd):
    chosen = selected(fd)
    original_close(fd)
    if chosen:
        counts['close_calls'] += 1
        if fault in ('operation-close', 'close'):
            raise OSError('contract README close failure')
        if fault == 'close-interruption':
            raise KeyboardInterrupt('contract README close interruption')
os.read, os.close = read, close
@atexit.register
def report():
    sys.stderr.write('contract-readme ' + json.dumps(counts, sort_keys=True) + '\\n')
"""
    source = source.replace(
        "target = TARGET", "target = " + repr(str(root / "README.md"))
    ).replace("fault = FAULT", "fault = " + repr(fault))
    source = source.replace(
        "after = AFTER", "after = " + repr(None if after is None else str(after))
    )
    p.write(root / "src/sitecustomize.py", source.encode())


def install_acquisition_guard(root):
    p.write(root / "src/sitecustomize.py", ACQUISITION_GUARD.encode())


def malformed_archive(data, fault):
    import io
    import zipfile

    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as original,
        zipfile.ZipFile(output, "w") as archive,
    ):
        for item in original.infolist():
            if fault == "missing-entry" and item.filename == "footman/__main__.py":
                continue
            content = original.read(item.filename)
            if fault == "entrypoint" and item.filename.endswith(
                ".dist-info/entry_points.txt"
            ):
                content = b"[console_scripts]\nfootman = wrong.module:main\n"
            archive.writestr(item, content)
        if fault == "traversal":
            archive.writestr("../outside.py", b"# never installed\n")
        elif fault == "absolute":
            archive.writestr("/outside.py", b"# never installed\n")
        elif fault == "symlink":
            entry = zipfile.ZipInfo("footman/link.py")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, b"../../outside.py")
        elif fault == "duplicate":
            archive.writestr("footman/duplicate.py", b"# first\n")
            archive.writestr("footman/different.py", b"# second\n")
        elif fault == "module-alias":
            archive.writestr("footman/ambiguous.py", b"# module\n")
            archive.writestr("footman/ambiguous/__init__.py", b"# package\n")
        elif fault == "scheme":
            archive.writestr(
                "footman-0.1.0.data/purelib/extra.py", b"# unsupported mapping\n"
            )
    result = output.getvalue()
    if fault == "duplicate":
        check(
            result.count(b"footman/different.py") == 2,
            "duplicate archive fixture anchor changed",
        )
        result = result.replace(b"footman/different.py", b"footman/duplicate.py")
    return result


def install_proxy(tmp, fault):
    import sys

    directory = tmp / "install-tool"
    directory.mkdir()
    records = directory / "records"
    records.mkdir()
    replacement = directory / "replacement"
    service.copy_tree(ROOT / "src/footman", replacement)
    replace(
        replacement / "container.py",
        p.read(replacement / "container.py") + b"\n# fixture-owned swapped package\n",
    )
    executable = directory / "uv"
    p.write(
        executable,
        (
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom tests.host_install_proxy import main\nmain()\n"
        ).encode(),
    )
    executable.chmod(0o700)
    native = shutil.which("uv")
    check(native is not None, "real uv prerequisite missing")
    environment = {
        "FOOTMAN_HOST_TOOL_REAL_UV": native,
        "FOOTMAN_HOST_TOOL_RECORDS": str(records),
        "FOOTMAN_HOST_TOOL_REPLACEMENT": str(replacement),
        "FOOTMAN_HOST_TOOL_FAULT": fault,
        "FOOTMAN_HOST_TOOL_BLOCK_INSTALL": "0",
    }
    return executable, environment, records


def configure_retag_loader(loader):
    data = p.read(loader).decode()
    anchor = '    if operation == "launch":\n        output = '
    check(data.count(anchor) == 1, "retag fixture launch anchor changed")
    data = data.replace(
        anchor,
        '    if operation == "launch":\n        images = p.load(Path(os.environ["FOOTMAN_STUB_IMAGE_MAP"]))\n        operand = arguments[arguments.index("--") + 1]\n        p.write(directory / "selected-image.json", p.canonical({"operand": operand, "resolved_image": images[operand]}))\n        output = ',
    )
    inspection = '(arguments[-1] + "|running|sha256:" + "b" * 64 + "\\n").encode()'
    check(data.count(inspection) == 1, "retag fixture inspection anchor changed")
    data = data.replace(
        inspection,
        '(arguments[-1] + "|running|" + p.load(directory / "selected-image.json")["resolved_image"] + "\\n").encode()',
    )
    replace(loader, data.encode())


def copy_probe_bundle(destination):
    (destination / "tests").mkdir(parents=True)
    for relative in (
        *p.SUPPORT_FILES,
        "tests/relocated_docker_child.py",
        "tests/docker_cli_stub.py",
        "tests/dispatcher_harness.py",
    ):
        p.write(destination / relative, p.read(ROOT / relative))


def standalone_probe(base, tmp, fault, *, admission: AdmissionPool, optimized=False):
    """Validate probe setup with the independent Phase-9 real-wheel fixture.

    This never substitutes for a production runner invocation. Immutable cases
    borrow that worker install; package mutations use a recorded private clone.
    """
    destination = tmp / "installation"
    destination.mkdir()
    bound = wheel.reuse_for_test(
        base,
        destination,
        admission=admission,
        mutable=fault
        in {
            "stale-module",
            "stale-entry",
            "self-consistent-stale",
            "missing-module",
            "extra-module",
            "symlink-module",
        },
        private_support=True,
    )
    copy_probe_bundle(tmp / "contract_support")
    if fault == "retag-image":
        configure_retag_loader(tmp / "contract_support/tests/relocated_docker_child.py")
    source = tmp / "probe.py"
    p.write(source, p.read(ROOT / "tests/host_runner_probe.py"))
    origin = tmp / "origin.json"
    p.write(origin, p.canonical(bound.expected_origin))
    neutral = tmp / "neutral"
    neutral.mkdir()
    records = tmp / "observations"
    records.mkdir()
    _, config = service.configuration(tmp)
    context = tmp / "context.json"
    p.write(
        context,
        p.canonical(
            {
                "expected_origin_path": str(origin),
                "venv_python": str(bound.environment / "bin/python"),
                "support_root": str(bound.support_root),
                "server_script": str(bound.environment / "bin/footman"),
                "neutral_cwd": str(neutral),
                "provenance_evidence_path": str(tmp / "verified-set.json"),
                "configured_image_id": "d9e853e87e55",
            }
        ),
    )
    bootstrap = """import importlib.util, runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('tests', root / 'tests/__init__.py', submodule_search_locations=[str(root / 'tests')])
if spec is None or spec.loader is None:
    raise RuntimeError('standalone probe has no declared support')
module = importlib.util.module_from_spec(spec)
sys.modules['tests'] = module
spec.loader.exec_module(module)
probe = runpy.run_path(sys.argv[2])
probe['record_installation']()
probe['run']()
"""
    env = wheel.clean_environment(
        {
            "FOOTMAN_HOST_CONTEXT": str(context),
            "FOOTMAN_HOST_CONTRACT_RECORDS": str(records),
            "FOOTMAN_HOST_CONTRACT_CONFIG": str(config),
            "FOOTMAN_HOST_CONTRACT_FAULT": fault,
        }
    )
    result = wheel.command(
        [
            bound.environment / "bin/python",
            *(["-O"] if optimized else []),
            "-P",
            "-c",
            bootstrap,
            bound.support_root,
            source,
        ],
        cwd=neutral,
        env=env,
        timeout_seconds=45,
    )
    p.write(tmp / "standalone-operation.json", p.canonical(operation_record(result)))
    return result, records, bound


def policy_root(tmp, case, *, admission):
    root, _ = prepared_root(tmp, admission=admission)
    environment, records = configure_policy_suite(root, tmp, case)
    return root, environment, records


def configure_policy_suite(root, tmp, case):
    conftest = b'pytest_plugins = ["tests.green_skip_policy"]\nfrom host_tests.probe_support import record_installation\ndef pytest_sessionstart(session):\n    record_installation(session)\n'
    p.write(
        root / "host_tests/probe_support.py",
        p.read(ROOT / "tests/host_runner_probe.py"),
    )
    copy_probe_bundle(root / "host_tests/contract_support")
    p.write(root / "host_tests/conftest.py", conftest)
    cases = {
        "pass": "from host_tests.probe_support import run\ndef test_probe():\n    run()\n",
        "skip": "import pytest\ndef test_probe():\n    pytest.skip('isolated runner control')\n",
        "xfail": "import pytest\n@pytest.mark.xfail(reason='isolated runner control')\ndef test_probe():\n    assert False\n",
        "xpass": "import pytest\nfrom host_tests.probe_support import run\n@pytest.mark.xfail(reason='isolated runner control')\ndef test_probe():\n    run()\n",
        "fail": "def test_probe():\n    assert False, 'isolated runner failure'\n",
        "empty": "# no tests: pytest exit 5 must remain unsuccessful\n",
    }
    check(case in cases, "unknown runner policy control")
    p.write(root / "host_tests/test_probe.py", cases[case].encode())
    # Deliberately invalid checkout metadata/testpaths must never be copied as
    # the installation target or silently choose the development suite.
    p.write(
        root / "tests/test_must_not_collect.py",
        b"raise RuntimeError('DEVELOPMENT_SUITE_SELECTED')\n",
    )
    spy, records = probe_docker(tmp)
    environment = wheel.clean_environment(
        {"PATH": str(spy.parent) + os.pathsep + os.environ["PATH"]}
    )
    observations = tmp / "host-probe-records"
    observations.mkdir()
    _, config = service.configuration(tmp)
    environment.update(
        FOOTMAN_HOST_CONTRACT_RECORDS=str(observations),
        FOOTMAN_HOST_CONTRACT_CONFIG=str(config),
    )
    return environment, records


def standalone_policy(base, tmp, case, *, admission: AdmissionPool):
    destination = tmp / "installation"
    destination.mkdir()
    bound = wheel.reuse_for_test(base, destination, admission=admission, mutable=True)
    workspace = tmp / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "host_tests").mkdir()
    p.write(workspace / "host_tests/__init__.py", b"")
    for relative in p.SUPPORT_FILES:
        p.write(workspace / relative, p.read(ROOT / relative))
    environment, _ = configure_policy_suite(workspace, tmp, case)
    deadline = time.monotonic() + 120
    with admission.acquire(deadline=deadline):
        provisioning = wheel.command(
            [
                "uv",
                "pip",
                "install",
                "--constraint",
                base.environment.parent / "locked-constraints.txt",
                "--python",
                bound.environment / "bin/python",
                "pytest==8.4.2",
                "pytest-xdist==3.8.0",
            ],
            cwd=tmp,
            env=wheel.clean_environment(),
            timeout_seconds=deadline - time.monotonic(),
        )
    check(
        provisioning["status"] == 0,
        "isolated policy fixture provisioning failed: " + repr(provisioning),
    )
    p.write(
        tmp / "pytest-provisioning.json", p.canonical(operation_record(provisioning))
    )
    origin = tmp / "origin.json"
    p.write(origin, p.canonical(bound.expected_origin))
    neutral = tmp / "neutral"
    neutral.mkdir()
    context = tmp / "context.json"
    p.write(
        context,
        p.canonical(
            {
                "expected_origin_path": str(origin),
                "venv_python": str(bound.environment / "bin/python"),
                "support_root": str(workspace),
                "server_script": str(bound.environment / "bin/footman"),
                "neutral_cwd": str(neutral),
                "provenance_evidence_path": str(tmp / "verified-set.json"),
                "configured_image_id": "d9e853e87e55",
            }
        ),
    )
    environment["FOOTMAN_HOST_CONTEXT"] = str(context)
    ini = tmp / "host.ini"
    p.write(ini, b"[pytest]\naddopts = --strict-config --strict-markers\n")
    # The decoy default project configuration must not select development tests.
    p.write(
        workspace / "pyproject.toml",
        b'[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    )
    result = wheel.command(
        [
            bound.environment / "bin/python",
            "-m",
            "pytest",
            "-c",
            ini,
            workspace / "host_tests",
            "-n",
            "1",
            "--dist",
            "worksteal",
            "--tb=short",
            "-q",
        ],
        cwd=workspace,
        env=environment,
        timeout_seconds=deadline - time.monotonic(),
    )
    p.write(
        tmp / "isolated-policy-operation.json", p.canonical(operation_record(result))
    )
    return result, Path(environment["FOOTMAN_HOST_CONTRACT_RECORDS"]), bound


def replace(path, data):
    """Reuse the existing fixture writer rather than acquiring another owner."""
    write_bytes(path, data)
