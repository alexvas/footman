"""Exercise the green gate in isolated pytest subprocesses, not this session.

Each probe runs under both the serial invocation and the mandated parallel
invocation, so a skip or expected failure cannot survive the parallel runner.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PARALLEL_ARGS = ("-p", "xdist", "-n", "2", "--dist", "loadfile")


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def test_ok(): pass", 0),
        ("def test_bad(): assert False", 1),
        ("import pytest\ndef test_skip(): pytest.skip('probe')", 1),
        # With no collected tests pytest already exits 5; preserve that failure.
        ("import pytest\npytest.skip('collection probe', allow_module_level=True)", 5),
        (
            "import pytest\n@pytest.fixture\ndef fixture(): pytest.skip('setup probe')\ndef test_setup(fixture): pass",
            1,
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='probe')\ndef test_xfail(): assert False",
            1,
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='probe')\ndef test_xpass(): pass",
            1,
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='probe', strict=True)\ndef test_xpass(): pass",
            1,
        ),
        (
            "import pytest\n@pytest.fixture\ndef fixture():\n    yield\n    pytest.skip('teardown probe')\ndef test_teardown(fixture): pass",
            1,
        ),
        ("def test_interrupt(): raise KeyboardInterrupt()", 2),
    ],
)
@pytest.mark.parametrize("runner", [(), PARALLEL_ARGS], ids=("serial", "parallel"))
def test_gate(tmp_path, source, expected, runner):
    (tmp_path / "test_probe.py").write_text(source + "\n")
    if "collection probe" in source:
        # Also verify a collection skip cannot hide behind a passing test.
        (tmp_path / "test_passing.py").write_text("def test_ok(): pass\n")
        expected = 1
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.green_skip_policy",
            *runner,
            "-q",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if "pytest.skip" in source or "mark.xfail" in source:
        assert "BLOCKED: skips or xfail/xpass" in result.stdout


# A missing ``xdist`` makes every parallel probe exit 4 instead of the expected
# status, so the parametrized parallel cases above also pin the required plugin.
