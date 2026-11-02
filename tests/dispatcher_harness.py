"""Disposable development resources; no Docker daemon or developer-index access."""

import base64
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import OwnedProcess


def write_bytes(path, data, *, mode=0o600):
    ops = PosixDescriptorOps()
    label = "fixture bytes"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with OwnedDescriptor(ops, fd, label=label) as owner:
        remaining = memoryview(data)
        while remaining:
            written = os.write(owner.fd, remaining)
            if written <= 0:
                raise OSError("fixture write made no progress")
            remaining = remaining[written:]


def read_bytes(path):
    ops = PosixDescriptorOps()
    label = "fixture read"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with OwnedDescriptor(ops, fd, label=label) as owner:
        chunks = []
        while chunk := os.read(owner.fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)


def run(argv, *, cwd, env=None, check=True, deadline_seconds=120):
    environment = dict(os.environ if env is None else env)
    environment.pop("PYTHONPATH", None)
    with OwnedProcess.launch(
        [str(arg) for arg in argv],
        deadline_seconds=deadline_seconds,
        popen_kwargs={"cwd": cwd, "env": environment},
    ) as owner:
        result = owner.capture()
    if result.lifecycle.failures or not result.lifecycle.complete or result.timed_out:
        error = (
            result.lifecycle.failures[0].error
            if result.lifecycle.failures
            else AssertionError(f"helper lifecycle failed: {argv!r}: {result!r}")
        )
        # Keep typed failures and the same owner reachable for bounded observation;
        # a helper must not discard incomplete cleanup or recreate its budget.
        error.cleanup = owner
        error.process_result = result
        raise error
    if check and result.returncode != 0:
        raise AssertionError(f"helper failed: {argv!r}: {result!r}")
    return result


@dataclass(frozen=True)
class GitFixture:
    path: Path

    @classmethod
    def create(cls, path):
        path.mkdir(mode=0o700)
        fixture = cls(path)
        fixture.git("init", "--quiet")
        fixture.git("config", "user.email", "fixture@example.invalid")
        fixture.git("config", "user.name", "Disposable Fixture")
        write_bytes(path / "payload", b"staged\n")
        fixture.git("add", "payload")
        fixture.git("commit", "--quiet", "-m", "fixture baseline")
        return fixture

    def git(self, *args):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return run(["git", "-C", str(self.path), *args], cwd=self.path, env=environment)

    def identity(self):
        return (
            read_bytes(self.path / ".git/index"),
            read_bytes(self.path / "payload"),
            self.git("rev-parse", "HEAD").stdout,
            self.git("show-ref").stdout,
            self.git("status", "--porcelain=v1", "-z").stdout,
        )

    def assert_unchanged(self, before):
        if self.identity() != before:
            raise AssertionError("disposable Git fixture changed")


@dataclass
class DockerStub:
    executable: Path
    records: list[Path]
    lock: object

    @classmethod
    def create(cls, directory):
        executable = directory / "docker-stub"
        source_root = str(Path(__file__).resolve().parents[1])
        script = (
            f"#!{sys.executable}\n"
            "import runpy, sys\n"
            f"sys.path.insert(0, {source_root!r})\n"
            "runpy.run_module('tests.docker_cli_stub', run_name='__main__')\n"
        )
        write_bytes(executable, script.encode(), mode=0o700)
        return cls(executable, [], threading.Lock())

    def invoke(
        self,
        argv,
        *,
        stdout=b"",
        stderr=b"",
        status=0,
        delay=0,
        container_id=None,
        deadline_seconds=120,
    ):
        # Only paths allocated by this fixture are ever read; pytest owns the
        # disposable directory lifetime. No daemon cleanup or resource scanning.
        with self.lock:
            record = self.executable.parent / f"call-{len(self.records)}.json"
            self.records.append(record)
        scenario = {
            "stdout": base64.b64encode(stdout).decode(),
            "stderr": base64.b64encode(stderr).decode(),
            "status": status,
            "delay": delay,
            "container_id": container_id,
            "record": str(record),
        }
        environment = dict(os.environ, FOOTMAN_STUB_SCENARIO=json.dumps(scenario))
        return run(
            [self.executable, *argv],
            cwd=self.executable.parent,
            env=environment,
            check=False,
            deadline_seconds=deadline_seconds,
        )

    def calls(self):
        return [json.loads(read_bytes(path)) for path in self.records]
