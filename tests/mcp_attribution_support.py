"""Recorded-PATH Phase-9 driver. Stub success always stops before installation."""

import base64
import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import patch

from footman.filesystem.descriptors import DirectoryDescriptor
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel


def check(value, message):
    if not value:
        raise AssertionError(message)


def dist_identity(root):
    dist = Path(root) / "dist"
    if not os.path.lexists(dist):
        return None

    def file(path, mode):
        check(stat.S_ISREG(mode), "unsupported dist node")
        return {
            "sha256": hashlib.sha256(p.read(path)).hexdigest(),
            "mode": mode & 0o777,
        }

    def visit(directory, path):
        result = {}
        for name in sorted(directory.list_names()):
            mode = directory.stat_child(name).st_mode
            if stat.S_ISLNK(mode):
                result[name] = {"link": os.readlink(name, dir_fd=directory.fd)}
            elif stat.S_ISDIR(mode):
                with directory.open_directory(name) as child:
                    result[name] = visit(child, path / name)
            else:
                result[name] = file(path / name, mode)
        return result

    mode = dist.lstat().st_mode
    if stat.S_ISLNK(mode):
        return {"link": os.readlink(dist)}
    if not stat.S_ISDIR(mode):
        return file(dist, mode)
    with DirectoryDescriptor.open_secure_path(p.OPS, dist) as directory:
        return visit(directory, dist)


def scripted_uv(tmp, case):
    tools = tmp / "tools"
    tools.mkdir()
    call = tools / "call.json"
    executable = tools / "uv"
    candidate = Path(__file__).resolve().parents[1]
    script = f"""#!{__import__("sys").executable}
import os, sys
from pathlib import Path
sys.path.insert(0, {str(candidate)!r})
from tests import mcp_provenance as p
root = Path.cwd()
p.write(Path({str(call)!r}), p.canonical({{"argv": sys.argv[1:], "cwd": str(root), "PATH": os.environ["PATH"]}}))
if sys.argv[1:] != ["build"]:
    raise SystemExit(17)
dist = root / "dist"
dist.mkdir(exist_ok=True)
p.write(dist / "footman-0.1.0.tar.gz", {case.sdist_bytes!r})
p.write(dist / "footman-0.1.0-py3-none-any.whl", {case.wheel_bytes!r})
mutation = {case.mutation!r}
if mutation:
    path = root / mutation
    original = p.read(path)
    path.unlink()
    p.write(path, original + b"\\n# deterministic case mutation\\n")
sys.stdout.buffer.write({case.stdout_bytes!r})
sys.stderr.buffer.write({case.stderr_bytes!r})
raise SystemExit({case.build_status!r})
"""
    p.write(executable, script.encode())
    executable.chmod(0o700)
    return executable, call


def normalize(result, failure, *, before, after):
    """Public-surface semantics, independent of implementation exception classes."""
    observation = result.command if result is not None else failure.observation
    attempted = observation["attempted"]
    category = None
    if failure is not None:
        if not attempted:
            category = "output-collision"
        elif observation["status"] != 0:
            category = "build-status"
        elif before != after:
            category = "input-change"
        else:
            category = "output-attribution"
    return {
        "accepted": result is not None,
        "attempted": attempted,
        "status": observation["status"],
        "argv": observation.get("argv"),
        "cwd": observation.get("cwd"),
        "wheel_path": None if result is None else str(result.wheel),
        "wheel_sha256": None if result is None else result.wheel_sha256,
        "sdist_path": None if result is None else str(result.sdist),
        "sdist_sha256": None if result is None else result.sdist_sha256,
        "pre_identity": before,
        "post_identity": after,
        "rejection_category": category,
        "surface_status": "returned" if failure is None else "raised",
        "exception_type": None if failure is None else type(failure).__name__,
        "exception": None if failure is None else str(failure),
    }


def exercise(root, tmp, case):
    from tests.mcp_contract_support import prepare_inputs

    before_candidate = dist_identity(root)
    prepared = prepare_inputs(root, tmp / "prepared")
    check(
        sorted(path.name for path in prepared.iterdir())
        == ["README.md", "pyproject.toml", "src"],
        "scripted fixture profile has undeclared/missing inputs",
    )
    check(
        all(
            not (prepared / name).exists()
            for name in ("tests", "host_tests", "scripts", "uv.lock", ".git")
        ),
        "identity-only roots leaked into minimal profile",
    )
    before, inputs = wheel.inventory(prepared)
    (prepared / "dist").mkdir()
    p.write(prepared / "dist/unrelated.whl", b"unowned unrelated stale bytes")
    if not case.attempted:
        p.write(prepared / "dist/footman-0.1.0-py3-none-any.whl", b"unowned collision")
    prepared_dist_before = dist_identity(prepared)
    executable, call = scripted_uv(tmp, case)
    env = wheel.clean_environment(
        {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    )
    output = tmp / "evidence"
    output.mkdir()
    real_command = wheel.command
    operations = []

    def command(argv, **kwargs):
        check(
            list(argv) == ["uv", "build"],
            "attribution-only boundary reached provisioning",
        )
        observation = real_command(argv, **kwargs)
        operations.append(observation)
        check(
            observation["status"] == case.build_status
            and observation["stdout"] == case.stdout_bytes
            and observation["stderr"] == case.stderr_bytes,
            "scripted tool differs from declared case status/streams",
        )
        return observation

    result = None
    failure = None
    repeated = None
    with patch.object(wheel, "command", command):
        try:
            result = wheel.build_and_install_contract_wheel(
                prepared, evidence_directory=output, attribution_only=True, env=env
            )
        except wheel.BuildFailure as error:
            failure = error
        check(
            (result is not None) is case.accepted,
            f"wrong attribution outcome: {case.case_id}: {failure}",
        )
        if case.repeat:
            retained = dist_identity(prepared)
            repeat_before, _ = wheel.inventory(prepared)
            try:
                wheel.build_and_install_contract_wheel(
                    prepared, evidence_directory=output, attribution_only=True, env=env
                )
            except wheel.BuildFailure as error:
                repeat_after, _ = wheel.inventory(prepared)
                repeated = normalize(
                    None, error, before=repeat_before, after=repeat_after
                )
                check(
                    repeated["rejection_category"] == "output-collision"
                    and repeated["pre_identity"] == repeated["post_identity"],
                    "repeat refusal lost its normalized public observation",
                )
                check(
                    error.observation["attempted"] is False
                    and error.observation["status"] is None,
                    "second invocation reached uv",
                )
                for name in ("footman-0.1.0.tar.gz", "footman-0.1.0-py3-none-any.whl"):
                    check(
                        str(prepared / "dist" / name) in str(error),
                        "collision diagnostic omits exact path",
                    )
                check(
                    str(prepared) in str(error)
                    and "no build/deletion/replacement" in str(error),
                    "collision root/no-delete diagnostic missing",
                )
            else:
                raise AssertionError(
                    "same-root second invocation overwrote prior outputs"
                )
            check(
                dist_identity(prepared) == retained,
                "repeat changed prior artifact/unrelated bytes",
            )
    after, after_inputs = wheel.inventory(prepared)
    prior_by_path = {item["path"]: item for item in inputs}
    after_by_path = {item["path"]: item for item in after_inputs}
    changed_paths = {
        name
        for name in prior_by_path.keys() | after_by_path.keys()
        if prior_by_path.get(name) != after_by_path.get(name)
    }
    check(
        changed_paths == ({case.mutation} if case.mutation is not None else set()),
        "case changed undeclared inputs or lost the declared mutation",
    )
    check(
        (before != after) is (case.mutation is not None),
        "case mutation identity relation mismatch",
    )
    check(
        len(operations) == int(case.attempted) and call.exists() is case.attempted,
        "uv attempt/status fabricated",
    )
    if case.attempted:
        invoked = p.load(call)
        check(
            invoked == {"argv": ["build"], "cwd": str(prepared), "PATH": env["PATH"]},
            "scripted tool argv/cwd/PATH drift",
        )
    if result is not None:
        check(
            result.environment is None
            and result.expected_origin is None
            and not (output / "venv").exists(),
            "stub success crossed install boundary",
        )
        check(
            result.wheel_sha256 == hashlib.sha256(case.wheel_bytes).hexdigest(),
            "exact wheel binding mismatch",
        )
        check(
            result.sdist_sha256 == hashlib.sha256(case.sdist_bytes).hexdigest(),
            "sdist attribution mismatch",
        )
    check(
        p.read(prepared / "dist/unrelated.whl") == b"unowned unrelated stale bytes",
        "unrelated stale artifact changed",
    )
    check(
        dist_identity(root) == before_candidate,
        "scripted fixture mutated candidate dist",
    )
    normalized = normalize(result, failure, before=before, after=after)
    expected_category = (
        None
        if case.accepted
        else "output-collision"
        if not case.attempted
        else "build-status"
        if case.build_status != 0
        else "input-change"
        if case.mutation
        else "output-attribution"
    )
    check(
        normalized["rejection_category"] == expected_category
        and normalized["attempted"] is case.attempted,
        "normalized attribution semantics mismatch",
    )
    record = {
        "case_id": case.case_id,
        "public_observations": [normalized] + ([] if repeated is None else [repeated]),
        "candidate_dist_pre": before_candidate,
        "candidate_dist_post": dist_identity(root),
        "prepared_dist_pre": prepared_dist_before,
        "prepared_dist_post": dist_identity(prepared),
        "wheel_path": None if result is None else str(result.wheel),
        "wheel_sha256": None if result is None else result.wheel_sha256,
        "sdist_path": None if result is None else str(result.sdist),
        "sdist_sha256": None if result is None else result.sdist_sha256,
        "failure_type": None if failure is None else type(failure).__name__,
        "candidate_revision": __import__("tests.dispatcher_harness", fromlist=["run"])
        .run(["git", "rev-parse", "HEAD"], cwd=root)
        .stdout.decode()
        .strip(),
        "profile": list(case.phase9_profile),
        "inventory": inputs,
        "post_inventory": after_inputs,
        "pre_identity": before,
        "post_identity": after,
        "stub_path": str(executable),
        "stub_bytes_base64": base64.b64encode(p.read(executable)).decode(),
        "stub_sha256": hashlib.sha256(p.read(executable)).hexdigest(),
        "PATH": env["PATH"],
        "accepted": result is not None,
        "attempted": case.attempted,
        "failure": None if failure is None else str(failure),
        "operations": [
            {
                **op,
                "stdout": base64.b64encode(op["stdout"]).decode(),
                "stderr": base64.b64encode(op["stderr"]).decode(),
            }
            for op in operations
        ],
    }
    p.write(output / "observation.json", p.canonical(record))
    check(
        p.load(output / "observation.json") == record,
        "attribution observation retention mismatch",
    )
