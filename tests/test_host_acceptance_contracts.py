"""Phase-11 Docker-free RED contracts, not real-container acceptance.

The first missing-interface assertion is deliberately outside expected rejection
boundaries: a missing public wrapper must not normalize to a passing negative
case. Production static roots remain unchanged until GREEN task 11.3.
"""

import ast
import json
import os
import re
import tomllib
from pathlib import Path

import pytest

from tests import host_acceptance_contract_support as h
from tests import mcp_attribution_support as attribution
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel
from tests.python_check_fixtures import load_check_module
from tests.test_green_gate import assert_documented_suite
from tests.test_python_check_lifecycle import scannable as scannable
from tests.wheel_attribution_cases import CASES


@pytest.mark.parametrize(
    "relative", ["docs/testing.md", "docs/python-checks.md", "README.md"]
)
def test_host_registration_preserves_documented_development_commands(relative):
    assert_documented_suite(p.read(h.ROOT / relative).decode())


def test_phase11_real_base_serialization_is_explicit_without_changing_commands():
    text = p.read(h.ROOT / "docs/testing.md").decode()
    scheduling = text.split("## Working schedule: gate plus specialised suites", 1)[
        1
    ].split("## Current development prerequisites", 1)[0]
    phase = text.split("## Phase 11 host-runner RED contracts", 1)[1]
    assert "Phase-11 real-base serialization exception" in scheduling
    assert "invocations must not overlap" in scheduling
    assert "settlement before starting the registered Phase-11 command" in scheduling
    assert "even with at least 32 available CPUs" in scheduling
    assert "sequential fallback" in scheduling
    assert (
        "neither registered commands nor" in scheduling
        and "worker counts" in scheduling
    )
    assert "Phase-11 real-base serialization exception" in phase
    assert "do not overlap" in phase
    assert_documented_suite(text)


def test_host_interfaces_are_registered_before_red():
    text = p.read(h.ROOT / "docs/testing.md").decode()
    assert "scripts/build-acceptance-wheel --manifest <build-manifest>" in text
    assert (
        "scripts/run-host-acceptance --wheel <exact-wheel> --source-identity <build-manifest> "
        "--image <configured-image-id> --evidence <directory>"
    ) in text
    assert "host_tests/" in text
    assert (
        ".venv/bin/python -m pytest tests/test_host_acceptance_contracts.py "
        "-n 2 --dist worksteal --tb=short -q"
    ) in text
    queue = text.split("| Queue priority | Registered suite | Workers |", 1)[1]
    queue = queue.split("\n\n", 1)[0]
    rows = re.findall(r"^\| (\d+) \| (.*?) \| (\d+) \|$", queue, re.MULTILINE)
    assert [int(priority) for priority, _, _ in rows] == list(range(1, 14))
    assert rows[-3] == ("11", "Phase 10 integrated acceptance and portability", "2")
    assert rows[-2] == (
        "12",
        "Phase 11 Docker-free host-runner contracts "
        "(`tests/test_host_acceptance_contracts.py`)",
        "2",
    )
    assert rows[-1] == (
        "13",
        "Standalone Linux proc-observer regression (`tests/test_proc_observer.py`)",
        "2",
    )
    assert "Historical Phase-10 validation covers eleven commands" in text
    assert "(through queue priority 11)" in text


@pytest.mark.parametrize("name", h.SURFACES)
def test_host_entrypoint_is_executable_shell_wrapper_with_python_implementation(name):
    wrapper = h.required_surface(name)
    data = p.read(wrapper)
    assert data.startswith(b"#!/bin/sh\n")
    implementation = name.replace("-", "_") + ".py"
    assert implementation.encode() in data
    assert (h.ROOT / "scripts" / implementation).is_file()
    assert b".venv/bin" not in data


@pytest.mark.parametrize("name", h.SURFACES)
def test_bootstrap_implementation_uses_normal_explicit_foundation_imports(name):
    h.required_surface(name)
    implementation = h.ROOT / "scripts" / (name.replace("-", "_") + ".py")
    data = p.read(implementation).decode()
    tree = ast.parse(data)
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert any(node.module and node.module.startswith("footman.") for node in imports)
    assert all(node.module != "footman.filesystem" for node in imports)
    assert "sys.path" not in data
    assert "E402" not in data
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("tests")
            )
            or (
                isinstance(node, ast.Import)
                and any(a.name.startswith("tests") for a in node.names)
            )
        )
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize("name", h.SURFACES)
@pytest.mark.parametrize("missing", ["python3.14", "uv"])
def test_bootstrap_missing_tool_has_diagnostic_and_status_127(tmp_path, name, missing):
    h.required_surface(name)
    root, _ = h.prepared_root(tmp_path)
    tools = h.isolated_path(
        tmp_path, python=missing != "python3.14", uv=missing != "uv"
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        name,
        ["--help"],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment({"PATH": str(tools)}),
    )
    assert result["status"] == 127, result
    assert missing.encode() in result["stderr"]
    assert not (root / ".venv").exists()
    assert not (root / "dist").exists()


@pytest.mark.parametrize("name", h.SURFACES)
def test_bootstrap_help_works_without_checkout_venv_from_unrelated_cwd(tmp_path, name):
    h.required_surface(name)
    root, _ = h.prepared_root(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    neutral = tmp_path / "unrelated"
    neutral.mkdir()
    result = h.invoke(root, name, ["--help"], cwd=neutral, evidence=evidence)
    assert result["status"] == 0, result
    assert b"usage" in result["stdout"].lower()
    assert not (root / ".venv").exists()
    assert not (root / "dist").exists()


@pytest.mark.parametrize("name", h.SURFACES)
def test_missing_checkout_foundation_cannot_fall_back_to_ambient_install(
    tmp_path, name
):
    wrapper = h.required_surface(name)
    root = tmp_path / "missing-foundation"
    (root / "scripts").mkdir(parents=True)
    (root / "src").mkdir()
    implementation = name.replace("-", "_") + ".py"
    for source in (wrapper, h.ROOT / "scripts" / implementation):
        target = root / "scripts" / source.name
        p.write(target, p.read(source))
        target.chmod(source.stat().st_mode & 0o777)
    poison = tmp_path / "ambient"
    (poison / "footman").mkdir(parents=True)
    p.write(
        poison / "footman/__init__.py",
        b"import sys\nsys.stderr.buffer.write(b'AMBIENT_FOUNDATION_EXECUTED\\n')\n"
        b"raise RuntimeError('ambient foundation must not execute')\n",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        name,
        ["--help"],
        cwd=tmp_path,
        evidence=evidence,
        env={**wheel.clean_environment(), "PYTHONPATH": str(poison)},
    )
    assert result["status"] != 0
    assert result["stderr"]
    assert b"AMBIENT_FOUNDATION_EXECUTED" not in result["stderr"]
    assert not (root / "dist").exists()
    assert not (root / ".venv").exists()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_production_build_wrapper_attributes_shared_cases_without_installing(
    tmp_path, case
):
    from tests import acceptance_attribution_support as phase10

    record = h.build_case(tmp_path, case)
    earlier = []
    for name, driver in (("phase9", attribution), ("phase10", phase10)):
        scratch = tmp_path / name
        scratch.mkdir()
        earlier.append(driver.exercise(h.ROOT, scratch, case))
    phase10.compare(earlier[1], earlier[0])
    for previous in earlier:
        assert record["inventory"] == previous["inventory"]
        assert record["post_inventory"] == previous["post_inventory"]
        observation = previous["public_observations"][0]
        assert observation["accepted"] is record["accepted"]
        assert observation["attempted"] is record["attempted"]
    assert record["case_id"] == case.case_id
    assert record["accepted"] is case.accepted
    assert record["attempted"] is case.attempted
    assert record["configured_tool_status"] == case.build_status
    assert record["candidate_dist_pre"] == record["candidate_dist_post"]


@pytest.mark.parametrize(
    "artifact", ["footman-0.1.0.tar.gz", "footman-0.1.0-py3-none-any.whl"]
)
@pytest.mark.parametrize("kind", ["file", "directory", "dangling-link"])
def test_build_refuses_either_unowned_destination_without_invoking_uv(
    tmp_path, artifact, kind
):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    (root / "dist").mkdir()
    target = root / "dist" / artifact
    if kind == "file":
        p.write(target, b"unowned artifact")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to("missing-unowned-target")
    p.write(root / "dist/unrelated.whl", b"unrelated bytes")
    before = attribution.dist_identity(root)
    executable, call = attribution.scripted_uv(tmp_path, CASES[0])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "manifest.json")],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    assert result["status"] != 0
    diagnostic = (result["stdout"] + result["stderr"]).decode("utf-8", "replace")
    assert str(root) in diagnostic
    assert str(target) in diagnostic
    assert "no build/deletion/replacement" in diagnostic
    assert not call.exists()
    assert not (evidence / "manifest.json").exists()
    assert attribution.dist_identity(root) == before


@pytest.mark.parametrize(
    "old,new",
    [
        (b'name = "footman"', b'name = "other-name"'),
        (b'version = "0.1.0"', b'version = "0.1.0+local"'),
        (b'readme = "README.md"', b'readme = "OTHER.md"'),
        (b'packages = ["src/footman"]', b'packages = ["src/other"]'),
    ],
    ids=["name", "noncanonical-version", "unsupported-readme", "package-profile"],
)
def test_unsupported_build_profile_is_rejected_before_uv(tmp_path, old, new):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    profile = p.read(root / "pyproject.toml")
    assert old in profile, "contract fixture no longer matches reviewed static profile"
    h.replace(root / "pyproject.toml", profile.replace(old, new))
    executable, call = attribution.scripted_uv(tmp_path, CASES[0])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "manifest.json")],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    assert result["status"] != 0
    assert result["stderr"]
    assert not call.exists()
    assert not (evidence / "manifest.json").exists()


@pytest.mark.parametrize(
    "setting", ["UV_BUILD_OUT_DIR", "UV_CONFIG_FILE", "HATCH_BUILD_HOOKS_ENABLE"]
)
def test_output_changing_environment_is_rejected_before_uv(tmp_path, setting):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    executable, call = attribution.scripted_uv(tmp_path, CASES[0])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "manifest.json")],
        cwd=tmp_path,
        evidence=evidence,
        env={
            **wheel.clean_environment(),
            "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"],
            setting: "unreviewed",
        },
    )
    assert result["status"] != 0
    assert not call.exists()
    assert not (evidence / "manifest.json").exists()


def test_real_uv_repeat_build_preserves_both_outputs_and_candidate_dist(tmp_path):
    h.required_surface("build-acceptance-wheel")
    before_candidate = attribution.dist_identity(h.ROOT)
    root, _ = h.prepared_root(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    first = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "first.json")],
        cwd=tmp_path,
        evidence=evidence,
        label="real-first",
    )
    assert first["status"] == 0, first
    before_repeat = attribution.dist_identity(root)
    assert set(before_repeat) == {
        "footman-0.1.0.tar.gz",
        "footman-0.1.0-py3-none-any.whl",
    }
    second = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "second.json")],
        cwd=tmp_path,
        evidence=evidence,
        label="real-second",
    )
    assert second["status"] != 0
    assert not (evidence / "second.json").exists()
    assert attribution.dist_identity(root) == before_repeat
    assert attribution.dist_identity(h.ROOT) == before_candidate


def test_four_root_inventory_and_shared_fixture_tuple():
    from tests import python_check_fixtures

    checks = load_check_module()
    assert python_check_fixtures.SOURCE_ROOTS == h.SOURCE_ROOTS
    assert checks.SOURCE_ROOTS == h.SOURCE_ROOTS
    assert checks._DISCOVERY_ARGS == h.SOURCE_ROOTS


def test_four_root_disposable_inventory_selects_host_python_and_excludes_shell(
    tmp_path,
):
    checks = load_check_module()
    for name in h.SOURCE_ROOTS:
        (tmp_path / name).mkdir()
    p.write(tmp_path / "host_tests/__init__.py", b"")
    p.write(tmp_path / "host_tests/test_host.py", b"def test_host():\n    pass\n")
    for name in h.SURFACES:
        p.write(tmp_path / "scripts" / name, b"#!/bin/sh\nexit 0\n")
        p.write(tmp_path / "scripts" / (name.replace("-", "_") + ".py"), b"value = 1\n")
    selection = checks.select_sources(tmp_path)
    assert "host_tests/__init__.py" in selection.selected
    assert "host_tests/test_host.py" in selection.selected
    for name in h.SURFACES:
        assert "scripts/" + name not in selection.selected
        assert "scripts/" + name.replace("-", "_") + ".py" in selection.selected
        assert any(
            entry.path == "scripts/" + name and entry.reason
            for entry in selection.exclusions
        )


@pytest.mark.parametrize("missing", h.SOURCE_ROOTS)
def test_each_missing_source_root_is_still_an_error(tmp_path, missing):
    checks = load_check_module()
    for name in h.SOURCE_ROOTS:
        if name != missing:
            (tmp_path / name).mkdir()
    with pytest.raises(checks.SourceSelectionError, match=missing):
        checks.select_sources(tmp_path)


def test_fixture_tree_builders_consume_one_shared_source_root_tuple():
    for relative in (
        "tests/test_python_syntax_compilation.py",
        "tests/test_python_check_lifecycle.py",
        "tests/test_python_source_selection.py",
    ):
        tree = ast.parse(p.read(h.ROOT / relative))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert any(
            node.module == "tests.python_check_fixtures"
            and any(
                alias.name in {"SOURCE_ROOTS", "prepare_roots"} for alias in node.names
            )
            for node in imports
        ), relative
        # A stale independent triple would silently omit the host root even if
        # some other fixture already imports prepare_roots.
        triples = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Tuple, ast.List))
            and all(isinstance(value, ast.Constant) for value in node.elts)
            and [value.value for value in node.elts] == ["src", "tests", "scripts"]
        ]
        assert not triples, relative
    from tests.test_python_syntax_compilation import _clean_repository_copy

    assert "prepare_roots" in _clean_repository_copy.__code__.co_names


def test_ty_scope_and_entrypoint_include_host_but_not_development_tests():
    config = tomllib.loads(p.read(h.ROOT / "pyproject.toml").decode())
    assert config["tool"]["ty"]["src"]["include"] == ["src", "host_tests"]
    wrapper = p.read(h.ROOT / "scripts/check-types").decode()
    assert re.search(
        r'exec\s+"\$ty"\s+check\s+--output-format\s+concise\s+src\s+host_tests\s*$',
        wrapper,
    )


def test_normal_host_support_closure_excludes_development_protocol_peer():
    assert "tests/mcp_protocol_child.py" not in p.SUPPORT_FILES
    assert {
        "tests/__init__.py",
        "tests/green_skip_policy.py",
        "tests/mcp_transport.py",
        "tests/mcp_server_child.py",
    } <= set(p.SUPPORT_FILES)
    for relative in p.SUPPORT_FILES:
        tree = ast.parse(p.read(h.ROOT / relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "mcp_protocol_child" not in (node.module or "")
                assert all(alias.name != "mcp_protocol_child" for alias in node.names)
            elif isinstance(node, ast.Import):
                assert all(
                    "mcp_protocol_child" not in alias.name for alias in node.names
                )


def test_mandated_development_collection_contains_no_host_items(tmp_path):
    result = wheel.command(
        [
            h.ROOT / ".venv/bin/python",
            "-m",
            "pytest",
            "tests",
            "--collect-only",
            "-q",
            "-n",
            "8",
            "--dist",
            "worksteal",
        ],
        cwd=h.ROOT,
        env=wheel.clean_environment(),
        timeout_seconds=120,
    )
    p.write(
        tmp_path / "collection-operation.json", p.canonical(h.operation_record(result))
    )
    assert result["status"] == 0, result
    nodes = [line for line in result["stdout"].decode().splitlines() if "::" in line]
    assert nodes, "empty collection cannot prove the development/host boundary"
    assert any("test_host_acceptance_contracts.py::" in line for line in nodes)
    assert all(not line.startswith("host_tests/") for line in nodes)


@pytest.mark.parametrize("image", ["tag:latest", "sha256:abcd", "0" * 11, "0" * 65])
def test_host_runner_rejects_invalid_image_before_install_or_docker(tmp_path, image):
    h.required_surface("run-host-acceptance")
    root, _ = h.prepared_root(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    tools = h.isolated_path(tmp_path)
    # An explicit fail-fast Docker canary is never a Docker implementation.
    # Its marker must stay absent: syntax rejection precedes all managed work.
    marker = tmp_path / "docker-called"
    p.write(
        tools / "docker", f"#!/bin/sh\nprintf called > '{marker}'\nexit 91\n".encode()
    )
    (tools / "docker").chmod(0o700)
    result = h.invoke(
        root,
        "run-host-acceptance",
        [
            "--wheel",
            str(tmp_path / "not-installed.whl"),
            "--source-identity",
            str(tmp_path / "not-read.json"),
            "--image",
            image,
            "--evidence",
            str(evidence),
        ],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment({"PATH": str(tools)}),
    )
    assert result["status"] != 0
    assert b"image" in result["stderr"].lower()
    assert not marker.exists()
    assert not (root / ".venv").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "tests/host_acceptance_contract_support.py",
        "tests/host_acceptance_assertions.py",
        "tests/host_runner_probe.py",
        "tests/host_install_proxy.py",
        "tests/host_expectation_support.py",
        "tests/host_orchestration_support.py",
        "tests/host_orchestration_tool.py",
    ],
)
def test_helper_contract_modules_have_no_optimizable_verification_assertions(relative):
    tree = ast.parse(p.read(h.ROOT / relative))
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))


@pytest.fixture(
    scope="module",
    params=[(None, 0), (None, 7), ("host_review", 0), ("host_review", 7)],
)
def simulated_host_run(request, tmp_path_factory):
    """Actual MCP/manifest observations, explicitly with a scripted Docker CLI."""
    import anyio

    from tests import mcp_contract_support as service

    tmp = tmp_path_factory.mktemp("host-assertion-simulation")
    phase, status = request.param
    world = service.world(h.ROOT, tmp)
    repo = world["repo"]
    tree = repo.git("write-tree").stdout.decode().strip()
    staged_bytes = repo.git("show", tree + ":payload").stdout
    h.replace(repo.path / "payload", b"later unstaged bytes must not select the tree\n")
    before = repo.identity()
    directory = service.install_docker(world, status=status)
    observed = {}

    async def scenario():
        async with service.client(world) as (session, _):
            result = await session.call_tool(
                "run_privileged_command", {} if phase is None else {"phase": phase}
            )
            observed["value"] = service.response_check(
                result, status=status, succeeded=status == 0
            )

    anyio.run(scenario)
    repo.assert_unchanged(before)
    value = observed["value"]
    manifest = p.load(value["resultPath"])
    calls = service.records(directory)
    assert [item["operation"] for item in calls] == [
        "launch",
        "inspect",
        "mkdir",
        "copy",
        "command",
        "remove",
        "confirm",
    ]
    streams = {
        "stdout": p.read(value["stdoutPath"]),
        "stderr": p.read(value["stderrPath"]),
        "startup_stdout": p.read(value["startupStdoutPath"]),
        "startup_stderr": p.read(value["startupStderrPath"]),
    }
    return {
        "value": value,
        "manifest": manifest,
        "streams": streams,
        "tree_id": tree,
        "status": status,
        "phase": phase,
        "command_argv": ["image-tool", "two words", "; $literal"],
        "user": "root",
        "cwd": "/workspace/source",
        "configured_image": "d9e853e87e55",
        "expected_stdout": b"command stdout\x00\xff\n",
        "expected_stderr": b"command stderr\x00\xfe\n",
        "snapshot_present": os.path.lexists(manifest["snapshot"]["path"]),
        "staged_bytes": staged_bytes,
        "expected_staged_bytes": staged_bytes.decode("utf-8"),
    }


def test_host_assertions_accept_actual_mcp_stub_manifest_observations(
    simulated_host_run,
):
    from tests.host_acceptance_assertions import verify_run

    verify_run(**simulated_host_run)


@pytest.mark.parametrize(
    "fault",
    [
        "tree",
        "argv",
        "status",
        "cleanup",
        "wrong-removal-id",
        "wrong-confirm-filter",
        "duplicate-cleanup",
        "incomplete-stream",
        "visible-container",
        "snapshot-present",
        "stdout",
        "stderr",
        "phase",
        "cwd",
        "outcome",
        "configured-image",
        "bool-status",
        "path",
    ],
)
def test_host_assertions_reject_controlled_observation_mismatches(
    simulated_host_run, fault
):
    import copy

    from tests.host_acceptance_assertions import verify_run

    value = copy.deepcopy(simulated_host_run)
    manifest = value["manifest"]
    operations = manifest["infrastructure_operations"]
    if fault == "tree":
        value["tree_id"] = "0" * len(value["tree_id"])
    elif fault == "argv":
        value["command_argv"] = ["wrong-command"]
    elif fault == "status":
        value["status"] = 99
    elif fault == "cleanup":
        manifest["cleanup"]["confirmed_absent"] = False
    elif fault == "wrong-removal-id":
        operations[-2]["argv"][-1] = "b" * 64
    elif fault == "wrong-confirm-filter":
        operations[-1]["argv"][6] = "id=" + "b" * 64
    elif fault == "duplicate-cleanup":
        operations.append(copy.deepcopy(operations[-2]))
    elif fault == "incomplete-stream":
        operations[-1]["stdout"]["completeness_name"] = "READ_FAILED"
    elif fault == "visible-container":
        operations[-1]["stdout"]["observed_bytes"] = 65
    elif fault == "snapshot-present":
        value["snapshot_present"] = True
    elif fault in {"stdout", "stderr"}:
        value["streams"][fault] += b"wrong bytes"
    elif fault == "phase":
        value["phase"] = "wrong-phase"
    elif fault == "cwd":
        value["cwd"] = "/wrong-cwd"
    elif fault == "outcome":
        manifest["outcome"] = "timed_out"
    elif fault == "configured-image":
        manifest["configured_image_id"] = "b" * 12
    elif fault == "bool-status":
        manifest["return_code"] = False
    elif fault == "path":
        value["value"]["stdoutPath"] = "/wrong-output/stdout.txt"
    else:
        raise AssertionError("unknown observation control")
    with pytest.raises(AssertionError):
        verify_run(**value)


@pytest.mark.parametrize(
    "fault", ["status", "cleanup", "wrong-removal-id", "incomplete-stream"]
)
def test_host_assertions_reject_invalid_evidence_under_optimization(
    simulated_host_run, tmp_path, fault
):
    import base64
    import copy
    import sys

    valid = copy.deepcopy(simulated_host_run)
    invalid = copy.deepcopy(valid)
    if fault == "status":
        invalid["status"] = 99
    elif fault == "cleanup":
        invalid["manifest"]["cleanup"]["confirmed_absent"] = False
    elif fault == "wrong-removal-id":
        invalid["manifest"]["infrastructure_operations"][-2]["argv"][-1] = "b" * 64
    else:
        invalid["manifest"]["infrastructure_operations"][-1]["stdout"][
            "completeness_name"
        ] = "READ_FAILED"
    for name, value in (("valid", valid), ("invalid", invalid)):
        value["streams"] = {
            key: base64.b64encode(data).decode("ascii")
            for key, data in value["streams"].items()
        }
        for key in ("expected_stdout", "expected_stderr", "staged_bytes"):
            value[key] = base64.b64encode(value[key]).decode("ascii")
        p.write(tmp_path / (name + ".json"), p.canonical(value))
    probe = """
import base64
import sys
from pathlib import Path
from tests import host_acceptance_assertions as assertions
from tests import mcp_provenance as p
if sys.flags.optimize != 1:
    raise RuntimeError('optimized subprocess prerequisite failed')
if Path(assertions.__file__).resolve() != Path(sys.argv[3]).resolve():
    raise RuntimeError('wrong verification module origin')
def load(path):
    value = p.load(Path(path))
    value['streams'] = {key: base64.b64decode(data) for key, data in value['streams'].items()}
    for key in ('expected_stdout', 'expected_stderr', 'staged_bytes'):
        value[key] = base64.b64decode(value[key])
    return value
assertions.verify_run(**load(sys.argv[1]))
try:
    assertions.verify_run(**load(sys.argv[2]))
except AssertionError:
    print('optimized verification control rejected')
else:
    raise RuntimeError('invalid host evidence silently accepted under -O')
"""
    result = wheel.command(
        [
            sys.executable,
            "-O",
            "-c",
            probe,
            str(tmp_path / "valid.json"),
            str(tmp_path / "invalid.json"),
            str(h.ROOT / "tests/host_acceptance_assertions.py"),
        ],
        cwd=h.ROOT,
        env=wheel.clean_environment(),
        timeout_seconds=30,
    )
    p.write(
        tmp_path / "optimized-operation.json", p.canonical(h.operation_record(result))
    )
    assert result["status"] == 0, result
    assert result["stdout"] == b"optimized verification control rejected\n"


@pytest.mark.parametrize(
    "fault",
    [
        "normal",
        "operation",
        "operation-close",
        "interrupt-close",
        "close",
        "close-interrupt",
    ],
)
def test_profile_directory_has_one_owner_and_preserves_failure_authority(
    tmp_path, monkeypatch, fault
):
    from tests.test_python_check_lifecycle import _carries_secondary, _close_hook

    p.write(tmp_path / "entry", b"profile bytes")
    owners = []
    original_open = h.DirectoryDescriptor.open_secure_path
    original_list = h.DirectoryDescriptor.list_names
    primary = (
        KeyboardInterrupt("profile interrupted")
        if fault == "interrupt-close"
        else OSError("profile enumeration failed")
    )
    release = (
        KeyboardInterrupt("profile release interrupted")
        if fault == "close-interrupt"
        else OSError("profile release failed")
    )

    def opened(*args, **kwargs):
        owner = original_open(*args, **kwargs)
        owners.append(owner)
        return owner

    def names(owner):
        if fault in {"operation", "operation-close", "interrupt-close"}:
            raise primary
        return original_list(owner)

    monkeypatch.setattr(h.DirectoryDescriptor, "open_secure_path", staticmethod(opened))
    monkeypatch.setattr(h.DirectoryDescriptor, "list_names", names)
    close, attempts = _close_hook(str(tmp_path), release)
    if "close" in fault:
        monkeypatch.setattr(os, "close", close)
    expected = (
        primary
        if fault in {"operation", "operation-close", "interrupt-close"}
        else release
    )
    if fault == "normal":
        assert h.root_names(tmp_path) == ["entry"]
    else:
        with pytest.raises(type(expected)) as caught:
            h.root_names(tmp_path)
        assert caught.value is expected
        if fault in {"operation-close", "interrupt-close"}:
            assert _carries_secondary(primary, release)
    assert len(owners) == 1
    assert owners[0].released
    owners[0].close()
    owners[0].close()
    assert len(attempts) == int("close" in fault)


def test_synthetic_revision_setup_is_deterministic_without_staging(tmp_path):
    roots = []
    for name in ("first", "relocated"):
        scratch = tmp_path / name
        scratch.mkdir()
        root = scratch / "repository"
        root.mkdir()
        roots.append((root, h.synthetic_head(root, scratch)))
        assert not (root / ".git/index").exists()
    assert roots[0][1] == roots[1][1]
    assert roots[0][0] != roots[1][0]


@pytest.fixture
def inert_profile_candidate(tmp_path):
    from tests import mcp_contract_support as service

    # Inert files validate preparation only; they are never executed and are
    # not substitutes for missing production interfaces or RED observations.
    candidate = service.prepare_inputs(h.ROOT, tmp_path / "candidate")
    service.copy_tree(h.ROOT / "scripts", candidate / "scripts")
    (candidate / "tests").mkdir()
    for relative in p.SUPPORT_FILES:
        p.write(candidate / relative, p.read(h.ROOT / relative))
    p.write(candidate / "uv.lock", p.read(h.ROOT / "uv.lock"))
    for name in h.SURFACES:
        path = candidate / "scripts" / name
        if path.exists():
            h.replace(path, b"#!/bin/sh\nexit 91\n")
        else:
            p.write(path, b"#!/bin/sh\nexit 91\n")
        path.chmod(0o700)
        implementation = candidate / "scripts" / (name.replace("-", "_") + ".py")
        if implementation.exists():
            h.replace(
                implementation, b"# inert preparation-only implementation marker\n"
            )
        else:
            p.write(implementation, b"# inert preparation-only implementation marker\n")
    return candidate


def test_prepared_profile_driver_has_valid_common_inputs_and_revision(
    tmp_path, monkeypatch, inert_profile_candidate
):
    candidate = inert_profile_candidate
    monkeypatch.setattr(h, "ROOT", candidate)
    scratch = tmp_path / "profile"
    scratch.mkdir()
    before = attribution.dist_identity(candidate)
    root, revision = h.prepared_root(scratch)
    assert h.root_names(root) == sorted((*h.IDENTITY_ROOTS, "README.md", ".git"))
    assert wheel.inventory(root)[0] == wheel.inventory(candidate)[0]
    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    assert not (root / ".git/index").exists()
    assert attribution.dist_identity(candidate) == before


@pytest.mark.parametrize("interrupted", [False, True])
def test_wrapper_driver_preserves_collection_failure_and_never_publishes_success(
    tmp_path, monkeypatch, interrupted
):
    from unittest.mock import Mock

    primary = (
        KeyboardInterrupt("wrapper observation interrupted")
        if interrupted
        else OSError("wrapper collection failed")
    )
    wrapper = tmp_path / "unused-wrapper"
    surface = Mock(return_value=wrapper)
    command = Mock(side_effect=primary)
    publish = Mock()
    monkeypatch.setattr(h, "required_surface", surface)
    monkeypatch.setattr(wheel, "command", command)
    monkeypatch.setattr(p, "write", publish)
    with pytest.raises(type(primary)) as caught:
        h.invoke(
            tmp_path, "build-acceptance-wheel", [], cwd=tmp_path, evidence=tmp_path
        )
    assert caught.value is primary
    surface.assert_called_once_with("build-acceptance-wheel", tmp_path)
    command.assert_called_once()
    publish.assert_not_called()


@pytest.fixture(autouse=True)
def host_contract_build_admission(tmp_path_factory, request, monkeypatch):
    base = tmp_path_factory.getbasetemp()
    shared = base.parent if hasattr(request.config, "workerinput") else base
    monkeypatch.setattr(wheel, "BUILD_SLOT_PATH", shared / "host-contract-build.lock")


PROBE_FAULTS = (
    "none",
    "stale-module",
    "stale-entry",
    "self-consistent-stale",
    "missing-module",
    "extra-module",
    "symlink-module",
    "wrong-root",
    "child-after-attestation",
    "attestation-missing",
    "attestation-duplicate",
    "attestation-malformed",
    "attestation-partial",
    "attestation-overlong",
    "attestation-incomplete",
    "evidence-unwritable",
    "evidence-operation-close",
    "evidence-close",
    "evidence-interruption",
    "evidence-missing",
    "evidence-truncated",
    "evidence-inconsistent",
    "origin-missing",
    "origin-schema",
    "origin-wheel-binding",
    "shadow-script",
    "shadow-cwd",
    "retag-image",
)


@pytest.mark.parametrize("fault", PROBE_FAULTS)
def test_isolated_runner_probe_itself_has_executable_wheel_controls(
    request, tmp_path, fault
):
    # Independent fixture validation, not production runner acceptance. A broken
    # copied probe must never be counted as RED for the missing public runner.
    base = request.getfixturevalue("_mcp_worker_wheel")
    before = wheel.inventory(base.repository_root)[0]
    result, records, bound = h.standalone_probe(base, tmp_path, fault)
    assert (result["status"] == 0) is (
        fault in {"none", "shadow-script", "shadow-cwd", "retag-image"}
    ), result
    installation = [
        p.load(records / name)
        for name in h.root_names(records)
        if name.startswith("installation-")
    ]
    assert installation
    assert all(item["pythonpath"] is None for item in installation)
    assert all(
        item["foundation"].startswith(str(bound.environment) + "/")
        for item in installation
    )
    observation = p.load(records / "service-observation.json")
    assert observation["child_cleanup_complete"] is True
    assert "--lifecycle" not in observation["argv"]
    assert "PYTHONPATH" not in observation["environment_keys"]
    assert "FOOTMAN_HOST_CONTEXT" not in observation["environment_keys"]
    assert b"SHADOW_SOURCE_EXECUTED" not in result["stderr"]
    assert b"STALE_ENTRY_EXECUTED" not in result["stderr"]
    if fault == "none":
        assert observation["parser_confirmed"] is True
        assert observation["child_return_code"] == 0
        retained = p.load(tmp_path / "verified-set.json")
        assert retained["wheel_sha256"] == base.wheel_sha256
    elif fault == "child-after-attestation":
        assert observation["parser_confirmed"] is True
        assert observation["child_return_code"] != 0
        assert (tmp_path / "verified-set.json").is_file()
    elif fault == "attestation-incomplete":
        assert observation["parser_terminal"] == "READ_FAILED"
        assert observation["actual_stream_terminals"]["STDERR"] == "EOF"
    if fault in {"evidence-operation-close", "evidence-close", "evidence-interruption"}:
        attempts = [
            p.load(records / name)
            for name in h.root_names(records)
            if name.startswith("evidence-attempt-")
        ]
        assert len(attempts) == 1
        assert attempts[0]["closes"] == 1
        assert attempts[0]["writes"] >= 1
    assert wheel.inventory(base.repository_root)[0] == before
    assert wheel.inventory(h.ROOT)[0] == before


@pytest.mark.parametrize("case", ["pass", "skip", "xfail", "xpass", "fail", "empty"])
def test_public_runner_isolates_host_suite_and_preserves_policy_status(tmp_path, case):
    h.required_surface("run-host-acceptance")
    before = attribution.dist_identity(h.ROOT)
    root, env, docker_records = h.policy_root(tmp_path, case)
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert (result["status"] == 0) is (case == "pass"), result
    report = p.load(tmp_path / "host-evidence/report.json")
    pytest_commands = [item for item in report["commands"] if "pytest" in item["argv"]]
    assert len(pytest_commands) == 1
    command = pytest_commands[0]
    args = command["argv"]
    assert args.count("-n") == 1
    assert args[args.index("--dist") + 1] == "worksteal"
    assert "-c" in args
    installations = [
        p.load(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]) / name)
        for name in h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))
        if name.startswith("installation-")
    ]
    assert installations
    for observed in installations:
        ini = observed["pytest_configuration"]
        assert "--strict-config" in ini and "--strict-markers" in ini
        assert "testpaths" not in ini
        assert observed["pytest_config_path"] == args[args.index("-c") + 1]
        assert not Path(observed["foundation"]).is_relative_to(root)
        assert not Path(observed["foundation"]).is_relative_to(h.ROOT)
        assert observed["pythonpath"] is None
    assert any(
        argument.endswith("/host_tests") or argument.endswith("/host_tests/")
        for argument in args
    )
    assert "PYTHONPATH" not in command["environment_keys"]
    assert report["cleanup_complete"] is True
    assert report["source_identity"] == binding["source_identity"]
    assert report["build_input_identity"] == binding["build_input_identity"]
    assert report["wheel_sha256"] == binding["wheel_sha256"]
    assert command["status"] == (0 if case == "pass" else 5 if case == "empty" else 1)
    for name in h.root_names(docker_records):
        assert p.load(docker_records / name)["argv"][0] in {
            "--version",
            "version",
            "info",
        }
    assert attribution.dist_identity(h.ROOT) == before


@pytest.mark.parametrize(
    "fault",
    [
        fault
        for fault in PROBE_FAULTS[1:]
        if not fault.startswith("shadow-") and fault != "retag-image"
    ],
)
def test_public_runner_rejects_install_attestation_and_evidence_faults(tmp_path, fault):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    env["FOOTMAN_HOST_CONTRACT_FAULT"] = fault
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0, (fault, result)
    report = p.load(tmp_path / "host-evidence/report.json")
    assert report["success"] is False
    assert report["cleanup_complete"] is True
    records = Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"])
    observation = p.load(records / "service-observation.json")
    assert observation["fault_input"] == fault
    assert observation["child_cleanup_complete"] is True
    assert observation["primary_type"] is not None or fault.startswith(
        ("evidence-", "attestation-", "origin-")
    )
    if fault == "child-after-attestation":
        assert observation["parser_confirmed"] is True
        assert observation["child_return_code"] != 0
    elif fault.startswith("attestation-"):
        assert observation["parser_confirmed"] is False


def test_reference_source_inventory_is_relocation_safe_and_has_distinct_build_scope(
    tmp_path,
):
    from tests import mcp_contract_support as service

    first = service.prepare_inputs(h.ROOT, tmp_path / "first")
    for name in ("tests", "host_tests", "scripts"):
        (first / name).mkdir()
        p.write(first / name / "__init__.py", b"# scoped marker\n")
    p.write(first / "uv.lock", b"fixture lock\n")
    other = tmp_path / "other location"
    service.copy_tree(first, other)
    assert h.inventory(first) == h.inventory(other)
    assert h.inventory(first, wheel.ROOTS) == wheel.inventory(first)
    source_before = h.inventory(first)[0]
    build_before = wheel.inventory(first)[0]
    h.replace(
        first / "README.md", p.read(first / "README.md") + b"\nREADME-only mutation\n"
    )
    assert h.inventory(first)[0] == source_before
    assert wheel.inventory(first)[0] != build_before


def test_scripted_tool_attempt_records_detect_a_second_execution(tmp_path):
    from tests import mcp_contract_support as service

    root = service.prepare_inputs(h.ROOT, tmp_path / "inputs")
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    first = wheel.command(
        [executable, "build"],
        cwd=root,
        env=wheel.clean_environment(),
        timeout_seconds=20,
    )
    second = wheel.command(
        [executable, "build"],
        cwd=root,
        env=wheel.clean_environment(),
        timeout_seconds=20,
    )
    assert first["status"] == 0
    assert second["status"] != 0  # Exclusive old call record refuses rewriting.
    assert len(h.root_names(attempts)) == 2
    for name in h.root_names(attempts):
        assert p.load(attempts / name)["argv"] == ["build"]


@pytest.mark.parametrize(
    "scope,changed",
    [
        ("src", True),
        ("tests", True),
        ("host_tests", True),
        ("scripts", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("executable", True),
        ("symlink-target", True),
        ("empty-directory", True),
        ("untracked", True),
        ("README.md", False),
        ("docs", False),
        ("openspec", False),
        ("dist", False),
        ("cache", False),
        ("staging-only", False),
        ("permission-nonexec", False),
    ],
)
def test_build_manifest_source_identity_has_exact_scope(tmp_path, scope, changed):
    h.required_surface("build-acceptance-wheel")
    before_candidate = attribution.dist_identity(h.ROOT)
    root, _ = h.prepared_root(tmp_path)
    (root / "tests/fixture-link").symlink_to("first-target")
    _, before = h.scripted_binding(
        root, tmp_path / "first-evidence", tmp_path / "first-tool"
    )
    # Recorded fixture-owned output remedy, outside the helper, never a scan or
    # implicit deletion by a new build invocation. Preserve bytes/digests first.
    prior_outputs = {
        name: p.read(root / "dist" / name)
        for name in ("footman-0.1.0.tar.gz", "footman-0.1.0-py3-none-any.whl")
    }
    p.write(
        tmp_path / "operator-remedy.json",
        p.canonical(
            {
                name: __import__("hashlib").sha256(data).hexdigest()
                for name, data in prior_outputs.items()
            }
        ),
    )
    for name in prior_outputs:
        (root / "dist" / name).unlink()
    if scope in {"src", "tests", "host_tests", "scripts"}:
        selected = (
            root
            / scope
            / (
                "footman/__init__.py"
                if scope == "src"
                else "__init__.py"
                if scope in {"tests", "host_tests"}
                else "run_host_acceptance.py"
            )
        )
        h.replace(selected, p.read(selected) + b"\n# scoped mutation\n")
    elif scope in {"pyproject.toml", "uv.lock", "README.md"}:
        selected = root / scope
        h.replace(selected, p.read(selected) + b"\n# controlled mutation\n")
    elif scope == "executable":
        (root / "tests/__init__.py").chmod(0o700)
    elif scope == "symlink-target":
        (root / "tests/fixture-link").unlink()
        (root / "tests/fixture-link").symlink_to("second-target")
    elif scope == "empty-directory":
        (root / "host_tests/empty").mkdir()
    elif scope == "untracked":
        p.write(root / "tests/not-in-head.dat", b"scoped untracked bytes")
    elif scope == "cache":
        (root / "tests/.pytest_cache").mkdir()
        p.write(root / "tests/.pytest_cache/cached.py", b"not source identity\n")
    elif scope == "staging-only":
        blob = h.git(root, "hash-object", "-w", "tests/__init__.py").decode().strip()
        h.git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            "100644",
            blob,
            "tests/__init__.py",
        )
    elif scope == "permission-nonexec":
        (root / "tests/__init__.py").chmod(0o640)
    else:
        directory = root / scope
        directory.mkdir(exist_ok=True)
        p.write(directory / "unrelated.dat", b"excluded output/planning/documentation")
    _, after = h.scripted_binding(
        root, tmp_path / "second-evidence", tmp_path / "second-tool"
    )
    assert (before["source_identity"] != after["source_identity"]) is changed
    assert (before["build_input_identity"] != after["build_input_identity"]) is (
        scope in {"src", "pyproject.toml", "README.md"}
    )
    assert before["revision"] == after["revision"]
    assert attribution.dist_identity(h.ROOT) == before_candidate


@pytest.mark.parametrize("name", h.SURFACES)
def test_wrapper_root_disagreement_precedes_all_managed_acquisition(tmp_path, name):
    h.required_surface(name)
    root, _ = h.prepared_root(tmp_path)
    h.install_acquisition_guard(root)
    foreign = h.relocate(root, tmp_path / "foreign-root")
    implementation = name.replace("-", "_") + ".py"
    (root / "scripts" / implementation).unlink()
    (root / "scripts" / implementation).symlink_to(foreign / "scripts" / implementation)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    args = (
        ["--manifest", str(evidence / "manifest.json")]
        if name == "build-acceptance-wheel"
        else [
            "--wheel",
            str(tmp_path / "absent.whl"),
            "--source-identity",
            str(tmp_path / "absent.json"),
            "--image",
            "d9e853e87e55",
            "--evidence",
            str(evidence),
        ]
    )
    result = h.invoke(root, name, args, cwd=foreign, evidence=evidence)
    assert result["status"] != 0
    text = result["stderr"].decode("utf-8", "replace")
    assert "root" in text.lower()
    counters = [
        json.loads(line.removeprefix("contract-acquisitions "))
        for line in text.splitlines()
        if line.startswith("contract-acquisitions ")
    ]
    assert counters, (
        "missing instrumentation is a broken fixture, not zero acquisitions"
    )
    assert all(
        item == {"descriptor_adoptions": 0, "launch_calls": 0} for item in counters
    )


def test_managed_acquisition_guard_has_a_nonvacuous_positive_control(tmp_path):
    import sys

    from tests import mcp_contract_support as service

    root = service.prepare_inputs(h.ROOT, tmp_path / "guard-root")
    h.install_acquisition_guard(root)
    config = tmp_path / "invalid.toml"
    p.write(config, b"version = 999\n")
    code = """from footman.configuration import load_configuration
from footman.process import OwnedProcess
import sys
try:
    load_configuration(sys.argv[1])
except Exception:
    pass
try:
    OwnedProcess.launch([])
except (ValueError, OSError):
    pass
"""
    env = {**wheel.clean_environment(), "PYTHONPATH": str(root / "src")}
    result = wheel.command(
        [sys.executable, "-c", code, config], cwd=tmp_path, env=env, timeout_seconds=20
    )
    assert result["status"] == 0, result
    counters = [
        json.loads(line.removeprefix("contract-acquisitions "))
        for line in result["stderr"].decode().splitlines()
        if line.startswith("contract-acquisitions ")
    ]
    assert len(counters) == 1
    assert counters[0]["descriptor_adoptions"] > 0
    assert counters[0]["launch_calls"] == 1


def test_build_selects_wrapper_root_not_foreign_caller_cwd_and_relocation_matches(
    tmp_path,
):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    caller = tmp_path / "unrelated caller"
    caller.mkdir()
    p.write(caller / "pyproject.toml", b"not the build input")
    (caller / "dist").mkdir()
    p.write(caller / "dist/sentinel", b"must survive")
    _, binding = h.bind(root, tmp_path / "first-build", cwd=caller)
    assert binding["repository_root"] == str(root)
    assert binding["wrapper_path"] == str(root / "scripts/build-acceptance-wheel")
    assert binding["caller_cwd"] == str(caller)
    moved = h.relocate(root, tmp_path / "relocated matching root")
    _, other = h.bind(moved, tmp_path / "second-build", cwd=root)
    for field in ("revision", "source_identity", "build_input_identity"):
        assert binding[field] == other[field]
    assert p.read(caller / "dist/sentinel") == b"must survive"
    assert not (caller / "dist/footman-0.1.0-py3-none-any.whl").exists()


@pytest.mark.parametrize("name", h.SURFACES)
def test_host_interfaces_do_not_accept_caller_selected_root_override(tmp_path, name):
    h.required_surface(name)
    root, _ = h.prepared_root(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    result = h.invoke(
        root,
        name,
        (
            ["--manifest", str(evidence / "manifest.json")]
            if name == "build-acceptance-wheel"
            else [
                "--wheel",
                str(tmp_path / "absent.whl"),
                "--source-identity",
                str(tmp_path / "absent.json"),
                "--image",
                "d9e853e87e55",
                "--evidence",
                str(evidence),
            ]
        )
        + ["--root", str(tmp_path)],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    assert result["status"] != 0
    assert b"--root" in result["stderr"]
    assert not h.root_names(attempts)


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("build_input_identity", None),
        ("build_input_identity", {}),
        (
            "build_input_identity",
            {
                "schema_version": True,
                "roots": ["src/", "pyproject.toml", "README.md"],
                "sha256": "0" * 64,
            },
        ),
        (
            "build_input_identity",
            {
                "schema_version": 1,
                "roots": ["src/", "pyproject.toml"],
                "sha256": "0" * 64,
            },
        ),
        ("source_identity", {}),
        ("source_identity", None),
        ("wheel_sha256", "0" * 64),
        ("revision", "0" * 40),
    ],
)
def test_runner_rejects_missing_malformed_stale_or_swapped_binding_before_install(
    tmp_path, field, invalid
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    malformed = dict(binding)
    if invalid is None:
        malformed.pop(field)
    else:
        malformed[field] = invalid
    h.replace(manifest, p.canonical(malformed))
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    env["PATH"] = str(executable.parent) + os.pathsep + env["PATH"]
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    assert field.encode() in result["stderr"], result
    assert not h.root_names(attempts), "invalid provenance reached provisioning"
    assert not h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))


@pytest.mark.parametrize(
    "fault",
    [
        "readme",
        "source",
        "support",
        "missing-readme",
        "symlink-readme",
        "swapped-wheel",
    ],
)
def test_runner_independently_compares_both_identities_and_exact_wheel_before_install(
    tmp_path, fault
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    if fault in {"readme", "source", "support"}:
        path = (
            root
            / {
                "readme": "README.md",
                "source": "src/footman/__init__.py",
                "support": "tests/mcp_transport.py",
            }[fault]
        )
        h.replace(path, p.read(path) + b"\n# stale after binding\n")
    elif fault == "missing-readme":
        (root / "README.md").unlink()
    elif fault == "symlink-readme":
        (root / "README.md").rename(tmp_path / "readme-original")
        (root / "README.md").symlink_to(tmp_path / "readme-original")
    else:
        h.replace(Path(binding["wheel_path"]), b"unbound replacement archive")
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    env["PATH"] = str(executable.parent) + os.pathsep + env["PATH"]
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    assert not h.root_names(attempts)
    assert not h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))
    if fault == "readme":
        assert h.inventory(root)[0] == binding["source_identity"]
        assert wheel.inventory(root)[0] != binding["build_input_identity"]


@pytest.mark.parametrize(
    "fault", ["source-readme-during", "source-support-during", "copied-support-during"]
)
def test_runner_completion_rechecks_readme_source_and_copied_support_independently(
    tmp_path, fault
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    env.update(
        FOOTMAN_HOST_CONTRACT_FAULT=fault, FOOTMAN_HOST_CONTRACT_SOURCE_ROOT=str(root)
    )
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    report = p.load(tmp_path / "host-evidence/report.json")
    assert report["success"] is False
    assert report["cleanup_complete"] is True
    assert [
        item["status"] for item in report["commands"] if "pytest" in item["argv"]
    ] == [0]
    assert (h.inventory(root)[0] != binding["source_identity"]) is (
        fault == "source-support-during"
    )
    assert (wheel.inventory(root)[0] != binding["build_input_identity"]) is (
        fault == "source-readme-during"
    )
    observation = p.load(
        Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]) / "service-observation.json"
    )
    assert (
        observation["child_return_code"] == 0
        and observation["parser_confirmed"] is True
    )


@pytest.mark.parametrize("image", ["d9e853e87e55", "a" * 64, "sha256:" + "a" * 64])
def test_runner_preserves_literal_configured_image_and_diagnostic_observed_identity(
    tmp_path, image
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root,
        manifest,
        binding["wheel_path"],
        tmp_path / "host-evidence",
        env=env,
        image=image,
    )
    assert result["status"] == 0, result
    records = Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"])
    value = p.load(records / "mcp-result.json")
    retained = p.load(Path(value["resultPath"]))
    assert retained["configured_image_id"] == image
    assert retained["observed_image_id"] == "sha256:" + "b" * 64
    calls = [
        p.load(records / "service-stub-records" / name)
        for name in h.root_names(records / "service-stub-records")
        if name.endswith("-call.json")
    ]
    launch = [item["argv"] for item in calls if item["argv"][0] == "run"]
    assert launch == [
        ["run", "--detach=true", "--pull=never", "--", image, "sleep", "infinity"]
    ]


def test_runner_retains_configured_image_launch_error_without_copy_or_exec(tmp_path):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    env["FOOTMAN_HOST_CONTRACT_FAULT"] = "image-launch-failure"
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    records = Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"])
    value = p.load(records / "mcp-result.json")
    retained = p.load(Path(value["resultPath"]))
    assert retained["configured_image_id"] == "d9e853e87e55"
    assert retained["startup"]["return_code"] == 23
    assert (
        p.read(Path(value["startupStderrPath"]))
        == b"controlled Docker resolution error\n"
    )
    assert retained["docker_command_argv"] == []
    assert retained["return_code"] is None


@pytest.mark.parametrize("status", [0, 17])
@pytest.mark.parametrize("boundary", ["none", "metadata", "stdout", "stderr"])
@pytest.mark.parametrize("kind", ["ordinary", "interruption", "unexpected"])
def test_install_tool_proxy_preserves_first_status_and_attempts_independent_delivery(
    tmp_path, monkeypatch, status, boundary, kind
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests import host_install_proxy as proxy

    error = (
        OSError("proxy observer failed")
        if kind == "ordinary"
        else KeyboardInterrupt("proxy observer interrupted")
        if kind == "interruption"
        else TypeError("unexpected proxy observer defect")
    )
    operation = {
        "status": status,
        "stdout": b"native stdout\x00\xff",
        "stderr": b"native stderr\x80\n",
    }
    command = Mock(return_value=operation)
    write = Mock(side_effect=error if boundary == "metadata" else None)
    output = Mock(side_effect=error if boundary == "stdout" else lambda data: len(data))
    diagnostic = Mock(
        side_effect=error if boundary == "stderr" else lambda data: len(data)
    )
    monkeypatch.setattr(
        proxy,
        "sys",
        SimpleNamespace(
            argv=["uv", "--version"],
            stdout=SimpleNamespace(buffer=SimpleNamespace(write=output)),
            stderr=SimpleNamespace(buffer=SimpleNamespace(write=diagnostic)),
        ),
    )
    monkeypatch.setattr(wheel, "command", command)
    monkeypatch.setattr(p, "write", write)
    monkeypatch.setenv("FOOTMAN_HOST_TOOL_RECORDS", str(tmp_path))
    monkeypatch.setenv("FOOTMAN_HOST_TOOL_REAL_UV", "/fixture/real-uv")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    expected_exit = boundary == "none" or (status != 0 and kind == "ordinary")
    with pytest.raises(SystemExit if expected_exit else type(error)) as caught:
        proxy.main()
    if expected_exit:
        assert caught.value.code == status
        if status and boundary != "none":
            assert error in caught.value.__cause__._transaction_secondary
    else:
        assert caught.value is error
    command.assert_called_once()
    write.assert_called_once()
    output.assert_called_once_with(operation["stdout"])
    diagnostic.assert_called_once_with(operation["stderr"])


@pytest.mark.parametrize(
    "fault", ["stale-module", "stale-entry", "self-consistent-stale", "swapped-copy"]
)
def test_install_proxy_mutates_an_actual_private_install_before_expectations(
    request, tmp_path, fault
):
    import hashlib

    base = request.getfixturevalue("_mcp_worker_wheel")
    destination = tmp_path / "clone"
    destination.mkdir()
    clone = wheel.reuse_for_test(base, destination, mutable=True)
    executable, extra, records = h.install_proxy(tmp_path, fault)
    result = wheel.command(
        [
            executable,
            "pip",
            "install",
            "--reinstall",
            "--no-deps",
            "--python",
            clone.environment / "bin/python",
            base.wheel,
        ],
        cwd=tmp_path,
        env=wheel.clean_environment(extra),
        timeout_seconds=60,
    )
    assert result["status"] == 0, result
    observed = [p.load(records / name) for name in h.root_names(records)]
    assert len(observed) == 1
    assert observed[0]["mutation_members"]
    assert observed[0]["status"] == 0
    assert "PYTHONPATH" not in observed[0]["environment_keys"]
    site = Path(observed[0]["site"])
    modules, _ = wheel.archive_expectations(base.wheel, base.wheel_sha256, site)
    target = next(
        item
        for item in modules
        if item["name"]
        == ("footman.__main__" if fault == "stale-entry" else "footman.container")
    )
    assert (
        hashlib.sha256(p.read(Path(target["expected_path"]))).hexdigest()
        != target["sha256"]
    )
    assert hashlib.sha256(p.read(base.wheel)).hexdigest() == base.wheel_sha256


@pytest.mark.parametrize(
    "fault", ["stale-module", "stale-entry", "self-consistent-stale", "swapped-copy"]
)
def test_runner_cannot_derive_self_matching_expectations_from_installed_bytes_or_record(
    tmp_path, fault
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    executable, extra, records = h.install_proxy(tmp_path, fault)
    env.update(extra)
    env["PATH"] = str(executable.parent) + os.pathsep + env["PATH"]
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    observed = [p.load(records / name) for name in h.root_names(records)]
    assert any(item["mutation_members"] for item in observed), (
        "stale-install fixture never reached the intended install"
    )
    assert all("PYTHONPATH" not in item["environment_keys"] for item in observed)
    report = p.load(tmp_path / "host-evidence/report.json")
    assert report["success"] is False
    assert report["wheel_sha256"] == binding["wheel_sha256"]
    assert report["cleanup_complete"] is True


def test_runner_bootstrap_poison_is_replaced_and_never_reaches_wheel_children(tmp_path):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    p.write(root / "src/sitecustomize.py", h.BOOTSTRAP_ENV_GUARD.encode())
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    executable, extra, tool_records = h.install_proxy(tmp_path, "none")
    env.update(extra)
    poison = tmp_path / "poison"
    (poison / "footman").mkdir(parents=True)
    p.write(
        poison / "footman/__init__.py",
        b"raise RuntimeError('BOOTSTRAP_POISON_IMPORTED')\n",
    )
    env.update(
        PYTHONPATH=str(poison), PATH=str(executable.parent) + os.pathsep + env["PATH"]
    )
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] == 0, result
    bootstrap = [
        json.loads(line.removeprefix("contract-bootstrap "))
        for line in result["stderr"].decode().splitlines()
        if line.startswith("contract-bootstrap ")
    ]
    assert bootstrap
    assert all(
        item
        == {
            "pythonpath": str(root / "src"),
            "foundation": str(root / "src/footman/__init__.py"),
        }
        for item in bootstrap
    )
    assert b"BOOTSTRAP_POISON_IMPORTED" not in result["stderr"]
    tools = [p.load(tool_records / name) for name in h.root_names(tool_records)]
    assert tools and all("PYTHONPATH" not in item["environment_keys"] for item in tools)
    report = p.load(tmp_path / "host-evidence/report.json")
    commands = report["commands"]
    installs = [
        item for item in commands if "pip" in item["argv"] and "install" in item["argv"]
    ]
    assert installs
    assert all(
        "-e" not in item["argv"]
        and "--editable" not in item["argv"]
        and "." not in item["argv"]
        for item in installs
    )
    assert any(
        any(arg.split("[", 1)[0] == binding["wheel_path"] for arg in item["argv"])
        for item in installs
    )
    assert any(
        item["argv"][0].endswith("/bin/footman") and item["argv"][-1] == "--help"
        for item in commands
    )
    assert any(item["argv"][-3:] == ["-m", "footman", "--help"] for item in commands)
    for item in commands:
        if (
            "install" in item["argv"]
            or "pytest" in item["argv"]
            or "--help" in item["argv"]
        ):
            assert "PYTHONPATH" not in item["environment_keys"]
    records = Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"])
    observations = [
        p.load(records / name)
        for name in h.root_names(records)
        if name.startswith("installation-")
    ]
    assert observations and all(item["pythonpath"] is None for item in observations)
    service = p.load(records / "service-observation.json")
    assert "PYTHONPATH" not in service["environment_keys"]
    origin = p.load(Path(report["expected_origin_path"]))
    root_site = Path(report["venv"]) / "lib/python3.14/site-packages"
    modules, target = wheel.archive_expectations(
        Path(binding["wheel_path"]), binding["wheel_sha256"], root_site
    )
    assert sorted(origin["modules"], key=lambda item: item["name"]) == sorted(
        modules, key=lambda item: item["name"]
    )
    assert origin["entrypoint"]["target"] == target
    assert origin["entrypoint"]["script_path"] == str(
        Path(report["venv"]) / "bin/footman"
    )
    assert origin["wheel_sha256"] == binding["wheel_sha256"]
    retained = p.load(Path(report["provenance_evidence_path"]))
    assert retained["sources"] == p.manifest_set(origin)[1]
    assert retained["source_count"] == len(retained["sources"])
    assert (
        retained["set_sha256"]
        == __import__("hashlib").sha256(p.canonical(retained["sources"])).hexdigest()
    )
    assert retained["attestation"]["set_sha256"] == retained["set_sha256"]


@pytest.mark.parametrize("missing", ["git", "docker"])
def test_runner_missing_host_prerequisite_fails_before_provisioning_without_skip(
    tmp_path, missing
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    tools = h.isolated_path(tmp_path, git=missing != "git")
    if missing != "docker":
        extra_tools = tmp_path / "extra-tools"
        extra_tools.mkdir()
        executable, _ = h.probe_docker(extra_tools)
        env["PATH"] = str(executable.parent) + os.pathsep + str(tools)
    else:
        env["PATH"] = str(tools)
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    assert missing.encode() in result["stderr"].lower()
    assert not h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))


@pytest.mark.parametrize("kind", ["symlink", "dangling-link", "file"])
def test_build_refuses_unsafe_dist_ancestry_without_acquiring_outputs(tmp_path, kind):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    target = tmp_path / "unowned-dist"
    target.mkdir()
    p.write(target / "sentinel", b"unrelated bytes")
    if kind == "file":
        p.write(root / "dist", b"unowned file")
    else:
        (root / "dist").symlink_to(
            target if kind == "symlink" else tmp_path / "absent-directory"
        )
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "manifest.json")],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    assert result["status"] != 0
    assert not h.root_names(attempts)
    assert p.read(target / "sentinel") == b"unrelated bytes"
    assert h.root_names(target) == ["sentinel"]


def test_host_typing_pins_and_bootstrap_exceptions_are_documented_consistently():
    for relative in ("tests/test_python_ruff_parity.py", "tests/test_green_gate.py"):
        tree = ast.parse(p.read(h.ROOT / relative))
        assert any(
            isinstance(node, ast.List)
            and all(isinstance(value, ast.Constant) for value in node.elts)
            and [value.value for value in node.elts] == ["src", "host_tests"]
            for node in ast.walk(tree)
        ), relative
    text = p.read(h.ROOT / "docs/python-checks.md").decode()
    assert "scripts/build-acceptance-wheel" in text
    assert "scripts/run-host-acceptance" in text
    assert "python3.14" in text and "127" in text
    assert re.search(r"\*\*Roots:\*\*[^\n]*host_tests", text)
    assert (
        "host_tests"
        in __import__("tests.python_check_fixtures", fromlist=["__doc__"]).__doc__
    )


def test_install_proxy_retains_actual_native_failure_status_and_binary_streams(
    tmp_path,
):
    executable, extra, records = h.install_proxy(tmp_path, "none")
    env = wheel.clean_environment(extra)
    direct = wheel.command(
        [extra["FOOTMAN_HOST_TOOL_REAL_UV"], "--deliberately-invalid-option"],
        cwd=tmp_path,
        env=env,
        timeout_seconds=20,
    )
    proxied = wheel.command(
        [executable, "--deliberately-invalid-option"],
        cwd=tmp_path,
        env=env,
        timeout_seconds=20,
    )
    assert direct["status"] != 0
    assert proxied["status"] == direct["status"]
    assert proxied["stdout"] == direct["stdout"]
    assert proxied["stderr"] == direct["stderr"]
    observed = [p.load(records / name) for name in h.root_names(records)]
    assert len(observed) == 1 and observed[0]["status"] == direct["status"]


@pytest.mark.parametrize("audit", ["none", "ordinary", "interruption", "unexpected"])
@pytest.mark.parametrize("release", ["none", "ordinary", "interruption", "unexpected"])
def test_acquisition_observer_adopts_before_audit_and_releases_failed_handoff_once(
    monkeypatch, audit, release
):
    import atexit
    from types import SimpleNamespace
    from unittest.mock import Mock

    from footman.filesystem.descriptors import OwnedDescriptor
    from footman.process import OwnedProcess

    primary = (
        OSError("adoption audit failed")
        if audit == "ordinary"
        else KeyboardInterrupt("adoption audit interrupted")
        if audit == "interruption"
        else TypeError("unexpected adoption audit failure")
    )
    closing = (
        OSError("adoption release failed")
        if release == "ordinary"
        else KeyboardInterrupt("adoption release interrupted")
        if release == "interruption"
        else TypeError("unexpected adoption release failure")
    )
    close = Mock(side_effect=None if release == "none" else closing)
    owner = OwnedDescriptor.__new__(OwnedDescriptor)
    scope = {}
    original_init = OwnedDescriptor.__init__
    original_launch = OwnedProcess.__dict__["launch"]

    class Counts(dict):
        def __getitem__(self, key):
            if audit != "none" and key == "descriptor_adoptions":
                raise primary
            return super().__getitem__(key)

    with monkeypatch.context() as patch:
        patch.setattr(atexit, "register", lambda function: function)
        patch.setattr(OwnedDescriptor, "__init__", original_init)
        patch.setattr(OwnedProcess, "launch", original_launch)
        exec(h.ACQUISITION_GUARD, scope)
        scope["counts"] = Counts(descriptor_adoptions=0, launch_calls=0)
        if audit == "none":
            owner.__init__(
                SimpleNamespace(close=close),
                12345,
                label="synthetic fd; no OS acquisition",
            )
            if release == "none":
                owner.close()
            else:
                with pytest.raises(type(closing)) as caught:
                    owner.close()
                assert caught.value is closing
        else:
            expected = (
                primary
                if audit == "interruption" or release in {"none", "ordinary"}
                else closing
            )
            with pytest.raises(type(expected)) as caught:
                owner.__init__(
                    SimpleNamespace(close=close),
                    12345,
                    label="synthetic fd; no OS acquisition",
                )
            assert caught.value is expected
        assert owner.released
        owner.close()
        owner.close()
    close.assert_called_once_with(12345)


@pytest.mark.parametrize(
    "fault",
    ["operation", "operation-close", "close", "interruption", "close-interruption"],
)
def test_readme_fault_instrumentation_is_executable_and_releases_once(tmp_path, fault):
    import sys

    from tests import mcp_contract_support as service

    root = service.prepare_inputs(h.ROOT, tmp_path / "instrumented-input")
    h.install_readme_guard(root, fault)
    code = "from footman.configuration import load_configuration; import sys; load_configuration(sys.argv[1])"
    result = wheel.command(
        [sys.executable, "-c", code, root / "README.md"],
        cwd=tmp_path,
        env={**wheel.clean_environment(), "PYTHONPATH": str(root / "src")},
        timeout_seconds=20,
    )
    assert result["status"] != 0
    counters = [
        json.loads(line.removeprefix("contract-readme "))
        for line in result["stderr"].decode().splitlines()
        if line.startswith("contract-readme ")
    ]
    assert len(counters) == 1 and counters[0]["close_calls"] == 1
    if fault in {"operation", "operation-close", "interruption"}:
        assert counters[0]["read_calls"] == 1
    if "interruption" in fault:
        assert result["status"] in {-2, 130}


@pytest.mark.parametrize(
    "fault",
    ["operation", "operation-close", "close", "interruption", "close-interruption"],
)
def test_public_build_readme_input_faults_keep_primary_and_never_retry_close(
    tmp_path, fault
):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    h.install_readme_guard(root, fault)
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(evidence / "manifest.json")],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    assert result["status"] != 0
    assert not h.root_names(attempts)
    counters = [
        json.loads(line.removeprefix("contract-readme "))
        for line in result["stderr"].decode().splitlines()
        if line.startswith("contract-readme ")
    ]
    assert len(counters) == 1 and counters[0]["close_calls"] == 1
    assert b"contract README" in result["stderr"]
    if "interruption" in fault:
        assert result["status"] in {-2, 130}


def test_scannable_fixture_supplies_every_shared_source_root(scannable):
    assert all((scannable / name).is_dir() for name in h.SOURCE_ROOTS)


def test_development_collection_boundary_is_nonvacuous_with_populated_host_fixture(
    tmp_path,
):
    for root in h.SOURCE_ROOTS:
        (tmp_path / root).mkdir()
    p.write(
        tmp_path / "pyproject.toml",
        b'[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "--strict-config --strict-markers"\n',
    )
    p.write(tmp_path / "tests/test_dev.py", b"def test_dev():\n    pass\n")
    p.write(
        tmp_path / "host_tests/test_host.py",
        b"raise RuntimeError('HOST_COLLECTION_CANARY')\ndef test_host():\n    pass\n",
    )
    argv = [
        h.ROOT / ".venv/bin/python",
        "-m",
        "pytest",
        "tests",
        "--collect-only",
        "-n",
        "8",
        "--dist",
        "worksteal",
        "--tb=short",
        "-q",
    ]
    development = wheel.command(
        argv, cwd=tmp_path, env=wheel.clean_environment(), timeout_seconds=60
    )
    assert development["status"] == 0
    assert b"tests/test_dev.py::test_dev" in development["stdout"]
    assert b"HOST_COLLECTION_CANARY" not in development["stdout"]
    host = wheel.command(
        [*argv[:3], "host_tests", *argv[4:]],
        cwd=tmp_path,
        env=wheel.clean_environment(),
        timeout_seconds=60,
    )
    assert host["status"] != 0
    assert b"HOST_COLLECTION_CANARY" in host["stdout"] + host["stderr"]
    p.write(
        tmp_path / "collection-controls.json",
        p.canonical(
            {
                "development": h.operation_record(development),
                "explicit_host_negative": h.operation_record(host),
            }
        ),
    )


@pytest.mark.parametrize(
    "scope",
    [
        "manifest-version",
        "build-bool-version",
        "build-extra-field",
        "build-root-order",
        "build-uppercase-digest",
        "source-bool-version",
        "source-extra-root",
    ],
)
def test_runner_binding_validation_changes_only_the_selected_field(tmp_path, scope):
    import copy

    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    changed = copy.deepcopy(binding)
    if scope == "manifest-version":
        changed["schema_version"] = True
    elif scope == "build-bool-version":
        changed["build_input_identity"]["schema_version"] = True
    elif scope == "build-extra-field":
        changed["build_input_identity"]["extra"] = 1
    elif scope == "build-root-order":
        changed["build_input_identity"]["roots"].reverse()
    elif scope == "build-uppercase-digest":
        changed["build_input_identity"]["sha256"] = changed["build_input_identity"][
            "sha256"
        ].upper()
    elif scope == "source-bool-version":
        changed["source_identity"]["schema_version"] = True
    else:
        changed["source_identity"]["roots"].append("README.md")
    h.replace(manifest, p.canonical(changed))
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    env["PATH"] = str(executable.parent) + os.pathsep + env["PATH"]
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    assert not h.root_names(attempts)
    assert not h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))


def test_runner_accepts_matching_relocation_and_selects_its_own_wrapper_root(tmp_path):
    h.required_surface("run-host-acceptance")
    before_candidate = attribution.dist_identity(h.ROOT)
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    moved = h.relocate(root, tmp_path / "matching relocated runner")
    result = h.runner(
        moved,
        manifest,
        binding["wheel_path"],
        tmp_path / "host-evidence",
        env=env,
        cwd=root,
    )
    assert result["status"] == 0, result
    report = p.load(tmp_path / "host-evidence/report.json")
    assert report["repository_root"] == str(moved)
    assert report["wrapper_path"] == str(moved / "scripts/run-host-acceptance")
    assert report["caller_cwd"] == str(root)
    assert report["source_identity"] == binding["source_identity"]
    assert report["build_input_identity"] == binding["build_input_identity"]
    assert attribution.dist_identity(h.ROOT) == before_candidate


@pytest.mark.parametrize("fault", ["shadow-script", "shadow-cwd"])
def test_runner_shadow_controls_do_not_execute_script_directory_or_cwd_sources(
    tmp_path, fault
):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    env["FOOTMAN_HOST_CONTRACT_FAULT"] = fault
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    records = Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"])
    observation = p.load(records / "service-observation.json")
    assert observation["parser_confirmed"] is True
    assert observation["child_return_code"] == 0
    assert observation["foundation"].startswith(
        str(p.load(tmp_path / "host-evidence/report.json")["venv"]) + "/"
    )
    assert b"SHADOW_SOURCE_EXECUTED" not in result["stderr"]
    assert p.load(tmp_path / "host-evidence/report.json")["success"] is (
        result["status"] == 0
    )


@pytest.mark.parametrize("audit_interrupted", [False, True])
@pytest.mark.parametrize("failure", ["none", "child", "parent", "both"])
def test_acquisition_guard_failed_handoff_also_releases_independent_parent(
    monkeypatch, audit_interrupted, failure
):
    import atexit
    from types import SimpleNamespace
    from unittest.mock import Mock

    from footman.filesystem.descriptors import DirectoryDescriptor, OwnedDescriptor
    from footman.process import OwnedProcess

    primary = (
        KeyboardInterrupt("handoff interrupted")
        if audit_interrupted
        else OSError("handoff audit failed")
    )
    released = []
    child_failure, parent_failure = (
        OSError("child release failed"),
        OSError("parent release failed"),
    )

    class Counts(dict):
        def __getitem__(self, key):
            value = super().__getitem__(key)
            if key == "descriptor_adoptions" and value == 1:
                raise primary
            return value

    def close(fd):
        released.append(fd)
        if fd == 2222 and failure in {"child", "both"}:
            raise child_failure
        if fd == 1111 and failure in {"parent", "both"}:
            raise parent_failure

    ops = SimpleNamespace(openat=Mock(side_effect=[1111, 2222]), close=close)
    scope = {}
    with monkeypatch.context() as patch:
        patch.setattr(atexit, "register", lambda function: function)
        patch.setattr(OwnedDescriptor, "__init__", OwnedDescriptor.__init__)
        patch.setattr(OwnedProcess, "launch", OwnedProcess.__dict__["launch"])
        exec(h.ACQUISITION_GUARD, scope)
        scope["counts"] = Counts(descriptor_adoptions=0, launch_calls=0)
        with pytest.raises(type(primary)) as caught:
            DirectoryDescriptor.open_secure_path(ops, "/first/second")
        assert caught.value is primary
    assert released == [2222, 1111]
    if failure != "none":
        secondary = primary._transaction_secondary
        if failure in {"child", "both"}:
            assert child_failure in secondary
        if failure in {"parent", "both"}:
            assert parent_failure in secondary


@pytest.mark.parametrize(
    "fault",
    [
        "undeclared-input",
        "missing-dependency",
        "changed-common",
        "missing-host-root",
        "unresolved-head",
    ],
)
def test_invalid_prepared_profile_is_setup_failure_not_expected_attribution_rejection(
    tmp_path, monkeypatch, inert_profile_candidate, fault
):
    from tests import mcp_contract_support as service

    monkeypatch.setattr(h, "ROOT", inert_profile_candidate)
    scratch = tmp_path / "invalid-profile"
    scratch.mkdir()
    original_copy = service.copy_tree

    def copy(source, destination):
        original_copy(source, destination)
        if destination == scratch / "prepared/scripts":
            root = destination.parent
            if fault == "undeclared-input":
                p.write(
                    destination / "undeclared.dat", b"must not be silently supplied"
                )
            elif fault == "missing-dependency":
                (destination / "build_acceptance_wheel.py").unlink()
            elif fault == "changed-common":
                h.replace(root / "src/footman/__init__.py", b"wrong common bytes")
            elif fault == "missing-host-root":
                (root / "host_tests/__init__.py").unlink()
                (root / "host_tests").rmdir()

    monkeypatch.setattr(service, "copy_tree", copy)
    if fault == "unresolved-head":
        monkeypatch.setattr(h, "synthetic_head", lambda *_: "0" * 40)
    with pytest.raises((AssertionError, FileNotFoundError)):
        h.prepared_root(scratch)
    assert not (scratch / "prepared/dist").exists()


@pytest.mark.parametrize(
    "fault", ["none", "evidence-inconsistent", "attestation-duplicate"]
)
def test_copied_runner_probe_rejection_survives_actual_python_optimization(
    request, tmp_path, fault
):
    base = request.getfixturevalue("_mcp_worker_wheel")
    result, records, _ = h.standalone_probe(base, tmp_path, fault, optimized=True)
    assert (result["status"] == 0) is (fault == "none")
    installs = [
        p.load(records / name)
        for name in h.root_names(records)
        if name.startswith("installation-")
    ]
    assert installs and all(item["optimize"] == 1 for item in installs)
    assert (
        p.load(records / "service-observation.json")["child_cleanup_complete"] is True
    )


ARCHIVE_FAULTS = (
    "traversal",
    "absolute",
    "symlink",
    "duplicate",
    "module-alias",
    "scheme",
    "missing-entry",
    "entrypoint",
)


@pytest.mark.parametrize("fault", ARCHIVE_FAULTS)
def test_archive_negative_control_setup_is_readable_and_rejected_independently(
    request, tmp_path, fault
):
    import hashlib

    base = request.getfixturevalue("_mcp_worker_wheel")
    archive = tmp_path / "controlled.whl"
    data = h.malformed_archive(p.read(base.wheel), fault)
    p.write(archive, data)
    with pytest.raises((ValueError, KeyError)):
        wheel.archive_expectations(
            archive, hashlib.sha256(data).hexdigest(), tmp_path / "not-installed"
        )
    assert not (tmp_path / "not-installed").exists()


@pytest.mark.parametrize("fault", ARCHIVE_FAULTS)
def test_runner_rejects_unsafe_or_ambiguous_archive_before_install(
    request, tmp_path, fault
):
    from dataclasses import replace

    h.required_surface("run-host-acceptance")
    base = request.getfixturevalue("_mcp_worker_wheel")
    root, env, _ = h.policy_root(tmp_path, "pass")
    case = replace(CASES[0], wheel_bytes=h.malformed_archive(p.read(base.wheel), fault))
    manifest, binding = h.scripted_binding(
        root, tmp_path / "build-evidence", tmp_path / "build-tool", case=case
    )
    executable, extra, records = h.install_proxy(tmp_path, "none")
    env.update(extra)
    env.update(
        PATH=str(executable.parent) + os.pathsep + env["PATH"],
        FOOTMAN_HOST_TOOL_BLOCK_INSTALL="1",
    )
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    attempts = [p.load(records / name) for name in h.root_names(records)]
    assert not any("install" in item["argv"] for item in attempts), (
        "a scripted archive control reached installation"
    )
    assert not h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))


@pytest.mark.parametrize("where", ["top", "build-input"])
def test_runner_rejects_duplicate_binding_keys_even_when_values_match(tmp_path, where):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    encoded = p.canonical(binding)
    if where == "top":
        encoded = b'{"schema_version":1,' + encoded[1:]
    else:
        encoded = encoded.replace(
            b'"build_input_identity":{',
            b'"build_input_identity":{"schema_version":1,',
            1,
        )
    h.replace(manifest, encoded)
    executable, _, attempts = h.scripted_uv(tmp_path, CASES[0])
    env["PATH"] = str(executable.parent) + os.pathsep + env["PATH"]
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] != 0
    assert not h.root_names(attempts)
    assert not h.root_names(Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"]))


@pytest.mark.parametrize(
    "case,expected",
    [("pass", 0), ("skip", 1), ("xfail", 1), ("xpass", 1), ("fail", 1), ("empty", 5)],
)
def test_isolated_policy_fixture_executes_with_locked_wheel_dependencies(
    request, tmp_path, case, expected
):
    base = request.getfixturevalue("_mcp_worker_wheel")
    result, records, bound = h.standalone_policy(base, tmp_path, case)
    assert result["status"] == expected, result
    assert b"DEVELOPMENT_SUITE_SELECTED" not in result["stdout"] + result["stderr"]
    installations = [
        p.load(records / name)
        for name in h.root_names(records)
        if name.startswith("installation-")
    ]
    assert installations
    assert all(
        item["foundation"].startswith(str(bound.environment) + "/")
        and item["pythonpath"] is None
        for item in installations
    )
    for item in installations:
        assert (
            "--strict-config" in item["pytest_configuration"]
            and "--strict-markers" in item["pytest_configuration"]
        )
        assert "testpaths" not in item["pytest_configuration"]
    if case in {"pass", "xpass"}:
        observation = p.load(records / "service-observation.json")
        assert (
            observation["parser_confirmed"] is True
            and observation["child_cleanup_complete"] is True
        )
        assert (
            p.load(tmp_path / "verified-set.json")["wheel_sha256"] == base.wheel_sha256
        )


def test_install_proxy_blocks_scripted_artifacts_before_native_tool_launch(tmp_path):
    import sys

    executable, extra, records = h.install_proxy(tmp_path, "none")
    marker = tmp_path / "native-was-called"
    native = tmp_path / "native-canary"
    p.write(
        native,
        (
            f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, {str(h.ROOT)!r})\nfrom tests import mcp_provenance as p\np.write(Path({str(marker)!r}), b'native called')\n"
        ).encode(),
    )
    native.chmod(0o700)
    env = wheel.clean_environment(
        {
            **extra,
            "FOOTMAN_HOST_TOOL_REAL_UV": str(native),
            "FOOTMAN_HOST_TOOL_BLOCK_INSTALL": "1",
        }
    )
    result = wheel.command(
        [
            executable,
            "pip",
            "install",
            "--python",
            tmp_path / "never-created/bin/python",
            tmp_path / "inert.whl",
        ],
        cwd=tmp_path,
        env=env,
        timeout_seconds=20,
    )
    assert result["status"] != 0
    assert not marker.exists()
    observed = [p.load(records / name) for name in h.root_names(records)]
    assert len(observed) == 1
    assert observed[0]["native_attempted"] is False and observed[0]["status"] is None


def test_runner_keeps_available_configured_id_when_operator_tag_is_retargeted(tmp_path):
    h.required_surface("run-host-acceptance")
    root, env, _ = h.policy_root(tmp_path, "pass")
    h.configure_retag_loader(
        root / "host_tests/contract_support/tests/relocated_docker_child.py"
    )
    env["FOOTMAN_HOST_CONTRACT_FAULT"] = "retag-image"
    manifest, binding = h.bind(root, tmp_path / "build-evidence")
    result = h.runner(
        root, manifest, binding["wheel_path"], tmp_path / "host-evidence", env=env
    )
    assert result["status"] == 0, result
    records = Path(env["FOOTMAN_HOST_CONTRACT_RECORDS"])
    before = p.load(records / "images-before.json")
    after = p.load(records / "images-after.json")
    assert before["operator-owned:latest"] != after["operator-owned:latest"]
    assert before["d9e853e87e55"] == after["d9e853e87e55"]
    selected = p.load(records / "service-stub-records/selected-image.json")
    assert selected == {
        "operand": "d9e853e87e55",
        "resolved_image": before["d9e853e87e55"],
    }
    value = p.load(records / "mcp-result.json")
    retained = p.load(Path(value["resultPath"]))
    assert retained["observed_image_id"] == before["d9e853e87e55"]


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "argv",
        "cwd",
        "bool-status",
        "missing-status",
        "unattempted-status",
        "relative-stream",
        "escaping-stream",
        "dotdot-stream",
    ],
)
def test_build_observation_reader_rejects_invalid_records_without_fabricating_status(
    tmp_path, fault
):
    root = tmp_path / "root"
    root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    p.write(evidence / "stdout.bin", b"complete\x00\xff stdout")
    p.write(evidence / "stderr.bin", b"complete\x80 stderr\n")
    operation = {
        "attempted": True,
        "argv": ["uv", "build"],
        "cwd": str(root),
        "status": 17,
        "stdout_path": str(evidence / "stdout.bin"),
        "stderr_path": str(evidence / "stderr.bin"),
    }
    if fault == "argv":
        operation["argv"].append("--out-dir")
    elif fault == "cwd":
        operation["cwd"] = str(tmp_path)
    elif fault == "bool-status":
        operation["status"] = False
    elif fault == "missing-status":
        operation["status"] = None
    elif fault == "unattempted-status":
        operation["attempted"] = False
    elif fault == "relative-stream":
        operation["stdout_path"] = "stdout.bin"
    elif fault == "escaping-stream":
        operation["stdout_path"] = str(root / "outside.bin")
    elif fault == "dotdot-stream":
        operation["stdout_path"] = str(evidence / "../root/outside.bin")
    manifest = evidence / "build.json"
    p.write(evidence / "build-operation.json", p.canonical(operation))
    if fault == "none":
        observed = h.build_operation(manifest, root)
        assert observed["status"] == 17
        assert observed["stdout_bytes"] == b"complete\x00\xff stdout"
        assert observed["stderr_bytes"] == b"complete\x80 stderr\n"
    else:
        with pytest.raises(AssertionError):
            h.build_operation(manifest, root)


def test_unattempted_build_record_exposes_no_observed_empty_streams(tmp_path):
    p.write(
        tmp_path / "build-operation.json",
        p.canonical(
            {
                "attempted": False,
                "argv": ["uv", "build"],
                "cwd": str(tmp_path),
                "status": None,
            }
        ),
    )
    observed = h.build_operation(tmp_path / "build.json", tmp_path)
    assert observed["status"] is None
    assert "stdout_bytes" not in observed and "stderr_bytes" not in observed


@pytest.mark.parametrize("post_fault", ["missing-readme", "interruption"])
def test_failed_build_keeps_real_status_and_streams_when_postcheck_also_fails(
    tmp_path, post_fault
):
    h.required_surface("build-acceptance-wheel")
    root, _ = h.prepared_root(tmp_path)
    case = CASES[1]
    assert case.build_status == 17
    executable, call, attempts = h.scripted_uv(tmp_path, case)
    if post_fault == "missing-readme":
        script = p.read(executable).decode()
        assert script.count("raise SystemExit(17)") == 1
        h.replace(
            executable,
            script.replace(
                "raise SystemExit(17)",
                "(root / 'README.md').unlink()\nraise SystemExit(17)",
            ).encode(),
        )
    else:
        h.install_readme_guard(root, "interruption", after=call)
    before_source = h.inventory(root)[0]
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = evidence / "build.json"
    result = h.invoke(
        root,
        "build-acceptance-wheel",
        ["--manifest", str(manifest)],
        cwd=tmp_path,
        evidence=evidence,
        env=wheel.clean_environment(
            {"PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
        ),
    )
    assert result["status"] != 0
    assert not manifest.exists()
    assert len(h.root_names(attempts)) == 1
    operation = h.build_operation(manifest, root)
    assert operation["status"] == 17
    assert operation["stdout_bytes"] == case.stdout_bytes
    assert operation["stderr_bytes"] == case.stderr_bytes
    assert operation["post_check_error"] is not None
    assert h.inventory(root)[0] == before_source
    if post_fault == "interruption":
        assert result["status"] in {-2, 130}
    else:
        assert "README.md" in json.dumps(operation["post_check_error"])


def test_generated_operation_record_preserves_binary_streams_and_status():
    value = {
        "argv": ["uv", "build"],
        "cwd": "/fixture",
        "status": 17,
        "stdout": b"\0\xff",
        "stderr": b"\x80\n",
    }
    record = h.operation_record(value)
    assert json.loads(p.canonical(record)) == {
        **value,
        "stdout": "AP8=",
        "stderr": "gAo=",
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"schema_version": True, "case_id": "cleanup", "expected_override": False},
        {"schema_version": 2, "case_id": "cleanup", "expected_override": False},
        {"schema_version": 1, "case_id": [], "expected_override": False},
        {"schema_version": 1, "case_id": "unknown", "expected_override": False},
        {"schema_version": 1, "case_id": "cleanup", "expected_override": 0},
        {"schema_version": 1, "case_id": "command_status", "expected_override": True},
        {"schema_version": 1, "case_id": "command_status", "expected_override": 1.0},
        {"schema_version": 1, "case_id": "command_status", "expected_override": "1"},
        {"schema_version": 1, "case_id": "staged_bytes", "expected_override": []},
        {"schema_version": 1, "case_id": "staged_bytes", "expected_override": "\ud800"},
        {"schema_version": 1, "case_id": "argv", "expected_override": []},
        {"schema_version": 1, "case_id": "argv", "expected_override": [""]},
        {"schema_version": 1, "case_id": "argv", "expected_override": ["a\x00b"]},
        {"schema_version": 1, "case_id": "argv", "expected_override": [True]},
        {"schema_version": 1, "case_id": "argv", "expected_override": "command"},
        {
            "schema_version": 1,
            "case_id": "cleanup",
            "expected_override": False,
            "command": ["x"],
        },
    ],
)
def test_fixture_expectation_schema_rejects_invalid_values_without_acquisition(value):
    from tests.host_acceptance_assertions import validate_expectation_input

    with pytest.raises(AssertionError):
        validate_expectation_input(value)


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
def test_fixture_expectation_schema_roundtrip_and_no_observation_mutation(case):
    from tests.host_acceptance_assertions import (
        validate_expectation_input,
        verify_expectation,
    )

    expected = {
        "staged_bytes": "staged\n",
        "argv": ["tool", "two words"],
        "command_status": -9,
        "cleanup": True,
    }[case]
    value = {"schema_version": 1, "case_id": case, "expected_override": expected}
    parsed = validate_expectation_input(json.loads(p.canonical(value)))
    actual = expected.encode() if case == "staged_bytes" else expected
    verify_expectation(case, actual, parsed["expected_override"])
    assert value == parsed
    if case == "argv":
        parsed["expected_override"].append("only copied expectation")
        assert value["expected_override"] == ["tool", "two words"]


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
def test_copied_fixture_negative_controls_reach_shared_assertions_with_raw_status(
    simulated_host_run, tmp_path, case
):
    import copy

    from tests import host_expectation_support as fixture
    from tests.host_acceptance_assertions import EXPECTATION_DIAGNOSTICS

    before = copy.deepcopy(simulated_host_run)
    overrides = {
        "staged_bytes": "later unstaged bytes must not select the tree\n",
        "argv": ["wrong expectation only", "literal ; $value"],
        "command_status": 99,
        "cleanup": False,
    }
    expectation = {
        "schema_version": 1,
        "case_id": case,
        "expected_override": overrides[case],
    }
    workspace, ini, profile = fixture.copied_fixture(
        tmp_path, simulated_host_run, profile="negative", expectation=expectation
    )
    result = fixture.execute_fixture(tmp_path, workspace, ini, profile["suite"])
    assert result["status"] == 1, result
    output = result["stdout"] + result["stderr"]
    assert EXPECTATION_DIAGNOSTICS[case].encode() in output, result
    assert b"1 failed" in output, result
    assert b"ERROR collecting" not in output and b"INTERNALERROR" not in output, result
    assert fixture.BASELINE_MARKER in output.splitlines(), result
    assert fixture.classify_negative_result(result, case) == {
        "detected": True,
        "baseline_verified": True,
        "failure_kind": "assertion",
    }
    assert simulated_host_run == before
    paths = {entry["path"] for entry in profile["inventory"]}
    assert fixture.NEGATIVE_SUITE in paths and fixture.NORMAL_SUITE not in paths
    assert "host_tests/expectation_input.json" in profile["generated_inputs"]
    assert "tests/host_acceptance_assertions.py" in {
        entry["path"] for entry in profile["source_support"]
    }
    assert "tests/mcp_protocol_child.py" not in paths


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
@pytest.mark.parametrize("fault", ["missing", "invalid"])
def test_copied_negative_same_field_invalid_observations_are_not_targeted_detections(
    simulated_host_run, tmp_path, case, fault
):
    import copy

    from tests import host_expectation_support as fixture
    from tests.host_acceptance_assertions import EXPECTATION_DIAGNOSTICS

    before = copy.deepcopy(simulated_host_run)
    overrides = {
        "staged_bytes": "wrong bytes",
        "argv": ["wrong argv"],
        "command_status": 99,
        "cleanup": False,
    }
    workspace, ini, profile = fixture.copied_fixture(
        tmp_path,
        simulated_host_run,
        profile="negative",
        expectation={
            "schema_version": 1,
            "case_id": case,
            "expected_override": overrides[case],
        },
    )
    # Corrupt only generated external observation JSON, after byte transport.
    # In particular None staged bytes must reach verify_run, not b64encode().
    path = workspace / "host_tests/observations.json"
    value = p.load(path)
    container, key = {
        "staged_bytes": (value, "staged_bytes"),
        "argv": (value["manifest"], "command_argv"),
        "command_status": (value["manifest"], "return_code"),
        "cleanup": (value["manifest"]["cleanup"], "confirmed_absent"),
    }[case]
    if fault == "missing":
        del container[key]
    else:
        container[key] = None
    h.replace(path, p.canonical(value))
    p.write(
        tmp_path / "invalid-observation-input.json",
        p.canonical({"case_id": case, "fault": fault, "generated_observations": value}),
    )
    result = fixture.execute_fixture(tmp_path, workspace, ini, profile["suite"])
    output = result["stdout"] + result["stderr"]
    assert result["status"] == 1, result
    assert b"FIXTURE_OBSERVATIONS_ACQUIRED" in output.splitlines(), result
    assert fixture.BASELINE_MARKER not in output.splitlines(), result
    assert b"verify_run(**observations)" in output, result
    if fault == "invalid":
        assert EXPECTATION_DIAGNOSTICS[case].encode() in output, result
    classified = fixture.classify_negative_result(result, case)
    p.write(tmp_path / "classification.json", p.canonical(classified))
    assert classified == {
        "detected": False,
        "baseline_verified": False,
        "failure_kind": "invalid-observation/setup",
    }
    assert simulated_host_run == before


@pytest.mark.parametrize("fault", ["both-missing", "both-none"])
def test_run_baseline_requires_both_staged_byte_arguments(simulated_host_run, fault):
    import copy

    from tests.host_acceptance_assertions import verify_run

    value = copy.deepcopy(simulated_host_run)
    verify_run(**value)
    for key in ("staged_bytes", "expected_staged_bytes"):
        if fault == "both-missing":
            del value[key]
        else:
            value[key] = None
    expected_error = TypeError if fault == "both-missing" else AssertionError
    with pytest.raises(expected_error):
        verify_run(**value)


@pytest.mark.parametrize("profile", ["normal", "negative"])
@pytest.mark.parametrize("fault", ["both-missing", "both-none"])
def test_copied_profiles_refuse_missing_or_none_staged_evidence_pair_before_baseline_marker(
    simulated_host_run, tmp_path, profile, fault
):
    from tests import host_expectation_support as fixture

    expectation = (
        None
        if profile == "normal"
        else {
            "schema_version": 1,
            "case_id": "staged_bytes",
            "expected_override": "wrong expectation",
        }
    )
    workspace, ini, record = fixture.copied_fixture(
        tmp_path,
        simulated_host_run,
        profile=profile,
        expectation=expectation,
    )
    path = workspace / "host_tests/observations.json"
    value = p.load(path)
    for key in ("staged_bytes", "expected_staged_bytes"):
        if fault == "both-missing":
            del value[key]
        else:
            value[key] = None
    h.replace(path, p.canonical(value))
    p.write(
        tmp_path / "invalid-observation-input.json",
        p.canonical(
            {
                "case_id": "staged_bytes",
                "profile": profile,
                "fault": fault,
                "generated_observations": value,
            }
        ),
    )
    result = fixture.execute_fixture(tmp_path, workspace, ini, record["suite"])
    output = result["stdout"] + result["stderr"]
    assert result["status"] == 1, result
    assert b"verify_run(**observations)" in output, result
    assert b"FIXTURE_OBSERVATIONS_ACQUIRED" in output.splitlines(), result
    assert fixture.BASELINE_MARKER not in output.splitlines(), result
    classified = fixture.classify_negative_result(result, "staged_bytes")
    p.write(tmp_path / "classification.json", p.canonical(classified))
    assert classified == {
        "detected": False,
        "baseline_verified": False,
        "failure_kind": "invalid-observation/setup",
    }
    if fault == "both-missing":
        assert (
            b"TypeError" in output and b"required keyword-only arguments" in output
        ), result
    else:
        assert b"staged bytes expectation must be a UTF-8 string" in output, result


def _negative_outcome_control(case):
    from tests import host_expectation_support as fixture

    value = {
        "schema_version": 1,
        "collected": 1,
        "exitstatus": 1,
        "collection_errors": [],
        "internal_errors": [],
        "interrupted": False,
        "cleanup_complete": True,
        "reports": [
            {
                "nodeid": fixture.NEGATIVE_SUITE + "::test_negative_expectations",
                "when": when,
                "outcome": outcome,
                "wasxfail": False,
                "message": "AssertionError: " + fixture.EXPECTATION_DIAGNOSTICS[case]
                if when == "call"
                else None,
                "longrepr": "AssertionError: " + fixture.EXPECTATION_DIAGNOSTICS[case]
                if when == "call"
                else "",
            }
            for when, outcome in (
                ("setup", "passed"),
                ("call", "failed"),
                ("teardown", "passed"),
            )
        ],
    }
    return fixture, value


def _classify_outcome_control(fixture, value, case, *, suffix=b"1 failed\n"):
    result = {
        "status": 1,
        "stdout": fixture.BASELINE_MARKER
        + b"\n"
        + fixture.EXPECTATION_DIAGNOSTICS[case].encode()
        + b"\n"
        + fixture.OUTCOMES_MARKER
        + p.canonical(value)
        + b"\n"
        + suffix,
        "stderr": b"",
    }
    return fixture.classify_negative_result(result, case)


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
def test_negative_attribution_requires_exact_successful_fixture_cleanup(case):
    fixture, value = _negative_outcome_control(case)
    assert _classify_outcome_control(fixture, value, case)["detected"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "schema-bool",
        "collected-bool",
        "extra-collected",
        "status-bool",
        "status-zero",
        "cleanup-false",
        "cleanup-missing",
        "cleanup-integer",
        "interrupted",
        "collection-error",
        "internal-error",
        "missing-teardown",
        "failed-teardown",
        "extra-failure",
        "extra-error",
        "setup-failure",
        "skipped",
        "xfail",
        "xpass",
        "wrong-node",
        "wrong-diagnostic",
        "non-assertion",
        "diagnostic-in-traceback-only",
        "extra-field",
        "wrong-order",
    ],
)
def test_negative_attribution_rejects_inexact_structured_outcomes(fault):
    import copy

    case = "staged_bytes"
    fixture, value = _negative_outcome_control(case)
    if fault in {"schema-bool", "collected-bool", "status-bool"}:
        value[
            {
                "schema-bool": "schema_version",
                "collected-bool": "collected",
                "status-bool": "exitstatus",
            }[fault]
        ] = True
    elif fault == "extra-collected":
        value["collected"] = 2
    elif fault == "status-zero":
        value["exitstatus"] = 0
    elif fault == "cleanup-missing":
        del value["cleanup_complete"]
    elif fault.startswith("cleanup-"):
        value["cleanup_complete"] = False if fault == "cleanup-false" else 1
    elif fault == "interrupted":
        value["interrupted"] = True
    elif fault in {"collection-error", "internal-error"}:
        value[
            {
                "collection-error": "collection_errors",
                "internal-error": "internal_errors",
            }[fault]
        ] = ["injected failure"]
    elif fault == "missing-teardown":
        value["reports"].pop()
    elif fault in {"extra-failure", "extra-error"}:
        extra = copy.deepcopy(value["reports"][1 if fault == "extra-failure" else 0])
        extra["nodeid"] += "_unrelated"
        extra["outcome"] = "failed"
        value["reports"].append(extra)
    elif fault in {"setup-failure", "failed-teardown", "skipped", "xpass", "xfail"}:
        index = {"setup-failure": 0, "failed-teardown": 2}.get(fault, 1)
        value["reports"][index]["outcome"] = {
            "skipped": "skipped",
            "xfail": "skipped",
            "xpass": "passed",
        }.get(fault, "failed")
        value["reports"][index]["wasxfail"] = fault in {"xfail", "xpass"}
    elif fault == "wrong-node":
        value["reports"][1]["nodeid"] += "_unrelated"
    elif fault in {"wrong-diagnostic", "non-assertion", "diagnostic-in-traceback-only"}:
        value["reports"][1]["message"] = (
            "AssertionError: unrelated"
            if fault == "wrong-diagnostic"
            else "RuntimeError: " + fixture.EXPECTATION_DIAGNOSTICS[case]
        )
        if fault != "diagnostic-in-traceback-only":
            value["reports"][1]["longrepr"] = value["reports"][1]["message"]
    elif fault == "extra-field":
        value["unexpected"] = True
    else:
        value["reports"].reverse()
    classified = _classify_outcome_control(
        fixture, value, case, suffix=b"1 failed, 1 error\n"
    )
    assert classified == {
        "detected": False,
        "baseline_verified": True,
        "failure_kind": "unrelated-failure",
    }


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "malformed", "duplicate-key"]
)
def test_negative_attribution_rejects_missing_or_ambiguous_outcome_records(fault):
    fixture, value = _negative_outcome_control("staged_bytes")
    raw = p.canonical(value)
    if fault == "malformed":
        raw = b"{"
    elif fault == "duplicate-key":
        raw = raw.replace(b'"collected":1', b'"collected":1,"collected":1')
    record = fixture.OUTCOMES_MARKER + raw + b"\n"
    result = {
        "status": 1,
        "stdout": fixture.BASELINE_MARKER
        + b"\n"
        + fixture.EXPECTATION_DIAGNOSTICS["staged_bytes"].encode()
        + b"\n1 failed, 1 error\nERROR at teardown: cleanup failed\n"
        + (b"" if fault == "missing" else record * (2 if fault == "duplicate" else 1)),
        "stderr": b"",
    }
    assert fixture.classify_negative_result(result, "staged_bytes") == {
        "detected": False,
        "baseline_verified": True,
        "failure_kind": "unrelated-failure",
    }


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
@pytest.mark.parametrize(
    "fault", ["teardown", "additional-failure", "additional-setup-error"]
)
def test_copied_negative_assertion_plus_independent_failure_is_not_detection(
    simulated_host_run, tmp_path, case, fault
):
    from tests import host_expectation_support as fixture

    overrides = {
        "staged_bytes": "wrong",
        "argv": ["wrong"],
        "command_status": 99,
        "cleanup": False,
    }
    workspace, ini, profile = fixture.copied_fixture(
        tmp_path,
        simulated_host_run,
        profile="negative",
        expectation={
            "schema_version": 1,
            "case_id": case,
            "expected_override": overrides[case],
        },
    )
    if fault == "teardown":
        path = workspace / "host_tests/conftest.py"
        extra = b'\n@pytest.fixture(autouse=True)\ndef fail_cleanup():\n    yield\n    raise RuntimeError("injected teardown cleanup failure")\n'
    else:
        path = workspace / fixture.NEGATIVE_SUITE
        extra = (
            b'\nimport pytest\n@pytest.fixture\ndef unrelated_setup():\n    raise RuntimeError("injected setup failure")\ndef test_unrelated_error(unrelated_setup):\n    pass\n'
            if fault == "additional-setup-error"
            else b'\ndef test_unrelated_failure():\n    raise AssertionError("injected unrelated failure")\n'
        )
    h.replace(path, p.read(path) + extra)
    result = fixture.execute_fixture(tmp_path, workspace, ini, profile["suite"])
    assert result["status"] == 1, result
    output = result["stdout"] + result["stderr"]
    assert fixture.BASELINE_MARKER in output.splitlines(), result
    assert fixture.EXPECTATION_DIAGNOSTICS[case].encode() in output, result
    assert (
        b"2 failed" if fault == "additional-failure" else b"1 failed, 1 error"
    ) in output, result
    # Pytest repeats captured call stdout in this same item's teardown error.
    # Preserve the existing exact-one-physical-marker rule; do not relax it to
    # make teardown failures look like valid baselines. Independently prove
    # that structured outcomes also reject the failure even without that gate.
    assert output.splitlines().count(fixture.BASELINE_MARKER) == (
        2 if fault == "teardown" else 1
    ), result
    assert (
        fixture._has_exact_negative_outcomes((result["stdout"], result["stderr"]), case)
        is False
    )
    classified = fixture.classify_negative_result(result, case)
    p.write(tmp_path / "classification.json", p.canonical(classified))
    assert classified == {
        "detected": False,
        "baseline_verified": fault != "teardown",
        "failure_kind": "invalid-observation/setup"
        if fault == "teardown"
        else "unrelated-failure",
    }


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
@pytest.mark.parametrize(
    "marker",
    [
        b"",
        b'    print("FIXTURE_BASELINE_VERIFIED")\n',
        b"FIXTURE_BASELINE_VERIFIED\nFIXTURE_BASELINE_VERIFIED\n",
    ],
)
def test_negative_attribution_rejects_missing_source_snippet_or_duplicate_baseline_marker(
    case, marker
):
    from tests import host_expectation_support as fixture
    from tests.host_acceptance_assertions import EXPECTATION_DIAGNOSTICS

    result = {
        "status": 1,
        "stdout": marker + EXPECTATION_DIAGNOSTICS[case].encode() + b"\n1 failed\n",
        "stderr": b"",
    }
    assert fixture.classify_negative_result(result, case) == {
        "detected": False,
        "baseline_verified": False,
        "failure_kind": "invalid-observation/setup",
    }


def test_copied_normal_fixture_uses_shared_assertions_without_override_input(
    simulated_host_run, tmp_path
):
    from tests import host_expectation_support as fixture

    workspace, ini, profile = fixture.copied_fixture(
        tmp_path, simulated_host_run, profile="normal"
    )
    result = fixture.execute_fixture(tmp_path, workspace, ini, profile["suite"])
    assert result["status"] == 0, result
    assert b"1 passed" in result["stdout"], result
    paths = {entry["path"] for entry in profile["inventory"]}
    assert fixture.NORMAL_SUITE in paths
    assert fixture.NEGATIVE_SUITE not in paths
    assert "host_tests/expectation_input.json" not in paths


@pytest.mark.parametrize("profile", ["normal", "negative"])
@pytest.mark.parametrize(
    "fault", ["missing", "malformed", "unknown", "duplicate", "unrelated"]
)
def test_copied_fixture_setup_and_unrelated_failures_cannot_count_as_detection(
    simulated_host_run, tmp_path, profile, fault
):
    import copy

    from tests import host_expectation_support as fixture

    observations = copy.deepcopy(simulated_host_run)
    expectation = {
        "schema_version": 1,
        "case_id": "cleanup",
        "expected_override": False,
    }
    raw = None
    if fault == "missing":
        expectation = None
    elif fault == "malformed":
        raw = b'{"schema_version":true,"case_id":"cleanup","expected_override":false}'
    elif fault == "unknown":
        expectation["case_id"] = "command_override"
    elif fault == "duplicate":
        raw = b'{"schema_version":1,"case_id":"cleanup","case_id":"cleanup","expected_override":false}'
    else:
        observations["streams"]["stdout"] += b"unrelated failure"
    workspace, ini, record = fixture.copied_fixture(
        tmp_path, observations, profile=profile, expectation=expectation, raw_input=raw
    )
    result = fixture.execute_fixture(tmp_path, workspace, ini, record["suite"])
    output = result["stdout"] + result["stderr"]
    if profile == "normal" and fault == "missing":
        assert result["status"] == 0, result
    else:
        assert result["status"] != 0, result
        assert fixture.classify_negative_result(result, "cleanup")["detected"] is False
        assert b"AssertionError: container cleanup mismatch" not in output, result
        if profile == "negative" and fault == "unrelated":
            assert b"command stdout mismatch" in output, result
        else:
            assert b"FIXTURE_OBSERVATIONS_ACQUIRED" not in output, result
            assert b"INTERNALERROR" in output, result


def _assert_no_orchestrator_shell_wrapper(scripts):
    forbidden = sorted(
        name
        for name in h.root_names(scripts)
        if name in {"verify-host-acceptance", "verify_host_acceptance"}
        or name.endswith(".sh")
    )
    assert not forbidden, f"forbidden host orchestrator wrappers: {forbidden}"


@pytest.mark.parametrize(
    "name",
    [
        "verify-host-acceptance",
        "verify_host_acceptance",
        "verify-host-acceptance.sh",
        "verify_host_acceptance.sh",
        "alternate.sh",
    ],
)
def test_single_file_wrapper_guard_rejects_all_forbidden_names(tmp_path, name):
    p.write(tmp_path / name, b"#!/bin/sh\n")
    with pytest.raises(AssertionError, match="forbidden host orchestrator wrappers"):
        _assert_no_orchestrator_shell_wrapper(tmp_path)


def test_single_file_wrapper_guard_preserves_required_low_level_wrappers(tmp_path):
    for name in ("build-acceptance-wheel", "run-host-acceptance"):
        p.write(tmp_path / name, b"#!/bin/sh\n")
    _assert_no_orchestrator_shell_wrapper(tmp_path)


def test_single_file_host_orchestrator_interface_and_internal_seams_are_declared():
    path = h.required_surface("verify_host_acceptance.py")
    data = p.read(path)
    assert data.splitlines()[0] == b"#!/usr/bin/env python3.14"
    _assert_no_orchestrator_shell_wrapper(h.ROOT / "scripts")
    tree = ast.parse(data)
    definitions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert {"main", "_execute_prepared_leg", "_load_host_runner"} <= definitions.keys()
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_RESULTS_BASE"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert "/tmp/gorshochek/ephemeral/footman.git/host-acceptance" in ast.unparse(
        assignments[0].value
    )
    source = data.decode()
    assert "bootstrap_import_root" in source
    assert "spec_from_file_location" in source and "exec_module" in source
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "scripts"
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
def test_copied_negative_profile_accepts_matching_expectation(
    simulated_host_run, tmp_path, case
):
    from tests import host_expectation_support as fixture

    expected = {
        "staged_bytes": simulated_host_run["expected_staged_bytes"],
        "argv": simulated_host_run["command_argv"],
        "command_status": simulated_host_run["status"],
        "cleanup": True,
    }[case]
    workspace, ini, profile = fixture.copied_fixture(
        tmp_path,
        simulated_host_run,
        profile="negative",
        expectation={
            "schema_version": 1,
            "case_id": case,
            "expected_override": expected,
        },
    )
    result = fixture.execute_fixture(tmp_path, workspace, ini, profile["suite"])
    assert result["status"] == 0, result
    assert b"1 passed" in result["stdout"], result


@pytest.mark.parametrize("status", [0, 19])
def test_orchestration_core_tool_fixture_is_executable_without_future_interfaces(
    tmp_path, status
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(
        tmp_path,
        fixture_source=b'#!/usr/bin/env python3.14\n"""Inert readiness marker, not the orchestrator."""\n',
        inspect_status=status,
        build_status=status,
    )
    environment = core.isolated_environment(prepared, tmp_path)
    assert environment["PATH"].split(os.pathsep) == [
        str(prepared["tools"]),
        str(tmp_path / "bootstrap-tools"),
    ]
    commands = [
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            core.TAG,
        ],
        [
            prepared["root"] / "scripts/build-acceptance-wheel",
            "--manifest",
            tmp_path / "synthetic-manifest.json",
        ],
    ]
    outcomes = []
    for ordinal, argv in enumerate(commands):
        outcome = wheel.command(
            argv, cwd=prepared["foreign"], env=environment, timeout_seconds=30
        )
        p.write(
            tmp_path / f"tool-readiness-{ordinal}.json",
            p.canonical(h.operation_record(outcome)),
        )
        assert outcome["status"] == status, outcome
        outcomes.append(outcome)
    assert outcomes[0]["stdout"] == (core.IMAGE + "\n").encode()
    assert outcomes[0]["stderr"] == b"inspection diagnostic\x00\xff\n"
    assert outcomes[1]["stdout"] == b"synthetic controlled build\x00\xff\n"
    assert outcomes[1]["stderr"] == b"synthetic build diagnostic\x80\n"
    calls = [
        p.load(prepared["control"].parent / "calls" / name)
        for name in h.root_names(prepared["control"].parent / "calls")
    ]
    assert sorted(item["mode"] for item in calls) == ["build", "docker"]
    assert all(
        item["resolved_docker"] == str(prepared["tools"] / "docker") for item in calls
    )
    assert all(item["simulation"] for item in calls)
    assert all(
        item["support_origin"] == str(prepared["root"] / "tests/mcp_provenance.py")
        for item in calls
    )
    if status == 0:
        manifest = p.load(tmp_path / "synthetic-manifest.json")
        assert manifest == p.load(prepared["control"])["manifest"]
        assert manifest["source_identity"] == h.inventory(prepared["root"])[0]
        assert manifest["build_input_identity"] == wheel.inventory(prepared["root"])[0]
        assert p.read(Path(manifest["wheel_path"])) == b"wheel"
        assert (
            hashlib_for_contract(p.read(Path(manifest["wheel_path"])))
            == manifest["wheel_sha256"]
        )
    else:
        assert not (tmp_path / "synthetic-manifest.json").exists()
    assert core.snapshot_candidate() == prepared["profile"]["candidate_before"]
    assert h.inventory(prepared["root"])[0] == prepared["profile"]["source_identity"]
    assert (
        wheel.inventory(prepared["root"])[0]
        == prepared["profile"]["build_input_identity"]
    )


def hashlib_for_contract(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()


def core_report(prepared, names):
    assert len(names) == 1, "each invocation owns one fresh results directory"
    run = prepared["base"] / names[0]
    report = p.load(run / "report.json")
    assert report["schema_version"] == 1 and type(report["schema_version"]) is int
    assert report["bootstrap_import_root"] == str(prepared["root"] / "src")
    assert report["repository_root"] == str(prepared["root"])
    assert report["source_identity"] == prepared["profile"]["source_identity"]
    assert report["build_input_identity"] == prepared["profile"]["build_input_identity"]
    assert report["revision"] == prepared["profile"]["synthetic_revision"]
    return run, report


def test_orchestrator_core_resolves_tag_once_and_hands_exact_build_to_all_profiles(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    result, observed, calls, names = core.invoke(prepared)
    assert result["status"] == observed["call_status"] == 0, result
    assert observed["error"] is None
    assert observed["cwd_restored"] and observed["environment_restored"]
    assert [item["case_id"] for item in observed["legs"]] == [*core.CASES, None]
    assert [item["profile"] for item in observed["legs"]] == ["negative"] * 4 + [
        "normal"
    ]
    inspect = [item for item in calls if item["mode"] == "docker"]
    builds = [item for item in calls if item["mode"] == "build"]
    assert len(inspect) == len(builds) == 1
    assert inspect[0]["argv"] == ["image", "inspect", "--format", "{{.Id}}", core.TAG]
    assert builds[0]["argv"][0] == "--manifest" and len(builds[0]["argv"]) == 2
    manifest_path = Path(builds[0]["argv"][1])
    manifest = p.load(manifest_path)
    assert all(
        leg["wheel"] == manifest["wheel_path"]
        and leg["source_identity"] == str(manifest_path)
        and leg["image"] == core.IMAGE
        for leg in observed["legs"]
    )
    assert len({leg["workspace"] for leg in observed["legs"]}) == 5
    assert len({leg["evidence"] for leg in observed["legs"]}) == 5
    run, report = core_report(prepared, names)
    assert str(run).encode() in result["stdout"], (
        "print exact fresh external evidence path"
    )
    assert report["state"] == "final" and report["success"] is True
    assert report["first_unexpected_status"] is None
    assert [leg["pytest_status"] for leg in report["legs"]] == [1, 1, 1, 1, 0]
    assert all(leg["detected"] for leg in report["legs"][:4])
    assert all("runner_process_status" not in leg for leg in report["legs"])
    from tests.host_acceptance_assertions import EXPECTATION_DIAGNOSTICS

    for case, leg in zip([*core.CASES, None], report["legs"], strict=True):
        diagnostic = (
            "normal suite passed" if case is None else EXPECTATION_DIAGNOSTICS[case]
        )
        baseline = "" if case is None else "FIXTURE_BASELINE_VERIFIED\n"
        assert p.read(leg["stdout_path"]) == (baseline + diagnostic + "\n").encode()
        if case is not None:
            assert leg["baseline_verified"] is True
        assert p.read(leg["stderr_path"]) == b"synthetic boundary diagnostic\x00\xff"
    recorded_builds = [
        command
        for command in report["commands"]
        if "build-acceptance-wheel" in str(command["argv"][0])
    ]
    recorded_inspects = [
        command
        for command in report["commands"]
        if Path(command["argv"][0]).name == "docker"
    ]
    assert len(recorded_builds) == len(recorded_inspects) == 1
    assert recorded_inspects[0]["argv"][1:] == inspect[0]["argv"]
    assert p.read(recorded_inspects[0]["stdout_path"]) == (core.IMAGE + "\n").encode()
    assert (
        p.read(recorded_inspects[0]["stderr_path"])
        == b"inspection diagnostic\x00\xff\n"
    )
    assert (
        p.read(recorded_builds[0]["stdout_path"])
        == b"synthetic controlled build\x00\xff\n"
    )
    assert (
        p.read(recorded_builds[0]["stderr_path"]) == b"synthetic build diagnostic\x80\n"
    )
    assert (
        p.read(prepared["root"] / "dist/unrelated-stale.whl")
        == b"never select this wheel"
    )


@pytest.mark.parametrize("case", ["staged_bytes", "argv", "command_status", "cleanup"])
def test_orchestrator_core_matching_diagnostic_without_verified_baseline_is_invalid_observation(
    tmp_path, case
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    # Keep assertion kind, raw status, failed count and exact target diagnostic
    # identical to a valid negative leg; remove only baseline confirmation.
    result, observed, _, names = core.invoke(
        prepared, outcomes={case: {"baseline_verified": False}}
    )
    assert result["status"] == observed["call_status"] == 1, result
    _, report = core_report(prepared, names)
    assert report["success"] is False and report["first_unexpected_status"] == 1
    assert [leg["case_id"] for leg in observed["legs"]] == [*core.CASES, None]
    leg = report["legs"][core.CASES.index(case)]
    assert leg["pytest_status"] == 1 and leg["detected"] is False
    assert leg["baseline_verified"] is False
    assert leg["failure_kind"] == "invalid-observation/setup"
    assert leg["counts"]["collected"] == leg["counts"]["failed"] == 1
    from tests.host_acceptance_assertions import EXPECTATION_DIAGNOSTICS

    assert p.read(leg["stdout_path"]) == (EXPECTATION_DIAGNOSTICS[case] + "\n").encode()
    assert b"FIXTURE_BASELINE_VERIFIED" not in p.read(leg["stdout_path"]).splitlines()


@pytest.mark.parametrize(
    "chosen,expected_status",
    [
        ({"status": 17, "failure_kind": "prerequisite"}, 17),
        ({"diagnostic": "unrelated assertion mismatch"}, 1),
        ({"failure_kind": "setup"}, 1),
        (
            {
                "counts": {
                    "collected": 1,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 1,
                    "xfailed": 0,
                    "xpassed": 0,
                }
            },
            1,
        ),
        ({"cleanup_complete": False}, 1),
        ({"evidence_complete": False}, 1),
        (
            {
                "status": 5,
                "counts": {
                    "collected": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                },
            },
            5,
        ),
        (
            {
                "status": 0,
                "counts": {
                    "collected": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                },
            },
            1,
        ),
    ],
)
def test_orchestrator_core_does_not_count_prerequisite_unrelated_or_unsettled_leg_as_detection(
    tmp_path, chosen, expected_status
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    result, observed, _, names = core.invoke(
        prepared, outcomes={"argv": chosen, "normal": {"status": 23}}
    )
    assert result["status"] == observed["call_status"] == expected_status, result
    assert observed["cwd_restored"] and observed["environment_restored"]
    assert [leg["case_id"] for leg in observed["legs"]] == [*core.CASES, None], (
        "attempt other independent valid legs"
    )
    _, report = core_report(prepared, names)
    assert (
        report["success"] is False
        and report["first_unexpected_status"] == expected_status
    )
    assert report["legs"][1]["detected"] is False
    assert report["legs"][1]["pytest_status"] == chosen.get("status", 1)
    assert report["legs"][-1]["pytest_status"] == 23


@pytest.mark.parametrize(
    "fault",
    ["cleanup_complete", "evidence_complete", "skipped", "xfailed", "xpassed", "zero"],
)
def test_orchestrator_core_requires_normal_suite_policy_evidence_and_reconciliation(
    tmp_path, fault
):
    from tests import host_orchestration_support as core

    chosen = {fault: False}
    if fault in {"skipped", "xfailed", "xpassed", "zero"}:
        chosen = {
            "counts": {
                "collected": 0 if fault == "zero" else 2,
                "passed": 0 if fault == "zero" else 1,
                "failed": 0,
                "skipped": int(fault == "skipped"),
                "xfailed": int(fault == "xfailed"),
                "xpassed": int(fault == "xpassed"),
            }
        }
    prepared = core.prepare(tmp_path)
    result, observed, _, names = core.invoke(prepared, outcomes={"normal": chosen})
    assert result["status"] != 0 and observed["call_status"] != 0, result
    _, report = core_report(prepared, names)
    assert report["success"] is False
    assert all(leg["detected"] for leg in report["legs"][:4])
    assert report["legs"][-1]["pytest_status"] == 0, (
        "preserve raw status despite aggregate failure"
    )


@pytest.mark.parametrize(
    "status,output",
    [
        (17, None),
        (0, b"tag:latest\n"),
        (0, b""),
        (0, ("sha256:" + "a" * 64 + "\nextra\n").encode()),
    ],
)
def test_orchestrator_core_inspection_failure_blocks_build_and_every_leg(
    tmp_path, status, output
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path, inspect_status=status, inspect_output=output)
    result, observed, calls, names = core.invoke(prepared)
    assert result["status"] != 0 and not observed["legs"], result
    if status:
        assert result["status"] == status
    assert [item["mode"] for item in calls] == ["docker"]
    _, report = core_report(prepared, names)
    assert report["success"] is False


@pytest.mark.parametrize(
    "fault", ["status", "swapped_wheel", "missing_wheel", "stale_source"]
)
def test_orchestrator_core_failed_or_invalid_controlled_build_never_feeds_legs(
    tmp_path, fault
):
    from tests import host_orchestration_support as core

    options = (
        {"build_status": 29}
        if fault == "status"
        else {"swapped_wheel": True}
        if fault == "swapped_wheel"
        else {"manifest_fault": fault}
    )
    prepared = core.prepare(tmp_path, **options)
    result, observed, calls, names = core.invoke(prepared)
    assert result["status"] != 0 and observed["legs"] == [], result
    if fault == "status":
        assert result["status"] == 29
    assert sorted(item["mode"] for item in calls) == ["build", "docker"]
    _, report = core_report(prepared, names)
    assert report["success"] is False


@pytest.mark.parametrize("failure", ["interrupt", "exception"])
def test_orchestrator_core_preserves_interruption_and_attempts_report_finalization(
    tmp_path, failure
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    result, observed, _, names = core.invoke(
        prepared, outcomes={"argv": {"raise": failure}}
    )
    assert result["status"] != 0, result
    assert observed["cwd_restored"] and observed["environment_restored"]
    run, report = core_report(prepared, names)
    assert report["success"] is False
    if failure == "interrupt":
        assert observed["error"]["type"] == "KeyboardInterrupt", (
            "do not translate interruption to ordinary failure"
        )
        assert report["state"] == "incomplete"
    assert (run / "report.json").is_file()


def test_orchestrator_core_probe_driver_is_executable_without_future_implementation(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(
        tmp_path,
        fixture_source=b"#!/usr/bin/env python3.14\n_RESULTS_BASE = None\ndef main(argv):\n    return 0\n",
    )
    result, observed, calls, names = core.invoke(prepared)
    assert result["status"] == observed["call_status"] == 0, result
    assert observed["error"] is None and observed["legs"] == []
    assert observed["cwd_restored"] and observed["environment_restored"]
    assert calls == [] and names == [], (
        "inert readiness marker must perform no orchestration"
    )


@pytest.mark.parametrize(
    "call",
    [
        "os.execv(sys.executable, [sys.executable, '-c', 'raise SystemExit(99)'])",
        "os.system('exit 99')",
    ],
)
def test_orchestrator_probe_refuses_self_reexec_and_shell_before_os_execution(
    tmp_path, call
):
    from tests import host_orchestration_support as core

    source = (
        "#!/usr/bin/env python3.14\nimport os\nimport sys\n_RESULTS_BASE = None\ndef main(argv):\n    "
        + call
        + "\n"
    ).encode()
    prepared = core.prepare(tmp_path, fixture_source=source)
    result, observed, calls, names = core.invoke(prepared)
    assert result["status"] == 1, result
    assert observed["error"]["type"] == "RuntimeError"
    assert "forbidden" in observed["error"]["message"]
    assert calls == [] and names == [] and observed["legs"] == []


def test_orchestrator_prepared_leg_probe_exercises_typed_profiles_without_future_code(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path, fixture_source=core.READINESS_SOURCE.encode())
    result, observed, calls, names = core.invoke(prepared)
    assert result["status"] == observed["call_status"] == 0, result
    assert observed["error"] is None, observed
    assert observed["cwd_restored"] and observed["environment_restored"]
    assert [leg["case_id"] for leg in observed["legs"]] == ["cleanup", None]
    assert observed["legs"][0]["expectation"] == {
        "schema_version": 1,
        "case_id": "cleanup",
        "expected_override": False,
    }
    assert observed["legs"][1]["expectation"] is None
    assert calls == [] and sorted(names) == ["cleanup", "normal"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["one", "two"],
        ["--results-dir", "/unexpected"],
        ["--image", "a" * 12],
        ["--case", "argv"],
        ["--runner", "/unexpected.py"],
        [""],
    ],
)
def test_orchestrator_core_exposes_only_one_tag_not_path_or_negative_control_options(
    tmp_path, argv
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    result, observed, calls, names = core.invoke(prepared, argv=argv)
    assert result["status"] != 0 and not observed["legs"], result
    assert calls == [] and names == [], "invalid input must precede managed acquisition"


def test_orchestrator_core_missing_uv_fails_before_image_or_build_and_is_not_detection(
    tmp_path,
):
    import shutil

    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    (prepared["tools"] / "uv").unlink()
    python = shutil.which("python3.14")
    assert python is not None, "test prerequisite: PATH python3.14"
    (prepared["tools"] / "python3.14").symlink_to(python)
    result, observed, calls, _ = core.invoke(
        prepared,
        environment={"PATH": str(prepared["tools"]) + os.pathsep + "/usr/bin:/bin"},
    )
    assert result["status"] == observed["call_status"] != 0, result
    assert b"uv" in result["stderr"].lower()
    assert calls == [] and observed["legs"] == []


def test_orchestrator_core_ignores_ambient_import_and_results_override_environment(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    poison = tmp_path / "poison"
    (poison / "footman").mkdir(parents=True)
    p.write(
        poison / "footman/__init__.py",
        b'raise RuntimeError("AMBIENT_FOOTMAN_EXECUTED")\n',
    )
    unexpected = tmp_path / "unexpected-results"
    result, observed, _, names = core.invoke(
        prepared,
        environment={
            "PYTHONPATH": str(poison),
            "FOOTMAN_HOST_RESULTS_DIR": str(unexpected),
            "FOOTMAN_HOST_CASE": "cleanup",
        },
    )
    assert result["status"] == 0 and observed["call_status"] == 0, result
    assert b"AMBIENT_FOOTMAN_EXECUTED" not in result["stdout"] + result["stderr"]
    assert not unexpected.exists()
    assert [leg["case_id"] for leg in observed["legs"]] == [*core.CASES, None]
    core_report(prepared, names)


def test_orchestrator_core_rejects_preloaded_wrong_root_before_tools_or_leg_execution(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    result, observed, calls, _ = core.invoke(prepared, preload=True)
    assert result["status"] != 0 and observed["legs"] == [], result
    assert calls == [], "preloaded wrong-root foundation must fail before acquisition"
    assert (
        "root" in str(observed["error"]).lower() or b"root" in result["stderr"].lower()
    )


def test_orchestrator_core_missing_foundation_has_no_editable_fallback(tmp_path):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    (prepared["root"] / "src/footman/process.py").unlink()
    # Deliberate corruption after successful complete-profile preparation is
    # the tested preflight fault, never an incomplete fixture passed as setup.
    prepared["profile"]["source_identity"] = h.inventory(prepared["root"])[0]
    prepared["profile"]["build_input_identity"] = wheel.inventory(prepared["root"])[0]
    result, observed, calls, _ = core.invoke(prepared)
    assert result["status"] != 0 and observed["legs"] == [], result
    assert calls == [], "missing checkout foundation must fail before acquisition"
    assert (
        b"foundation" in result["stderr"].lower()
        or "foundation" in str(observed["error"]).lower()
    )


def test_orchestrator_core_wrong_root_runner_is_refused_before_exec_module(tmp_path):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    other = tmp_path / "other/scripts"
    other.mkdir(parents=True)
    target = other / "run_host_acceptance.py"
    p.write(target, b'raise RuntimeError("WRONG_ROOT_RUNNER_EXECUTED")\n')
    sibling = prepared["root"] / "scripts/run_host_acceptance.py"
    sibling.unlink()
    sibling.symlink_to(target)
    prepared["profile"]["source_identity"] = h.inventory(prepared["root"])[0]
    result, observed, calls, _ = core.invoke(prepared, substitute=False)
    assert result["status"] != 0 and observed["legs"] == [], result
    output = result["stdout"] + result["stderr"]
    assert b"WRONG_ROOT_RUNNER_EXECUTED" not in output
    assert calls == [], (
        "runner-root disagreement must precede build/install/service acquisition"
    )
    assert b"root" in output.lower() or b"unsafe" in output.lower()


def test_orchestrator_core_repeat_reserves_fresh_evidence_without_reusing_prior_report(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    first, _, _, names = core.invoke(prepared)
    assert first["status"] == 0, first
    first_run, _ = core_report(prepared, names)
    first_bytes = p.read(first_run / "report.json")
    artifacts = attribution.dist_identity(prepared["root"])
    # The synthetic build boundary refuses a repeat; the core must still reserve
    # fresh evidence and preserve the first observation, not scan/reuse dist.
    control = p.load(prepared["control"])
    control["build"]["status"] = 31
    h.replace(prepared["control"], p.canonical(control))
    second, observed, _, after = core.invoke(prepared)
    assert second["status"] == observed["call_status"] == 31, second
    assert observed["legs"] == [] and set(names) < set(after)
    assert len(after) == 2
    assert p.read(first_run / "report.json") == first_bytes
    assert attribution.dist_identity(prepared["root"]) == artifacts
    (new_name,) = set(after) - set(names)
    failed = p.load(prepared["base"] / new_name / "report.json")
    assert failed["success"] is False and failed["first_unexpected_status"] == 31


def test_orchestrator_exact_shebang_refuses_missing_path_interpreter_before_script_execution(
    tmp_path,
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    empty = tmp_path / "no-python"
    empty.mkdir()
    result = wheel.command(
        [prepared["root"] / "scripts/verify_host_acceptance.py", core.TAG],
        cwd=prepared["foreign"],
        env=wheel.clean_environment({"PATH": str(empty)}),
        timeout_seconds=30,
    )
    p.write(
        tmp_path / "missing-interpreter-operation.json",
        p.canonical(h.operation_record(result)),
    )
    assert result["status"] == 127, result
    assert b"python3.14" in result["stderr"] and result["stderr"]
    assert h.root_names(prepared["control"].parent / "calls") == []
    assert core.snapshot_candidate() == prepared["profile"]["candidate_before"]


# Task 11.3: local simulations, not host-to-container mount proof.
@pytest.fixture(autouse=True)
def _account_for_orchestrator_results(tmp_path, monkeypatch):
    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    invoke = core.invoke
    ledgers = {}
    managed = []

    def register(prepared, ledger):
        for entry in managed:
            if entry["ledger"] is ledger and ledger is not None:
                return entry
        entry = {
            "prepared": prepared,
            "ledger": ledger,
            "start": len(prepared.get("reservation_journals", [])),
            "before": core.results_base_inventory(prepared["base"])
            if ledger is None
            else None,
        }
        managed.append(entry)  # Guaranteed teardown is registered before launch.
        return entry

    def account(entry):
        prepared = entry["prepared"]
        ledger = entry["ledger"]
        if ledger is None:
            receipts = core.reservation_receipts(prepared, entry["start"])
            creations = [
                item for item in receipts if Path(item["path"]) == prepared["base"]
            ]
            if not creations:
                assert (
                    core.results_base_inventory(prepared["base"]) == entry["before"]
                ), "unresolved base creation without receipt"
                return
            assert len(creations) == 1
            ledger = results.ResultsLedger(
                prepared["base"],
                prepared["_missing_base_evidence"],
                created_base_receipt=creations[0],
            )
            entry["ledger"] = ledger
            prepared["_results_ledger"] = ledger
        if not ledger.attempted:
            core.account_reservations(prepared, ledger, entry["start"])

    def accounted(prepared, **kwargs):
        base = prepared["base"]
        if prepared.get("_missing_base_evidence") is not None:
            entry = register(prepared, prepared.get("_results_ledger"))
        elif prepared.get("_results_ledger") is not None or (
            base.is_dir() and not base.is_symlink()
        ):
            ledger = prepared.get("_results_ledger") or ledgers.get(base)
            if ledger is None:
                evidence = tmp_path / ("results-accounting-" + str(len(ledgers)))
                evidence.mkdir()
                ledger = results.ResultsLedger(base, evidence)
                ledgers[base] = ledger
            prepared["_results_ledger"] = ledger
            entry = register(prepared, ledger)
        else:
            before = core.results_base_inventory(base)
            with core.accounting_scope(
                lambda: h.check(
                    core.results_base_inventory(base) == before,
                    "unresolved refused-base resources",
                )
            ):
                return invoke(prepared, **kwargs)
        with core.accounting_scope(lambda: account(entry)):
            return invoke(prepared, **kwargs)

    monkeypatch.setattr(core, "invoke", accounted)
    yield register
    from footman.filesystem.cleanup import CleanupFailures

    failures = CleanupFailures(None)
    for entry in managed:
        if entry["ledger"] is None:
            failures.run(lambda e=entry: account(e), ordinary=(Exception,))
        if entry["ledger"] is not None:
            failures.run(
                lambda e=entry: core.reconcile_reservations(
                    e["prepared"], e["ledger"], e["start"]
                ),
                ordinary=(Exception,),
            )
    failure = failures.complete()
    if failure is not None:
        raise failure


@pytest.mark.parametrize("existing", [False, True], ids=["missing", "existing"])
def test_default_results_base_setup_readiness_reaches_fixture_orchestrator(
    tmp_path, existing
):
    import stat

    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    prepared = core.prepare(
        tmp_path,
        fixture_source=b"""_RESULTS_BASE = None
def main(argv):
    (_RESULTS_BASE / "setup-ready").mkdir()
    return 0
""",
    )
    base = prepared["base"]
    if existing:
        p.write(base / "operator-marker", b"unchanged existing base")
    else:
        with results.DirectoryDescriptor.open_secure_path(p.OPS, base.parent) as parent:
            parent.remove_child_directory(base.name)
    before = results.inventory(base) if existing else None
    created = core.prepare_default_results_base(
        base
    )  # Same call as literal-base driver.
    info = base.stat()
    assert created == (None if existing else (info.st_dev, info.st_ino))
    if not existing:
        assert stat.S_IMODE(info.st_mode) == 0o700
    value, observed, _, _ = core.invoke(prepared)
    assert value["status"] == observed["call_status"] == 0
    assert (base / "setup-ready").is_dir(), "setup must reach the fixture orchestrator"
    prepared["_results_ledger"].finish()
    assert results.inventory(base) == (before if existing else [])


@pytest.mark.parametrize("operation", [None, "ordinary", "interrupt"])
@pytest.mark.parametrize("close_target", ["parent", "child"])
def test_default_results_base_setup_owner_failure_and_repeat_matrix(
    tmp_path, monkeypatch, operation, close_target
):
    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    base = tmp_path / "absent-base"
    opening = results.DirectoryDescriptor.open_secure_path
    creating = results.DirectoryDescriptor.create_directory
    closing = p.OPS.close
    stating = os.fstat
    owners = {}
    live = {}
    released = []
    child_fd = None
    primary = (
        KeyboardInterrupt("setup interrupted")
        if operation == "interrupt"
        else OSError("setup observation failed")
    )
    close_failure = OSError("setup owner close failed")

    def watch(kind, owner):
        owners[kind] = owner
        live[owner.fd] = kind
        return owner

    def open_parent(ops, path, **kwargs):
        return watch("parent", opening(ops, path, **kwargs))

    def create(parent, name, *, mode):
        nonlocal child_fd
        owner = watch("child", creating(parent, name, mode=mode))
        child_fd = owner.fd
        return owner

    def fstat(fd):
        if operation is not None and fd == child_fd:
            raise primary
        return stating(fd)

    def close(fd):
        closing(fd)
        kind = live.pop(fd, None)
        if kind is not None:
            released.append(kind)
            if kind == close_target:
                raise close_failure

    with monkeypatch.context() as patch:
        patch.setattr(results.DirectoryDescriptor, "open_secure_path", open_parent)
        patch.setattr(results.DirectoryDescriptor, "create_directory", create)
        patch.setattr(type(p.OPS), "close", lambda self, fd: close(fd))
        patch.setattr(os, "fstat", fstat)
        with pytest.raises(BaseException) as caught:
            core.prepare_default_results_base(base)
        assert caught.value is (primary if operation else close_failure)
        assert released == ["child", "parent"]
        assert all(owner.released for owner in owners.values())
        for owner in owners.values():
            owner.close()
        assert released == ["child", "parent"], "repeat release must not close again"
    assert base.is_dir(), "setup failure does not grant a new cleanup claim"


def test_orchestrator_unpatched_default_direct_and_loaded_legs_reconcile_exact_runs(
    tmp_path,
    _account_for_orchestrator_results,
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    # Missing implementation fails here, before any real-base operation. Only
    # prepared copies ever run; Docker is the recorded CLI double throughout.
    prepared = core.prepare(tmp_path)
    direct_environment = core.isolated_environment(prepared, tmp_path)
    direct_environment.update(
        FOOTMAN_HOST_RESULTS_DIR=str(tmp_path / "redirect"),
        RESULTS_DIR=str(tmp_path / "redirect"),
    )
    base = results.DEFAULT_BASE
    reconciliation_entries = []
    made_base = core.prepare_default_results_base(base)
    try:
        for mode in ("loaded", "direct", "unknown-option"):
            evidence = tmp_path / ("default-" + mode)
            evidence.mkdir()
            ledger = results.ResultsLedger(base, evidence)
            prepared["_results_ledger"] = ledger
            prepared["base"] = base  # Driver attribution, NOT a production override.
            entry = _account_for_orchestrator_results(prepared, ledger)
            reconciliation_entries.append(entry)
            if mode == "loaded":
                value, observed, _, names = core.invoke(prepared, patch_base=False)
            else:
                if mode == "direct":
                    # Disposable sibling runner boundary only; the orchestrator
                    # source/constant stays byte-identical to the candidate.
                    runner = prepared["root"] / "scripts/run_host_acceptance.py"
                    h.replace(
                        runner,
                        b"""from types import SimpleNamespace
from pathlib import Path

def _execute_prepared_host_leg(leg):
    case = leg.case_id
    diagnostics = {"staged_bytes": "staged bytes mismatch", "argv": "configured argv mismatch", "command_status": "command status mismatch", "cleanup": "container cleanup mismatch"}
    return SimpleNamespace(pytest_status=1 if case else 0,
        stdout=(("FIXTURE_BASELINE_VERIFIED\\n" + diagnostics[case] + "\\n") if case else "normal suite passed\\n").encode(),
        stderr=b"synthetic boundary diagnostic\\x00\\xff",
        counts={"collected":1 if case else 2,"passed":0 if case else 2,"failed":1 if case else 0,"skipped":0,"xfailed":0,"xpassed":0},
        failure_kind="assertion" if case else None, cleanup_complete=True, evidence_complete=True)
""",
                    )
                    prepared["profile"]["source_identity"] = h.inventory(
                        prepared["root"]
                    )[0]
                    control = p.load(prepared["control"])
                    (
                        control["manifest"]["source_identity"],
                        control["manifest"]["source_inventory"],
                    ) = h.inventory(prepared["root"])
                    h.replace(prepared["control"], p.canonical(control))
                    # Fresh scripted build outputs; only this prepared root's
                    # exact attributed prior artifacts, never candidate dist.
                    for name in (
                        "footman-0.1.0.tar.gz",
                        "footman-0.1.0-py3-none-any.whl",
                    ):
                        (prepared["root"] / "dist" / name).unlink()
                argv = (
                    [core.TAG]
                    if mode == "direct"
                    else [core.TAG, "--results-dir", str(tmp_path / "redirect")]
                )
                calls_directory = prepared["control"].parent / "calls"
                calls_before = set(h.root_names(calls_directory))
                with core.accounting_scope(
                    lambda current=ledger, start=entry["start"]: (
                        core.account_reservations(prepared, current, start)
                    )
                ):
                    core.prepare_direct_recorder(prepared, evidence, direct_environment)
                    value = wheel.command(
                        [prepared["root"] / "scripts/verify_host_acceptance.py", *argv],
                        cwd=prepared["foreign"],
                        env=direct_environment,
                        timeout_seconds=90,
                    )
                p.write(
                    evidence / "operation.json",
                    p.canonical(
                        {
                            **h.operation_record(value),
                            "PATH": direct_environment["PATH"],
                        }
                    ),
                )
                calls = [
                    p.load(calls_directory / name)
                    for name in sorted(
                        set(h.root_names(calls_directory)) - calls_before
                    )
                ]
                p.write(evidence / "control-calls.json", p.canonical(calls))
                names = h.root_names(base)
            try:
                before_names = {entry["path"].split("/")[0] for entry in ledger.before}
                created = set(names) - before_names
                assert len(created) == (0 if mode == "unknown-option" else 1)
                for name in created:
                    run = base / name
                    report = results.read_accepted_report(run / "report.json")
                    assert report["repository_root"] == str(prepared["root"])
                    assert report["run_id"] == name
                    assert str(run).encode() in value["stdout"]
                    assert run.name in ledger.claims, (
                        "new directory/report is not a reservation receipt"
                    )
                    core.check_synthetic_leg_report(report)
                    if mode == "direct":
                        assert sorted(item["mode"] for item in calls) == [
                            "build",
                            "docker",
                        ]
                        inspect = next(
                            item for item in calls if item["mode"] == "docker"
                        )
                        build = next(item for item in calls if item["mode"] == "build")
                        assert inspect["argv"] == [
                            "image",
                            "inspect",
                            "--format",
                            "{{.Id}}",
                            core.TAG,
                        ]
                        assert inspect["resolved_docker"] == str(
                            prepared["tools"] / "docker"
                        )
                        assert build["resolved_docker"] == inspect["resolved_docker"]
                        assert (
                            len(build["argv"]) == 2 and build["argv"][0] == "--manifest"
                        )
                        manifest_path = Path(build["argv"][1])
                        assert (
                            manifest_path.is_absolute()
                            and manifest_path.is_relative_to(run)
                        )
                        assert (
                            p.load(manifest_path)["wheel_path"]
                            == p.load(prepared["control"])["manifest"]["wheel_path"]
                        )
                        assert all(item["simulation"] for item in calls)
                        assert all(
                            item["support_origin"]
                            == str(prepared["root"] / "tests/mcp_provenance.py")
                            for item in calls
                        )
                        inspects = [
                            command
                            for command in report["commands"]
                            if Path(command["argv"][0]).name == "docker"
                        ]
                        builds = [
                            command
                            for command in report["commands"]
                            if Path(command["argv"][0]).name == "build-acceptance-wheel"
                        ]
                        assert len(inspects) == len(builds) == 1
                        assert inspects[0]["argv"][1:] == inspect["argv"]
                        assert builds[0]["argv"] == [
                            str(prepared["root"] / "scripts/build-acceptance-wheel"),
                            *build["argv"],
                        ]
                        assert inspects[0]["status"] == builds[0]["status"] == 0
                        assert (
                            p.read(inspects[0]["stdout_path"])
                            == (core.IMAGE + "\n").encode()
                        )
                        assert (
                            p.read(inspects[0]["stderr_path"])
                            == b"inspection diagnostic\x00\xff\n"
                        )
                if mode != "unknown-option":
                    core.require_direct_success(value)
                else:
                    assert value["status"] != 0, value
                if mode == "unknown-option":
                    assert calls == [], (
                        "invalid options must precede Docker/build execution"
                    )
                if mode == "loaded":
                    assert observed["call_status"] == 0
                    assert [leg["case_id"] for leg in observed["legs"]] == [
                        *core.CASES,
                        None,
                    ]
                assert not (tmp_path / "redirect").exists()
                assert p.read(
                    prepared["root"] / "scripts/verify_host_acceptance.py"
                ) == p.read(h.required_surface("verify_host_acceptance.py"))
            finally:
                import sys

                from footman.filesystem.cleanup import CleanupFailures

                primary = sys.exc_info()[1]
                failures = CleanupFailures(primary)
                failures.run(
                    lambda current=ledger, start=entry["start"]: (
                        core.reconcile_reservations(prepared, current, start)
                    ),
                    ordinary=(Exception,),
                )
                selected = failures.complete()
                if selected is not None and selected is not primary:
                    raise selected
            assert core.snapshot_candidate() == prepared["profile"]["candidate_before"]
    finally:
        import sys

        from footman.filesystem.cleanup import CleanupFailures

        primary = sys.exc_info()[1]
        failures = CleanupFailures(primary)
        for pending in reconciliation_entries:
            failures.run(
                lambda e=pending: core.reconcile_reservations(
                    prepared, e["ledger"], e["start"]
                ),
                ordinary=(Exception,),
            )
        if made_base:
            # rmdir only, after reconciliation: unknown residue explicitly fails.
            def remove_base():
                with DirectoryDescriptor.open_secure_path(p.OPS, base.parent) as parent:
                    info = parent.stat_child(base.name, follow_symlinks=False)
                    assert (info.st_dev, info.st_ino) == made_base, (
                        "created results base was replaced"
                    )
                    parent.remove_child_directory(base.name)

            failures.run(remove_base, ordinary=(Exception,))
        selected = failures.complete()
        if selected is not None and selected is not primary:
            raise selected


def test_results_inventory_includes_caches_and_dangling_links_without_following(
    tmp_path,
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    (base / "__pycache__").mkdir()
    p.write(base / "__pycache__/partial", b"partial\x00\xff")
    (base / "outside").symlink_to(tmp_path / "absent", target_is_directory=True)
    observed = results.inventory(base)
    assert [entry["path"] for entry in observed] == [
        "__pycache__",
        "__pycache__/partial",
        "outside",
    ]
    assert observed[-1]["kind"] == "link"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("fault", ["collision", "publish", "readback"])
def test_orchestrator_results_reservation_and_finalization_faults_fail_closed(
    tmp_path, fault
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    sentinel = prepared["base"] / "operator-retained"
    sentinel.mkdir()
    p.write(sentinel / "report.json", b"operator retained bytes")
    setup = """
original_mkdir_for_fault = os.mkdir
original_replace = os.replace
original_read = os.read
publications = []
def candidate_path(path, fd=None):
    parent = Path(os.readlink('/proc/self/fd/' + str(fd))) if fd is not None else Path.cwd()
    return Path(path) if Path(path).is_absolute() else parent / path
def collision(path, *args, **kwargs):
    if candidate_path(path, kwargs.get('dir_fd')).parent == Path(BASE):
        raise FileExistsError('SIMULATED_RESERVATION_COLLISION')
    return original_mkdir_for_fault(path, *args, **kwargs)
def publication(src, dst, *args, **kwargs):
    target = candidate_path(dst, kwargs.get('dst_dir_fd'))
    if target.name == 'report.json' and target.is_relative_to(Path(BASE)):
        publications.append(str(target))
        if len(publications) > 1:
            raise OSError('SIMULATED_FINAL_PUBLICATION_FAILURE')
    return original_replace(src, dst, *args, **kwargs)
def denied_read(fd, count):
    path = Path(os.readlink('/proc/self/fd/' + str(fd)))
    if path.name == 'report.json' and path.is_relative_to(Path(BASE)) and path.parent.name != 'operator-retained':
        raise PermissionError('SIMULATED_REPORT_READBACK_DENIED')
    return original_read(fd, count)
"""
    setup += {
        "collision": "\nos.mkdir = collision\n",
        "publish": "\nos.replace = publication\n",
        "readback": "\nos.read = denied_read\n",
    }[fault]
    value, observed, calls, names = core.invoke(prepared, probe_setup=setup)
    assert value["status"] != 0, value
    assert p.read(sentinel / "report.json") == b"operator retained bytes"
    assert "operator-retained" in names
    if fault == "collision":
        assert observed["exclusive_reservations"] == []
        assert observed["legs"] == [] and calls == []
    else:
        assert observed["exclusive_reservations"], (
            "publication fault follows acquisition"
        )
    # A visible final report alone is never accepted over the raw unsuccessful
    # invocation, including post-publication readback/permission failures.
    assert value["stdout"] is not None and value["stderr"] is not None


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "partial",
        "schema",
        "incomplete",
        "permission",
        "missing",
        "success",
        "run-id",
        "root",
    ],
)
def test_manual_handoff_reads_only_supplied_path_and_rejects_unreadable_or_partial_local_evidence(
    tmp_path, monkeypatch, fault
):
    from tests import host_results_support as results

    # Tests the shared local reader, not delivered host inspection or mount
    # access. No separate mount-confirmation input is needed for this reader.
    supplied = tmp_path / "operator-supplied"
    newer = tmp_path / "newer-unrelated"
    supplied.mkdir()
    newer.mkdir()
    good = {
        "schema_version": 1,
        "state": "final",
        "success": True,
        "run_id": supplied.name,
        "repository_root": str(tmp_path / "candidate-supplied"),
    }
    unrelated = {
        **good,
        "run_id": newer.name,
        "repository_root": str(tmp_path / "candidate-other"),
    }
    p.write(newer / "report.json", p.canonical(unrelated))
    bad = dict(good)
    if fault == "schema":
        bad["schema_version"] = True
    elif fault == "incomplete":
        bad["state"] = "incomplete"
    elif fault == "success":
        bad["success"] = False
    elif fault == "run-id":
        bad["run_id"] = newer.name
    elif fault == "root":
        bad["repository_root"] = "relative-root"
    path = supplied / "report.json"
    if fault != "missing":
        p.write(path, b"{" if fault == "partial" else p.canonical(bad))
    os.utime(supplied, (100, 100))
    os.utime(newer, (200, 200))
    if fault == "permission":
        read = p.read

        def deny(target, **kwargs):
            if Path(target) == path:
                raise PermissionError("simulated shared-file access failure")
            return read(target, **kwargs)

        monkeypatch.setattr(p, "read", deny)

    assert results.read_accepted_report(newer / "report.json") == unrelated
    if fault is None:
        accepted = results.read_accepted_report(path)
        assert accepted == good
        assert accepted["run_id"] == supplied.name != unrelated["run_id"]
        assert (
            accepted["repository_root"]
            == good["repository_root"]
            != unrelated["repository_root"]
        )
    else:
        with pytest.raises((AssertionError, ValueError, OSError)):
            results.read_accepted_report(path)
    assert p.load(newer / "report.json") == unrelated
    assert supplied.is_dir() and newer.is_dir(), (
        "simulated operator evidence remains retained"
    )


@pytest.mark.parametrize("phase", ["valid", "invalid", "read-error", "interrupt"])
@pytest.mark.parametrize("close_fault", [False, True])
def test_shared_report_reader_preserves_operation_and_close_authority_and_releases_once(
    tmp_path, monkeypatch, phase, close_fault
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_results_support as results

    run = tmp_path / "supplied-run"
    run.mkdir()
    path = run / "report.json"
    value = {
        "schema_version": 1,
        "state": "incomplete" if phase == "invalid" else "final",
        "success": True,
        "run_id": run.name,
        "repository_root": str(tmp_path / "candidate"),
    }
    p.write(path, p.canonical(value))
    original_ops = p.OPS
    original_read = os.read
    opening = DirectoryDescriptor.open_secure_path
    descriptor = p.OwnedDescriptor
    active = False
    owners = []
    closed = []
    primary = (
        KeyboardInterrupt("reader interrupted")
        if phase == "interrupt"
        else OSError("reader failed")
    )

    class FaultOps(type(original_ops)):
        def close(self, fd):
            original_ops.close(fd)
            if active:
                failure = OSError(f"reader close failed: {fd}")
                closed.append((fd, failure))
                if close_fault:
                    raise failure

    class TrackedDescriptor(descriptor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)

    def acquire(*args, **kwargs):
        nonlocal active
        owner = opening(*args, **kwargs)
        owners.append(owner)
        active = True  # Ancestor traversal remains fault-free fixture setup.
        return owner

    def read(fd, count):
        if phase in {"read-error", "interrupt"}:
            raise primary
        return original_read(fd, count)

    with monkeypatch.context() as patch:
        patch.setattr(p, "OPS", FaultOps())
        patch.setattr(p, "OwnedDescriptor", TrackedDescriptor)
        patch.setattr(DirectoryDescriptor, "open_secure_path", acquire)
        patch.setattr(os, "read", read)
        if phase == "valid" and not close_fault:
            assert results.read_accepted_report(path) == value
        else:
            with pytest.raises(BaseException) as caught:
                results.read_accepted_report(path)
            if phase in {"read-error", "interrupt"}:
                assert caught.value is primary
            elif phase == "invalid":
                assert isinstance(caught.value, AssertionError)
                assert "not final" in str(caught.value)
            else:
                assert caught.value is closed[0][1]
        assert len(owners) == len(closed) == 2, (
            "file and directory are independent owners"
        )
        assert len({fd for fd, _ in closed}) == 2
        assert all(owner.released for owner in owners)
        for owner in owners:
            owner.close()
        assert len(closed) == 2, "repeat cleanup must not retry either close"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "empty",
        "missing-normal",
        "duplicate",
        "order",
        "status",
        "counts",
        "bool-count",
        "not-detected",
        "unsuccessful",
    ],
)
def test_synthetic_direct_report_rule_rejects_zero_or_incomplete_execution(fault):
    from tests import host_orchestration_support as core

    report = {
        "state": "final",
        "success": True,
        "first_unexpected_status": None,
        "legs": [],
    }
    for case in ("staged_bytes", "argv", "command_status", "cleanup", None):
        report["legs"].append(
            {
                "case_id": case,
                "pytest_status": 0 if case is None else 1,
                "detected": case is not None,
                "counts": {
                    "collected": 2 if case is None else 1,
                    "passed": 2 if case is None else 0,
                    "failed": 0 if case is None else 1,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                },
            }
        )
    core.check_synthetic_leg_report(report)
    if fault == "empty":
        report["legs"] = []
    elif fault == "missing-normal":
        report["legs"].pop()
    elif fault == "duplicate":
        report["legs"][-1] = report["legs"][0]
    elif fault == "order":
        report["legs"].reverse()
    elif fault == "status":
        report["legs"][0]["pytest_status"] = 0
    elif fault == "counts":
        report["legs"][-1]["counts"]["collected"] = 0
    elif fault == "bool-count":
        report["legs"][0]["counts"]["collected"] = True
    elif fault == "not-detected":
        report["legs"][0]["detected"] = False
    elif fault == "unsuccessful":
        report["success"] = False
    if fault is not None:
        with pytest.raises(AssertionError):
            core.check_synthetic_leg_report(report)


@pytest.mark.parametrize("kind", ["link", "file"])
def test_results_inventory_nested_ancestor_replacement_never_redirects_leaf_read(
    tmp_path, monkeypatch, kind
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    nested = base / "nested"
    nested.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    if kind == "link":
        (nested / "leaf").symlink_to("original-target")
        (foreign / "leaf").symlink_to("foreign-target")
    else:
        p.write(nested / "leaf", b"original bytes")
        p.write(foreign / "leaf", b"foreign bytes")
    parked = tmp_path / "parked"
    reading_link = os.readlink
    reading_file = results._read_file
    calls = []

    def replace():
        nested.rename(parked)
        nested.symlink_to(foreign, target_is_directory=True)

    def readlink(name, *, dir_fd=None):
        if name == "leaf":
            replace()
            calls.append(dir_fd)
        return reading_link(name, dir_fd=dir_fd)

    def read_file(directory, name, expected):
        replace()
        calls.append(directory.fd)
        return reading_file(directory, name, expected)

    with monkeypatch.context() as patch:
        if kind == "link":
            patch.setattr(os, "readlink", readlink)
        else:
            patch.setattr(results, "_read_file", read_file)
        observed = results.inventory(base)
    leaf = next(entry for entry in observed if entry["path"] == "nested/leaf")
    assert len(calls) == 1 and calls[0] is not None
    if kind == "link":
        assert leaf["target"] == "original-target"
    else:
        import hashlib

        assert leaf["sha256"] == hashlib.sha256(b"original bytes").hexdigest()
    assert nested.is_symlink() and parked.is_dir() and foreign.is_dir()


@pytest.mark.parametrize("contents", ["empty", "unowned", "claimed"])
def test_results_ledger_rejects_replaced_base_even_when_descendant_inventory_matches(
    tmp_path, contents
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    if contents == "unowned":
        p.write(base / "operator", b"retained")
    ledger = results.ResultsLedger(base, evidence)
    if contents == "claimed":
        (base / "owned").mkdir()
        p.write(base / "owned/output", b"retained")
        ledger.claim(base / "owned", reservation_succeeded=True)
    before_replacement = results.inventory(base)
    parked = tmp_path / "old-base"
    base.rename(parked)
    base.mkdir()
    for child in parked.iterdir():
        child.rename(base / child.name)
    assert results.inventory(base) == before_replacement
    assert (base.stat().st_dev, base.stat().st_ino) != ledger.base_identity
    assert p.load(evidence / "base-identity.json")["identity"] == list(
        ledger.base_identity
    )
    with pytest.raises(AssertionError, match="base identity changed") as caught:
        ledger.finish()
    with pytest.raises(AssertionError) as repeated:
        ledger.finish()
    assert caught.value is repeated.value
    assert results.inventory(base) == before_replacement
    assert not (evidence / "retained").exists()
    assert not (evidence / "reconciled.json").exists()


@pytest.mark.parametrize("boundary", ["claim", "cleanup-open", "final-snapshot"])
def test_results_ledger_checks_base_identity_on_subsequent_reopenings(
    tmp_path, monkeypatch, boundary
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    (base / "owned").mkdir()
    p.write(base / "owned/output", b"retained")
    if boundary != "claim":
        ledger.claim(base / "owned", reservation_succeeded=True)
    opening = results.DirectoryDescriptor.open_secure_path
    parked = tmp_path / "old-base"
    acquisitions = []

    def acquire(ops, path, **kwargs):
        if Path(path) == base:
            acquisitions.append(Path(path))
            # finish: initial snapshot, then cleanup open, then final snapshot.
            trigger = {"claim": 1, "cleanup-open": 2, "final-snapshot": 3}[boundary]
            if len(acquisitions) == trigger:
                base.rename(parked)
                base.mkdir()
                for child in parked.iterdir():
                    child.rename(base / child.name)
        return opening(ops, path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(results.DirectoryDescriptor, "open_secure_path", acquire)
        with pytest.raises(AssertionError, match="base identity changed"):
            if boundary == "claim":
                ledger.claim(base / "owned", reservation_succeeded=True)
            else:
                ledger.finish()
    assert not (evidence / "reconciled.json").exists()
    if boundary == "claim":
        assert ledger.claims == {}
        assert p.read(base / "owned/output") == b"retained"
    elif boundary == "cleanup-open":
        assert p.read(base / "owned/output") == b"retained"
        assert p.read(evidence / "retained/owned/output") == b"retained"
    else:
        assert not (base / "owned").exists()
        assert p.read(evidence / "retained/owned/output") == b"retained"


@pytest.mark.parametrize("boundary", ["inventory", "claim", "finish"])
def test_results_base_check_rejects_ancestor_symlink_back_to_original_inode(
    tmp_path, monkeypatch, boundary
):
    from footman.filesystem.descriptors import DescriptorError
    from tests import host_results_support as results

    ancestor = tmp_path / "ancestor"
    base = ancestor / "base"
    base.mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    (base / "owned").mkdir()
    p.write(base / "owned/output", b"retain original bytes")
    if boundary == "finish":
        ledger.claim(base / "owned", reservation_succeeded=True)
    moved = tmp_path / "moved-ancestor"
    opening = results.DirectoryDescriptor.open_secure_path
    owners = []

    def acquire(ops, path, **kwargs):
        owner = opening(ops, path, **kwargs)
        if Path(path) == base:
            owners.append(owner)
            ancestor.rename(moved)
            ancestor.symlink_to(moved, target_is_directory=True)
        return owner

    with monkeypatch.context() as patch:
        patch.setattr(results.DirectoryDescriptor, "open_secure_path", acquire)
        with pytest.raises((OSError, DescriptorError)) as caught:
            if boundary == "inventory":
                results.inventory(base)
            elif boundary == "claim":
                ledger.claim(base / "owned", reservation_succeeded=True)
            else:
                ledger.finish()
        if boundary == "finish":
            with pytest.raises(BaseException) as repeated:
                ledger.finish()
            assert repeated.value is caught.value
    assert len(owners) == 1 and owners[0].released
    owners[0].close()
    # Deliberately demonstrate why the old pathname lstat check missed this:
    # its leaf identity is unchanged despite the forbidden ancestor traversal.
    info = base.lstat()
    assert (info.st_dev, info.st_ino) == ledger.base_identity
    assert p.read(moved / "base/owned/output") == b"retain original bytes"
    assert not (evidence / "reconciled.json").exists()
    assert not (evidence / "retained").exists()
    if boundary == "claim":
        assert ledger.claims == {}


@pytest.mark.parametrize("operation", [None, "stat", "interrupt", "identity"])
@pytest.mark.parametrize("close_fault", [False, True])
def test_results_base_parent_check_release_matrix(
    tmp_path, monkeypatch, operation, close_fault
):
    from types import SimpleNamespace

    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    opening = results.DirectoryDescriptor.open_secure_path
    closing = p.OPS.close
    stating = results.DirectoryDescriptor.stat_child
    owners = []
    targets = set()
    closed = []
    calls = []
    primary = (
        KeyboardInterrupt("parent stat interrupted")
        if operation == "interrupt"
        else OSError("parent stat failed")
    )
    close_failure = OSError("parent close failed")

    def close(fd):
        closing(fd)
        if fd in targets:
            targets.remove(fd)
            closed.append(fd)
            if close_fault:
                raise close_failure

    def acquire(ops, path, **kwargs):
        owner = opening(ops, path, **kwargs)
        assert Path(path) == base.parent
        owners.append(owner)
        targets.add(owner.fd)
        patch.setattr(type(p.OPS), "close", lambda self, fd: close(fd))
        return owner

    def stat_child(owner, name, *, follow_symlinks=False):
        calls.append((owner, name, follow_symlinks))
        if operation in {"stat", "interrupt"}:
            raise primary
        info = stating(owner, name, follow_symlinks=follow_symlinks)
        if operation == "identity":
            return SimpleNamespace(
                st_dev=info.st_dev, st_ino=info.st_ino + 1, st_mode=info.st_mode
            )
        return info

    with opening(p.OPS, base) as base_owner:
        expected = results._identity(os.fstat(base_owner.fd))
        with monkeypatch.context() as patch:
            patch.setattr(results.DirectoryDescriptor, "open_secure_path", acquire)
            patch.setattr(results.DirectoryDescriptor, "stat_child", stat_child)
            if operation is None and not close_fault:
                results._check_base(base_owner, base, expected)
            else:
                with pytest.raises(BaseException) as caught:
                    results._check_base(base_owner, base, expected)
                if operation in {"stat", "interrupt"}:
                    assert caught.value is primary
                elif operation == "identity":
                    assert isinstance(caught.value, AssertionError)
                    assert "base identity changed" in str(caught.value)
                else:
                    assert caught.value is close_failure
            assert len(owners) == len(closed) == 1
            assert calls == [(owners[0], base.name, False)]
            assert owners[0].released
            owners[0].close()
            assert len(closed) == 1
            assert not base_owner.released
            assert results._identity(os.fstat(base_owner.fd)) == expected
    assert base_owner.released


@pytest.mark.parametrize("reader", ["inventory", "cleanup-leaf"])
@pytest.mark.parametrize("close_fault", [False, True])
@pytest.mark.parametrize("operation", [None, "read", "interrupt", "identity"])
def test_descriptor_relative_inventory_file_release_matrix(
    tmp_path, monkeypatch, close_fault, operation, reader
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    p.write(base / "leaf", b"original bytes")
    entry = results.inventory(base)[0]

    def inspect():
        if reader == "inventory":
            return results.inventory(base)[0]
        with results.DirectoryDescriptor.open_secure_path(p.OPS, base) as parent:
            results._verify_preserved_leaf(parent, "leaf", entry)
        return entry

    original_ops = p.OPS
    descriptor = results.OwnedDescriptor
    opening = results.DirectoryDescriptor.open_secure_path
    reading = os.read
    owners = []
    closed = []
    targets = set()
    primary = (
        KeyboardInterrupt("file read interrupted")
        if operation == "interrupt"
        else OSError("file read failed")
    )

    class FaultOps(type(original_ops)):
        def close(self, fd):
            original_ops.close(fd)
            if fd in targets:
                targets.remove(fd)
                error = OSError(f"inventory close failed: {fd}")
                closed.append((fd, error))
                if close_fault:
                    raise error

    class TrackedDescriptor(descriptor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)
            targets.add(self.fd)

    def acquire(ops, path, **kwargs):
        owner = opening(ops, path, **kwargs)
        if Path(path) == base:
            owners.append(owner)
            targets.add(owner.fd)
        return owner

    def read(fd, count):
        if operation in {"read", "interrupt"}:
            raise primary
        return reading(fd, count)

    checking = results._identity

    def identity(info):
        value = checking(info)
        if operation == "identity" and len(owners) == 2:
            return value[0], value[1] + 1
        return value

    with monkeypatch.context() as patch:
        patch.setattr(p, "OPS", FaultOps())
        patch.setattr(results, "OwnedDescriptor", TrackedDescriptor)
        patch.setattr(results.DirectoryDescriptor, "open_secure_path", acquire)
        patch.setattr(os, "read", read)
        patch.setattr(results, "_identity", identity)
        if operation is None and not close_fault:
            assert inspect()["kind"] == "file"
        else:
            with pytest.raises(BaseException) as caught:
                inspect()
            if operation in {"read", "interrupt"}:
                assert caught.value is primary
            elif operation == "identity":
                assert isinstance(caught.value, AssertionError)
                assert "file identity changed" in str(caught.value)
            else:
                assert caught.value is closed[0][1]
        assert len(owners) == len(closed) == 2
        assert len({fd for fd, _ in closed}) == 2
        assert all(owner.released for owner in owners)
        for owner in owners:
            owner.close()
        assert len(closed) == 2


@pytest.mark.parametrize("boundary", ["snapshot", "claim", "finish"])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("close_fault", [False, True])
def test_results_base_check_failure_preserves_primary_and_releases_once(
    tmp_path, monkeypatch, boundary, interrupted, close_fault
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    (base / "owned").mkdir()
    opening = results.DirectoryDescriptor.open_secure_path
    closing = p.OPS.close
    primary = (
        KeyboardInterrupt("base check interrupted")
        if interrupted
        else AssertionError("results base identity changed")
    )
    owners = []
    closed = []

    def close(fd):
        closing(fd)
        closed.append(fd)
        if close_fault:
            raise OSError("base check close failed")

    def acquire(*args, **kwargs):
        owner = opening(*args, **kwargs)
        owners.append(owner)
        patch.setattr(type(p.OPS), "close", lambda self, fd: close(fd))
        return owner

    def check(*args):
        raise primary

    with monkeypatch.context() as patch:
        patch.setattr(results.DirectoryDescriptor, "open_secure_path", acquire)
        patch.setattr(results, "_check_base", check)
        with pytest.raises(BaseException) as caught:
            if boundary == "snapshot":
                results.inventory(base)
            elif boundary == "claim":
                ledger.claim(base / "owned", reservation_succeeded=True)
            else:
                ledger.finish()
        assert caught.value is primary
        assert len(owners) == len(closed) == 1 and owners[0].released
        owners[0].close()
        if boundary == "finish":
            with pytest.raises(BaseException) as repeated:
                ledger.finish()
            assert repeated.value is primary
        assert len(closed) == 1
    assert ledger.claims == {} and (base / "owned").is_dir()
    assert not (evidence / "reconciled.json").exists()


@pytest.mark.parametrize(
    "case", ["empty", "claimed", "unknown", "path", "identity", "boolean"]
)
def test_created_base_receipt_binds_identity_without_implicitly_claiming_descendants(
    tmp_path, case
):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()  # Independently successful exclusive creation, before children.
    info = base.stat()
    receipt = {"path": str(base), "device": info.st_dev, "inode": info.st_ino}
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    if case in {"claimed", "unknown"}:
        (base / "run").mkdir()
        p.write(base / "run/output", b"retained")
    if case == "path":
        receipt["path"] = str(tmp_path / "other")
    elif case == "identity":
        receipt["inode"] += 1
    elif case == "boolean":
        receipt["inode"] = True
    if case in {"path", "identity", "boolean"}:
        with pytest.raises(AssertionError):
            results.ResultsLedger(base, evidence, created_base_receipt=receipt)
        assert not (evidence / "reconciled.json").exists()
        return
    ledger = results.ResultsLedger(base, evidence, created_base_receipt=receipt)
    assert ledger.before == [] and ledger.base_identity == (info.st_dev, info.st_ino)
    if case == "unknown":
        with pytest.raises(AssertionError, match="unknown or changed") as caught:
            ledger.finish()
        with pytest.raises(AssertionError) as repeated:
            ledger.finish()
        assert repeated.value is caught.value
        assert p.read(base / "run/output") == b"retained"
    else:
        if case == "claimed":
            ledger.claim(base / "run", reservation_succeeded=True)
        ledger.finish()
        ledger.finish()
        assert results.inventory(base) == []
        if case == "claimed":
            assert p.read(evidence / "retained/run/output") == b"retained"


@pytest.mark.parametrize("fault", ["interrupt", "observation", "completion"])
def test_loaded_invocation_failure_accounts_durable_reservations_without_observation(
    tmp_path, monkeypatch, fault
):
    from tests import host_orchestration_support as core

    source = b"""from pathlib import Path
_RESULTS_BASE = None
def main(argv):
    (_RESULTS_BASE / "exclusive-run").mkdir()
    return 0
"""
    prepared = core.prepare(tmp_path, fixture_source=source)
    command = wheel.command
    writing = p.write
    primary = (
        KeyboardInterrupt("after child reservation")
        if fault == "interrupt"
        else OSError("completion failed")
    )

    def execute(argv, **kwargs):
        value = command(argv, **kwargs)
        if "-c" in argv and "root = Path(ROOT)" in str(argv):
            if fault == "interrupt":
                raise primary
            if fault == "observation":
                value = {**value, "stdout": b"lost observation\n"}
        return value

    def write(path, data):
        if fault == "completion" and Path(path).name == "completion-profile.json":
            raise primary
        return writing(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(wheel, "command", execute)
        patch.setattr(p, "write", write)
        with pytest.raises(BaseException) as caught:
            core.invoke(prepared)
    if fault != "observation":
        assert caught.value is primary
    else:
        assert isinstance(caught.value, AssertionError)
        assert "complete observation" in str(caught.value)
    ledger = prepared["_results_ledger"]
    receipts = core.reservation_receipts(prepared)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert ledger.claims["exclusive-run"] == (receipt["device"], receipt["inode"])
    core.reconcile_reservations(prepared, ledger)
    core.reconcile_reservations(prepared, ledger)
    assert not (prepared["base"] / "exclusive-run").exists()
    assert p.load(ledger.evidence / "reconciled.json")["complete"] is True


@pytest.mark.parametrize("fault", ["child-status", "observation", "interrupt"])
def test_direct_execution_failure_and_parsing_are_inside_receipt_reconciliation_scope(
    tmp_path, monkeypatch, fault, _account_for_orchestrator_results
):
    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    base = tmp_path / "fixture-results"
    source = (
        f"""#!/usr/bin/env python3.14
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
assert (root / "src/footman/filesystem/descriptors.py").is_file()
sys.path.insert(0, str(root / "src"))
from footman.filesystem.descriptors import DirectoryDescriptor
from footman.filesystem.operations import PosixDescriptorOps
(Path({str(base)!r}) / "exclusive-run").mkdir()
print("not-json")
raise SystemExit({19 if fault == "child-status" else 0})
"""
    ).encode()
    prepared = core.prepare(tmp_path, fixture_source=source)
    environment = core.isolated_environment(prepared, tmp_path)
    evidence = tmp_path / "direct-failure-evidence"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    entry = _account_for_orchestrator_results(prepared, ledger)
    command = wheel.command
    primary = (
        KeyboardInterrupt("direct parent interrupted")
        if fault == "interrupt"
        else OSError("direct status failure")
    )

    def execute(*args, **kwargs):
        value = command(*args, **kwargs)
        if (
            fault == "interrupt"
            and Path(args[0][0]).name == "verify_host_acceptance.py"
        ):
            raise primary
        return value

    with monkeypatch.context() as patch:
        patch.setattr(wheel, "command", execute)
        with pytest.raises(BaseException) as caught:
            with core.accounting_scope(
                lambda: core.reconcile_reservations(prepared, ledger, entry["start"])
            ):
                with core.accounting_scope(
                    lambda: core.account_reservations(prepared, ledger, entry["start"])
                ):
                    core.prepare_direct_recorder(prepared, evidence, environment)
                    value = wheel.command(
                        [prepared["root"] / "scripts/verify_host_acceptance.py"],
                        cwd=prepared["foreign"],
                        env=environment,
                        timeout_seconds=30,
                    )
                if fault == "child-status":
                    assert value["status"] == 19
                    raise primary
                json.loads(value["stdout"])
    if fault != "observation":
        assert caught.value is primary
    else:
        assert isinstance(caught.value, ValueError)
    receipts = core.reservation_receipts(prepared, entry["start"])
    assert len(receipts) == 1 and receipts[0]["operation"] == "exclusive-mkdir-success"
    assert ledger.claims["exclusive-run"] == (
        receipts[0]["device"],
        receipts[0]["inode"],
    )
    assert not (base / "exclusive-run").exists()
    assert p.load(evidence / "reconciled.json")["complete"] is True


@pytest.mark.parametrize("validated_bootstrap", [False, True])
def test_direct_observation_does_not_supply_orchestrator_bootstrap(
    tmp_path, validated_bootstrap, _account_for_orchestrator_results
):
    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    base = tmp_path / "fixture-results"
    source = (
        f"""#!/usr/bin/env python3.14
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
assert sys.path[0] == str(root / "scripts"), sys.path
assert str(root) not in sys.path and str(root / "src") not in sys.path
assert not any(name == "footman" or name.startswith("footman.") for name in sys.modules)
assert sys.orig_argv[1] == str(Path(__file__)), sys.orig_argv
if {validated_bootstrap!r}:
    def bootstrap():
        source = root / "src"
        assert source.is_dir() and not source.is_symlink()
        for relative in ("footman/__init__.py", "footman/process.py", "footman/filesystem/descriptors.py", "footman/filesystem/operations.py"):
            path = source / relative
            assert path.is_file() and not path.is_symlink()
        sys.path.insert(0, str(source))
        from footman.filesystem.descriptors import DirectoryDescriptor
        from footman.filesystem.operations import PosixDescriptorOps
    bootstrap()
import footman.process
assert Path(footman.process.__file__).resolve() == root / "src/footman/process.py"
(Path({str(base)!r}) / "exclusive-run").mkdir()
print("own-bootstrap-established")
"""
    ).encode()
    prepared = core.prepare(tmp_path, fixture_source=source)
    environment = core.isolated_environment(prepared, tmp_path)
    evidence = tmp_path / "bootstrap-observation"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    entry = _account_for_orchestrator_results(prepared, ledger)
    with core.accounting_scope(
        lambda: core.reconcile_reservations(prepared, ledger, entry["start"])
    ):
        with core.accounting_scope(
            lambda: core.account_reservations(prepared, ledger, entry["start"])
        ):
            core.prepare_direct_recorder(prepared, evidence, environment)
            value = wheel.command(
                [prepared["root"] / "scripts/verify_host_acceptance.py"],
                cwd=prepared["foreign"],
                env=environment,
                timeout_seconds=30,
            )
        p.write(evidence / "operation.json", p.canonical(h.operation_record(value)))
        assert "PYTHONPATH" not in environment
        if validated_bootstrap:
            core.require_direct_success(value)
            assert value["stdout"] == b"own-bootstrap-established\n"
            assert len(core.reservation_receipts(prepared)) == len(ledger.claims) == 1
        else:
            assert value["status"] != 0
            assert b"ModuleNotFoundError: No module named 'footman'" in value["stderr"]
            with pytest.raises(AssertionError, match="ModuleNotFoundError"):
                core.require_direct_success(
                    value
                )  # Same acceptance check as the direct contract.
            assert core.reservation_receipts(prepared) == [] and ledger.claims == {}
    assert p.load(evidence / "reconciled.json")["complete"] is True


def test_direct_observer_leaves_early_mkdir_unknown_without_importing_foundation(
    tmp_path, monkeypatch
):
    import sys

    from tests import host_orchestration_support as core
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    journal = tmp_path / "journal"
    prepared = {"base": base}
    core.new_reservation_journal(prepared, journal)
    ledger = results.ResultsLedger(base, evidence)
    before_path = list(sys.path)
    namespace = {}
    exec(compile(core.DIRECT_RECORDER, "direct-observer.py", "exec"), namespace)
    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", os.mkdir)
        patch.delitem(sys.modules, "footman.filesystem.descriptors")
        patch.delitem(sys.modules, "footman.filesystem.operations")
        namespace["install"](base, journal)
        (base / "unknown").mkdir()
        assert "footman.filesystem.descriptors" not in sys.modules
        assert "footman.filesystem.operations" not in sys.modules
        assert sys.path == before_path
    assert core.reservation_receipts(prepared) == []
    with pytest.raises(AssertionError, match="unknown or changed"):
        core.reconcile_reservations(prepared, ledger)
    assert (base / "unknown").is_dir()


@pytest.mark.parametrize("progress", [0, 3])
def test_direct_observer_short_write_progress_or_explicit_failure(
    tmp_path, monkeypatch, progress
):
    from tests import host_orchestration_support as core

    base, evidence, ledger = _results_fixture(tmp_path)
    prepared = {"base": base}
    core.new_reservation_journal(prepared, tmp_path / "journal")
    writing = os.write
    calls = []
    namespace = {}
    exec(compile(core.DIRECT_RECORDER, "direct-observer.py", "exec"), namespace)

    def write(fd, data):
        calls.append(len(data))
        return writing(fd, data[:progress]) if progress else 0

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", os.mkdir)
        patch.setattr(os, "write", write)
        namespace["install"](base, prepared["reservation_journals"][0])
        if progress:
            (base / "owned").mkdir()
        else:
            with pytest.raises(OSError, match="made no progress"):
                (base / "owned").mkdir()
    if progress:
        assert len(calls) > 1 and all(
            a - b == progress for a, b in zip(calls, calls[1:], strict=False)
        )
        core.reconcile_reservations(prepared, ledger)
        core.reconcile_reservations(prepared, ledger)
        assert not (base / "owned").exists()
    else:
        assert len(calls) == 1
        with pytest.raises(ValueError):
            core.reconcile_reservations(prepared, ledger)
        assert (base / "owned").is_dir() and not ledger.claims
    assert (
        p.read(base / "operator-retained/report.json")
        == b"operator evidence must survive"
    )


def test_direct_observer_concurrent_exclusive_reservation_losers_have_no_receipts(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from tests import host_orchestration_support as core

    base, evidence, ledger = _results_fixture(tmp_path)
    prepared = {"base": base}
    core.new_reservation_journal(prepared, tmp_path / "journal")
    namespace = {}
    exec(compile(core.DIRECT_RECORDER, "direct-observer.py", "exec"), namespace)

    def reserve(_):
        try:
            (base / "owned").mkdir()
        except FileExistsError:
            return False
        return True

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", os.mkdir)
        namespace["install"](base, prepared["reservation_journals"][0])
        with ThreadPoolExecutor(max_workers=4) as workers:
            outcomes = list(workers.map(reserve, range(4)))
    assert outcomes.count(True) == 1 and outcomes.count(False) == 3
    assert len(core.reservation_receipts(prepared)) == 1
    core.reconcile_reservations(prepared, ledger)
    assert not (base / "owned").exists()
    assert (
        p.read(base / "operator-retained/report.json")
        == b"operator evidence must survive"
    )


@pytest.mark.parametrize("fault", ["unknown", "malformed", "replacement", "collision"])
def test_unresolved_reservations_fail_without_deleting_unknown_resources_and_do_not_retry(
    tmp_path, monkeypatch, fault
):
    from tests import host_orchestration_support as core
    from tests import host_results_support as results
    from tests.host_orchestration_tool import install_reservation_recorder

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    journal = tmp_path / "journal"
    prepared = {"base": base}
    core.new_reservation_journal(prepared, journal)
    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", os.mkdir)
        install_reservation_recorder(base, journal)
        (base / "owned").mkdir()
        if fault == "collision":
            with pytest.raises(FileExistsError):
                (base / "owned").mkdir()
    assert len(core.reservation_receipts(prepared)) == 1, (
        "reservation loser must emit no receipt"
    )
    if fault in {"unknown", "collision"}:
        (base / "unknown").mkdir()
        p.write(
            base / "unknown/report.json",
            p.canonical({"state": "final", "success": True, "run_id": "unknown"}),
        )
    elif fault == "malformed":
        p.write(journal / "000-invalid.json", b"{")
        (base / "unknown").mkdir()
    else:
        (base / "owned").rename(tmp_path / "original-owned")
        (base / "owned").mkdir()
    before = results.inventory(base)
    with pytest.raises((AssertionError, ValueError)) as caught:
        core.reconcile_reservations(prepared, ledger)
    assert ledger.attempted
    assert results.inventory(base) == before
    if fault != "replacement":
        assert "owned" in ledger.claims, (
            "a malformed sibling receipt must not hide valid acquisitions"
        )
    else:
        assert ledger.claims == {}
    with pytest.raises(BaseException) as repeated:
        core.reconcile_reservations(prepared, ledger)
    assert repeated.value is caught.value
    assert results.inventory(base) == before
    assert not (evidence / "reconciled.json").exists()


@pytest.mark.parametrize(
    "recorder", ["loaded-base", "direct-base", "direct-journal", "direct-file"]
)
@pytest.mark.parametrize("operation", [None, "write", "interrupt"])
@pytest.mark.parametrize("close_fault", [False, True])
def test_reservation_recorder_preserves_failures_and_releases_parent_exactly_once(
    tmp_path, monkeypatch, operation, close_fault, recorder
):
    from tests import host_orchestration_support as core
    from tests import host_results_support as results
    from tests.host_orchestration_tool import install_reservation_recorder

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    ledger = results.ResultsLedger(base, evidence)
    journal = tmp_path / "journal"
    prepared = {"base": base}
    core.new_reservation_journal(prepared, journal)
    opening = results.DirectoryDescriptor.open_secure_path
    constructing = results.OwnedDescriptor.__init__
    closing = p.OPS.close
    writing = os.write
    owners = []
    live = {}
    targets = set()
    closed = []
    primary = (
        KeyboardInterrupt("receipt write interrupted")
        if operation == "interrupt"
        else OSError("receipt write failed")
    )
    close_failure = OSError("recorder parent close failed")

    def watch(owner, kind):
        owners.append(owner)
        live[owner.fd] = owner
        if recorder.endswith(kind):
            targets.add(owner.fd)

    def close(fd):
        closing(fd)
        if fd in live:
            closed.append(live.pop(fd))
        if fd in targets:
            targets.remove(fd)
            if close_fault:
                raise close_failure

    def acquire(ops, path, **kwargs):
        owner = opening(ops, path, **kwargs)
        if Path(path) in (base, journal):
            watch(owner, "base" if Path(path) == base else "journal")
        return owner

    def construct(owner, ops, fd, **kwargs):
        constructing(owner, ops, fd, **kwargs)
        if kwargs.get("label") in {
            "MCP exclusive evidence",
            "direct reservation receipt",
        }:
            watch(owner, "file")

    def write(fd, data):
        if operation is not None:
            raise primary
        return writing(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", os.mkdir)
        patch.setattr(type(p.OPS), "close", lambda self, fd: close(fd))
        patch.setattr(results.DirectoryDescriptor, "open_secure_path", acquire)
        patch.setattr(results.OwnedDescriptor, "__init__", construct)
        patch.setattr(os, "write", write)
        if recorder.startswith("direct"):
            namespace = {}
            exec(compile(core.DIRECT_RECORDER, "direct-observer.py", "exec"), namespace)
            namespace["install"](base, journal)
        else:
            install_reservation_recorder(base, journal)
        if operation is None and not close_fault:
            (base / "owned").mkdir()
        else:
            with pytest.raises(BaseException) as caught:
                (base / "owned").mkdir()
            assert caught.value is (primary if operation else close_failure)
        assert len(owners) == len(closed) == 3, (
            "all independent receipt owners must release"
        )
        assert all(owner.released for owner in owners)
        assert {id(owner) for owner in closed} == {id(owner) for owner in owners}
        for owner in owners:
            owner.close()
        assert len(closed) == 3
    if operation is None:
        core.reconcile_reservations(prepared, ledger)
        assert not (base / "owned").exists()
    else:
        with pytest.raises((AssertionError, ValueError)):
            core.reconcile_reservations(prepared, ledger)
        assert ledger.attempted and ledger.claims == {}
        assert (base / "owned").is_dir()


def _results_fixture(tmp_path):
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    old = base / "operator-retained"
    old.mkdir()
    p.write(old / "report.json", b"operator evidence must survive")
    return base, evidence, results.ResultsLedger(base, evidence)


def test_results_ledger_preserves_binary_evidence_and_operator_runs_and_repeats(
    tmp_path,
):
    from tests import host_results_support as results

    base, evidence, ledger = _results_fixture(tmp_path)
    run = base / "exclusive-test-run"
    run.mkdir()
    p.write(run / "stdout", b"complete\x00\xff")
    (run / "link").symlink_to(base / "operator-retained")
    ledger.claim(run, reservation_succeeded=True)
    ledger.finish()
    assert not run.exists()
    assert (
        p.read(evidence / "retained/exclusive-test-run/stdout") == b"complete\x00\xff"
    )
    assert results.inventory(base) == ledger.before
    assert p.load(evidence / "reconciled.json")["complete"] is True
    ledger.finish()
    assert results.inventory(base) == ledger.before


@pytest.mark.parametrize("fault", ["loser", "existing", "unknown", "mutation"])
def test_results_ledger_never_adopts_collisions_or_deletes_unknown_resources(
    tmp_path, fault
):
    from tests import host_results_support as results

    base, _, ledger = _results_fixture(tmp_path)
    old = base / "operator-retained"
    if fault in {"loser", "existing"}:
        with pytest.raises(AssertionError, match="ownership|unowned"):
            ledger.claim(old, reservation_succeeded=fault == "existing")
    else:
        if fault == "unknown":
            (base / "concurrent-unattributed").mkdir()
        else:
            p.write(old / "unknown", b"concurrent mutation")
        with pytest.raises(AssertionError, match="unknown or changed") as first:
            ledger.finish()
        with pytest.raises(AssertionError) as repeated:
            ledger.finish()
        assert repeated.value is first.value
    assert old.is_dir()
    assert p.read(old / "report.json") == b"operator evidence must survive"
    assert ledger.claims == {}
    assert results.inventory(base)


@pytest.mark.parametrize(
    "failure",
    [OSError("inventory failure"), KeyboardInterrupt("inventory interrupted")],
)
def test_results_inventory_error_preserves_identity_and_releases_once(
    tmp_path, monkeypatch, failure
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    owners = []
    original = DirectoryDescriptor.list_names

    def fail(owner):
        owners.append(owner)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(DirectoryDescriptor, "list_names", fail)
        with pytest.raises(BaseException) as caught:
            results.inventory(base)
    assert caught.value is failure and len(owners) == 1
    assert owners[0].released
    owners[0].close()
    assert original is DirectoryDescriptor.list_names


@pytest.mark.parametrize(
    "primary", [None, OSError("operation"), KeyboardInterrupt("interrupted")]
)
def test_results_inventory_operation_plus_close_and_sole_close_matrix(
    tmp_path, monkeypatch, primary
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_results_support as results

    base = tmp_path / "base"
    base.mkdir()
    failure = OSError("sole directory close")
    close = p.OPS.close
    owners = []
    closed = []

    def names(owner):
        owners.append(owner)
        if primary is not None:
            raise primary
        return ()

    targets = set()

    def fail_close(fd):
        close(fd)
        if fd in targets:
            targets.remove(fd)
            closed.append(fd)
            raise failure

    # Fault only the inventory owner's release. Secure-parent identity checks
    # have independent traversal/close behavior, covered separately below.
    opening = DirectoryDescriptor.open_secure_path

    def acquire(ops, path, **kwargs):
        owner = opening(ops, path, **kwargs)
        if Path(path) == base:
            targets.add(owner.fd)
            patch.setattr(type(p.OPS), "close", lambda self, fd: fail_close(fd))
        return owner

    with monkeypatch.context() as patch:
        patch.setattr(DirectoryDescriptor, "open_secure_path", acquire)
        patch.setattr(DirectoryDescriptor, "list_names", names)
        with pytest.raises(BaseException) as caught:
            results.inventory(base)
        owners[0].close()
    assert caught.value is (primary if primary is not None else failure)
    assert len(closed) == 1 and owners[0].released


@pytest.mark.parametrize("interrupted", [False, True])
def test_results_cleanup_failure_attempts_independent_claims_without_retry(
    tmp_path, monkeypatch, interrupted
):
    from tests import host_results_support as results

    base, evidence, ledger = _results_fixture(tmp_path)
    for name in ("first", "second"):
        run = base / name
        run.mkdir()
        p.write(run / "partial", b"retained before deletion")
        ledger.claim(run, reservation_succeeded=True)
    remove = results._remove
    calls = []
    failure = (
        KeyboardInterrupt("cleanup interruption")
        if interrupted
        else OSError("cleanup denied")
    )

    def fail(parent, name, identity, preserved):
        calls.append(name)
        if name == "first":
            raise failure
        return remove(parent, name, identity, preserved)

    monkeypatch.setattr(results, "_remove", fail)
    with pytest.raises(BaseException) as caught:
        ledger.finish()
    with pytest.raises(BaseException) as repeated:
        ledger.finish()
    assert caught.value is repeated.value is failure
    assert calls == ["first", "second"]
    assert (base / "first").is_dir() and not (base / "second").exists()
    assert p.read(evidence / "retained/first/partial") == b"retained before deletion"
    assert not (evidence / "reconciled.json").exists()
    # Explicit fixture reconciliation after fault restoration; not ledger retry.
    monkeypatch.setattr(results, "_remove", remove)
    with results.DirectoryDescriptor.open_secure_path(p.OPS, base) as parent:
        remove(
            parent,
            "first",
            ledger.claims["first"],
            results._preserved_subtree(ledger.after, "first"),
        )
    assert results.inventory(base) == ledger.before


@pytest.mark.parametrize("timing", ["before-read", "after-read"])
def test_results_evidence_copy_change_cannot_authorize_cleanup(
    tmp_path, monkeypatch, timing
):
    base, evidence, ledger = _results_fixture(tmp_path)
    run = base / "owned"
    run.mkdir()
    output = run / "output"
    p.write(output, b"snapshot bytes")
    ledger.claim(run, reservation_succeeded=True)
    reading = p.read
    changed = False

    def read(path, **kwargs):
        nonlocal changed
        if Path(path) == output and ledger.after is not None and not changed:
            changed = True
            if timing == "before-read":
                h.replace(output, b"concurrent bytes")
            data = reading(path, **kwargs)
            if timing == "after-read":
                h.replace(output, b"concurrent bytes")
            return data
        return reading(path, **kwargs)

    monkeypatch.setattr(p, "read", read)
    with pytest.raises(AssertionError, match="results changed.*preservation") as caught:
        ledger.finish()
    assert changed and reading(output) == b"concurrent bytes"
    if timing == "after-read":
        assert reading(evidence / "retained/owned/output") == b"snapshot bytes"
    else:
        assert not (evidence / "retained/owned/output").exists()
    with pytest.raises(AssertionError) as repeated:
        ledger.finish()
    assert repeated.value is caught.value
    assert reading(output) == b"concurrent bytes"
    assert not (evidence / "reconciled.json").exists()


@pytest.mark.parametrize("stage", ["preserved", "preflight", "leaf"])
@pytest.mark.parametrize(
    "mutation", ["addition", "bytes", "replacement", "mode", "link-target"]
)
@pytest.mark.parametrize("close_fault", [False, True])
def test_results_cleanup_preserved_allowlist_rejects_concurrent_changes_without_retry(
    tmp_path, monkeypatch, stage, mutation, close_fault
):
    from tests import host_results_support as results

    base, evidence, ledger = _results_fixture(tmp_path)
    run = base / "owned"
    nested = run / "nested"
    nested.mkdir(parents=True)
    p.write(nested / "known-file", b"preserved original")
    (nested / "known-link").symlink_to("original-target")
    sibling = base / "sibling"
    sibling.mkdir()
    p.write(sibling / "output", b"independent preserved output")
    ledger.claim(run, reservation_succeeded=True)
    ledger.claim(sibling, reservation_succeeded=True)
    run_identity = results._identity(run.stat())
    nested_identity = results._identity(nested.stat())
    removing = results._remove
    inventory = results._inventory
    verifying = results._verify_preserved_leaf
    opening = results.DirectoryDescriptor.open_directory
    closing = p.OPS.close
    changed = False
    active = False
    observed = None
    watched_fd = None
    released = []
    owners = []
    calls = []
    close_failure = OSError("changed run close failed")

    def mutate():
        nonlocal changed, observed
        assert not changed
        changed = True
        assert (
            p.read(evidence / "retained/owned/nested/known-file")
            == b"preserved original"
        )
        if mutation == "addition":
            p.write(nested / "unknown-file", b"unpreserved bytes")
            (nested / "unknown-directory").mkdir()
            p.write(nested / "unknown-directory/payload", b"unpreserved nested bytes")
            (nested / "unknown-link").symlink_to("unpreserved-target")
        elif mutation == "bytes":
            original = (nested / "known-file").stat().st_ino
            h.replace(nested / "known-file", b"changed after preservation")
            assert (nested / "known-file").stat().st_ino == original
        elif mutation == "replacement":
            (nested / "known-file").rename(tmp_path / "parked-file")
            p.write(nested / "known-file", b"preserved original")
        elif mutation == "mode":
            (nested / "known-file").chmod(0o640)
        else:
            (nested / "known-link").unlink()
            (nested / "known-link").symlink_to("changed-target")
        observed = results.inventory(nested)

    def inspect(owner):
        value = inventory(owner)
        if (
            active
            and not changed
            and stage == "preflight"
            and results._identity(os.fstat(owner.fd)) == run_identity
        ):
            mutate()  # After successful preflight read, before removal uses it.
        return value

    def verify(owner, name, entry):
        trigger = "known-link" if mutation == "link-target" else "known-file"
        if (
            active
            and not changed
            and stage == "leaf"
            and name == trigger
            and results._identity(os.fstat(owner.fd)) == nested_identity
        ):
            mutate()  # After directory preflight/listing, before leaf verification.
        return verifying(owner, name, entry)

    def open_child(parent, name, **kwargs):
        nonlocal watched_fd
        owner = opening(parent, name, **kwargs)
        if (
            active
            and name == run.name
            and results._identity(os.fstat(owner.fd)) == run_identity
        ):
            watched_fd = owner.fd
            owners.append(owner)
        return owner

    def close(fd):
        nonlocal watched_fd
        closing(fd)
        if fd == watched_fd:
            watched_fd = None
            released.append(fd)
            if close_fault:
                raise close_failure

    def remove(parent, name, identity, preserved):
        nonlocal active
        calls.append(name)
        was_active = active
        active = True
        try:
            with monkeypatch.context() as patch:
                patch.setattr(results, "_inventory", inspect)
                patch.setattr(results, "_verify_preserved_leaf", verify)
                patch.setattr(results.DirectoryDescriptor, "open_directory", open_child)
                patch.setattr(type(p.OPS), "close", lambda self, fd: close(fd))
                if name == run.name and not changed and stage == "preserved":
                    mutate()  # Exact post-snapshot, post-evidence-copy boundary.
                return removing(parent, name, identity, preserved)
        finally:
            active = was_active

    monkeypatch.setattr(results, "_remove", remove)
    with pytest.raises(
        AssertionError, match="results.*(preservation|descendants)"
    ) as caught:
        ledger.finish()
    assert changed and ledger.attempted and ledger.failure is caught.value
    assert caught.value is not close_failure
    assert len(owners) == len(released) == 1 and owners[0].released
    assert not sibling.exists(), "independent claimed run still reconciles"
    current = results.inventory(nested)
    if mutation == "addition" and stage == "leaf":
        # Known, preserved entries may already have been removed; no newly
        # discovered resource is ever added to the deletion allowlist.
        assert [item for item in current if item["path"].startswith("unknown")] == [
            item for item in observed if item["path"].startswith("unknown")
        ]
    else:
        assert current == observed
    assert (
        p.read(evidence / "retained/owned/nested/known-file") == b"preserved original"
    )
    assert (
        p.read(evidence / "retained/sibling/output") == b"independent preserved output"
    )
    assert (
        p.read(base / "operator-retained/report.json")
        == b"operator evidence must survive"
    )
    previous_calls = list(calls)
    with pytest.raises(AssertionError) as repeated:
        ledger.finish()
    assert repeated.value is caught.value and calls == previous_calls
    owners[0].close()
    assert len(released) == 1 and results.inventory(nested) == current
    assert not (evidence / "reconciled.json").exists()


@pytest.mark.parametrize("nested", [False, True], ids=["run", "descendant"])
@pytest.mark.parametrize("close_fault", [False, True])
def test_results_removal_rejects_replacement_open_before_traversal_and_never_retries(
    tmp_path, monkeypatch, nested, close_fault
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_results_support as results

    base, evidence, ledger = _results_fixture(tmp_path)
    run = base / "owned"
    run.mkdir()
    target = run / "nested" if nested else run
    if nested:
        target.mkdir()
    p.write(target / "original", b"owned bytes")
    ledger.claim(run, reservation_succeeded=True)
    parked = tmp_path / "parked-owned"
    outside = tmp_path / "outside"
    outside.mkdir()
    p.write(outside / "sentinel", b"never follow replacement links")
    expected_parent = results._identity(target.parent.stat())
    opening = DirectoryDescriptor.open_directory
    listing = DirectoryDescriptor.list_names
    closing = p.OPS.close
    removing = results._remove
    swapped = False
    replacement_before = None
    opened = []
    releases = []
    traversals = []
    calls = []
    watched_fd = None
    close_failure = OSError("replacement descriptor close failed")

    def open_child(parent, name, **kwargs):
        nonlocal swapped, replacement_before, watched_fd
        if (
            not swapped
            and calls[-1] == target.name  # Actual removal, not read-only preflight.
            and name == target.name
            and results._identity(os.fstat(parent.fd)) == expected_parent
        ):
            # _remove has already passed stat_child. Replace just before its
            # open, returning a perfectly valid capability for an unknown inode.
            swapped = True
            target.rename(parked)
            target.mkdir()
            (target / "unknown-directory").mkdir()
            p.write(target / "unknown-directory/payload", b"unknown nested bytes")
            p.write(target / "unknown-file", b"unknown bytes")
            (target / "unknown-link").symlink_to(outside, target_is_directory=True)
            replacement_before = results.inventory(target)
            owner = opening(parent, name, **kwargs)
            opened.append(owner)
            watched_fd = owner.fd
            return owner
        return opening(parent, name, **kwargs)

    def list_names(owner):
        if any(owner is child for child in opened):
            traversals.append(owner)
        return listing(owner)

    def close(fd):
        nonlocal watched_fd
        closing(fd)
        if fd == watched_fd:
            watched_fd = None
            releases.append(fd)
            if close_fault:
                raise close_failure

    def remove(parent, name, identity, preserved):
        calls.append(name)
        with monkeypatch.context() as patch:
            patch.setattr(DirectoryDescriptor, "open_directory", open_child)
            patch.setattr(DirectoryDescriptor, "list_names", list_names)
            patch.setattr(type(p.OPS), "close", lambda self, fd: close(fd))
            return removing(parent, name, identity, preserved)

    monkeypatch.setattr(results, "_remove", remove)
    with pytest.raises(AssertionError, match="owned run identity changed") as caught:
        ledger.finish()
    assert swapped and ledger.attempted
    assert len(opened) == len(releases) == 1 and opened[0].released
    assert traversals == [], "do not even list the unclaimed opened inode"
    assert results.inventory(target) == replacement_before
    assert p.read(parked / "original") == b"owned bytes"
    assert p.read(outside / "sentinel") == b"never follow replacement links"
    assert (
        p.read(base / "operator-retained/report.json")
        == b"operator evidence must survive"
    )
    if close_fault:
        assert caught.value is not close_failure  # Identity failure stays primary.
    previous_calls = list(calls)
    with pytest.raises(AssertionError) as repeated:
        ledger.finish()
    assert repeated.value is caught.value
    assert calls == previous_calls and len(releases) == 1
    opened[0].close()
    assert len(releases) == 1
    assert results.inventory(target) == replacement_before
    assert not (evidence / "reconciled.json").exists()


@pytest.mark.parametrize("operation", [None, "ordinary", "interrupt"])
def test_results_removal_sole_close_operation_plus_close_and_repeat_matrix(
    tmp_path, monkeypatch, operation
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_results_support as results

    base, evidence, ledger = _results_fixture(tmp_path)
    run = base / "owned"
    run.mkdir()
    p.write(run / "output", b"complete original bytes")
    ledger.claim(run, reservation_succeeded=True)
    closing = DirectoryDescriptor.close
    unlinking = DirectoryDescriptor.unlink_child
    removing = results._remove
    close_failure = OSError("removal close failure")
    primary = (
        KeyboardInterrupt("removal interrupted")
        if operation == "interrupt"
        else OSError("removal denied")
        if operation
        else None
    )
    released = []

    def close(owner):
        path = (
            Path(os.readlink("/proc/self/fd/" + str(owner.fd)))
            if not owner.released
            else None
        )
        closing(owner)
        if path == run:
            released.append(owner)
            raise close_failure

    def unlink(owner, name):
        if (
            primary is not None
            and Path(os.readlink("/proc/self/fd/" + str(owner.fd))) == run
        ):
            raise primary
        return unlinking(owner, name)

    def remove(parent, name, identity, preserved):
        # Activate only after inventory/evidence preservation, at the actual
        # cleanup acquisition. Earlier reads keep their ordinary owner behavior.
        with monkeypatch.context() as patch:
            patch.setattr(DirectoryDescriptor, "close", close)
            patch.setattr(DirectoryDescriptor, "unlink_child", unlink)
            return removing(parent, name, identity, preserved)

    monkeypatch.setattr(results, "_remove", remove)
    with pytest.raises(BaseException) as caught:
        ledger.finish()
    assert caught.value is (primary if primary is not None else close_failure)
    with pytest.raises(BaseException) as repeated:
        ledger.finish()
    assert repeated.value is caught.value
    assert len(released) == 1 and released[0].released
    released[0].close()
    assert len(released) == 1
    assert p.read(evidence / "retained/owned/output") == b"complete original bytes"
    assert run.is_dir() and not (evidence / "reconciled.json").exists()
    # Deliberate fault fixture is reconciled explicitly, never by retrying the
    # failed ledger continuation or granting ownership to a collision path.
    remaining = results.inventory(base)
    assert all(entry in ledger.after for entry in remaining), "no new cleanup authority"
    with DirectoryDescriptor.open_secure_path(p.OPS, base) as parent:
        removing(
            parent,
            run.name,
            ledger.claims[run.name],
            results._preserved_subtree(remaining, run.name),
        )
    assert results.inventory(base) == ledger.before


@pytest.mark.parametrize(
    "readiness", [False, True], ids=["orchestrator-red", "probe-readiness"]
)
def test_missing_results_base_is_created_automatically_with_exclusive_receipt(
    tmp_path, readiness
):
    from footman.filesystem.descriptors import DirectoryDescriptor
    from tests import host_orchestration_support as core

    # The marker exercises only the driver/receipt/cleanup fixture, not the
    # missing production behavior. Candidate source is never rewritten.
    source = (
        b"""#!/usr/bin/env python3.14
_RESULTS_BASE = None

def main(argv):
    _RESULTS_BASE.mkdir(mode=0o700)
    return 0
"""
        if readiness
        else None
    )
    prepared = core.prepare(tmp_path, fixture_source=source)
    base = prepared["base"]
    evidence = tmp_path / "missing-base-evidence"
    evidence.mkdir()
    base.rmdir()  # Deliberate fixture precondition: parent exists, base absent.
    prepared["_missing_base_evidence"] = evidence
    p.write(
        evidence / "missing-precondition.json",
        p.canonical(core.results_base_inventory(base)),
    )
    value, observed, _, names = core.invoke(prepared)
    ledger = prepared["_results_ledger"]
    receipts = observed["exclusive_base_creations"]
    # Record successful base acquisition before checking the orchestration
    # outcome. No receipt means no authority to delete any observed new base.
    assert len(receipts) == 1, value
    receipt = receipts[0]
    assert receipt["path"] == str(base)
    info = base.lstat()
    identity = (receipt["device"], receipt["inode"])
    assert (info.st_dev, info.st_ino) == identity
    try:
        assert base.is_dir() and not base.is_symlink()
        assert value["status"] == observed["call_status"] == 0, value
        assert observed["error"] is None
        assert len(names) == (0 if readiness else 1)
        if not readiness:
            _, report = core_report(prepared, names)
            assert report["state"] == "final" and report["success"] is True
            assert [leg["case_id"] for leg in observed["legs"]] == [*core.CASES, None]
    finally:
        ledger.finish()
        # Empty-base cleanup uses the exact successful mkdir receipt as well;
        # it is neither diff-based adoption nor recursive deletion of leftovers.
        with DirectoryDescriptor.open_secure_path(p.OPS, base.parent) as parent:
            info = parent.stat_child(base.name)
            assert (info.st_dev, info.st_ino) == identity
            parent.remove_child_directory(base.name)
    assert not base.exists()
    assert p.load(evidence / "missing-precondition.json") == {"kind": "missing"}
    assert p.load(evidence / "reconciled.json")["complete"] is True


@pytest.mark.parametrize("fault", ["file", "symlink", "denied", "missing-denied"])
def test_orchestrator_results_base_failure_is_not_a_negative_detection(tmp_path, fault):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    base = prepared["base"]
    setup = ""
    if fault == "file":
        base.rmdir()
        p.write(base, b"unowned file")
    elif fault == "symlink":
        base.rmdir()
        target = tmp_path / "outside-base"
        target.mkdir()
        base.symlink_to(target, target_is_directory=True)
    else:
        if fault == "missing-denied":
            base.rmdir()
        # Deterministic EACCES even under root; this is a local simulation.
        setup = """
original_mkdir = os.mkdir
def denied(path, *args, **kwargs):
    if str(path).startswith(BASE) or (kwargs.get("dir_fd") is not None and os.readlink("/proc/self/fd/" + str(kwargs["dir_fd"])) == BASE):
        raise PermissionError("SIMULATED_RESULTS_EACCES")
    return original_mkdir(path, *args, **kwargs)
os.mkdir = denied
"""
    result, observed, calls, _ = core.invoke(prepared, probe_setup=setup)
    assert result["status"] != 0 and observed["legs"] == []
    assert calls == [], "base refusal must precede Docker/build acquisition"
    if fault == "file":
        assert p.read(base) == b"unowned file"
    elif fault == "symlink":
        assert h.root_names(target) == []
    elif fault == "missing-denied":
        assert not base.exists()
        assert observed["exclusive_base_creations"] == []
        assert observed["exclusive_reservations"] == []


def test_orchestrator_results_default_is_a_literal_not_an_external_setting():
    from tests import host_results_support as results

    source = p.read(h.required_surface("verify_host_acceptance.py"))
    tree = ast.parse(source)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_RESULTS_BASE"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    if isinstance(value, ast.Call):
        assert len(value.args) == 1 and not value.keywords
        value = value.args[0]
    assert isinstance(value, ast.Constant) and value.value == str(results.DEFAULT_BASE)
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "_RESULTS_BASE"
        and isinstance(node.ctx, ast.Store)
    ]
    assert len(writes) == 1, "CLI/env/config must not assign a second results base"


@pytest.mark.parametrize("state", ["incomplete", "final"])
def test_orchestrator_report_has_handoff_identity_counts_warnings_and_resource_state(
    tmp_path, state
):
    from tests import host_orchestration_support as core

    prepared = core.prepare(tmp_path)
    outcomes = {"argv": {"raise": "interrupt"}} if state == "incomplete" else None
    result, observed, _, names = core.invoke(prepared, outcomes=outcomes)
    run, report = core_report(prepared, names)
    assert report["state"] == state
    assert isinstance(report["run_id"], str) and report["run_id"] == run.name
    assert type(report["local_changes"]) is bool
    assert isinstance(report["warnings"], list)
    assert "resource_reconciliation" in report
    assert "source_inventory" in report and "build_input_inventory" in report
    assert report["success"] is (state == "final")
    assert (result["status"] == 0) is (state == "final")
    assert observed["cwd_restored"] and observed["environment_restored"]
    for leg in report["legs"]:
        assert type(leg["pytest_status"]) is int
        assert isinstance(leg["counts"], dict)
        assert p.read(leg["stdout_path"]) is not None
        assert p.read(leg["stderr_path"]) is not None
