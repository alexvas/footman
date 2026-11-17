"""Docker-free producer/ownership checks; no exported code runs on the host."""

import os
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from host_tests import execution as e
from tests import mcp_provenance as p


@pytest.mark.parametrize("kind", ["image", "wrapper"])
@pytest.mark.parametrize("status", [0, 17])
@pytest.mark.parametrize("phase", [None, "host_phase"])
def test_host_fixture_stages_disposable_bytes_modes_and_real_symlink(
    tmp_path, kind, status, phase
):
    flow = e.HostFlow(
        {
            "support_root": str(tmp_path),
            "service_deadline_monotonic": time.monotonic() + 120,
            "configured_image_id": "d9e853e87e55",
        }
    )
    scenario = flow.prepare(kind, status, phase)
    repo = scenario["repository"]
    assert flow.git(repo, "show", scenario["tree"] + ":payload") == e.PAYLOAD.encode()
    assert p.read(repo / "payload") != e.PAYLOAD.encode()
    assert flow.git(repo, "show", scenario["tree"] + ":binary") == e.BINARY
    assert (repo / "internal-link").is_symlink() and os.readlink(
        repo / "internal-link"
    ) == "payload"
    listing = flow.git(repo, "ls-tree", "-r", scenario["tree"])
    assert b"100755 blob" in listing and b"120000 blob" in listing
    assert b"untracked" not in listing and b"ignored" not in listing
    assert not (repo / ".git/refs/heads/master").exists()
    settings = tomllib.loads(p.read(scenario["config"]).decode())
    assert settings["image_id"] == "d9e853e87e55" and settings["user"] == "root"
    assert settings["container_snapshot_directory"].startswith("/tmp/footman-source-")
    assert settings["timeout_seconds"] == 60 and settings["cleanup_grace_seconds"] == 10
    assert settings["command_argv"] == scenario["command"]
    assert scenario["command"][0] == (
        "python3" if kind == "image" else "./probe-wrapper"
    )
    assert scenario["command"][-4:] == [*e.ARGUMENTS, str(status)]
    with pytest.raises(AssertionError, match="scenario incomplete"):
        flow.finish()
    assert p.load(flow.root / "summary.json")["cases"][0]["complete"] is False
    for owner in flow.commands.owners:
        before = owner.cleanup_status
        assert before.complete and not before.failures and owner.shutdown() == before


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_host_command_collector_and_pipe_release_authority(
    tmp_path, monkeypatch, fault
):
    from footman.filesystem.streams import OwnedStream

    primary = (
        KeyboardInterrupt("host collector interrupted")
        if fault == "interruption-close"
        else OSError("host collector failed")
    )
    secondary = OSError("host stdout close failed")
    closes = []
    original_close = OwnedStream.close

    class Buffer(bytearray):
        def extend(self, data):
            if fault != "close":
                raise primary
            return super().extend(data)

    def close(owner):
        released = owner.released
        original_close(owner)
        if not released:
            closes.append(owner.label)
            if owner.label == "stdout":
                raise secondary

    monkeypatch.setattr(e, "bytearray", Buffer, raising=False)
    monkeypatch.setattr(OwnedStream, "close", close)
    commands = e.Commands(tmp_path, time.monotonic() + 30)
    with pytest.raises(BaseException) as caught:
        commands.run(
            [
                sys.executable,
                "-P",
                "-c",
                "import sys; print('out'); sys.stderr.write('err')",
            ],
            tmp_path,
        )
    assert caught.value is (secondary if fault == "close" else primary)
    assert closes.count("stdout") == closes.count("stderr") == 1
    assert p.load(tmp_path / "command-0.json")["status"] is not None
    for owner in commands.owners:
        assert owner.cleanup_status.complete
        owner.shutdown()
    assert closes.count("stdout") == closes.count("stderr") == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr", "summary"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_host_command_retention_attempts_all_outputs_once(
    tmp_path, monkeypatch, stream, interrupted
):
    commands = e.Commands(tmp_path, time.monotonic() + 30)
    primary = (
        KeyboardInterrupt("retention interrupted")
        if interrupted
        else OSError("retention failed")
    )
    path = "command-0.json" if stream == "summary" else "command-0-" + stream + ".txt"
    original = p.write
    writes = []

    def write(target, data):
        writes.append(target.name)
        if target.name == path:
            raise primary
        original(target, data)

    monkeypatch.setattr(p, "write", write)
    with pytest.raises(BaseException) as caught:
        commands.run(
            [
                sys.executable,
                "-P",
                "-c",
                "import sys; print('out'); sys.stderr.write('err')",
            ],
            tmp_path,
        )
    assert caught.value is primary
    assert writes == ["command-0-stdout.txt", "command-0-stderr.txt", "command-0.json"]
    assert commands.records[0]["status"] == 0
    for owner in commands.owners:
        before = owner.cleanup_status
        assert before.complete and owner.shutdown() == before


def test_host_command_deadline_does_not_launch_or_renew(tmp_path, monkeypatch):
    commands = e.Commands(tmp_path, 0)

    def launch(*args, **kwargs):
        raise AssertionError("must refuse before acquisition")

    monkeypatch.setattr(e.OwnedProcess, "launch", launch)
    for _ in range(2):
        with pytest.raises(
            ValueError, match="original host fixture deadline exhausted"
        ):
            commands.run(["never"], tmp_path)
    assert not commands.records and not commands.owners and commands.deadline == 0


@pytest.mark.parametrize("fault", ["operation-close", "close", "interruption-close"])
def test_host_directory_creation_releases_parent_and_child_once(
    tmp_path, monkeypatch, fault
):
    parent = e.DirectoryDescriptor.open_secure_path(e.PosixDescriptorOps(), tmp_path)
    original_close = e.DirectoryDescriptor.close
    create = parent.create_directory
    primary = (
        KeyboardInterrupt("directory interrupted")
        if fault == "interruption-close"
        else OSError("directory operation failed")
    )
    secondary = OSError("directory close failed")
    closed = []

    def close(owner):
        if owner.released:
            return original_close(owner)
        closed.append(owner)
        original_close(owner)
        if owner is not parent:
            raise secondary

    def created(owner, name, *, mode):
        assert owner is parent
        child = create(name, mode=mode)

        class Scope:
            def __enter__(self):
                if fault != "close":
                    try:
                        raise primary
                    except BaseException:
                        child.__exit__(type(primary), primary, primary.__traceback__)
                        raise
                return child

            def __exit__(self, *args):
                return child.__exit__(*args)

        return Scope()

    monkeypatch.setattr(
        e.DirectoryDescriptor, "open_secure_path", lambda *args, **kwargs: parent
    )
    monkeypatch.setattr(e.DirectoryDescriptor, "create_directory", created)
    monkeypatch.setattr(e.DirectoryDescriptor, "close", close)
    with pytest.raises(BaseException) as caught:
        e.mkdir(tmp_path / "created")
    assert caught.value is (secondary if fault == "close" else primary)
    assert len(closed) == 2 and parent in closed
    for owner in closed:
        owner.close()
    assert len(closed) == 2


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("retention", [False, True])
def test_host_flow_retains_service_observer_and_preserves_failure(
    tmp_path, monkeypatch, interrupted, retention
):
    flow = e.HostFlow(
        {
            "support_root": str(tmp_path),
            "service_deadline_monotonic": time.monotonic() + 30,
        }
    )
    case = flow.root / "case"
    e.mkdir(case)
    flow.cases.append({"directory": str(case), "complete": False})
    primary = (
        KeyboardInterrupt("service interrupted")
        if interrupted
        else ValueError("service failed")
    )
    secondary = OSError("stderr retention failed")
    owner = SimpleNamespace(cleanup_status=SimpleNamespace(complete=False, failures=()))

    @asynccontextmanager
    async def verified(context, config, observation):
        observation.owner = owner
        observation.stderr_tail.extend(b"service diagnostic")
        raise primary
        yield  # Explicit failing async-generator boundary double

    original = p.write
    writes = []

    def write(target, data):
        writes.append(target.name)
        if retention:
            raise secondary
        original(target, data)

    monkeypatch.setattr(e, "verified_session", verified)
    monkeypatch.setattr(p, "write", write)
    with pytest.raises(BaseException) as caught:
        flow.execute({"case": case, "config": case / "config"})
    assert caught.value is primary
    assert writes == ["service-stderr-tail.txt"]
    assert flow.services[0].owner is owner
    if retention:
        assert secondary in getattr(primary, "_transaction_secondary", [])
    else:
        assert p.read(case / "service-stderr-tail.txt") == b"service diagnostic"


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("publication", ["none", "failure", "interruption"])
def test_host_flow_summary_and_independent_settlement_repeat(
    tmp_path, monkeypatch, complete, publication
):
    flow = e.HostFlow(
        {
            "support_root": str(tmp_path),
            "service_deadline_monotonic": time.monotonic() + 30,
        }
    )
    flow.cases.append(
        {
            "complete": complete,
            "cleanup": {
                "confirmed_absent": True,
                "snapshot_deleted": True,
                "unresolved_resources": False,
            },
        }
    )
    reads, writes = [], []

    class Observation:
        @property
        def cleanup_status(self):
            reads.append(True)
            return SimpleNamespace(complete=True, failures=())

    flow.commands.owners.append(Observation())
    flow.services.append(SimpleNamespace(owner=Observation()))
    error = (
        KeyboardInterrupt("summary interrupted")
        if publication == "interruption"
        else OSError("summary write failed")
    )
    original = p.write

    def write(path, data):
        writes.append(path)
        original(path, data)
        if publication != "none":
            raise error

    monkeypatch.setattr(p, "write", write)
    if complete and publication == "none":
        flow.finish()
    else:
        with pytest.raises(BaseException) as caught:
            flow.finish()
        if publication != "none":
            assert caught.value is error
        else:
            assert type(caught.value) is AssertionError
        with pytest.raises(BaseException) as repeated:
            flow.finish()
        assert repeated.value is caught.value
    assert len(reads) == 4 and len(writes) == 1
    assert p.load(flow.root / "summary.json")["cases"][0]["complete"] is complete
    if complete and publication == "none":
        flow.finish()
    assert len(reads) == 4 and len(writes) == 1
