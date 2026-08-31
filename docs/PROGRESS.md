# Current Phase

The **engineering phase began on 2026-08-25** after the dedicated local non-production PostgreSQL test connection was verified. Foundation workflow, validation, promotion-evidence, action-lifecycle, audit/reconciliation, health/configuration, and safe retention/audit-read work through PR #65 are merged into `develop`. The automated CSV compatibility contract remains approved but its follow-on implementation is deferred until its foundation dependencies and the consumer acceptance gate are complete.


## Cross-agent implementation checkpoints

Every checkpoint records these fields.

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

Checkpoints live in `docs/checkpoints/`, one file per checkpoint, named
`<YYYY-MM-DD>-<HHMM>-<owner-and-scope>.md`.

There is deliberately **no index list here**. An index is a single shared region that every
issue would edit, which is exactly the merge conflict this layout removes: two agents adding a
checkpoint touch two different new files and never the same lines. The filename ordering is the
chronological ordering, so the most recent checkpoint is the last filename in the directory.

Before starting work, read the most recent checkpoints:

```bash
ls docs/checkpoints | tail -3
```

Add a checkpoint at issue start, after every independently testable task, after a review/fix
round, when blocked, before changing agents or chats, and after merge. Never include credential
values. Do not edit an existing checkpoint: it is a point-in-time record, so correct it by adding
a newer one.

# Completed

- GitHub #14 merged through PR #55 at 228746e: FR-007 explicit workflow states, terminal behavior, dependency conditions, deterministic ordering, and auditable transition decisions.
- GitHub #18 merged through PR #56 at 3317483: durable restart-safe execution state and recovery behavior.
- GitHub #36 merged through PR #57 at b3ae64e: promotion eligibility, UOM, atomic candidate state, and decision evidence; formal winner priority remains unresolved.
- GitHub #12 merged through PR #59 at 10eb6db: structural source validation and safe quarantine; configured range/domain thresholds remain explicitly deferred to blocked discovery issue #58.
- GitHub #19 merged through PR #60 at 19d1641: durable logical idempotency keys, action lifecycle, attempts, and reconciliation-required outcomes.
- GitHub #25 merged through PR #61 at 422a072: reconciliation balance validation plus durable audit, exception, and event evidence.
- GitHub #27 merged through PR #63 at 61aabcb: liveness/readiness/dependency health and versioned configuration validation. Its GitHub scope differed from the AD-016 Task 7 assignment, so the plan's retention and audit-read work was separately tracked rather than silently folded into #27.
- GitHub #62 merged through PR #65 at 6ba029f: disabled-by-default configurable evidence retention guards and sanitized audit read models, completing the remaining AD-016 Task 7 work. Its identified foreign-key purge limitation is tracked by blocked issue #64, after the Task 8 migration slot is resolved.
- AD-016 Task 2 merged through PR #45 at 4ce8eba, closing #13: configuration, canonical snapshot, and difference persistence with additive migration 0002_configuration_and_snapshots. GitHub Actions verified the branch (Ruff, mypy for 16 source files, pytest 55 passed / 12 skipped without a configured test database, frontend typecheck, Vitest, and Vite build). The same suite reports 67 passed with no skip against the dedicated non-production database, which now rests at 0002.
- Approved authoritative data model merged through PR #41 at 1d1ca8d; root develop is synchronized and post-merge verification passed.
- Inspected all supplied current-system evidence under `docs/sql-server/` and `docs/hop-jenkins-pipeline/`.
- Established the approved target direction: one modular service with durable execution state, rather than a microservice platform or another ETL script.
- Documented a temporary read-only AIMS PostgreSQL compatibility adapter for the first cutover; vendor-supported APIs remain the preferred long-term boundary.
- Created the initial source-of-truth specification, architecture, and operations documents.
- Created the approved foundation-and-shadow implementation plan at `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`.
- Implemented and independently reviewed Task 1: Python/React/Tailwind project scaffold and CI.
- Implemented and independently reviewed Task 2: validated settings and the production DPAPI secret boundary, including Windows Service identity, known-folder, owner, and ACL safeguards.

# In Progress

- BR-005 promotion precedence is on hold pending POS/merchandising decision.
- The GitHub requirement backlog and its later follow-on issues are assigned and tracked through the issue-led `develop` workflow. Implement only a selected, unblocked issue at a time.
- Promotion-rule issue #37 remains open. Its #36 candidate-state and #25 audit/reconciliation prerequisites are complete; it must retain unresolved ambiguity rather than invent winner priority. #38 and representative cases remain required before any deployed-compatibility claim.
- P0 issue #38 blocks source-adapter parity until SQL-owner review removes or explains the direct procedure self-invocation and supplies non-production boundary evidence.
- P2 issue #58 is blocked until merchandising/POS and data owners provide configured range/domain thresholds and severity policy. P2 issue #64 is blocked until AD-016 Task 8 resolves the next migration slot.
- Discovery of the unknown operational and business contracts listed below.

# Next

1. Retain/export complete Jenkins job definitions and broader history if available; the supplied screenshots/logs are manual evidence snapshots.
2. Retain/export SQL Agent history with calendar dates and root-cause detail if it becomes available; the supplied history is a limited snapshot.
3. Identify the legacy CSV consumer owner and validate the approved automated file/manifest/acknowledgement contract in non-production.
4. Obtain SOLUM-supported API documentation and decide the retirement path for compatibility reads.
5. Confirm promotion precedence, same-economic campaign terms, non-CLR UOM conversion, and final weekday-metadata policy.
6. Measure workload, schedule, latency, failure, and recovery baselines.
7. Maintain the AD-016 migration sequence: Task 8 / #21 owns the reserved `0008` authoritative-model-gate migration; issue #64 follows only after that slot is resolved. Issue #37 consumes the completed #25 evidence after Task 8; do not claim deployed compatibility until #38 and representative parity cases are complete.

# Decisions Made

- **AD-001:** The target is a modular single deployable service with clear internal component boundaries and a durable state store.
- **AD-002:** AIMS is a vendor-owned external bounded context. Direct AIMS database writes are prohibited.
- **AD-003:** First cutover may use least-privilege, read-only PostgreSQL compatibility queries encapsulated behind an adapter while supported API alternatives are investigated.
- **AD-004:** No repository-local project skill is created yet. The official documentation search did not establish a repository-local discovery convention that can be verified for this environment; there is no repeatable project-specific workflow requiring one at this phase.
- **AD-005:** Canonical records are compared in memory; complete immutable canonical snapshots under configurable retention, hashes/differences, checkpoints, and delivery state are persisted in the target service database. Physical CSV files are not target workflow state, comparison evidence, or audit; they remain only as a temporary compatibility delivery adapter if an external consumer requires them.
- **AD-006:** Use Python + PostgreSQL as an internal web application with browser UI/API and CLI. It can run continuously as a dedicated Windows Service or directly by command line for development, diagnostics, and controlled administration. PostgreSQL is the target state/audit/queryable-log database.
- **AD-007:** The available high-spec Windows 10 PC is accepted as the production host under explicit operational risk acceptance. Builds/releases use GitHub Actions to create approved immutable artifacts, which are currently transferred through a controlled method and installed locally. GitHub access may be enabled later after security review and outbound HTTPS allowlisting; secrets remain Windows DPAPI-protected, ACL-restricted per-environment bundles outside the repository.
- **AD-008:** Use FastAPI for the internal backend/API and React + TypeScript + Vite + Tailwind CSS for the browser UI. Google Stitch exports are versioned design handoffs, not production application code; React uses authenticated FastAPI endpoints only.
- **AD-013:** `develop` is the remote integration branch. Issue branches start from it, target it through pull requests, use GitHub auto-merge only after required checks/review, and local `develop` is fast-forwarded after each merge.
- **AD-012:** Use an automated ACL-restricted CSV/ready-manifest/acknowledgement handshake for bounded compatibility delivery. PostgreSQL remains authoritative for lifecycle/audit; filesystem presence is not completion, no blind resend is permitted, and the adapter stays disabled until consumer acceptance.
- **AD-014:** Every implementation is traced to one GitHub issue. A meaningful issue/epic branch starts from current `develop`, is committed and pushed after verification, merges back to `develop`, and is followed by a local fast-forward pull of remote `develop`.
- **AD-015:** Promotion behavior is extracted into independently testable domain rules. The initial target follows the supplied reference-directed policy, records ambiguity, and does not invent a campaign winner, generic member filter, non-CLR UOM conversion, or manual-text parsing rule; deployed legacy parity is separately measured and not assumed.
- **AD-016:** Use a hybrid relational plus versioned-JSONB application data model. Stable identities/states/relationships/query fields are relational; complete immutable canonical snapshots and evolving evidence are typed, versioned, hashed JSONB under configurable retention. Business/model documentation is reviewed before app/schema changes.

# Risks / Blockers

- **Medium:** Jenkins configuration screenshots and SQL Agent history are limited manual snapshots; complete Jenkins XML/retry settings and calendar-dated SQL Agent root-cause history remain unavailable.
- **High:** Promotion selection is not deterministic for overlapping campaigns; time/day and priority semantics require business confirmation.
- **High:** The target CSV contract is approved, but the legacy consumer owner has not accepted its schema, service identities/ACLs, timeout, retention, or rollback; production delivery therefore remains disabled.
- **Medium:** Current AIMS reads rely on database structures that are not vendor-supported contracts.
- **Medium:** Current AIMS page-change traffic is HTTP in the supplied artifact; support, authentication, and TLS capability must be verified.
- **Medium:** No current workload, timing, availability, RPO/RTO, or recovery baseline was supplied.
- **High:** The latest supplied `RefreshESL_New` is marked review/test only and directly invokes itself. This indicates apparent unbounded recursion if executed; its safe deployment and production parity are unverified and require #38 DBA/SQL-owner review.

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
- **VERIFIED:** The newly supplied promotion reference directs target policy for primary date/time eligibility, category-`001` regular price, explicit PFS exclusion, structured promotion validation, CLR normalization, no invented non-CLR conversion, post-calculation KGS display transformation, raw `DISC_TEXT`, atomic promotion state, and store/item/selling-UOM isolation. It is not by itself deployed-parity evidence.
- **VERIFIED:** The same reference requires observable ambiguity for different calculated economic outcomes and same-economic/different-term outcomes, while formal priority and effective-price/rounding comparison remain unresolved.
- **VERIFIED:** The latest supplied procedure source is review/test only, directly invokes itself, filters campaign status, and does not establish cross-midnight or calculated-economic parity; #38 captures the SQL-review gate.
- **INFERRED:** The supplied artifacts are a snapshot/backup and are not a complete representation of production.

# Verification Performed

- Enumerated the repository files and inspected the SQL procedure, table DDL, troubleshooting report, Hop project metadata, all `.hpl`/`.hwf` definitions, database metadata, and the supplied current-state runbook.
- Confirmed required documentation exists and contains no copied credentials or secret values.
