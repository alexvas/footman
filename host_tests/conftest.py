"""Explicit policy registration, independent of development conftest."""

import pytest

from host_tests.support import HOST_MARKERS

pytest_plugins = ("tests.green_skip_policy",)


def pytest_configure(config: pytest.Config) -> None:
    for marker in HOST_MARKERS:
        config.addinivalue_line("markers", marker)
