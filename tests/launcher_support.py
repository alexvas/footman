"""Ordered, per-request Docker boundary; only recorded fixture paths are read."""

import base64
import json
import os
import threading

from footman.process import OwnedProcess
from tests.dispatcher_harness import DockerStub, read_bytes


class DockerSequence:
    def __init__(self, directory, steps):
        self.stub = DockerStub.create(directory)
        self.steps = steps
        self.owners = []
        self.parameters = []
        self.lock = threading.Lock()

    def launch(self, argv, **kwargs):
        with self.lock:
            index = len(self.parameters)
            self.parameters.append((list(argv), kwargs.copy()))
            if index >= len(self.steps):
                raise AssertionError("unexpected additional Docker invocation")
            step = self.steps[index]
            record = self.stub.executable.parent / f"sequence-{index}.json"
            self.stub.records.append(record)
        scenario = {
            "stdout": base64.b64encode(step.get("stdout", b"")).decode(),
            "stderr": base64.b64encode(step.get("stderr", b"")).decode(),
            "stdout_repeat": step.get("stdout_repeat", 1),
            "stderr_repeat": step.get("stderr_repeat", 1),
            "status": step.get("status", 0),
            "delay": step.get("delay", 0),
            "container_id": step.get("container_id"),
            "record": str(record),
            "expected_argv": step["argv"][1:],
        }
        options = dict(kwargs.get("popen_kwargs") or {})
        environment = dict(os.environ if options.get("env") is None else options["env"])
        environment["FOOTMAN_STUB_SCENARIO"] = json.dumps(scenario)
        kwargs["popen_kwargs"] = {**options, "env": environment}
        # Only replace the executable at the test boundary. All actual Docker
        # operands pass unchanged to a real, sole-owned child running the stub.
        owner = OwnedProcess.launch([str(self.stub.executable), *argv[1:]], **kwargs)
        self.owners.append(owner)
        return owner

    def calls(self):
        return [json.loads(read_bytes(path)) for path in self.stub.records]

    def assert_owners_settled(self):
        """Check local completion only, retaining failures for scenario policy.

        A settled owner can retain an operation or release failure. Returning
        these same observations lets callers check failure policy separately,
        without another wait, collection, cleanup attempt or renewed budget.
        """
        observations = tuple(owner.cleanup_status for owner in self.owners)
        if not all(status.complete for status in observations):
            raise AssertionError(f"helper settlement incomplete: {observations!r}")
        return observations

    def assert_records(self):
        """Strict child evidence for every requested record, without tolerance."""
        for call, (argv, _) in zip(self.calls(), self.parameters, strict=True):
            if call["argv"] != argv[1:]:
                raise AssertionError("unexpected child argv")
            if call["stdin_eof"] is not True:
                raise AssertionError("every helper must receive closed stdin")

    def assert_settled(self):
        """Require completion and strict records, not operation success.

        As in existing negative-contract callers, the scenario must separately
        check retained failures. Completion does not certify successful release.
        """
        self.assert_owners_settled()
        self.assert_records()
