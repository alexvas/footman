"""Real-host scenario producer; development controls never imply Docker proof.

Only disposable source indexes are populated. The candidate checkout is never a
Git/Docker target. Generated fixtures and command evidence are retained outside
copied source files; every child and descriptor keeps its existing sole owner.
"""

import base64
import json
import os
import shlex
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

import anyio

from footman.filesystem.cleanup import CleanupFailures
from footman.filesystem.descriptors import DirectoryDescriptor
from footman.filesystem.operations import PosixDescriptorOps
from footman.process import OwnedProcess, ProcessStream, StreamOutcome
from host_tests.provenance import verified_session
from tests import host_acceptance_assertions as assertions
from tests import mcp_provenance as p
from tests.mcp_transport import TransportObservation

PAYLOAD = "staged host payload\n"
BINARY = b"binary\x00\xff\n"
ARGUMENTS = ["space argument", "$HOME;not-a-shell", "quote'\""]
PROBE = """import base64,json,os,stat,sys
from pathlib import Path
root=Path.cwd()
value={"payload":base64.b64encode((root/"payload").read_bytes()).decode(),
"binary":base64.b64encode((root/"binary").read_bytes()).decode(),
"payload_mode":stat.S_IMODE((root/"payload").stat().st_mode),
"wrapper_mode":stat.S_IMODE((root/"probe-wrapper").stat().st_mode),
"link":(root/"internal-link").is_symlink(),"target":os.readlink(root/"internal-link"),
"uid":os.getuid(),"cwd":str(root),"argv":sys.argv[1:],
"entries":sorted(item.name for item in root.iterdir())}
sys.stdout.buffer.write(json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(",",":")).encode()+b"\\nSTDOUT\\x00\\xff\\n")
sys.stderr.buffer.write(b"STDERR\\x00\\x80\\n")
raise SystemExit(int(sys.argv[-1]))
"""


def mkdir(path: Path) -> None:
    ops = PosixDescriptorOps()
    with DirectoryDescriptor.open_secure_path(ops, path.parent) as parent:
        with parent.create_directory(path.name, mode=0o700):
            pass


class Commands:
    """Original-envelope helper commands with independent retained evidence."""

    def __init__(self, root: Path, deadline: float):
        self.root = root
        self.deadline = deadline
        self.records: list[dict[str, Any]] = []
        self.owners: list[Any] = []

    def run(self, argv: list[str], cwd: Path) -> dict[str, Any]:
        remaining = min(60, self.deadline - time.monotonic())
        p.require(remaining > 0, "original host fixture deadline exhausted")
        index = len(self.records)
        record: dict[str, Any] = {"argv": argv, "cwd": str(cwd), "status": None}
        self.records.append(record)
        output = {stream: bytearray() for stream in ProcessStream}
        ends: dict[Any, Any] = {}
        errors: list[BaseException] = []
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PYTHON", "PYTEST", "GIT_", "FOOTMAN_"))
        } | {
            "PYTHONNOUSERSITE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        record["environment_keys"] = sorted(environment)
        primary = None
        owner = None

        def chunk(stream: Any, data: bytes) -> None:
            try:
                output[stream].extend(data)
            except BaseException as error:
                errors.append(error)
                raise

        try:
            reserve = min(2, remaining / 4)
            with OwnedProcess.launch(
                argv,
                deadline_seconds=remaining - reserve,
                grace_seconds=reserve,
                capture_limit=0,
                popen_kwargs={"cwd": str(cwd), "env": environment},
            ) as owner:
                self.owners.append(owner)
                result = owner.stream(
                    chunk, on_stream_end=lambda s, e: ends.__setitem__(s, e)
                )
                record["status"] = result.returncode
                if errors:
                    raise errors[0]
                if result.lifecycle.failures:
                    raise result.lifecycle.failures[0].error
                p.require(
                    not result.timed_out
                    and all(ends.get(s) is StreamOutcome.EOF for s in ProcessStream),
                    "host helper output incomplete",
                )
            p.require(
                owner.cleanup_status.complete and not owner.cleanup_status.failures,
                "host helper cleanup incomplete",
            )
        except BaseException as error:
            primary = error
        if owner is not None:
            record["status"] = owner.returncode
        record["cleanup_complete"] = owner is not None and owner.cleanup_status.complete
        record["terminals"] = {s.name: e.name for s, e in ends.items()}
        failures = CleanupFailures(primary)
        for stream in ProcessStream:
            name = stream.name.lower()
            record[name + "_path"] = str(self.root / f"command-{index}-{name}.txt")
            failures.run(
                lambda stream=stream, name=name: p.write(
                    Path(record[name + "_path"]), bytes(output[stream])
                ),
                ordinary=(Exception,),
            )
        failures.run(
            lambda: p.write(self.root / f"command-{index}.json", p.canonical(record)),
            ordinary=(Exception,),
        )
        selected = failures.complete()
        if selected is None:
            selected = primary
        if selected is not None:
            raise selected
        p.require(record["status"] == 0, "host helper failed: " + repr(record))
        return record | {
            "stdout": bytes(output[ProcessStream.STDOUT]),
            "stderr": bytes(output[ProcessStream.STDERR]),
        }


class HostFlow:
    def __init__(self, context: dict[str, Any]):
        self.context = dict(context)
        self.root = Path(context["support_root"]) / ("host-runs-" + uuid.uuid4().hex)
        if context.get("pytest_workers", 1) > 1:
            # Each worker owns one exclusively reserved flow root. Shared
            # support/expected-origin/install inputs remain read-only.
            self.context["provenance_evidence_path"] = str(
                self.root / "provenance.json"
            )
            self.context["service_observations_path"] = str(
                self.root / "service-observations.json"
            )
        self.commands = Commands(self.root, context["service_deadline_monotonic"])
        self.cases: list[dict[str, Any]] = []
        self.services: list[Any] = []
        self.finish_started = False
        self.finish_error: BaseException | None = None
        mkdir(self.root)

    def git(self, repository: Path, *args: str) -> bytes:
        return self.commands.run(["git", "-C", str(repository), *args], repository)[
            "stdout"
        ]

    def prepare(
        self,
        kind: str,
        status: int,
        phase: str | None,
        preparation: str = "fresh",
        *,
        probe: str = PROBE,
    ) -> dict[str, Any]:
        case = self.root / ("case-" + str(len(self.cases)))
        mkdir(case)
        self.cases.append({"directory": str(case), "complete": False})
        repository = case / "repository"
        mkdir(repository)
        self.git(repository, "init", "--quiet")
        wrapper = (
            "#!/bin/sh\nexec python3 -c " + shlex.quote(probe) + ' "$@"\n'
        ).encode()
        for name, data, mode in (
            ("payload", PAYLOAD.encode(), "100644"),
            ("binary", BINARY, "100644"),
            ("probe-wrapper", wrapper, "100755"),
        ):
            p.write(repository / name, data)
            (repository / name).chmod(int(mode[-3:], 8))
            digest = (
                self.git(repository, "hash-object", "-w", "--no-filters", "--", name)
                .decode()
                .strip()
            )
            self.git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                mode + "," + digest + "," + name,
            )
        p.write(case / "link-target", b"payload")
        digest = (
            self.git(
                repository,
                "hash-object",
                "-w",
                "--no-filters",
                "--",
                str(case / "link-target"),
            )
            .decode()
            .strip()
        )
        self.git(
            repository,
            "update-index",
            "--add",
            "--cacheinfo",
            "120000," + digest + ",internal-link",
        )
        (repository / "internal-link").symlink_to("payload")
        tree = self.git(repository, "write-tree").decode().strip()
        (repository / "payload").unlink()
        p.write(repository / "payload", b"later unstaged workdir bytes\n")
        p.write(repository / "untracked", b"not staged\n")
        p.write(repository / ".gitignore", b"ignored\n")
        p.write(repository / "ignored", b"not staged\n")
        snapshot = case / "snapshots"
        mkdir(snapshot)
        output = case / "results"
        mkdir(output)
        destination = "/tmp/footman-source-" + uuid.uuid4().hex
        if preparation == "missing-parent":
            destination += "/source"
        elif preparation == "existing":
            destination = "/tmp"
        arguments = [*ARGUMENTS, str(status)]
        command = (
            ["python3", "-c", probe, *arguments]
            if kind == "image"
            else ["./probe-wrapper", *arguments]
        )
        config = case / "config.toml"
        settings = {
            "version": 1,
            "repository": str(repository),
            "container_start_argv": [
                "docker",
                "run",
                "--detach=true",
                "--pull=never",
                "--entrypoint=sleep",
                "--",
                "{image_id}",
                "infinity",
            ],
            "image_id": self.context["configured_image_id"],
            "user": "root",
            "command_argv": command,
            "working_directory": ".",
            "snapshot_directory": str(snapshot),
            "container_snapshot_directory": destination,
            "output_dir": str(output / "{utc_datetime}"),
            "timeout_seconds": 60,
            "cleanup_grace_seconds": 10,
        }
        p.write(
            config,
            (
                "\n".join(
                    key + " = " + json.dumps(value) for key, value in settings.items()
                )
                + "\n"
            ).encode(),
        )
        before = (
            p.read(repository / ".git/index"),
            p.read(repository / "payload"),
            self.git(repository, "status", "--porcelain=v1", "-z"),
        )
        return {
            "case": case,
            "repository": repository,
            "config": config,
            "tree": tree,
            "command": command,
            "status": status,
            "phase": phase,
            "destination": destination,
            "arguments": arguments,
            "before": before,
            "preparation": preparation,
        }

    def execute(self, scenario: dict[str, Any]) -> dict[str, Any]:
        context = dict(self.context)
        if self.services:
            context["provenance_evidence_path"] = str(
                scenario["case"] / "provenance.json"
            )
        observation = TransportObservation()
        self.services.append(observation)

        async def call() -> Any:
            async with verified_session(
                context, scenario["config"], observation
            ) as session:
                tools = (await session.list_tools()).tools
                assertions.require(
                    [t.name for t in tools] == ["run_privileged_command"],
                    "unexpected MCP tools",
                )
                return await session.call_tool(
                    "run_privileged_command",
                    {} if scenario["phase"] is None else {"phase": scenario["phase"]},
                )

        primary = None
        result = None
        try:
            result = anyio.run(call)
        except BaseException as error:
            primary = error
        failures = CleanupFailures(primary)
        failures.run(
            lambda: p.write(
                scenario["case"] / "service-stderr-tail.txt",
                bytes(observation.stderr_tail),
            ),
            ordinary=(Exception,),
        )
        selected = failures.complete()
        if selected is None:
            selected = primary
        if selected is not None:
            raise selected
        if result is None:
            raise AssertionError("service produced no result")
        value = result.structuredContent
        assertions.require(type(value) is dict, "missing MCP result")
        manifest = p.load(Path(value["resultPath"]))
        streams = {
            name: p.read(Path(value[field]))
            for name, field in (
                ("stdout", "stdoutPath"),
                ("stderr", "stderrPath"),
                ("startup_stdout", "startupStdoutPath"),
                ("startup_stderr", "startupStderrPath"),
            )
        }
        repository = scenario["repository"]
        assertions.require(
            (
                p.read(repository / ".git/index"),
                p.read(repository / "payload"),
                self.git(repository, "status", "--porcelain=v1", "-z"),
            )
            == scenario["before"],
            "dispatcher changed disposable development state",
        )
        assertions.require(
            result.isError
            is scenario.get(
                "expected_error",
                scenario["status"] != 0 or scenario["preparation"] != "fresh",
            ),
            "MCP application outcome mismatch",
        )
        record = self.cases[-1]
        record.update(
            complete=True,
            result=value,
            provenance_evidence_path=context["provenance_evidence_path"],
            expected_origin_path=context["expected_origin_path"],
            wheel_sha256=context["wheel_sha256"],
            authority="bound wheel archive; parent matched child attestation after EOF",
            stderr_tail_path=str(scenario["case"] / "service-stderr-tail.txt"),
            stderr_tail_limit=65536,
            service_status=getattr(observation.result, "returncode", None),
            service_cwd=context["neutral_cwd"],
            service_import_environment="explicit clean environment; no PYTHONPATH; interpreter -P",
            cleanup=manifest["cleanup"],
            server_argv=p.load(Path(context["provenance_evidence_path"]))["argv"],
        )
        p.write(scenario["case"] / "observation.json", p.canonical(record))
        metadata = self.context.get("service_observations_path")
        if metadata and len(self.services) == 1:
            p.write(
                Path(metadata),
                p.canonical(
                    {
                        "configured_image_id": manifest["configured_image_id"],
                        "observed_image_id": manifest["observed_image_id"],
                    }
                ),
            )
        return {"value": value, "manifest": manifest, "streams": streams}

    def verify(
        self, scenario: dict[str, Any], observed: dict[str, Any]
    ) -> dict[str, Any]:
        destination = scenario["destination"]
        expected = {
            "payload": base64.b64encode(PAYLOAD.encode()).decode(),
            "binary": base64.b64encode(BINARY).decode(),
            "payload_mode": 0o644,
            "wrapper_mode": 0o755,
            "link": True,
            "target": "payload",
            "uid": 0,
            "cwd": destination,
            "argv": scenario["arguments"],
            "entries": ["binary", "internal-link", "payload", "probe-wrapper"],
        }
        probe = json.loads(observed["streams"]["stdout"].split(b"\n", 1)[0])
        options = dict(
            value=observed["value"],
            manifest=observed["manifest"],
            streams=observed["streams"],
            tree_id=scenario["tree"],
            status=scenario["status"],
            phase=scenario["phase"],
            command_argv=scenario["command"],
            user="root",
            cwd=destination,
            configured_image=self.context["configured_image_id"],
            expected_stdout=p.canonical(expected) + b"\nSTDOUT\x00\xff\n",
            expected_stderr=b"STDERR\x00\x80\n",
            snapshot_present=os.path.lexists(observed["manifest"]["snapshot"]["path"]),
            staged_bytes=base64.b64decode(probe["payload"], validate=True),
            expected_staged_bytes=PAYLOAD,
        )
        assertions.verify_run(**options)
        assertions.require(
            probe == expected,
            "actual copied bytes/modes/symlink/root/cwd/argv mismatch",
        )
        assertions.verify_copy(observed["manifest"], destination)
        settings = tomllib.loads(p.read(scenario["config"]).decode())
        launch = [
            self.context["configured_image_id"] if arg == "{image_id}" else arg
            for arg in settings["container_start_argv"]
        ]
        assertions.require(
            observed["manifest"]["startup"]["argv"] == launch,
            "configured launch argv mismatch",
        )
        return options

    def finish(self) -> None:
        if self.finish_started:
            if self.finish_error is not None:
                raise self.finish_error
            return
        self.finish_started = True
        failures = CleanupFailures(None)
        failures.run(
            lambda: p.write(
                self.root / "summary.json",
                p.canonical({"cases": self.cases, "commands": self.commands.records}),
            ),
            ordinary=(Exception,),
        )
        failures.run(
            lambda: assertions.require(
                all(
                    case["complete"]
                    and case.get("cleanup", {}).get("confirmed_absent") is True
                    and (
                        case["cleanup"].get("snapshot_deleted") is True
                        or (
                            case.get("expected_retention") is True
                            and case.get("fixture_reconciled") is True
                            and case.get("runtime_outcome") == "dispatch_failed"
                            and case["cleanup"].get("resources_complete") is True
                            and isinstance(case["cleanup"].get("failure"), dict)
                        )
                    )
                    and not case["cleanup"].get("unresolved_resources")
                    for case in self.cases
                ),
                "host fixture scenario incomplete; retain evidence for reconciliation",
            ),
            ordinary=(Exception,),
        )
        failures.run(
            lambda: assertions.require(
                all(
                    owner.cleanup_status.complete and not owner.cleanup_status.failures
                    for owner in self.commands.owners
                ),
                "host fixture helper resources unsettled",
            ),
            ordinary=(Exception,),
        )
        failures.run(
            lambda: assertions.require(
                all(
                    item.owner is not None
                    and item.owner.cleanup_status.complete
                    and not item.owner.cleanup_status.failures
                    for item in self.services
                ),
                "host fixture service resources unsettled",
            ),
            ordinary=(Exception,),
        )
        try:
            selected = failures.complete()
            if selected is not None:
                raise selected
        except BaseException as error:
            self.finish_error = error
            raise
