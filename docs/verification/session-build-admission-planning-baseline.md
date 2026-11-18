# Session build admission: historical planning baseline

This is an explicitly retained repository historical summary, serving the same historical-only role as docs/testing-worksteal.md. It transcribes the planning baseline rather than certifying a current candidate. Current-candidate reports follow docs/testing.md and live outside the repository under `/tmp/gorshochek/ephemeral/footman.git/verification/`.

- Checked revision: `fdd84bcb9798517d39500f8646910340c2943d23`.
- Local state reported for that run: only untracked `openspec/changes/add-session-build-admission-pool/` before and after the command.
- Working directory: repository root.
- Exact command: `GREEN_CHECKS_WORKERS=8 scripts/check-green`.
- Reported command exit status: 0.
- Reported checks: lint, format, syntax compilation and types passed; full pytest reported 4548 passed in 492.44 seconds, zero skips/xfails/xpasses.
- Reported total wall time: 494 seconds.
- Significant warnings: none reported in the historical summary.
- Environment limitations: affinity, cgroup quota, resolved admission capacity and detailed tool versions were not captured in that summary. The result supports historical correctness and descriptive wall-time/check-count comparisons. CPU/capacity-controlled comparisons require a fresh baseline with those observations and comparable conditions.
- Log limitation: the original complete log was temporary; this record has no durable full-log attachment. Individual check statuses were summarized as passed; their numeric statuses were not separately preserved in the planning prose.

Future checks must record the actual candidate revision, local changes, environment and results separately. Current applicability depends on checked code/tests/configuration and environment, rather than staging or documentation changes.
