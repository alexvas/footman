#!/usr/bin/env python3.14
"""Operator entry point. Core contracts are not real-host acceptance evidence.

Only main bootstraps the checked checkout foundation. Importing this module is
resource-free; prepared-leg execution is private, shared by both profiles.
"""

import argparse
import ast
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

_RESULTS_BASE = Path("/tmp/gorshochek/ephemeral/footman.git/host-acceptance")
_SOURCE_ROOTS = (
    "src/",
    "tests/",
    "host_tests/",
    "scripts/",
    "pyproject.toml",
    "uv.lock",
)
_BUILD_ROOTS = ("src/", "pyproject.toml", "README.md")
_CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
_SOURCE_CACHES = _CACHES | {".git", ".mypy_cache", ".venv"}
_DIAGNOSTICS = {
    "staged_bytes": "staged bytes mismatch",
    "argv": "configured argv mismatch",
    "command_status": "command status mismatch",
    "cleanup": "container cleanup mismatch",
}
_CASES = tuple(_DIAGNOSTICS)
bootstrap_import_root = None


@dataclass(frozen=True)
class PreparedLeg:
    wheel: Path
    source_identity: Path
    image: str
    evidence: Path
    workspace: Path
    profile: str
    suite_paths: tuple[Path, ...]
    case_id: str | None


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate JSON field")
        value[key] = item
    return value


def _bootstrap():
    global bootstrap_import_root
    root = Path(__file__).resolve(strict=True).parents[1]
    source = root / "src"
    required = (
        "__init__.py",
        "process.py",
        "filesystem/__init__.py",
        "filesystem/descriptors.py",
        "filesystem/operations.py",
        "filesystem/cleanup.py",
    )
    # No foundation code is trusted until its lexical origins are established.
    for relative in (
        "src",
        "src/footman",
        "src/footman/filesystem",
        *("src/footman/" + name for name in required),
    ):
        path = root / relative
        _require(
            path.exists() and not path.is_symlink() and path.resolve() == path,
            "missing or unsafe checkout foundation root: " + str(path),
        )
    for name, module in tuple(sys.modules.items()):
        if name == "footman" or name.startswith("footman."):
            origin = getattr(module, "__file__", None)
            expected = source.joinpath(*name.split("."))
            expected = (
                expected / "__init__.py"
                if expected.is_dir()
                else expected.with_suffix(".py")
            )
            loaded_spec = getattr(module, "__spec__", None)
            _require(
                origin is not None
                and Path(origin) == expected
                and expected.resolve() == expected
                and not expected.is_symlink()
                and loaded_spec is not None
                and loaded_spec.origin == str(expected),
                "preloaded wrong-root foundation: " + name,
            )
    spec = importlib.machinery.PathFinder.find_spec("footman", [str(source)])
    _require(
        spec is not None and spec.origin == str(source / "footman/__init__.py"),
        "foundation spec/root disagreement",
    )
    _require(sys.path.count(str(source)) <= 1, "duplicate foundation bootstrap root")
    if str(source) in sys.path:
        sys.path.remove(str(source))
    sys.path.insert(0, str(source))
    # This is the single allowed single-file bootstrap, not a module-top import.
    from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
    from footman.filesystem.operations import PosixDescriptorOps
    from footman.process import OwnedProcess, ProcessStream, StreamOutcome

    # Validate what import actually selected, not only the PathFinder preflight.
    for name, module in tuple(sys.modules.items()):
        if name == "footman" or name.startswith("footman."):
            expected = source.joinpath(*name.split("."))
            expected = (
                expected / "__init__.py"
                if expected.is_dir()
                else expected.with_suffix(".py")
            )
            loaded_spec = getattr(module, "__spec__", None)
            _require(
                getattr(module, "__file__", None) == str(expected)
                and expected.resolve() == expected
                and loaded_spec is not None
                and loaded_spec.origin == str(expected),
                "imported wrong-root foundation: " + name,
            )
    bootstrap_import_root = source
    return (
        root,
        DirectoryDescriptor,
        OwnedDescriptor,
        PosixDescriptorOps(),
        OwnedProcess,
        ProcessStream,
        StreamOutcome,
    )


class _Core:
    def __init__(self, foundation):
        (
            self.root,
            self.Directory,
            self.Descriptor,
            self.ops,
            self.Process,
            self.Stream,
            self.Outcome,
        ) = foundation

    def read(self, path, transform=lambda data: data):
        path = Path(path)
        with self.Directory.open_secure_path(self.ops, path.parent) as parent:
            return self.read_child(parent, path.name, transform)

    def read_child(self, parent, name, transform=lambda data: data):
        name = parent.child_basename(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        label = "host orchestration input"
        fd = os.open(name, flags, dir_fd=parent.fd)
        with self.Descriptor(self.ops, fd, label=label) as owner:
            _require(stat.S_ISREG(os.fstat(owner.fd).st_mode), "input must be regular")
            chunks = []
            while chunk := os.read(owner.fd, 65536):
                chunks.append(chunk)
            return transform(b"".join(chunks))

    def load(self, path):
        return self.read(path, lambda data: json.loads(data, object_pairs_hook=_unique))

    def write_child(self, parent, name, data):
        name = parent.child_basename(name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        label = "host orchestration evidence"
        fd = os.open(name, flags, 0o600, dir_fd=parent.fd)
        with self.Descriptor(self.ops, fd, label=label) as owner:
            remaining = memoryview(data)
            while remaining:
                count = os.write(owner.fd, remaining)
                _require(count > 0, "evidence write made no progress")
                remaining = remaining[count:]
            os.fsync(owner.fd)
        os.fsync(parent.fd)

    def write(self, path, data):
        with self.Directory.open_secure_path(self.ops, path.parent) as parent:
            self.write_child(parent, path.name, data)

    def retain_streams(self, directory, prefix, stdout, stderr):
        from footman.filesystem.cleanup import CleanupFailures

        paths = {}
        failures = CleanupFailures(None)
        for name, payload in (("stdout", stdout), ("stderr", stderr)):
            path = directory / (prefix + name + ".txt")
            failures.run(
                lambda path=path, payload=payload: self.write(path, payload),
                ordinary=(Exception,),
            )
            paths[name + "_path"] = str(path)
        selected = failures.complete()
        if selected is not None:
            raise selected
        return paths

    def mkdir(self, path):
        with self.Directory.open_secure_path(self.ops, path.parent) as parent:
            with parent.create_directory(path.name, mode=0o700):
                pass

    def publish(self, run, report):
        data = _canonical(report)
        temporary = ".report-" + uuid.uuid4().hex
        replaced = False
        try:
            with self.Directory.open_secure_path(self.ops, run) as directory:
                self.write_child(directory, temporary, data)
                os.replace(
                    temporary,
                    "report.json",
                    src_dir_fd=directory.fd,
                    dst_dir_fd=directory.fd,
                )
                replaced = True
                os.fsync(directory.fd)
            _require(self.read(run / "report.json") == data, "report readback mismatch")
        except BaseException as primary:
            if replaced:
                from footman.filesystem.cleanup import CleanupFailures

                # The publication owner may already have consumed its fd on a
                # failed close. A separate capability owns invalidation; never
                # reuse that fd, retry close or follow an absolute-path alias.
                def quarantine():
                    with self.Directory.open_secure_path(self.ops, run) as directory:
                        os.replace(
                            "report.json",
                            temporary + ".failed",
                            src_dir_fd=directory.fd,
                            dst_dir_fd=directory.fd,
                        )
                        os.fsync(directory.fd)

                failures = CleanupFailures(primary)
                failures.run(quarantine, ordinary=(Exception,))
                # Invocation status remains authoritative even if quarantine
                # itself is impossible; never accept report.json in isolation.
                failures.complete()
            raise

    def inventory(self, roots):
        records = []
        excluded = _CACHES if roots == _BUILD_ROOTS else _SOURCE_CACHES

        def visit(directory, name, relative):
            info = directory.stat_child(name)
            value = {"path": relative, "executable": info.st_mode & 0o111}
            if stat.S_ISLNK(info.st_mode):
                records.append(
                    value
                    | {"kind": "link", "target": os.readlink(name, dir_fd=directory.fd)}
                )
            elif stat.S_ISREG(info.st_mode):
                records.append(
                    value
                    | {
                        "kind": "file",
                        "sha256": self.read_child(
                            directory,
                            name,
                            lambda data: hashlib.sha256(data).hexdigest(),
                        ),
                    }
                )
            elif stat.S_ISDIR(info.st_mode):
                records.append(value | {"kind": "directory"})
                with directory.open_directory(name) as child:
                    for nested in sorted(child.list_names()):
                        if nested not in excluded:
                            visit(child, nested, relative + "/" + nested)
            else:
                raise ValueError("unsupported identity node: " + relative)

        with self.Directory.open_secure_path(self.ops, self.root) as directory:
            for name in roots:
                base = name.rstrip("/")
                info = directory.stat_child(base)
                _require(
                    stat.S_ISDIR(info.st_mode)
                    if name.endswith("/")
                    else stat.S_ISREG(info.st_mode),
                    "missing or unsafe identity root: " + name,
                )
                visit(directory, base, base)
        records.sort(key=lambda item: item["path"])
        return {
            "schema_version": 1,
            "roots": list(roots),
            "sha256": hashlib.sha256(_canonical(records)).hexdigest(),
        }, records

    def environment(self):
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "GIT_", "FOOTMAN_BOOTSTRAP_"))
        } | {"PYTHONNOUSERSITE": "1", "GIT_OPTIONAL_LOCKS": "0"}

    def command(self, argv, run=None, label=None, report=None):
        argv = list(map(str, argv))
        env = self.environment()
        data = {stream: bytearray() for stream in self.Stream}
        ends = {}
        failures = []

        def chunk(stream, payload):
            try:
                data[stream].extend(payload)
            except BaseException as error:
                failures.append(error)
                if not isinstance(error, Exception):
                    raise

        owner = result = primary = None
        try:
            with self.Process.launch(
                argv,
                deadline_seconds=120,
                grace_seconds=5,
                capture_limit=0,
                popen_kwargs={"cwd": str(self.root), "env": env},
            ) as owner:
                result = owner.stream(
                    chunk,
                    on_stream_end=lambda stream, outcome: ends.__setitem__(
                        stream, outcome
                    ),
                )
            if failures:
                raise failures[0]
            _require(
                result.lifecycle.complete
                and not result.lifecycle.failures
                and not result.timed_out
                and all(ends.get(s) is self.Outcome.EOF for s in self.Stream),
                "incomplete helper output/cleanup",
            )
        except BaseException as error:
            primary = error
        # The owner alone performs shutdown/reaping. Observing its status after
        # a failed stream/exit neither retries cleanup nor invents a child code.
        status = result.returncode if result is not None else None
        if status is None:
            status = getattr(owner, "returncode", None)
        out, err = bytes(data[self.Stream.STDOUT]), bytes(data[self.Stream.STDERR])
        operation_primary = failures[0] if failures else primary
        record = {
            "argv": argv,
            "cwd": str(self.root),
            "environment": {
                "keys": sorted(env),
                "PATH": env.get("PATH"),
                "PYTHONNOUSERSITE": env.get("PYTHONNOUSERSITE"),
            },
            "status": status,
            "cleanup_complete": primary is None
            and result is not None
            and result.lifecycle.complete,
            "error": None
            if operation_primary is None
            else type(operation_primary).__name__ + ": " + str(operation_primary),
        }
        from footman.filesystem.cleanup import CleanupFailures

        retention = CleanupFailures(operation_primary)

        def propagate(error):
            raise error

        for error in [*failures[1:], primary]:
            if error is not None and error is not operation_primary:
                retention.run(
                    lambda error=error: propagate(error), ordinary=(Exception,)
                )
        if run is not None:
            report["commands"].append(record)
            retention.run(
                lambda: record.update(self.retain_streams(run, label + "-", out, err)),
                ordinary=(Exception,),
            )
        try:
            selected = retention.complete()
            if selected is not None:
                raise selected
            if operation_primary is not None:
                raise operation_primary
        except BaseException as error:
            from footman.filesystem.cleanup import attach_secondary

            # Preflight commands have no run directory yet. Preserve their
            # observation on the original exception too, without wrapping it
            # or changing interruption/operation identity.
            try:
                error._host_command_evidence = {
                    "record": record,
                    "stdout": out,
                    "stderr": err,
                }
            except Exception as attachment_error:
                attach_secondary(error, [attachment_error])
            raise
        return status, out, err


def _runner_target(core):
    _require(
        bootstrap_import_root is None or bootstrap_import_root == core.root / "src",
        "runner/bootstrap root disagreement before module execution",
    )
    path = core.root / "scripts/run_host_acceptance.py"
    _require(
        not path.is_symlink()
        and path.resolve(strict=True) == path
        and path.parents[1] == core.root,
        "unsafe runner root",
    )
    data = core.read(path)
    # Loader inputs are checked against this root's recorded inventory before
    # execution, not an ambient import name or an installed scripts package.
    _, inventory = core.inventory(_SOURCE_ROOTS)
    entry = next(
        item for item in inventory if item["path"] == "scripts/run_host_acceptance.py"
    )
    digest = hashlib.sha256(data).hexdigest()
    _require(entry.get("sha256") == digest, "stale runner inventory")
    spec = importlib.util.spec_from_file_location("_footman_host_runner", path)
    _require(
        spec is not None and spec.loader is not None and spec.origin == str(path),
        "runner spec origin/root mismatch",
    )
    return path, digest


class _ValidatedSourceLoader(importlib.machinery.SourceFileLoader):
    """Execute only the already-owned, hashed source snapshot; never a pyc."""

    def __init__(self, name, path, source):
        super().__init__(name, str(path))
        self.source = source

    def get_code(self, fullname):
        return compile(self.source, self.path, "exec", dont_inherit=True)


def _import_expressions_safe(expressions):
    for expression in expressions:
        _require(
            expression is None
            or not any(isinstance(child, ast.Call) for child in ast.walk(expression)),
            "runner import acquisition refused",
        )


def _import_safe(nodes):
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Even a bare-name decorator is called implicitly. Custom bases
            # and metaclasses likewise execute hooks without an ast.Call.
            # This narrow loader accepts plain declarations, not unvetted
            # decoration/inheritance; it is not a general Python sandbox.
            _require(not node.decorator_list, "runner import decorator refused")
            if isinstance(node, ast.ClassDef):
                _import_expressions_safe(
                    [*node.bases, *(keyword.value for keyword in node.keywords)]
                )
                _require(
                    not node.bases and not node.keywords,
                    "runner import class hooks refused",
                )
                _import_safe(node.body)
            else:
                _import_expressions_safe([*node.args.defaults, *node.args.kw_defaults])
        elif isinstance(node, ast.If):
            _require(
                ast.unparse(node.test) == "__name__ == '__main__'",
                "unguarded runner import execution refused",
            )
            # The private module name skips the CLI body, but executes else.
            _import_safe(node.orelse)
        elif isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Raise)):
            _import_expressions_safe([node])
        else:
            _require(
                isinstance(
                    node,
                    (
                        ast.Import,
                        ast.ImportFrom,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.Raise,
                        ast.Pass,
                    ),
                ),
                "runner import execution refused",
            )


def _load_host_runner(core, observation=None):
    path, digest = _runner_target(core)
    source = core.read(path)
    _require(
        hashlib.sha256(source).hexdigest() == digest,
        "runner source changed before load",
    )
    tree = ast.parse(source, filename=str(path))
    # Refuse executable module-body acquisitions before exec_module. This is
    # an import-safe local tooling contract, not a malicious-code sandbox.
    _import_safe(tree.body)
    spec = importlib.util.spec_from_file_location(
        "_footman_host_runner",
        path,
        loader=_ValidatedSourceLoader("_footman_host_runner", path, source),
    )
    _require(
        spec is not None and spec.loader is not None and spec.origin == str(path),
        "runner spec origin/root mismatch",
    )
    module = importlib.util.module_from_spec(spec)
    before_path, before_env, before_cwd = list(sys.path), dict(os.environ), os.getcwd()
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        changed = (
            sys.path != before_path
            or dict(os.environ) != before_env
            or os.getcwd() != before_cwd
        )
        sys.path[:] = before_path
        if dict(os.environ) != before_env:
            os.environ.clear()
            os.environ.update(before_env)
        if os.getcwd() != before_cwd:
            os.chdir(before_cwd)
    _require(
        not changed
        and sys.path == before_path
        and dict(os.environ) == before_env
        and os.getcwd() == before_cwd,
        "runner import side effect",
    )
    _require(
        Path(module.__file__) == path
        and module._execute_prepared_host_leg.__code__.co_filename == str(path),
        "runner function origin mismatch",
    )
    _require(
        _runner_target(core) == (path, digest), "runner source changed during load"
    )
    if observation is not None:
        observation.update(
            module_path=str(path), spec_origin=spec.origin, source_sha256=digest
        )
    return module


@dataclass(frozen=True)
class _AttributedResult:
    result: object
    loader: dict

    def __getattr__(self, name):
        return getattr(self.result, name)


def _execute_prepared_leg(leg):
    core = _Core(_bootstrap())
    manifest = core.load(leg.source_identity)
    _require(
        manifest["source_identity"] == core.inventory(_SOURCE_ROOTS)[0],
        "stale prepared-leg source",
    )
    _require(
        manifest["build_input_identity"] == core.inventory(_BUILD_ROOTS)[0],
        "stale prepared-leg build inputs",
    )
    observation = {}
    runner = _load_host_runner(core, observation)
    from footman.filesystem.cleanup import CleanupFailures

    primary = None
    try:
        result = runner._execute_prepared_host_leg(leg)
    except BaseException as error:
        primary = error
    failures = CleanupFailures(primary)
    failures.run(
        lambda: _require(
            _runner_target(core)
            == (Path(observation["module_path"]), observation["source_sha256"]),
            "runner changed during prepared leg",
        ),
        ordinary=(Exception,),
    )
    selected = failures.complete()
    if selected is not None:
        raise selected
    if primary is not None:
        raise primary
    return _AttributedResult(result, observation)


def _leg_result(core, leg, result, record=None):
    outpath, errpath = leg.evidence / "stdout.txt", leg.evidence / "stderr.txt"
    counts = result.counts
    if isinstance(counts, dict):
        counts = counts.copy()
    _require(type(result.pytest_status) is int, "invalid child status")
    valid_counts = (
        isinstance(counts, dict)
        and set(counts)
        == {"collected", "passed", "failed", "skipped", "xfailed", "xpassed"}
        and all(type(v) is int and v >= 0 for v in counts.values())
    )
    policy = (
        valid_counts
        and counts["collected"] > 0
        and not any(counts[key] for key in ("skipped", "xfailed", "xpassed"))
    )
    baseline = result.stdout.splitlines().count(b"FIXTURE_BASELINE_VERIFIED") == 1
    detected = False
    kind = result.failure_kind
    if leg.case_id is not None:
        message = _DIAGNOSTICS.get(leg.case_id)
        diagnostic = message.encode() if message is not None else None
        detected = bool(
            diagnostic is not None
            and policy
            and baseline
            and result.pytest_status == 1
            and kind == "assertion"
            and counts["failed"] == 1
            and counts["passed"] == 0
            and counts["collected"] == 1
            and diagnostic in result.stdout.splitlines()
            and result.cleanup_complete
            and result.evidence_complete
        )
        if diagnostic is None or not baseline:
            kind = "invalid-observation/setup"
        accepted = detected
    else:
        accepted = bool(
            policy
            and result.pytest_status == 0
            and not counts["failed"]
            and counts["passed"] == counts["collected"]
            and kind is None
            and result.cleanup_complete
            and result.evidence_complete
        )
    if record is None:
        record = {}
    record.update(
        {
            "case_id": leg.case_id,
            "profile": leg.profile,
            "pytest_status": result.pytest_status,
            "counts": counts,
            "failure_kind": kind,
            "baseline_verified": baseline,
            "detected": detected,
            "cleanup_complete": result.cleanup_complete,
            "evidence_complete": result.evidence_complete,
            "stdout_path": str(outpath),
            "stderr_path": str(errpath),
            "suite_paths": list(map(str, leg.suite_paths)),
            "workspace": str(leg.workspace),
            "loader": getattr(result, "loader", None),
            "accepted": False,
            "evidence_retention": "pending",
        }
    )
    # Publish the observation into the caller's record before any evidence IO.
    core.retain_streams(leg.evidence, "", result.stdout, result.stderr)
    record["evidence_retention"] = "complete"
    record["accepted"] = accepted
    return record, accepted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_image_tag")
    args = parser.parse_args(argv)
    if (
        not args.local_image_tag
        or args.local_image_tag.startswith("-")
        or any(c.isspace() for c in args.local_image_tag)
        or "\x00" in args.local_image_tag
    ):
        parser.error("one nonempty local image tag is required")
    core = _Core(_bootstrap())
    if shutil.which("uv") is None:
        print("host acceptance prerequisite missing: uv", file=sys.stderr)
        return 127
    # Complete path/digest/syntax/import/exec preflight before any helper process,
    # even when prepared execution is doubled. Each leg still loads a fresh
    # module for independent state; this preflight never invokes the leg or CLI.
    runner_preflight = {}
    _load_host_runner(core, runner_preflight)
    runner_identity = (
        Path(runner_preflight["module_path"]),
        runner_preflight["source_sha256"],
    )
    source, source_inventory = core.inventory(_SOURCE_ROOTS)
    build, build_inventory = core.inventory(_BUILD_ROOTS)
    status, revision, _ = core.command(["git", "rev-parse", "HEAD"])
    _require(
        status == 0 and re.fullmatch(rb"[0-9a-f]{40,64}\n?", revision),
        "unresolved repository HEAD",
    )
    status, changes, _ = core.command(["git", "status", "--porcelain"])
    _require(status == 0, "cannot observe local changes")
    base = Path(_RESULTS_BASE)
    with core.Directory.open_secure_path(core.ops, base.parent) as parent:
        try:
            parent.stat_child(base.name)
        except FileNotFoundError:
            with parent.create_directory(base.name, mode=0o700):
                pass
    run_id = uuid.uuid4().hex
    run = base / run_id
    _require(
        not any(
            run.is_relative_to(core.root / name.rstrip("/")) for name in _SOURCE_ROOTS
        ),
        "evidence must be outside identity roots",
    )
    core.mkdir(run)
    print(str(run), flush=True)
    report = {
        "schema_version": 1,
        "state": "incomplete",
        "success": False,
        "run_id": run_id,
        "repository_root": str(core.root),
        "bootstrap_import_root": str(bootstrap_import_root),
        "revision": revision.decode().strip(),
        "local_changes": bool(changes),
        "source_identity": source,
        "source_inventory": source_inventory,
        "build_input_identity": build,
        "build_input_inventory": build_inventory,
        "caller_cwd": os.getcwd(),
        "tag": args.local_image_tag,
        "commands": [],
        "legs": [],
        "call_outcomes": [],
        "preparation_outcomes": [],
        "runner_preflight": runner_preflight | {"load_complete": True},
        "runtime": {"python": sys.version, "platform": sys.platform},
        "execution_scope": "core; integration and operator acceptance pending",
        "warnings": [
            "Full orchestration integration pending task 11.10.1; real-host execution pending operator task 11.15."
        ],
        "resource_reconciliation": {"evidence": "retained", "run_directory": str(run)},
        "first_unexpected_status": None,
    }
    primary = None
    first = None
    initial_confirmed = False
    try:
        core.publish(run, report)
        initial_confirmed = True
        status, output, _ = core.command(
            ["docker", "image", "inspect", "--format", "{{.Id}}", args.local_image_tag],
            run,
            "inspect",
            report,
        )
        if status:
            first = status
        else:
            _require(
                re.fullmatch(rb"sha256:[0-9a-fA-F]{64}\n?", output) is not None,
                "invalid inspect full image ID",
            )
            image = output.decode().rstrip("\n")
            report["resolved_image_id"] = image
            manifest_path = run / "build-manifest.json"
            status, _, _ = core.command(
                [
                    core.root / "scripts/build-acceptance-wheel",
                    "--manifest",
                    manifest_path,
                ],
                run,
                "build",
                report,
            )
            if status:
                first = status
            else:
                manifest = core.load(manifest_path)
                _require(
                    type(manifest.get("schema_version")) is int
                    and manifest["schema_version"] == 1,
                    "invalid build manifest schema",
                )
                _require(
                    manifest["source_identity"] == source
                    and manifest["build_input_identity"] == build
                    and manifest["revision"] == report["revision"],
                    "stale build manifest inputs",
                )
                wheel = Path(manifest["wheel_path"])
                _require(
                    wheel.is_absolute()
                    and wheel.parent == core.root / "dist"
                    and wheel.suffix == ".whl",
                    "invalid manifest-selected wheel",
                )
                _require(
                    hashlib.sha256(core.read(wheel)).hexdigest()
                    == manifest["wheel_sha256"],
                    "wheel digest mismatch",
                )
                report["build_manifest"] = str(manifest_path)
                report["wheel_path"] = str(wheel)
                report["wheel_sha256"] = manifest["wheel_sha256"]
                for case in (*_CASES, None):
                    directory = run / (case or "normal")
                    workspace = directory / "workspace"
                    host = workspace / "host_tests"
                    evidence = directory / "evidence"
                    preparation = {
                        "case_id": case,
                        "directory": str(directory),
                        "workspace": str(workspace),
                        "outcome": "preparing",
                    }
                    report["preparation_outcomes"].append(preparation)
                    stage = "leg-directory"
                    try:
                        core.mkdir(directory)
                        stage = "workspace-directory"
                        core.mkdir(workspace)
                        stage = "host-directory"
                        core.mkdir(host)
                        stage = "evidence-directory"
                        core.mkdir(evidence)
                        if case:
                            stage = "expectation-input"
                            override = {
                                "staged_bytes": "deliberately wrong staged bytes",
                                "argv": ["deliberately-wrong-argv"],
                                "command_status": 987654,
                                "cleanup": False,
                            }[case]
                            core.write(
                                host / "expectation_input.json",
                                _canonical(
                                    {
                                        "schema_version": 1,
                                        "case_id": case,
                                        "expected_override": override,
                                    }
                                ),
                            )
                        stage = "prepared-inputs"
                        leg = PreparedLeg(
                            wheel,
                            manifest_path,
                            image,
                            evidence,
                            workspace,
                            "negative" if case else "normal",
                            (host / "test_negative_expectations.py",)
                            if case
                            else (host,),
                            case,
                        )
                    except BaseException as error:
                        preparation.update(
                            outcome="failed",
                            stage=stage,
                            exception_type=type(error).__name__,
                            diagnostic=str(error),
                        )
                        report["warnings"].append(str(error))
                        if first is None:
                            first = 1
                        if not isinstance(error, Exception):
                            raise
                        continue
                    preparation["outcome"] = "prepared"
                    try:
                        result = _execute_prepared_leg(leg)
                    except BaseException as error:
                        report["call_outcomes"].append(
                            {
                                "case_id": case,
                                "outcome": "raised",
                                "exception_type": type(error).__name__,
                                "diagnostic": str(error),
                            }
                        )
                        report["warnings"].append(str(error))
                        if first is None:
                            first = 1
                        if not isinstance(error, Exception):
                            raise
                        continue
                    call = {"case_id": case, "outcome": "returned"}
                    report["call_outcomes"].append(call)
                    record = {
                        "case_id": case,
                        "profile": leg.profile,
                        "cleanup_complete": False,
                        "evidence_complete": False,
                        "accepted": False,
                        "detected": False,
                    }
                    report["legs"].append(record)
                    observed_status = None
                    try:
                        # Snapshot returned observations before validation or IO.
                        observed_status = result.pytest_status
                        counts = result.counts
                        call.update(
                            pytest_status=observed_status,
                            counts=counts.copy()
                            if isinstance(counts, dict)
                            else counts,
                        )
                        record.update(
                            pytest_status=observed_status,
                            counts=call["counts"],
                            cleanup_complete=result.cleanup_complete,
                        )
                        _, accepted = _leg_result(core, leg, result, record)
                        if not accepted and first is None:
                            first = observed_status or 1
                    except BaseException as error:
                        stage = (
                            "evidence-retention"
                            if record.get("evidence_retention") == "pending"
                            else "result-validation"
                        )
                        record.update(
                            accepted=False,
                            detected=False,
                            evidence_complete=False,
                            processing_error={
                                "stage": stage,
                                "exception_type": type(error).__name__,
                                "diagnostic": str(error),
                            },
                        )
                        if stage == "evidence-retention":
                            record["evidence_retention"] = "failed"
                        report["warnings"].append(str(error))
                        if first is None:
                            first = (
                                observed_status
                                if type(observed_status) is int and observed_status != 0
                                else 1
                            )
                        if not isinstance(error, Exception):
                            raise
        _require(
            core.inventory(_SOURCE_ROOTS)[0] == source
            and core.inventory(_BUILD_ROOTS)[0] == build
            and _runner_target(core) == runner_identity,
            "inputs changed during orchestration",
        )
        report["state"] = "final"
    except BaseException as error:
        primary = error
        if first is None:
            first = next(
                (
                    command["status"]
                    for command in report["commands"]
                    if type(command["status"]) is int and command["status"] != 0
                ),
                1,
            )
        report["warnings"].append(str(error))
    finally:
        report["first_unexpected_status"] = first
        report["resource_reconciliation"].update(
            preparation_attempts=len(report["preparation_outcomes"]),
            preparation_failures=sum(
                item["outcome"] == "failed" for item in report["preparation_outcomes"]
            ),
            prepared_calls=len(report["call_outcomes"]),
            settled_legs=sum(leg["cleanup_complete"] is True for leg in report["legs"]),
            complete=len(report["legs"]) == 5
            and all(
                leg["cleanup_complete"] is True and leg["evidence_complete"] is True
                for leg in report["legs"]
            ),
        )
        report["success"] = (
            first is None and report["state"] == "final" and len(report["legs"]) == 5
        )
        from footman.filesystem.cleanup import CleanupFailures

        failures = CleanupFailures(primary)
        if initial_confirmed:
            failures.run(lambda: core.publish(run, report), ordinary=(Exception,))
        selected = failures.complete()
        if selected is not None and selected is not primary:
            primary = selected
            if first is None:
                first = 1
        if primary is not None:
            if not isinstance(primary, Exception):
                raise primary
            print(str(primary), file=sys.stderr)
    return first or 0


if __name__ == "__main__":
    raise SystemExit(main())
