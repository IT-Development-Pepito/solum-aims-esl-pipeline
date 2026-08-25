# Production Workflow and Operations Guide

## Status of this guide

This is the operating model for the target internal web application. The application does not yet exist, so UI/API and command placeholders are deliberately named as placeholders rather than fictional executable commands. Replace them only when the implementation establishes the real interface and update this guide in the same change.

## Safety rules

- Never edit production source tables, AIMS databases, SQL Agent jobs, Jenkins jobs, Hop definitions, or device state to compensate for a failed target run.
- Do not retry an ambiguous AIMS action until reconciliation determines whether AIMS accepted it.
- All manual actions require an authorized identity, execution ID/scope, reason, and audit entry.
- Use a bounded store/workflow scope; do not replay an unbounded historical range.
- Escalate vendor-boundary issues through the approved SOLUM support path. Do not use direct AIMS database writes.

## Daily development workflow

This workflow applies to every GitHub issue and is usable by Codex, Claude, or a human developer. It governs repository work only; it does not authorize production database, AIMS, SQL Server, Hop, Jenkins, or ESL changes.

### 1. Create and prepare the issue

1. Use the title format **`[area] imperative outcome`**, for example: `[persistence] add durable scope lease`.
2. State the problem, requirement/rule IDs, acceptance criteria, non-goals, operational risk, and test evidence required for closure.
3. Add GitHub labels (the per-issue tags): exactly one type label (`type:feature`, `type:bug`, `type:chore`, `type:docs`, or `type:security`), one or more area labels (`area:domain`, `area:persistence`, `area:adapters`, `area:web`, `area:runtime`, `area:ci`, or `area:docs`), and a priority label (`priority:p0` through `priority:p3`). Add `blocked` only while a named dependency prevents progress.
4. Assign the issue to the currently authenticated GitHub account. With GitHub CLI use `gh issue edit <number> --add-assignee "@me"`; confirm the assignee before work starts.
5. Record the issue number and accepted scope in `docs/PROGRESS.md` before implementation.

### 2. Use one branch and worktree per issue

1. Start from current `main`: `git switch main`, then `git pull --ff-only origin main`.
2. Create branch `issue/<number>-<kebab-case-outcome>`, for example `issue/42-durable-scope-lease`.
3. Create a sibling worktree inside the ignored directory: `git worktree add .worktrees/issue-42-durable-scope-lease -b issue/42-durable-scope-lease main`.
4. Work only from that issue worktree. Never mix two issues in one worktree or branch.
5. GitHub labels are the required per-issue tags. Create an annotated Git tag only for an approved release or tested milestone, using `v<major>.<minor>.<patch>` or `test-<YYYYMMDD>-issue-<number>`; do not create a Git tag for an unreviewed issue branch.

### 3. Implement, test, and checkpoint

1. Read `AGENTS.md`, `docs/PROGRESS.md`, and the issue before changing files.
2. Write or update the focused failing test first for production behavior. Run it and capture the expected failure before implementing the minimum change.
3. Run focused tests after each behavior change, then the relevant lint, type, build, and integration checks before committing.
4. Use meaningful Conventional Commit-style messages: `feat(persistence): add scope lease (#42)`, `fix(config): reject broad service SID (#42)`, `docs(workflow): add issue handoff rules (#42)`.
5. Update the issue and `docs/PROGRESS.md` checkpoint after each independently testable task, review result, blocker, migration, configuration contract, or external side effect.

### 4. Open and review the pull request

1. Push the issue branch and open a PR using `.github/pull_request_template.md`.
2. Use title format `<type>(<area>): <outcome> (#<issue>)`.
3. Apply the same type, area, and priority labels as the issue; assign the PR to the current authenticated account and request the appropriate review.
4. A PR must link the issue, name requirement/rule IDs, include commands and results, state migration/configuration/side-effect impact, and identify rollback/recovery where relevant.
5. Merge only after review and required checks pass. Delete the issue worktree only after the branch is merged and the `PROGRESS.md` checkpoint records the merge commit and next step.

### Cross-agent handoff rule

Before changing chats or agents, add a checkpoint to `docs/PROGRESS.md` containing: issue number/title, branch, commit SHA, exact completed scope, commands and results, uncommitted state, configuration variable names without values, external systems touched, unresolved risks, and the next smallest action. A new agent must read the latest checkpoint before work.
## Operational concepts

| Term | Meaning |
| --- | --- |
| Execution ID | Immutable identifier for one workflow run; included in logs, metrics, audit, and reconciliation. |
| Scope | Workflow, store(s), source window, and configuration/rule version owned by a run. |
| Checkpoint | Durable progress marker from which work can resume/reconcile. |
| Idempotency key | Stable logical action identity used to prevent duplicate external effect. |
| Reconciliation | Comparison of source/eligible/rejected/submitted/acknowledged/unresolved counts and records. |
| Compatibility read | Temporary read-only query to AIMS PostgreSQL; not an AIMS mutation mechanism. |

## Standard procedures

### Check service and dependency status

1. Use **`<target-service status>`** to confirm process health and current version.
2. Use **`<target-service health>`** to inspect liveness, readiness, SQL Server, AIMS API, compatibility-read, state-store, filesystem-consumer, and telemetry dependencies.
3. If readiness is false, do not manually trigger work. Capture the execution/health evidence and resolve or escalate the failed dependency.

Expected implementation outcome: liveness indicates the process is running; readiness indicates new work can be accepted safely; dependency health identifies degraded external systems without leaking secrets. Status and run-event data must be queryable from the target database by execution ID, workflow, store, and time range.

### Check workflow status and find failures

1. Query **`<target-service runs list --workflow <name> --store <code> --from <time> --to <time>>`**.
2. Select the execution ID and inspect **`<target-service runs show <execution-id>>`**.
3. Review terminal state, failed step, retry count, timeout/error class, configuration/rule version, checkpoint, source window, and affected-record counts.
4. Query the target database's structured execution/event logs and open correlated metrics using the execution ID. Do not treat a missing log line as evidence an external action did not occur.

### Manually trigger a workflow

1. Verify readiness and that the schedule is not already owning the same scope.
2. Submit **`<target-service run start --workflow <name> --store <code> --window <bounded-window> --reason <ticket>>`**.
3. Record the execution ID in the change/incident ticket.
4. Monitor until terminal state and perform reconciliation.

The implementation must reject or explicitly queue an overlapping scope; it must not run two owners concurrently.

### Retry a failed run

1. Inspect error classification and the last external action.
2. For retryable failures, use **`<target-service run retry <execution-id> --reason <ticket>>`**.
3. For an ambiguous AIMS submission, run reconciliation first; retry only unresolved idempotency keys.
4. For malformed data/configuration or AIMS rejection, correct through the approved business/configuration process, then create a recorded replay.

### Replay a processing range

1. Obtain business/data-owner approval for store and exact time/key range.
2. Confirm replay effect and that no higher-priority active workflow owns the scope.
3. Use **`<target-service run replay --workflow <name> --store <code> --window <start..end> --reason <ticket>>`**.
4. Reconcile the replay against original and target outcomes; preserve both audit trails.

### Determine whether data reached AIMS

1. Review per-record action ledger and adapter response/acknowledgement for the execution ID.
2. Run **`<target-service reconcile --execution <execution-id>>`**.
3. If required by the approved contract, use the supported AIMS read/query interface to confirm state.
4. Use compatibility reads only where the documented first-cutover adapter permits them; record their use.
5. If outcome remains ambiguous, mark the item `UNRESOLVED`, suppress blind retry, and escalate.

### Reconcile a workflow

The report must show, at minimum: extracted, valid, rejected, eligible, skipped-idempotent, submitted, acknowledged, rejected-by-AIMS, failed, and unresolved counts; plus record identifiers for every imbalance. The balancing rule is:

`extracted = rejected + valid`; `valid = ineligible + eligible`; `eligible = skipped-idempotent + acknowledged + rejected-by-AIMS + failed + unresolved`.

Any non-zero `unresolved` count blocks automatic completion for affected scope unless an approved policy explicitly permits it.

### Disable and re-enable scheduling

1. Use **`<target-service schedules disable --workflow <name> --reason <ticket>>`**; do not stop the service merely to disable one workflow.
2. Confirm the schedule status and existing run ownership.
3. Allow/stop in-flight runs only through the documented graceful-cancel procedure; never kill a process before recording recovery state.
4. Resolve/reconcile outstanding work.
5. Use **`<target-service schedules enable --workflow <name> --reason <ticket>>`**, then verify the next controlled run.

### Restart the service

1. Disable affected schedules if maintenance requires it.
2. Confirm active executions and request graceful checkpoint/cancellation where needed.
3. Restart using the approved Windows Service Control Manager command: **`<deployment-specific service restart>`**. A pause must stop new schedule claims and allow in-flight work to checkpoint/safely complete; do not use an abrupt process kill as a substitute.
4. Confirm startup recovery has reconciled/resumed prior leases; inspect its recovery report.
5. Re-enable schedules only after readiness and dependency health are green.

## Common failure scenarios

| Scenario | First response | Safe recovery |
| --- | --- | --- |
| SQL Server unavailable | Stop new affected scope; inspect dependency health. | Restore connectivity; retry from saved window/checkpoint. |
| AIMS API timeout | Inspect action ledger/idempotency key. | Reconcile before retry; escalate vendor/API issues. |
| AIMS rejection | Capture response and rule/config version. | Correct data/config/rule only through approved process; replay bounded scope. |
| AIMS compatibility query fails | Mark dependency degraded. | Do not substitute direct DB writes; use approved fallback or wait/escalate. |
| Malformed input | Quarantine with reason. | Correct source/validation issue; replay only rejected records/window. |
| Process/server restart | Inspect recovery report. | Resume/reconcile durable runs; do not launch duplicate manual runs. |
| CSV delivery uncertainty | Treat as unresolved, not successful. | Verify consumer acknowledgement contract; replay only idempotently. CSV files are never the run's authoritative state or comparison evidence. |
| Disk/telemetry failure | Protect audit durability, alert operations. | Restore capacity/telemetry then reconcile affected run. |

## Escalation

Include execution ID, workflow/store/window, timestamps/timezone, failed dependency, error classification, retry count, affected counts, reconciliation report, configuration/rule version, and sanitized logs. Route business-rule issues to merchandising/POS owners; SQL connectivity to DBA/infrastructure; AIMS/API behavior to ESL administrator/SOLUM support; service/runtime incidents to operations/SRE.

## Rollback / fallback

Rollback only under the criteria in `SPECIFICATION.md`: disable target schedules, preserve target audit/state, restore the approved legacy trigger for the affected scope, reconcile the cutover window, and open an incident/change record. SQL Agent, Jenkins, and Hop must not be modified or removed as part of an emergency rollback unless explicitly authorized by the approved rollback plan. Production releases must first have a staging deploy and rollback rehearsal under NFR-012.
