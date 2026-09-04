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
3. Create a meaningful branch from that exact `develop` HEAD using an agent-identifying prefix: `codex/` for Codex, `claude/` for Claude, or `issue/` for a human developer. The rest of the name must identify the issue or epic and the outcome, for example `codex/42-durable-scope-lease`, `claude/13-configuration-snapshots-differences`, or `codex/epic-6-workflow-recovery`. The prefix keeps concurrent agents' branches distinguishable in the branch list, in `docs/checkpoints/`, and during pull-request review; it does not grant an agent exclusive ownership of an area.
4. Create a sibling worktree inside the ignored directory, naming the branch exactly as in the previous step: `git worktree add .worktrees/issue-42-durable-scope-lease -b codex/42-durable-scope-lease develop`.
5. Work only from that issue worktree. Never mix two issues in one worktree or branch.
6. GitHub labels are the required per-issue tags. Create an annotated Git tag only for an approved release or tested milestone, using `v<major>.<minor>.<patch>` or `test-<YYYYMMDD>-issue-<number>`; do not create a Git tag for an unreviewed issue branch.

### 3. Implement, test, and checkpoint

1. Read `AGENTS.md`, `docs/PROGRESS.md`, the most recent files in `docs/checkpoints/` (`ls docs/checkpoints | tail -3`), and the issue before changing files.
2. Write or update the focused failing test first for production behavior. Run it and capture the expected failure before implementing the minimum change.
3. Run focused tests after each behavior change, then the relevant lint, type, build, and integration checks before committing.
4. Commit the completed, verified issue scope with a meaningful Conventional Commit-style message, for example `feat(persistence): add scope lease (#42)`, `fix(config): reject broad service SID (#42)`, or `docs(workflow): add issue handoff rules (#42)`.
5. Push the implementation branch to the remote repository before opening its pull request: `git push -u origin <branch>`.
6. Update the issue and add a new file in `docs/checkpoints/` after each independently testable task, review result, blocker, migration, configuration contract, or external side effect. Never edit an existing checkpoint; add a newer one instead.

### 4. Open and review the pull request

1. Open a PR from the already-pushed issue branch targeting `develop`, using `.github/pull_request_template.md`.
2. Use title format `<type>(<area>): <outcome> (#<issue>)`.
3. Apply the same type, area, and priority labels as the issue; assign the PR to the current authenticated account and request the appropriate review.
4. A PR must link the issue, name requirement/rule IDs, include commands and results, state migration/configuration/side-effect impact, and identify rollback/recovery where relevant. Link the issue in GitHub's **Development** sidebar, not only in the PR body: because issue branches target `develop` while the repository default branch is `main`, a `Closes #<issue>` keyword does **not** create the link and does **not** close the issue on merge. Confirm the link registered:

    ```bash
    gh api graphql -f query='{repository(owner:"IT-Development-Pepito",name:"solum-aims-esl-pipeline"){pullRequest(number:<pr>){closingIssuesReferences(first:10){nodes{number state}}}}}'
    ```

    An empty result means only a mention exists. Set the link in the PR's Development sidebar; the GitHub CLI cannot set it.
5. Before merge, the non-authoring agent records a cross-agent review on the PR. Both agents authenticate as the same GitHub account, so GitHub's approving-review flow is unavailable — an account cannot approve its own pull request. Record the review as a comment review instead:

    ```bash
    gh pr review <pr> --comment --body "<findings>"
    ```

    The review must state whether the change stayed inside the issue's accepted scope, whether the recorded evidence supports the claims, what the migration, configuration, and external side-effect impact is, and whether the change overlaps work the reviewing agent has in flight. Recording "no overlap" explicitly is the point: it is the only place the two agents' work is compared before it lands.

6. `develop` is protected and requires the `verify` check to pass before merge. Approving reviews are deliberately not required, because a single account cannot satisfy them. Merge once `verify` is green and the cross-agent review is recorded. Direct feature-to-`main` merges are not permitted. Administrators are not bound by the protection (`enforce_admins` is disabled), so an owner can still merge when a check is genuinely blocked; doing so requires recording the reason on the PR.
7. After the PR is merged, close the issue manually with the merge commit, since the keyword cannot close it, and update any dependent issues or epics with the merge information and next steps.
8. After GitHub records the merge commit, update the local integration checkout immediately and automatically. From the root checkout use `git switch develop`, then `git pull --ff-only origin develop`. From an agent worktree `git switch develop` fails, because `develop` is checked out in the root worktree; run it against the root checkout instead, which is safe and refuses rather than rewriting if anything diverged:

    ```bash
    git -C <repository-root> pull --ff-only origin develop
    ```

    Confirm the local and remote `develop` SHAs match.
9. Delete the issue worktree and its branch only after the branch is merged, local `develop` is updated, and a checkpoint in `docs/checkpoints/` records the merge commit and next step. The repository deletes the remote branch automatically on merge.

### Local AIMS databases for adapter work

Adapter work against AIMS (#24 and its successors) runs against a local clone of `AIMS_PORTAL_DB` and `AIMS_CORE_DB`, never against production. The procedure, the expected result, and every failure mode met while producing it are in [`docs/development/aims-local-clone.md`](development/aims-local-clone.md). Read it in full before dumping: the dump reads production AIMS and must run off-peak, and two of its failure modes look like different problems and are the same one.

### Cross-agent handoff rule

Before changing chats or agents, add a checkpoint file to `docs/checkpoints/` containing: issue number/title, branch, commit SHA, exact completed scope, commands and results, uncommitted state, configuration variable names without values, external systems touched, unresolved risks, and the next smallest action. A new agent must read the most recent checkpoints before work.

Checkpoints are one file per checkpoint, named `<YYYY-MM-DD>-<HHMM>-<owner-and-scope>.md`, so two agents adding a checkpoint never edit the same lines. Filename order is chronological order, and there is deliberately no index file to conflict over. `docs/PROGRESS.md` keeps the required-field table and the project's phase, decisions, risks, and discovery sections.

### Documentation-first data-model change procedure

This procedure applies whenever business behavior, a persistent entity, a field meaning, relationship, constraint, lifecycle, retention class, API shape, or application-level data type changes. SYSTEM_ARCHITECTURE.md is the model authority; this guide owns the repeatable development procedure.

1. Identify and cite the affected FR/NFR/BR IDs in the GitHub issue.
2. Update SPECIFICATION.md first when business logic, processing behavior, evidence classification, or acceptance criteria change. Do not invent a rule to satisfy an implementation.
3. Update the authoritative data-model section in SYSTEM_ARCHITECTURE.md, including ownership, entity fields, keys, invariants, lifecycle, retention, compatibility, and migration impact.
4. Obtain documentation review before creating or changing application models or database schema.
5. Add a new Alembic migration. Never edit a migration that has already been applied in any shared environment. Set `down_revision` to the migration graph's current head and keep the graph at exactly one head: when another issue merged a migration first, rebase your revision onto the new head rather than leaving both branched from a shared parent. Check the head before authoring a revision with `python -m alembic heads`, which reads the migration files and needs no database. `tests/unit/persistence/test_migration_graph.py` enforces the single head in CI.
6. Update the affected SQLAlchemy persistence, domain/Pydantic, FastAPI, and exposed TypeScript models. Keep transport models separate from domain and persistence models.
7. Add requirement/rule-traceable tests, including prior-schema migration coverage and contract-drift checks where applicable.
8. Add a checkpoint in `docs/checkpoints/` with the issue, branch/worktree, migration state, exact commands/results, configuration variable names without values, external effects, risks, and next action.
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

### Who may perform a manual operation

Every manual operation in this section is authorized under the two-role model of AD-018 (FR-023). An **operator** may trigger a run, query status, retry, replay, and request reconciliation. An **admin** may do all of that and, in addition, enable or disable a schedule and apply the fallback below. Roles are assigned per Windows account in `ESL_OPERATOR_ROLES` (`identity=role[,role]`, entries separated by `;`, names compared without regard to case) until the authenticated web session of #28 replaces it. An account with no assignment is refused, and every refusal is written to `audit_entry` under that account's name with the operation and the role it lacked, so an unexpected refusal is diagnosed from the ledger rather than from a log. Every mutation also requires a reason, normally the change or incident ticket; a blank reason is refused before any role check. A malformed `ESL_OPERATOR_ROLES` stops the service at startup rather than silently authorizing nobody.

### Check service and dependency status

1. Run **`esl-admin status`** on the host. It prints `ready: yes|no` and one line per dependency (`state-store`, `warehouse`, `legacy-baseline`, `pepito-ho`, `aims-portal`, `aims-core`) with its state and whether it is required; the exit code is non-zero when not ready, so it can run unattended.
2. From a monitor, use **`GET /health/live`** and **`GET /health/ready`** on `ESL_INTERNAL_HOST:ESL_INTERNAL_PORT`. Neither needs a token; readiness answers `503` with the same dependency list when new work cannot be accepted. AIMS API, filesystem-consumer, and telemetry dependencies are not probed yet because those adapters do not exist.
3. If readiness is false, do not manually trigger work. Capture the execution/health evidence and resolve or escalate the failed dependency.

Expected implementation outcome: liveness indicates the process is running; readiness indicates new work can be accepted safely; dependency health identifies degraded external systems without leaking secrets. Status and run-event data must be queryable from the target database by execution ID, workflow, store, and time range.

### Check workflow status and find failures

1. Query **`esl-admin runs list --workflow <name> --store <code> --from <iso-instant> --to <iso-instant>`** (instants carry an offset, e.g. `2026-09-02T07:00:00+07:00`; at least one selector is required), or **`GET /runs?workflow_name=&store_code=&started_from=&started_to=`** with a bearer token.
2. Select the execution ID and inspect **`esl-admin runs show <execution-id>`** or **`GET /runs/{execution-id}`**. Each step includes its latest attempt, outcome, failure class, start/end time, duration, last checkpoint, and the allowlisted `canonicalize` counts. The same result includes the four recovery fields derived by #21: scope, last durable checkpoint and resume step, uncertain external actions, and the recommended operator action. A `FAILED` run's `terminal_reason` reads `step:dependency:kind:class`, for example `read-store:sql_server:unavailable:RETRYABLE:attempts_exhausted`.
3. Summarize exclusions and anomalies with **`esl-admin runs issues <execution-id>`** or **`GET /runs/{execution-id}/issues`**. Drill down with `--code`, `--severity`, `--item`, `--limit`, and `--offset` (the API uses matching query parameters). The summary combines relational `record_issue` rows with keyless `RECORD_EXCLUDED` events; the drill-down returns store, item, selling UOM, rule, severity, and sanitized evidence.
4. Inspect the newest reconciliation revision with **`esl-admin runs report <execution-id>`** or **`GET /runs/{execution-id}/report`**. Use `--category`, `--item`, `--limit`, and `--offset` for exception drill-down. A `LEGACY_BASELINE_MISMATCH` shows computed/expected and legacy/actual evidence side by side; it remains mismatch evidence, not a parity claim. An unknown execution id answers `Not found` (HTTP 404); a run that has not reconciled yet answers `Not found: ... has no reconciliation report yet`. If a stored evidence row carries a secret-like key, the page is withheld with `Evidence withheld` and exit code 4 (HTTP 500 with a fixed message); fix the source of the leak rather than the read.
5. Open authenticated **`GET /metrics`** for aggregate issue counts, reconciliation counts, and completed-step duration totals/samples (`esl_run_step_duration_window_seconds`, `esl_run_step_samples_window`). It includes only the newest `ESL_METRICS_RUN_LIMIT` executions independently per workflow/store (20 by default) and never uses an execution ID as a Prometheus label. Use the per-run commands above for investigation; metrics are the trend surface.

### Review promotion anomalies

1. Run **`esl-admin runs issues <execution-id>`** to rank the issue codes, then use **`--code <issue-code> --item <item-code>`** (or the matching API query) to isolate a record. Distinguish a validation rejection from an unresolved compatibility selection by its rule, issue code, severity, and sanitized evidence.
2. Review the returned evidence for the affected store, item, and selling UOM. Where the issue records them, compare campaign/date-time eligibility, category-`001` regular price, source/resolved UOM, raw `DISC_TEXT`, weekday metadata/fallback, and calculated economic outcome. Do not query PostgreSQL JSONB directly to fill a missing operator field; open a bounded issue if additional sanitized evidence is required.
3. Treat `PROMO_PRIORITY_DIFFERENT_ECONOMIC`, `DISPLAY_PRIORITY_SAME_ECONOMIC`, and unsupported non-CLR UOM conversion as business/data-review outcomes. Do not choose a winner, infer a unit conversion, or parse manual display text to force a result.
4. If an approved policy or corrected source is supplied, record its reference and create a bounded replay. Otherwise retain the anomaly and compatibility evidence for merchandising/POS escalation.

### Manually trigger a workflow

1. Verify readiness and that the schedule is not already owning the same scope.
2. Submit **`esl-admin runs start --workflow <name> --store <code> --window-start <iso-instant> --window-end <iso-instant> --reason <ticket>`** as an account holding the `operator` or `admin` role, or **`POST /runs`** with the same fields as JSON and a bearer token. The run is created in the mode configuration dictates (`ESL_SHADOW_MODE`), under the active configuration version and rule version. The service's worker (#102) picks it up within `ESL_WORKER_CONCURRENCY` slots and runs it step by step (`discover`, `read-warehouse`, `read-store`, `read-pepito-ho`, `canonicalize`, `persist`); a retryable source failure leaves it in `RETRY_WAIT` and the worker retries it under the configured policy, a non-retryable one ends it in `FAILED` with a terminal reason naming the step, dependency, kind, and class. In shadow mode the run reads the sources, computes, and persists its evidence and reconciliation report; nothing is sent to AIMS. `ACTIVE` mode fails terminally until the AIMS mutation adapter (#23) exists.
3. Record the execution ID in the change/incident ticket.
4. Monitor until terminal state and perform reconciliation.

The implementation rejects an overlapping scope; it must not run two owners concurrently. A refused launch creates no execution and returns an explicit rejection naming the execution that currently owns the scope and what triggered it, and the decision is recorded in the audit trail. A manual request does not displace a scheduled run, and a scheduled run does not displace a manual one: wait for the owner to finish, or cancel it through the documented graceful-cancel procedure first.

### Retry a failed run

1. Inspect error classification and the last external action.
2. For retryable failures, use **`esl-admin runs retry <execution-id> --reason <ticket>`** or **`POST /runs/{execution-id}/retry`**. Only a `FAILED` run without an unresolved external action is accepted; the refusal reason is printed otherwise.
3. For an ambiguous AIMS submission, run reconciliation first; retry only unresolved idempotency keys.
4. For malformed data/configuration or AIMS rejection, correct through the approved business/configuration process, then create a recorded replay.

### Replay a processing range

1. Obtain business/data-owner approval for store and exact time/key range.
2. Confirm replay effect and that no higher-priority active workflow owns the scope.
3. Use **`esl-admin runs replay <execution-id> --window-start <iso-instant> --window-end <iso-instant> --reason <ticket>`** or **`POST /runs/{execution-id}/replay`**. The replay inherits the workflow, store, and mode of the original run and carries exactly the window given; both bounds are mandatory.
4. Reconcile the replay against original and target outcomes; preserve both audit trails.

### Reproduce a run from its retained snapshot

This is not the window replay above. A snapshot replay (#114) reads **no source**: raw source rows are not retained (AD-005), so it cannot re-canonicalize or re-evaluate promotions. It re-persists the original run's finalized canonical capture under the original's own window, configuration version, and rule version, and proves two things from durable state alone: that the capture's aggregate hash reproduces, and how that capture differs from the store's current expected state. It never intends an action.

1. Use **`esl-admin runs replay-snapshot <execution-id> --reason <ticket>`** or **`POST /runs/{execution-id}/replay-snapshot`** with a reason only; the replay role applies. The new run carries trigger type `SNAPSHOT_REPLAY` and links to the original.
2. The request is refused, and the refusal audited, when the original's finalized `SOURCE_EXPECTED` capture no longer exists (`SNAPSHOT_EVIDENCE_MISSING`, for example after evidence retention purged it) or its latest reconciliation report is not final (`RECONCILIATION_UNRESOLVED`).
3. The run has one step, `replay-snapshot`. Its `SNAPSHOT_REPLAYED` event records the source capture, both aggregate hashes, and `hash_reproduced`; a run whose hash did not reproduce ends `SUCCEEDED_WITH_EXCEPTIONS`, which means the retained evidence or the canonical serializer changed and must be investigated before any parity claim.
4. Read the replay's reconciliation report: `extracted` and `valid` are the retained records; `eligible`/`unchanged` are those identical to the store's current expected state; `ineligible` are those that differ, with the path-level detail in its difference rows; `intended` is always zero.

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

1. Use **`esl-admin schedules disable <schedule-id> --reason <ticket>`** (admin role) or **`POST /schedules/{schedule-id}/disable`**; re-enable with `enable`. Do not stop the service merely to disable one workflow. To stop *all* scheduling while the process keeps serving status, pause the scheduler instead: **`POST /scheduler/pause`** with a reason (admin), or `sc.exe pause <service>` on the host; both are audited as `scheduler.paused` / `service.paused`.
2. Confirm the schedule status and existing run ownership.
3. Allow/stop in-flight runs only through the documented graceful-cancel procedure; never kill a process before recording recovery state.
4. Resolve/reconcile outstanding work.
5. Use **`<target-service schedules enable --workflow <name> --reason <ticket>>`**, then verify the next controlled run.

### Set up credentials for a new environment

Do this once per environment, before the service is started for the first time. The rotation procedure that follows assumes this has been done. `README.md` carries the same procedure with a worked screen example.

1. Create the database accounts: the state-store user named in `ESL_DATABASE_URL`, the read-only SQL Server account (`esl_reader`) that every SQL Server tier shares, and the read-only AIMS account (`esl_aims_reader`). In staging and production also create the bundle directory, `C:\ProgramData\SOLUM\ESL` by default, as an administrator with an ACL limited to the service account, Administrators, and SYSTEM; the tool refuses to write into a missing directory when a service identity is configured, because a folder it created itself would carry inherited permissions the startup validator rejects. On a development machine the tool creates the directory and says so.
2. Provision the four bundle keys with **`esl-admin secrets set <key> --reason <ticket>`**, one command per key, typing each value at the hidden prompt. In staging and production run this **as the Windows Service account**; the tool refuses any other account when `ESL_SERVICE_IDENTITY_SID` is configured, because under user-scope DPAPI (AD-017) a bundle written by another account is unreadable by the service. On a development machine the variable is unset and the tool proceeds under your own account after saying so.
3. Migrate the state store with **`alembic upgrade head`**. Alembic resolves the password the same way the service does, from `state.password` in the bundle, so `ESL_DATABASE_URL` needs no password here either. Until this step is done, `secrets set` stores the secret but warns that the audit entry could not be recorded because the schema is not migrated.
4. Prove every value with **`esl-admin check-connections`**. Every target must be `REACHABLE`, or `UNCONFIGURED` for a tier deliberately not in use yet. `CREDENTIAL_REJECTED` means the bundle value is wrong; `SECRET_UNAVAILABLE` means a key was not set; `DRIVER_MISSING` names an ODBC driver that is not installed, or a driver setting still in URL-encoded form with `+` instead of spaces.
5. Start the service.

| Key | Password of | Database |
| --- | --- | --- |
| `state.password` | the user in `ESL_DATABASE_URL` | the service's own PostgreSQL |
| `source.sql.password` | `ESL_SOURCE_SQL_USERNAME` | `DBWH_8555`, `ESL`, `PEPITO_HO`, and every per-store server |
| `aims.portal.password` | `ESL_AIMS_PORTAL_USERNAME` | `AIMS_PORTAL_DB` |
| `aims.core.password` | `ESL_AIMS_CORE_USERNAME` | `AIMS_CORE_DB` |

Where a password is written determines how it is written:

| Where | Form |
| --- | --- |
| the bundle, through `esl-admin secrets set` (all four keys) | **raw**, exactly as it is |
| `.env`, only the three `ESL_TEST_*_URL` test variables | **percent-encoded** (`@`→`%40`, `:`→`%3A`, `/`→`%2F`, `#`→`%23`, `%`→`%25`) because they sit inside a URL |

No other variable in `.env` carries a password, and the startup gate refuses an `ESL_DATABASE_URL` that embeds one.

What the prompt looks like, so it is clear where the raw value is typed:

```
PS> esl-admin secrets set state.password --reason "CHG-1042 initial provisioning"
Secret value:               <- raw password, not echoed
Repeat for confirmation:
Stored secret 'state.password' in C:\ProgramData\SOLUM\ESL\secrets.dpapi.
```

`secrets set` is run again only when a database password is rotated (that key), when a new source needs its own credential (the new key), or when an administrator resets the service account's Windows password (all four keys; see the next procedure). It is never part of startup and never scheduled.

### Provision or rotate a secret

1. Run as the Windows Service account. Under user-scope DPAPI (AD-017) a bundle written by any other account cannot be read by the service. When `ESL_SERVICE_IDENTITY_SID` is configured and does not match the running account, the command refuses with exit code 2; when it is not configured, as on a development machine, the command says the identity check was skipped and proceeds.
2. Run **`esl-admin secrets set <key> --reason <ticket>`** and type the value at the hidden, confirmed prompt, or pipe it with `--stdin`. The value is never accepted as an argument, so it cannot reach shell history or a process listing. The service reads exactly four keys: `state.password` for its own PostgreSQL, `source.sql.password` for every SQL Server tier including per-store servers, `aims.portal.password`, and `aims.core.password`. `ESL_DATABASE_URL` names where the state store is and as whom to connect, and the startup gate refuses it if it still embeds a password; the source and AIMS settings have no field that could hold one.
3. Confirm with **`esl-admin secrets list`**, which prints names only, and remove a retired key with **`esl-admin secrets remove <key> --reason <ticket>`**.
4. Prove the value works with the connectivity check below. Setting a secret proves only that it is readable.
5. Each change is audited as `secret.set` or `secret.removed`, naming the actor, the key, and the reason, never the value. If the state store is unreachable the command warns and still succeeds, because the store's own password is provisioned this way and may not be reachable yet.

**If an administrator resets the service account's password**, the bundle becomes permanently unreadable and the service reports `secret bundle is unavailable`, which is indistinguishable from a missing or corrupt bundle. Recovery is to recreate every secret with `secrets set` as the service account. Changing the password *as the account*, knowing the old one, does not have this effect. An existing bundle that cannot be read is never overwritten by `set`, so a bad write cannot silently discard the other secrets.

### Check database connectivity

1. Run **`esl-admin check-connections`**. It probes the state store from configuration and any extra target given as `--target name=postgresql://user@host:port/db#bundle.key` or `--target name=sqlserver://user@host/db#bundle.key`. The part after `#` is a bundle key, never a password; an inline password is rejected.
2. Read the outcome per target: `REACHABLE` with the identity the server reports; `UNREACHABLE` means no answer from the host, port, or database; `CREDENTIAL_REJECTED` means the server answered and refused the credential; `SECRET_UNAVAILABLE` means the key is not in the bundle; `UNCONFIGURED` means the target has no host, database, or username yet and is not counted as a failure.
3. The exit code is non-zero when any target is neither `REACHABLE` nor `UNCONFIGURED`, so the check can run unattended. Output never contains a connection string or a password; driver error text is dropped because it commonly embeds both.
4. The AIMS compatibility reader waits at most `ESL_AIMS_CONNECT_TIMEOUT_SECONDS` (10 by default; #112) for a connection to either AIMS database, then classifies the fault `UNAVAILABLE` and hands it to the retry policy; a host that silently drops packets no longer stalls an attempt until TCP gives up. The value is a provisional operational default (NFR-004): raise it only against a measured connection-latency baseline, never to paper over an unreachable host.

### Provision an API token

The internal API authenticates with per-account bearer tokens held in the DPAPI bundle (AD-019). One token per account, under the key `api.token.<account>`, where `<account>` is the bare Windows account name exactly as `ESL_OPERATOR_ROLES` names it.

1. Issue it, as the service account: **`esl-admin secrets issue-token <account> --reason <ticket> --out <path>`**. The command generates 32 random bytes, stores them under `api.token.<account>`, and writes the value to `<path>` with the bundle's own ACL (#98). Use `--stdout` instead when you are at the console and will copy the value straight into the approved channel; at a terminal it is also the default, so the command with neither option simply prints the token. When standard output is a pipe, a redirect, or a transcript, naming a channel is required, because a token written into a log is not a reveal anyone chose.
2. Hand the file to the account holder, then delete it. The value is revealed once and is nowhere else in plaintext: it is not in the audit entry, not in any log, and not recoverable from the bundle by any command. Losing it means issuing again.
3. Assign the account a role in `ESL_OPERATOR_ROLES`; a token without a role authenticates and is then refused for every operation, with the refusal audited under that account. The command warns when the account it just issued for holds no role.
4. Restart the service, or wait for the next start: tokens are read from the bundle when the host builds its authenticator.
5. **Rotate** by running the same command again with a fresh `--out` path. The previous token stops authenticating the moment the service reloads the bundle, so the restart is the cutover; the command says so when it replaced an existing key, and the audit entry carries `rotated: true` so the ledger tells a rotation apart from a first provisioning.
6. Revoke with **`esl-admin secrets remove api.token.<account> --reason <ticket>`** and a restart.

The command refuses to overwrite an existing `--out` file rather than risk discarding a token still in use, and it obeys the same identity guard as `secrets set`: in staging and production it must run as the account in `ESL_SERVICE_IDENTITY_SID`, because a user-scope DPAPI bundle written by anyone else is unreadable by the service.

Calls carry `Authorization: Bearer <token>`. A missing or unknown token is `401` and is never logged; a role refusal is `403` and is already in `audit_entry`.

Pasting a token you generated elsewhere still works — `esl-admin secrets set api.token.<account> --reason <ticket>` is unchanged — but it moves a secret through a clipboard and a half-typed prompt, and it cannot be scripted. Prefer `issue-token`.

### Install and control the Windows Service

1. Deploy the approved artifact and create the virtual environment on the host (NFR-016). Set the production variables for the service account, including `ESL_INTERNAL_HOST`, `ESL_INTERNAL_PORT`, `ESL_OPERATOR_ROLES`, `ESL_SERVICE_IDENTITY_SID`, and `ESL_WINDOWS_SERVICE_NAME`.
2. Write the bundle as the service account (previous sections), including `state.password`. API tokens can wait for step 3.
3. As an administrator run **`.\scripts\install-service.ps1 -PythonExe <venv>\Scripts\python.exe -ServiceAccount <account>`**. It registers `ESL_WINDOWS_SERVICE_NAME` (default `SOLUM_ESL_PIPELINE`) with automatic start; the password prompt is not stored. Add **`-IssueTokensFor ops.alice,ops.bob -TokenDirectory <dir>`** to issue one token per account in the same pass: each is stored in the bundle and written to `<dir>\<account>.token` for you to hand over and delete. `<dir>` must already exist with an ACL you control, and running the install again with the same accounts rotates their tokens.
4. Control it with `sc.exe start|stop|pause|continue <service>`. Start registers the active configuration version, resumes scheduling, and starts the API listener bound to `ESL_INTERNAL_HOST:ESL_INTERNAL_PORT`. Pause quiesces scheduling and keeps the process answering status. Stop pauses scheduling first, then stops the tick loop within its deadline; if the loop misses the deadline the `service.stopped` audit entry says so. Every transition is audited under the `service` actor.
5. For development and diagnostics run the same host in the foreground with **`esl-admin serve`** under your own account; Ctrl+C stops it through the same lifecycle.

### Restart the service

1. Disable affected schedules if maintenance requires it.
2. Confirm active executions and request graceful checkpoint/cancellation where needed.
3. Restart using the approved Windows Service Control Manager command: **`<deployment-specific service restart>`**. A pause must stop new schedule claims and allow in-flight work to checkpoint/safely complete; do not use an abrupt process kill as a substitute.
4. Confirm startup recovery has reconciled/resumed prior leases with **`esl-admin runs show <execution-id>`** or **`GET /runs/{execution-id}`**. Inspect the scope, durable checkpoint and resume step, uncertain external actions, and next operator action. These fields are derived from durable state by `application/recovery.py`; never launch a duplicate run while the report says recovery owns it.
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

The service automates only the first two of those steps (#26, AD-018). An admin applies `fallback` for a workflow, optionally bounded to one store, with the incident ticket as the reason. It disables every enabled schedule in that scope, each as its own audited change, touches no execution, checkpoint, or audit row, and appends one `fallback.applied` audit entry whose `reconcile_window_from` instant marks where the cutover-window reconciliation must start. Restoring the legacy SQL Agent trigger, reconciling that window, and opening the incident record remain manual steps of the approved rollback plan.
