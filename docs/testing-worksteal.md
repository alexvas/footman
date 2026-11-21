# Worksteal adoption: historical validation record

This is a deliberately retained, self-contained **historical adoption record**,
not a current-candidate verification certificate. Current commands, resource
ownership and evidence applicability rules remain in [testing.md](testing.md) and
[python-checks.md](python-checks.md). A later code/test/configuration change
requires fresh affected checks; the 1000 runs below are not repeated or implied
by a later green gate. Historical planning commands and previous reports may
still name `loadfile`; they must not be relabelled as worksteal executions.

## Decision

At adoption time, the decision was to use `pytest-xdist --dist worksteal` for
the full gate, registered specialised suites and parallel skip-policy probes,
retaining the then-current eight-worker gate allocation and registered
specialised worker counts. That adoption also retained the former separate
gate-plus-specialised admission budget and minimum-32-available-CPU condition.
These are historical decisions, not current scheduling instructions.

Worksteal balances pending tests inside a pytest invocation. For the current
three-group gate allocation (`auto`, `8`, `8`) and the requirement to settle G
before starting the specialised queue, follow
[the current testing schedule](testing.md#working-schedule-gate-plus-specialised-suites).

Tests from a single file may run in different interpreters. Module/session
fixtures can initialize in several workers; they are not globally unique.
Mutable resources and timing assumptions must be isolated per test/worker;
file-wide worker affinity or shared module state is not a supported contract.
Skip/xfail/xpass rejection, exact-command guards, full-suite scope, first-failure
preservation and owned cleanup are not relaxed by this scheduler change.

## Checked candidate and environment

The stress series ran on revision
`1482996d8829836ebd66a43d21be8938ef6b38f0` **with local changes**, not the committed
revision alone. Relevant changes included owned-stdin fixes, split launcher and
lifecycle collection modules, inventory guards and scheduling documentation.
It preceded the documentation adoption of worksteal and its new
scheduler-drift negative tests. No code/test/configuration or index edits were
made during the series; content/inventory fingerprints matched before, between
and after runs across `src/`, `tests/`, `scripts/`, `docs/`, `examples/`,
`openspec/`, `pyproject.toml`, `uv.lock`, `README.md` and `AGENTS.md`.

- Linux 7.0.0-43-generic x86_64, glibc 2.41; Docker-free development environment.
- Python 3.14.7; pytest 8.4.2; pytest-xdist 3.8.0.
- Git 2.47.3; uv 0.12.24; Ruff 0.17.1; ty 0.0.73.
- 32 logical CPUs, affinity CPUs 0–31; visible `cpu.max`: `max 100000`.
- `cpuset.cpus.effective` was unavailable; CPU accounting resolution 100 Hz.
- No real Docker image/container, MCP delivery or host acceptance was tested.

## Exact execution and results

From `/home/aavasiljev/work/home/footman.git`, run the following command **1000
separate times, sequentially**, with `NNNN` replaced by `0001` through `1000`:

```sh
.venv/bin/python -m pytest tests -n 8 --dist worksteal --tb=short -q --junitxml=/tmp/gorshochek/ephemeral/footman.git/verification/worksteal-stress-cpu-20261110T160349/run-NNNN.xml
```

This is the literal historical invocation with a run-specific report destination,
not an additional required gate command or a reusable scratch-path instruction.
The external driver used one `OwnedProcess` per invocation, one `stream()` call
for both outputs and existing filesystem owners to persist full live logs.
Per-command deadline: 900 s; cleanup grace: 10 s. Driver budget: 42600 s with
60 s reserve; outer execution timeout: 43200 s. Failure, timeout, incomplete
cleanup or candidate-content change would stop the series for diagnosis.

Series: **2026-11-10 16:03:49–22:49:48 UTC**.

| Result | Observation |
| --- | --- |
| Completed invocations | **1000 / 1000, all exit 0** |
| Each invocation | **2185 passed** |
| Total test executions | **2,185,000**; 2185 distinct cases repeated 1000 times |
| Case identity/outcome comparison | All JUnit multisets equal the first successful run |
| Failures/errors/skips/xfails/xpasses | **0** |
| Warnings in complete logs | None observed |
| Timeout/incomplete owner cleanup/lifecycle failures | **0** |
| Source/test/configuration fixes needed | None |
| Whole-series wall time, including between-run work | **24359.09 s (6h45m59s)** |
| Median invocation wall time | **24.2428 s** |
| Fastest / P95 / slowest invocation | **23.9177 / 24.6393 / 25.1334 s** |

No test filtering, warning suppression or registered-plugin disabling was used.
The earlier user-interrupted no-CPU series completed three invocations; its
fourth partial run is not counted, and no numeric aborted status is invented.
Historical RED was not reconstructed or counted as passing verification.

A preceding three-runs-per-mode comparison on the same implementation measured
median complete pytest-command wall times of **25.69 s for loadfile** and
**24.23 s for worksteal**, both with eight workers and identical JUnit reporting.
All cases passed and outcome sets matched. Observed improvement was approximately
**5.7%**, not a guarantee for another candidate/machine or the combined gate-plus-
specialised schedule. Those earlier whole-schedule timing comparisons used
loadfile, not worksteal; do not transfer their exact timing claim to the new mode.

## CPU and I/O observations

For every invocation, `/proc/stat` snapshots before launch and after owned
settlement measured interval utilisation. A further snapshot pair with a requested
**50 ms wait** measured the gap after each run (including one final post-run
sample). Per-CPU, system aggregate, affinity aggregate, iowait/steal, load average
and visible cgroup usage/throttling deltas were retained.

| Metric | Observation |
| --- | --- |
| Median busy during a run, normalized across 32 affinity CPUs | **10.0845%** |
| Mean busy during a run | 10.0973% |
| Median equivalent busy CPU cores | **3.2270** |
| Median visible-cgroup average busy CPU cores | 3.0381 |
| Median busy during short post-run samples | **0.6211%** |
| Median affinity iowait during a run | **25.0781%** |
| Observed cgroup throttling events / throttled microseconds | **0 / 0** |

Busy excludes idle and iowait; percentages describe total available CPU capacity,
not one CPU or one worker. These are interval means, not peak utilisation.
System counters may include other tasks; short gap samples are tick-granular and
noisy. The operator reports that the machine was occupied exclusively by these
tests. Together with low measured CPU utilisation and high iowait, this points
to I/O as the next investigation target rather than a lack of CPU capacity; it
does not yet identify the responsible files, devices or operations.

### Deferred work: identify and reduce test I/O

I/O optimization is **pending**, not part of scheduler adoption:

1. Attribute I/O to test-owned processes/devices and lifecycle phases using
   suitable process/cgroup I/O counters, pressure metrics and storage latency
   measurements; separate Git preparation, snapshot export, persistence and
   repeated integration-child startup costs.
2. Measure opportunities for independent copies of prepared Git fixtures and
   reductions in redundant setup. A previous small experiment found roughly
   105 ms fresh preparation versus 21 ms copying, with capture/export unchanged;
   that is not yet a fully fault-tested shared-fixture implementation.
3. Preserve real Git/index isolation, actual copy/permission/symlink checks,
   persistence semantics and all operation/close/interruption/repeat-cleanup
   contracts. Do not optimize by dropping fsync assertions, weakening timeouts,
   sharing writable fixture state or replacing required integration paths with
   fake-only tests. Benchmark storage/tmpfs changes separately before adoption.

## Resource reconciliation, evidence and limitations

All 1000 command owners reported clean EOF on both streams and complete cleanup;
existing fixture lifecycle assertions passed. A post-series read-only `/proc`
observation found no commands referencing the run directory. That is not proof
about every system descriptor or unrelated process. Synthetic alias-retention
cases remain deliberately contained in their isolated child, not reclassified
as successful production cleanup. Pytest scratch remains fixture-owned; no broad
cleanup, independent signal/reap or real-container deletion was performed.

Raw logs, JUnit files, per-run JSON, `reference-cases.json`, fingerprints,
`summary.json`, `metrics.json` and `cpu-and-results.csv` were retained under
`/tmp/gorshochek/ephemeral/footman.git/verification/worksteal-stress-cpu-20261110T160349/`.
The driver and full driver output were adjacent in the verification directory;
its exit status was 0. These are optional ephemeral diagnostic artifacts (about
353 MB before the final CSV/report), not permanent prerequisites or durable
links on which this document depends. This summary retains the essential facts
if those artifacts disappear. Evidence and earlier benchmark/smoke scratch were
intentionally retained, not claimed deleted.

One post-processing attempt used system `python3` instead of the project venv
and exited 1 with missing `footman`; rerunning aggregation with `.venv/bin/python`
exited 0. This reporting setup failure is separate from the 1000 test statuses.

1000 clean repetitions are strong observed stability evidence, not proof of all
possible schedules, fixture initialization counts or absence of rare races.
The historical worksteal experiment was not the then-mandated loadfile green
gate and did not rerun lint/type/build/help/install checks. Adoption changes must
pass the **current** worksteal gate and registered specialised commands on their
own final candidate; that verification belongs in a fresh ephemeral summary,
not by silently updating these historical results.
