# test-build-admission Specification

## Purpose

Provide bounded, session-isolated concurrency for independent test builds and provisioning while preserving original deadlines.

## Requirements

### Requirement: Session-wide bounded capacity
Each pytest invocation SHALL establish one immutable admission capacity shared by all its workers, including replacements. Capacity SHALL be bounded by the resolved worker count and the available CPU budget rounded down with a minimum of one. Unknown CPU capacity SHALL conservatively yield one slot. Separate invocations SHALL use disjoint admission namespaces.

#### Scenario: Multiple workers share a pool
- **WHEN** workers start or a worker is replaced
- **THEN** they receive the same capacity and admission namespace

#### Scenario: Automatic worker selection
- **WHEN** xdist resolves automatic or logical worker selection, including a maximum-worker cap
- **THEN** admission capacity is bounded by the final planned worker count
- **AND** replacement workers retain the original admission decision

#### Scenario: Nondistributed execution
- **WHEN** pytest runs in nondistributed mode
- **THEN** admission uses local configuration with a worker-count bound of one

#### Scenario: Restricted or unknown CPU budget
- **WHEN** CPU affinity or an effective container quota limits capacity, or available capacity cannot be established
- **THEN** the limit respects the restriction or conservatively uses one slot, with a minimum capacity of one for fractional CPU quotas

#### Scenario: Independent sessions
- **WHEN** two pytest invocations establish their admission resources
- **THEN** each invocation's slots have distinct file identities and independent capacity

### Requirement: Free-slot admission
An admission request SHALL search the session pool for any free slot. At most the configured number of requests SHALL hold admission simultaneously. When all slots are busy, a request SHALL wait only within its original deadline. Ordering among competing callers is implementation-defined.

#### Scenario: Earlier slot is busy
- **WHEN** one slot is held and another slot is free
- **THEN** another request can enter while the first slot remains held

#### Scenario: Pool is full
- **WHEN** all K slots are held and another request arrives
- **THEN** the extra request waits for release, then may enter while the number of simultaneous admitted requests remains at most K

### Requirement: Original deadline accounting
Admission SHALL charge probing, waiting and retries to the caller's original absolute deadline. An exhausted deadline SHALL prevent entry into protected work, including exhaustion during acquisition. Build, workspace, host and cleanup allowances SHALL retain their original budgets throughout admission.

#### Scenario: Waiting spends the allowance
- **WHEN** contention consumes part of a caller's allowance
- **THEN** subsequent commands receive only the remaining allowance

#### Scenario: Exact or past exhaustion
- **WHEN** the deadline is reached before protected work begins
- **THEN** admission raises timeout before protected entry

### Requirement: Explicit pool admission
Test callers SHALL use an explicit immutable pool with deadline-bounded acquisition. Pool construction SHALL create a resource-free configuration description. Single-slot admission SHALL use capacity one. The legacy single-path admission API SHALL be removed after callers migrate; the explicit directory SHALL define pool namespace identity.

#### Scenario: Single-slot pool
- **WHEN** two independent callers acquire the same capacity-one pool
- **THEN** their protected work is mutually exclusive

#### Scenario: Resource-free construction
- **WHEN** a caller constructs a valid pool description
- **THEN** construction completes using configuration values alone, with filesystem work deferred to acquisition

### Requirement: Preserve heavy-work admission boundaries
Existing heavy-work boundaries and mandatory direct-workspace admission SHALL remain enforced, including worker-session setup before function-scoped fixtures. Existing intentional attribution-only bypasses SHALL remain explicit caller branches; acquisition SHALL require a configured pool.

#### Scenario: Missing workspace admission
- **WHEN** direct workspace provisioning receives a missing-admission configuration
- **THEN** it reports a configuration failure before provisioning

#### Scenario: Session installation precedes autouse setup
- **WHEN** a worker installation is first requested before function-scoped admission setup
- **THEN** its heavy work still enters the same session pool used by host preparation, wrapper calls, clone copies and workspace provisioning

### Requirement: Secure sole-owner resource lifecycle
Admission SHALL reject unsafe directory or slot nodes and SHALL assign exactly one filesystem owner to each acquired descriptor. All independent releases SHALL be attempted exactly once. An active operation failure or interruption SHALL retain precedence over ordinary close failures; a sole close failure SHALL remain visible. Resource owners SHALL perform explicit release, with repeat cleanup observing the original cleanup state.

#### Scenario: Busy probe and later failure
- **WHEN** a busy slot has been probed and a later probe fails or is interrupted
- **THEN** all acquired probe resources are released once and the primary failure retains precedence

#### Scenario: Unsafe slot
- **WHEN** acquisition encounters a symlink, non-regular node or wrongly owned slot
- **THEN** it terminates admission with a security failure

#### Scenario: Release and repeat cleanup
- **WHEN** protected work succeeds, fails or is interrupted and cleanup is observed again
- **THEN** each descriptor is released once, other independent releases are attempted, and repeat cleanup observes the original cleanup state

#### Scenario: Sole close failure
- **WHEN** work succeeds but releasing an admission resource fails
- **THEN** the invocation reports the close failure

### Requirement: Supported operating-system boundary
Admission SHALL support an explicitly supplied OS adapter for descriptor operations, nonblocking locking, monotonic time and waiting. The default SHALL use the real OS. Substitution SHALL occur through the explicitly supplied adapter while admission policy and filesystem owners remain active.

#### Scenario: Fault-injected acquisition
- **WHEN** a caller acquires through an adapter that reports an operation or release failure
- **THEN** the real admission path reports the specified error precedence and attempts each independent release exactly once

#### Scenario: Controlled policy time
- **WHEN** the adapter advances monotonic time while admission waits
- **THEN** the supplied monotonic observations determine deadline-governed entry
