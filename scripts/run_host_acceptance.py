"""Private isolated host-workspace machinery.

Public bootstrap, binding/provenance authority and prepared-leg orchestration
are separate delivery tasks. Importing this module acquires no resources.
"""

import ast
import configparser
import hashlib
import io
import json
import math
import os
import re
import stat
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

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
_LAUNCHER = """import sys
from footman.startup import main
if __name__ == "__main__":
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
"""


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST", "FOOTMAN_", "GIT_"))
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
            ast.dump(ast.parse(launcher)) == ast.dump(ast.parse(_LAUNCHER)),
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
