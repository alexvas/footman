"""Development-only subprocess probes; never part of relocated support closure."""

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import acceptance_attribution_support as attribution
from tests import acceptance_portability_support as support
from tests import mcp_provenance as p


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def rejected(action):
    try:
        action()
    except AssertionError:
        return
    raise AssertionError("invalid evidence was silently accepted under -O")


def attribution_probe(mode):
    def record():
        return {
            "case_id": "case",
            "inventory": [],
            "post_inventory": [],
            "public_observations": [
                {
                    "accepted": True,
                    "attempted": True,
                    "status": 0,
                    "wheel_sha256": "a",
                    "sdist_sha256": "b",
                    "rejection_category": None,
                    "surface_status": 0,
                    "pre_identity": "input",
                    "post_identity": "input",
                }
            ],
        }

    left, right = record(), record()
    attribution.compare(left, right)
    if mode == "attribution-id":
        right["case_id"] = "other"
    elif mode == "attribution-inventory":
        right["inventory"] = ["unbound"]
    elif mode == "attribution-status":
        right["public_observations"][0]["status"] = 17
    else:
        right["public_observations"][0]["post_identity"] = "changed"
    rejected(lambda: attribution.compare(left, right))


def worker_probe(mode, root):
    source_set = p.source_set([{"path": "__init__.py", "sha256": "0" * 64}])
    lock_digest = support.hashlib.sha256(b"lock").hexdigest()

    def worker():
        return SimpleNamespace(
            cleanup_count=0,
            candidate=root,
            root=root,
            worker_id="probe",
            bound=SimpleNamespace(
                repository_root=root, build_input_identity="bound", wheel=root / "wheel"
            ),
            dist_pre="dist",
            dependency_identity=lock_digest,
            inventory=(),
            wheel_sha256="wheel",
            site=root / "site",
            python=root / "venv/bin/python",
            tests=[{"resources_reconciled": True}],
            operations=[],
            reuse_count=0,
        )

    write = Mock()
    with (
        patch.object(
            support.build, "inventory", return_value=("bound", ())
        ) as inventory,
        patch.object(support, "dist_identity", return_value="dist"),
        patch.object(support.p, "read", return_value=b"lock"),
        patch.object(support.p, "write", write),
        patch.object(support.p, "sources", return_value=source_set),
        patch.object(
            support.build,
            "archive_expectations",
            return_value=(
                [{"source_path": "footman/__init__.py", "sha256": "0" * 64}],
                None,
            ),
        ),
    ):
        good = worker()
        support.finish_worker(good)
        require(
            good.cleanup_count == 1 and write.call_count == 1,
            "positive worker control failed",
        )
        write.reset_mock()
        bad = worker()
        if mode == "build-identity":
            inventory.return_value = ("changed", ())
        else:
            bad.tests[0]["resources_reconciled"] = False
        rejected(lambda: support.finish_worker(bad))
        require(
            bad.cleanup_count == 0 and write.call_count == 0,
            "invalid completion published",
        )


def reconciliation_probe(mode, root):
    case = {
        "tmp": root,
        "request": {
            "journal_path": str(root / "journal.json"),
            "transport_deadline_monotonic": time.monotonic() + 5,
            "lifecycle_binding": {"instance_token": "bound"},
        },
    }
    if mode == "journal-token":
        p.write(root / "journal.json", p.canonical({"instance_token": "wrong"}) + b"\n")
        rejected(lambda: support.journal(case))
        return

    class Directory:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def list_names(self):
            return list(records)

    identities = ("a" * 64, "b" * 64)
    records = {}
    for index, identity in enumerate(identities):
        for operation, argv in (
            ("run", ["run"]),
            ("rm", ["rm", "-f", identity]),
            (
                "ls",
                [
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "id=" + identity,
                    "--format",
                    "{{.ID}}",
                ],
            ),
        ):
            records[f"{index}-{operation}-call.json"] = {
                "argv": argv,
                "container_id": identity if operation == "run" else None,
                "status": 0,
                "stdin_eof": True,
            }
    with (
        patch.object(
            support, "journal", return_value={"instance_token": "bound"}
        ) as journal,
        patch.object(support, "incarnation", return_value="absent"),
        patch.object(
            support,
            "DirectoryDescriptor",
            SimpleNamespace(open_secure_path=lambda *_: Directory()),
        ),
        patch.object(support.p, "load", side_effect=lambda path: records[path.name]),
        patch.object(
            support,
            "run_resources",
            return_value=[{"container_id": identity} for identity in identities],
        ),
    ):
        require(
            support.reconcile(case)["resources_reconciled"],
            "positive reconciliation failed",
        )
        if mode == "observed-token":
            journal.return_value = None
            rejected(
                lambda: support.reconcile(
                    case, observed_incarnation={"instance_token": "wrong"}
                )
            )
        elif mode == "missing-remove":
            del records["0-rm-call.json"]
            rejected(lambda: support.reconcile(case))
        elif mode in {"wrong-id", "duplicate-cleanup"}:
            records["1-rm-call.json"]["argv"][-1] = (
                "c" if mode == "wrong-id" else "a"
            ) * 64
            rejected(lambda: support.reconcile(case))
        elif mode == "mismatched-filter":
            records["1-ls-call.json"]["argv"][5] = "id=" + "c" * 64
            rejected(lambda: support.reconcile(case))
        else:
            records["0-rm-call.json"]["status"] = 17
            rejected(lambda: support.reconcile(case))


def result_probe(mode, root):
    value = {
        "schema_version": 1,
        "provenance_final": True,
        "evidence_readback": True,
        "service_exit_code": 0,
        "wheel_sha256": "bound",
        "evidence_path": str(root / "evidence"),
        "service_context": "wheel",
        "padding": "",
    }
    payload = p.canonical(value) + b"\n"
    cleanup = SimpleNamespace(complete=True, failures=())
    stderr = b""

    class Owner:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def stream(self, chunk, *, on_stream_end):
            chunk(support.ProcessStream.STDOUT, payload)
            chunk(support.ProcessStream.STDERR, stderr)
            for stream in support.ProcessStream:
                on_stream_end(stream, SimpleNamespace(name="EOF"))
            return SimpleNamespace(lifecycle=cleanup, returncode=0)

    case = {
        "argv": [],
        "neutral": root,
        "env": {},
        "worker": SimpleNamespace(wheel_sha256="bound"),
        "request": {
            "transport_deadline_monotonic": time.monotonic() + 5,
            "provenance_evidence_path": str(root / "evidence"),
        },
    }
    with patch.object(
        support, "OwnedProcess", SimpleNamespace(launch=lambda *_, **__: Owner())
    ):
        require(support.run_helper(case)["accepted"], "positive result control failed")
        if mode == "oversized-result":
            value["padding"] = "x" * (65537 - len(payload))
            payload = p.canonical(value) + b"\n"
            require(len(payload) == 65537, "oversize probe setup drift")
            require(
                not support.run_helper(case)["accepted"], "oversize evidence accepted"
            )
        elif mode == "incomplete-cleanup":
            cleanup.complete = False
            rejected(lambda: support.run_helper(case))
        else:
            stderr = b"FOOTMAN_PROVENANCE_V1 not-helper-authority\n"
            rejected(lambda: support.run_helper(case))


def main():
    require(sys.flags.optimize == 1, "probe must actually run under -O")
    mode, root = sys.argv[1], Path(sys.argv[2])
    if mode.startswith("attribution-"):
        attribution_probe(mode)
    elif mode in {"build-identity", "unresolved-worker"}:
        worker_probe(mode, root)
    elif mode in {
        "journal-token",
        "observed-token",
        "missing-remove",
        "unsuccessful-call",
        "wrong-id",
        "duplicate-cleanup",
        "mismatched-filter",
    }:
        reconciliation_probe(mode, root)
    else:
        result_probe(mode, root)
    print(mode + ": rejected under -O")


if __name__ == "__main__":
    main()
