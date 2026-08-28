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

**Mandatory rule:** every implementation change—application code, tests, configuration, infrastructure, or documentation—must be tracked by a GitHub issue before work begins. Do not create an ad-hoc implementation branch or commit work that is not in an issue's accepted scope.

### 1. Create and prepare the issue

1. Use the title format **`[area] imperative outcome`**, for example: `[persistence] add durable scope lease`.
2. State the problem, requirement/rule IDs, acceptance criteria, non-goals, operational risk, and test evidence required for closure.
3. Add GitHub labels (the per-issue tags): exactly one type label (`type:feature`, `type:bug`, `type:chore`, `type:docs`, or `type:security`), one or more area labels (`area:domain`, `area:ingestion`, `area:workflow`, `area:persistence`, `area:adapters`, `area:operations`, `area:runtime`, `area:web`, `area:observability`, `area:ci`, or `area:docs`), and a priority label (`priority:p0` through `priority:p3`). A parent epic also carries `kind:epic`; add `blocked` only while a named dependency prevents progress.
4. Assign the issue to the currently authenticated GitHub account. With GitHub CLI use `gh issue edit <number> --add-assignee "@me"`; confirm the assignee before work starts.
5. Record the issue number and accepted scope in `docs/PROGRESS.md` before implementation.

### 2. Use one branch and worktree per issue

1. Use remote `develop` as the integration branch. Bootstrap it exactly once from the current remote `main` when `origin/develop` does not exist: `git fetch origin --prune`, then `git push origin origin/main:refs/heads/develop`.
2. Start every issue from current `develop`: `git switch develop`, then `git pull --ff-only origin develop`.
3. Create a meaningful branch from that exact `develop` HEAD with the `codex/` prefix. Its name must identify the issue or epic and outcome, for example `codex/42-durable-scope-lease` or `codex/epic-6-workflow-recovery`.
4. Create a sibling worktree inside the ignored directory: `git worktree add .worktrees/issue-42-durable-scope-lease -b codex/issue-42-durable-scope-lease develop`.
5. Work only from that issue worktree. Never mix two issues in one worktree or branch.
6. GitHub labels are the required per-issue tags. Create an annotated Git tag only for an approved release or tested milestone, using `v<major>.<minor>.<patch>` or `test-<YYYYMMDD>-issue-<number>`; do not create a Git tag for an unreviewed issue branch.

### 3. Implement, test, and checkpoint

1. Read `AGENTS.md`, `docs/PROGRESS.md`, and the issue before changing files.
2. Write or update the focused failing test first for production behavior. Run it and capture the expected failure before implementing the minimum change.
3. Run focused tests after each behavior change, then the relevant lint, type, build, and integration checks before committing.
4. Commit the completed, verified issue scope with a meaningful Conventional Commit-style message, for example `feat(persistence): add scope lease (#42)`, `fix(config): reject broad service SID (#42)`, or `docs(workflow): add issue handoff rules (#42)`.
5. Push the implementation branch to the remote repository before opening its pull request: `git push -u origin <branch>`.
6. Update the issue and `docs/PROGRESS.md` checkpoint after each independently testable task, review result, blocker, migration, configuration contract, or external side effect.

### 4. Open and review the pull request

1. Open a PR from the already-pushed issue branch targeting `develop`, using `.github/pull_request_template.md`.
2. Use title format `<type>(<area>): <outcome> (#<issue>)`.
3. Apply the same type, area, and priority labels as the issue; assign the PR to the current authenticated account and request the appropriate review.
4. A PR must link the issue, name requirement/rule IDs, include commands and results, state migration/configuration/side-effect impact, and identify rollback/recovery where relevant.
5. After the applicable review and required checks pass, enable auto-merge to `develop` (or complete the approved merge if auto-merge is unavailable). When branch protection is not configured, the merging owner must first verify the visible successful review/check evidence. Direct feature-to-`main` merges are not permitted.
6. After the PR is merged, ensure that any dependent issues are closed or epics are updated with the merge information and next steps.
7. After GitHub records the merge commit, update the local integration checkout immediately: `git switch develop`, then `git pull --ff-only origin develop`. Confirm the local and remote `develop` SHAs match.
8. Delete the issue worktree only after the branch is merged, local `develop` is updated, and the `PROGRESS.md` checkpoint records the merge commit and next step.

### Cross-agent handoff rule

Before changing chats or agents, add a checkpoint to `docs/PROGRESS.md` containing: issue number/title, branch, commit SHA, exact completed scope, commands and results, uncommitted state, configuration variable names without values, external systems touched, unresolved risks, and the next smallest action. A new agent must read the latest checkpoint before work.

### Documentation-first data-model change procedure

This procedure applies whenever business behavior, a persistent entity, a field meaning, relationship, constraint, lifecycle, retention class, API shape, or application-level data type changes. SYSTEM_ARCHITECTURE.md is the model authority; this guide owns the repeatable development procedure.

1. Identify and cite the affected FR/NFR/BR IDs in the GitHub issue.
2. Update SPECIFICATION.md first when business logic, processing behavior, evidence classification, or acceptance criteria change. Do not invent a rule to satisfy an implementation.
3. Update the authoritative data-model section in SYSTEM_ARCHITECTURE.md, including ownership, entity fields, keys, invariants, lifecycle, retention, compatibility, and migration impact.
4. Obtain documentation review before creating or changing application models or database schema.
5. Add a new Alembic migration. Never edit a migration that has already been applied in any shared environment.
6. Update the affected SQLAlchemy persistence, domain/Pydantic, FastAPI, and exposed TypeScript models. Keep transport models separate from domain and persistence models.
7. Add requirement/rule-traceable tests, including prior-schema migration coverage and contract-drift checks where applicable.
8. Update PROGRESS.md with the issue, branch/worktree, migration state, exact commands/results, configuration variable names without values, external effects, risks, and next action.
9. In the PR, verify that documentation, migration, application models, API types, and tests describe the same semantics. A model-changing PR cannot merge while any one of these layers is missing or contradictory.

Emergency fixes follow the same contract: reconcile the documents in the same PR before merge. A documentation-only architecture decision does not authorize an Alembic migration or application implementation; those require their own accepted issue scope.

## Operational concepts

| Term               | Meaning                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------ |
| Execution ID       | Immutable identifier for one workflow run; included in logs, metrics, audit, and reconciliation. |
| Scope              | Workflow, store(s), source window, and configuration/rule version owned by a run.                |
| Checkpoint         | Durable progress marker from which work can resume/reconcile.                                    |
| Idempotency key    | Stable logical action identity used to prevent duplicate external effect.                        |
| Reconciliation     | Comparison of source/eligible/rejected/submitted/acknowledged/unresolved counts and records.     |
| Compatibility read | Temporary read-only query to AIMS PostgreSQL; not an AIMS mutation mechanism.                    |

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

### Review promotion anomalies

1. Query the execution’s promotion anomaly records by store, item, selling UOM, and rule version; distinguish rejection from unresolved compatibility selection.
2. For an affected record, inspect raw campaign values, date/time eligibility, category-`001` regular price, source/resolved UOM, raw `DISC_TEXT`, weekday metadata/fallback, calculated economic outcome, and every eligible candidate.
3. Treat `PROMO_PRIORITY_DIFFERENT_ECONOMIC`, `DISPLAY_PRIORITY_SAME_ECONOMIC`, and unsupported non-CLR UOM conversion as business/data-review outcomes. Do not choose a winner, infer a unit conversion, or parse manual display text to force a result.
4. If an approved policy or corrected source is supplied, record its reference and create a bounded replay. Otherwise retain the anomaly and compatibility evidence for merchandising/POS escalation.

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

The report must show, at minimum: extracted, valid, rejected, ineligible, eligible, unchanged, intended, skipped-idempotent, submitted, acknowledged, rejected-by-AIMS, failed, and unresolved counts; plus record identifiers for every imbalance.

Transformation balance is always:

```text
extracted = rejected + valid
valid = ineligible + eligible
```

A terminal active execution balances as:

```text
eligible = unchanged + skipped_idempotent + acknowledged
         + rejected_by_aims + failed + unresolved
```

A terminal shadow execution balances as:

```text
eligible = unchanged + skipped_idempotent + intended + unresolved
```

Submitted is an in-flight observation, not a terminal category. A submitted action without a confirmed outcome becomes OUTCOME_UNKNOWN and is counted as unresolved before terminal reconciliation. Any non-zero unresolved count blocks automatic completion for the affected scope unless an approved policy explicitly permits it.

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

| Scenario                              | First response                                                                                              | Safe recovery                                                                                                                                                                                                  |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SQL Server unavailable                | Stop new affected scope; inspect dependency health.                                                         | Restore connectivity; retry from saved window/checkpoint.                                                                                                                                                      |
| AIMS API timeout                      | Inspect action ledger/idempotency key.                                                                      | Reconcile before retry; escalate vendor/API issues.                                                                                                                                                            |
| AIMS rejection                        | Capture response and rule/config version.                                                                   | Correct data/config/rule only through approved process; replay bounded scope.                                                                                                                                  |
| AIMS compatibility query fails        | Mark dependency degraded.                                                                                   | Do not substitute direct DB writes; use approved fallback or wait/escalate.                                                                                                                                    |
| Malformed input                       | Quarantine with reason.                                                                                     | Correct source/validation issue; replay only rejected records/window.                                                                                                                                          |
| Promotion ambiguity / unsupported UOM | Preserve candidate and calculation evidence; do not treat it as a transport failure.                        | Escalate to merchandising/POS or correct approved source; replay only after the rule/data decision is recorded.                                                                                                |
| Process/server restart                | Inspect recovery report.                                                                                    | Resume/reconcile durable runs; do not launch duplicate manual runs.                                                                                                                                            |
| CSV delivery uncertainty              | Treat missing, rejected, malformed, mismatched, or timed-out acknowledgement as unresolved, not successful. | Inspect the durable delivery/event record and matching automatic acknowledgement; never infer completion from file presence or blindly resend. CSV files are never authoritative state or comparison evidence. |
| Disk/telemetry failure                | Protect audit durability, alert operations.                                                                 | Restore capacity/telemetry then reconcile affected run.                                                                                                                                                        |

## Escalation

Include execution ID, workflow/store/window, timestamps/timezone, failed dependency, error classification, retry count, affected counts, reconciliation report, configuration/rule version, and sanitized logs. Route business-rule issues to merchandising/POS owners; SQL connectivity to DBA/infrastructure; AIMS/API behavior to ESL administrator/SOLUM support; service/runtime incidents to operations/SRE.

## Rollback / fallback

Rollback only under the criteria in `SPECIFICATION.md`: disable target schedules, preserve target audit/state, restore the approved legacy trigger for the affected scope, reconcile the cutover window, and open an incident/change record. SQL Agent, Jenkins, and Hop must not be modified or removed as part of an emergency rollback unless explicitly authorized by the approved rollback plan. Production releases must first have a staging deploy and rollback rehearsal under NFR-012.
