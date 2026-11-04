# Staged snapshots

Snapshots contain one captured staged Git tree, not the current working tree.
The internal snapshot adapter is not a public command-execution CLI.

## Identity and exclusive ownership

After output reservation, reserve exactly
`<canonical snapshot_directory>/<run_id>`. The run ID is the request's fixed
26-character Crockford UUIDv7 identity; it is not regenerated on collision.
Storage must remain outside the development repository and retained output.
Configuration establishes that separation; per-request reservation walks the
canonical snapshot root with no-follow directory capabilities. Missing shared
parents are created with mode 0700; existing shared parents keep their modes.
Existing directories,
files and symlinks at the final path are failures, never reused or renamed.
Only the successful reservation acquires capabilities. A losing request never
exports into, modifies or deletes the winner's directory. A concurrent shared-
parent creation may be reopened without changing its permissions; a final run-
directory collision is never reopened. A failed leaf reservation does not undo
shared-parent creation that already succeeded, or delete an injected sibling.

Reservation compares the acquired root and run-directory capability paths
(`/proc/self/fd`) with their configured canonical paths during the walk. Capture
and export recheck the run-directory capability at entry. These checks detect
already-moved/removed directories and walk-time symlink parent replacement
before source capture or export writes. They are path comparisons at those
boundaries, **not** a lease against later renames: an authorized party could move
an open directory after a check. Operators must protect storage parents against
unauthorized renames or mutation. Writes remain relative to the acquired
no-follow capabilities; this is not a hostile-code sandbox.

Reservations retain their sole directory owners and managed Git process owners.
Closing a reservation releases capabilities once, including independent releases
on error/interruption; it does **not** delete directory entries. Failed exports
may leave a partial owned snapshot. Snapshot deletion must be coordinated with
confirmed container removal by the run supervisor (Phase 6), not guessed from a
Git child or Docker client's exit. Incomplete Git helper cleanup retains the same
process owners for `cleanup_status` / bounded `wait_cleanup` observations; do not
recapture, relaunch or grant fresh cleanup budgets.

## Capture

Git redirection variables inherited from the environment are removed. System and
global Git configuration are disabled, along with fsmonitor/hooks, optional
locking, replacement objects and lazy fetching. Repository configuration is not
executed as dispatcher configuration. Ordinary and linked worktrees use Git's
actual per-worktree index location.

One no-follow-opened index version is copied to an exclusively created private
index inside the reserved directory. Exact read-length and size/time checks,
plus Git's index checksum when present, reject inconsistent/in-place capture.
The valid all-zero checksum written by `index.skipHash` is supported; open-version
size/time and structural validation still apply. Atomic index replacement may leave the
opened old inode unlinked; that complete old version remains valid. Subsequent
staging and workdir changes cannot alter the captured tree. Index formats 2, 3
and 4 and SHA-1/SHA-256 object formats are supported. Split indexes are explicitly
unsupported and fail before tree capture/export; their shared-index references
are not silently lost or resolved against a different directory. An absent index
is the observed empty staged state, including an unborn repository.

Unresolved entries fail explicitly. `write-tree` receives `GIT_INDEX_FILE`
pointing **only** at the private copy; it may create Git tree objects but never
commits, refs or worktrees. Development index bytes, workdir, HEAD and branches
are not changed. The recorded full tree ID and object format identify the input;
optional HEAD is context, not the tested identity. The private index and its
possible lock are unlinked independently before export; failure remains visible.

## Raw export and limitations

Export uses raw NUL-delimited `ls-tree` entries and streamed `cat-file blob`
bytes. It does not run checkout, archive, smudge/clean filters, hooks, staged
scripts, `export-ignore` or `export-subst`. No consumer executable runs on the
host. It includes staged additions/modifications/deletions and excludes
unstaged-only, untracked and ignored build artifacts. There is no `.git`.

- `100644` and `100755`: exclusive regular files with exact blob bytes and
  final permissions 0644 and 0755 respectively.
- `040000`: directories with safe host permissions 0700. Git stores no source
  directory permission metadata to preserve.
- `120000`: actual symlinks with exact filesystem-byte targets, not regular
  files containing the target text. Validate targets/chains against the complete
  captured tree before creating links. Absolute/outward targets, cycles, missing
  targets, file-as-directory traversal and unprovable containment are rejected.
  Targets longer than 4095 bytes or chains exceeding 40 link traversals are
  explicitly unsupported.
- `160000` gitlinks and unsupported modes/types are rejected, not coerced.

Spaces, tabs, newlines and non-UTF-8 filesystem-byte names/targets are preserved
on POSIX. Empty/traversal/absolute paths, `.git` components, duplicate paths,
non-directory ancestors and components longer than 255 bytes fail explicitly.
Tree records over 1 MiB fail rather than being silently truncated. All writes
use directory capabilities, no-follow/exclusive file creation and sole descriptor
owners; symlinks are created last, so export never writes through an exported
link. No arbitrary source directory mode or checkout conversion is fabricated.

The operator-prepared image and configured command must supply generated inputs,
dependencies and any required build steps. Builds requiring development Git
metadata are unsupported. Staging is not a security approval, and these snapshots
do not claim hermeticity or that later workdir edits were tested.

The [Phase 4 suite](testing.md#phase-4-staged-snapshot-suite) uses real disposable
Git repositories and filesystem resources without Docker. Its
`test_dev_snapshot_mount_identity` is the historical export-only node name and
proves staged-byte identity, not a mount. Phase 5's `dev_snapshot_copy_identity`
checks this host export as the literal Docker copy source. Actual in-container
copy visibility, bytes/modes/symlinks and execution remain Phase 10 obligations.
