"""Shared static import boundary for every filesystem foundation module."""

import ast
import sys

FOUNDATION_MODULES = ("cleanup.py", "streams.py", "operations.py", "descriptors.py")
FORBIDDEN_DOMAIN_MODULES = ("footman.process", "footman.startup", "footman.__main__")


def assert_foundation_imports(source, filename):
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                raise AssertionError(f"{filename}: relative import bypasses boundary")
            names = [node.module or ""]
        else:
            continue
        for name in names:
            allowed = (
                name.startswith("footman.filesystem.")
                or name.split(".")[0] in sys.stdlib_module_names
            )
            if not allowed:
                raise AssertionError(f"{filename}: non-foundation import {name!r}")
