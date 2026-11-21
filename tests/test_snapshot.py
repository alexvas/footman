"""Phase 4 contracts against disposable repositories, never the developer index."""

import importlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.dispatcher_harness import GitFixture, read_bytes, run, write_bytes


def api():
    # Missing behavior is an assertion failure, not a collection/import error.
    import footman

    assert (Path(footman.__file__).parent / "snapshot.py").is_file(), (
        "Phase 4 snapshot reservation/capture/export adapter is absent"
    )
    return importlib.import_module("footman.snapshot")


@pytest.fixture
def repository(tmp_path):
    return GitFixture.create(tmp_path / "repository")


@pytest.fixture
def storage(tmp_path):
    root = tmp_path / "snapshots"
    root.mkdir(mode=0o700)
    return root


RUN_ID = "01k8w00n007008000000000000"


def reserve(storage, run_id=RUN_ID):
    return api().reserve_snapshot(storage, run_id)


def track_snapshot_descriptors(monkeypatch, *, owners=(), close_error=None):
    """Observe borrowed fds at the ops seam; foundation owners still release."""
    module = api()
    openat = module.PosixDescriptorOps.openat
    close = module.PosixDescriptorOps.close
    readlink = module.os.readlink
    acquired = [
        (owner.fd, Path(readlink(f"/proc/self/fd/{owner.fd}"))) for owner in owners
    ]
    logical_paths = dict(acquired)
    released = []
    close_errors = []

    def acquire(ops, parent, name, flags, mode=0):
        expected = Path(name) if parent is None else logical_paths[parent] / name
        fd = openat(ops, parent, name, flags, mode)
        acquired.append((fd, Path(readlink(f"/proc/self/fd/{fd}"))))
        logical_paths[fd] = expected
        return fd

    def release(ops, fd):
        released.append(fd)
        close(ops, fd)
        logical_paths.pop(fd, None)
        if close_error is not None and not close_errors:
            close_errors.append(close_error)
            raise close_error

    monkeypatch.setattr(module.PosixDescriptorOps, "openat", acquire)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", release)
    if os.environ.get("FOOTMAN_SNAPSHOT_CONTROL") == "stale-capability-path":
        # Isolated negative control: lie only about tracked capability paths,
        # as though a move had not invalidated their original path identity.
        def stale_path(path, *args, **kwargs):
            for fd, expected in logical_paths.items():
                if path == f"/proc/self/fd/{fd}":
                    return str(expected)
            return readlink(path, *args, **kwargs)

        monkeypatch.setattr(module.os, "readlink", stale_path)
    return acquired, released, close_errors


def assert_snapshot_descriptors_released_once(acquired, released):
    numbers = [fd for fd, _ in acquired]
    assert numbers
    assert len(numbers) == len(set(numbers)), (
        "test must track one live generation per fd"
    )
    assert sorted(released) == sorted(numbers)
    assert len(released) == len(set(released))


def test_reservation_path_and_repeat_release(storage):
    owner = reserve(storage)
    assert owner.path == storage / RUN_ID
    assert owner.path.is_dir()
    assert stat.S_IMODE(owner.path.stat().st_mode) == 0o700
    owner.close()
    owner.close()
    assert owner.path.is_dir(), "descriptor release must not delete snapshot entries"


@pytest.mark.parametrize("name", ["../escape", "/escape", ".", "..", "", "a/b"])
def test_reservation_rejects_unsafe_identity(storage, name):
    with pytest.raises((ValueError, api().SnapshotError)):
        reserve(storage, name)
    assert list(storage.iterdir()) == []


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_destination_grants_no_ownership(storage, tmp_path, symlink):
    destination = storage / RUN_ID
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    write_bytes(sentinel / "bytes", b"untouched")
    if symlink:
        destination.symlink_to(sentinel, target_is_directory=True)
    else:
        destination.mkdir()
        write_bytes(destination / "bytes", b"winner")
    with pytest.raises(api().SnapshotError):
        reserve(storage)
    assert read_bytes(sentinel / "bytes") == b"untouched"
    if not symlink:
        assert read_bytes(destination / "bytes") == b"winner"


def test_same_id_concurrent_collision_has_one_owner(storage):
    module = api()

    def attempt(_):
        try:
            return module.reserve_snapshot(storage, RUN_ID)
        except module.SnapshotError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    write_bytes(winner.path / "winner", b"owned by winner")
    with pytest.raises(module.SnapshotError):
        reserve(storage)
    assert read_bytes(winner.path / "winner") == b"owned by winner"
    winner.close()
    assert read_bytes(winner.path / "winner") == b"owned by winner"


def test_replaced_root_symlink_is_not_followed(storage, tmp_path):
    original = storage.with_name("original")
    storage.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    storage.symlink_to(outside, target_is_directory=True)
    with pytest.raises(api().SnapshotError):
        reserve(storage)
    assert list(outside.iterdir()) == []


def test_missing_snapshot_parents_confined_and_permissions_unchanged(
    tmp_path, monkeypatch
):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o751)
    root = shared / "missing" / "nested" / "snapshots"
    created = [shared / "missing", shared / "missing/nested", root, root / RUN_ID]
    acquired, released, _ = track_snapshot_descriptors(monkeypatch)
    snapshot = reserve(root)
    try:
        assert snapshot.path == root / RUN_ID
        assert root.resolve() == root
        for directory in created:
            assert directory.is_relative_to(shared)
            assert not directory.is_symlink()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert list(shared.iterdir()) == [created[0]]
        assert list(root.iterdir()) == [snapshot.path]
        assert list(snapshot.path.iterdir()) == []
        assert stat.S_IMODE(shared.stat().st_mode) == 0o751
    finally:
        snapshot.close()
        snapshot.close()
    assert stat.S_IMODE(shared.stat().st_mode) == 0o751
    assert_snapshot_descriptors_released_once(acquired, released)


@pytest.mark.parametrize("leaf_collision", [False, True])
def test_concurrent_snapshot_parent_creation_reopens_same_name(
    tmp_path, monkeypatch, leaf_collision
):
    module = api()
    shared = tmp_path / "shared"
    shared.mkdir()
    root = shared / "parent" / "snapshots"
    create = module.DirectoryDescriptor.create_directory
    mkdir = module.PosixDescriptorOps.mkdirat
    injected = []
    attempted = []
    sibling = root.parent / "injected-sibling"
    winner = root / RUN_ID / "winner-sentinel"
    acquired, released, _ = track_snapshot_descriptors(monkeypatch)

    def create_parent(parent, name, **kwargs):
        if name == "parent" and not injected:
            # The preceding no-follow open saw ENOENT. Another request wins
            # mkdir before this request attempts the same basename.
            root.parent.mkdir(mode=0o700)
            sibling.mkdir(mode=0o700)
            if leaf_collision:
                root.mkdir(mode=0o700)
                winner.parent.mkdir(mode=0o700)
                winner.mkdir(mode=0o700)
            injected.append(sibling.stat().st_ino)
        return create(parent, name, **kwargs)

    def mkdirat(ops, fd, name, mode):
        attempted.append(name)
        return mkdir(ops, fd, name, mode)

    def never_delete(*args, **kwargs):
        pytest.fail("parent/leaf collision grants no deletion authority")

    monkeypatch.setattr(module.DirectoryDescriptor, "create_directory", create_parent)
    monkeypatch.setattr(module.PosixDescriptorOps, "mkdirat", mkdirat)
    monkeypatch.setattr(module.PosixDescriptorOps, "unlinkat", never_delete)
    monkeypatch.setattr(module.PosixDescriptorOps, "rmdirat", never_delete)
    if leaf_collision:
        with pytest.raises(module.SnapshotError) as caught:
            reserve(root)
        assert isinstance(caught.value.__cause__, module.DescriptorError)
        assert caught.value.__cause__.stage == "create-descriptor"
        assert isinstance(caught.value.__cause__.cause, FileExistsError)
        assert attempted == ["parent", RUN_ID]
        assert list(winner.parent.iterdir()) == [winner]
    else:
        snapshot = reserve(root)
        try:
            assert snapshot.path == root / RUN_ID
            assert list(snapshot.path.iterdir()) == []
        finally:
            snapshot.close()
            snapshot.close()
        assert attempted == ["parent", "snapshots", RUN_ID]
    assert injected and sibling.stat().st_ino == injected[0]
    assert sorted(path.name for path in root.parent.iterdir()) == [
        "injected-sibling",
        "snapshots",
    ]
    assert_snapshot_descriptors_released_once(acquired, released)


@pytest.mark.parametrize("stage", ["before-open", "before-create"])
@pytest.mark.parametrize("close_type", [None, OSError, KeyboardInterrupt])
def test_snapshot_parent_symlink_replacement_during_walk(
    tmp_path, monkeypatch, stage, close_type
):
    module = api()
    shared = tmp_path / "shared"
    shared.mkdir()
    root = shared / "parent" / "snapshots"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").mkdir()
    if stage == "before-open":
        root.parent.mkdir()
        root.mkdir()
    replaced = []
    open_directory = module.DirectoryDescriptor.open_directory
    create_directory = module.DirectoryDescriptor.create_directory
    close_error = close_type("walk cleanup failed") if close_type else None
    acquired, released, close_errors = track_snapshot_descriptors(
        monkeypatch, close_error=close_error
    )

    def replace():
        if replaced:
            return
        if stage == "before-open":
            root.parent.rename(shared / "original-parent")
        root.parent.symlink_to(outside, target_is_directory=True)
        replaced.append(True)

    def open_parent(parent, name, **kwargs):
        if name == "parent" and stage == "before-open":
            replace()
        return open_directory(parent, name, **kwargs)

    def create_parent(parent, name, **kwargs):
        if name == "parent" and stage == "before-create":
            replace()
        return create_directory(parent, name, **kwargs)

    monkeypatch.setattr(module.DirectoryDescriptor, "open_directory", open_parent)
    monkeypatch.setattr(module.DirectoryDescriptor, "create_directory", create_parent)
    expected = (
        KeyboardInterrupt if close_type is KeyboardInterrupt else module.SnapshotError
    )
    with pytest.raises(expected) as caught:
        reserve(root)
    assert replaced and root.parent.is_symlink()
    assert all(not path.is_relative_to(outside) for _, path in acquired)
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert_snapshot_descriptors_released_once(acquired, released)
    if close_type is KeyboardInterrupt:
        assert caught.value is close_error
    elif close_type is OSError:
        assert close_errors == [close_error]
        assert close_error in getattr(caught.value, "_transaction_secondary", [])


@pytest.mark.parametrize("stage", ["root", "parent", "leaf"])
@pytest.mark.parametrize("close_type", [None, OSError, KeyboardInterrupt])
def test_snapshot_capability_move_during_reservation(
    tmp_path, monkeypatch, stage, close_type
):
    module = api()
    shared = tmp_path / "shared"
    shared.mkdir()
    root = shared / "snapshots"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved"
    open_directory = module.DirectoryDescriptor.open_directory
    create_directory = module.DirectoryDescriptor.create_directory
    close_error = close_type("move cleanup failed") if close_type else None
    acquired, released, close_errors = track_snapshot_descriptors(
        monkeypatch, close_error=close_error
    )
    mutation = []

    def open_parent(parent, name, **kwargs):
        child = open_directory(parent, name, **kwargs)
        if not mutation and (
            (stage == "root" and name == "snapshots")
            or (stage == "parent" and name == "shared")
        ):
            original = root if stage == "root" else shared
            original.rename(moved)
            original.symlink_to(outside, target_is_directory=True)
            mutation.append(True)
        return child

    def create_leaf(parent, name, **kwargs):
        child = create_directory(parent, name, **kwargs)
        if stage == "leaf" and name == RUN_ID:
            (root / RUN_ID).rename(moved)
            mutation.append(True)
        return child

    monkeypatch.setattr(module.DirectoryDescriptor, "open_directory", open_parent)
    monkeypatch.setattr(module.DirectoryDescriptor, "create_directory", create_leaf)
    expected = (
        KeyboardInterrupt if close_type is KeyboardInterrupt else module.SnapshotError
    )
    with pytest.raises(expected) as caught:
        with reserve(root):
            pass
    assert mutation
    target = moved / "snapshots" if stage == "parent" else moved
    assert list(target.iterdir()) == []
    assert list(outside.iterdir()) == []
    assert_snapshot_descriptors_released_once(acquired, released)
    if close_type is KeyboardInterrupt:
        assert caught.value is close_error
    else:
        message = (
            "snapshot capability moved"
            if stage == "leaf"
            else "snapshot root capability moved"
        )
        assert message in str(caught.value)
        if close_type is OSError:
            assert close_errors == [close_error]
            assert close_error in getattr(caught.value, "_transaction_secondary", [])


@pytest.mark.parametrize("operation", ["capture", "export"])
@pytest.mark.parametrize("mutation", ["leaf", "root", "parent", "removed"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_snapshot_move_after_reservation_prevents_io(
    repository, tmp_path, monkeypatch, operation, mutation, close_fails
):
    module = api()
    shared = tmp_path / "shared"
    shared.mkdir()
    root = shared / "snapshots"
    root.mkdir()
    snapshot = reserve(root)
    tree = snapshot.capture(repository.path) if operation == "export" else None
    original_state = (snapshot._captured, snapshot._exported, snapshot.processes)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").mkdir()
    moved = tmp_path / "moved"
    close_error = OSError("independent move close failure") if close_fails else None
    acquired, released, close_errors = track_snapshot_descriptors(
        monkeypatch, owners=snapshot._directories, close_error=close_error
    )
    original_acquired = list(acquired)
    if mutation == "leaf":
        snapshot.path.rename(moved)
        target = moved
    elif mutation == "root":
        root.rename(moved)
        root.symlink_to(outside, target_is_directory=True)
        target = moved / RUN_ID
    elif mutation == "parent":
        shared.rename(moved)
        shared.symlink_to(outside, target_is_directory=True)
        target = moved / "snapshots" / RUN_ID
    else:
        snapshot.path.rmdir()
        target = None
    with pytest.raises(
        module.SnapshotError, match="snapshot capability moved"
    ) as caught:
        with snapshot:
            if tree is None:
                snapshot.capture(repository.path)
            else:
                snapshot.export(tree)
    # Identity rejection is at the entry boundary, not a later Git/storage
    # failure that could hide an already-started read or a transient write.
    assert (
        snapshot._captured,
        snapshot._exported,
        snapshot.processes,
    ) == original_state
    assert acquired == original_acquired
    if target is not None:
        assert list(target.iterdir()) == []
    else:
        assert not snapshot.path.exists()
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert_snapshot_descriptors_released_once(acquired, released)
    if close_fails:
        assert close_errors == [close_error]
        assert close_error in getattr(caught.value, "_transaction_secondary", [])
    before = list(released)
    snapshot.close()
    snapshot.close()
    assert released == before


def test_capture_preserves_development_state(repository, storage):
    before = repository.identity()
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        assert (
            tree.tree_id
            == repository.git("rev-parse", "HEAD^{tree}").stdout.strip().decode()
        )
        assert tree.object_format == "sha1"
    repository.assert_unchanged(before)


def test_capture_sanitizes_git_redirection(repository, storage, tmp_path, monkeypatch):
    other = GitFixture.create(tmp_path / "other")
    write_bytes(other.path / "payload", b"wrong repository")
    other.git("add", "payload")
    monkeypatch.setenv("GIT_DIR", str(other.path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other.path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other.path / ".git/index"))
    before = repository.identity()
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        assert (
            tree.tree_id
            == repository.git("rev-parse", "HEAD^{tree}").stdout.strip().decode()
        )
    repository.assert_unchanged(before)


def test_linked_worktree_capture(repository, storage, tmp_path):
    linked = tmp_path / "linked"
    repository.git("worktree", "add", "--quiet", "--detach", str(linked))
    fixture = GitFixture(linked)
    write_bytes(linked / "payload", b"linked staged")
    fixture.git("add", "payload")
    before = fixture.git("ls-files", "--stage", "-z").stdout
    with reserve(storage) as snapshot:
        tree = snapshot.capture(linked)
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == b"linked staged"
    assert fixture.git("ls-files", "--stage", "-z").stdout == before


@pytest.mark.sleepy
def test_unresolved_index_is_rejected(repository, storage):
    blob = repository.git("rev-parse", "HEAD:payload").stdout.strip().decode()
    repository.git("update-index", "--force-remove", "payload")
    # update-index accepts an argv cacheinfo for ordinary entries; use a real
    # conflict produced by merging disposable branches instead of pipe stdin.
    repository.git("reset", "--hard", "HEAD")
    repository.git("checkout", "--quiet", "-b", "other")
    write_bytes(repository.path / "payload", b"other\n")
    repository.git("commit", "--quiet", "-am", "other")
    repository.git("checkout", "--quiet", "master")
    write_bytes(repository.path / "payload", b"ours\n")
    repository.git("commit", "--quiet", "-am", "ours")
    result = run(["git", "merge", "other"], cwd=repository.path, check=False)
    assert result.returncode != 0 and blob
    before = repository.identity()
    with (
        reserve(storage) as snapshot,
        pytest.raises(api().SnapshotError, match="unresolved"),
    ):
        snapshot.capture(repository.path)
    repository.assert_unchanged(before)


@pytest.mark.sleepy
def test_split_index_explicit_policy(repository, storage):
    repository.git("update-index", "--split-index")
    before = repository.identity()
    with reserve(storage) as snapshot:
        try:
            tree = snapshot.capture(repository.path)
        except api().SnapshotError as exc:
            assert "split" in str(exc).lower() and "unsupported" in str(exc).lower()
        else:
            snapshot.export(tree)
            assert read_bytes(snapshot.path / "payload") == b"staged\n"
    repository.assert_unchanged(before)


@pytest.mark.sleepy
def test_dev_snapshot_mount_identity(repository, storage):
    write_bytes(repository.path / "payload", b"staged replacement\x00\xff")
    repository.git("add", "payload")
    staged = repository.git("write-tree").stdout.strip().decode()
    before = repository.identity()
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        assert tree.tree_id == staged
        repository.assert_unchanged(before)
        write_bytes(repository.path / "payload", b"later workdir changes")
        repository.git("add", "payload")
        deliberately_changed = repository.identity()
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == b"staged replacement\x00\xff"
        assert not (snapshot.path / ".git").exists()
        repository.assert_unchanged(deliberately_changed)


def test_raw_export_modes_names_and_exclusions(repository, storage):
    repository.git("rm", "--quiet", "--cached", "payload")
    for name, data in [
        ("binary", b"\x00\xff\x01"),
        ("space name", b"space"),
        ("line\nname", b"newline"),
        ("executable", b"#!/bin/sh\nexit 91\n"),
    ]:
        write_bytes(repository.path / name, data)
    os.chmod(repository.path / "executable", 0o755)
    (repository.path / "nested").mkdir()
    write_bytes(repository.path / "nested/target", b"target")
    (repository.path / "link").symlink_to("nested/target")
    (repository.path / "chain").symlink_to("link")
    write_bytes(repository.path / ".gitignore", b"ignored\n")
    repository.git(
        "add",
        "binary",
        "space name",
        "line\nname",
        "executable",
        "nested",
        "link",
        "chain",
        ".gitignore",
    )
    write_bytes(repository.path / "ignored", b"build artifact")
    write_bytes(repository.path / "untracked", b"not staged")
    before = repository.identity()
    with reserve(storage) as snapshot:
        snapshot.export(snapshot.capture(repository.path))
        assert read_bytes(snapshot.path / "binary") == b"\x00\xff\x01"
        assert read_bytes(snapshot.path / "space name") == b"space"
        assert read_bytes(snapshot.path / "line\nname") == b"newline"
        assert stat.S_IMODE((snapshot.path / "executable").stat().st_mode) == 0o755
        assert stat.S_IMODE((snapshot.path / "binary").stat().st_mode) == 0o644
        assert (snapshot.path / "nested").is_dir()
        assert stat.S_IMODE((snapshot.path / "nested").stat().st_mode) == 0o700
        assert (snapshot.path / "link").is_symlink()
        assert os.readlink(snapshot.path / "link") == "nested/target"
        assert os.readlink(snapshot.path / "chain") == "link"
        for excluded in ["payload", "ignored", "untracked", ".git"]:
            assert not (snapshot.path / excluded).exists()
    repository.assert_unchanged(before)


@pytest.mark.parametrize(
    "target", ["/etc/passwd", "../outside", "cycle", "missing/../../outside"]
)
def test_unsafe_symlink_export_rejected(repository, storage, target):
    (repository.path / "cycle").symlink_to(target)
    repository.git("add", "cycle")
    before = repository.identity()
    with (
        reserve(storage) as snapshot,
        pytest.raises(api().SnapshotError, match="symlink"),
    ):
        snapshot.export(snapshot.capture(repository.path))
    repository.assert_unchanged(before)


def test_gitlink_rejected(repository, storage):
    commit = repository.git("rev-parse", "HEAD").stdout.strip().decode()
    repository.git("update-index", "--add", "--cacheinfo", f"160000,{commit},submodule")
    before = repository.identity()
    with (
        reserve(storage) as snapshot,
        pytest.raises(api().SnapshotError, match="gitlink"),
    ):
        snapshot.export(snapshot.capture(repository.path))
    repository.assert_unchanged(before)


def test_filters_hooks_and_archive_attributes_are_not_evaluated(repository, storage):
    marker = repository.path.parent / "consumer-code-ran"
    script = f"#!/bin/sh\ntouch '{marker}'\nexit 99\n".encode()
    write_bytes(repository.path / ".git/hooks/post-checkout", script, mode=0o700)
    repository.git("config", "filter.poison.smudge", f"touch '{marker}'")
    write_bytes(
        repository.path / ".gitattributes",
        b"payload filter=poison export-ignore\nsubst export-subst\n",
    )
    write_bytes(repository.path / "subst", b"$Format:%H$\n")
    repository.git("add", ".gitattributes", "subst")
    before = repository.identity()
    with reserve(storage) as snapshot:
        snapshot.export(snapshot.capture(repository.path))
        assert read_bytes(snapshot.path / "payload") == b"staged\n"
        assert read_bytes(snapshot.path / "subst") == b"$Format:%H$\n"
        assert not marker.exists()
    repository.assert_unchanged(before)


def test_atomic_index_replacement_reads_one_open_version(
    repository, storage, monkeypatch
):
    module = api()
    original_read = module.os.read
    old_tree = repository.git("rev-parse", "HEAD^{tree}").stdout.strip().decode()
    index = repository.path / ".git/index"
    original_inode = index.stat().st_ino
    changed = []

    def replace_after_read(fd, size):
        data = original_read(fd, min(size, 31))
        if not changed and os.fstat(fd).st_ino == original_inode:
            changed.append(True)
            write_bytes(repository.path / "payload", b"concurrently staged")
            repository.git("add", "payload")
            changed.append(repository.identity())
        return data

    monkeypatch.setattr(module.os, "read", replace_after_read)
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        assert changed and tree.tree_id == old_tree
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == b"staged\n"
    repository.assert_unchanged(changed[1])


def test_in_place_index_change_is_detected(repository, storage, monkeypatch):
    module = api()
    original_read = module.os.read
    index = repository.path / ".git/index"
    inode = index.stat().st_ino
    changed = []

    def modify_open_version(fd, size):
        data = original_read(fd, size)
        if not changed and os.fstat(fd).st_ino == inode:
            changed.append(True)
            write_bytes(index, b"corrupt index")
        return data

    monkeypatch.setattr(module.os, "read", modify_open_version)
    with (
        reserve(storage) as snapshot,
        pytest.raises(module.SnapshotError, match="index"),
    ):
        snapshot.capture(repository.path)
    assert changed


def test_large_blob_stream_is_not_capture_tail(repository, storage):
    payload = bytes(range(256)) * 16384
    write_bytes(repository.path / "large", payload)
    repository.git("add", "large")
    with reserve(storage) as snapshot:
        snapshot.export(snapshot.capture(repository.path))
        assert read_bytes(snapshot.path / "large") == payload


@pytest.mark.parametrize(
    "name", [b"../escape", b"/absolute", b"a/../b", b".git/config", b"a//b", b"a/./b"]
)
def test_unsafe_raw_tree_paths_are_rejected(name):
    parser = api().TreeEntries("sha1")
    with pytest.raises(api().SnapshotError, match="path"):
        parser.feed(b"100644 blob " + b"a" * 40 + b"\t" + name + b"\0")


@pytest.mark.parametrize("mode", [b"100600", b"100664", b"170000", b"000000"])
def test_unsupported_raw_tree_modes_are_rejected(mode):
    parser = api().TreeEntries("sha1")
    with pytest.raises(api().SnapshotError, match="mode"):
        parser.feed(mode + b" blob " + b"a" * 40 + b"\tfile\0")


def test_non_utf8_name_and_symlink_target_preserved(repository, storage):
    target = b"target-\xff"
    write_bytes(repository.path / os.fsdecode(target), b"exact")
    os.symlink(target, os.fsencode(repository.path / "link-bytes"))
    repository.git("add", "--all")
    with reserve(storage) as snapshot:
        snapshot.export(snapshot.capture(repository.path))
        assert os.readlink(os.fsencode(snapshot.path / "link-bytes")) == target
        assert read_bytes(snapshot.path / os.fsdecode(target)) == b"exact"


@pytest.mark.parametrize("target", ["link/../outside", "missing", "file/child"])
def test_unprovable_symlink_chains_rejected(repository, storage, target):
    (repository.path / "link").symlink_to("nested")
    (repository.path / "nested").mkdir()
    write_bytes(repository.path / "file", b"not a directory")
    (repository.path / "bad").symlink_to(target)
    repository.git("add", "--all")
    with (
        reserve(storage) as snapshot,
        pytest.raises(api().SnapshotError, match="symlink"),
    ):
        snapshot.export(snapshot.capture(repository.path))


def test_export_refuses_injected_symlink_parent(repository, storage):
    (repository.path / "dir").mkdir()
    write_bytes(repository.path / "dir/file", b"staged")
    repository.git("add", "dir")
    outside = storage.parent / "outside"
    outside.mkdir()
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        (snapshot.path / "dir").symlink_to(outside, target_is_directory=True)
        with pytest.raises(api().SnapshotError):
            snapshot.export(tree)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("branch", ["index-read", "private-index", "export-file"])
@pytest.mark.parametrize("operation", [None, OSError, KeyboardInterrupt])
def test_file_branch_operation_close_and_repeat(
    repository, storage, monkeypatch, branch, operation
):
    module = api()
    original_close = module.PosixDescriptorOps.close
    original_read = module.os.read
    original_write = module.os.write
    close_error = OSError("injected sole close")
    primary = operation("injected operation") if operation else None
    wanted = {
        "index-read": "index",
        "private-index": ".footman-index",
        "export-file": "payload",
    }[branch]
    closed = []
    touched = []

    def matches(fd):
        try:
            return Path(os.readlink(f"/proc/self/fd/{fd}")).name == wanted
        except OSError:
            return False

    def close(ops, fd):
        target = matches(fd)
        original_close(ops, fd)
        if target:
            closed.append(fd)
            raise close_error

    def read(fd, size):
        if branch == "index-read" and matches(fd) and primary is not None:
            touched.append(True)
            raise primary
        return original_read(fd, size)

    def write(fd, data):
        if branch != "index-read" and matches(fd) and primary is not None:
            touched.append(True)
            raise primary
        return original_write(fd, data)

    snapshot = reserve(storage)
    tree = snapshot.capture(repository.path) if branch == "export-file" else None
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    monkeypatch.setattr(module.os, "read", read)
    monkeypatch.setattr(module.os, "write", write)
    expected = primary or close_error
    try:
        with pytest.raises(BaseException) as caught:
            if tree is None:
                snapshot.capture(repository.path)
            else:
                snapshot.export(tree)
        assert caught.value is expected
        assert len(closed) == 1
        if primary is not None:
            assert touched
            assert close_error in getattr(primary, "_transaction_secondary", [])
    finally:
        snapshot.close()
        snapshot.close()
    assert len(closed) == 1
    assert not (snapshot.path / ".footman-index").exists()


@pytest.mark.parametrize("primary_type", [None, OSError, KeyboardInterrupt])
def test_snapshot_independent_release_and_concurrent_repeat(
    storage, monkeypatch, primary_type
):
    module = api()
    snapshot = reserve(storage)
    primary = primary_type("body failure") if primary_type else None
    failure = OSError("snapshot close failure")
    original = module.PosixDescriptorOps.close
    seen = []

    def close(ops, fd):
        seen.append(fd)
        original(ops, fd)
        if len(seen) == 1:
            raise failure

    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    if primary is None:
        with pytest.raises(OSError) as caught:
            snapshot.close()
        assert caught.value is failure
    else:
        try:
            with snapshot:
                raise primary
        except BaseException as caught:
            assert caught is primary
            assert failure in getattr(primary, "_transaction_secondary", [])
    count = len(seen)
    assert count >= 2, "leaf and parent independently released"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: snapshot.close(), range(2)))
    assert len(seen) == count


@pytest.mark.parametrize("version", [2, 3, 4])
def test_index_versions_are_read_without_mutation(repository, storage, version):
    repository.git("update-index", f"--index-version={version}")
    before = repository.identity()
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == b"staged\n"
    repository.assert_unchanged(before)


def test_sha256_object_format(tmp_path, storage):
    path = tmp_path / "sha256"
    path.mkdir()
    run(["git", "init", "--quiet", "--object-format=sha256"], cwd=path)
    fixture = GitFixture(path)
    write_bytes(path / "payload", b"sha256 staged")
    fixture.git("add", "payload")
    index_before = read_bytes(path / ".git/index")
    with reserve(storage) as snapshot:
        tree = snapshot.capture(path)
        assert tree.object_format == "sha256" and len(tree.tree_id) == 64
        assert tree.head is None
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == b"sha256 staged"
    assert read_bytes(path / ".git/index") == index_before


def test_empty_unborn_repository_has_empty_staged_tree(tmp_path, storage):
    path = tmp_path / "unborn"
    path.mkdir()
    fixture = GitFixture(path)
    fixture.git("init", "--quiet")
    write_bytes(path / "untracked", b"excluded")
    with reserve(storage) as snapshot:
        tree = snapshot.capture(path)
        assert tree.head is None
        snapshot.export(tree)
        assert list(snapshot.path.iterdir()) == []
    assert not (path / ".git/index").exists()


@pytest.mark.parametrize("primary_type", [OSError, KeyboardInterrupt])
def test_reservation_setup_failure_releases_all_parents(
    storage, monkeypatch, primary_type
):
    module = api()
    primary = primary_type("mkdir failure")
    release_error = OSError("parent close failure")
    original_mkdir = module.PosixDescriptorOps.mkdirat
    original_close = module.PosixDescriptorOps.close
    closed = []

    def mkdir(ops, fd, name, mode):
        if name == RUN_ID:
            raise primary
        original_mkdir(ops, fd, name, mode)

    def close(ops, fd):
        closed.append(fd)
        original_close(ops, fd)
        if len(closed) == 1:
            raise release_error

    monkeypatch.setattr(module.PosixDescriptorOps, "mkdirat", mkdir)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(BaseException) as caught:
        reserve(storage)
    if primary_type is KeyboardInterrupt:
        assert caught.value is primary
    else:
        assert isinstance(caught.value, module.SnapshotError)
    assert release_error in getattr(caught.value, "_transaction_secondary", [])
    assert len(closed) == len(storage.parts)
    assert list(storage.iterdir()) == []


@pytest.mark.parametrize("branch", ["symlink", "directory", "mode"])
@pytest.mark.parametrize("primary_type", [OSError, KeyboardInterrupt])
def test_export_partial_setup_failure_and_independent_close(
    repository, storage, monkeypatch, branch, primary_type
):
    module = api()
    (repository.path / "nested").mkdir()
    write_bytes(repository.path / "nested/file", b"content")
    (repository.path / "link").symlink_to("payload")
    repository.git("add", "--all")
    snapshot = reserve(storage)
    tree = snapshot.capture(repository.path)
    before = repository.identity()
    primary = primary_type("export operation failed")
    secondary = OSError("independent close failed")
    original_close = module.PosixDescriptorOps.close
    closed = []
    target = {
        "symlink": (module.os, "symlink"),
        "directory": (module.PosixDescriptorOps, "mkdirat"),
        "mode": (module.PosixDescriptorOps, "fchmod"),
    }[branch]

    original_operation = getattr(*target)
    operation_failed = []
    release_failed = []

    def operation(*args, **kwargs):
        if branch == "mode" and not stat.S_ISREG(os.fstat(args[1]).st_mode):
            return original_operation(*args, **kwargs)
        operation_failed.append(True)
        raise primary

    def close(ops, fd):
        closed.append(fd)
        original_close(ops, fd)
        if operation_failed and not release_failed:
            release_failed.append(True)
            raise secondary

    monkeypatch.setattr(*target, operation)
    monkeypatch.setattr(module.PosixDescriptorOps, "close", close)
    with pytest.raises(BaseException) as caught:
        with snapshot:
            snapshot.export(tree)
    authority = caught.value
    if branch == "directory" and primary_type is OSError:
        assert isinstance(authority, module.SnapshotError)
    else:
        assert authority is primary
    # A failed newly-created directory may release itself before its error is
    # mapped; retain secondary diagnostics either on that operation or wrapper.
    assert secondary in getattr(
        authority, "_transaction_secondary", []
    ) or secondary in getattr(primary, "_transaction_secondary", [])
    assert len(closed) >= len(storage.parts) + 1
    count = len(closed)
    snapshot.close()
    assert len(closed) == count
    repository.assert_unchanged(before)


def test_managed_git_launch_failure_is_not_empty_success(
    repository, storage, monkeypatch
):
    module = api()
    original_launch = module.OwnedProcess.launch
    failure = OSError("Git cannot start")

    def unavailable(*args, **kwargs):
        raise failure

    def launch(argv, **kwargs):
        return original_launch(argv, factory=unavailable, **kwargs)

    monkeypatch.setattr(module.OwnedProcess, "launch", launch)
    with reserve(storage) as snapshot, pytest.raises(OSError) as caught:
        snapshot.capture(repository.path)
    assert caught.value is failure
    assert len(snapshot.processes) == 1
    assert snapshot.processes[0].cleanup_status.complete
    with pytest.raises(RuntimeError):
        snapshot.processes[0].capture()


@pytest.mark.parametrize(
    "outcome", ["read_failed", "aborted", "timeout", "incomplete", "close_failure"]
)
def test_git_terminal_observations_prevent_success(
    repository, storage, monkeypatch, outcome
):
    from dataclasses import replace

    from footman.process import (
        FailureInfo,
        FailureKind,
        FailurePhase,
        LifecycleResult,
        ProcessStream,
        StreamOutcome,
    )

    module = api()
    snapshot = reserve(storage)
    original = module.OwnedProcess.stream
    failure = OSError("Git pipe release failed")

    def stream(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        if outcome in ("read_failed", "aborted"):
            terminal = (
                StreamOutcome.READ_FAILED
                if outcome == "read_failed"
                else StreamOutcome.ABORTED
            )
            return replace(
                result,
                stream_outcomes=(
                    (ProcessStream.STDOUT, terminal),
                    (ProcessStream.STDERR, StreamOutcome.EOF),
                ),
            )
        if outcome == "timeout":
            return replace(result, timed_out=True)
        if outcome == "incomplete":
            return replace(result, lifecycle=LifecycleResult(False))
        return replace(
            result,
            lifecycle=LifecycleResult(
                True,
                (
                    FailureInfo(
                        failure,
                        FailureKind.ORDINARY,
                        FailurePhase.RELEASE,
                        ProcessStream.STDOUT,
                    ),
                ),
            ),
        )

    monkeypatch.setattr(module.OwnedProcess, "stream", stream)
    try:
        with pytest.raises((module.SnapshotError, OSError)) as caught:
            snapshot.capture(repository.path)
        if outcome == "close_failure":
            assert caught.value is failure
        assert snapshot.processes
        assert all(process.cleanup_status.complete for process in snapshot.processes)
    finally:
        snapshot.close()
        snapshot.close()


@pytest.mark.parametrize("kind", ["ordinary", "unexpected"])
def test_sink_failure_and_typed_process_cleanup_failure(
    repository, storage, monkeypatch, kind
):
    from dataclasses import replace

    from footman.process import (
        FailureInfo,
        FailureKind,
        FailurePhase,
        LifecycleResult,
        ProcessStream,
    )

    module = api()
    snapshot = reserve(storage)
    tree = snapshot.capture(repository.path)
    original_stream = module.OwnedProcess.stream
    primary = OSError("sink failed despite command zero")
    secondary = OSError("pipe release failed")

    # First ls-tree must succeed; only replace lifecycle observations after the
    # sink write actually failed, so this tests export persistence, not parsing.
    def after_write_stream(owner, *args, **kwargs):
        result = original_stream(owner, *args, **kwargs)
        if getattr(primary, "seen", False):
            failure_kind = (
                FailureKind.ORDINARY if kind == "ordinary" else FailureKind.UNEXPECTED
            )
            return replace(
                result,
                lifecycle=LifecycleResult(
                    True,
                    (
                        FailureInfo(
                            secondary,
                            failure_kind,
                            FailurePhase.RELEASE,
                            ProcessStream.STDOUT,
                        ),
                    ),
                ),
            )
        return result

    def writer(fd, data):
        primary.seen = True
        raise primary

    monkeypatch.setattr(module, "_write_all", writer)
    monkeypatch.setattr(module.OwnedProcess, "stream", after_write_stream)
    try:
        with pytest.raises(OSError) as caught:
            snapshot.export(tree)
        expected, displaced = (
            (primary, secondary) if kind == "ordinary" else (secondary, primary)
        )
        assert caught.value is expected
        assert displaced in getattr(expected, "_transaction_secondary", [])
        assert all(process.cleanup_status.complete for process in snapshot.processes)
    finally:
        snapshot.close()


def test_fsmonitor_and_environment_config_cannot_run_consumer_code(
    repository, storage, monkeypatch
):
    marker = repository.path.parent / "fsmonitor-ran"
    script = repository.path.parent / "poison-fsmonitor"
    write_bytes(script, f"#!/bin/sh\ntouch '{marker}'\nexit 0\n".encode(), mode=0o700)
    before = repository.identity()
    repository.git("config", "core.fsmonitor", str(script))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(script))
    try:
        with reserve(storage) as snapshot:
            snapshot.export(snapshot.capture(repository.path))
        assert not marker.exists()
    finally:
        repository.git("config", "--unset", "core.fsmonitor")
    repository.assert_unchanged(before)


def test_chain_resolution_not_lexical_dotdot(repository, storage):
    (repository.path / "alias").symlink_to(".")
    (repository.path / "escape").symlink_to("alias/../payload")
    repository.git("add", "alias", "escape")
    with (
        reserve(storage) as snapshot,
        pytest.raises(api().SnapshotError, match="symlink"),
    ):
        snapshot.export(snapshot.capture(repository.path))


def test_independent_requests_and_one_capture_export_attempt(repository, storage):
    second_id = RUN_ID[:-1] + "1"
    with reserve(storage) as first, reserve(storage, second_id) as second:
        first_tree = first.capture(repository.path)
        write_bytes(repository.path / "payload", b"second staged")
        repository.git("add", "payload")
        second_tree = second.capture(repository.path)
        assert first_tree.tree_id != second_tree.tree_id
        first.export(first_tree)
        second.export(second_tree)
        assert read_bytes(first.path / "payload") == b"staged\n"
        assert read_bytes(second.path / "payload") == b"second staged"
        with pytest.raises(RuntimeError):
            first.capture(repository.path)
        with pytest.raises(RuntimeError):
            first.export(first_tree)
        with pytest.raises(RuntimeError):
            second.export(first_tree)


@pytest.mark.parametrize("primary_type", [None, OSError, KeyboardInterrupt])
@pytest.mark.parametrize("failed_name", [".footman-index", ".footman-index.lock"])
def test_private_unlink_failure_is_visible_once(
    repository, storage, monkeypatch, primary_type, failed_name
):
    module = api()
    snapshot = reserve(storage)
    primary = primary_type("private index operation") if primary_type else None
    secondary = OSError("cannot unlink private index")
    original_write = module._write_all
    original_unlink = module.DirectoryDescriptor.unlink_child
    attempted = []

    def write(fd, data):
        if primary is not None:
            raise primary
        return original_write(fd, data)

    def unlink(owner, name):
        attempted.append(name)
        if name == failed_name:
            raise secondary
        return original_unlink(owner, name)

    monkeypatch.setattr(module, "_write_all", write)
    monkeypatch.setattr(module.DirectoryDescriptor, "unlink_child", unlink)
    try:
        with pytest.raises(BaseException) as caught:
            snapshot.capture(repository.path)
        assert caught.value is (primary or secondary)
        if primary is not None:
            assert secondary in getattr(primary, "_transaction_secondary", [])
        assert attempted == [".footman-index", ".footman-index.lock"]
        assert (snapshot.path / ".footman-index").exists() == (
            failed_name == ".footman-index"
        )
    finally:
        snapshot.close()
        snapshot.close()
    assert attempted == [".footman-index", ".footman-index.lock"]


def test_index_open_must_not_block_on_special_files(repository, storage, monkeypatch):
    module = api()
    original = module.PosixDescriptorOps.openat

    def openat(ops, parent, name, flags, mode=0):
        if name == "index":
            assert flags & os.O_NONBLOCK, "index open can block before type validation"
        return original(ops, parent, name, flags, mode)

    monkeypatch.setattr(module.PosixDescriptorOps, "openat", openat)
    with reserve(storage) as snapshot:
        snapshot.capture(repository.path)


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_special_index_rejected_without_following(repository, storage, kind):
    index = repository.path / ".git/index"
    saved = index.with_name("saved-index")
    index.rename(saved)
    if kind == "fifo":
        os.mkfifo(index)
    else:
        index.symlink_to(saved)
    with reserve(storage) as snapshot, pytest.raises((api().SnapshotError, OSError)):
        snapshot.capture(repository.path)
    assert read_bytes(saved).startswith(b"DIRC")


def test_unsettled_git_retains_private_paths_and_same_process_owner(
    repository, storage, monkeypatch
):
    from dataclasses import replace

    from footman.process import LifecycleResult

    module = api()
    snapshot = reserve(storage)
    original_stream = module.OwnedProcess.stream
    original_status = module.OwnedProcess.cleanup_status
    pending = []

    def stream(owner, *args, **kwargs):
        result = original_stream(owner, *args, **kwargs)
        if (snapshot.path / ".footman-index").exists():
            pending.append(owner)
            return replace(result, lifecycle=LifecycleResult(False))
        return result

    def status(owner):
        if any(owner is item for item in pending):
            return LifecycleResult(False)
        return original_status.__get__(owner)

    monkeypatch.setattr(module.OwnedProcess, "stream", stream)
    monkeypatch.setattr(module.OwnedProcess, "cleanup_status", property(status))
    try:
        with pytest.raises(module.SnapshotError):
            snapshot.capture(repository.path)
        assert pending and any(process is pending[0] for process in snapshot.processes)
        assert (snapshot.path / ".footman-index").exists(), (
            "live helper paths must not be unlinked"
        )
        owners = snapshot.processes
        with pytest.raises(module.SnapshotError):
            snapshot.close()
        snapshot.close()
        assert snapshot.processes == owners
        assert (snapshot.path / ".footman-index").exists()
    finally:
        # The injected observation is deliberately incomplete; the real local
        # child was collected and settled. Restore truthful observations.
        monkeypatch.undo()
        assert all(process.cleanup_status.complete for process in snapshot.processes)
        snapshot.close()


def test_valid_index_skip_hash_is_captured_without_mutation(repository, storage):
    repository.git("config", "index.skipHash", "true")
    write_bytes(repository.path / "payload", b"skip-hash staged")
    repository.git("add", "payload")
    original_index = read_bytes(repository.path / ".git/index")
    assert original_index[-20:] == bytes(20)
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == b"skip-hash staged"
    assert read_bytes(repository.path / ".git/index") == original_index


def test_partially_staged_inputs_only(repository, storage):
    write_bytes(repository.path / "payload", b"staged portion")
    repository.git("add", "payload")
    write_bytes(
        repository.path / "payload", b"unstaged portion already present before capture"
    )
    write_bytes(repository.path / "addition", b"staged addition")
    repository.git("add", "addition")
    before = repository.identity()
    expected = b"staged portion"
    if os.environ.get("FOOTMAN_SNAPSHOT_CONTROL") == "workdir-bytes":
        expected = read_bytes(repository.path / "payload")
    with reserve(storage) as snapshot:
        tree = snapshot.capture(repository.path)
        snapshot.export(tree)
        assert read_bytes(snapshot.path / "payload") == expected
        assert read_bytes(snapshot.path / "addition") == b"staged addition"
    repository.assert_unchanged(before)
