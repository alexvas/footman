"""Direct-wheel service supervision for copied host fixtures.

The pytest fixture is the service's immediate parent. It borrows no pipe or
child release authority from the SDK; the declared transport keeps one owner.
"""

import hashlib
import os
import sys
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession

from footman.filesystem.cleanup import CleanupFailures
from footman.process import StreamOutcome
from tests import mcp_provenance as provenance
from tests import mcp_transport as transport


def _support(context: Mapping[str, Any]) -> None:
    root = Path(context["support_root"])
    provenance.require(
        root.is_absolute()
        and root == root.resolve()
        and Path(__file__) == root / "host_tests/provenance.py"
        and Path(provenance.__file__) == root / "tests/mcp_provenance.py"
        and Path(transport.__file__) == root / "tests/mcp_transport.py",
        "ambient support fallback",
    )
    records = context["support_inventory"]
    expected = set(provenance.SUPPORT_FILES) | {"tests/host_acceptance_assertions.py"}
    provenance.require(
        expected <= {item["path"] for item in records},
        "missing assertion/support closure",
    )
    for item in records:
        relative = Path(item["path"])
        provenance.require(
            not relative.is_absolute() and ".." not in relative.parts,
            "unsafe support inventory path",
        )
        provenance.require(
            hashlib.sha256(provenance.read(root / relative)).hexdigest()
            == item["sha256"],
            "changed copied support: " + str(relative),
        )


@asynccontextmanager
async def verified_session(
    context: Mapping[str, Any], config: Path, observation: Any
) -> AsyncIterator[ClientSession]:
    """Retain evidence after final EOF, independently of protocol/process status.

    Context is runner-generated input outside source roots. The caller reserves
    its deadline and evidence path before service acquisition; no renewal occurs
    on failure or cleanup. Complete-set evidence is not a service result.
    """
    _support(context)
    provenance.require(
        context["venv_python"] == sys.executable, "wrong host interpreter"
    )
    origin = Path(context["expected_origin_path"])
    source = provenance.read(origin)
    manifest = provenance.load(origin)
    provenance.require(
        manifest["context"] == "wheel"
        and manifest["wheel_sha256"] == context["wheel_sha256"],
        "host wheel binding mismatch",
    )
    package, entries = provenance.manifest_set(manifest)
    provenance.require(
        package.is_relative_to(Path(sys.prefix))
        and Path(context["server_script"]) == Path(sys.prefix) / "bin/footman",
        "host package/launcher outside installed environment",
    )
    # Client imports have already happened; this is post-import attribution.
    # The child bootstrap independently repeats full-package checks pre-dispatch.
    provenance.verify(manifest)
    parser = provenance.AttestationParser(entries)
    argv = [
        context["venv_python"],
        "-P",
        str(Path(context["support_root"]) / "tests/mcp_server_child.py"),
        "--expected-origin",
        str(origin),
        "--entrypoint",
        context["server_script"],
        "--",
        "--config",
        str(config),
    ]
    remaining = context["service_deadline_monotonic"] - time.monotonic()
    provenance.require(remaining > 0, "original host service deadline exhausted")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("PYTHON", "PYTEST", "FOOTMAN_HOST_", "FOOTMAN_BOOTSTRAP_")
        )
    } | {"PYTHONNOUSERSITE": "1"}
    evidence = Path(context["provenance_evidence_path"])
    provenance.require(
        evidence.is_absolute()
        and evidence == evidence.resolve()
        and evidence.is_relative_to(Path(context["support_root"]))
        and evidence not in {origin, config},
        "unsafe provenance evidence path",
    )
    try:
        async with transport.stdio_transport(
            argv,
            cwd=context["neutral_cwd"],
            env=environment,
            timeout_seconds=remaining,
            parser=parser,
            observation=observation,
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session
    finally:
        primary = sys.exception()
        failures = CleanupFailures(primary)
        failures.run(lambda: _support(context), ordinary=(Exception,))
        failures.run(
            lambda: provenance.require(
                provenance.read(origin) == source, "origin input changed"
            ),
            ordinary=(Exception,),
        )
        failures.run(
            lambda: provenance.require(
                parser.confirmed, "host attestation incomplete: " + str(parser.failure)
            ),
            ordinary=(Exception,),
        )
        if parser.confirmed:
            failures.run(
                lambda: provenance.retain_evidence(evidence, manifest, parser, argv),
                ordinary=(Exception,),
            )

        def settled() -> None:
            owner = observation.owner
            result = observation.result
            provenance.require(
                owner is not None
                and owner.cleanup_status.complete
                and not owner.cleanup_status.failures,
                "host service cleanup incomplete or failed",
            )
            provenance.require(
                result is not None
                and result.returncode == 0
                and not result.timed_out
                and all(
                    observation.terminals.get(s) is StreamOutcome.EOF
                    for s in transport.ProcessStream
                ),
                "host service process/collection unsuccessful",
            )

        failures.run(settled, ordinary=(Exception,))
        selected = failures.complete()
        if selected is not None and selected is not primary:
            raise selected
