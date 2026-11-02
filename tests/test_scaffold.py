"""Installation/entry-point checks only, not MCP acceptance tests."""

import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import pytest


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "footman", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_package_is_importable() -> None:
    import footman

    assert footman.__doc__


def test_service_entry_point() -> None:
    scripts = {
        entry.name: entry.value
        for entry in distribution("footman").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts == {"footman": "footman.startup:main"}
    result = subprocess.run(
        [str(Path(sys.executable).with_name("footman")), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "usage: footman " in result.stdout
    assert result.stderr == ""


def test_help() -> None:
    result = invoke("--help")
    assert result.returncode == 0
    assert "--config" in result.stdout
    assert result.stderr == ""


def test_missing_configuration_file_refuses_startup(tmp_path: Path) -> None:
    missing = tmp_path / "absent-configuration.toml"
    assert not missing.exists()
    result = invoke("--config", str(missing))
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr
    assert "configuration" in result.stderr.lower()
    assert "load" in result.stderr.lower()
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("subcommand", ["run", "mcp"])
def test_no_subcommands(subcommand: str) -> None:
    result = invoke(subcommand, "--config", "unused.toml")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "unrecognized arguments" in result.stderr


def test_config_is_required() -> None:
    result = invoke()
    assert result.returncode == 2
    assert result.stdout == ""
    assert "required" in result.stderr
