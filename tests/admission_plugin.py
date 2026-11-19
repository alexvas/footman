"""Session admission fixture and controller/worker delivery hooks."""

from pathlib import Path

import pytest

from tests.admission_support import (
    AdmissionPool,
    CapacityDecision,
    SessionAdmission,
    resolve_admission_capacity,
)

_DECISION = pytest.StashKey[CapacityDecision]()
_PAYLOAD = "footman_admission"


def _refuse(reason):
    raise pytest.UsageError(f"invalid admission configuration: {reason}")


def _received_decision(payload):
    if not isinstance(payload, dict):
        _refuse("missing or malformed controller payload")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        _refuse("unsupported or missing controller payload version")
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        _refuse("missing or malformed controller decision")
    try:
        return CapacityDecision(**decision)
    except (TypeError, ValueError) as error:
        raise pytest.UsageError(
            f"invalid admission configuration: invalid controller decision: {error}"
        ) from error


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if hasattr(config, "workerinput"):
        # Validate before fixtures/tests; no new CPU observation in a worker.
        config.stash[_DECISION] = _received_decision(config.workerinput.get(_PAYLOAD))
        return
    count = config.getoption("numprocesses", default=None)
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
    ):
        _refuse("worker count must be resolved and nonnegative")
    distributed = config.getoption("dist", default="no") != "no" and bool(
        config.getoption("tx", default=[])
    )
    if not distributed:
        if count not in (None, 0):
            _refuse("distributed worker count has no final local specs")
        config.stash[_DECISION] = resolve_admission_capacity(workers=1)


@pytest.hookimpl(tryfirst=True)
def pytest_xdist_setupnodes(config, specs):
    workers = len(specs)
    if workers <= 0:
        _refuse("final worker count must be positive")
    for spec in specs:
        if spec.popen is not True or any(
            getattr(spec, key, None) is not None
            for key in ("ssh", "socket", "vagrant_ssh", "via", "installvia", "chdir")
        ):
            _refuse("only same-host local popen without chdir is supported")
    if _DECISION in config.stash:
        _refuse("controller decision is already resolved")
    config.stash[_DECISION] = resolve_admission_capacity(workers=workers)


def pytest_configure_node(node):
    if _DECISION not in node.config.stash:
        _refuse("controller decision is unresolved")
    decision = node.config.stash[_DECISION]
    node.workerinput[_PAYLOAD] = {
        "version": 1,
        "decision": {"capacity": decision.capacity, "reason": decision.reason},
    }


@pytest.fixture(scope="session")
def admission(request, tmp_path_factory):
    if _DECISION not in request.config.stash:
        _refuse("session decision is unresolved")
    base = tmp_path_factory.getbasetemp()
    if not isinstance(base, Path):
        _refuse("basetemp must be a path")
    directory = base.parent if hasattr(request.config, "workerinput") else base
    if not directory.is_absolute():
        _refuse("namespace must be absolute")
    decision = request.config.stash[_DECISION]
    return SessionAdmission(
        shared_pool=AdmissionPool(directory=directory, capacity=decision.capacity)
    )
