"""Operator-only startup configuration and the phase-only request boundary.

No filesystem reservation or command execution occurs during validation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps

MAX_TIMEOUT_SECONDS = 86400
MAX_CLEANUP_GRACE_SECONDS = 300
_REQUIRED = frozenset(
    {
        "version",
        "repository",
        "container_start_argv",
        "command_argv",
        "working_directory",
        "snapshot_directory",
        "container_snapshot_directory",
        "output_dir",
        "timeout_seconds",
        "cleanup_grace_seconds",
    }
)
_PHASE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class ConfigurationError(ValueError):
    """An operator file could not be loaded or statically validated."""


class RequestError(ValueError):
    """Input is outside the phase-only request capability."""


@dataclass(frozen=True, slots=True)
class Configuration:
    version: int
    repository: Path
    container_start_argv: tuple[str, ...]
    command_argv: tuple[str, ...]
    user: str
    working_directory: str
    snapshot_directory: Path
    container_snapshot_directory: str
    # output_dir is a display path, not a reparsable template: resolved names
    # can contain literal braces. Expand only output_suffix, below output_root.
    output_dir: str
    output_root: Path
    output_suffix: tuple[str, ...]
    timeout_seconds: float
    cleanup_grace_seconds: float

    @property
    def requires_phase(self) -> bool:
        return any("{phase}" in component for component in self.output_suffix)

    @property
    def container_working_directory(self) -> str:
        return posixpath.normpath(
            posixpath.join(
                self.container_snapshot_directory,
                self.working_directory,
            )
        )

    @property
    def identity(self) -> str:
        # Digest only effective explicit fields, never environment or file bytes.
        # Raw argv (which can contain operator secrets) is not exposed by identity.
        fields = {
            "version": self.version,
            "repository": str(self.repository),
            "container_start_argv": self.container_start_argv,
            "command_argv": self.command_argv,
            "user": self.user,
            "working_directory": self.working_directory,
            "snapshot_directory": str(self.snapshot_directory),
            "container_snapshot_directory": self.container_snapshot_directory,
            "output_dir": self.output_dir,
            "output_root": str(self.output_root),
            "output_suffix": self.output_suffix,
            "timeout_seconds": self.timeout_seconds,
            "cleanup_grace_seconds": self.cleanup_grace_seconds,
        }
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigurationError(
            f"configuration {field} must be a nonempty NUL-free string"
        )
    return value


def _argv(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(
            f"configuration {field} must be a nonempty string array"
        )
    if any(not isinstance(arg, str) or "\x00" in arg for arg in value):
        raise ConfigurationError(f"configuration {field} must contain NUL-free strings")
    _string(value[0], field)
    return tuple(value)


def _placeholders(value: str, field: str, allowed: frozenset[str]) -> frozenset[str]:
    names = frozenset(_PLACEHOLDER.findall(value))
    remainder = _PLACEHOLDER.sub("", value)
    if names - allowed or "{" in remainder or "}" in remainder:
        raise ConfigurationError(
            f"configuration {field} contains an unsupported placeholder"
        )
    return names


def _seconds(value: object, field: str, maximum: int) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value <= maximum
        or not math.isfinite(value)
    ):
        raise ConfigurationError(f"configuration {field} must be > 0 and <= {maximum}")
    return float(value)


def _host_path(value: object, field: str, base: Path) -> Path:
    path = Path(_string(value, field))
    try:
        return (base / path).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigurationError(
            f"configuration {field} cannot be canonicalized"
        ) from error


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def validate_configuration(data: Mapping[str, object], path: Path) -> Configuration:
    """Validate schema and canonical existing ancestors without creating resources."""
    unknown = data.keys() - (_REQUIRED | {"user"})
    missing = _REQUIRED - data.keys()
    if unknown or missing:
        raise ConfigurationError(
            f"configuration unknown fields {sorted(unknown)}; missing fields {sorted(missing)}"
        )
    if type(data["version"]) is not int or data["version"] != 1:
        raise ConfigurationError("configuration version must be 1")
    base = path.absolute().parent
    repository = _host_path(data["repository"], "repository", base)
    if not repository.is_dir():
        raise ConfigurationError(
            "configuration repository must be an existing directory"
        )
    snapshot = _host_path(data["snapshot_directory"], "snapshot_directory", base)
    output = _string(data["output_dir"], "output_dir")
    _placeholders(output, "output_dir", frozenset({"phase", "utc_datetime"}))
    # Parse only operator-supplied components. The config directory and names
    # introduced by canonicalization are literal, even if they contain braces.
    # Everything before the first template component is the storage ancestor.
    supplied_output = Path(output)
    prefix = Path(supplied_output.anchor) if supplied_output.is_absolute() else base
    components = (
        supplied_output.parts[1:]
        if supplied_output.is_absolute()
        else supplied_output.parts
    )
    suffix: list[str] = []
    dynamic = False
    for component in components:
        dynamic = dynamic or "{" in component
        if dynamic:
            suffix.append(component)
        else:
            prefix /= component
    depth = 0
    for component in suffix:
        depth += -1 if component == ".." else 1
        if depth < 0:
            raise ConfigurationError(
                "configuration output_dir escapes its storage ancestor"
            )
    output_root = _host_path(str(prefix), "output_dir", base)
    output_template = str(output_root.joinpath(*suffix))
    roots = (repository, snapshot, output_root)
    if any(_overlap(a, b) for i, a in enumerate(roots) for b in roots[i + 1 :]):
        raise ConfigurationError(
            "configuration repository/snapshot_directory/output_dir roots overlap"
        )
    # Existing storage components must be directories; reservation revalidates
    # paths and no-follow containment at request time in the output adapter.
    for field, root in (("snapshot_directory", snapshot), ("output_dir", output_root)):
        ancestor = root
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ConfigurationError(
                f"configuration {field} ancestor must be a directory"
            )
    launch = _argv(data["container_start_argv"], "container_start_argv")
    launch_names = frozenset().union(
        *(
            _placeholders(arg, "container_start_argv", frozenset({"snapshot_path"}))
            for arg in launch
        )
    )
    if "snapshot_path" not in launch_names:
        raise ConfigurationError(
            "configuration container_start_argv requires {snapshot_path}"
        )
    command = _argv(data["command_argv"], "command_argv")
    working = _string(data["working_directory"], "working_directory")
    working = posixpath.normpath(working)
    if working.startswith("/") or working == ".." or working.startswith("../"):
        raise ConfigurationError(
            "configuration working_directory must remain below the snapshot"
        )
    mount = _string(
        data["container_snapshot_directory"], "container_snapshot_directory"
    )
    if not mount.startswith("/"):
        raise ConfigurationError(
            "configuration container_snapshot_directory must be absolute"
        )
    mount = posixpath.normpath(mount)
    return Configuration(
        version=1,
        repository=repository,
        container_start_argv=launch,
        command_argv=command,
        user=_string(data.get("user", "root"), "user"),
        working_directory=working,
        snapshot_directory=snapshot,
        container_snapshot_directory=mount,
        output_dir=output_template,
        output_root=output_root,
        output_suffix=tuple(suffix),
        timeout_seconds=_seconds(
            data["timeout_seconds"], "timeout_seconds", MAX_TIMEOUT_SECONDS
        ),
        cleanup_grace_seconds=_seconds(
            data["cleanup_grace_seconds"],
            "cleanup_grace_seconds",
            MAX_CLEANUP_GRACE_SECONDS,
        ),
    )


def load_configuration(path: str | Path) -> Configuration:
    """Read once through one descriptor owner, retaining operation authority."""
    path = Path(path).absolute()
    ops = PosixDescriptorOps()
    label = "operator configuration"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
        with OwnedDescriptor(ops, fd, label=label) as owner:
            if not stat.S_ISREG(os.fstat(owner.fd).st_mode):
                raise ConfigurationError("configuration load requires a regular file")
            chunks = []
            while chunk := os.read(owner.fd, 65536):
                chunks.append(chunk)
            try:
                data = tomllib.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeError, tomllib.TOMLDecodeError) as error:
                raise ConfigurationError(
                    "configuration load failed: invalid TOML/UTF-8"
                ) from error
            return validate_configuration(data, path)
    except OSError as error:
        raise ConfigurationError(f"configuration load failed: {error}") from error


def validate_request(config: Configuration, value: object) -> str | None:
    if not isinstance(value, dict) or value.keys() - {"phase"}:
        raise RequestError("request must be an object containing only optional phase")
    if "phase" not in value:
        if config.requires_phase:
            raise RequestError("phase is required by output_dir")
        return None
    phase = value["phase"]
    if not isinstance(phase, str) or _PHASE.fullmatch(phase) is None:
        raise RequestError("phase must be a nonempty [A-Za-z0-9_-]+ string")
    return phase


@dataclass(frozen=True, slots=True)
class ConfiguredApplication:
    """Process-lifetime configuration; no request can reload or override it."""

    config: Configuration

    @classmethod
    def from_path(cls, path: str | Path) -> Self:
        return cls(load_configuration(path))

    def accept[T](
        self, value: object, dispatch: Callable[[Configuration, str | None], T]
    ) -> T:
        phase = validate_request(self.config, value)
        return dispatch(self.config, phase)
