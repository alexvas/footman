"""Permanent local green-gate contracts; negative mutations stay in fixtures.

The gate plan is a checked contract: dropping a required check, narrowing the
suite or its parallelism, masking a failing status, or leaving process cleanup
unsettled must fail the suite rather than quietly shorten the gate.  Nothing
here runs the gate itself; ``scripts/check-green`` remains the only entry point
that runs it, and ``docs/python-checks.md`` records the commanded scope.
"""

from __future__ import annotations

import importlib.util
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

EXPECTED_COMMANDS = (
    ("scripts/check-python-lint",),
    ("scripts/check-python-format",),
    ("scripts/check-python-compile",),
    ("scripts/check-types",),
    (
        ".venv/bin/python",
        "-m",
        "pytest",
        "tests",
        "-n",
        "8",
        "--dist",
        "loadfile",
        "--tb=short",
        "-q",
    ),
)
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


@pytest.mark.parametrize("omitted", range(len(EXPECTED_COMMANDS)))
def test_omitted_existing_command_is_detected(omitted):
    shortened = EXPECTED_COMMANDS[:omitted] + EXPECTED_COMMANDS[omitted + 1 :]
    assert shortened != load_gate().COMMANDS


@pytest.mark.parametrize("failed", range(len(EXPECTED_COMMANDS)))
def test_gate_preserves_failure_and_runs_independent_checks(failed):
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
    assert tuple(calls) == EXPECTED_COMMANDS


@pytest.mark.parametrize("workers", ["1", "2", "3", "8", "16", "1024", "0002"])
@pytest.mark.parametrize("status", [0, 7])
def test_worker_counts_preserve_full_scope_and_failure_status(workers, status):
    gate = load_gate()
    checks = load_check_module()
    calls = []

    def run(argv, *, root):
        calls.append(tuple(argv))
        return checks.CommandResult(status, b"", b"", True)

    assert gate.main(run=run, workers=workers) == status
    expected = list(EXPECTED_COMMANDS[-1])
    expected[expected.index("-n") + 1] = str(int(workers))
    assert tuple(calls) == (*EXPECTED_COMMANDS[:-1], tuple(expected))
    assert gate.COMMANDS == EXPECTED_COMMANDS


@pytest.mark.parametrize(
    "workers",
    ["", "0", "-1", "1025", "999999999999999999999", "1.5", "auto", "2 --ignore=tests"],
)
def test_invalid_worker_counts_fail_before_running_checks(workers, capsys):
    def unexpected_run(*args, **kwargs):
        pytest.fail("invalid worker setting must not execute checks")

    assert load_gate().main(run=unexpected_run, workers=workers) == 2
    assert "must be an integer from 1 to 1024" in capsys.readouterr().err


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
