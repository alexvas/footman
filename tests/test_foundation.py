"""Docker-free foundation contracts; controls mutate only disposable inputs."""

import os
import sys
from pathlib import Path

import pytest

from tests.dispatcher_harness import GitFixture, run, write_bytes

CONTROL = os.environ.get("FOOTMAN_FOUNDATION_CONTROL")
ROOT = Path(__file__).resolve().parents[1]


def test_exact_wheel_installation(tmp_path):
    run(["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")], cwd=ROOT)
    wheels = tuple((tmp_path / "dist").glob("*.whl"))
    assert len(wheels) == 1
    environment = tmp_path / "installed"
    run(["uv", "venv", "--python", sys.executable, str(environment)], cwd=tmp_path)
    python = environment / "bin/python"
    run(["uv", "pip", "install", "--python", str(python), str(wheels[0])], cwd=tmp_path)
    modules = [
        "footman.filesystem.cleanup",
        "footman.filesystem.streams",
        "footman.filesystem.operations",
        "footman.filesystem.descriptors",
        "footman.process",
        "mcp",
    ]
    if CONTROL == "missing-module":
        modules.append("footman.missing_foundation_module")
    code = (
        "import importlib,pathlib; "
        f"modules=[importlib.import_module(n) for n in {modules!r}]; "
        f"assert all(not pathlib.Path(m.__file__).is_relative_to({str(ROOT)!r}) for m in modules)"
    )
    run([str(python), "-c", code], cwd=tmp_path)
    executable = environment / "bin/footman"
    if CONTROL == "missing-executable":
        executable = environment / "bin/missing-footman"
    assert executable.is_file(), "installed console executable is missing"
    for argv in ([str(executable), "--help"], [str(python), "-m", "footman", "--help"]):
        result = run(argv, cwd=tmp_path)
        assert b"--config" in result.stdout
        assert result.stderr == b""


def test_git_fixture_isolation(tmp_path):
    fixture = GitFixture.create(tmp_path / "repository")
    before = fixture.identity()
    if CONTROL == "mutate-fixture":
        write_bytes(fixture.path / "payload", b"intentional negative-control mutation")
    fixture.assert_unchanged(before)
    assert fixture.path.is_relative_to(tmp_path)
    assert not fixture.path.is_relative_to(ROOT)


def test_stub_contract(tmp_path):
    from tests.dispatcher_harness import DockerStub

    stub = DockerStub.create(tmp_path)
    result = stub.invoke(
        ["exec", "a" * 64, "literal ; $x", "two words"],
        stdout=b"out\x00",
        stderr=b"warning\xff",
        status=7,
    )
    assert result.returncode == 7
    assert result.stdout == b"out\x00"
    assert result.stderr == b"warning\xff"
    assert stub.calls() == [
        {
            "argv": ["exec", "a" * 64, "literal ; $x", "two words"],
            "container_id": "a" * 64,
        }
    ]


@pytest.mark.parametrize("status", [0, 17])
def test_stub_delay_and_status(tmp_path, status):
    from tests.dispatcher_harness import DockerStub

    stub = DockerStub.create(tmp_path)
    result = stub.invoke(["inspect", "b" * 64], delay=0.01, status=status)
    assert result.returncode == status
    assert len(stub.calls()) == 1
