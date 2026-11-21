"""Docker-free checks of the delivered host package and isolated policy."""

import shutil

import pytest

from tests import mcp_wheel_support as wheel
from tests.python_check_fixtures import REPO_ROOT


@pytest.mark.sleepy
@pytest.mark.parametrize(
    "body", ["pass", "pytest.skip('no')", "pytest.xfail('no')", "pass  # xpass"]
)
@pytest.mark.parametrize(
    "marker", ["process_time", "real_process_time", "own_process_time"]
)
def test_isolated_host_conftest_policy(tmp_path, body, marker):
    host = tmp_path / "host_tests"
    host.mkdir()
    for name in ("__init__.py", "support.py", "conftest.py"):
        shutil.copy2(REPO_ROOT / "host_tests" / name, host / name)
    support = tmp_path / "tests"
    support.mkdir()
    for name in ("__init__.py", "green_skip_policy.py"):
        shutil.copy2(REPO_ROOT / "tests" / name, support / name)
    decorator = "@pytest.mark.xfail\n" if "xpass" in body else ""
    (host / "test_probe.py").write_text(
        "import sys\nimport time\nimport pytest\n"
        + decorator
        + f"@pytest.mark.{marker}\ndef test_probe():\n"
        + "    assert 'tests.conftest' not in sys.modules\n"
        + "    assert 'tests.clock_support' not in sys.modules\n"
        + "    assert time.monotonic.__module__ == 'time'\n"
        + "    assert time.monotonic() > 0\n    "
        + body
        + "\n",
        encoding="utf-8",
    )
    ini = tmp_path / "pytest.ini"
    ini.write_text(
        "[pytest]\naddopts = --strict-config --strict-markers\n", encoding="utf-8"
    )
    result = wheel.command(
        [
            REPO_ROOT / ".venv/bin/python",
            "-m",
            "pytest",
            "-c",
            ini,
            host,
            "-n",
            "1",
            "--dist",
            "worksteal",
            "-q",
        ],
        cwd=tmp_path,
        env=wheel.clean_environment(),
        timeout_seconds=60,
    )
    assert result["status"] == (0 if body == "pass" else 1), result
    output = result["stdout"] + result["stderr"]
    if body == "pass":
        assert b"1 passed" in output, result
    else:
        assert b"BLOCKED: skips or xfail/xpass" in output, result
