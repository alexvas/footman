"""Explicit synthetic Docker/SDK boundaries over real Git and retained runs."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

from footman.filesystem.cleanup import CleanupFailures
from footman.process import (
    FailureInfo,
    FailureKind,
    FailurePhase,
    LifecycleResult,
    ProcessResult,
    ProcessStream,
    StreamOutcome,
)
from host_tests import cleanup as c
from host_tests import execution as e
from host_tests.cleanup_paths import RecordedTree, identity
from tests import mcp_provenance as p

ID = "a" * 64
SENTINEL = "f" * 64
IMAGE = "sha256:" + "b" * 64
PRODUCER = c.CleanupFlow


def generated():
    import base64

    return {
        "pid": 31,
        "start_ticks": "1234",
        "uid": 0,
        "alive": True,
        "file_uid": 0,
        "file_regular": True,
        "file_bytes": base64.b64encode(c.GENERATED).decode(),
        "link_uid": 0,
        "link_is_symlink": True,
        "link_target": "root-owned-file",
        "payload": base64.b64encode(b"container changed payload\n").decode(),
    }


class DockerBoundary:
    def __init__(self):
        self.calls = []
        self.command_calls = []
        self.helpers = []
        self.live_sentinel = False
        self.sentinel_status = "running"
        self.fail = None
        self.observation = generated()
        self.observer_failure = None
        self.local_state = None
        self.operation_error = OSError("synthetic stream operation failed")
        self.close_error = OSError("synthetic pipe close failed")

    def launch(self, argv, **kwargs):
        boundary = self
        argv = list(argv)
        self.calls.append((argv, kwargs))

        class Helper:
            returncode = None
            _proc = SimpleNamespace(pid=424242)
            cleanup_status = LifecycleResult(True)
            collections = 0
            exits = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.exits += 1
                failures = CleanupFailures(exc)

                def close():
                    if boundary.fail in {
                        "close",
                        "operation-close",
                        "interruption-close",
                    }:
                        self.cleanup_status = LifecycleResult(
                            True,
                            (
                                FailureInfo(
                                    boundary.close_error,
                                    FailureKind.ORDINARY,
                                    FailurePhase.RELEASE,
                                ),
                            ),
                        )
                        raise boundary.close_error

                failures.run(close, ordinary=(OSError,))
                selected = failures.complete()
                if selected is not None and selected is not exc:
                    raise selected

            def stream(self, chunk, *, on_stream_end):
                self.collections += 1
                if boundary.fail in {"operation-close", "interruption-close"}:
                    raise boundary.operation_error
                timed_out = "-w" in argv and argv[-1] == "timeout"
                code = -15 if timed_out else 0
                if argv[1] == "run":
                    data = (ID + "\n").encode()
                elif argv[1:3] == ["container", "inspect"]:
                    data = (ID + "|running|" + IMAGE + "\n").encode()
                elif "-w" in argv:
                    data = (
                        p.canonical({"pid": 31, "start_ticks": "1234", "uid": 0})
                        + b"\n"
                    )
                else:
                    data = b""
                if boundary.fail == "rm" and argv[1:3] == ["rm", "-f"]:
                    code = 17
                for stream in ProcessStream:
                    chunk(stream, data if stream is ProcessStream.STDOUT else b"")
                    on_stream_end(stream, StreamOutcome.EOF)
                self.returncode = code
                return ProcessResult(
                    b"",
                    b"",
                    code,
                    timed_out,
                    self.cleanup_status,
                    tuple((s, StreamOutcome.EOF) for s in ProcessStream),
                )

        helper = Helper()
        self.helpers.append(helper)
        return helper

    def command(self, argv, root):
        argv = list(argv)
        self.command_calls.append(argv)
        if argv[1] == "run":
            self.live_sentinel = True
            return {"stdout": (SENTINEL + "\n").encode(), "stderr": b"", "status": 0}
        if argv[1:3] == ["rm", "-f"]:
            if argv[-1] == SENTINEL:
                self.live_sentinel = False
            return {"stdout": b"", "stderr": b"", "status": 0}
        if argv[1:3] == ["container", "inspect"]:
            return {
                "stdout": (
                    SENTINEL + "|" + self.sentinel_status + "|" + IMAGE + "\n"
                ).encode(),
                "stderr": b"",
                "status": 0,
            }
        if argv[1:3] == ["container", "ls"]:
            data = (
                (SENTINEL + "\n").encode()
                if self.live_sentinel and "id=" + SENTINEL in argv
                else b""
            )
            return {"stdout": data, "stderr": b"", "status": 0}
        if argv[1] == "exec":
            if self.observer_failure is not None:
                raise self.observer_failure
            return {
                "stdout": p.canonical(self.observation) + b"\n",
                "stderr": b"",
                "status": 0,
            }
        raise AssertionError("unexpected synthetic Docker observation: " + repr(argv))


def prepare(tmp, monkeypatch):
    flow = e.HostFlow(
        {
            "support_root": str(tmp),
            "service_deadline_monotonic": time.monotonic() + 120,
            "configured_image_id": IMAGE,
        }
    )
    boundary = DockerBoundary()
    original = e.Commands.run

    def command(owner, argv, root):
        if argv[0] == "docker":
            return boundary.command(argv, root)
        return original(owner, argv, root)

    monkeypatch.setattr(e.Commands, "run", command)
    monkeypatch.setattr(c, "OwnedProcess", SimpleNamespace(launch=boundary.launch))
    monkeypatch.setattr(p, "read_proc_stat", lambda pid: boundary.local_state)
    # Only start's existing MCP baseline boundary is doubled. Local retained-run
    # production reservation/export/supervision/manifest logic is not replaced.
    calls = []

    def execute(scenario):
        calls.append(scenario)
        case = flow.cases[-1]
        if scenario.get("mode") == "timeout":
            # This is a manufactured SDK boundary over a native local retained
            # run, not real MCP or a container-semantic observation.
            config = c.load_configuration(scenario["config"])
            with c.reserve_request(config, {}) as output:
                record = producer._run_reserved(scenario, output, "timeout", None)
            result = {"manifest": record["manifest"], "value": record["run"].response}
            cleanup = record["manifest"]["cleanup"]
        else:
            result = {"synthetic_baseline": True}
            cleanup = {
                "confirmed_absent": True,
                "snapshot_deleted": True,
                "unresolved_resources": False,
            }
        case.update(complete=True, cleanup=cleanup)
        return result

    monkeypatch.setattr(flow, "execute", execute)
    monkeypatch.setattr(
        flow, "verify", lambda scenario, observed: calls.append(observed)
    )
    producer = PRODUCER(flow)
    producer.start()
    return producer, flow, boundary, calls


def copied_cleanup_profile(tmp, *, fail=False):
    import shutil
    import sys

    from tests import mcp_wheel_support as wheel

    source = Path(__file__).resolve().parents[1]
    workspace = tmp / "copied-cleanup"
    workspace.mkdir()
    shutil.copytree(
        source / "host_tests",
        workspace / "host_tests",
        ignore=shutil.ignore_patterns("__pycache__", "test_negative_expectations.py"),
    )
    (workspace / "tests").mkdir()
    for relative in (
        *p.SUPPORT_FILES,
        "tests/host_acceptance_assertions.py",
        "tests/host_cleanup_controls.py",
    ):
        p.write(workspace / relative, p.read(source / relative))
    plugin = """import sys
from pathlib import Path
import pytest
from footman.filesystem.cleanup import CleanupFailures
from tests import host_cleanup_controls as controls
class Boundary:
    def __init__(self, flow):
        self.patch=pytest.MonkeyPatch()
        self.actual,self.flow,self.boundary,_=controls.prepare(flow.root,self.patch)
        if FAIL:
            self.boundary.observation["file_uid"]=1000
    def start(self):
        pass
    def __getattr__(self,name):
        return getattr(self.actual,name)
    def finish(self,primary=None):
        failures=CleanupFailures(primary)
        failures.run(lambda:self.actual.finish(primary),ordinary=(Exception,))
        failures.run(self.flow.finish,ordinary=(Exception,))
        failures.run(self.patch.undo,ordinary=(Exception,))
        selected=failures.complete()
        if selected is not None: raise selected

def pytest_sessionstart(session):
    import host_tests.cleanup as implementation
    implementation.CleanupFlow=Boundary
""".replace("FAIL", repr(fail))
    p.write(workspace / "tests/control_cleanup.py", plugin.encode())
    p.write(workspace / "conftest.py", b'pytest_plugins=("tests.control_cleanup",)\n')
    context = workspace / "context.json"
    p.write(
        context,
        p.canonical(
            {
                "support_root": str(workspace),
                "service_deadline_monotonic": time.monotonic() + 120,
                "configured_image_id": IMAGE,
            }
        ),
    )
    ini = tmp / "cleanup.ini"
    p.write(ini, b"[pytest]\naddopts=--strict-config --strict-markers\n")
    result = wheel.command(
        [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "-c",
            str(ini),
            "--rootdir",
            str(workspace),
            str(workspace / "host_tests/test_cleanup.py"),
            "-n",
            "1",
            "--dist",
            "worksteal",
            "--tb=short",
            "-q",
        ],
        cwd=workspace,
        env=wheel.clean_environment({"FOOTMAN_HOST_CONTEXT": str(context)}),
        timeout_seconds=120,
    )
    p.write(tmp / "copied-cleanup-stdout.txt", result["stdout"])
    p.write(tmp / "copied-cleanup-stderr.txt", result["stderr"])
    return workspace, result


def standalone_tree(tmp):
    path = tmp / "owned"
    e.mkdir(path)
    p.write(path / "file", b"owned payload")
    e.mkdir(path / "directory")
    p.write(path / "directory" / "nested", b"nested")
    sentinel = tmp / "unrelated"
    e.mkdir(sentinel)
    p.write(sentinel / "keep", b"untouched")
    (path / "outward").symlink_to(sentinel)
    return RecordedTree(path, identity(os.stat(path))), sentinel
