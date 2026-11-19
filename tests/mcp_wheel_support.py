"""Independent Phase-9 build/install fixture; never imports later acceptance code."""

import configparser
import fcntl
import hashlib
import io
import math
import os
import re
import stat
import sys
import time
import tomllib
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import OwnedProcess, ProcessStream, StreamOutcome
from tests import mcp_provenance as p
from tests.admission_support import AdmissionPool
from tests.build_observation_values import freeze

ROOTS = ("src/", "pyproject.toml", "README.md")
WHEEL_ONLY_SUPPORT_FILES = ("tests/mcp_wheel_client.py",)
OPS = PosixDescriptorOps()
BUILD_SLOT_PATH = None  # pytest supplies one shared, fixture-owned path per invocation.


@contextmanager
def build_slot(path, *, deadline):
    """Admit one provisioning leg; waiting consumes the original setup budget."""
    if path is None:
        yield
        return
    if time.monotonic() >= deadline:
        raise TimeoutError("build admission envelope exhausted")
    path = Path(path)
    with DirectoryDescriptor.open_secure_path(OPS, path.parent) as parent:
        name = parent.child_basename(path.name)
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        label = "MCP build admission"
        fd = os.open(name, flags, 0o600, dir_fd=parent.fd)
        with OwnedDescriptor(OPS, fd, label=label) as owner:
            info = os.fstat(owner.fd)
            p.require(
                stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(),
                "unsafe build admission node",
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("build admission envelope exhausted")
                try:
                    fcntl.flock(owner.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.01, remaining))
            yield
        # Closing the sole descriptor releases the lock; no retry/unlock owner.


def provision_workspace(
    workspace, wheel, wheel_sha256, *, admission: AdmissionPool | None, interpreter
):
    """Test-only admission; the borrowed workspace keeps its original deadline/owners."""
    if not isinstance(admission, AdmissionPool):
        raise ValueError("missing shared workspace provisioning admission")
    with admission.acquire(deadline=workspace.deadline):
        return workspace.provision(wheel, wheel_sha256, interpreter=interpreter)


# Reviewed transport/cache/concurrency controls cannot select build destinations.
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
    environment: Path | None = None
    expected_origin: dict | None = None
    support_root: Path | None = None
    setup_operations: tuple = ()
    support_inventory: tuple = ()
    dependency_identity: str | None = None

    def __post_init__(self):
        for name in ("build_input_identity", "inventory", "command"):
            object.__setattr__(self, name, freeze(getattr(self, name)))


def reuse_for_test(base, destination, *, mutable=False, private_support=False):
    """Reuse an immutable worker install; destructive controls get a CoW copy.

    This is a clone of the recorded installation, not another claimed install.
    Only the public Footman launcher is used/relocated; activation and unrelated
    dependency CLI launchers are deliberately outside this fixture's contract.
    """
    destination = Path(destination)
    end = time.monotonic() + 120

    def remaining():
        value = end - time.monotonic()
        if value <= 0:
            raise TimeoutError("installation clone setup envelope exhausted")
        return value

    support = base.support_root
    if private_support:
        support = destination / "copied"
        (support / "tests").mkdir(parents=True, mode=0o700)
        with build_slot(BUILD_SLOT_PATH, deadline=end):
            for item in base.support_inventory:
                copy_support_file(
                    base.support_root / item["path"], support / item["path"]
                )
    manifest = __import__("json").loads(p.canonical(base.expected_origin))
    environment = base.environment
    operations = base.setup_operations
    if mutable:
        environment = destination / "venv"
        environment.mkdir(mode=0o700)
        env = clean_environment()
        with build_slot(BUILD_SLOT_PATH, deadline=end):
            copied = command(
                [
                    "cp",
                    "--archive",
                    "--reflink=auto",
                    "--no-preserve=ownership",
                    str(base.environment) + "/.",
                    str(environment),
                ],
                cwd=destination,
                env=env,
                timeout_seconds=remaining(),
            )
        p.require(copied["status"] == 0, f"installation clone failed: {copied}")
        scheme = command(
            [
                str(environment / "bin/python"),
                "-P",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            cwd=destination,
            env=env,
            timeout_seconds=remaining(),
        )
        p.require(scheme["status"] == 0, f"clone layout failed: {scheme}")
        site = Path(scheme["stdout"].decode().strip())
        p.require(
            site.is_relative_to(environment), "clone interpreter retained original site"
        )
        modules, target = archive_expectations(base.wheel, base.wheel_sha256, site)
        script = environment / "bin/footman"
        data = p.read(script)
        p.require(data.startswith(b"#!"), "unsupported cloned launcher")
        script.unlink()
        p.write(
            script,
            b"#!"
            + os.fsencode(environment / "bin/python")
            + b"\n"
            + data.split(b"\n", 1)[1],
        )
        script.chmod(0o700)
        manifest = {
            "schema_version": 1,
            "context": "wheel",
            "wheel_sha256": base.wheel_sha256,
            "modules": modules,
            "entrypoint": {
                "script_path": str(script),
                "target": target,
                "observed_script_sha256": hashlib.sha256(p.read(script)).hexdigest(),
            },
        }
        p.manifest_set(manifest)
        operations = (*operations, copied, scheme)
    return replace(
        base,
        environment=environment,
        expected_origin=manifest,
        support_root=support,
        setup_operations=operations,
    )


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


def build_and_install_contract_wheel(
    repository_root,
    *,
    admission: AdmissionPool | None,
    evidence_directory,
    timeout_seconds=120,
    attribution_only=False,
    env=None,
):
    if type(timeout_seconds) not in {int, float}:
        raise TypeError("setup timeout must be numeric and not boolean")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("setup timeout must be finite and positive")
    end = time.monotonic() + timeout_seconds
    if attribution_only:
        context = nullcontext()
    else:
        if not isinstance(admission, AdmissionPool):
            raise ValueError("installation requires an AdmissionPool")
        context = admission.acquire(deadline=end)
    with context:
        return _build_and_install_contract_wheel(
            repository_root,
            evidence_directory=evidence_directory,
            deadline=end,
            timeout_seconds=timeout_seconds,
            attribution_only=attribution_only,
            env=env,
        )


def _build_and_install_contract_wheel(
    repository_root,
    *,
    evidence_directory,
    deadline,
    timeout_seconds=120,
    attribution_only=False,
    env=None,
):
    if type(timeout_seconds) not in {int, float}:
        raise TypeError("setup timeout must be numeric and not boolean")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("setup timeout must be finite and positive")
    end = deadline

    def remaining():
        value = end - time.monotonic()
        if value <= 0:
            raise TimeoutError("original build/install envelope exhausted")
        return value

    root = Path(repository_root).absolute()
    evidence = Path(evidence_directory).absolute()
    p.require(
        root == root.resolve() and evidence == evidence.resolve(),
        "build/evidence paths must be canonical",
    )
    p.require(not evidence.is_relative_to(root), "build evidence must be external")
    environment = clean_environment(env)
    before, supplied = inventory(root)
    sdist, wheel = predict(root, environment)
    dist = root / "dist"
    if os.path.lexists(dist):
        p.require(stat.S_ISDIR(dist.lstat().st_mode), "unsafe dist ancestry")
    conflicts = [str(path) for path in (sdist, wheel) if os.path.lexists(path)]
    if conflicts:
        raise BuildFailure(
            f"root {root}: conflicts {conflicts}; no build/deletion/replacement occurred",
            {
                "attempted": False,
                "status": None,
                "conflicts": conflicts,
                "build_input_identity": before,
            },
        )
    primary = None
    observation = None
    try:
        observation = command(
            ["uv", "build"], cwd=root, env=environment, timeout_seconds=remaining()
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
        primary.add_note("post-build input verification also failed: " + str(error))
    if primary is not None:
        retained = getattr(primary, "observation", None)
        if retained is not None:
            retained.update(attempted=True, pre_identity=before, post_identity=after)
        raise primary
    p.require(observation is not None, "missing attempted-build observation")
    observation.update(attempted=True, pre_identity=before, post_identity=after)
    if observation["status"] != 0 or before != after:
        raise BuildFailure("build failed or relevant inputs changed", observation)
    output = (observation["stdout"] + observation["stderr"]).decode("utf-8", "strict")
    attributed = re.findall(r"^Successfully built (.+)$", output, re.MULTILINE)
    expected = [str(sdist.relative_to(root)), str(wheel.relative_to(root))]
    if len(attributed) != 2 or sorted(attributed) != sorted(expected):
        raise BuildFailure(
            "missing/ambiguous/stale successful output attribution", observation
        )
    sdist_digest = hashlib.sha256(p.read(sdist)).hexdigest()
    wheel_bytes = p.read(wheel)
    bound = hashlib.sha256(wheel_bytes).hexdigest()
    result = BuildObservation(
        root, wheel, bound, sdist, sdist_digest, before, tuple(supplied), observation
    )
    if attribution_only:
        return result
    # Validate bound archive paths/metadata BEFORE creating an installation.
    archive_expectations(wheel, bound, Path("/archive-validation-only"))
    venv = evidence / "venv"
    candidate = Path(__file__).resolve().parents[1]
    lock_bytes = p.read(candidate / "uv.lock")
    dependency_identity = hashlib.sha256(lock_bytes).hexdigest()
    packages = tomllib.loads(lock_bytes.decode("utf-8"))["package"]
    constraints = sorted(
        {
            f"{item['name']}=={item['version']}"
            for item in packages
            if item["name"] != "footman"
        }
    )
    constraint_file = evidence / "locked-constraints.txt"
    p.write(constraint_file, ("\n".join(constraints) + "\n").encode())
    setup_operations = []
    for args in (
        ["uv", "venv", "--python", sys.executable, str(venv)],
        [
            "uv",
            "pip",
            "install",
            "--constraint",
            str(constraint_file),
            "--python",
            str(venv / "bin/python"),
            str(wheel),
            "anyio==4.16.0",
        ],
    ):
        p.require(
            hashlib.sha256(p.read(wheel)).hexdigest() == bound,
            "bound wheel changed before provisioning",
        )
        op = command(args, cwd=evidence, env=environment, timeout_seconds=remaining())
        setup_operations.append(op)
        p.require(op["status"] == 0, f"wheel provisioning failed: {op}")
    p.require(
        hashlib.sha256(p.read(wheel)).hexdigest() == bound,
        "bound wheel changed during provisioning",
    )
    queried = command(
        [
            str(venv / "bin/python"),
            "-P",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        cwd=evidence,
        env=environment,
        timeout_seconds=remaining(),
    )
    setup_operations.append(queried)
    p.require(queried["status"] == 0, "install scheme query failed")
    site = Path(queried["stdout"].decode().strip())
    modules, target = archive_expectations(wheel, bound, site)
    script = venv / "bin/footman"
    manifest = {
        "schema_version": 1,
        "context": "wheel",
        "wheel_sha256": bound,
        "modules": modules,
        "entrypoint": {
            "script_path": str(script),
            "target": target,
            "observed_script_sha256": hashlib.sha256(p.read(script)).hexdigest(),
        },
    }
    p.manifest_set(manifest)
    support_root = evidence / "copied"
    (support_root / "tests").mkdir(parents=True, mode=0o700)
    support_inventory = []
    for name in (*p.SUPPORT_FILES, *WHEEL_ONLY_SUPPORT_FILES):
        copy_support_file(candidate / name, support_root / name)
        support_inventory.append(
            {
                "path": name,
                "sha256": hashlib.sha256(p.read(candidate / name)).hexdigest(),
                "executable": (candidate / name).lstat().st_mode & 0o111,
            }
        )
    p.require(
        hashlib.sha256(p.read(candidate / "uv.lock")).hexdigest()
        == dependency_identity,
        "dependency inputs changed during setup",
    )
    p.require(inventory(root)[0] == before, "inputs changed during provisioning/copy")
    p.require(
        not (support_root / "tests/mcp_protocol_child.py").exists()
        and not (support_root / "tests/mcp_wheel_support.py").exists(),
        "development-only fixture leaked into client closure",
    )
    return BuildObservation(
        root,
        wheel,
        bound,
        sdist,
        sdist_digest,
        before,
        tuple(supplied),
        observation,
        venv,
        manifest,
        support_root,
        setup_operations=tuple(setup_operations),
        support_inventory=tuple(support_inventory),
        dependency_identity=dependency_identity,
    )
