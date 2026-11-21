"""Docker-free results accounting, never an operator-run evidence collector.

A reservation loser has no claim. Claims require an independently recorded
successful reservation, not a directory diff. Unknown changes stop deletion.
All descriptor acquisition/release delegates to directory capabilities or the
existing sole-owner JSON helpers. Evidence lives outside the deletion targets.
"""

import hashlib
import json
import os
import stat
from pathlib import Path

from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
from tests import host_acceptance_contract_support as h
from tests import mcp_provenance as p

DEFAULT_BASE = Path("/tmp/gorshochek/ephemeral/footman.git/host-acceptance")


def read_accepted_report(path):
    """Read only the supplied report; validate its local completion envelope.

    Test-support readback, not full host acceptance/provenance verification.
    Parsing and validation run inside the existing sole-owner read scope so an
    ordinary close failure cannot replace a validation error or interruption.
    No directory search, mtime selection or fallback to another report exists.
    """
    path = Path(path)

    def accept(data):
        value = json.loads(data.decode("utf-8"), object_pairs_hook=p.unique)
        h.check(isinstance(value, dict), "report must be an object")
        h.check(
            type(value.get("schema_version")) is int and value["schema_version"] == 1,
            "unsupported report schema",
        )
        h.check(value.get("state") == "final", "report is not final")
        h.check(value.get("success") is True, "report is not successful")
        h.check(value.get("run_id") == path.parent.name, "report run identity mismatch")
        root = value.get("repository_root")
        h.check(
            isinstance(root, str) and Path(root).is_absolute(),
            "report repository root must be absolute",
        )
        return value

    return p.read(path, transform=accept)


def _identity(info):
    return info.st_dev, info.st_ino


def _check_base(owner, base, expected):
    base = Path(base)
    h.check(_identity(os.fstat(owner.fd)) == expected, "results base identity changed")
    # Re-walk the parent securely: lstat(base) would still follow symlinks in
    # intermediate components, even if they lead back to this same inode.
    with DirectoryDescriptor.open_secure_path(p.OPS, base.parent) as parent:
        info = parent.stat_child(base.name, follow_symlinks=False)
        h.check(
            stat.S_ISDIR(info.st_mode) and _identity(info) == expected,
            "results base identity changed",
        )


def _read_file(directory, name, expected):
    """Borrow the directory; give the no-follow leaf fd exactly one owner."""
    base = directory.child_basename(name)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    ops = p.OPS
    label = "results inventory file"
    fd = os.open(base, flags, dir_fd=directory.fd)
    with OwnedDescriptor(ops, fd, label=label) as owner:
        info = os.fstat(owner.fd)
        h.check(
            stat.S_ISREG(info.st_mode) and _identity(info) == expected,
            "results file identity changed",
        )
        chunks = []
        while chunk := os.read(owner.fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)


def _inventory(owner):
    entries = []

    def visit(directory, relative):
        for name in sorted(directory.list_names()):
            info = directory.stat_child(name)
            path = relative / name
            record = {
                "path": path.as_posix(),
                "mode": stat.S_IMODE(info.st_mode),
                "device": info.st_dev,
                "inode": info.st_ino,
            }
            if stat.S_ISDIR(info.st_mode):
                record["kind"] = "directory"
                with directory.open_directory(name) as child:
                    visit(child, path)
            elif stat.S_ISREG(info.st_mode):
                record.update(
                    kind="file",
                    sha256=hashlib.sha256(
                        _read_file(directory, name, _identity(info))
                    ).hexdigest(),
                )
            elif stat.S_ISLNK(info.st_mode):
                record.update(
                    kind="link", target=os.readlink(name, dir_fd=directory.fd)
                )
            else:
                raise ValueError(f"unsupported results entry: {path}")
            entries.append(record)

    visit(owner, Path())
    return sorted(entries, key=lambda entry: entry["path"])


def _snapshot(base, expected_identity=None):
    with DirectoryDescriptor.open_secure_path(p.OPS, base) as owner:
        identity = _identity(os.fstat(owner.fd))
        expected = identity if expected_identity is None else expected_identity
        _check_base(owner, base, expected)
        entries = _inventory(owner)
        _check_base(owner, base, expected)
        return identity, entries


def inventory(base, *, expected_identity=None):
    """Descriptor-relative traversal; no cache exclusions or link following."""
    return _snapshot(base, expected_identity)[1]


def _preserved_subtree(entries, name):
    prefix = Path(name)
    return [
        {**entry, "path": Path(entry["path"]).relative_to(prefix).as_posix()}
        for entry in entries
        if Path(entry["path"]).is_relative_to(prefix)
    ]


def _verify_preserved_leaf(directory, name, entry):
    info = directory.stat_child(name)
    kind = (
        "directory"
        if stat.S_ISDIR(info.st_mode)
        else "file"
        if stat.S_ISREG(info.st_mode)
        else "link"
        if stat.S_ISLNK(info.st_mode)
        else "unsupported"
    )
    h.check(
        kind == entry["kind"]
        and _identity(info) == (entry["device"], entry["inode"])
        and stat.S_IMODE(info.st_mode) == entry["mode"],
        "results changed since preservation",
    )
    if kind == "file":
        data = _read_file(directory, name, (entry["device"], entry["inode"]))
        h.check(
            hashlib.sha256(data).hexdigest() == entry["sha256"],
            "results changed since preservation",
        )
    elif kind == "link":
        h.check(
            os.readlink(name, dir_fd=directory.fd) == entry["target"],
            "results changed since preservation",
        )


def _remove(parent, name, expected, preserved):
    """Delete only the preserved allowlist, never freshly discovered entries."""
    records = {entry["path"]: entry for entry in preserved}
    info = parent.stat_child(name)
    h.check((info.st_dev, info.st_ino) == expected, "owned run identity changed")
    with parent.open_directory(name) as child:
        info = os.fstat(child.fd)
        h.check(_identity(info) == expected, "owned run identity changed")
        h.check(
            records["."]["kind"] == "directory"
            and (records["."]["device"], records["."]["inode"]) == expected
            and records["."]["mode"] == stat.S_IMODE(info.st_mode),
            "results changed since preservation",
        )
        contents = [entry for entry in preserved if entry["path"] != "."]
        h.check(_inventory(child) == contents, "results changed since preservation")
        names = sorted(child.list_names())
        allowed = sorted({entry["path"].split("/")[0] for entry in contents})
        h.check(names == allowed, "results changed since preservation")
        for leaf in names:
            entry = records[leaf]
            _verify_preserved_leaf(child, leaf, entry)
            if entry["kind"] == "directory":
                _remove(
                    child,
                    leaf,
                    (entry["device"], entry["inode"]),
                    _preserved_subtree(contents, leaf),
                )
            else:
                child.unlink_child(leaf)
        h.check(not child.list_names(), "unrecorded results descendants after cleanup")
    info = parent.stat_child(name)
    h.check((info.st_dev, info.st_ino) == expected, "owned run identity changed")
    parent.remove_child_directory(name)


class ResultsLedger:
    """Explicit test-only path claims; never automatically adopts operator runs."""

    def __init__(
        self,
        base,
        evidence,
        *,
        created_base_receipt=None,
        disposable_venv_profiles=(),
        quiescence=None,
    ):
        # Opt-in only for the fixed prepared integration layout, never a broad
        # basename exclusion. All omitted payload bytes remain in the inventory
        # and the unchanged pre-deletion identity/content checks.
        h.check(
            type(disposable_venv_profiles) is tuple
            and all(
                type(profile) is str
                and profile
                in {"staged_bytes", "argv", "command_status", "cleanup", "normal"}
                for profile in disposable_venv_profiles
            )
            and len(set(disposable_venv_profiles)) == len(disposable_venv_profiles),
            "invalid disposable venv profiles",
        )
        h.check(quiescence is None or callable(quiescence), "invalid quiescence guard")
        self.quiescence = quiescence
        self.disposable_venv_profiles = disposable_venv_profiles
        self.base = Path(base)
        self.evidence = Path(evidence)
        h.check(
            not self.evidence.is_relative_to(self.base),
            "test evidence must be outside cleanup targets",
        )
        expected = None
        if created_base_receipt is not None:
            h.check(
                created_base_receipt["path"] == str(self.base),
                "wrong created base path",
            )
            expected = (created_base_receipt["device"], created_base_receipt["inode"])
            h.check(
                all(type(value) is int for value in expected), "invalid base receipt"
            )
        self.base_identity, observed = _snapshot(self.base, expected)
        # A successful exclusive mkdir receipt proves an initially empty base.
        # Descendants observed later still need independent reservation claims;
        # unknown entries remain unowned and prevent cleanup. Never rebind an
        # existing ledger to a replacement identity.
        self.before = observed if created_base_receipt is None else []
        if created_base_receipt is not None:
            p.write(
                self.evidence / "created-base.json",
                p.canonical({"receipt": created_base_receipt, "observed": observed}),
            )
        p.write(
            self.evidence / "base-identity.json",
            p.canonical({"path": str(self.base), "identity": self.base_identity}),
        )
        self.claims = {}
        self.attempted = False
        self.failure = None
        self.reconciliation_failure = None
        self.after = None
        p.write(self.evidence / "before.json", p.canonical(self.before))

    def claim_receipt(self, receipt):
        h.check(
            receipt.get("operation") == "exclusive-mkdir-success",
            "unverified reservation receipt",
        )
        identity = (receipt["device"], receipt["inode"])
        h.check(
            all(type(value) is int for value in identity),
            "invalid reservation identity",
        )
        path = Path(receipt["path"])
        if path.name in self.claims:
            h.check(
                self.claims[path.name] == identity and path.parent == self.base,
                "reservation receipt changed",
            )
            return
        self.claim(path, reservation_succeeded=True, reservation_identity=identity)

    def claim(self, path, *, reservation_succeeded, reservation_identity=None):
        path = Path(path)
        h.check(reservation_succeeded is True, "failed reservation grants no ownership")
        h.check(path.parent == self.base, "run is not an immediate base child")
        h.check(not self.attempted, "cleanup already attempted")
        h.check(path.name not in self.claims, "duplicate run claim")
        h.check(
            all(entry["path"].split("/")[0] != path.name for entry in self.before),
            "pre-existing run is unowned",
        )
        with DirectoryDescriptor.open_secure_path(p.OPS, self.base) as parent:
            _check_base(parent, self.base, self.base_identity)
            info = parent.stat_child(path.name)
            h.check(stat.S_ISDIR(info.st_mode), "run must not be a symlink")
            if reservation_identity is not None:
                h.check(
                    _identity(info) == reservation_identity,
                    "reserved run identity changed",
                )
            _check_base(parent, self.base, self.base_identity)
        self.claims[path.name] = _identity(info)
        p.write(
            self.evidence / ("claim-" + path.name + ".json"),
            p.canonical({"path": str(path), "identity": self.claims[path.name]}),
        )

    def finish(self, primary=None):
        if self.attempted:
            if self.failure is not None:
                raise self.failure
            return
        self.attempted = True
        try:
            if self.quiescence is not None:
                barrier = CleanupFailures(primary)
                original_interruptions = len(barrier.interruptions)
                barrier.run(self.quiescence, ordinary=(Exception,))
                if (
                    barrier.ordinary_failures
                    or barrier.unexpected
                    or len(barrier.interruptions) > original_interruptions
                ):
                    selected = barrier.complete()
                    # complete() leaves an ordinary original primary with its
                    # caller; do not interpret that None as a passed barrier.
                    if selected is None:
                        selected = primary
                    if selected is not None:
                        raise selected
            self.after = inventory(self.base, expected_identity=self.base_identity)
            omitted = {
                f"{name}/{profile}/workspace/venv"
                for name in self.claims
                for profile in self.disposable_venv_profiles
            }
            entries = {entry["path"]: entry for entry in self.after}
            omitted = {path for path in omitted if path in entries}
            h.check(
                all(entries[path]["kind"] == "directory" for path in omitted),
                "disposable venv root must be a directory",
            )
            p.write(
                self.evidence / "after.json",
                p.canonical(
                    {
                        "base_identity": self.base_identity,
                        "inventory": self.after,
                        "owned": self.claims,
                        "omitted_venv_payloads": sorted(omitted),
                    }
                ),
            )
            untouched = [
                entry
                for entry in self.after
                if entry["path"].split("/")[0] not in self.claims
            ]
            h.check(untouched == self.before, "unknown or changed pre-existing results")
            # Preserve every evidence file, including partial/binary output,
            # before deletion. Only explicitly declared disposable venv payload
            # copies are omitted; their complete inventory and removal checks
            # are unchanged. Symlinks remain records, never read targets.
            retained = self.evidence / "retained"
            retained.mkdir()
            for entry in self.after:
                if (
                    entry["path"].split("/")[0] in self.claims
                    and entry["kind"] == "file"
                    and not any(
                        Path(entry["path"]).is_relative_to(path) for path in omitted
                    )
                ):
                    destination = retained / entry["path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    data = p.read(self.base / entry["path"])
                    h.check(
                        hashlib.sha256(data).hexdigest() == entry["sha256"],
                        "results changed during preservation",
                    )
                    p.write(destination, data)
            failures = CleanupFailures(primary)
            with DirectoryDescriptor.open_secure_path(p.OPS, self.base) as parent:
                _check_base(parent, self.base, self.base_identity)

                def remove_claim(name, identity):
                    _check_base(parent, self.base, self.base_identity)
                    _remove(
                        parent, name, identity, _preserved_subtree(self.after, name)
                    )
                    _check_base(parent, self.base, self.base_identity)

                for name, identity in self.claims.items():
                    failures.run(
                        lambda n=name, i=identity: remove_claim(n, i),
                        ordinary=(Exception,),
                    )
                selected = failures.complete()
                if selected is not None:
                    raise selected
                _check_base(parent, self.base, self.base_identity)
            h.check(
                inventory(self.base, expected_identity=self.base_identity)
                == self.before,
                "undeclared residue after cleanup",
            )
            p.write(
                self.evidence / "reconciled.json",
                p.canonical(
                    {
                        "complete": True,
                        "owned_paths": [str(self.base / n) for n in self.claims],
                    }
                ),
            )
        except BaseException as error:
            self.failure = error
            raise
