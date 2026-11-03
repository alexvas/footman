"""Phase 3 identity, containment and adapter-owned output handles."""

import dataclasses
import errno
import importlib
import os
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from footman.configuration import RequestError, validate_configuration
from footman.filesystem.descriptors import DescriptorError
from tests.dispatcher_harness import read_bytes, write_bytes

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
FILES = {
    "stdout.txt",
    "stderr.txt",
    "startup-stdout.txt",
    "startup-stderr.txt",
    "result.json",
}


def output_module():
    try:
        return importlib.import_module("footman.output")
    except ModuleNotFoundError as error:
        pytest.fail(f"required output reservation behavior is absent: {error}")


@pytest.fixture
def config(tmp_path):
    (tmp_path / "repository").mkdir()
    return validate_configuration(
        {
            "version": 1,
            "repository": "repository",
            "container_start_argv": ["docker", "run", "{snapshot_path}"],
            "command_argv": ["tool", "literal {phase}"],
            "working_directory": ".",
            "container_snapshot_directory": "/workspace",
            "snapshot_directory": "snapshots",
            "output_dir": "results/{phase}/{utc_datetime}",
            "timeout_seconds": 60,
            "cleanup_grace_seconds": 10,
        },
        tmp_path / "operator.toml",
    )


def supplied_uuid(milliseconds, payload=0):
    return uuid.UUID(int=(milliseconds << 80) | (7 << 76) | (2 << 62) | payload)


@pytest.mark.parametrize(
    "milliseconds", [0, 1, 999, 1000, 1001, 1793529000123, 253402300799999]
)
@pytest.mark.parametrize("payload", [0, (1 << 62) - 1, (0xFFF << 64) | ((1 << 62) - 1)])
def test_uuid_identity_roundtrip_exact_milliseconds(monkeypatch, milliseconds, payload):
    module = output_module()
    original = supplied_uuid(milliseconds, payload)
    generate = Mock(return_value=original)
    monkeypatch.setattr(uuid, "uuid7", generate)
    identity = module.RequestIdentity.create()
    generate.assert_called_once_with()
    assert identity.uuid == original
    assert len(identity.run_id) == 26
    assert identity.run_id.isascii()
    assert set(identity.run_id) <= set(ALPHABET)
    assert identity.run_id[0] in "01234567"
    decoded = 0
    for digit in identity.run_id:
        decoded = decoded * 32 + ALPHABET.index(digit)
    recovered = uuid.UUID(int=decoded)
    assert recovered == original
    assert recovered.version == 7
    assert recovered.variant == uuid.RFC_4122
    assert recovered.time == milliseconds
    expected = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
    assert identity.utc_timestamp == expected.isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    assert (
        identity.utc_datetime
        == expected.strftime("%Y%m%dT%H%M%S") + f"{milliseconds % 1000:03d}Z"
    )
    if milliseconds == 0:
        assert identity.run_id.startswith("0000000000")
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.run_id = "changed"


def test_identity_real_standard_library_uuid7():
    identity = output_module().RequestIdentity.create()
    assert identity.uuid.version == 7
    assert identity.uuid.variant == uuid.RFC_4122


def test_stalled_backward_clock_uses_returned_uuid_only(monkeypatch, config):
    module = output_module()
    # Logical UUID time can move forward despite wall-clock stalls/backsteps.
    values = [supplied_uuid(ms) for ms in [1793529000999, 1793529001000, 1793529001001]]
    generate = Mock(side_effect=values)
    monkeypatch.setattr(uuid, "uuid7", generate)
    identities = [module.RequestIdentity.create() for _ in values]
    assert generate.call_count == 3
    for identity, original in zip(identities, values, strict=True):
        assert identity.uuid is original
        path = module.expand_output(config, identity, "phase")
        assert path.name == identity.utc_datetime
        assert int(identity.utc_timestamp[20:23]) == original.time % 1000
    # No second clock source is permitted in request identity/expansion.
    import inspect

    source = inspect.getsource(module.RequestIdentity) + inspect.getsource(
        module.expand_output
    )
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert (
        "fromtimestamp" not in source
    )  # integer epoch arithmetic avoids float rounding


@pytest.mark.parametrize(
    "suffix", [(), ("{phase}",), ("{utc_datetime}",), ("{phase}", "{utc_datetime}")]
)
def test_single_pass_absolute_expansion(config, tmp_path, suffix):
    module = output_module()
    identity = module.RequestIdentity.create()
    # Canonical root names containing braces are literal, not template input.
    root = tmp_path / "literal-{phase}-{unknown}"
    selected = dataclasses.replace(config, output_root=root, output_suffix=suffix)
    expected = root.joinpath(
        *(
            part.replace("{phase}", "review_2").replace(
                "{utc_datetime}", identity.utc_datetime
            )
            for part in suffix
        )
    )
    assert module.expand_output(selected, identity, "review_2") == expected
    assert expected.is_absolute()
    assert identity.run_id not in str(expected)


def test_reserved_files_modes_paths_and_metadata(config):
    module = output_module()
    with module.reserve_request(config, {"phase": "review_2"}) as reserved:
        assert reserved.phase == "review_2"
        assert reserved.directory.name == reserved.identity.utc_datetime
        assert reserved.directory.parent.name == "review_2"
        assert set(reserved.paths) == FILES
        assert set(reserved.files) == FILES
        assert stat.S_IMODE(reserved.directory.stat().st_mode) == 0o700
        for name, owner in reserved.files.items():
            assert reserved.paths[name] == reserved.directory / name
            assert reserved.paths[name].is_absolute()
            assert stat.S_IMODE(os.fstat(owner.fd).st_mode) == 0o600
            os.write(owner.fd, name.encode())
    assert all(owner.released for owner in reserved.files.values())
    reserved.close()
    for name, path in reserved.paths.items():
        assert read_bytes(path) == name.encode()


@pytest.mark.parametrize("template", ["static", "phase"])
def test_static_and_reused_phase_collisions_are_output_failed(
    config, tmp_path, template
):
    module = output_module()
    root = (
        tmp_path / "results" / "static"
        if template == "static"
        else tmp_path / "results"
    )
    selected = dataclasses.replace(
        config,
        output_root=root,
        output_suffix=() if template == "static" else ("{phase}",),
    )
    with module.reserve_request(selected, {"phase": "repeat"}) as winner:
        os.write(winner.files["stdout.txt"].fd, b"winner")
        with pytest.raises(module.OutputError) as caught:
            module.reserve_request(selected, {"phase": "repeat"})
        assert caught.value.outcome == "output_failed"
        assert caught.value.paths == {}
        assert read_bytes(winner.paths["stdout.txt"]) == b"winner"
    assert set(winner.directory.iterdir()) == set(winner.paths.values())


@pytest.mark.parametrize("existing", ["directory", "symlink", "file"])
def test_existing_destination_untouched(config, tmp_path, existing):
    module = output_module()
    destination = tmp_path / "results" / "existing"
    destination.parent.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    write_bytes(sentinel / "bytes", b"untouched")
    if existing == "directory":
        destination.mkdir()
        write_bytes(destination / "stdout.txt", b"original")
    elif existing == "file":
        write_bytes(destination, b"original")
    else:
        destination.symlink_to(sentinel, target_is_directory=True)
    selected = dataclasses.replace(config, output_root=destination, output_suffix=())
    with pytest.raises(module.OutputError):
        module.reserve_request(selected, {})
    assert read_bytes(sentinel / "bytes") == b"untouched"
    if existing == "directory":
        assert read_bytes(destination / "stdout.txt") == b"original"
    elif existing == "file":
        assert read_bytes(destination) == b"original"
    else:
        assert destination.is_symlink()


def test_concurrent_identical_path_one_winner_no_regeneration(config, monkeypatch):
    module = output_module()
    barrier = Barrier(8)
    original = supplied_uuid(1793529000123)
    generate = Mock(return_value=original)
    monkeypatch.setattr(uuid, "uuid7", generate)

    def attempt(_):
        barrier.wait()
        try:
            with module.reserve_request(config, {"phase": "race"}) as reserved:
                os.write(reserved.files["stdout.txt"].fd, b"winner")
                return reserved
        except module.OutputError as error:
            assert error.outcome == "output_failed"
            assert error.paths == {}
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert generate.call_count == 8
    assert read_bytes(winners[0].paths["stdout.txt"]) == b"winner"
    assert list((config.output_root / "race").iterdir()) == [winners[0].directory]


@pytest.mark.parametrize("replacement", ["repository", "snapshot", "external"])
def test_replaced_storage_ancestor_rejected(config, tmp_path, replacement):
    module = output_module()
    target = {
        "repository": config.repository,
        "snapshot": config.snapshot_directory,
        "external": tmp_path / "external",
    }[replacement]
    target.mkdir(exist_ok=True)
    config.output_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(module.OutputError):
        module.reserve_request(config, {"phase": "safe"})
    assert list(target.iterdir()) == []


def test_dynamic_parent_symlink_rejected(config, tmp_path):
    module = output_module()
    target = tmp_path / "external"
    target.mkdir()
    config.output_root.mkdir()
    (config.output_root / "safe").symlink_to(target, target_is_directory=True)
    with pytest.raises(module.OutputError):
        module.reserve_request(config, {"phase": "safe"})
    assert list(target.iterdir()) == []


def test_shared_parent_permissions_unchanged(config):
    module = output_module()
    config.output_root.mkdir(mode=0o750)
    with module.reserve_request(config, {"phase": "shared"}):
        assert stat.S_IMODE(config.output_root.stat().st_mode) == 0o750


@pytest.mark.parametrize(
    "input_value", [{}, {"phase": "../escape"}, {"phase": ""}, {"command": "override"}]
)
def test_rejected_input_generates_no_identity_or_resources(
    config, monkeypatch, input_value
):
    module = output_module()
    generate = Mock()
    monkeypatch.setattr(uuid, "uuid7", generate)
    with pytest.raises(RequestError):
        module.reserve_request(config, input_value)
    generate.assert_not_called()
    assert not config.output_root.exists()
    assert not config.snapshot_directory.exists()


@pytest.mark.parametrize("stage", ["mkdir", "file"])
@pytest.mark.parametrize("number", [errno.EACCES, errno.ENOSPC])
def test_preexecution_storage_failure_has_zero_snapshot_launch_calls(
    config, monkeypatch, stage, number
):
    module = output_module()
    snapshot, launch = Mock(), Mock()
    original_open = module.os.open

    def open_file(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise OSError(number, "injected storage failure")
        return original_open(path, flags, *args, **kwargs)

    def mkdir(*args, **kwargs):
        raise OSError(number, "injected storage failure")

    if stage == "file":
        monkeypatch.setattr(module.os, "open", open_file)
    else:
        monkeypatch.setattr(module.PosixDescriptorOps, "mkdirat", mkdir)
    with pytest.raises(module.OutputError) as caught:
        with module.reserve_request(config, {"phase": "failure"}):
            snapshot()
            launch()
    assert caught.value.outcome == "output_failed"
    assert caught.value.paths == {}
    snapshot.assert_not_called()
    launch.assert_not_called()
    assert not config.snapshot_directory.exists()


@pytest.mark.parametrize("fail_after", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
def test_partial_file_setup_releases_all_owners_once(
    config, monkeypatch, fail_after, failure_type
):
    module = output_module()
    original_open, original_close = module.os.open, module.PosixDescriptorOps.close
    records, acquired, released = [], [], []
    failure = failure_type("setup failed")

    def open_file(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            if len(records) == fail_after:
                raise failure
            records.append(path)
        fd = original_open(path, flags, *args, **kwargs)
        acquired.append(fd)
        return fd

    def close(ops, fd):
        released.append(fd)
        original_close(ops, fd)

    monkeypatch.setattr(module.os, "open", open_file)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    expected = module.OutputError if failure_type is OSError else KeyboardInterrupt
    with pytest.raises(expected) as caught:
        module.reserve_request(config, {"phase": "partial"})
    if failure_type is KeyboardInterrupt:
        assert caught.value is failure
    else:
        assert set(caught.value.paths) == set(records)
        assert all(path.exists() for path in caught.value.paths.values())
    assert sorted(acquired) == sorted(released)
    assert len(set(released)) == len(released)


@pytest.mark.parametrize("primary_type", [None, OSError, KeyboardInterrupt])
@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt, RuntimeError])
def test_close_authority_independent_release_repeat_and_concurrency(
    config, monkeypatch, primary_type, close_type
):
    module = output_module()
    reserved = module.reserve_request(config, {"phase": "closing"})
    original_close = module.PosixDescriptorOps.close
    released = []
    failure = close_type("close failed")
    primary = primary_type("operation failed") if primary_type else None

    def close(ops, fd):
        released.append(fd)
        original_close(ops, fd)
        raise failure

    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    if primary_type is KeyboardInterrupt:
        expected = primary
    elif close_type in (KeyboardInterrupt, RuntimeError):
        expected = failure
    elif primary is None:
        expected = failure
    else:
        expected = None
    if expected is not None:
        with pytest.raises(type(expected)) as caught:
            reserved.close(primary)
        assert caught.value is expected
    else:
        reserved.close(primary)
        assert failure in getattr(primary, "_transaction_secondary", [])
    count = len(released)
    assert count >= 6
    assert len(set(released)) == count
    assert all(owner.released for owner in reserved.files.values())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: reserved.close(), range(8)))
    assert len(released) == count


@pytest.mark.parametrize("close_type", [None, OSError, KeyboardInterrupt])
@pytest.mark.parametrize("primary_type", [None, ValueError, KeyboardInterrupt])
def test_inflight_cleanup_invalidates_views_and_waits_for_release(
    config, monkeypatch, close_type, primary_type
):
    module = output_module()
    reserved = module.reserve_request(config, {"phase": "inflight"})
    acquired = [owner.fd for owner in reserved._files.values()] + [
        owner.fd for owner in reserved._directories
    ]
    view = reserved.files["stdout.txt"]
    entered, unblock, second_started = Event(), Event(), Event()
    original_close = module.PosixDescriptorOps.close
    closed = []
    failure = close_type("blocked close failure") if close_type else None
    primary = primary_type("active operation failure") if primary_type else None

    def close(ops, fd):
        closed.append(fd)
        if len(closed) == 1:
            entered.set()
            if not unblock.wait(5.0):
                raise AssertionError("blocked release was not unblocked")
        original_close(ops, fd)
        if len(closed) == 1 and failure is not None:
            raise failure

    def first_close():
        # Capture process-control exceptions too, without losing their identity
        # or letting a worker's KeyboardInterrupt interrupt the test runner.
        try:
            reserved.close(primary)
        except BaseException as error:
            return error
        return None

    def second_close():
        second_started.set()
        return reserved.close()

    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_close)
        second = None
        try:
            assert entered.wait(2.0)
            assert view.released
            with pytest.raises(RuntimeError, match="released"):
                _ = view.fd
            second = pool.submit(second_close)
            assert second_started.wait(2.0)
            with pytest.raises(FutureTimeoutError):
                second.result(timeout=0.1)
            assert not second.done()
            assert len(closed) == 1
        finally:
            unblock.set()
        result = first.result(timeout=2.0)
        assert second is not None
        assert second.result(timeout=2.0) is None

    if primary_type is KeyboardInterrupt:
        expected = primary
    elif close_type is KeyboardInterrupt:
        expected = failure
    elif primary is None:
        expected = failure
    else:
        expected = None
    assert result is expected
    assert reserved.cleanup_failure is expected
    if failure is not None and primary is not None:
        authority = expected if expected is not None else primary
        displaced = failure if authority is primary else primary
        assert displaced in getattr(authority, "_transaction_secondary", [])
    assert sorted(closed) == sorted(acquired)
    assert len(closed) == len(set(closed))
    assert all(borrowed.released for borrowed in reserved.files.values())
    before = list(closed)
    reserved.close()
    assert closed == before


def test_setup_operation_plus_close_failure(config, monkeypatch):
    module = output_module()
    original_open, original_close = module.os.open, module.PosixDescriptorOps.close
    operation = OSError(errno.ENOSPC, "file creation failed")
    secondary = OSError("close failed")
    released = []

    def open_file(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise operation
        return original_open(path, flags, *args, **kwargs)

    def close(ops, fd):
        released.append(fd)
        original_close(ops, fd)
        raise secondary

    monkeypatch.setattr(module.os, "open", open_file)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(module.OutputError) as caught:
        module.reserve_request(config, {"phase": "failed"})
    assert caught.value.__cause__ is operation
    assert secondary in getattr(caught.value, "_transaction_secondary", [])
    assert len(released) == len(set(released))


def test_setup_close_interruption_retains_operational_cause(config, monkeypatch):
    module = output_module()
    original_open, original_close = module.os.open, module.PosixDescriptorOps.close
    operation = OSError(errno.ENOSPC, "creation failed")
    interruption = KeyboardInterrupt("close interrupted")
    released = []

    def open_file(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise operation
        return original_open(path, flags, *args, **kwargs)

    def close(ops, fd):
        released.append(fd)
        original_close(ops, fd)
        raise interruption

    monkeypatch.setattr(module.os, "open", open_file)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(KeyboardInterrupt) as caught:
        module.reserve_request(config, {"phase": "interrupt"})
    assert caught.value is interruption
    displaced = getattr(interruption, "_transaction_secondary", [])
    assert len(displaced) == 1
    assert isinstance(displaced[0], module.OutputError)
    assert displaced[0].__cause__ is operation
    assert len(released) >= 1
    assert len(set(released)) == len(released)


def test_fixed_file_exclusive_even_if_injected_after_mkdir(config, monkeypatch):
    module = output_module()
    original = module.os.open
    injected = False

    def open_file(path, flags, *args, **kwargs):
        nonlocal injected
        if flags & os.O_CREAT and not injected:
            injected = True
            # A colliding entry must not be truncated or followed.
            directory = os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}")
            write_bytes(os.path.join(directory, path), b"preexisting")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_file)
    with pytest.raises(module.OutputError) as caught:
        module.reserve_request(config, {"phase": "collision"})
    assert caught.value.paths == {}
    assert read_bytes(caught.value.directory / "stdout.txt") == b"preexisting"


def test_directory_handoff_failure_is_retained_and_closed(config, monkeypatch):
    module = output_module()
    original = module.DirectoryDescriptor.create_directory
    owners = []

    def create(parent, *args, **kwargs):
        child = original(parent, *args, **kwargs)
        owners.append(child)
        return child

    monkeypatch.setattr(module.DirectoryDescriptor, "create_directory", create)
    with module.reserve_request(config, {"phase": "handoff"}):
        assert all(not owner.released for owner in owners)
    assert all(owner.released for owner in owners)


def test_optional_phase_retained_without_affecting_argv(config, tmp_path):
    module = output_module()
    selected = dataclasses.replace(
        config, output_root=tmp_path / "static", output_suffix=()
    )
    with module.reserve_request(selected, {"phase": "unused"}) as reserved:
        assert reserved.phase == "unused"
        assert selected.command_argv == config.command_argv
    selected = dataclasses.replace(selected, output_root=tmp_path / "another")
    with module.reserve_request(selected, {}) as reserved:
        assert reserved.phase is None


@pytest.mark.parametrize(
    "stage",
    [
        "root",
        "parent-open",
        "parent-create",
        "leaf-reopen",
        "directory-mode",
        "file-mode",
    ],
)
@pytest.mark.parametrize("operation_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("close_fails", [False, True])
def test_acquisition_branch_fault_matrix(
    config, monkeypatch, stage, operation_type, close_fails
):
    module = output_module()
    operation = operation_type("injected setup operation")
    secondary = OSError("injected independent release")
    real_open = module.os.open
    real_close = module.PosixDescriptorOps.close
    real_openat = module.PosixDescriptorOps.openat
    real_mkdir = module.PosixDescriptorOps.mkdirat
    real_mode = module.PosixDescriptorOps.fchmod
    acquired, released, reservations = [], [], []
    original_reservation = module.OutputReservation

    class RecordingReservation(original_reservation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            reservations.append(self)

    def open_file(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        acquired.append(fd)
        return fd

    def openat(ops, parent, name, flags, mode):
        if stage == "root" and parent is None:
            raise operation
        if stage == "parent-open" and name == "results":
            raise operation
        if stage == "leaf-reopen" and len(name) == 19 and name.endswith("Z"):
            raise operation
        return real_openat(ops, parent, name, flags, mode)

    def mkdir(ops, parent, name, mode):
        if stage == "parent-create" and name == "results":
            raise operation
        return real_mkdir(ops, parent, name, mode)

    def chmod(ops, fd, mode):
        if stage == "directory-mode" and mode == 0o700:
            raise operation
        if stage == "file-mode" and mode == 0o600:
            raise operation
        return real_mode(ops, fd, mode)

    def close(ops, fd):
        released.append(fd)
        real_close(ops, fd)
        if close_fails:
            raise secondary

    monkeypatch.setattr(module, "OutputReservation", RecordingReservation)
    monkeypatch.setattr(module.os, "open", open_file)
    monkeypatch.setattr(module.PosixDescriptorOps, "openat", openat)
    monkeypatch.setattr(module.PosixDescriptorOps, "mkdirat", mkdir)
    monkeypatch.setattr(module.PosixDescriptorOps, "fchmod", chmod)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    expected = module.OutputError if operation_type is OSError else KeyboardInterrupt
    with pytest.raises(expected) as caught:
        module.reserve_request(config, {"phase": "fault"})
    if operation_type is KeyboardInterrupt:
        assert caught.value is operation
    else:
        cause = caught.value.__cause__
        assert cause is operation or cause.cause is operation
    assert sorted(acquired) == sorted(released)
    assert len(set(released)) == len(released)
    before = list(released)
    reservations[0].close()
    assert released == before
    assert all(path.exists() for path in reservations[0].paths.values())


@pytest.mark.parametrize("close_fails", [False, True])
def test_parent_validation_enoent_is_not_retried(config, monkeypatch, close_fails):
    module = output_module()
    config.output_root.mkdir()
    operation = OSError(errno.ENOENT, "directory validation failed")
    secondary = OSError("validation cleanup failed")
    real_open = module.os.open
    real_fstat = module.PosixDescriptorOps.fstat
    real_close = module.PosixDescriptorOps.close
    real_mkdir = module.PosixDescriptorOps.mkdirat
    original_reservation = module.OutputReservation
    acquired, released, mkdir_calls, reservations = [], [], [], []
    validation_calls = []
    failing_fd = None
    close_injected = False

    class RecordingReservation(original_reservation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            reservations.append(self)

    def open_file(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        acquired.append(fd)
        return fd

    def fstat(ops, fd):
        nonlocal failing_fd
        if os.readlink(f"/proc/self/fd/{fd}") == str(config.output_root):
            validation_calls.append(fd)
            # Fail once: a retry would incorrectly conceal this operation.
            if len(validation_calls) == 1:
                failing_fd = fd
                raise operation
        return real_fstat(ops, fd)

    def close(ops, fd):
        nonlocal close_injected
        released.append(fd)
        real_close(ops, fd)
        if close_fails and fd == failing_fd and not close_injected:
            close_injected = True
            raise secondary

    def mkdir(ops, parent, name, mode):
        mkdir_calls.append(name)
        return real_mkdir(ops, parent, name, mode)

    monkeypatch.setattr(module, "OutputReservation", RecordingReservation)
    monkeypatch.setattr(module.os, "open", open_file)
    monkeypatch.setattr(module.PosixDescriptorOps, "fstat", fstat)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    monkeypatch.setattr(module.PosixDescriptorOps, "mkdirat", mkdir)
    with pytest.raises(module.OutputError) as caught:
        with module.reserve_request(config, {"phase": "validation"}):
            pass
    cause = caught.value.__cause__
    assert isinstance(cause, DescriptorError)
    assert cause.stage == "validate-descriptor"
    assert cause.cause is operation
    assert getattr(cause, "_transaction_secondary", []) == (
        [secondary] if close_fails else []
    )
    assert validation_calls == [failing_fd]
    assert mkdir_calls == []
    assert caught.value.paths == {}
    assert list(config.output_root.iterdir()) == []
    assert acquired
    assert sorted(acquired) == sorted(released)
    assert len(set(released)) == len(released)
    before = list(released)
    reservations[0].close()
    assert released == before


def test_replacement_between_check_and_open_is_not_followed(
    config, tmp_path, monkeypatch
):
    module = output_module()
    target = tmp_path / "sentinel"
    target.mkdir()
    original = module.DirectoryDescriptor.open_directory

    def open_directory(parent, name, **kwargs):
        if name == "results":
            config.output_root.symlink_to(target, target_is_directory=True)
        return original(parent, name, **kwargs)

    monkeypatch.setattr(module.DirectoryDescriptor, "open_directory", open_directory)
    with pytest.raises(module.OutputError):
        module.reserve_request(config, {"phase": "safe"})
    assert list(target.iterdir()) == []


def test_distinct_requests_share_parents_not_ownership(config):
    module = output_module()

    # Different labels avoid even a legitimate equal-millisecond collision.
    def attempt(index):
        with module.reserve_request(config, {"phase": f"request_{index}"}) as reserved:
            os.write(reserved.files["stdout.txt"].fd, str(index).encode())
            return reserved

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert len({result.directory for result in results}) == 8
    assert len({result.identity.run_id for result in results}) == 8
    for index, reserved in enumerate(results):
        assert read_bytes(reserved.paths["stdout.txt"]) == str(index).encode()
        assert all(owner.released for owner in reserved.files.values())


def test_closed_reservation_cannot_be_reentered(config):
    module = output_module()
    reserved = module.reserve_request(config, {"phase": "closed"})
    reserved.close()
    with pytest.raises(RuntimeError, match="released"):
        with reserved:
            pytest.fail("released handles were reused")


def test_context_operation_is_preserved_on_independent_close_failures(
    config, monkeypatch
):
    module = output_module()
    reserved = module.reserve_request(config, {"phase": "operation"})
    original = module.PosixDescriptorOps.close
    operation = ValueError("downstream failed")
    secondary = OSError("close failed")
    released = []

    def close(ops, fd):
        released.append(fd)
        original(ops, fd)
        raise secondary

    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(ValueError) as caught:
        with reserved:
            raise operation
    assert caught.value is operation
    assert secondary in getattr(operation, "_transaction_secondary", [])
    assert len(released) >= 6
    assert len(set(released)) == len(released)
    reserved.close()
    assert len(set(released)) == len(released)


@pytest.mark.parametrize("close_fails", [False, True])
def test_acquired_directory_move_fails_before_file_creation(
    config, tmp_path, monkeypatch, close_fails
):
    module = output_module()
    original = module.DirectoryDescriptor.create_directory
    moved = tmp_path / "moved-output"
    released = []
    close = module.PosixDescriptorOps.close

    def create(parent, name, **kwargs):
        child = original(parent, name, **kwargs)
        if name == "race":
            (config.output_root / "race").rename(moved)
        return child

    def release(ops, fd):
        released.append(fd)
        close(ops, fd)
        if close_fails:
            raise OSError("independent close failure")

    selected = dataclasses.replace(config, output_suffix=("race",))
    monkeypatch.setattr(module.DirectoryDescriptor, "create_directory", create)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", release)
    with pytest.raises(module.OutputError) as caught:
        module.reserve_request(selected, {})
    assert caught.value.paths == {}
    assert list(moved.iterdir()) == []
    assert len(released) == len(set(released))
    if close_fails:
        assert getattr(caught.value, "_transaction_secondary", [])


@pytest.mark.parametrize("forbidden_name", ["repository", "snapshot_directory"])
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_forbidden_root_symlink_replacement_during_walk(
    config, monkeypatch, forbidden_name, close_fails, interrupted
):
    module = output_module()
    selected = dataclasses.replace(config, output_suffix=("race",))
    forbidden = getattr(selected, forbidden_name)
    forbidden.mkdir(exist_ok=True)
    create = module.DirectoryDescriptor.create_directory
    setup = module.OutputReservation._setup
    close = module.PosixDescriptorOps.close
    reservations, acquired, closed, close_errors = [], [], [], []
    interruption = KeyboardInterrupt("interrupted overlap cleanup")

    def capture(reserved, settings):
        reservations.append(reserved)
        setup(reserved, settings)

    def replace_root(parent, name, **kwargs):
        child = create(parent, name, **kwargs)
        if name == "race":
            # Preflight has passed. Redirect only the forbidden configured
            # path, leaving the acquired output path equal to its destination.
            forbidden.rmdir()
            forbidden.symlink_to(selected.output_root / "race" / "sub")
            acquired.extend(owner.fd for owner in reservations[0]._directories)
            acquired.append(child.fd)
        return child

    def release(ops, fd):
        closed.append(fd)
        close(ops, fd)
        if interrupted and len(closed) == 1:
            raise interruption
        if close_fails:
            failure = OSError("independent overlap close failure")
            close_errors.append(failure)
            raise failure

    monkeypatch.setattr(module.OutputReservation, "_setup", capture)
    monkeypatch.setattr(module.DirectoryDescriptor, "create_directory", replace_root)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", release)
    expected = KeyboardInterrupt if interrupted else module.OutputError
    with pytest.raises(expected) as caught:
        module.reserve_request(selected, {})
    if interrupted:
        assert caught.value is interruption
        secondaries = getattr(interruption, "_transaction_secondary", [])
        domain = next(
            error for error in secondaries if isinstance(error, module.OutputError)
        )
    else:
        domain = caught.value
    assert domain.outcome == "output_failed"
    assert "acquired output overlaps" in str(domain.__cause__)
    assert domain.paths == {}
    assert forbidden.is_symlink()
    assert list((selected.output_root / "race").iterdir()) == []
    assert acquired
    assert sorted(closed) == sorted(acquired)
    assert len(closed) == len(set(closed))
    if close_fails:
        secondaries = getattr(caught.value, "_transaction_secondary", [])
        assert close_errors
        assert all(error in secondaries for error in close_errors)
    if interrupted:
        assert reservations[0].cleanup_failure is interruption
    before = list(closed)
    reservations[0].close()
    assert closed == before


@pytest.mark.parametrize("stage", ["acquired-path", "borrowed-view"])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
def test_new_setup_branches_release_once(
    config, monkeypatch, stage, interrupted, close_fails
):
    module = output_module()
    failure = (
        KeyboardInterrupt("interrupted setup") if interrupted else OSError("setup")
    )
    reservations = []
    closed = []
    setup = module.OutputReservation._setup
    close = module.PosixDescriptorOps.close
    readlink = os.readlink

    def capture(reserved, settings):
        reservations.append(reserved)
        setup(reserved, settings)

    def release(ops, fd):
        closed.append(fd)
        close(ops, fd)
        if close_fails:
            raise OSError("secondary close failure")

    def fail_view(*args):
        raise failure

    def fail_path(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise failure
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(module.OutputReservation, "_setup", capture)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", release)
    if stage == "acquired-path":
        monkeypatch.setattr(os, "readlink", fail_path)
    else:
        monkeypatch.setattr(module, "BorrowedDescriptor", fail_view)
    expected = KeyboardInterrupt if interrupted else module.OutputError
    with pytest.raises(expected) as caught:
        module.reserve_request(config, {"phase": "fault"})
    if interrupted:
        assert caught.value is failure
    else:
        assert caught.value.__cause__ is failure
    assert len(closed) >= 1
    assert len(closed) == len(set(closed))
    before = list(closed)
    reservations[0].close()
    assert closed == before
    assert len(reservations[0].paths) == (1 if stage == "borrowed-view" else 0)
    if close_fails:
        assert getattr(caught.value, "_transaction_secondary", [])


def test_output_views_do_not_expose_release_authority(config):
    module = output_module()
    with module.reserve_request(config, {"phase": "borrowed"}) as reserved:
        view = reserved.files["stdout.txt"]
        assert not hasattr(view, "close")
        assert not hasattr(view, "detach")
        with pytest.raises(TypeError):
            reserved.files["stdout.txt"] = view
        with pytest.raises(TypeError):
            reserved.paths["stdout.txt"] = reserved.directory / "wrong"
        os.write(view.fd, b"borrowed")
    assert view.released
    with pytest.raises(RuntimeError, match="released"):
        _ = view.fd
    assert read_bytes(reserved.paths["stdout.txt"]) == b"borrowed"
    reserved.close()


def test_capability_failure_is_domain_output_failure(config, monkeypatch):
    module = output_module()

    def unavailable(*args, **kwargs):
        raise DescriptorError("open-descriptor", "unavailable storage")

    monkeypatch.setattr(module.DirectoryDescriptor, "open_secure_path", unavailable)
    with pytest.raises(module.OutputError) as caught:
        module.reserve_request(config, {"phase": "failure"})
    assert caught.value.paths == {}
    assert caught.value.outcome == "output_failed"
