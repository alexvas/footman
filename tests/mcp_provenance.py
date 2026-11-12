"""Test-only full-package source attribution and bounded stderr attestation.

SUPPORT_FILES is the declared copy closure; no development protocol peer imports.
"""

import hashlib
import importlib.util
import json
import os
import re
import stat
from pathlib import Path

from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps

SUPPORT_FILES = (
    "tests/__init__.py",
    "tests/green_skip_policy.py",
    "tests/mcp_provenance.py",
    "tests/mcp_server_child.py",
    "tests/mcp_transport.py",
    "tests/mcp_lifecycle.py",
)
PREFIX = b"FOOTMAN_PROVENANCE_"
WIRE = b"FOOTMAN_PROVENANCE_V1 "
HEX = re.compile(r"[0-9a-f]{64}\Z")
OPS = PosixDescriptorOps()


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
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read(path, *, transform=lambda data: data):
    path = Path(path)
    with DirectoryDescriptor.open_secure_path(OPS, path.parent) as parent:
        label = "MCP input"
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(parent.child_basename(path.name), flags, dir_fd=parent.fd)
        with OwnedDescriptor(OPS, fd, label=label) as owner:
            require(
                stat.S_ISREG(os.fstat(owner.fd).st_mode), "input must be a regular file"
            )
            chunks = []
            while chunk := os.read(owner.fd, 65536):
                chunks.append(chunk)
            return transform(b"".join(chunks))


def write(path, data):
    path = Path(path)
    with DirectoryDescriptor.open_secure_path(OPS, path.parent) as parent:
        label = "MCP exclusive evidence"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(parent.child_basename(path.name), flags, 0o600, dir_fd=parent.fd)
        with OwnedDescriptor(OPS, fd, label=label) as owner:
            remaining = memoryview(data)
            while remaining:
                count = os.write(owner.fd, remaining)
                require(count > 0, "evidence write made no progress")
                remaining = remaining[count:]
            os.fsync(owner.fd)
        os.fsync(parent.fd)


def load(path):
    return read(
        path,
        transform=lambda data: json.loads(
            data.decode("utf-8"), object_pairs_hook=unique
        ),
    )


def sources(root):
    root = Path(root)
    entries = []

    def walk(directory, relative):
        for name in sorted(directory.list_names()):
            if name == "__pycache__":
                continue
            info = directory.stat_child(name)
            child = relative / name
            if stat.S_ISDIR(info.st_mode):
                with directory.open_directory(name) as nested:
                    walk(nested, child)
            elif stat.S_ISLNK(info.st_mode):
                raise ValueError(f"symlink in package: {child}")
            elif stat.S_ISREG(info.st_mode):
                if name.endswith(".py"):
                    label = "MCP package source"
                    flags = os.O_RDONLY | os.O_NOFOLLOW
                    fd = os.open(name, flags, dir_fd=directory.fd)
                    with OwnedDescriptor(OPS, fd, label=label) as owner:
                        digest = hashlib.sha256()
                        while chunk := os.read(owner.fd, 65536):
                            digest.update(chunk)
                    entries.append(
                        {"path": child.as_posix(), "sha256": digest.hexdigest()}
                    )
            else:
                raise ValueError(f"unsupported package node: {child}")

    with DirectoryDescriptor.open_secure_path(OPS, root) as directory:
        walk(directory, Path())
    return sorted(entries, key=lambda item: item["path"])


def source_set(entries):
    require(isinstance(entries, list), "source set must be an array")
    seen = set()
    for entry in entries:
        require(
            isinstance(entry, dict) and set(entry) == {"path", "sha256"},
            "invalid source entry",
        )
        path = entry["path"]
        require(
            isinstance(path, str)
            and path.endswith(".py")
            and not Path(path).is_absolute()
            and all(p not in {"", ".", ".."} for p in path.split("/")),
            "unsafe source path",
        )
        require(
            path not in seen
            and isinstance(entry["sha256"], str)
            and HEX.fullmatch(entry["sha256"]),
            "duplicate path or malformed digest",
        )
        seen.add(path)
    return sorted(entries, key=lambda item: item["path"])


def attestation(entries):
    encoded = canonical(source_set(entries))
    payload = {
        "schema_version": 1,
        "source_count": len(entries),
        "set_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    record = WIRE + canonical(payload) + b"\n"
    require(len(record) <= 512, "attestation overflow")
    return record


def emit(entries, borrowed):
    record = attestation(entries)
    if borrowed.write(record) != len(record):
        raise OSError("short provenance write")
    borrowed.flush()


class AttestationParser:
    """Ordinary lines are discarded; only bounded reserved candidates retained."""

    def __init__(self, entries):
        self.expected = attestation(entries)
        self.buffer = bytearray()
        self.ordinary = False
        self.reserved = 0
        self.provisional = False
        self.failure = None
        self.terminal = None

    @property
    def buffered_bytes(self):
        return len(self.buffer)

    @property
    def confirmed(self):
        from footman.process import StreamOutcome

        return (
            self.provisional
            and self.failure is None
            and self.terminal is StreamOutcome.EOF
        )

    def feed(self, data):
        for byte in data:
            if self.ordinary:
                if byte == 10:
                    self.ordinary = False
                continue
            self.buffer.append(byte)
            candidate = bytes(self.buffer)
            if len(candidate) <= len(PREFIX) and not PREFIX.startswith(candidate):
                self.buffer.clear()
                self.ordinary = byte != 10
                continue
            if len(candidate) == len(PREFIX):
                self.reserved += 1
                if self.reserved > 1:
                    self.failure = "duplicate reserved line"
            if byte == 10:
                if candidate == self.expected and self.reserved == 1:
                    self.provisional = True
                else:
                    self.failure = self.failure or "malformed or mismatched attestation"
                self.buffer.clear()
            elif len(self.buffer) >= 512:
                self.failure = self.failure or "overlong attestation"
                self.buffer.clear()
                self.ordinary = True

    def end(self, terminal):
        from footman.process import StreamOutcome

        self.terminal = terminal
        if self.buffer:
            self.failure = self.failure or "unterminated reserved candidate"
        if not self.provisional:
            self.failure = self.failure or "missing attestation"
        if terminal is not StreamOutcome.EOF:
            self.failure = self.failure or "incomplete stderr collection"


def module_name(relative):
    parts = Path(relative).with_suffix("").parts
    return (
        ".".join(("footman", *parts[:-1]))
        if parts[-1] == "__init__"
        else ".".join(("footman", *parts))
    )


def development_manifest(root):
    root = Path(root).absolute()
    return {
        "schema_version": 1,
        "context": "development",
        "wheel_sha256": None,
        "entrypoint": None,
        "modules": [
            {
                "name": module_name(item["path"]),
                "source_path": str(root / item["path"]),
                "expected_path": str(root / item["path"]),
                "sha256": item["sha256"],
            }
            for item in sources(root)
        ],
    }


def manifest_set(manifest):
    require(
        isinstance(manifest, dict)
        and set(manifest)
        == {"schema_version", "context", "wheel_sha256", "entrypoint", "modules"},
        "invalid expected-origin keys",
    )
    require(
        type(manifest["schema_version"]) is int and manifest["schema_version"] == 1,
        "invalid expected-origin version",
    )
    require(manifest["context"] in {"development", "wheel"}, "invalid origin context")
    wheel = manifest["wheel_sha256"]
    require(
        wheel is None
        if manifest["context"] == "development"
        else isinstance(wheel, str) and HEX.fullmatch(wheel),
        "invalid wheel binding",
    )
    entrypoint = manifest["entrypoint"]
    if manifest["context"] == "development":
        require(entrypoint is None, "development origin must not select a script")
    else:
        require(
            isinstance(entrypoint, dict)
            and set(entrypoint) == {"script_path", "target", "observed_script_sha256"},
            "invalid wheel entrypoint",
        )
        require(
            isinstance(entrypoint["script_path"], str)
            and Path(entrypoint["script_path"]).is_absolute()
            and entrypoint["target"] == "footman.startup:main"
            and isinstance(entrypoint["observed_script_sha256"], str)
            and HEX.fullmatch(entrypoint["observed_script_sha256"]),
            "malformed installed launcher expectations",
        )
    modules = manifest["modules"]
    require(isinstance(modules, list) and modules, "missing modules")
    initializers = [
        m for m in modules if isinstance(m, dict) and m.get("name") == "footman"
    ]
    require(len(initializers) == 1, "missing/duplicate package initializer")
    root = Path(initializers[0]["expected_path"]).parent
    require(root.is_absolute(), "expected package root must be absolute")
    names, entries = set(), []
    for item in modules:
        require(
            isinstance(item, dict)
            and set(item) == {"name", "source_path", "expected_path", "sha256"},
            "invalid module entry",
        )
        path = Path(item["expected_path"])
        require(
            path.is_absolute() and path.is_relative_to(root),
            "expected path outside package",
        )
        relative = path.relative_to(root).as_posix()
        require(
            item["name"] == module_name(relative) and item["name"] not in names,
            "invalid module mapping",
        )
        require(
            isinstance(item["source_path"], str) and item["source_path"],
            "missing source authority",
        )
        if manifest["context"] == "wheel":
            require(
                item["source_path"] == "footman/" + relative, "unsafe archive mapping"
            )
        else:
            require(item["source_path"] == str(path), "development authority mismatch")
        names.add(item["name"])
        entries.append({"path": relative, "sha256": item["sha256"]})
    require(
        {"footman", "footman.startup", "footman.process", "footman.__main__"} <= names,
        "required modules missing",
    )
    return root, source_set(entries)


def verify(manifest):
    root, expected = manifest_set(manifest)
    spec = importlib.util.find_spec("footman")
    require(
        spec is not None
        and spec.origin == str(root / "__init__.py")
        and tuple(spec.submodule_search_locations or ()) == (str(root),),
        "unexpected package spec/root",
    )
    require(sources(root) == expected, "full package source-set mismatch")
    import sys

    for item in manifest["modules"]:
        module = sys.modules.get(item["name"])
        if module is not None:
            require(
                getattr(module, "__file__", None) == item["expected_path"],
                "imported module origin mismatch",
            )
    return expected


def retain_evidence(path, manifest, parser, argv):
    require(parser.confirmed, "provenance has not reached clean EOF confirmation")
    _, entries = manifest_set(manifest)
    require(
        parser.expected == attestation(entries),
        "attestation belongs to a different expected set",
    )
    payload = json.loads(attestation(entries)[len(WIRE) :])
    value = {
        "schema_version": 1,
        "sources": entries,
        **payload,
        "context": manifest["context"],
        "wheel_sha256": manifest["wheel_sha256"],
        "attestation": payload,
        "argv": list(argv),
    }
    write(path, canonical(value))
    retained = load(path)
    require(
        retained == value
        and canonical(source_set(retained["sources"])) == canonical(entries),
        "persisted evidence mismatch",
    )
    return retained
