"""Isolated incompatible-factory controls; all descriptor numbers are synthetic.

Permanent fail-closed retention is intentional. Exit through the OS after writing
observations, without asking Python finalizers to release a conflicting group.
No real descriptor is acquired by these synthetic streams or the fake child.
"""

from __future__ import annotations

import gc
import io
import json
import os
import sys
import threading
import time
import traceback
import weakref

import footman.process as process


class Stream:
    def __init__(self, name, descriptor, counts, *, close_error=None, probe_error=None):
        self.name = name
        self.descriptor = descriptor
        self.counts = counts
        self.close_error = close_error
        self.probe_error = probe_error

    def fileno(self):
        if self.probe_error:
            raise self.probe_error
        if self.descriptor is None:
            raise io.UnsupportedOperation("synthetic descriptorless stream")
        return self.descriptor

    def read(self, size):
        return b""

    def close(self):
        self.counts[self.name] += 1
        if self.close_error:
            raise self.close_error

    def __del__(self):
        # Model a wrapper whose finalizer could close an old descriptor number.
        # Already-released synthetic streams have nothing left to finalize.
        if self.counts[self.name] == 0:
            self.counts[self.name] += 100


class Child:
    def __init__(self, stdout, stderr, stdin):
        self.stdout, self.stderr, self.stdin = stdout, stderr, stdin
        self.returncode = None
        self.actions = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.actions.append("terminate")
        self.returncode = -15

    def kill(self):
        self.actions.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.actions.append("wait")
        if self.returncode is None:
            raise process.subprocess.TimeoutExpired("synthetic", timeout)
        return self.returncode


def main():
    scenario, failure_name = sys.argv[1:]
    counts = dict(stdout=0, stderr=0, stdin=0)
    failure = {
        "none": None,
        "close": OSError("independent close"),
        "interrupt": KeyboardInterrupt("independent close"),
    }[failure_name]
    stdout = Stream("stdout", 100001, counts)
    stderr = Stream("stderr", 100002, counts)
    stdin = Stream("stdin", 100003, counts)
    if scenario in ("stdout", "stderr", "park-interrupt"):
        stdin.descriptor = (
            stderr.descriptor if scenario == "stderr" else stdout.descriptor
        )
        (stdout if scenario == "stderr" else stderr).close_error = failure
    elif scenario in (
        "probe",
        "probe-interrupt",
        "stdout-probe",
        "stdout-probe-interrupt",
    ):
        target = stdout if scenario.startswith("stdout-") else stdin
        target.probe_error = (
            KeyboardInterrupt("descriptor probe")
            if scenario.endswith("interrupt")
            else OSError("descriptor probe")
        )
        # This sibling has no descriptor and cannot share stdin's OS fd.
        stderr.descriptor = None
        stderr.close_error = failure
        del target
    elif scenario in ("fd-bool", "fd-negative", "fd-text"):
        stdin.descriptor = {"fd-bool": True, "fd-negative": -1, "fd-text": "100001"}[
            scenario
        ]
        stderr.descriptor = None
        stderr.close_error = failure
    elif scenario == "descriptorless-stdin":
        stdin.descriptor = None
        stderr.close_error = failure
    elif scenario not in ("distinct", "opposite"):
        raise ValueError("unknown fixture scenario")
    else:
        stderr.close_error = failure

    child = Child(stdout, stderr, stdin)
    refs = {
        name: weakref.ref(stream)
        for name, stream in (("stdout", stdout), ("stderr", stderr), ("stdin", stdin))
    }
    # Real OS I/O never uses the synthetic numbers. These seams affect this
    # fixture alone; the outer pytest child still uses its real process owner.
    process.os.set_blocking = lambda fd, blocking: None
    if scenario in ("distinct", "opposite"):
        # Distinct descriptors may identify the same inode/open description
        # (dup or opposite pipe ends). Only integer-fd equality is an alias.
        process.os.fstat = lambda fd: os.stat_result(
            (0, 42, 1, 1, 1000, 1000, 0, 0, 0, 0)
        )
    process.OwnedProcess._read_chunk = lambda self, index: b""
    launched = []
    retention_error = KeyboardInterrupt("retention observation wait")
    if scenario == "park-interrupt":
        original_make = process._make_continuation

        def make(action):
            prepared_owner = action.__self__
            original_wait = prepared_owner._finished.wait
            interrupted = False

            def wait(timeout=None):
                nonlocal interrupted
                if (
                    threading.current_thread() is prepared_owner._continuation
                    and not interrupted
                ):
                    interrupted = True
                    raise retention_error
                return original_wait(timeout)

            prepared_owner._finished.wait = wait
            return original_make(action)

        process._make_continuation = make
    owner = None
    try:
        owner = process.OwnedProcess.launch(
            ["synthetic"],
            stdin_mode="pipe",
            grace_seconds=0.03,
            factory=lambda *args, **kwargs: child,
            on_launched=lambda: launched.append(True),
        )
    except BaseException as error:
        owner = getattr(error, "cleanup", None)
        if owner is None:
            raise

    if scenario in ("distinct", "opposite"):
        owner.close_stdin(timeout_seconds=1)
        child.returncode = 0
    if scenario == "park-interrupt":
        observation_end = time.monotonic() + 1
        while not any(
            f.error is retention_error for f in owner.cleanup_status.failures
        ):
            if time.monotonic() >= observation_end:
                raise AssertionError("retention wait interruption was not published")
            owner.wait_cleanup(0.005)
    initial = owner.shutdown()
    deadline = owner._cleanup_end
    continuation = owner._continuation
    actions = list(child.actions)
    for _ in range(3):
        owner.shutdown()
        owner.wait_cleanup(0.005)
    status = owner.cleanup_status
    input_calls_rejected = []
    if owner._stdin_candidate is not None:
        for operation in (
            lambda: owner.write_stdin(b"", timeout_seconds=1),
            lambda: owner.close_stdin(timeout_seconds=1),
        ):
            try:
                operation()
            except process.StdinStateError:
                input_calls_rejected.append(True)
            else:
                input_calls_rejected.append(False)
    retained = {name: reference() for name, reference in refs.items()}
    child.stdin = child.stdout = child.stderr = None
    del stdin, stdout, stderr, retained
    # Do not let exception-frame locals accidentally supply the strong
    # references whose deliberate retention this control is meant to prove.
    for failure in status.failures:
        traceback.clear_frames(failure.error.__traceback__)
    gc.collect()
    result = dict(
        scenario=scenario,
        input_calls_rejected=input_calls_rejected,
        candidate_retained=owner._stdin_candidate is refs["stdin"](),
        stdin_ever_bound=owner._stdin_owner._stream is not None,
        descriptor_numbers=[
            refs[name]().descriptor for name in ("stdout", "stderr", "stdin")
        ],
        counts=counts,
        initial_complete=initial.complete,
        complete=status.complete,
        launched=bool(launched),
        alive={name: ref() is not None for name, ref in refs.items()},
        continuation_same=owner._continuation is continuation,
        continuation_alive=continuation.is_alive(),
        deadline_same=owner._cleanup_end == deadline,
        actions_same=child.actions == actions,
        returncode=owner.returncode,
        failures=[
            dict(
                kind=f.kind.name,
                phase=f.phase.name,
                stream=None if f.stream is None else f.stream.name,
                type=type(f.error).__name__,
                message=str(f.error),
            )
            for f in status.failures
        ],
        failures_stable=initial.failures == status.failures,
    )
    # No second output collection, additional subprocess owner or release of a
    # runtime-owned inherited stream. OS exit contains deliberate retention.
    payload = (json.dumps(result, sort_keys=True) + "\n").encode()
    written = sys.stdout.buffer.write(payload)
    if written != len(payload):
        raise OSError("short observation write")
    sys.stdout.buffer.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
