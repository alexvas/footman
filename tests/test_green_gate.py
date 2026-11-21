"""Permanent local green-gate contracts; negative mutations stay in fixtures.

The gate plan is a checked contract: dropping a required check, narrowing the
suite or its parallelism, masking a failing status, or leaving process cleanup
unsettled must fail the suite rather than quietly shorten the gate.  Nothing
here runs the gate itself; ``scripts/check-green`` remains the only entry point
that runs it, and ``docs/python-checks.md`` records the commanded scope.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from tests.python_check_fixtures import (
    REPO_ROOT,
    load_check_module,
    prepare_roots,
    source_digests,
    write_source,
)


def documented_groups(text):
    """Read runnable full-suite commands; documentation owns allocations."""
    return tuple(
        tuple(argv)
        for body in re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S)
        for line in body.splitlines()
        if (argv := shlex.split(line))[:4]
        == [".venv/bin/python", "-m", "pytest", "tests"]
    )


EXPECTED_COMMANDS = (
    ("scripts/check-python-lint",),
    ("scripts/check-python-format",),
    ("scripts/check-python-compile",),
    ("scripts/check-types",),
    *documented_groups((REPO_ROOT / "docs/testing.md").read_text()),
)


def gate_examples():
    return "\n".join(shlex.join(command) for command in EXPECTED_COMMANDS[4:])


ENTRY_POINTS = (
    "check-green",
    "check-python-lint",
    "check-python-format",
    "check-python-compile",
    "check-types",
)


def load_gate():
    path = REPO_ROOT / "scripts/green_checks.py"
    assert path.is_file(), "permanent green gate is missing"
    spec = importlib.util.spec_from_file_location("green_checks_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_local_plan_preserves_every_required_command():
    gate = load_gate()
    assert gate.COMMANDS == EXPECTED_COMMANDS


DOCUMENTED_GATE_PATHS = ("docs/testing.md", "docs/python-checks.md", "README.md")


def assert_documented_suite(text):
    assert documented_groups(text) == EXPECTED_COMMANDS[4:], (
        "document each full-suite group exactly once in the registered order"
    )
    # Runnable phase-suite examples may narrow scope, but remain parallel.
    # Prose describing intentionally serial policy probes is not a suite command.
    for body in re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S):
        for line in body.splitlines():
            argv = shlex.split(line)
            invocation = next(
                (index for index, arg in enumerate(argv) if Path(arg).name == "pytest"),
                None,
            )
            if invocation is None:
                continue
            args = argv[invocation + 1 :]
            if args in (["--version"], ["--help"]):
                continue
            workers = []
            distributions = []
            index = 0
            while index < len(args):
                arg = args[index]
                assert not arg.startswith("--numprocesses"), "use one -n setting"
                if arg in ("-n", "--dist"):
                    assert index + 1 < len(args), "missing parallel option value"
                    value = args[index + 1]
                    index += 1
                elif arg.startswith("-n"):
                    value = arg[2:].removeprefix("=")
                    arg = "-n"
                elif arg.startswith("--dist="):
                    value = arg.partition("=")[2]
                    arg = "--dist"
                else:
                    index += 1
                    continue
                if arg == "-n":
                    workers.append(value)
                else:
                    distributions.append(value)
                index += 1
            assert len(workers) == 1, "use exactly one parallel worker setting"
            assert workers[0] == "auto" or (
                workers[0].isascii()
                and workers[0].isdecimal()
                and 1 <= int(workers[0]) <= 1024
            )
            assert distributions == ["worksteal"], "use exactly one worksteal setting"
            assert not any("no:xdist" in arg for arg in args)


def assert_registered_suite(text, modules):
    """Check suite scope/options; the guide owns its worker allocation."""
    assert_documented_suite(text)
    prefix = [".venv/bin/python", "-m", "pytest", *modules]
    workers = []
    for body in re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S):
        for line in body.splitlines():
            argv = shlex.split(line)
            if argv[: len(prefix)] != prefix:
                continue
            options = []
            allocation = None
            args = iter(argv[len(prefix) :])
            for arg in args:
                if arg == "-n":
                    allocation = int(next(args))
                elif arg.startswith("-n"):
                    allocation = int(arg[2:].removeprefix("="))
                elif arg == "--dist":
                    options.extend((arg, next(args)))
                elif arg.startswith("--dist="):
                    options.extend(("--dist", arg.partition("=")[2]))
                else:
                    options.append(arg)
            if options == ["--dist", "worksteal", "--tb=short", "-q"]:
                assert allocation is not None
                workers.append(allocation)
    assert len(workers) == 1, f"register exactly one complete suite: {modules}"
    return workers[0]


@pytest.mark.parametrize("workers", [1, 2, 8, 16, 32])
@pytest.mark.parametrize("spelling", ["-n {}", "-n{}", "-n={}"])
def test_registered_suite_observes_documented_worker_allocation(workers, spelling):
    gate = gate_examples()
    phase = (
        ".venv/bin/python -m pytest tests/test_scaffold.py "
        f"{spelling.format(workers)} --dist=worksteal --tb=short -q"
    )
    text = f"```sh\n{gate}\n{phase}\n```\n"
    assert assert_registered_suite(text, ("tests/test_scaffold.py",)) == workers


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "extra-module", "filtered", "wrong-module", "prose"],
)
def test_registered_suite_still_requires_one_complete_runnable_scope(mutation):
    gate = gate_examples()
    phase = (
        ".venv/bin/python -m pytest tests/test_scaffold.py "
        "-n 16 --dist worksteal --tb=short -q"
    )
    prose = ""
    if mutation == "missing":
        phase = ""
    elif mutation == "duplicate":
        phase += "\n" + phase
    elif mutation == "extra-module":
        phase = phase.replace(" -n ", " tests/test_configuration.py -n ")
    elif mutation == "filtered":
        phase += " -k selected"
    elif mutation == "wrong-module":
        phase = phase.replace("test_scaffold.py", "test_configuration.py")
    elif mutation == "prose":
        prose, phase = phase, ""
    text = f"{prose}\n```sh\n{gate}\n{phase}\n```\n"
    with pytest.raises(AssertionError, match="register exactly one complete suite"):
        assert_registered_suite(text, ("tests/test_scaffold.py",))


@pytest.mark.parametrize("relative", DOCUMENTED_GATE_PATHS)
def test_documented_suite_matches_gate(relative):
    path = REPO_ROOT / relative
    assert path.is_file(), f"missing gate documentation: {relative}"
    assert_documented_suite(path.read_text())


@pytest.mark.parametrize("relative", ("docs/testing.md", "docs/python-checks.md"))
def test_instruction_referenced_testing_guides_exist(relative):
    assert (REPO_ROOT / relative).is_file()
    assert relative in (REPO_ROOT / "AGENTS.md").read_text()
    tasks = REPO_ROOT / "openspec/changes/add-privileged-test-dispatcher/tasks.md"
    # The change may eventually be archived; its instructions must remain valid.
    if tasks.is_file():
        assert relative in tasks.read_text()


def test_command_constants_match_documentation_with_worker_environment_set(monkeypatch):
    monkeypatch.setenv("GREEN_CHECKS_WORKERS", "1")
    assert load_gate().COMMANDS == EXPECTED_COMMANDS


@pytest.mark.parametrize(
    "replacement",
    (
        ".venv/bin/python -m pytest tests -q",
        ".venv/bin/python -m pytest tests -n 4 --dist worksteal --tb=short -q",
        ".venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q",
    ),
)
def test_documentation_command_drift_is_detected(replacement):
    with pytest.raises(AssertionError):
        assert_documented_suite(f"```sh\n{replacement}\n```\n")


@pytest.mark.parametrize(
    "launcher",
    (
        ".venv/bin/python -m pytest",
        "pytest",
        ".venv/bin/pytest",
        "uv run --locked pytest",
    ),
)
@pytest.mark.parametrize(
    "options",
    ("-n 8 --dist worksteal", "-n8 --dist=worksteal", "-n=8 --dist=worksteal"),
)
def test_parallel_phase_suite_is_allowed(launcher, options):
    command = gate_examples()
    phase = f"{launcher} tests/test_scaffold.py {options} --tb=short -q"
    assert_documented_suite(f"```sh\n{command}\n{phase}\n```\n")


@pytest.mark.parametrize("probe", ("pytest --version", "pytest --help"))
def test_pure_pytest_tool_probe_is_allowed(probe):
    command = gate_examples()
    assert_documented_suite(f"```sh\n{command}\n{probe}\n```\n")


@pytest.mark.parametrize(
    "launcher",
    (
        ".venv/bin/python -m pytest",
        "pytest",
        ".venv/bin/pytest",
        "uv run --locked pytest",
    ),
)
@pytest.mark.parametrize(
    "options",
    (
        "-q",
        "--version -q",
        "-n 8 --dist worksteal -n0",
        "-n 8 --dist worksteal -n=0",
        "-n8 --dist=worksteal -n=8",
        "-n 8 --dist worksteal --dist=each",
        "-n8 --dist=worksteal --dist worksteal",
        "-n0 --dist=worksteal",
        "-n=1025 --dist=worksteal",
        "-n8 --dist=each",
        "-n8 --dist=loadfile",
        "-n8 --dist=worksteal --numprocesses=0",
        "-n8 --dist=worksteal -pno:xdist",
    ),
)
def test_direct_and_attached_option_bypasses_are_rejected(launcher, options):
    command = gate_examples()
    phase = f"{launcher} tests/test_scaffold.py {options}"
    with pytest.raises(AssertionError):
        assert_documented_suite(f"```sh\n{command}\n{phase}\n```\n")


@pytest.mark.parametrize(
    "phase",
    (
        ".venv/bin/python -m pytest tests/test_scaffold.py -q",
        ".venv/bin/python -m pytest tests/test_scaffold.py -n 0 --dist worksteal",
        ".venv/bin/python -m pytest tests/test_scaffold.py -n 8 --dist each",
        ".venv/bin/python -m pytest tests/test_scaffold.py -n 8 --dist loadfile",
        ".venv/bin/python -m pytest tests/test_scaffold.py -n 8 --dist worksteal -p no:xdist",
        ".venv/bin/python -m pytest tests/test_scaffold.py -n 8 --dist worksteal --numprocesses=0",
    ),
)
def test_invalid_phase_suite_is_rejected_even_with_old_escape_hatch(phase):
    command = gate_examples()
    with pytest.raises(AssertionError):
        assert_documented_suite(
            f"```sh\n{command}\n```\n```sh non-mandated\n{phase}\n```\n"
        )


def test_duplicate_full_suite_example_is_rejected():
    command = gate_examples()
    with pytest.raises(AssertionError):
        assert_documented_suite(f"```sh\n{command}\n{command}\n```\n")


@pytest.mark.parametrize("omitted", range(len(EXPECTED_COMMANDS)))
def test_omitted_existing_command_is_detected(omitted):
    shortened = EXPECTED_COMMANDS[:omitted] + EXPECTED_COMMANDS[omitted + 1 :]
    assert shortened != load_gate().COMMANDS


@pytest.mark.parametrize("failed", range(len(EXPECTED_COMMANDS)))
def test_gate_runs_all_fast_checks_and_stops_before_pytest_on_failure(failed):
    gate = load_gate()
    checks = load_check_module()
    calls = []

    def run(argv, *, root):
        calls.append(tuple(argv))
        return checks.CommandResult(
            7 if tuple(argv) == EXPECTED_COMMANDS[failed] else 0,
            b"",
            b"",
            True,
        )

    assert gate.main(run=run) == 7
    expected = EXPECTED_COMMANDS[: max(4, failed + 1)]
    assert tuple(calls) == expected


@pytest.mark.parametrize("status", [0, 7])
def test_groups_preserve_full_scope_and_failure_status(status, capsys):
    gate = load_gate()
    checks = load_check_module()
    calls = []

    def run(argv, *, root):
        calls.append(tuple(argv))
        # A successful fast block must still run the complete configured suite,
        # preserving its status independently of the worker-count spelling.
        code = status if tuple(argv)[0] == ".venv/bin/python" else 0
        return checks.CommandResult(code, b"", b"", True)

    assert gate.main(run=run) == status
    assert tuple(calls) == (EXPECTED_COMMANDS if status == 0 else EXPECTED_COMMANDS[:5])
    assert gate.COMMANDS == EXPECTED_COMMANDS
    output = capsys.readouterr().out
    records = [
        json.loads(line.removeprefix("G-record "))
        for line in output.splitlines()
        if line.startswith("G-record ")
    ]
    assert len(records) == 2 * len(calls)
    for index, argv in enumerate(calls):
        start, end = records[2 * index : 2 * index + 2]
        assert start["event"] == "start" and end["event"] == "end"
        assert shlex.split(start["command"]) == list(argv)
        assert end["command"] == start["command"]
        assert end["group"] == (index - 3 if index >= 4 else None)
        assert end["raw_status"] == (status if index >= 4 else 0)
        assert end["clean"] is True and end["lifecycle_failures"] == []


def test_gate_does_not_swallow_interruption():
    gate = load_gate()
    interruption = KeyboardInterrupt("injected")

    def interrupt(*args, **kwargs):
        raise interruption

    with pytest.raises(KeyboardInterrupt) as caught:
        gate.main(run=interrupt)
    assert caught.value is interruption


def test_unsettled_cleanup_is_not_green():
    gate = load_gate()
    checks = load_check_module()
    assert (
        gate.main(
            run=lambda *a, **kw: checks.CommandResult(
                0, b"", b"", False, (OSError("close failed"),)
            )
        )
        != 0
    )


@pytest.mark.parametrize("failed", range(4))
@pytest.mark.parametrize(
    ("code", "clean", "expected"),
    [(7, True, 7), (-15, True, 143), (None, True, 1), (0, False, 1)],
)
def test_fast_failure_keeps_first_status_and_never_runs_slow_checks(
    failed, code, clean, expected, capsys
):
    gate = load_gate()
    checks = load_check_module()
    calls = []

    def run(argv, *, root):
        index = len(calls)
        calls.append(tuple(argv))
        if index == failed:
            return checks.CommandResult(
                code, b"", b"", clean, () if clean else (OSError("cleanup failed"),)
            )
        # Every later fast check fails too: none can replace the first status.
        return checks.CommandResult(9 if index > failed else 0, b"", b"", True)

    assert gate.main(run=run) == expected
    assert tuple(calls) == EXPECTED_COMMANDS[:4]
    diagnostic = capsys.readouterr().err
    assert "Fast checks failed; pytest was not run." in diagnostic
    if not clean:
        assert "check lifecycle failure: cleanup failed" in diagnostic


def test_fast_interruption_is_not_hidden_by_an_earlier_failure():
    gate = load_gate()
    checks = load_check_module()
    calls = []
    interruption = KeyboardInterrupt("fast block interrupted")

    def run(argv, *, root):
        calls.append(tuple(argv))
        if len(calls) == 2:
            raise interruption
        return checks.CommandResult(7, b"", b"", True)

    with pytest.raises(KeyboardInterrupt) as caught:
        gate.main(run=run)
    assert caught.value is interruption
    assert tuple(calls) == EXPECTED_COMMANDS[:2]


def test_entry_point_rejects_narrowing_and_mutation_arguments():
    result = load_check_module().run_captured(
        [str(REPO_ROOT / "scripts/check-green"), "--fix"], root=REPO_ROOT
    )
    assert result.clean
    assert result.returncode == 1
    assert b"does not support narrowing or mutation" in result.stderr


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_entry_point_without_the_project_tools_fails_closed(entry_point, tmp_path):
    """A missing locked tool exits 127 instead of checking with another tool."""
    script = tmp_path / "scripts" / entry_point
    script.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / entry_point, script)

    result = load_check_module().run_captured([str(script)], root=tmp_path)

    assert result.clean
    assert result.returncode == 127, result.stdout + result.stderr
    assert b".venv/bin" in result.stderr


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_check_entry_points_are_never_mutating(entry_point):
    text = (REPO_ROOT / "scripts" / entry_point).read_text(encoding="utf-8")
    for forbidden in ("--fix", "--unsafe-fixes", "format .", "continue-on-error"):
        assert forbidden not in text, f"{entry_point}: {forbidden}"
    assert "git commit" not in text and "git push" not in text


def test_type_check_target_matches_the_declared_policy():
    """The gate may not narrow the typed tree below the declared include list."""
    text = (REPO_ROOT / "scripts/check-types").read_text(encoding="utf-8")
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = project["tool"]["ty"]["src"]["include"]
    assert include == ["src", "host_tests"]
    assert text.rstrip().endswith("check --output-format concise " + " ".join(include))


@pytest.mark.parametrize(
    ("mode", "source"),
    [
        ("lint", "print(undefined_name)\n"),
        ("format", "value=  [1,2]\n"),
        ("compile", "def broken(:\n"),
    ],
)
def test_isolated_source_failures_are_nonzero_and_readonly(tmp_path, mode, source):
    checks = load_check_module()
    prepare_roots(tmp_path)
    write_source(tmp_path, "src/broken.py", source)
    before = source_digests(tmp_path)
    assert checks.main([mode, "--root", str(tmp_path)]) == 1
    assert source_digests(tmp_path) == before


def test_isolated_type_failure_is_nonzero(tmp_path):
    checks = load_check_module()
    path = write_source(tmp_path, "broken.py", 'value: int = "not an integer"\n')
    result = checks.run_captured(
        [str(REPO_ROOT / ".venv/bin/python"), "-m", "ty", "check", str(path)],
        root=REPO_ROOT,
    )
    assert result.clean
    assert result.returncode == 1, result.stderr + result.stdout
    assert b"invalid-assignment" in result.stdout + result.stderr


@pytest.mark.parametrize("relative", ("docs/testing.md", "docs/python-checks.md"))
def test_formatting_is_documented_as_a_separate_pre_gate_step(relative):
    from tests import mcp_provenance as p

    text = " ".join(p.read(REPO_ROOT / relative).decode().split())
    assert ".venv/bin/ruff format src tests host_tests scripts" in text
    assert "extensionless Python sources explicitly" in text
    assert "read-only" in text
    assert "before pytest" in text


def test_agent_rules_require_the_fast_block_before_pytest():
    from tests import mcp_provenance as p

    text = " ".join(p.read(REPO_ROOT / "AGENTS.md").decode().split())
    assert "before G" in text and "separate write step" in text
    assert "Attempt all four fast checks even after a failure" in text
    assert "do not launch pytest unless the entire fast block passes" in text


@pytest.mark.parametrize("status", [0, 7])
def test_documented_capture_keeps_complete_output_and_original_status(tmp_path, status):
    """The logging recipe in docs/python-checks.md keeps the real gate status."""
    log = tmp_path / "gate.log"
    probe = "bash -c 'echo diagnostic-stdout; echo diagnostic-stderr >&2; exit 7'"
    if status == 0:
        probe = probe.replace("exit 7", "exit 0")
    block = (
        f'log="{log}"\n'
        "set +e\n"
        f'{probe} >"$log" 2>&1\n'
        "status=$?\n"
        "set -e\n"
        'cat "$log"\n'
        'exit "$status"\n'
    )
    result = load_check_module().run_captured(["bash", "-c", block], root=tmp_path)

    assert result.clean
    assert result.returncode == status
    expected = b"diagnostic-stdout\ndiagnostic-stderr\n"
    assert result.stdout == expected
    assert Path(log).read_bytes() == expected
