"""Copied-host fixture boundary doubles; never real container/provenance proof."""

import base64
import shutil
import sys

from tests import host_acceptance_contract_support as h
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel
from tests.host_expectation_support import ASSERTION_SUPPORT


def profile(tmp, observations, *, negative, expectation=None, raw=None, teardown=False):
    workspace = tmp / "workspace"
    workspace.mkdir()
    shutil.copytree(
        h.ROOT / "host_tests",
        workspace / "host_tests",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "test_negative_expectations.py" if not negative else "test_execution.py",
        ),
    )
    (workspace / "tests").mkdir()
    for relative in ASSERTION_SUPPORT:
        p.write(workspace / relative, p.read(h.ROOT / relative))
    value = dict(observations)
    value["streams"] = {
        k: base64.b64encode(v).decode() for k, v in value["streams"].items()
    }
    for key in ("expected_stdout", "expected_stderr", "staged_bytes"):
        value[key] = base64.b64encode(value[key]).decode()
    p.write(workspace / "observations.json", p.canonical(value))
    # Double only the expensive HostFlow boundary. Actual delivered test bodies,
    # conftest validation/outcomes and copied shared assertions remain unchanged.
    plugin = """import base64,copy
from pathlib import Path
import pytest
from tests import mcp_provenance as p
from tests import host_acceptance_assertions as a
ROOT=Path(__file__).parents[1]
class Flow:
    def __init__(self, context):
        p.write(ROOT / "flow-acquired", b"explicit execution-boundary double")
        self.value=p.load(ROOT / "observations.json")
        self.value["streams"]={k:base64.b64decode(v) for k,v in self.value["streams"].items()}
        for key in ("expected_stdout","expected_stderr","staged_bytes"):
            self.value[key]=base64.b64decode(self.value[key])
    def prepare(self,kind,status,phase,preparation="fresh"):
        return {"preparation":preparation,"destination":self.value["cwd"]}
    def execute(self,scenario):
        value=copy.deepcopy(self.value)
        if scenario["preparation"]!="fresh":
            for mapping in (value["value"],value["manifest"]):
                mapping.update(outcome="dispatch_failed",return_code=None)
            value["manifest"]["docker_command_argv"]=[]
            value["manifest"]["infrastructure_operations"][1]["return_code"]=1
            value["manifest"]["infrastructure_operations"][2].update(argv=[],return_code=None)
            value["streams"].update(stdout=b"",stderr=b"")
        return value
    def verify(self,scenario,observed):
        a.verify_run(**self.value)
        return self.value
    def finish(self):
        TEARDOWN

def pytest_sessionstart(session):
    import host_tests.execution as execution
    execution.HostFlow=Flow
""".replace(
        "TEARDOWN",
        "raise RuntimeError('injected host teardown failure')" if teardown else "pass",
    )
    p.write(workspace / "tests/control.py", plugin.encode())
    p.write(workspace / "conftest.py", b'pytest_plugins = ("tests.control",)\n')
    # The module is an explicit development double, not copied normal support.
    # Its transitive imports must exist even though HostFlow itself is replaced.
    for relative in p.SUPPORT_FILES:
        target = workspace / relative
        if not target.exists():
            p.write(target, p.read(h.ROOT / relative))
    context = workspace / "context.json"
    p.write(context, b"{}")
    if expectation is not None or raw is not None:
        p.write(
            workspace / "host_tests/expectation_input.json",
            p.canonical(expectation) if raw is None else raw,
        )
    ini = tmp / "host.ini"
    p.write(ini, b"[pytest]\naddopts = --strict-config --strict-markers\n")
    suite = (
        workspace
        / "host_tests"
        / ("test_negative_expectations.py" if negative else "test_execution.py")
    )
    environment = wheel.clean_environment({"FOOTMAN_HOST_CONTEXT": str(context)})
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
            str(suite),
            "-n",
            "1",
            "--dist",
            "worksteal",
            "--tb=short",
            "-q",
        ],
        cwd=workspace,
        env=environment,
        timeout_seconds=60,
    )
    p.write(tmp / "operation.json", p.canonical(h.operation_record(result)))
    return workspace, result
