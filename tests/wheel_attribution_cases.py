"""Inert attribution parity data; no acquisition, execution or later-phase imports."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AttributionCase:
    case_id: str
    accepted: bool
    attempted: bool = True
    mutation: str | None = None
    repeat: bool = False
    build_status: int = 0
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b"Successfully built dist/footman-0.1.0.tar.gz\nSuccessfully built dist/footman-0.1.0-py3-none-any.whl\n"
    phase9_profile: tuple[str, ...] = ("src/", "pyproject.toml", "README.md")
    phase10_profile: tuple[str, ...] = ("src/", "pyproject.toml", "README.md")
    phase11_profile: tuple[str, ...] = (
        "src/",
        "tests/",
        "host_tests/",
        "scripts/",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        ".git/HEAD",
    )
    sdist_bytes: bytes = b"case-owned inert sdist, never an install input"
    wheel_bytes: bytes = b"case-owned inert wheel, never an install input"


CASES = (
    AttributionCase("success", True),
    AttributionCase(
        "build-status-failure",
        False,
        build_status=17,
        stdout_bytes=b"scripted raw stdout\x00\xff\n",
        stderr_bytes=b"scripted build failure\n",
    ),
    AttributionCase(
        "ambiguous",
        False,
        stderr_bytes=b"Successfully built dist/footman-0.1.0.tar.gz\nSuccessfully built dist/footman-0.1.0-py3-none-any.whl\nSuccessfully built dist/footman-0.1.0-py3-none-any.whl\n",
    ),
    AttributionCase(
        "missing", False, stderr_bytes=b"Successfully built dist/footman-0.1.0.tar.gz\n"
    ),
    AttributionCase(
        "stale-output",
        False,
        stderr_bytes=b"Successfully built dist/footman-0.1.0.tar.gz\nSuccessfully built dist/unrelated.whl\n",
    ),
    AttributionCase("unowned-output-collision", False, attempted=False),
    AttributionCase("same-root-double-invocation", True, repeat=True),
    AttributionCase("readme-only-mutation", False, mutation="README.md"),
    AttributionCase(
        "concurrent-input-change", False, mutation="src/footman/__init__.py"
    ),
)


def case(case_id):
    matches = [item for item in CASES if item.case_id == case_id]
    if len(matches) != 1:
        raise ValueError("unknown attribution case")
    return matches[0]
