"""Phase-10 public-surface attribution driver; scripted wheels are never installed."""

import base64
import hashlib
import importlib
import os
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from tests import mcp_attribution_support as reference
from tests import mcp_provenance as p
from tests import mcp_wheel_support as phase9
from tests.mcp_contract_support import prepare_inputs


def exercise(root, tmp, case):
    # Resolve outside the rejection boundary: missing implementation is RED,
    # never an expected attribution rejection or a fixture setup success.
    surface = importlib.import_module("tests.acceptance_build_support")
    candidate_dist = reference.dist_identity(root)
    prepared = prepare_inputs(root, tmp / "prepared")
    if not (
        sorted(path.name for path in prepared.iterdir())
        == sorted(name.rstrip("/") for name in case.phase10_profile)
    ):
        raise AssertionError(
            "verification failed: sorted((path.name for path in prepared.iterdir())) == sorted((name.rstrip('/') for name in case.phase10_profile))"
        )
    if not (
        all(
            not (prepared / name).exists()
            for name in ("tests", "host_tests", "scripts", "uv.lock", ".git")
        )
    ):
        raise AssertionError(
            "verification failed: all((not (prepared / name).exists() for name in ('tests', 'host_tests', 'scripts', 'uv.lock', '.git')))"
        )
    before, inputs = phase9.inventory(prepared)
    if not (before == phase9.inventory(root)[0]):
        raise AssertionError("prepared profile differs from declared common bytes")
    (prepared / "dist").mkdir()
    p.write(prepared / "dist/unrelated.whl", b"unowned stale bytes")
    if not case.attempted:
        p.write(prepared / "dist/footman-0.1.0-py3-none-any.whl", b"unowned collision")
    dist_before = reference.dist_identity(prepared)
    executable, call = reference.scripted_uv(tmp, case)
    path = str(executable.parent) + os.pathsep + os.environ["PATH"]
    evidence = tmp / "evidence"
    evidence.mkdir()
    result = failure = repeated = None

    def invoke():
        return surface.build_and_bind_wheel(
            prepared, evidence_directory=evidence, timeout_seconds=30
        )

    with patch.dict(os.environ, {"PATH": path}):
        try:
            result = invoke()
        except Exception as error:
            # Every rejection must retain a public operation observation. An
            # unrelated programming/setup error must fail here, not normalize.
            if not (hasattr(error, "observation")):
                raise AssertionError(repr(error)) from error
            failure = error
        after, after_inputs = phase9.inventory(prepared)
        normalized = reference.normalize(result, failure, before=before, after=after)
        if normalized["accepted"] is not case.accepted:
            raise AssertionError(
                "verification failed: normalized['accepted'] is case.accepted"
            )
        if normalized["attempted"] is not case.attempted:
            raise AssertionError(
                "verification failed: normalized['attempted'] is case.attempted"
            )
        if not (
            normalized["status"] == (case.build_status if case.attempted else None)
        ):
            raise AssertionError(
                "verification failed: normalized['status'] == (case.build_status if case.attempted else None)"
            )
        observation = result.command if result is not None else failure.observation
        if not (observation["argv"] == ["uv", "build"]):
            raise AssertionError(
                "verification failed: observation['argv'] == ['uv', 'build']"
            )
        if not (observation["cwd"] == str(prepared)):
            raise AssertionError(
                "verification failed: observation['cwd'] == str(prepared)"
            )
        if not (observation["pre_identity"] == before):
            raise AssertionError(
                "verification failed: observation['pre_identity'] == before"
            )
        if not (observation["post_identity"] == after):
            raise AssertionError(
                "verification failed: observation['post_identity'] == after"
            )
        if case.attempted:
            if not (observation["stdout"] == case.stdout_bytes):
                raise AssertionError(
                    "verification failed: observation['stdout'] == case.stdout_bytes"
                )
            if not (observation["stderr"] == case.stderr_bytes):
                raise AssertionError(
                    "verification failed: observation['stderr'] == case.stderr_bytes"
                )
            if not (
                p.load(call)
                == {
                    "argv": ["build"],
                    "cwd": str(prepared),
                    "PATH": path,
                }
            ):
                raise AssertionError(
                    "verification failed: p.load(call) == {'argv': ['build'], 'cwd': str(prepared), 'PATH': path}"
                )
        else:
            if not (not call.exists()):
                raise AssertionError("verification failed: not call.exists()")
            if not (reference.dist_identity(prepared) == dist_before):
                raise AssertionError(
                    "verification failed: reference.dist_identity(prepared) == dist_before"
                )
            if str(prepared) not in str(failure):
                raise AssertionError(
                    "verification failed: str(prepared) in str(failure)"
                )
            if str(prepared / "dist/footman-0.1.0-py3-none-any.whl") not in str(
                failure
            ):
                raise AssertionError(
                    "verification failed: str(prepared / 'dist/footman-0.1.0-py3-none-any.whl') in str(failure)"
                )
            if "no build/deletion/replacement" not in str(failure):
                raise AssertionError(
                    "verification failed: 'no build/deletion/replacement' in str(failure)"
                )
        if result is not None:
            if not (result.wheel == prepared / "dist/footman-0.1.0-py3-none-any.whl"):
                raise AssertionError(
                    "verification failed: result.wheel == prepared / 'dist/footman-0.1.0-py3-none-any.whl'"
                )
            if not (
                result.wheel_sha256 == hashlib.sha256(case.wheel_bytes).hexdigest()
            ):
                raise AssertionError(
                    "verification failed: result.wheel_sha256 == hashlib.sha256(case.wheel_bytes).hexdigest()"
                )
            if not (result.sdist == prepared / "dist/footman-0.1.0.tar.gz"):
                raise AssertionError(
                    "verification failed: result.sdist == prepared / 'dist/footman-0.1.0.tar.gz'"
                )
            if not (
                result.sdist_sha256 == hashlib.sha256(case.sdist_bytes).hexdigest()
            ):
                raise AssertionError(
                    "verification failed: result.sdist_sha256 == hashlib.sha256(case.sdist_bytes).hexdigest()"
                )
            if not (result.build_input_identity == before == after):
                raise AssertionError(
                    "verification failed: result.build_input_identity == before == after"
                )
            if not (result.inventory == tuple(inputs) == tuple(after_inputs)):
                raise AssertionError(
                    "verification failed: result.inventory == tuple(inputs) == tuple(after_inputs)"
                )
            try:
                result.wheel_sha256 = "0" * 64
            except FrozenInstanceError, AttributeError, TypeError:
                pass
            else:
                raise AssertionError("public build binding is mutable")
        if case.repeat:
            retained = reference.dist_identity(prepared)
            retained_call = p.read(call)
            try:
                invoke()
            except Exception as error:
                repeated = reference.normalize(None, error, before=after, after=after)
                if not (repeated["attempted"] is False and repeated["status"] is None):
                    raise AssertionError(
                        "verification failed: repeated['attempted'] is False and repeated['status'] is None"
                    ) from error
                if not (repeated["rejection_category"] == "output-collision"):
                    raise AssertionError(
                        "verification failed: repeated['rejection_category'] == 'output-collision'"
                    ) from error
                for name in ("footman-0.1.0.tar.gz", "footman-0.1.0-py3-none-any.whl"):
                    if str(prepared / "dist" / name) not in str(error):
                        raise AssertionError(
                            "verification failed: str(prepared / 'dist' / name) in str(error)"
                        ) from error
                if str(prepared) not in str(error):
                    raise AssertionError(
                        "verification failed: str(prepared) in str(error)"
                    ) from error
                if "no build/deletion/replacement" not in str(error):
                    raise AssertionError(
                        "verification failed: 'no build/deletion/replacement' in str(error)"
                    ) from error
            else:
                raise AssertionError("repeat build overwrote unowned outputs")
            if not (p.read(call) == retained_call):
                raise AssertionError(
                    "verification failed: p.read(call) == retained_call"
                )
            if not (reference.dist_identity(prepared) == retained):
                raise AssertionError(
                    "verification failed: reference.dist_identity(prepared) == retained"
                )
    prior = {entry["path"]: entry for entry in inputs}
    current = {entry["path"]: entry for entry in after_inputs}
    if not (
        {
            name
            for name in prior.keys() | current.keys()
            if prior.get(name) != current.get(name)
        }
        == ({case.mutation} if case.mutation else set())
    ):
        raise AssertionError(
            "verification failed: {name for name in prior.keys() | current.keys() if prior.get(name) != current.get(name)} == ({case.mutation} if case.mutation else set())"
        )
    if (before != after) is not (case.mutation is not None):
        raise AssertionError(
            "verification failed: (before != after) is (case.mutation is not None)"
        )
    if not (p.read(prepared / "dist/unrelated.whl") == b"unowned stale bytes"):
        raise AssertionError(
            "verification failed: p.read(prepared / 'dist/unrelated.whl') == b'unowned stale bytes'"
        )
    if not (reference.dist_identity(root) == candidate_dist):
        raise AssertionError(
            "verification failed: reference.dist_identity(root) == candidate_dist"
        )
    record = {
        "case_id": case.case_id,
        "profile": list(case.phase10_profile),
        "inventory": inputs,
        "post_inventory": after_inputs,
        "pre_identity": before,
        "post_identity": after,
        "PATH": path,
        "stub_path": str(executable),
        "stub_sha256": hashlib.sha256(p.read(executable)).hexdigest(),
        "stub_bytes_base64": base64.b64encode(p.read(executable)).decode(),
        "candidate_dist_pre": candidate_dist,
        "candidate_dist_post": reference.dist_identity(root),
        "prepared_dist_pre": dist_before,
        "prepared_dist_post": reference.dist_identity(prepared),
        "public_observations": [normalized] + ([] if repeated is None else [repeated]),
        "operation": {
            **observation,
            "stdout": base64.b64encode(observation["stdout"]).decode(),
            "stderr": base64.b64encode(observation["stderr"]).decode(),
        },
    }
    p.write(evidence / "observation.json", p.canonical(record))
    if not (p.load(evidence / "observation.json") == record):
        raise AssertionError(
            "verification failed: p.load(evidence / 'observation.json') == record"
        )
    return record


def compare(phase10, phase9_record):
    if not (phase10["case_id"] == phase9_record["case_id"]):
        raise AssertionError(
            "verification failed: phase10['case_id'] == phase9_record['case_id']"
        )
    if not (phase10["inventory"] == phase9_record["inventory"]):
        raise AssertionError(
            "verification failed: phase10['inventory'] == phase9_record['inventory']"
        )
    if not (phase10["post_inventory"] == phase9_record["post_inventory"]):
        raise AssertionError(
            "verification failed: phase10['post_inventory'] == phase9_record['post_inventory']"
        )
    fields = (
        "accepted",
        "attempted",
        "status",
        "wheel_sha256",
        "sdist_sha256",
        "rejection_category",
        "surface_status",
    )
    if not (
        len(phase10["public_observations"]) == len(phase9_record["public_observations"])
    ):
        raise AssertionError(
            "verification failed: len(phase10['public_observations']) == len(phase9_record['public_observations'])"
        )
    for left, right in zip(
        phase10["public_observations"],
        phase9_record["public_observations"],
        strict=True,
    ):
        if not (
            {key: left[key] for key in fields} == {key: right[key] for key in fields}
        ):
            raise AssertionError(
                "verification failed: {key: left[key] for key in fields} == {key: right[key] for key in fields}"
            )
        if (left["pre_identity"] == left["post_identity"]) is not (
            right["pre_identity"] == right["post_identity"]
        ):
            raise AssertionError(
                "verification failed: (left['pre_identity'] == left['post_identity']) is (right['pre_identity'] == right['post_identity'])"
            )
