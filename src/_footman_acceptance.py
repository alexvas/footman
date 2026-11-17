"""Private checkout-only acceptance tooling, excluded from the Footman wheel.

The wrappers share this module through their single validated root/src bootstrap,
not a scripts package or a second import root. It is not a runtime/public API.
Local acceptance-tool attribution remains separate from runtime dispatch policy.

These helpers never select artifacts by scanning dist or remove retained output.
All descriptors and children have exactly one foundation release authority.
"""

import hashlib
import json
import os
import re
import stat
import sys
import time
import tomllib
from pathlib import Path

import footman
from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import OwnedProcess, ProcessStream, StreamOutcome

SOURCE_ROOTS = (
    "src/",
    "tests/",
    "host_tests/",
    "scripts/",
    "pyproject.toml",
    "uv.lock",
)
BUILD_ROOTS = ("src/", "pyproject.toml", "README.md")
CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".mypy_cache", ".venv"}
BUILD_ENV_ALLOWLIST = {
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "UV_CACHE_DIR",
    "UV_NO_PROGRESS",
    "UV_HTTP_TIMEOUT",
    "UV_HTTP_RETRIES",
    "UV_NATIVE_TLS",
    "UV_SYSTEM_CERTS",
    "UV_LINK_MODE",
    "UV_PYTHON_DOWNLOADS",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field: " + key)
        result[key] = value
    return result


def clean_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PYTEST", "GIT_", "FOOTMAN_BOOTSTRAP_"))
    } | {
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }


def checked_root(implementation, wrapper):
    lexical = Path(implementation).absolute()
    root = lexical.resolve(strict=True).parents[1]
    require(
        lexical == lexical.resolve() and lexical.parent.name == "scripts",
        "wrapper/implementation root disagreement",
    )
    source = root / "src"
    require(
        Path(__file__) == source / "_footman_acceptance.py"
        and Path(__file__).resolve() == Path(__file__),
        "bootstrap tooling root disagreement",
    )
    require(
        Path(footman.__file__) == source / "footman/__init__.py",
        "bootstrap foundation root disagreement",
    )
    for name, module in tuple(sys.modules.items()):
        if name == "footman" or name.startswith("footman."):
            origin = getattr(module, "__file__", None)
            require(
                origin is not None
                and Path(origin).is_relative_to(source)
                and Path(origin).resolve() == Path(origin),
                "wrong-root foundation: " + name,
            )
    path = root / "scripts" / wrapper
    require(
        path.is_file() and not path.is_symlink() and path.resolve() == path,
        "unsafe wrapper root",
    )
    return root


class Tooling:
    """One invocation's owners, original deadline and externally retained files."""

    def __init__(self, root, *, timeout_seconds=120):
        self.root = Path(root)
        self.ops = PosixDescriptorOps()
        self.deadline = time.monotonic() + timeout_seconds
        self.commands = []
        self.owners = []

    def read_child(self, parent, name):
        name = parent.child_basename(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        label = "acceptance attribution input"
        fd = os.open(name, flags, dir_fd=parent.fd)
        with OwnedDescriptor(self.ops, fd, label=label) as owner:
            require(stat.S_ISREG(os.fstat(owner.fd).st_mode), "input must be regular")
            chunks = []
            while chunk := os.read(owner.fd, 65536):
                chunks.append(chunk)
            return b"".join(chunks)

    def read(self, path):
        path = Path(path)
        with DirectoryDescriptor.open_secure_path(self.ops, path.parent) as parent:
            return self.read_child(parent, path.name)

    def load(self, path):
        return json.loads(self.read(path), object_pairs_hook=unique)

    def write(self, path, data):
        path = Path(path)
        with DirectoryDescriptor.open_secure_path(self.ops, path.parent) as parent:
            name = parent.child_basename(path.name)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            label = "acceptance attribution evidence"
            fd = os.open(name, flags, 0o600, dir_fd=parent.fd)
            with OwnedDescriptor(self.ops, fd, label=label) as owner:
                remaining = memoryview(data)
                while remaining:
                    count = os.write(owner.fd, remaining)
                    require(count > 0, "evidence write made no progress")
                    remaining = remaining[count:]
                os.fsync(owner.fd)
            os.fsync(parent.fd)
        require(self.read(path) == data, "evidence readback mismatch")

    def external(self, path):
        path = Path(path).absolute()
        require(path == path.resolve(), "unsafe external evidence path")
        require(not path.is_relative_to(self.root), "evidence inside checkout")
        for scope in (*SOURCE_ROOTS, "README.md"):
            require(
                not path.is_relative_to(self.root / scope.rstrip("/")),
                "evidence inside identity roots",
            )
        return path

    def inventory(self, roots):
        records = []
        excluded = (
            {"__pycache__", ".pytest_cache", ".ruff_cache"}
            if roots == BUILD_ROOTS
            else CACHES
        )

        def visit(parent, name, relative):
            info = parent.stat_child(name)
            value = {"path": relative, "executable": info.st_mode & 0o111}
            if stat.S_ISLNK(info.st_mode):
                records.append(
                    value
                    | {"kind": "link", "target": os.readlink(name, dir_fd=parent.fd)}
                )
            elif stat.S_ISREG(info.st_mode):
                records.append(
                    value
                    | {
                        "kind": "file",
                        "sha256": hashlib.sha256(
                            self.read_child(parent, name)
                        ).hexdigest(),
                    }
                )
            elif stat.S_ISDIR(info.st_mode):
                records.append(value | {"kind": "directory"})
                with parent.open_directory(name) as child:
                    for nested in sorted(child.list_names()):
                        if nested not in excluded:
                            visit(child, nested, relative + "/" + nested)
            else:
                raise ValueError("unsupported identity node: " + relative)

        with DirectoryDescriptor.open_secure_path(self.ops, self.root) as directory:
            for entry in roots:
                name = entry.rstrip("/")
                info = directory.stat_child(name)
                require(
                    stat.S_ISDIR(info.st_mode)
                    if entry.endswith("/")
                    else stat.S_ISREG(info.st_mode),
                    "missing or unsafe identity root: " + entry,
                )
                visit(directory, name, name)
        records.sort(key=lambda record: record["path"])
        return {
            "schema_version": 1,
            "roots": list(roots),
            "sha256": hashlib.sha256(canonical(records)).hexdigest(),
        }, records

    def command(self, argv, *, cwd=None):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "original tooling deadline exhausted")
        env = clean_environment()
        record = {
            "argv": list(map(str, argv)),
            "cwd": str(self.root if cwd is None else cwd),
            "status": None,
            "attempted": True,
            "environment_keys": sorted(env),
            "PATH": env.get("PATH"),
            "cleanup_complete": False,
        }
        self.commands.append(record)
        output = {stream: bytearray() for stream in ProcessStream}
        ends = {}
        failures = []

        def chunk(stream, data):
            try:
                output[stream].extend(data)
            except BaseException as error:
                failures.append(error)
                raise

        owner = None
        try:
            with OwnedProcess.launch(
                record["argv"],
                deadline_seconds=remaining - min(5, remaining / 2),
                grace_seconds=min(5, remaining / 2),
                capture_limit=0,
                popen_kwargs={"cwd": record["cwd"], "env": env},
            ) as owner:
                self.owners.append(owner)
                result = owner.stream(
                    chunk,
                    on_stream_end=lambda stream, end: ends.__setitem__(stream, end),
                )
                record["status"] = result.returncode
                if failures:
                    raise failures[0]
                if result.lifecycle.failures:
                    raise result.lifecycle.failures[0].error
                require(
                    not result.timed_out
                    and all(
                        ends.get(stream) is StreamOutcome.EOF
                        for stream in ProcessStream
                    ),
                    "incomplete tooling output",
                )
        finally:
            if owner is not None and record["status"] is None:
                record["status"] = owner.returncode
            record.update(
                stdout=bytes(output[ProcessStream.STDOUT]),
                stderr=bytes(output[ProcessStream.STDERR]),
                cleanup_complete=owner is not None and owner.cleanup_status.complete,
                terminals={
                    stream.name: outcome.name for stream, outcome in ends.items()
                },
            )
        require(record["cleanup_complete"], "incomplete tooling cleanup")
        return record

    def revision(self):
        record = self.command(["git", "rev-parse", "HEAD"])
        require(
            record["status"] == 0
            and re.fullmatch(rb"[0-9a-f]{40,64}\n?", record["stdout"]),
            "unresolved repository revision",
        )
        return record["stdout"].decode().strip()

    def local_changes(self):
        record = self.command(["git", "status", "--porcelain", "--untracked-files=all"])
        require(record["status"] == 0, "Git local-change observation failed")
        return bool(record["stdout"])

    def predict(self):
        profile = tomllib.loads(self.read(self.root / "pyproject.toml").decode())
        project = profile["project"]
        require(
            profile["build-system"]
            == {"requires": ["hatchling==1.27.0"], "build-backend": "hatchling.build"},
            "unsupported build backend",
        )
        require(
            project["name"] == "footman"
            and re.fullmatch(
                r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                project["version"],
            )
            and project.get("readme") == "README.md"
            and not project.get("dynamic"),
            "unsupported static project profile",
        )
        require(
            profile.get("tool", {}).get("hatch")
            == {"build": {"targets": {"wheel": {"packages": ["src/footman"]}}}},
            "unsupported hatch output profile",
        )
        require(
            not any(
                key.startswith("HATCH_")
                or (key.startswith("UV_") and key not in BUILD_ENV_ALLOWLIST)
                for key in os.environ
            ),
            "unsupported ambient build settings",
        )
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        )
        configs = [
            config_home / "uv/uv.toml",
            Path("/etc/uv/uv.toml"),
            self.root / "uv.toml",
        ]
        configs.extend(
            Path(part) / "uv/uv.toml"
            for part in os.environ.get("XDG_CONFIG_DIRS", "").split(os.pathsep)
            if part
        )
        require(
            all(path.is_absolute() and not os.path.lexists(path) for path in configs)
            and "uv" not in profile.get("tool", {}),
            "unsupported discovered uv configuration",
        )
        version = project["version"]
        return (
            self.root / "dist" / f"footman-{version}.tar.gz",
            self.root / "dist" / f"footman-{version}-py3-none-any.whl",
        )

    def dist_inventory(self):
        with DirectoryDescriptor.open_secure_path(self.ops, self.root) as directory:
            if "dist" not in directory.list_names():
                return []
            with directory.open_directory("dist") as dist:
                result = []
                for name in sorted(dist.list_names()):
                    info = dist.stat_child(name)
                    record = {
                        "path": str(self.root / "dist" / name),
                        "kind": stat.S_IFMT(info.st_mode),
                    }
                    if stat.S_ISREG(info.st_mode):
                        record["sha256"] = hashlib.sha256(
                            self.read_child(dist, name)
                        ).hexdigest()
                    elif stat.S_ISLNK(info.st_mode):
                        record["target"] = os.readlink(name, dir_fd=dist.fd)
                    result.append(record)
                return result

    def command_evidence(self, record, directory, prefix):
        """Describe intended evidence paths without asserting successful writes."""
        result = {
            key: value
            for key, value in record.items()
            if key not in {"stdout", "stderr"}
        }
        for name in ("stdout", "stderr"):
            if name in record:
                result[name + "_path"] = str(directory / (prefix + name + ".txt"))
        return result

    def retain_command(self, record, directory, prefix):
        failures = CleanupFailures(sys.exception())
        result = self.command_evidence(record, directory, prefix)
        for name in ("stdout", "stderr"):
            if name in record:
                path = directory / (prefix + name + ".txt")
                failures.run(
                    lambda path=path, name=name: self.write(path, record[name]),
                    ordinary=(Exception,),
                )
        selected = failures.complete()
        if selected is not None:
            raise selected
        return result


def validate_identity(value, roots):
    require(
        type(value) is dict
        and set(value) == {"schema_version", "roots", "sha256"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["roots"] == list(roots)
        and isinstance(value["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]),
        "malformed input identity",
    )


def compare_binding(tool, manifest, wheel):
    require(
        type(manifest) is dict
        and type(manifest.get("schema_version")) is int
        and manifest["schema_version"] == 1,
        "invalid build manifest",
    )
    for key, roots in (
        ("source_identity", SOURCE_ROOTS),
        ("build_input_identity", BUILD_ROOTS),
    ):
        try:
            validate_identity(manifest.get(key), roots)
        except ValueError as error:
            raise ValueError(key + ": " + str(error)) from error
        require(tool.inventory(roots)[0] == manifest[key], "stale " + key)
    require(manifest.get("revision") == tool.revision(), "stale repository revision")
    require(str(wheel) == manifest.get("wheel_path"), "build wheel path mismatch")
    require(
        hashlib.sha256(tool.read(wheel)).hexdigest() == manifest.get("wheel_sha256"),
        "wheel_sha256/build digest mismatch",
    )
