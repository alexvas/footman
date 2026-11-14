"""Independent Phase-10 build binding; no Phase-9 or future host-helper imports."""

import configparser
import hashlib
import io
import math
import os
import re
import stat
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import OwnedProcess, ProcessStream, StreamOutcome
from tests import mcp_provenance as p
from tests.build_observation_values import freeze

ROOTS = ("src/", "pyproject.toml", "README.md")
OPS = PosixDescriptorOps()
BUILD_ENV_ALLOWLIST = frozenset(
    {
        "UV_CACHE_DIR",
        "UV_LINK_MODE",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_DEFAULT_INDEX",
        "UV_INDEX",
        "UV_OFFLINE",
        "UV_HTTP_TIMEOUT",
        "UV_HTTP_RETRIES",
        "UV_NATIVE_TLS",
        "UV_SYSTEM_CERTS",
        "UV_INSECURE_HOST",
        "UV_NO_PROGRESS",
        "UV_COLOR",
        "UV_CONCURRENT_BUILDS",
        "UV_CONCURRENT_DOWNLOADS",
        "UV_CONCURRENT_INSTALLS",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_DOWNLOADS",
        "UV_PYTHON",
    }
)


def inventory(root):
    root = Path(root)
    entries = []

    def walk(directory, relative):
        for name in sorted(directory.list_names()):
            if name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
                continue
            info = directory.stat_child(name)
            path = relative / name
            record = {"path": path.as_posix(), "executable": info.st_mode & 0o111}
            if stat.S_ISDIR(info.st_mode):
                entries.append(record | {"kind": "directory"})
                with directory.open_directory(name) as child:
                    walk(child, path)
            elif stat.S_ISLNK(info.st_mode):
                entries.append(
                    record
                    | {"kind": "link", "target": os.readlink(name, dir_fd=directory.fd)}
                )
            elif stat.S_ISREG(info.st_mode):
                label = "build input"
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory.fd)
                with OwnedDescriptor(OPS, fd, label=label) as owner:
                    digest = hashlib.sha256()
                    while chunk := os.read(owner.fd, 65536):
                        digest.update(chunk)
                entries.append(record | {"kind": "file", "sha256": digest.hexdigest()})
            else:
                raise ValueError("unsupported build input node")

    with DirectoryDescriptor.open_secure_path(OPS, root) as directory:
        for name in ROOTS:
            base = name.rstrip("/")
            info = directory.stat_child(base)
            if base == "src":
                p.require(stat.S_ISDIR(info.st_mode), "src must be a real directory")
                entries.append(
                    {
                        "path": "src",
                        "kind": "directory",
                        "executable": info.st_mode & 0o111,
                    }
                )
                with directory.open_directory(base) as child:
                    walk(child, Path(base))
            else:
                p.require(
                    stat.S_ISREG(info.st_mode),
                    f"{base} must be a regular non-symlink file",
                )
                entries.append(
                    {
                        "path": base,
                        "kind": "file",
                        "executable": info.st_mode & 0o111,
                        "sha256": hashlib.sha256(p.read(root / base)).hexdigest(),
                    }
                )
    entries.sort(key=lambda item: item["path"])
    return {
        "schema_version": 1,
        "roots": list(ROOTS),
        "sha256": hashlib.sha256(p.canonical(entries)).hexdigest(),
    }, entries


def clean_environment(extra=None):
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
            "PYTHONINSPECT",
            "PYTHONSAFEPATH",
        }
    }
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra or {})
    env = {
        key: value
        for key, value in env.items()
        if not key.startswith(("PYTHON", "FOOTMAN_BOOTSTRAP_"))
    }
    env["PYTHONNOUSERSITE"] = "1"
    return env


def command(argv, *, cwd, env, timeout_seconds):
    if type(timeout_seconds) not in {int, float}:
        raise TypeError("helper timeout must be numeric and not boolean")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("helper timeout must be finite and positive")
    started = time.monotonic()
    data = {stream: bytearray() for stream in ProcessStream}
    terminals = {}
    sink_failure = None

    def chunk(stream, payload):
        nonlocal sink_failure
        if sink_failure is not None:
            return
        try:
            data[stream].extend(payload)
        except BaseException as error:
            sink_failure = error
            if not isinstance(error, Exception):
                raise

    reserve = min(2.0, timeout_seconds / 4)
    with OwnedProcess.launch(
        list(map(str, argv)),
        deadline_seconds=timeout_seconds - reserve,
        grace_seconds=reserve,
        capture_limit=0,
        popen_kwargs={"cwd": str(cwd), "env": env},
    ) as owner:
        result = owner.stream(
            chunk,
            on_stream_end=lambda stream, outcome: terminals.__setitem__(
                stream, outcome
            ),
        )
    observation = {
        "argv": list(map(str, argv)),
        "started_monotonic": started,
        "ended_monotonic": time.monotonic(),
        "cwd": str(cwd),
        "PATH": env.get("PATH"),
        "status": result.returncode,
        "stdout": bytes(data[ProcessStream.STDOUT]),
        "stderr": bytes(data[ProcessStream.STDERR]),
    }
    if sink_failure is not None:
        raise sink_failure
    if not (
        result.lifecycle.complete
        and not result.lifecycle.failures
        and not result.timed_out
        and all(terminals.get(s) is StreamOutcome.EOF for s in ProcessStream)
    ):
        error = BuildFailure("build/install helper lifecycle failure", observation)
        error.owner = owner
        if result.lifecycle.failures:
            raise error from result.lifecycle.failures[0].error
        raise error
    return observation


class BuildFailure(Exception):
    def __init__(self, message, observation):
        super().__init__(message + "; observation=" + repr(observation))
        self.observation = observation


def predict(root, env):
    profile = tomllib.loads(p.read(root / "pyproject.toml").decode("utf-8"))
    project = profile["project"]
    p.require(
        profile["build-system"]
        == {"requires": ["hatchling==1.27.0"], "build-backend": "hatchling.build"},
        "unsupported build backend",
    )
    p.require(
        project["name"] == "footman"
        and re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            project["version"],
        )
        and project.get("readme") == "README.md"
        and not project.get("dynamic"),
        "unsupported static project profile",
    )
    p.require(
        profile.get("tool", {}).get("hatch")
        == {"build": {"targets": {"wheel": {"packages": ["src/footman"]}}}},
        "unsupported output-changing hatch profile",
    )
    unsupported = sorted(
        key
        for key in env
        if key.startswith("HATCH_")
        or (key.startswith("UV_") and key not in BUILD_ENV_ALLOWLIST)
    )
    p.require(
        not unsupported, "unsupported ambient build settings: " + ", ".join(unsupported)
    )
    config_home = Path(
        env.get(
            "XDG_CONFIG_HOME", str(Path(env.get("HOME", str(Path.home()))) / ".config")
        )
    )
    configs = [config_home / "uv/uv.toml", Path("/etc/uv/uv.toml")]
    configs.extend(
        Path(part) / "uv/uv.toml"
        for part in env.get("XDG_CONFIG_DIRS", "").split(os.pathsep)
        if part
    )
    p.require(
        all(path.is_absolute() for path in configs)
        and not any(os.path.lexists(path) for path in configs),
        "unsupported discovered uv configuration",
    )
    p.require(
        not os.path.lexists(root / "uv.toml") and "uv" not in profile.get("tool", {}),
        "unsupported uv config",
    )
    version = project["version"]
    return (
        root / "dist" / f"footman-{version}.tar.gz",
        root / "dist" / f"footman-{version}-py3-none-any.whl",
    )


def archive_expectations(path, bound, site):
    def inspect(data):
        p.require(
            hashlib.sha256(data).hexdigest() == bound,
            "archive does not match build binding",
        )
        modules = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            p.require(len(names) == len(set(names)), "duplicate wheel members")
            for name in names:
                member = PurePosixPath(name)
                p.require(
                    not member.is_absolute()
                    and ".." not in member.parts
                    and "\\" not in name,
                    "unsafe archive member",
                )
                info = archive.getinfo(name)
                mode = info.external_attr >> 16
                p.require(
                    stat.S_IFMT(mode) in {0, stat.S_IFREG, stat.S_IFDIR},
                    "symlink/special wheel member",
                )
                p.require(
                    name.startswith("footman/")
                    or re.match(r"footman-[0-9]+\.[0-9]+\.[0-9]+\.dist-info/", name),
                    "unsupported wheel install scheme",
                )
                if name.startswith("footman/") and name.endswith(".py"):
                    relative = name[len("footman/") :]
                    modules.append(
                        {
                            "name": p.module_name(relative),
                            "source_path": name,
                            "expected_path": str(site / name),
                            "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
                        }
                    )
            metadata = [
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            ]
            p.require(len(metadata) == 1, "ambiguous entry point metadata")
            config = configparser.ConfigParser()
            config.read_string(archive.read(metadata[0]).decode("utf-8"))
            target = config["console_scripts"]["footman"]
            p.require(
                target == "footman.startup:main", "unexpected wheel console entry"
            )
        module_names = {item["name"] for item in modules}
        p.require(
            len(module_names) == len(modules)
            and {"footman", "footman.startup", "footman.process", "footman.__main__"}
            <= module_names,
            "missing or duplicate package module mapping",
        )
        p.source_set(
            [
                {
                    "path": item["source_path"][len("footman/") :],
                    "sha256": item["sha256"],
                }
                for item in modules
            ]
        )
        return modules, target

    return p.read(path, transform=inspect)


def copy_support_file(source, destination):
    data = p.read(source)
    p.write(destination, data)
    mode = Path(source).lstat().st_mode & 0o777
    os.chmod(destination, mode, follow_symlinks=False)
    p.require(
        p.read(destination) == data
        and Path(destination).lstat().st_mode & 0o111 == mode & 0o111,
        "support copy identity mismatch",
    )


@dataclass(frozen=True)
class BuildObservation:
    repository_root: Path
    wheel: Path
    wheel_sha256: str
    sdist: Path
    sdist_sha256: str
    build_input_identity: dict
    inventory: tuple
    command: dict

    def __post_init__(self):
        for name in ("build_input_identity", "inventory", "command"):
            object.__setattr__(self, name, freeze(getattr(self, name)))


def build_and_bind_wheel(repository_root, *, evidence_directory, timeout_seconds):
    if type(timeout_seconds) not in {int, float}:
        raise TypeError("build timeout must be numeric and not boolean")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("build timeout must be finite and positive")
    deadline = time.monotonic() + timeout_seconds
    root, evidence = (
        Path(repository_root).absolute(),
        Path(evidence_directory).absolute(),
    )
    p.require(
        root == root.resolve() and evidence == evidence.resolve(),
        "noncanonical build paths",
    )
    p.require(not evidence.is_relative_to(root), "build evidence must be external")
    env = clean_environment()
    before, supplied = inventory(root)
    sdist, wheel = predict(root, env)
    if os.path.lexists(root / "dist"):
        p.require(stat.S_ISDIR((root / "dist").lstat().st_mode), "unsafe dist ancestry")
    observation = {
        "argv": ["uv", "build"],
        "cwd": str(root),
        "PATH": env.get("PATH"),
        "attempted": False,
        "status": None,
        "stdout": b"",
        "stderr": b"",
        "pre_identity": before,
        "post_identity": before,
    }
    conflicts = [str(path) for path in (sdist, wheel) if os.path.lexists(path)]
    if conflicts:
        observation["conflicts"] = conflicts
        raise BuildFailure(
            f"root {root}: conflicts {conflicts}; no build/deletion/replacement occurred",
            observation,
        )
    primary = None
    try:
        observation = command(
            ["uv", "build"],
            cwd=root,
            env=env,
            timeout_seconds=deadline - time.monotonic(),
        )
    except BaseException as error:
        primary = error
    else:
        observation.update(attempted=True, pre_identity=before, post_identity=None)
        if observation["status"] != 0:
            primary = BuildFailure("build exited unsuccessfully", observation)
    after = None
    try:
        after, _ = inventory(root)
    except BaseException as error:
        if primary is None:
            error.observation = dict(
                observation, attempted=True, pre_identity=before, post_identity=None
            )
            raise
        if not isinstance(error, Exception):
            raise error from primary
        if isinstance(primary, BuildFailure):
            primary.post_check_error = error
        primary.add_note("post-build verification failed: " + str(error))
    if primary is not None:
        retained = getattr(primary, "observation", None)
        if retained is not None:
            retained.update(attempted=True, pre_identity=before, post_identity=after)
        raise primary
    observation.update(attempted=True, pre_identity=before, post_identity=after)
    if observation["status"] != 0 or before != after:
        raise BuildFailure("build failed or relevant inputs changed", observation)
    output = (observation["stdout"] + observation["stderr"]).decode("utf-8", "strict")
    attributed = re.findall(r"^Successfully built (.+)$", output, re.MULTILINE)
    if len(attributed) != 2 or sorted(attributed) != sorted(
        [str(sdist.relative_to(root)), str(wheel.relative_to(root))]
    ):
        raise BuildFailure(
            "missing/ambiguous/stale successful output attribution", observation
        )
    return BuildObservation(
        root,
        wheel,
        hashlib.sha256(p.read(wheel)).hexdigest(),
        sdist,
        hashlib.sha256(p.read(sdist)).hexdigest(),
        before,
        tuple(supplied),
        observation,
    )
