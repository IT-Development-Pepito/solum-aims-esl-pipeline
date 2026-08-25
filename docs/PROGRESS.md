# Current Phase

Project preparation and requirements gathering continue in this chat. Task 3 has an assigned issue and isolated worktree but remains blocked before database work because no dedicated non-production `ESL_TEST_DATABASE_URL` is available. The automated CSV compatibility contract is approved and its follow-on implementation plan is prepared but deferred until foundation Tasks 3, 6, and 7 and the consumer acceptance gate are complete.


## Cross-agent implementation checkpoints

Append a checkpoint at issue start, after every independently testable task, after a review/fix round, when blocked, before changing agents/chats, and after merge. Never include credential values.

| Field | Required content |
| --- | --- |
| Timestamp and owner | Local timestamp and agent/human identifier. |
| Issue | GitHub issue number, title, labels, and assignee. |
| Git state | Branch, worktree path, HEAD SHA, merge/PR state, and uncommitted-file status. |
| Scope | Requirement/rule IDs, completed behavior, explicit non-goals, and files changed. |
| Evidence | Exact test/lint/type/build commands, results, and environment version. |
| Configuration | Variable names and secret-storage location only; never values, tokens, URLs containing passwords, or DPAPI blobs. |
| External state | Read/write action taken against PostgreSQL, SQL Server, AIMS, filesystem delivery, or none. |
| Risks and next action | Open question/blocker, decision owner, and one smallest safe next action. |

### Latest handoff checkpoint

- **Timestamp / owner:** 2026-08-25 17:26 +08:00; Codex Task 3 planning session.
- **Issue:** GitHub #1, `Task 3: add durable PostgreSQL workflow state`, assigned to the authenticated account.
- **Git state:** local branch `codex/task-3-durable-workflow-state` in `.worktrees/codex-task-3-durable-state`, HEAD `9731e3976c9b9e453a1649e57b0f929830a2a2ae` at checkpoint start; no PR, merge, or push. The approved design, follow-on plan, and reconciled source-of-truth documents are committed; this progress-only checkpoint is the sole uncommitted change.
- **Scope:** The automated ACL-restricted CSV file/manifest/acknowledgement contract is approved. Its implementation is a separate follow-on plan and does not expand Task 3. No persistence, CSV adapter, scheduler, AIMS, or production behavior was implemented.
- **Evidence:** The verified legacy SKU CSV has 42 ordered fields, UTF-8 encoding, comma delimiter, DOS line endings, and no header. Local Python 3.12.7 checks recorded 37 tests passing, Ruff passing, and mypy passing.
- **Configuration:** Task 3 still requires `ESL_TEST_DATABASE_URL` through the approved secret boundary. Future CSV settings are names/contract only; no path, SID, credential, or secret value is stored in documentation.
- **External state:** no PostgreSQL, SQL Server, AIMS, Jenkins, Hop, physical ESL, or filesystem-delivery action occurred.
- **Risks / next action:** Task 3 remains blocked on the non-production PostgreSQL URL. The smallest safe next action is to provision `ESL_TEST_DATABASE_URL` outside source control, then write and run the required failing Task 3 lease integration test before implementation.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-25; Codex project-preparation session.
- **Issue:** no implementation issue is active. Next planned issue is durable PostgreSQL workflow state (Task 3 of `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`).
- **Git state:** `main` tracking `origin/main`; the preparation workflow/templates/testing-plan commit `7bb4cef` is pushed. The next agent should start from its own issue branch/worktree and record its resulting HEAD in a new checkpoint.
- **Scope completed:** Task 1 Python/React/Vite/Tailwind scaffold and CI; Task 2 settings and Windows DPAPI secret-boundary hardening. BR-005 remains on hold.
- **Evidence:** Task 2 final review recorded 37 Python tests passing, Ruff passing, and mypy passing. CI targets Python 3.12 and Node 22; local verification also needs to be repeated under the selected Python 3.12 runtime.
- **Configuration:** `.env.dev.example` and `.env.production.example` are credential-free references. A future Task 3 integration environment needs `ESL_TEST_DATABASE_URL`; no value has been supplied or stored.
- **External state:** GitHub `main` was pushed after safely merging the remote initial README and licence. No production or integration database, AIMS, SQL Server, Hop, Jenkins, or ESL side effect occurred.
- **Risks / next action:** Create and assign the Task 3 GitHub issue, create its branch/worktree, set a dedicated non-production `ESL_TEST_DATABASE_URL` outside source control, and implement only Task 3 under the approved plan.
# Completed

- Inspected all supplied current-system evidence under `docs/sql-server/` and `docs/hop-jenkins-pipeline/`.
- Established the approved target direction: one modular service with durable execution state, rather than a microservice platform or another ETL script.
- Documented a temporary read-only AIMS PostgreSQL compatibility adapter for the first cutover; vendor-supported APIs remain the preferred long-term boundary.
- Created the initial source-of-truth specification, architecture, and operations documents.
- Created the approved foundation-and-shadow implementation plan at `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`.
- Implemented and independently reviewed Task 1: Python/React/Tailwind project scaffold and CI.
- Implemented and independently reviewed Task 2: validated settings and the production DPAPI secret boundary, including Windows Service identity, known-folder, owner, and ACL safeguards.

# In Progress

- BR-005 promotion precedence is on hold pending POS/merchandising decision.
- Discovery of the unknown operational and business contracts listed below.

# Next

1. Obtain the Jenkins job definitions, schedules, command lines, service account, and job history.
2. Obtain SQL Agent run history for `Refresh ESL Data`; its job definition and schedule have been captured.
3. Identify the legacy CSV consumer owner and validate the approved automated file/manifest/acknowledgement contract in non-production.
4. Obtain SOLUM-supported API documentation and decide the retirement path for compatibility reads.
5. Confirm promotion precedence, price category, and time/day eligibility.
6. Measure workload, schedule, latency, failure, and recovery baselines.
7. Establish the local Git repository and isolated implementation workspace, then execute the approved plan task by task with review gates.

# Decisions Made

- **AD-001:** The target is a modular single deployable service with clear internal component boundaries and a durable state store.
- **AD-002:** AIMS is a vendor-owned external bounded context. Direct AIMS database writes are prohibited.
- **AD-003:** First cutover may use least-privilege, read-only PostgreSQL compatibility queries encapsulated behind an adapter while supported API alternatives are investigated.
- **AD-004:** No repository-local project skill is created yet. The official documentation search did not establish a repository-local discovery convention that can be verified for this environment; there is no repeatable project-specific workflow requiring one at this phase.
- **AD-005:** Canonical records are compared in memory and their hashes/differences, checkpoints, and delivery state are persisted in the target service database. Physical CSV files are not target workflow state, comparison evidence, or audit; they remain only as a temporary compatibility delivery adapter if an external consumer requires them.
- **AD-006:** Use Python + PostgreSQL as an internal web application with browser UI/API and CLI. It can run continuously as a dedicated Windows Service or directly by command line for development, diagnostics, and controlled administration. PostgreSQL is the target state/audit/queryable-log database.
- **AD-007:** The available high-spec Windows 10 PC is accepted as the production host under explicit operational risk acceptance. Builds/releases use GitHub Actions to create approved immutable artifacts, which are currently transferred through a controlled method and installed locally. GitHub access may be enabled later after security review and outbound HTTPS allowlisting; secrets remain Windows DPAPI-protected, ACL-restricted per-environment bundles outside the repository.
- **AD-008:** Use FastAPI for the internal backend/API and React + TypeScript + Vite + Tailwind CSS for the browser UI. Google Stitch exports are versioned design handoffs, not production application code; React uses authenticated FastAPI endpoints only.
- **AD-012:** Use an automated ACL-restricted CSV/ready-manifest/acknowledgement handshake for bounded compatibility delivery. PostgreSQL remains authoritative for lifecycle/audit; filesystem presence is not completion, no blind resend is permitted, and the adapter stays disabled until consumer acceptance.

# Risks / Blockers

- **High:** Jenkins definitions/schedules/runtime commands and historical run/failure data remain unavailable. SQL Agent job definition/schedule is now captured, but its run history is not.
- **High:** Promotion selection is not deterministic for overlapping campaigns; time/day and priority semantics require business confirmation.
- **High:** The target CSV contract is approved, but the legacy consumer owner has not accepted its schema, service identities/ACLs, timeout, retention, or rollback; production delivery therefore remains disabled.
- **Medium:** Current AIMS reads rely on database structures that are not vendor-supported contracts.
- **Medium:** Current AIMS page-change traffic is HTTP in the supplied artifact; support, authentication, and TLS capability must be verified.
- **Medium:** No current workload, timing, availability, RPO/RTO, or recovery baseline was supplied.

# Discoveries

- **VERIFIED:** `dbo.RefreshESL_New` processes stores `075` and `084`, builds product/promotion state, and transactionally deletes, updates, and inserts `ESL.dbo.tb_ESL` rows.
- **VERIFIED:** SQL Agent job `Refresh ESL Data` executes `RefreshESL_New` every 30 minutes from 07:00 to 23:59 daily, with zero retries.
- **VERIFIED:** `RefreshESL_New` currently enumerates two stores (`075` and `084`); target requirements now require a configurable multi-store workflow that can extend beyond them.
- **VERIFIED:** For scalable `KGS` items, the source price is per kilogram and ESL display price is per 100 grams (`50,000/kg` → `5,000/100GR`).
- **VERIFIED:** The supplied Hop masters constrain their SQL to store `084` and invoke SKU and promotion branches.
- **VERIFIED:** SKU processing compares `tb_ESL` with AIMS Portal article data and creates a CSV of new/changed SKU data.
- **VERIFIED:** The newest `backup-pipeline/esl-sku-promo-multi-page.hpl` enables Page 3 REST and uses `STORE_CODE_PARAM`; the older `backup pipeline` snapshot is historical evidence with different behavior.
- **VERIFIED:** Hop uses SQL Server, AIMS Portal PostgreSQL, local files, and an AIMS Dashboard page-change HTTP endpoint. Credentials/configuration references exist in evidence but are intentionally not reproduced here.
- **VERIFIED:** The live AIMS Dashboard OpenAPI documents `POST /common/labels/page` for page changes, but declares no API security scheme. It accepts a store query parameter and a `pageChangeList` of label/page pairs and returns response code/message plus batch ID.
- **VERIFIED:** CSV samples provide evidence of Page 1/Page 2/Page 3/Page 4 operational outputs but do not identify a SKU CSV consumer or acknowledgement contract.
- **VERIFIED:** The legacy SKU output definition contains 42 ordered fields, UTF-8 encoding, comma delimiter, DOS line endings, and no header. The approved target contract adds automatic atomic publication and matching acknowledgement without treating files as durable state.
- **INFERRED:** The supplied artifacts are a snapshot/backup and are not a complete representation of production.

# Verification Performed

- Enumerated the repository files and inspected the SQL procedure, table DDL, troubleshooting report, Hop project metadata, all `.hpl`/`.hwf` definitions, database metadata, and the supplied current-state runbook.
- Confirmed required documentation exists and contains no copied credentials or secret values.
