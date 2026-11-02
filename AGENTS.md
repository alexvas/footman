# Repository agent rules

Read and follow [docs/testing.md](docs/testing.md) for testing instructions.
Run `scripts/check-green` from the repository root on the final candidate state;
it runs the Ruff lint, Ruff format, syntax-compilation and `ty` checks over one
deterministic Python inventory, then the mandated full-suite parallel pytest
command. Attempt every independent check, preserve the first failing status, and
never add scope or fix arguments to narrow it. See
[docs/python-checks.md](docs/python-checks.md) for installation, source
selection, parallelism and the reviewed suppressions.
G applies to a delivered GREEN/INTROSPECT/VALIDATE candidate state and requires
all mandated checks to pass without skips, xfails or xpasses. A phase suite's
documented RED recording is exempt from G: capture its exact command, complete
output and actual nonzero exit status, and establish that failures are due to
missing required behavior. RED evidence is not passing verification. Never
weaken requirements, skip/xfail tests or disable the registered pytest plugin
to obtain G; there are no approved skip exceptions. Preserve all exit statuses.

## Descriptor and stream lifecycle (mandatory)

Any new code that acquires an operating-system descriptor or an already
fd-owning stream MUST name exactly one `footman.filesystem` owner and release
through that owner:

- **raw integer fd** (`os.open`, `os.dup`, `os.pipe`, `tempfile.mkstemp`, or a
  factory returning an fd): adopt it immediately into `OwnedDescriptor`, with
  constructor inputs prepared before the acquisition;
- **directory**: use the `DirectoryDescriptor` capability
  (`open_secure_path`/`adopt` and its child methods);
- **subprocess coordination**: use `footman.process.OwnedProcess.launch`,
  which prepares one internal `OwnedStream` per captured pipe; never compose
  unprotected launch/adoption/readers or independently signal, wait/reap or
  release its borrowed child/pipes;
- **other already fd-owning stream**: adopt it into `OwnedStream`; do not
  also adopt its `fileno()`;
- **Python file object over a raw fd**: use
  `os.fdopen(owner.fd, ..., closefd=False)` and keep the `OwnedDescriptor` as
  the sole release authority.

Never retry a close, never give one fd two owners, and never rely on garbage
collection or a borrowed stream for release. Preserve an active
operation/domain failure over an ordinary close failure, and attempt every
independent release exactly once. Import explicit `footman.filesystem`
submodules, never the aggregate package.

Select configured `StopPolicy` stages at launch, not by overriding shutdown.
Stage work and child waiting share one total shutdown budget and local-cleanup
reserve. Incomplete cleanup retains the same owners and continuation; observe
it through `cleanup_status` / `wait_cleanup`, without retry or new budgets.
Managed action `done` and `cleanup_complete` are separate observations: local
child exit proves neither external-work success nor settled helper resources.
See `docs/process-lifecycle.md`; process/domain policy never belongs in the
filesystem foundation.

Every new acquisition, transfer, wrapper, or cleanup branch MUST add tests for
operation-plus-close failure, sole-close failure, interruption, and repeat
cleanup, plus independent cleanup, handoff, or concurrency where applicable.

See `docs/filesystem-lifecycle.md` for the full contract and illustrative
examples. Lifecycle tests live in `tests/test_filesystem_*.py` and
`tests/test_process_*.py`. Guard suites are `tests/test_green_skip_policy.py`,
`tests/test_green_gate.py` and `tests/test_lifecycle_doc_links.py`; the ported
check-tooling contracts are `tests/test_python_*.py`; see `docs/testing.md` for
their scope.
