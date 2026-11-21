"""Complete delivered-profile driver for task 11.10.1 (simulation only)."""

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

from tests import host_acceptance_contract_support as h
from tests import host_orchestration_support as core
from tests import mcp_attribution_support as attribution
from tests import mcp_contract_support as service
from tests import mcp_provenance as p
from tests import mcp_wheel_support as wheel


def prepare(tmp, *, docker_fault=None):
    candidate = core.snapshot_candidate()
    root = service.prepare_inputs(h.ROOT, tmp / "prepared")
    for name in ("tests", "host_tests", "scripts"):
        service.copy_tree(h.ROOT / name, root / name)
    p.write(root / "uv.lock", p.read(h.ROOT / "uv.lock"))
    revision = h.synthetic_head(root, tmp)
    tools = tmp / "tools"
    tools.mkdir()
    state = tmp / "docker-simulation"
    state.mkdir()
    program = (
        f"import sys; sys.path[:0] = {[str(root), str(root / 'src')]!r}; "
        "from pathlib import Path; "
        "from tests.host_integration_docker import main; "
        f"fault = {docker_fault!r}; "
        f"normal = Path({str(tmp / 'controls/normal-prerequisite')!r}).exists(); "
        f"base = Path({str(tmp / 'results')!r}); "
        "relative = Path.cwd().relative_to(base) if Path.cwd().is_relative_to(base) else Path(); "
        "cleanup = len(relative.parts) > 1 and relative.parts[1] == 'cleanup'; "
        "refuse = fault == 'prerequisite' and normal and sys.argv[1:] == ['--version']; "
        "sys.stderr.write('injected Docker CLI failure\\n' if refuse else ''); "
        f"raise SystemExit(23 if refuse else main({str(state)!r}, sys.argv[1:], observed_uid=1 if fault == 'unrelated' and cleanup else 0))"
    )
    p.write(
        tools / "docker",
        (
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " -P -c "
            + shlex.quote(program)
            + ' "$@"\n'
        ).encode(),
    )
    (tools / "docker").chmod(0o700)
    foreign = tmp / "foreign-cwd"
    foreign.mkdir()
    base = tmp / "results"
    base.mkdir()
    controls = tmp / "controls"
    controls.mkdir()
    (controls / "calls").mkdir()
    diagnostics = tmp / "diagnostics"
    diagnostics.mkdir()
    source, inventory = h.inventory(root)
    build, build_inventory = wheel.inventory(root)
    profile = {
        "prepared_root": str(root),
        "source_identity": source,
        "source_inventory": inventory,
        "build_input_identity": build,
        "build_input_inventory": build_inventory,
        "candidate_revision": candidate["repository_state"]["revision"],
        "synthetic_revision": revision,
        "candidate_before": candidate,
        "dist_before": attribution.dist_identity(root),
        "simulation": True,
        "docker_executable_sha256": hashlib.sha256(
            p.read(tools / "docker")
        ).hexdigest(),
    }
    p.write(tmp / "prepared-profile.json", p.canonical(profile))
    h.check(source == h.inventory(h.ROOT)[0], "incomplete delivered source profile")
    h.check(build == wheel.inventory(h.ROOT)[0], "prepared build inputs differ")
    h.check(core.snapshot_candidate() == candidate, "preparation changed candidate")
    return {
        "root": root,
        "tmp": tmp,
        "foreign": foreign,
        "base": base,
        "tools": tools,
        "control": controls / "unused.json",
        "profile": profile,
        "diagnostics": diagnostics,
        "invocations": [],
        "state": state,
    }


def environment(prepared):
    return {"PATH": str(prepared["tools"]) + os.pathsep + os.environ["PATH"]}


def require_quiescence(prepared):
    """Read-only barrier, not ownership of a descendant or a new wait budget.

    A complete outer observation is necessary, never sufficient. For every
    acquired real flow, require actual controller teardown observations before
    any ledger preservation/removal. Lost completion leaves all paths retained.
    """
    observed = prepared.get("integration_observed")
    h.check(
        type(observed) is dict,
        "unsettled integration: outer completion unavailable; retain run paths",
    )
    for receipt in core.reservation_receipts(prepared):
        run = Path(receipt["path"])
        if run == prepared["base"]:
            continue
        for profile in (*core.CASES, "normal"):
            workspace = run / profile / "workspace"
            if not os.path.lexists(workspace):
                continue
            flows = [
                name
                for name in h.root_names(workspace)
                if name.startswith("host-runs-")
            ]
            if not flows:
                continue  # No HostFlow acquisition, not a fabricated teardown.
            h.check(
                all(re.fullmatch(r"host-runs-[0-9a-f]{32}", name) for name in flows),
                "invalid flow root",
            )
            for name in flows:
                h.root_names(workspace / name)  # Secure no-follow capability.
            report = p.load(run / profile / "evidence/report.json")
            commands = [row for row in report["commands"] if "pytest" in row["argv"]]
            h.check(
                len(commands) == 1,
                "unsettled integration: pytest observation unavailable",
            )
            command = commands[0]
            output = Path(command["stdout_path"])
            h.check(
                output.is_relative_to(run / profile / "evidence"),
                "unbound pytest output",
            )
            records = [
                line.removeprefix(b"FIXTURE_OUTCOMES=")
                for line in p.read(output).splitlines()
                if line.startswith(b"FIXTURE_OUTCOMES=")
            ]
            h.check(
                len(records) == 1,
                "unsettled integration: missing fixture teardown outcomes",
            )
            value = json.loads(records[0], object_pairs_hook=p.unique)
            teardown = [row for row in value["reports"] if row["when"] == "teardown"]
            h.check(
                type(value["schema_version"]) is int
                and value["schema_version"] == 1
                and type(value["collected"]) is int
                and value["collected"] > 0
                and value["exitstatus"] == command["status"]
                and value["cleanup_complete"] is True
                and not value["interrupted"]
                and not value["internal_errors"]
                and not value["collection_errors"]
                and len(teardown) == value["collected"]
                and len({row["nodeid"] for row in teardown}) == value["collected"]
                and all(row["outcome"] == "passed" for row in teardown),
                "unsettled integration: fixture cleanup unconfirmed; retain run paths",
            )
    for name in h.root_names(prepared["state"]):
        if name.endswith(".json"):
            h.check(
                re.fullmatch(r"[0-9a-f]{64}\.json", name) is not None,
                "unknown simulated resource",
            )
            h.check(
                p.load(prepared["state"] / name)["active"] is False,
                "unsettled simulated container; retain run paths",
            )
