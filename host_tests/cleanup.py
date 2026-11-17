"""Authored real-host cleanup fixtures; Docker-free controls are not host proof.

MCP timeout exercises the public protocol. A separate installed-wheel retained
run observes the exact pre-rm/post-confirmation boundaries, without changing
production code, flags, budgets or process-owner shutdown. Host filesystem faults
are scoped to that local run only; they are not container ownership claims.
"""

import base64
import errno
import json
import os
import sys
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from footman.configuration import load_configuration
from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.output import reserve_request
from footman.process import FailureKind, OwnedProcess
from footman.results import RetainedRun
from host_tests import execution as e
from host_tests.cleanup_paths import RecordedTree, identity
from tests import host_acceptance_assertions as a
from tests import mcp_provenance as p

GENERATED = b"container-only root bytes\x00\xff\n"
PROBE = """import base64,json,os,signal,sys
from pathlib import Path
root=Path.cwd()
(root/"root-owned-file").write_bytes(b"container-only root bytes\\x00\\xff\\n")
(root/"root-owned-link").symlink_to("root-owned-file")
(root/"payload").write_bytes(b"container changed payload\\n")
raw=Path("/proc/self/stat").read_text().rpartition(")")[2].split()
value={"pid":os.getpid(),"start_ticks":raw[19],"uid":os.getuid()}
(root/"survivor.json").write_text(json.dumps(value))
print(json.dumps(value,sort_keys=True,separators=(",",":")),flush=True)
if sys.argv[-1]=="timeout":
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    signal.signal(signal.SIGHUP,signal.SIG_IGN)
    while True: signal.pause()
"""
OBSERVE = """import base64,json,os,stat
from pathlib import Path
root=Path.cwd()
v=json.loads((root/"survivor.json").read_text())
process=Path("/proc")/str(v["pid"])/"stat"
raw=process.read_text().rpartition(")")[2].split() if process.exists() else []
v.update(alive=bool(raw) and raw[0]!="Z" and raw[19]==v["start_ticks"],
file_uid=(root/"root-owned-file").lstat().st_uid,
file_regular=stat.S_ISREG((root/"root-owned-file").lstat().st_mode),
file_bytes=base64.b64encode((root/"root-owned-file").read_bytes()).decode(),
link_uid=(root/"root-owned-link").lstat().st_uid,
link_is_symlink=(root/"root-owned-link").is_symlink(),
link_target=os.readlink(root/"root-owned-link"),
payload=base64.b64encode((root/"payload").read_bytes()).decode())
print(json.dumps(v,sort_keys=True,separators=(",",":")))
"""


def configure(flow: Any, kind: str, mode: str) -> dict[str, Any]:
    scenario = flow.prepare(kind, 0, None, probe=PROBE)
    settings = tomllib.loads(p.read(scenario["config"]).decode())
    command = scenario["command"][:-1] + [mode]
    settings.update(
        command_argv=command, timeout_seconds=12 if mode == "timeout" else 60
    )
    path = scenario["case"] / "cleanup.toml"
    p.write(
        path,
        (
            "\n".join(k + " = " + json.dumps(v) for k, v in settings.items()) + "\n"
        ).encode(),
    )
    scenario.update(
        config=path, command=command, expected_error=mode == "timeout", mode=mode
    )
    return scenario


def container_id(data: bytes) -> str:
    value = data.removesuffix(b"\n").decode("ascii")
    a.require(
        len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value),
        "fixture launch has no attributable full container ID",
    )
    return value


def confirmation(identity: str) -> list[str]:
    return [
        "docker",
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        "id=" + identity,
        "--format",
        "{{.ID}}",
    ]


class CleanupFlow:
    def __init__(self, flow: Any):
        self.flow = flow
        self.root = flow.root / "cleanup"
        e.mkdir(self.root)
        self.runs: list[dict[str, Any]] = []
        self.sentinel: dict[str, Any] = {
            "attempted": False,
            "id": None,
            "removed": False,
        }
        self.sentinel_tree: RecordedTree | None = None
        self.sentinel_path = self.root / "sentinel"
        self.sentinel_path_reserved = False
        self.finished = False
        self.failure: BaseException | None = None

    def start(self) -> None:
        path = self.sentinel_path
        e.mkdir(path)
        self.sentinel_path_reserved = True
        # Successful exclusive mkdir is this fixture's ownership receipt.
        info = path.stat()
        p.write(path / "keep", b"unrelated host sentinel\n")
        self.sentinel_tree = RecordedTree(path, identity(info))
        self.sentinel["attempted"] = True
        result = self.flow.commands.run(
            [
                "docker",
                "run",
                "--detach=true",
                "--pull=never",
                "--entrypoint=sleep",
                "--",
                self.flow.context["configured_image_id"],
                "infinity",
            ],
            self.root,
        )
        self.sentinel["id"] = container_id(result["stdout"])
        self.check_sentinel()
        # Every worker owning local cleanup cases first establishes the same
        # archive-authoritative MCP baseline and canonical provenance artifact.
        scenario = self.flow.prepare("image", 0, None)
        observed = self.flow.execute(scenario)
        self.flow.verify(scenario, observed)

    def check_sentinel(self) -> None:
        if self.sentinel_tree is None:
            raise AssertionError("missing fixture sentinel path")
        self.sentinel_tree.check()
        selected = self.sentinel["id"]
        a.require(selected is not None, "unknown fixture sentinel container")
        result = self.flow.commands.run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{.Id}}|{{.State.Status}}|{{.Image}}",
                selected,
            ],
            self.root,
        )
        fields = result["stdout"].removesuffix(b"\n").decode("ascii").split("|")
        a.require(
            len(fields) == 3 and fields[0] == selected and fields[1] == "running",
            "unrelated sentinel container changed or disappeared",
        )
        before = self.sentinel.setdefault("inspection", fields)
        a.require(fields == before, "unrelated sentinel image/state changed")

    def mcp_timeout(self) -> dict[str, Any]:
        scenario = configure(self.flow, "image", "timeout")
        result = self.flow.execute(scenario)
        a.require(
            result["value"]["outcome"] == result["manifest"]["outcome"] == "timed_out",
            "MCP timeout outcome missing",
        )
        a.require(
            result["value"]["return_code"] == result["manifest"]["return_code"],
            "MCP timeout command status mismatch",
        )
        a.verify_copy(result["manifest"], scenario["destination"])
        a.verify_cleanup_protocol(result["manifest"])
        a.require(
            not os.path.lexists(result["manifest"]["snapshot"]["path"]),
            "MCP timeout snapshot retained",
        )
        self.check_sentinel()
        return result

    def run(self, kind: str, mode: str, fault: str | None = None) -> dict[str, Any]:
        scenario = configure(self.flow, kind, mode)
        config = load_configuration(scenario["config"])
        with reserve_request(config, {}) as output:
            return self._run_reserved(scenario, output, mode, fault)

    def _run_reserved(
        self, scenario: Any, output: Any, mode: str, fault: str | None
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "scenario": str(scenario["case"]),
            "mode": mode,
            "fault": fault,
            "complete": False,
            "reconciled": False,
            "snapshot": None,
            "observations": {},
            "owners": [],
            "run": None,
        }
        self.runs.append(record)  # Prepared before any runtime launch.
        original_list = DirectoryDescriptor.list_names
        original_stat = PosixDescriptorOps.fstat
        armed = False
        snapshot_path = Path(output.identity.run_id)
        expected = (
            load_configuration(scenario["config"]).snapshot_directory / snapshot_path
        )
        observers = self.root / ("observer-" + str(len(self.runs)))
        e.mkdir(observers)
        commands = e.Commands(observers, self.flow.commands.deadline)
        record["commands"] = commands

        def list_names(directory: Any) -> Any:
            if (
                armed
                and fault == "permission"
                and Path(os.readlink(f"/proc/self/fd/{directory.fd}")) == expected
            ):
                raise PermissionError(
                    errno.EACCES, "fixture host snapshot permission fault"
                )
            return original_list(directory)

        def fstat(ops: Any, fd: int) -> Any:
            info = original_stat(ops, fd)
            if (
                armed
                and fault == "owner"
                and Path(os.readlink(f"/proc/self/fd/{fd}")) == expected
            ):
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        @contextmanager
        def launch(argv: Any, **kwargs: Any) -> Any:
            nonlocal armed
            started = time.monotonic()
            run = record["run"]
            snapshot = run.snapshot
            if record["snapshot"] is None:
                a.require(
                    snapshot is not None and snapshot.path == expected,
                    "missing exact runtime snapshot reservation",
                )
                source = snapshot.copy_source()
                a.require(
                    snapshot._root is not None, "missing live reservation capability"
                )
                record["snapshot"] = RecordedTree(
                    source, identity(os.fstat(snapshot._root.fd))
                )
            if list(argv[:3]) == ["docker", "rm", "-f"]:
                selected = run.supervisor.execution.container_id
                a.require(
                    selected is not None
                    and list(argv) == ["docker", "rm", "-f", selected]
                    and selected != self.sentinel["id"],
                    "wrong cleanup target",
                )
                commands.deadline = min(
                    commands.deadline,
                    run.supervisor.cleanup._deadline,
                    started + kwargs["deadline_seconds"],
                )
                observation = record["observations"]
                receipt = commands.run(
                    [
                        "docker",
                        "exec",
                        "-u",
                        "root",
                        "-w",
                        scenario["destination"],
                        selected,
                        "python3",
                        "-c",
                        OBSERVE,
                    ],
                    self.root,
                )
                observation["generated"] = json.loads(receipt["stdout"])
                observation["snapshot_before_removal"] = os.path.lexists(expected)
                record["snapshot"].check()
                if mode == "timeout":
                    # Read-only PID metadata, never a second signal/wait/reap owner.
                    owner = record["command_owner"]
                    pid = owner._proc.pid
                    raw = p.read_proc_stat(pid)
                    state = None if raw is None else raw.rpartition(b")")[2].split()[0]
                    observation["local_client_exited"] = raw is None or state == b"Z"
                    observation["survivor_alive"] = observation["generated"]["alive"]
                    observation["local_client_pid"] = pid
                    observation["local_client_proc_state"] = (
                        None if state is None else state.decode()
                    )
                if fault is not None:
                    if self.sentinel_tree is None:
                        raise AssertionError("missing host sentinel")
                    (expected / "host-sentinel-link").symlink_to(
                        self.sentinel_tree.path
                    )
                    record["snapshot_before_host_fault"] = record["snapshot"].witness
                    record["snapshot"] = RecordedTree(
                        expected, identity(os.stat(expected))
                    )
                    armed = True
            kwargs["deadline_seconds"] -= time.monotonic() - started
            a.require(
                kwargs["deadline_seconds"] > 0,
                "cleanup observer exhausted original helper deadline",
            )
            with OwnedProcess.launch(argv, **kwargs) as owner:
                record["owners"].append(owner)
                if list(argv[:2]) == ["docker", "exec"] and "-w" in argv:
                    record["command_owner"] = owner
                yield owner
            selected = run.supervisor.execution.container_id
            if selected is not None and list(argv) == confirmation(selected):
                record["observations"]["snapshot_after_confirmation"] = os.path.lexists(
                    expected
                )
                # Fault injections apply only to runtime removal, not witnesses.
                if fault is None:
                    record["snapshot"].check()

        try:
            run = RetainedRun(
                load_configuration(scenario["config"]), output, launch=launch
            )
            record["run"] = run
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(DirectoryDescriptor, "list_names", list_names)
                patch.setattr(PosixDescriptorOps, "fstat", fstat)
                value = run.execute()
            manifest = p.load(Path(value["resultPath"]))
            record["manifest"] = manifest
            a.require(
                run.manifest.finalized
                and run.manifest.failure is None
                and run.startup.failure is None
                and run.command.failure is None
                and output.file_failure is None
                and output.cleanup_failure is None,
                "cleanup run evidence retention/publication failed",
            )
            for domain, prefix in (("startup", "startup-"), ("command", "")):
                for stream in ("stdout", "stderr"):
                    data = p.read(output.directory / (prefix + stream + ".txt"))
                    a.require(
                        len(data)
                        == manifest["persistence"][domain][stream]["retained_bytes"],
                        "cleanup retained stream length mismatch",
                    )
            record["observations"]["snapshot_after_run"] = os.path.lexists(expected)
            repository = scenario["repository"]
            a.require(
                (
                    p.read(repository / ".git/index"),
                    p.read(repository / "payload"),
                    self.flow.git(repository, "status", "--porcelain=v1", "-z"),
                )
                == scenario["before"],
                "cleanup run changed disposable source state",
            )
            a.require(
                manifest["configured_image_id"]
                == self.flow.context["configured_image_id"]
                and manifest["snapshot"]["tree_id"] == scenario["tree"]
                and manifest["command_argv"] == scenario["command"]
                and manifest["user"] == "root"
                and manifest["cwd"] == scenario["destination"]
                and manifest["docker_command_argv"]
                == [
                    "docker",
                    "exec",
                    "-u",
                    "root",
                    "-w",
                    scenario["destination"],
                    manifest["container_id"],
                    *scenario["command"],
                ],
                "cleanup run configuration/tree/exec attribution mismatch",
            )
            a.verify_copy(manifest, scenario["destination"])
            a.verify_cleanup_protocol(manifest)
            if fault is not None:
                a.verify_safe_retention(
                    manifest, snapshot_present=os.path.lexists(expected)
                )
                failure = manifest["cleanup"]["failure"]
                diagnostic = (
                    "permission fault" if fault == "permission" else "owner validation"
                )
                a.require(
                    diagnostic
                    in base64.b64decode(
                        failure["message_base64"], validate=True
                    ).decode(),
                    "unrelated retention failure",
                )
            if fault is not None:
                tree: Any = record["snapshot"]
                if tree is None:
                    raise AssertionError("missing retained snapshot witness")
                tree.check()
                tree.finish()  # Explicit fixture reconciliation, not runtime retry.
            a.require(not os.path.lexists(expected), "fixture snapshot unresolved")
            record.update(complete=True, reconciled=True)
            # Keep the unsuccessful runtime result unchanged in both summaries.
            case = self.flow.cases[-1]
            case.update(
                complete=True,
                cleanup=manifest["cleanup"],
                fixture_reconciled=True,
                expected_retention=fault is not None,
                runtime_outcome=value["outcome"],
            )
            self.check_sentinel()
            return record
        finally:
            failures = CleanupFailures(sys.exception())
            failures.run(lambda: self._retain(record), ordinary=(Exception,))
            selected = failures.complete()
            if selected is not None and selected is not sys.exception():
                raise selected

    def _retain(self, record: dict[str, Any]) -> None:
        path = Path(record["scenario"]) / "cleanup-observations.json"
        value = {
            key: record.get(key)
            for key in (
                "scenario",
                "mode",
                "fault",
                "complete",
                "reconciled",
                "manifest",
                "observations",
                "snapshot_before_host_fault",
            )
        }
        tree = record["snapshot"]
        value["snapshot_witness"] = None if tree is None else tree.witness
        value["commands"] = record["commands"].records
        p.write(path, p.canonical(value))

    def _remove_container(self, selected: str) -> None:
        failures = CleanupFailures(None)
        failures.run(
            lambda: self.flow.commands.run(["docker", "rm", "-f", selected], self.root),
            ordinary=(Exception,),
        )

        def confirm() -> None:
            result = self.flow.commands.run(confirmation(selected), self.root)
            a.require(result["stdout"] == b"", "fixture container unresolved")

        failures.run(confirm, ordinary=(Exception,))
        selected_error = failures.complete()
        if selected_error is not None:
            raise selected_error

    def finish(self, primary: BaseException | None = None) -> None:
        if self.finished:
            if self.failure is not None:
                raise self.failure
            return
        self.finished = True
        failures = CleanupFailures(primary)
        for record in self.runs:
            run = record["run"]

            def settle(record: Any = record, run: Any = run) -> None:
                a.require(
                    run is not None
                    and run.supervisor is not None
                    and run.supervisor.cleanup_status.complete,
                    "runtime owners unsettled; fixture reconciliation forbidden",
                )
                for owner in [
                    *record["owners"],
                    *record["commands"].owners,
                    *run.snapshot.processes,
                ]:
                    status = owner.cleanup_status
                    a.require(status.complete, "fixture helper resources unsettled")
                    # Settled-but-failed is unsuccessful, but must not suppress
                    # independent recorded-ID/path fixture reconciliation.
                    for info in status.failures:
                        record.setdefault("owner_failures", []).append(
                            {
                                "kind": info.kind.name,
                                "phase": info.phase.name,
                                "type": type(info.error).__name__,
                            }
                        )

                        def failed(error: BaseException = info.error) -> None:
                            raise error

                        failures.run(
                            failed,
                            ordinary=(Exception,)
                            if info.kind is FailureKind.ORDINARY
                            else (),
                        )
                if not run.supervisor.cleanup.confirmed_absent:
                    selected = run.supervisor.execution.container_id
                    a.require(
                        selected is not None and selected != self.sentinel["id"],
                        "unknown fixture container; no guessed removal",
                    )
                    self._remove_container(selected)
                tree = record["snapshot"]
                if tree is not None and (tree.attempted or os.path.lexists(tree.path)):
                    tree.finish()
                record["reconciled"] = True

            failures.run(settle, ordinary=(Exception,))
        if self.sentinel["attempted"]:

            def sentinel() -> None:
                a.require(
                    self.sentinel["id"] is not None,
                    "unknown sentinel launch; unresolved",
                )
                self._remove_container(self.sentinel["id"])
                self.sentinel["removed"] = True

            failures.run(sentinel, ordinary=(Exception,))
        if self.sentinel_tree is not None:
            failures.run(self.sentinel_tree.finish, ordinary=(Exception,))

        def complete() -> None:
            a.require(
                all(record["complete"] and record["reconciled"] for record in self.runs)
                and self.sentinel["removed"],
                "host cleanup fixture incomplete",
            )

        failures.run(complete, ordinary=(Exception,))
        failures.run(
            lambda: p.write(
                self.root / "summary.json",
                p.canonical(
                    {
                        "sentinel": self.sentinel,
                        "runs": [
                            {
                                k: r.get(k)
                                for k in ("scenario", "complete", "reconciled", "fault")
                            }
                            for r in self.runs
                        ],
                        "unresolved": [
                            r["scenario"] for r in self.runs if not r["reconciled"]
                        ],
                        "sentinel_path": str(self.sentinel_path),
                        "sentinel_path_reserved": self.sentinel_path_reserved,
                        "sentinel_path_removed": self.sentinel_tree is not None
                        and self.sentinel_tree.removed,
                        "owner_failures": [
                            info
                            for r in self.runs
                            for info in r.get("owner_failures", [])
                        ],
                        "unresolved_source_cases": [
                            case["directory"]
                            for case in self.flow.cases
                            if not case["complete"]
                            or (
                                not case.get("fixture_reconciled", False)
                                and (
                                    not case.get("cleanup", {}).get(
                                        "confirmed_absent", False
                                    )
                                    or not case.get("cleanup", {}).get(
                                        "snapshot_deleted", False
                                    )
                                )
                            )
                        ],
                    }
                ),
            ),
            ordinary=(Exception,),
        )
        try:
            self.failure = failures.complete() or primary
        except BaseException as error:
            self.failure = error
        if self.failure is not None:
            raise self.failure
