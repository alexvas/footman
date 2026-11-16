"""Synthetic parent-observation controls and real evidence-owner faults.

No synthetic child/parser observation in this module establishes service proof.
Real wheel-child checks are in test_host_provenance.py.
"""

import hashlib
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest

from host_tests import provenance as parent
from tests import mcp_provenance as p


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    manifest = {
        "schema_version": 1,
        "context": "wheel",
        "wheel_sha256": "a" * 64,
        "entrypoint": {
            "script_path": str(tmp_path / "venv/bin/footman"),
            "target": "footman.startup:main",
            "observed_script_sha256": "b" * 64,
        },
        "modules": [
            {
                "name": name,
                "source_path": "footman/" + relative,
                "expected_path": str(tmp_path / "venv/footman" / relative),
                "sha256": "c" * 64,
            }
            for name, relative in (
                ("footman", "__init__.py"),
                ("footman.startup", "startup.py"),
                ("footman.process", "process.py"),
                ("footman.__main__", "__main__.py"),
            )
        ],
    }
    origin = tmp_path / "origin.json"
    p.write(origin, p.canonical(manifest))
    context = {
        "support_root": str(tmp_path),
        "venv_python": sys.executable,
        "expected_origin_path": str(origin),
        "wheel_sha256": "a" * 64,
        "server_script": str(tmp_path / "venv/bin/footman"),
        "service_deadline_monotonic": parent.time.monotonic() + 30,
        "provenance_evidence_path": str(tmp_path / "provenance.json"),
        "neutral_cwd": str(tmp_path),
    }
    _, entries = p.manifest_set(manifest)
    # Explicit synthetic attribution boundary: there is no real installation.
    monkeypatch.setattr(parent, "_support", lambda context: None)
    monkeypatch.setattr(
        parent,
        "sys",
        SimpleNamespace(
            executable=sys.executable,
            prefix=str(tmp_path / "venv"),
            exception=sys.exception,
        ),
    )
    monkeypatch.setattr(p, "verify", lambda manifest: entries)

    class Session:
        def __init__(self, *streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

    monkeypatch.setattr(parent, "ClientSession", Session)
    attempts = []

    def use(fault="none", body_error=None):
        observed = SimpleNamespace(owner=None, result=None, terminals={})

        @asynccontextmanager
        async def transport(argv, **kwargs):
            attempts.append(argv)
            parser = kwargs["parser"]
            observed.owner = SimpleNamespace(
                cleanup_status=SimpleNamespace(complete=True, failures=())
            )
            record = p.attestation(entries)
            if fault == "missing":
                record = b"ordinary diagnostic\n"
            elif fault == "duplicate":
                record += record
            elif fault == "partial":
                record = record[:-1]
            elif fault == "malformed":
                record = record.replace(b'"schema_version":1', b'"schema_version":true')
            elif fault == "overlong":
                record = p.WIRE + b"x" * 600 + b"\n"
            for byte in record:
                parser.feed(bytes([byte]))
                assert parser.buffered_bytes <= 512
            try:
                yield (None, None)
            finally:
                terminal = (
                    parent.StreamOutcome.READ_FAILED
                    if fault == "read-failed"
                    else parent.StreamOutcome.ABORTED
                    if fault == "aborted"
                    else parent.StreamOutcome.EOF
                )
                parser.end(terminal)
                observed.terminals = {
                    s: terminal for s in parent.transport.ProcessStream
                }
                observed.result = SimpleNamespace(
                    returncode=19 if fault == "child-failed" else 0, timed_out=False
                )

        monkeypatch.setattr(parent.transport, "stdio_transport", transport)

        async def run():
            async with parent.verified_session(
                context, tmp_path / "config.toml", observed
            ):
                if body_error is not None:
                    raise body_error

        anyio.run(run)
        return observed

    return SimpleNamespace(
        use=use,
        context=context,
        attempts=attempts,
        entries=entries,
        evidence=tmp_path / "provenance.json",
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "partial",
        "malformed",
        "overlong",
        "read-failed",
        "aborted",
    ],
)
def test_parent_requires_final_complete_attestation(synthetic, fault):
    with pytest.raises(ValueError, match="attestation incomplete"):
        synthetic.use(fault)
    assert not synthetic.evidence.exists()
    assert len(synthetic.attempts) == 1


def test_parent_retains_verified_set_separately_from_child_failure(synthetic):
    with pytest.raises(ValueError, match="process/collection unsuccessful"):
        synthetic.use("child-failed")
    value = p.load(synthetic.evidence)
    assert value["sources"] == synthetic.entries
    assert (
        value["set_sha256"]
        == hashlib.sha256(p.canonical(synthetic.entries)).hexdigest()
    )


@pytest.mark.parametrize("fault", ["write-close", "close", "interruption-close"])
def test_parent_evidence_owner_failures_preserve_primary_and_release_once(
    synthetic, monkeypatch, fault
):
    original_owner = p.OwnedDescriptor
    original_write = p.os.write
    owners = []
    fds = set()
    releases = []
    error = (
        KeyboardInterrupt("evidence interruption")
        if fault == "interruption-close"
        else OSError("evidence write failed")
    )
    release = OSError("evidence close failed")

    class Ops:
        def close(self, fd):
            releases.append(fd)
            p.OPS.close(fd)
            raise release

    def owner(ops, fd, *, label):
        if label != "MCP exclusive evidence":
            return original_owner(ops, fd, label=label)
        value = original_owner(Ops(), fd, label=label)
        owners.append(value)
        fds.add(fd)
        return value

    def write(fd, data):
        if fd in fds and fault != "close":
            raise error
        return original_write(fd, data)

    monkeypatch.setattr(p, "OwnedDescriptor", owner)
    monkeypatch.setattr(p.os, "write", write)
    with pytest.raises(BaseException) as caught:
        synthetic.use()
    assert caught.value is (release if fault == "close" else error)
    assert len(owners) == len(releases) == 1
    owners[0].close()
    assert len(releases) == 1


@pytest.mark.parametrize(
    "fault", ["missing", "truncated", "inconsistent", "unwritable"]
)
def test_parent_rejects_failed_evidence_retention(synthetic, monkeypatch, fault):
    original = p.write

    def write(path, data):
        if path != synthetic.evidence:
            return original(path, data)
        if fault == "missing":
            return None
        if fault == "unwritable":
            raise PermissionError("evidence denied")
        data = (
            data[: len(data) // 2]
            if fault == "truncated"
            else data.replace(b'"source_count":4', b'"source_count":3')
        )
        return original(path, data)

    monkeypatch.setattr(p, "write", write)
    with pytest.raises((OSError, ValueError)):
        synthetic.use()


def test_parent_preserves_body_interruption_and_independent_retention_failure(
    synthetic, monkeypatch
):
    primary = KeyboardInterrupt("caller interruption")

    def denied(*args):
        raise OSError("independent evidence failure")

    monkeypatch.setattr(p, "retain_evidence", denied)
    with pytest.raises(KeyboardInterrupt) as caught:
        synthetic.use(body_error=primary)
    assert caught.value is primary


def test_parent_refuses_repeat_evidence_publication(synthetic):
    synthetic.use()
    first = p.read(synthetic.evidence)
    with pytest.raises(FileExistsError):
        synthetic.use()
    assert p.read(synthetic.evidence) == first


def test_parent_rejects_ambient_support_without_acquisition(tmp_path):
    with pytest.raises(ValueError, match="ambient support fallback"):
        parent._support({"support_root": str(tmp_path)})


def test_parent_exhausted_deadline_and_unsafe_evidence_acquire_nothing(synthetic):
    synthetic.context["service_deadline_monotonic"] = parent.time.monotonic() - 1
    with pytest.raises(ValueError, match="deadline exhausted"):
        synthetic.use()
    assert not synthetic.attempts
    synthetic.context["service_deadline_monotonic"] = parent.time.monotonic() + 30
    synthetic.context["provenance_evidence_path"] = str(
        synthetic.evidence.parent / ".." / "outside.json"
    )
    with pytest.raises(ValueError, match="unsafe provenance"):
        synthetic.use()
    assert not synthetic.attempts
