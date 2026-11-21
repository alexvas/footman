"""Isolated exact-wheel host execution; importing acquires no resources."""

import argparse
import ast
import configparser
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import stat
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from _footman_acceptance import (
    Tooling,
    canonical,
    checked_root,
    compare_binding,
)
from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import OwnedProcess, ProcessStream, StreamOutcome

_SUPPORT_FILES = (
    "tests/__init__.py",
    "tests/green_skip_policy.py",
    "tests/host_acceptance_assertions.py",
    "tests/mcp_provenance.py",
    "tests/mcp_server_child.py",
    "tests/mcp_transport.py",
    "tests/mcp_lifecycle.py",
)
_NEGATIVE_SUITE = "host_tests/test_negative_expectations.py"

# Explicit host policy, not Tooling's short build-helper default. Windows are
# planning allowances, not a collection filter/count limit. Fixture dispatches
# use at most this declared timeout (the intentional timeout scenario is lower).
_HOST_PROVISIONING_SECONDS = 600
_HOST_PREFLIGHT_AND_SMOKES_SECONDS = 120
_HOST_DISPATCH_TIMEOUT_SECONDS = 60
_HOST_DISPATCH_CLEANUP_SECONDS = 10
_HOST_NORMAL_DISPATCH_WINDOWS = 32
_HOST_FINALIZATION_SECONDS = 30


def _leg_budget(profile):
    windows = _HOST_NORMAL_DISPATCH_WINDOWS if profile == "normal" else 1
    return {
        "schema_version": 1,
        "profile": profile,
        "provisioning_seconds": _HOST_PROVISIONING_SECONDS,
        "preflight_and_smokes_seconds": _HOST_PREFLIGHT_AND_SMOKES_SECONDS,
        "dispatch_timeout_seconds": _HOST_DISPATCH_TIMEOUT_SECONDS,
        "dispatch_cleanup_seconds": _HOST_DISPATCH_CLEANUP_SECONDS,
        "dispatch_windows": windows,
        "finalization_seconds": _HOST_FINALIZATION_SECONDS,
        "total_seconds": _HOST_PROVISIONING_SECONDS
        + _HOST_PREFLIGHT_AND_SMOKES_SECONDS
        + windows * (_HOST_DISPATCH_TIMEOUT_SECONDS + _HOST_DISPATCH_CLEANUP_SECONDS)
        + _HOST_FINALIZATION_SECONDS,
    }


_HOST_INI = b"[pytest]\naddopts = --strict-config --strict-markers\n"
_CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
_LAUNCHER = """import sys
from footman.startup import main
if __name__ == "__main__":
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
"""


def _launcher_body(data, interpreter):
    # uv uses this exact shell/Python polyglot when a shebang is too long.
    # Strip only a fully bound trampoline, never arbitrary Python string nodes.
    quoted = "'" + str(interpreter).replace("'", "'\"'\"'") + "'"
    prefix = ("#!/bin/sh\n'''exec' " + quoted + ' "$0" "$@"\n' + "' '''\n").encode()
    if data.startswith(b"#!/bin/sh\n"):
        _require(data.startswith(prefix), "unsupported installed launcher semantics")
        return data[len(prefix) :]
    return data


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST", "GIT_"))
        and (
            not key.startswith("FOOTMAN_")
            or key.startswith(("FOOTMAN_HOST_CONTRACT_", "FOOTMAN_HOST_TOOL_"))
        )
    } | {"PYTHONNOUSERSITE": "1"}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate JSON key: " + key)
        value[key] = item
    return value


def _archive_modules(data):
    modules = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        _require(len(names) == len(set(names)), "duplicate wheel members")
        for name in names:
            path = PurePosixPath(name)
            canonical = path.as_posix() + ("/" if name.endswith("/") else "")
            _require(
                name == canonical
                and not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in name,
                "unsafe archive path",
            )
            mode = archive.getinfo(name).external_attr >> 16
            kind = stat.S_IFMT(mode)
            _require(
                kind in {0, stat.S_IFREG, stat.S_IFDIR}
                and (kind != stat.S_IFDIR or name.endswith("/"))
                and (kind != stat.S_IFREG or not name.endswith("/")),
                "unsafe archive node",
            )
            _require(
                name.startswith("footman/")
                or re.match(r"footman-[0-9]+\.[0-9]+\.[0-9]+\.dist-info/", name),
                "unsupported archive install scheme",
            )
            if name.startswith("footman/") and name.endswith(".py"):
                parts = path.with_suffix("").parts
                _require(
                    all(part.isidentifier() for part in parts), "invalid module path"
                )
                module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
                modules.append(
                    {
                        "name": module,
                        "source_path": name,
                        "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
                    }
                )
        distributions = {
            PurePosixPath(name).parts[0]
            for name in names
            if not name.startswith("footman/")
        }
        _require(len(distributions) == 1, "ambiguous wheel distribution")
        metadata = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        _require(len(metadata) == 1, "ambiguous entry point metadata")
        config = configparser.ConfigParser()
        config.read_string(archive.read(metadata[0]).decode())
        target = config["console_scripts"]["footman"]
        _require(target == "footman.startup:main", "unexpected console target")
    mapping = {item["name"] for item in modules}
    _require(
        len(mapping) == len(modules)
        and {"footman", "footman.startup", "footman.process", "footman.__main__"}
        <= mapping,
        "missing or duplicate package mapping",
    )
    return sorted(modules, key=lambda item: item["source_path"]), target


class _Workspace:
    """One externally reserved leg; files are retained, descriptors scoped.

    The caller supplies an existing empty external directory and owns its storage
    lifetime. This object never deletes it, including after partial setup. Every
    command has one process owner and consumes the original setup deadline.
    """

    def __init__(self, root, directory, *, timeout_seconds=120):
        _require(
            type(timeout_seconds) in (int, float)
            and math.isfinite(timeout_seconds)
            and timeout_seconds > 0,
            "workspace timeout must be finite and positive",
        )
        self.root = Path(root).absolute()
        self.directory = Path(directory).absolute()
        _require(
            self.root == self.root.resolve()
            and self.directory == self.directory.resolve()
            and not self.directory.is_relative_to(self.root),
            "workspace must be canonical and external",
        )
        self.ops = PosixDescriptorOps()
        self.deadline = time.monotonic() + timeout_seconds
        self.commands = []
        self.owners = []
        self.provisioning_started = False
        self.provisioned = False
        self.provenance_started = False
        self.inventory = []
        self.generated = []
        self.suite_paths = ()
        self.profile = None
        self.started = False
        self.ready = False
        self.environment_extra = {}
        self.junit = None
        self.venv = self.directory / "venv"
        self.python = self.venv / "bin/python"
        self.ini = self.directory / "host.ini"

    def _read(self, path):
        with DirectoryDescriptor.open_secure_path(self.ops, path.parent) as parent:
            name = parent.child_basename(path.name)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            label = "host workspace input"
            fd = os.open(name, flags, dir_fd=parent.fd)
            with OwnedDescriptor(self.ops, fd, label=label) as owner:
                _require(stat.S_ISREG(os.fstat(owner.fd).st_mode), "unsafe input")
                data = bytearray()
                while chunk := os.read(owner.fd, 65536):
                    data.extend(chunk)
                return bytes(data)

    def _write(self, path, data):
        with DirectoryDescriptor.open_secure_path(self.ops, path.parent) as parent:
            name = parent.child_basename(path.name)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            label = "host workspace exclusive output"
            fd = os.open(name, flags, 0o600, dir_fd=parent.fd)
            with OwnedDescriptor(self.ops, fd, label=label) as owner:
                remaining = memoryview(data)
                while remaining:
                    count = os.write(owner.fd, remaining)
                    _require(count > 0, "workspace write made no progress")
                    remaining = remaining[count:]
                os.fsync(owner.fd)
            os.fsync(parent.fd)

    def _generate(self, path, data):
        record = {
            "path": path.relative_to(self.directory).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        # Attempted paths are accounting, not deletion authority. A failed
        # write/close may leave partial bytes and must remain visible to callers.
        record["state"] = "attempted"
        self.generated.append(record)
        try:
            self._write(path, data)
        except BaseException:
            record["state"] = "failed"
            raise
        record["state"] = "complete"

    def _mkdir(self, path):
        with DirectoryDescriptor.open_secure_path(self.ops, path.parent) as parent:
            with parent.create_directory(path.name, mode=0o700):
                pass

    def _host_files(self):
        files = []

        def visit(parent, prefix):
            for name in sorted(parent.list_names()):
                if name in _CACHES:
                    continue
                info = parent.stat_child(name)
                relative = prefix + "/" + name
                if stat.S_ISDIR(info.st_mode):
                    with parent.open_directory(name) as child:
                        visit(child, relative)
                else:
                    _require(stat.S_ISREG(info.st_mode), "unsafe host suite entry")
                    files.append(relative)

        with DirectoryDescriptor.open_secure_path(
            self.ops, self.root / "host_tests"
        ) as host:
            visit(host, "host_tests")
        return tuple(files)

    def prepare(self, *, profile="normal", expectation=None, prepared=False):
        _require(not self.started, "workspace preparation is once-only")
        _require(profile in ("normal", "negative"), "unknown copy profile")
        _require(
            (profile == "negative") == (expectation is not None),
            "profile/input mismatch",
        )
        self.started = True
        with DirectoryDescriptor.open_secure_path(
            self.ops, self.directory
        ) as directory:
            _require(
                set(directory.list_names()) == ({"host_tests"} if prepared else set()),
                "workspace must be empty or explicitly prepared",
            )
            if prepared:
                with directory.open_directory("host_tests") as host:
                    _require(
                        set(host.list_names())
                        == (
                            {"expectation_input.json"}
                            if profile == "negative"
                            else set()
                        ),
                        "unexpected prepared host input",
                    )
                    if profile == "negative":
                        _require(
                            json.loads(
                                self._read(
                                    self.directory / "host_tests/expectation_input.json"
                                ),
                                object_pairs_hook=_unique,
                            )
                            == expectation,
                            "prepared expectation mismatch",
                        )
        files = self._host_files()
        _require(
            "host_tests/expectation_input.json" not in files,
            "unexpected source override input",
        )
        _require(
            "host_tests/conftest.py" in files and "host_tests/__init__.py" in files,
            "missing host support",
        )
        if profile == "negative":
            _require(_NEGATIVE_SUITE in files, "missing negative suite")
            # Resource-free validation by the copied assertion authority happens
            # in host fixtures; preparation accepts only a schema-shaped input.
            _require(type(expectation) is dict, "expectation must be an object")
            files = tuple(
                f
                for f in files
                if not Path(f).name.startswith("test_") or f == _NEGATIVE_SUITE
            )
            self.suite_paths = (_NEGATIVE_SUITE,)
        else:
            files = tuple(f for f in files if f != _NEGATIVE_SUITE)
            self.suite_paths = ("host_tests",)
        self.profile = profile
        for name in ("host_tests", "tests"):
            if name != "host_tests" or not prepared:
                self._mkdir(self.directory / name)
        directories = {"host_tests", "tests"}
        for relative in (*files, *_SUPPORT_FILES):
            for parent in reversed(Path(relative).parents):
                if str(parent) != "." and str(parent) not in directories:
                    self._mkdir(self.directory / parent)
                    directories.add(str(parent))
            data = self._read(self.root / relative)
            if relative.endswith(".py"):
                tree = ast.parse(data)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        imports = [a.name for a in node.names]
                        if isinstance(node, ast.ImportFrom):
                            imports.append(node.module or "")
                        _require(
                            not any("mcp_protocol_child" in value for value in imports),
                            "development protocol dependency",
                        )
            self._write(self.directory / relative, data)
            self.inventory.append(
                {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}
            )
        self._generate(self.ini, _HOST_INI)
        if expectation is not None and not prepared:
            self._generate(
                self.directory / "host_tests/expectation_input.json",
                json.dumps(expectation).encode(),
            )
        self.verify_copy()
        self.ready = True
        return self

    def verify_copy(self):
        if self.profile == "normal":
            with DirectoryDescriptor.open_secure_path(
                self.ops, self.directory / "host_tests"
            ) as host:
                _require(
                    not {
                        "expectation_input.json",
                        "test_negative_expectations.py",
                    }.intersection(host.list_names()),
                    "negative input leaked into normal workspace",
                )
        for record in self.inventory:
            for base in (self.root, self.directory):
                _require(
                    hashlib.sha256(self._read(base / record["path"])).hexdigest()
                    == record["sha256"],
                    "host support copy changed: " + record["path"],
                )
        _require(self._read(self.ini) == _HOST_INI, "host configuration changed")

    def _command(self, argv):
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0, "original workspace deadline exhausted")
        env = _environment() | self.environment_extra
        record = {
            "argv": list(map(str, argv)),
            "cwd": str(self.directory),
            "environment_keys": sorted(env),
            "status": None,
            "cleanup_complete": False,
        }
        self.commands.append(record)
        output = {s: bytearray() for s in ProcessStream}
        ends = {}
        errors = []

        def chunk(stream, data):
            try:
                output[stream].extend(data)
            except BaseException as error:
                errors.append(error)
                raise

        owner = None
        try:
            with OwnedProcess.launch(
                record["argv"],
                deadline_seconds=remaining - min(5, remaining / 2),
                grace_seconds=min(5, remaining / 2),
                capture_limit=0,
                popen_kwargs={"cwd": str(self.directory), "env": env},
            ) as owner:
                self.owners.append(owner)
                result = owner.stream(
                    chunk, on_stream_end=lambda s, end: ends.__setitem__(s, end)
                )
                record["status"] = result.returncode
                record["cleanup_complete"] = owner.cleanup_status.complete
                if errors:
                    raise errors[0]
                if result.lifecycle.failures:
                    raise result.lifecycle.failures[0].error
            record["cleanup_complete"] = result.lifecycle.complete
            _require(
                result.lifecycle.complete
                and not result.lifecycle.failures
                and not result.timed_out
                and all(ends.get(s) is StreamOutcome.EOF for s in ProcessStream),
                "incomplete workspace command",
            )
        finally:
            if owner is not None:
                record["cleanup_complete"] = owner.cleanup_status.complete
                if record["status"] is None:
                    record["status"] = getattr(owner, "returncode", None)
            record["terminals"] = {
                stream.name: end.name for stream, end in ends.items()
            }
            record["stdout"] = bytes(output[ProcessStream.STDOUT])
            record["stderr"] = bytes(output[ProcessStream.STDERR])
        return record

    def provision(self, wheel, wheel_sha256, *, interpreter):
        _require(self.ready, "workspace not prepared")
        _require(not self.provisioning_started, "provisioning is once-only")
        self.provisioning_started = True
        with DirectoryDescriptor.open_secure_path(
            self.ops, self.directory
        ) as directory:
            _require("venv" not in directory.list_names(), "venv must be fresh")
        wheel = Path(wheel).absolute()
        archive = self._read(wheel)
        _require(
            hashlib.sha256(archive).hexdigest() == wheel_sha256,
            "wheel binding mismatch",
        )
        _archive_modules(archive)
        lock = self._read(self.root / "uv.lock")
        project = self._read(self.root / "pyproject.toml")
        packages = tomllib.loads(lock.decode())["package"]
        versions = {p["name"]: p["version"] for p in packages if p["name"] != "footman"}
        dev = tomllib.loads(project.decode())["project"]["optional-dependencies"]["dev"]
        for name in ("pytest", "pytest-xdist"):
            _require(name + "==" + versions[name] in dev, "dev/lock pin disagreement")
        constraints = self.directory / "locked-constraints.txt"
        self._generate(
            constraints,
            (
                "\n".join(f"{k}=={v}" for k, v in sorted(versions.items())) + "\n"
            ).encode(),
        )
        for argv in (
            ["uv", "venv", "--python", str(interpreter), str(self.venv)],
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(self.python),
                "--constraint",
                str(constraints),
                str(wheel),
                "pytest==" + versions["pytest"],
                "pytest-xdist==" + versions["pytest-xdist"],
            ],
        ):
            _require(
                hashlib.sha256(self._read(wheel)).hexdigest() == wheel_sha256,
                "wheel changed before provisioning",
            )
            result = self._command(argv)
            _require(
                result["status"] == 0, "workspace provisioning failed: " + repr(result)
            )
        _require(
            self._read(self.root / "uv.lock") == lock
            and self._read(self.root / "pyproject.toml") == project,
            "dependency inputs changed",
        )
        _require(
            hashlib.sha256(self._read(wheel)).hexdigest() == wheel_sha256,
            "wheel changed during provisioning",
        )
        self.verify_copy()
        self.provisioned = True

    def prepare_provenance(self, wheel, build_manifest):
        """Derive expectations from the bound archive, never installed RECORD."""
        _require(self.provisioned, "workspace not provisioned")
        _require(not self.provenance_started, "provenance preparation is once-only")
        self.provenance_started = True
        self.verify_copy()
        wheel = Path(wheel).absolute()
        binding = json.loads(
            self._read(Path(build_manifest)), object_pairs_hook=_unique
        )
        _require(
            type(binding.get("schema_version")) is int
            and binding["schema_version"] == 1,
            "invalid build manifest version",
        )
        bound = binding.get("wheel_sha256")
        _require(
            isinstance(bound, str) and re.fullmatch(r"[0-9a-f]{64}", bound),
            "invalid build wheel digest",
        )
        _require(binding.get("wheel_path") == str(wheel), "build wheel path mismatch")
        data = self._read(wheel)
        _require(
            hashlib.sha256(data).hexdigest() == bound,
            "archive does not match build binding",
        )
        # Validate the entire archive before asking the installed interpreter
        # for its layout; no archive code is imported or executed.
        modules, target = _archive_modules(data)
        queried = self._command(
            [
                self.python,
                "-P",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ]
        )
        _require(queried["status"] == 0, "install scheme query failed")
        site = Path(queried["stdout"].decode().strip())
        _require(
            site.is_absolute()
            and site.is_relative_to(self.venv)
            and site == site.resolve(),
            "wrong installed scheme",
        )
        script = self.venv / "bin/footman"
        launcher = self._read(script)
        _require(
            ast.dump(ast.parse(_launcher_body(launcher, self.python)))
            == ast.dump(ast.parse(_LAUNCHER)),
            "unsupported installed launcher semantics",
        )
        manifest = {
            "schema_version": 1,
            "context": "wheel",
            "wheel_sha256": bound,
            "modules": [
                item | {"expected_path": str(site / item["source_path"])}
                for item in modules
            ],
            "entrypoint": {
                "script_path": str(script),
                "target": target,
                "observed_script_sha256": hashlib.sha256(launcher).hexdigest(),
            },
        }
        origin = self.directory / "expected-origin.json"
        encoded = json.dumps(
            manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
        self._generate(origin, encoded)
        _require(self._read(origin) == encoded, "expected-origin readback mismatch")
        _require(
            self._read(wheel) == data, "bound wheel changed during provenance setup"
        )
        self.verify_copy()
        neutral = self.directory / "neutral-service"
        self._mkdir(neutral)
        self.host_context = {
            "venv_python": str(self.python),
            "server_script": str(script),
            "support_root": str(self.directory),
            "support_inventory": list(self.inventory),
            "expected_origin_path": str(origin),
            "wheel_sha256": bound,
            "provenance_evidence_path": str(self.directory / "provenance.json"),
            "neutral_cwd": str(neutral),
            # The pytest owner ends execution five seconds before the outer
            # deadline; leave a further five seconds for parent evidence/exit.
            "service_deadline_monotonic": self.deadline - 10,
        }
        self._generate(
            self.directory / "host-context.json", json.dumps(self.host_context).encode()
        )
        return manifest

    def run_pytest(self):
        _require(self.provisioned, "workspace not provisioned")
        self.verify_copy()
        result = self._command(
            [
                self.python,
                "-P",
                "-m",
                "pytest",
                "-c",
                self.ini,
                *([] if self.junit is None else ["--junitxml", str(self.junit)]),
                *(self.directory / p for p in self.suite_paths),
                "-n",
                "8" if self.profile == "normal" else "1",
                "--dist",
                "worksteal",
                "--tb=short",
                "-q",
            ]
        )
        self.verify_copy()
        # Exit 5 (zero items) and skip-policy failures are real unsuccessful
        # outcomes, never normalized to passing host acceptance.
        return result


def _counts(workspace):
    counts = dict.fromkeys(
        ("collected", "passed", "failed", "skipped", "xfailed", "xpassed"), 0
    )
    tree = ET.fromstring(workspace._read(workspace.junit))
    for case in tree.iter("testcase"):
        counts["collected"] += 1
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
        elif case.find("skipped") is not None:
            skipped = case.find("skipped")
            counts[
                "xfailed" if skipped.get("type") == "pytest.xfail" else "skipped"
            ] += 1
        else:
            counts["passed"] += 1
    return counts


def _verify_installed(workspace, manifest):
    initializer = next(
        item for item in manifest["modules"] if item["name"] == "footman"
    )
    package = Path(initializer["expected_path"]).parent
    _require(
        package.is_relative_to(workspace.venv),
        "installed package outside fresh environment",
    )
    _, records = Tooling(package.parent).inventory(("footman/",))
    _require(
        all(item["kind"] in {"directory", "file"} for item in records),
        "unsafe installed package node",
    )
    actual = {
        item["path"]: item["sha256"]
        for item in records
        if item["kind"] == "file" and item["path"].endswith(".py")
    }
    expected = {item["source_path"]: item["sha256"] for item in manifest["modules"]}
    _require(
        actual == expected, "installed full package source-set mismatch before smoke"
    )


def _flow_artifacts(workspace, filename):
    """Find only exclusive per-flow outputs, never follow replaced flow roots."""
    paths = []
    if workspace.host_context.get("pytest_workers", 1) > 1:
        with DirectoryDescriptor.open_secure_path(
            workspace.ops, workspace.directory
        ) as root:
            for name in sorted(root.list_names()):
                if name.startswith("host-runs-"):
                    _require(
                        re.fullmatch(r"host-runs-[0-9a-f]{32}", name) is not None,
                        "invalid host flow root name",
                    )
                    _require(
                        stat.S_ISDIR(root.stat_child(name).st_mode),
                        "unsafe host flow root",
                    )
                    paths.append(workspace.directory / name / filename)
    # Primitive/probe suites use the original direct-parent evidence contract;
    # real host fixtures always reserve a flow root before launching a service.
    return paths or [workspace.directory / filename]


def _verified_evidence(workspace):
    paths = _flow_artifacts(workspace, "provenance.json")
    for path in paths:
        _verified_evidence_file(workspace, path)
    workspace.verified_evidence_paths = list(map(str, paths))
    return True


def _verified_evidence_file(workspace, path):
    expected = json.loads(
        workspace._read(workspace.directory / "expected-origin.json"),
        object_pairs_hook=_unique,
    )
    retained = json.loads(
        workspace._read(path),
        object_pairs_hook=_unique,
    )
    sources = sorted(
        (
            {
                "path": item["source_path"].removeprefix("footman/"),
                "sha256": item["sha256"],
            }
            for item in expected["modules"]
        ),
        key=lambda item: item["path"],
    )
    attestation = {
        "schema_version": 1,
        "source_count": len(sources),
        "set_sha256": hashlib.sha256(canonical(sources)).hexdigest(),
    }
    _require(
        type(retained) is dict
        and set(retained)
        == {
            "schema_version",
            "source_count",
            "set_sha256",
            "sources",
            "context",
            "wheel_sha256",
            "attestation",
            "argv",
        }
        and type(retained.get("schema_version")) is int
        and type(retained.get("source_count")) is int
        and type(retained.get("attestation")) is dict
        and type(retained["attestation"].get("schema_version")) is int
        and type(retained["attestation"].get("source_count")) is int
        and retained.get("sources") == sources
        and retained.get("attestation") == attestation
        and all(retained.get(key) == value for key, value in attestation.items())
        and retained.get("wheel_sha256") == expected["wheel_sha256"]
        and retained.get("wheel_sha256") == workspace.host_context["wheel_sha256"]
        and retained.get("context") == "wheel"
        and type(retained.get("argv")) is list
        and len(retained["argv"]) == 10
        and retained["argv"][:9]
        == [
            str(workspace.python),
            "-P",
            str(workspace.directory / "tests/mcp_server_child.py"),
            "--expected-origin",
            str(workspace.directory / "expected-origin.json"),
            "--entrypoint",
            str(workspace.venv / "bin/footman"),
            "--",
            "--config",
        ],
        "inconsistent retained provenance",
    )
    return True


def _execute_prepared_host_leg(leg):
    """Private typed-input execution shared by CLI and both orchestrator profiles.

    Existing prepared directories remain caller-owned; this function retains
    generated files but never deletes a resource or changes process globals.
    """
    root = checked_root(__file__, "run-host-acceptance")
    _require(leg.profile in {"normal", "negative"}, "invalid prepared profile")
    _require(
        isinstance(leg.image, str)
        and re.fullmatch(r"(?:sha256:[0-9A-Fa-f]{64}|[0-9A-Fa-f]{12,64})", leg.image),
        "invalid configured image ID",
    )
    budget = _leg_budget(leg.profile)
    tool = Tooling(root, timeout_seconds=budget["total_seconds"])
    evidence = tool.external(leg.evidence)
    directory = tool.external(leg.workspace)
    _require(
        not directory.is_relative_to(evidence)
        and not evidence.is_relative_to(directory),
        "overlapping prepared domains",
    )
    expected = (
        (directory / "host_tests",)
        if leg.profile == "normal"
        else (directory / _NEGATIVE_SUITE,)
    )
    _require(
        type(leg.suite_paths) is tuple and leg.suite_paths == expected,
        "incomplete or unsafe prepared suite paths",
    )
    _require(
        (leg.case_id is None) == (leg.profile == "normal"), "case/profile mismatch"
    )
    wheel = Path(leg.wheel).absolute()
    manifest = None
    workspace = _Workspace(root, directory)
    workspace.deadline = tool.deadline
    report = {
        "schema_version": 1,
        "budget": budget,
        "repository_root": str(root),
        "wrapper_path": str(root / "scripts/run-host-acceptance"),
        "caller_cwd": os.getcwd(),
        # Unknown until the initial independent binding check succeeds.
        "source_identity": None,
        "build_input_identity": None,
        "revision": None,
        "build_manifest_path": str(leg.source_identity),
        "wheel_path": str(wheel),
        "wheel_sha256": None,
        "configured_image_id": leg.image,
        "observed_image_id": None,
        "success": False,
        "cleanup_complete": False,
        "commands": [],
        "warnings": [],
    }
    primary = None
    result = None
    verified = False
    counts = dict.fromkeys(
        ("collected", "passed", "failed", "skipped", "xfailed", "xpassed"), 0
    )
    try:
        # Safe external domains/report state exist before prerequisite/provenance
        # work: even an early failure must retain acquired command observations
        # and attempt the same independent evidence finalization below.
        for name in ("uv", "git", "docker"):
            _require(
                shutil.which(name) is not None, "missing host prerequisite: " + name
            )
        manifest = tool.load(leg.source_identity)
        compare_binding(tool, manifest, wheel)
        report.update(
            source_identity=manifest["source_identity"],
            build_input_identity=manifest["build_input_identity"],
            revision=manifest["revision"],
            wheel_sha256=manifest["wheel_sha256"],
        )
        # Archive preflight must still precede all install acquisition.
        _archive_modules(tool.read(wheel))
        for argv in (["git", "--version"], ["docker", "--version"]):
            record = tool.command(argv)
            _require(record["status"] == 0, "host prerequisite failed: " + repr(record))
        expectation = None
        if leg.profile == "negative":
            expectation = tool.load(directory / "host_tests/expectation_input.json")
            _require(
                type(expectation) is dict
                and set(expectation)
                == {"schema_version", "case_id", "expected_override"}
                and type(expectation["schema_version"]) is int
                and expectation["schema_version"] == 1
                and expectation["case_id"] == leg.case_id
                and leg.case_id
                in {"staged_bytes", "argv", "command_status", "cleanup"},
                "invalid prepared expectation",
            )
            override = expectation["expected_override"]
            _require(
                (
                    isinstance(override, str)
                    if leg.case_id == "staged_bytes"
                    else type(override) is list
                    and all(isinstance(item, str) and item for item in override)
                    if leg.case_id == "argv"
                    else type(override) is int
                    if leg.case_id == "command_status"
                    else type(override) is bool
                ),
                "invalid prepared expectation value",
            )
        with DirectoryDescriptor.open_secure_path(workspace.ops, directory) as existing:
            prepared = "host_tests" in existing.list_names()
        workspace.prepare(
            profile=leg.profile, expectation=expectation, prepared=prepared
        )
        compare_binding(tool, manifest, wheel)
        workspace.provision(wheel, manifest["wheel_sha256"], interpreter=sys.executable)
        origin = workspace.prepare_provenance(wheel, leg.source_identity)
        _verify_installed(workspace, origin)
        workspace.host_context["configured_image_id"] = leg.image
        workspace.host_context["leg_budget"] = budget
        workspace.host_context["pytest_workers"] = 8 if leg.profile == "normal" else 1
        workspace.host_context["service_observations_path"] = str(
            directory / "service-observations.json"
        )
        # The original context file is exclusive and already retained. Publish
        # the augmented fixture context at a distinct recorded path, no rewrite.
        context = directory / "execution-context.json"
        workspace._generate(context, canonical(workspace.host_context))
        workspace.environment_extra = {"FOOTMAN_HOST_CONTEXT": str(context)}
        attribution = workspace._command(
            [
                workspace.python,
                "-P",
                "-c",
                "import importlib.metadata as m,json,sys; import footman,footman.process; "
                "print(json.dumps({'python':sys.version,'interpreter':sys.executable,'package':footman.__file__,'process':footman.process.__file__,'dependencies':{n:m.version(n) for n in ('mcp','pytest','pytest-xdist')}}))",
            ]
        )
        _require(attribution["status"] == 0, "installed import smoke failed")
        imported = json.loads(attribution["stdout"])
        paths = {item["name"]: item["expected_path"] for item in origin["modules"]}
        _require(
            imported["interpreter"] == str(workspace.python)
            and imported["package"] == paths["footman"]
            and imported["process"] == paths["footman.process"],
            "installed import origins disagree with bound wheel",
        )
        report["environment"] = imported | {"os": platform.platform()}
        for argv in (
            [workspace.venv / "bin/footman", "--help"],
            [workspace.python, "-P", "-m", "footman", "--help"],
        ):
            smoke = workspace._command(argv)
            _require(smoke["status"] == 0, "installed help smoke failed")
        workspace.junit = directory / "pytest.xml"
        result = workspace.run_pytest()
        counts = _counts(workspace)
        xpasses = re.findall(
            rb"(?:^|[ ,])(\d+) xpassed(?:[, ]|$)", result["stdout"], re.MULTILINE
        )
        if xpasses:
            counts["xpassed"] = int(xpasses[-1])
            counts["passed"] -= counts["xpassed"]
        if result["status"] == 0 or leg.profile == "negative":
            verified = _verified_evidence(workspace)
        _require(counts["collected"] > 0, "zero host items collected")
        _require(
            not any(counts[name] for name in ("skipped", "xfailed", "xpassed")),
            "host skip policy failed",
        )
    except BaseException as error:
        primary = error
    failures = CleanupFailures(primary)

    def image_attribution_file(path):
        if not os.path.lexists(path):
            report["warnings"].append("observed image metadata unavailable")
            return
        try:
            value = tool.load(path)
        except json.JSONDecodeError:
            report["warnings"].append("unusable observed image metadata")
            return
        observed = value.get("observed_image_id") if isinstance(value, dict) else None
        if isinstance(observed, str) and re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", observed
        ):
            report["observed_image_id"] = observed
        else:
            report["warnings"].append("unusable observed image metadata")
        report["image_metadata"] = value
        report.setdefault("worker_image_metadata", []).append(
            {"path": str(path), "metadata": value}
        )

    def image_attribution():
        paths = (
            _flow_artifacts(workspace, "service-observations.json")
            if workspace.provenance_started
            else [directory / "service-observations.json"]
        )
        for path in paths:
            image_attribution_file(path)
        report["worker_provenance_evidence_paths"] = getattr(
            workspace, "verified_evidence_paths", []
        )

    failures.run(image_attribution, ordinary=(Exception,))
    if manifest is not None:
        failures.run(
            lambda: compare_binding(tool, manifest, wheel), ordinary=(Exception,)
        )
    if workspace.ready:
        failures.run(workspace.verify_copy, ordinary=(Exception,))
    report.update(
        venv=str(workspace.venv),
        expected_origin_path=str(directory / "expected-origin.json"),
        provenance_evidence_path=next(
            iter(getattr(workspace, "verified_evidence_paths", [])), None
        ),
        counts=counts,
        inventory=workspace.inventory,
        generated=workspace.generated,
    )
    report["cleanup_complete"] = all(
        owner.cleanup_status.complete and not owner.cleanup_status.failures
        for owner in (*tool.owners, *workspace.owners)
    )
    report["pytest_status"] = None if result is None else result["status"]
    report["success"] = (
        primary is None
        and result is not None
        and result["status"] == 0
        and report["cleanup_complete"]
    )

    for index, record in enumerate((*tool.commands, *workspace.commands)):
        prefix = f"command-{index}-"
        # Keep raw status/metadata even if one of this command's writes fails.
        # Paths describe attempted destinations, not proof of retained bytes.
        report["commands"].append(tool.command_evidence(record, evidence, prefix))
        failures.run(
            lambda record=record, prefix=prefix: tool.retain_command(
                record, evidence, prefix
            ),
            ordinary=(Exception,),
        )
    try:
        selected = failures.complete()
    except BaseException as error:
        selected = error
    if selected is None:
        selected = primary
    if selected is not None:
        report["success"] = False
        report["warnings"].append(type(selected).__name__ + ": " + str(selected))
    publication = CleanupFailures(selected)
    publication.run(
        lambda: tool.write(evidence / "report.json", canonical(report)),
        ordinary=(Exception,),
    )
    published = publication.complete()
    if published is not None:
        selected = published
    if selected is not None:
        raise selected
    return SimpleNamespace(
        pytest_status=result["status"],
        counts=counts,
        failure_kind=None
        if result["status"] == 0
        else "assertion"
        if counts["failed"]
        else "policy",
        cleanup_complete=report["cleanup_complete"],
        evidence_complete=verified,
        stdout=result["stdout"],
        stderr=result["stderr"],
        commands=report["commands"],
        success=report["success"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--source-identity", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(argv)
    root = checked_root(__file__, "run-host-acceptance")
    _require(
        re.fullmatch(r"(?:sha256:[0-9A-Fa-f]{64}|[0-9A-Fa-f]{12,64})", args.image),
        "invalid configured image ID",
    )
    tool = Tooling(root)
    evidence = tool.external(args.evidence)
    directory = evidence.parent / (evidence.name + "-workspace")
    workspace = _Workspace(root, directory)
    workspace._mkdir(directory)
    leg = SimpleNamespace(
        wheel=args.wheel.absolute(),
        source_identity=args.source_identity.absolute(),
        image=args.image,
        evidence=evidence,
        workspace=directory,
        profile="normal",
        suite_paths=(directory / "host_tests",),
        case_id=None,
    )
    result = _execute_prepared_host_leg(leg)
    return (
        result.pytest_status
        if result.pytest_status != 0
        else 0
        if result.success
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
