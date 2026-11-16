"""Private isolated host-workspace machinery.

Public bootstrap, binding/provenance authority and prepared-leg orchestration
are separate delivery tasks. Importing this module acquires no resources.
"""

import ast
import hashlib
import json
import math
import os
import stat
import time
import tomllib
from pathlib import Path

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
_HOST_INI = b"[pytest]\naddopts = --strict-config --strict-markers\n"
_CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST", "FOOTMAN_", "GIT_"))
    } | {"PYTHONNOUSERSITE": "1"}


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
        self.inventory = []
        self.generated = []
        self.suite_paths = ()
        self.profile = None
        self.started = False
        self.ready = False
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
        self._write(path, data)
        self.generated.append(record)

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

    def prepare(self, *, profile="normal", expectation=None):
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
            _require(not directory.list_names(), "workspace must be empty")
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
        if expectation is not None:
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
        env = _environment()
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

        try:
            with OwnedProcess.launch(
                record["argv"],
                deadline_seconds=remaining / 2,
                grace_seconds=min(5, remaining / 2),
                capture_limit=0,
                popen_kwargs={"cwd": str(self.directory), "env": env},
            ) as owner:
                self.owners.append(owner)
                result = owner.stream(
                    chunk, on_stream_end=lambda s, end: ends.__setitem__(s, end)
                )
                record["status"] = result.returncode
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
        _require(
            hashlib.sha256(self._read(wheel)).hexdigest() == wheel_sha256,
            "wheel binding mismatch",
        )
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
                *(self.directory / p for p in self.suite_paths),
                "-n",
                "1",
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
