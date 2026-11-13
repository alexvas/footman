# dispatch-deadline-verification Specification

## Purpose

Define trustworthy, scheduler-independent evidence for dispatcher deadline enforcement. Separate launch admission, real child timeout, child-written diagnostics and cleanup so that an interrupted diagnostic cannot determine whether the lifecycle contract passed.

## Requirements

### Requirement: Parent-side launch evidence
Deadline verification SHALL distinguish requested launches, acquired child owners and failed acquisition. It SHALL compare ordered working argv against the expected execution prefix and account separately for cleanup launches. Child-written files SHALL NOT determine whether a launch was requested.

#### Scenario: Child never reaches its recording point
- **WHEN** an acquired execution helper times out before producing a child record
- **THEN** its parent-side launch remains visible and is distinguishable from a failed or unrequested launch

#### Scenario: Unexpected working invocation
- **WHEN** an additional, reordered or operand-mismatched working launch is requested
- **THEN** verification fails even if no child record was written

### Requirement: Deterministic execution admission boundary
Deadline-policy verification SHALL control elapsed policy time explicitly and SHALL reject the next working launch when the shared execution budget is exhausted. Clock reads and scheduler pauses SHALL NOT advance policy time. Cleanup SHALL remain independently observable under its own allowance.

#### Scenario: Expiry before inspection, directory creation or copying
- **WHEN** a controlled scenario exhausts the execution budget after the preceding successful operation at each of those three boundaries
- **THEN** the next working launch is absent from the parent journal, the exact preceding argv prefix is preserved, and known-ID removal and confirmation are still attempted in order

#### Scenario: Exact exhaustion and positive remainder
- **WHEN** the controlled clock reaches the deadline exactly or passes it
- **THEN** the next working launch is forbidden
- **AND** a separate positive-remainder control demonstrates that the same launch is admitted before exhaustion

### Requirement: Cumulative execution allowance
Verification SHALL demonstrate that successive working helpers receive the remaining shared execution allowance rather than a fresh per-helper timeout. Scenario expenditures SHALL be explicit and independent of wall-clock sleeps.

#### Scenario: Multiple operations spend the budget
- **WHEN** successful operations explicitly consume known portions of one execution allowance
- **THEN** later launch allowances match the remaining budget, decrease with those expenditures, and no working launch is requested after exhaustion

### Requirement: Positive real-process timeout evidence
The real-process deadline check SHALL require a positively observed execution timeout and completed local child/resource cleanup. An arbitrary run failure SHALL NOT satisfy that check. The check SHALL NOT require timeout at a fixed execution stage or require the child to reach its recording point.

#### Scenario: Timeout interrupts a helper
- **WHEN** a real helper exceeds its execution allowance
- **THEN** verification requires its timeout result, completed local cleanup and absence of subsequent working launches

#### Scenario: Non-timeout failure
- **WHEN** acquisition, imports or execution fail without positive timeout evidence, or resource cleanup remains incomplete
- **THEN** the real-process timeout check fails with the available launch and result diagnostics

### Requirement: Scoped child-record interpretation
A child record SHALL be mandatory and validated for successful helpers and cleanup. An absent or incomplete record SHALL be tolerated only for an individually identified, acquired execution helper with positive timeout evidence and completed local cleanup. A complete record SHALL still be validated; record tolerance SHALL NOT become a global missing-file filter.

#### Scenario: Timed-out helper has no complete record
- **WHEN** the identified timed-out execution helper has an absent or incomplete record
- **THEN** verification reports that record as unavailable or incomplete without treating it as proof of child execution or failing solely for its absence

#### Scenario: Required or complete evidence is invalid
- **WHEN** a successful or cleanup helper lacks a valid complete record, or a complete record contains unexpected argv or reports stdin not at EOF
- **THEN** verification fails rather than silently omitting that helper

### Requirement: Identity-aware cleanup verification
Deadline verification SHALL distinguish local settlement from external-resource attribution. With a known container identity it SHALL require exact recorded-ID removal and successful empty-listing confirmation before snapshot deletion. Without an attributable identity it SHALL require unresolved-resource reporting, snapshot retention and no guessed-ID cleanup launch.

#### Scenario: Timeout after identity attribution
- **WHEN** execution times out after a valid container identity is retained
- **THEN** successful verification requires ordered removal and confirmation records, settled local owners and the expected snapshot deletion

#### Scenario: Timeout before identity attribution
- **WHEN** startup times out without a valid attributable container identity
- **THEN** verification requires settled local owners, no guessed removal or confirmation launch, and explicit unresolved-resource state with the snapshot retained

### Requirement: Verification failures retain diagnostic identity
Deadline-check failures SHALL identify the relevant launch attempts, acquisition outcomes, remaining allowances, timeout/status observations and cleanup state. Diagnostic rendering SHALL NOT replace the original assertion or lifecycle failure.

#### Scenario: Missing record accompanies a different defect
- **WHEN** a missing child record coexists with a launch error, unexpected exit or incomplete cleanup
- **THEN** verification remains unsuccessful and reports the independent defect instead of presenting only an incidental file-read error
