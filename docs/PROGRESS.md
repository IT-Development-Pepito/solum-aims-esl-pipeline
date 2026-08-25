# Current Phase

Architecture and specification completed; the foundation-and-shadow implementation plan is approved for subagent-driven execution. No replacement application has been implemented.

# Completed

- Inspected all supplied current-system evidence under `docs/sql-server/` and `docs/hop-jenkins-pipeline/`.
- Established the approved target direction: one modular service with durable execution state, rather than a microservice platform or another ETL script.
- Documented a temporary read-only AIMS PostgreSQL compatibility adapter for the first cutover; vendor-supported APIs remain the preferred long-term boundary.
- Created the initial source-of-truth specification, architecture, and operations documents.
- Created the approved foundation-and-shadow implementation plan at `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`.

# In Progress

- BR-005 promotion precedence is on hold pending POS/merchandising decision.
- Discovery of the unknown operational and business contracts listed below.

# Next

1. Obtain the Jenkins job definitions, schedules, command lines, service account, and job history.
2. Obtain SQL Agent run history for `Refresh ESL Data`; its job definition and schedule have been captured.
3. Identify the consumer and contractual acknowledgement mechanism for `ESL_SKU_*.csv`.
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

# Risks / Blockers

- **High:** Jenkins definitions/schedules/runtime commands and historical run/failure data remain unavailable. SQL Agent job definition/schedule is now captured, but its run history is not.
- **High:** Promotion selection is not deterministic for overlapping campaigns; time/day and priority semantics require business confirmation.
- **High:** The production contract for the SKU CSV output is unknown.
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
- **INFERRED:** The supplied artifacts are a snapshot/backup and are not a complete representation of production.

# Verification Performed

- Enumerated the repository files and inspected the SQL procedure, table DDL, troubleshooting report, Hop project metadata, all `.hpl`/`.hwf` definitions, database metadata, and the supplied current-state runbook.
- Confirmed required documentation exists and contains no copied credentials or secret values.
