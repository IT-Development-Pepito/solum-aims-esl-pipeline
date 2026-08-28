# Current Phase

The **engineering phase began on 2026-08-25** after the dedicated local non-production PostgreSQL test connection was verified. Task 3 durable workflow state and its CI recovery fix are merged into `develop`; GitHub Actions verified the resulting branch. The automated CSV compatibility contract remains approved but its follow-on implementation is deferred until foundation Tasks 3, 6, and 7 and the consumer acceptance gate are complete.


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

- **Timestamp / owner:** 2026-08-28 14:29:00 +08:00; Codex issue #10 final re-review correction checkpoint.
- **Issue:** GitHub #10, [domain] define deterministic canonical records and rule boundaries, assigned to it20pepito; labels type:feature, area:domain, and priority:p1.
- **Git state:** At this checkpoint, codex/10-canonical-records in D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-10-canonical-records is at clean committed HEAD 136a72410d2015cdce7f727e5bf2d65e57c106ab; the only subsequent uncommitted files are this checkpoint and the final-review test correction. Earlier Task 1 commits are 8785562, bf5fb60, 52a4be4, and 2b5ef2f.
- **Scope:** Completed approved AD-016 Task 1 for FR-004, FR-005, and BR-018: immutable pure canonical record contracts, deterministic canonical payload/hash, and sorted path-level diffs. Canonical key is exactly store_code + item_code + selling_uom; KGS source and /100GR display prices remain distinct. No persistence, migration, adapter, scheduler, UI/API, promotion selection, conversion, weekday, or rounding policy was added; alembic/versions/0001_operational_state.py remains unchanged.
- **Evidence:** Python 3.12.7. Initial RED: python -m pytest tests/unit/domain/test_canonical.py tests/unit/domain/test_diff.py -v produced expected ModuleNotFoundError: No module named esl_service.domain. Review correction RED: UTC/naive-datetime regression selection produced 2 expected failures against pre-fix serializer; GREEN: 2 passed. Final-review coverage directly proves raw DISC_TEXT preservation. The SHA-256 assertion now pins the literal canonical UTF-8 JSON vector and separately calculated SHA-256 digest 369f225d5f871f993c9140c74ea1c1920fc469bff24b52f2e2c7f0b300d36c85. Focused literal-vector test and Ruff passed; complete verification will be rerun before the final review.
- **Configuration:** No configuration variable, secret, or approved secret-storage location was read or changed. The existing root .venv was used for verification only.
- **External state:** No PostgreSQL, SQL Server, AIMS, Jenkins, Hop, CSV delivery, device, or production state was read or mutated.
- **Risks / next action:** No new domain policy was introduced. Promotion winner, non-CLR conversion, weekday, and rounding policies remain UNKNOWN / NEEDS-DISCOVERY and intentionally unencoded. Next action: commit this final-review correction, run the complete gate, then obtain final whole-branch review; do not execute a production or external-system action.
- **Timestamp / owner:** 2026-08-28 11:10 +08:00; Codex issue #10 implementation start.
- **Issue:** GitHub #10, `[domain] define deterministic canonical records and rule boundaries`, assigned to `it20pepito`; labels `type:feature`, `area:domain`, and `priority:p1`; no GitHub dependency or blocker is recorded.
- **Git state:** `codex/10-canonical-records` in `D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-10-canonical-records`, created from current `origin/develop` at `5ecead2c2e4037e47d42e5a8886f6e949a430807`; no uncommitted files before this checkpoint.
- **Scope:** Implement only approved AD-016 Task 1: pure immutable canonical domain contracts, deterministic serialization/hash, and path-level record diffs for FR-004, FR-005, and BR-018. Preserve `0001_operational_state.py`; do not add persistence, migrations, adapters, scheduling, UI/API, promotion-winner policy, CSV delivery, or external-system actions.
- **Evidence:** Read the assigned issue, approved AD-016 plan, architecture canonical-snapshot contract, relevant specification/workflow requirements, and verified `tb_ESL` DDL. The legacy DDL confirms source fields; the target canonical key is `store_code + item_code + selling_uom` and source/display KGS price bases remain distinct.
- **Configuration:** No configuration variable or secret value has been read or changed. Existing root `.venv` will be used for verification only.
- **External state:** Created the isolated local worktree/branch only. No PostgreSQL, SQL Server, AIMS, Jenkins, Hop, CSV delivery, device, or production state was read or mutated.
- **Risks / next action:** No issue blocker is recorded. Unresolved promotion priority, same-economic terms, non-CLR conversion, and weekday policy remain out of scope and must not be defaulted. Next action: inspect current tests, write the required failing canonical-contract tests, and capture the expected red result.

- **Timestamp / owner:** 2026-08-28 10:29 +08:00; Codex AD-016 implementation-planning handoff.
- **Issue:** GitHub #42, [docs] plan authoritative data model implementation, assigned to it20pepito; PR #43 is open against develop with type:docs, area:docs, area:persistence, and priority:p1.
- **Git state:** Primary plan commit 738d21d on codex/42-plan-authoritative-data-model is pushed to origin; PR #43 is open and CI is running. This checkpoint is the only subsequent local change.
- **Scope:** Planning is complete. The new authoritative-data-model plan sequences #10, #13, #18, #36, #12, #19, #25/#27, and #21; reserves additive migrations 0002–0008; defines TDD, replay, database-CI, traceability, and review gates; and defers compatibility_delivery to issue #30 after consumer acceptance. No application implementation is authorized in this preparation task.
- **Evidence:** Commit 738d21d contains only docs/PROGRESS.md and three planning files. Local gates passed: plan structure/placeholder/fence/migration checks; git staged diff check; Ruff; mypy; pytest 37 passed/1 skipped; frontend typecheck; Vitest 1 passed; Vite build. PR #43 initial CI runs were in progress when this checkpoint was written.
- **Configuration:** No configuration variable or secret value was read or changed.
- **External state:** Pushed the documentation branch and opened/tagged/assigned PR #43. No PostgreSQL, SQL Server, AIMS, Jenkins, Hop, CSV delivery, device, or production state was read or mutated.
- **Risks / next action:** Do not implement until PR #43 is reviewed and merged. Retention durations, promotion winner policy, and CSV consumer acceptance remain UNKNOWN / NEEDS-DISCOVERY. Next action is review the plan, merge it if approved, then start only the first selected issue in a separate implementation task.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-28 10:26 +08:00; Codex AD-016 implementation-planning session.
- **Issue:** GitHub #42, [docs] plan authoritative data model implementation, labels type:docs, area:docs, area:persistence, priority:p1, assigned to it20pepito.
- **Git state:** codex/42-plan-authoritative-data-model in D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-42-plan-authoritative-data-model at base 1d1ca8d; the verified documentation set is ready to commit and submit to develop.
- **Scope:** Added the focused AD-016 implementation plan with eight issue-bounded TDD tasks, immutable additive revisions 0002–0008, exact files/interfaces, replay/CI/traceability gates, and explicit deferral of compatibility_delivery. Added dated supersession/sequencing notices to the foundation and CSV plans. Application code and database/external-system changes remain out of scope.
- **Evidence:** Plan structure check: exact header, 8 uniquely ordered task headings, 43 checkbox steps, 68 balanced code fences, revisions 0002–0008 present, no unresolved placeholder markers, and final newline present. Python 3.12.7: Ruff passed; mypy passed for 7 source files; pytest produced 37 passed/1 skipped. Node 22.23.2/npm 12.0.2: npm ci reported zero vulnerabilities; frontend typecheck passed; Vitest produced 1 passed; Vite production build passed.
- **Configuration:** No configuration variable or secret value was read or changed. Verification used the existing root .venv only; ESL_TEST_DATABASE_URL was not required or accessed.
- **External state:** Created/assigned GitHub issue #42 earlier in this session. No PostgreSQL, SQL Server, AIMS, Jenkins, Hop, CSV delivery, device, or production state was read or mutated.
- **Risks / next action:** Retention durations, promotion winner policy, and CSV consumer acceptance remain UNKNOWN / NEEDS-DISCOVERY and are deliberately blocked in the plan. Commit and push the documentation, open a PR to develop, and obtain plan review before any implementation task starts.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-28 10:06 +08:00; Codex AD-016 implementation-planning session.
- **Issue:** GitHub #42, [docs] plan authoritative data model implementation, labels type:docs, area:docs, area:persistence, priority:p1, assigned to it20pepito.
- **Git state:** codex/42-plan-authoritative-data-model in D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-42-plan-authoritative-data-model, created from synchronized develop at merge commit 1d1ca8d; plan and checkpoint are uncommitted.
- **Scope:** Planning only for AD-016 and the authoritative data model in SYSTEM_ARCHITECTURE.md section 5. The plan must define additive migration order, exact files/interfaces, TDD evidence, issue boundaries, and supersession of pre-AD-016 plan portions. Application code and database/external-system changes are out of scope.
- **Evidence:** PR #41 merged issue #40 into develop as 1d1ca8d; local and remote develop match. Post-merge verification: documentation mapping covers all 42 legacy tb_ESL columns; Ruff and mypy passed; Python tests produced 37 passed/1 skipped; frontend typecheck passed; Vitest produced 1 passed; Vite production build passed.
- **Configuration:** No configuration variable or secret value was read or changed.
- **External state:** Merged approved PR #41 and created/assigned GitHub issue #42 plus its local plan worktree. No PostgreSQL, SQL Server, AIMS, Jenkins, Hop, CSV delivery, device, or production state was read or mutated.
- **Risks / next action:** Exact retention durations and unresolved promotion/consumer policies remain discovery gates. Write and self-review the focused implementation plan without encoding those unknowns, then submit it through a PR to develop for review.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-28 09:51 +08:00; Codex authoritative-data-model documentation session.
- **Issue:** GitHub #40, [docs] define authoritative application data model, labels type:docs, area:docs, area:persistence, priority:p1, assigned to it20pepito.
- **Git state:** codex/40-authoritative-data-model in D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-40-authoritative-data-model, branched from synchronized local/remote develop at bb813bb; the primary verified data-model change is commit 91a0d98 and the PR is pending.
- **Scope:** Approved hybrid relational plus versioned-JSONB model for FR-002–FR-027 as applicable, BR-004–BR-019, NFR-002, NFR-005–NFR-012, and NFR-014. SYSTEM_ARCHITECTURE.md defines ownership, entities, keys, canonical snapshot contract, promotion evidence, action/reconciliation lifecycles, retention, indexing, security, application mapping, and verification. WORKFLOW.md adds the documentation-first model-change procedure and stage-correct reconciliation balances. No application code or migration is in scope.
- **Evidence:** Read the latest complete SPECIFICATION.md, current SYSTEM_ARCHITECTURE.md/WORKFLOW.md/PROGRESS.md, implemented Task 3 SQLAlchemy models and immutable migration, legacy tb_ESL DDL, and the approved foundation/shadow plan. Clean baseline using Python 3.12.7: python -m pytest -q produced 37 passed, 1 skipped. Final checks: model-specific documentation integrity passed for 32 requirement/rule references, all 42 legacy tb_ESL columns, 21 entities, and 16 unique architecture decisions; git diff --check passed; Ruff passed; mypy passed; pytest produced 37 passed, 1 skipped; npm ci reported zero vulnerabilities; frontend typecheck passed; Vitest produced 1 passed; Vite production build passed.
- **Configuration:** No configuration variable or secret value was read or changed.
- **External state:** Created and assigned GitHub issue #40 and created its local branch/worktree. No PostgreSQL, SQL Server, AIMS, Jenkins, Hop, CSV delivery, device, or production state was read or mutated.
- **Risks / next action:** Exact retention durations, unresolved promotion priority/rounding/non-CLR conversion/weekday policies, and CSV consumer acceptance remain NEEDS-DISCOVERY. Push the verified branch, open its PR to develop, and request written-design review. The pre-AD-016 implementation plan must be revised after written-design approval before snapshot persistence is implemented.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-27 17:45 +08:00; Codex promotion-evidence reconciliation session.
- **Issue:** GitHub #35, `[docs] reconcile promotion-rule evidence and SQL review risk`, assigned to `it20pepito`. Created child issues #36 (promotion eligibility/UOM/atomic state) and #37 (compatibility selection/ambiguity audit) under epic #5, plus P0 SQL-review gate #38.
- **Git state:** `codex/35-reconcile-promotion-evidence` branched from local and remote `develop` at `466e0c0a0ae854981e6e8b14ae8c1d26c3765186`; supplied evidence files and documentation reconciliation are uncommitted.
- **Scope:** New reference `docs/sql-server/ESL_Promotion_Business_Logic_and_Business_Rules_Reference.md` is current evidence. The latest supplied review-only procedure adds Patch 2.5 promotion handling; its direct self-invocation is recorded as an apparent unbounded-recursion defect, not execution proof. Requirements, architecture, workflow, and affected backlog items are being reconciled without implementation or production changes.
- **Evidence:** Inspected the supplied reference and UTF-16 procedure text. The reference directs target rules for date/time eligibility, category-`001` price, PFS exclusion, CLR handling, no invented non-CLR conversion, scalable-item ordering, raw `DISC_TEXT`, atomic state, store/item/UOM key, and observable ambiguity. The review-only procedure still filters campaign status, its date filter does not establish cross-midnight parity, and its raw type/price/percent comparison does not establish calculated-economic parity. Formal winner priority, same-economic display-term selection, final weekday policy, authoritative non-CLR conversion, and deployed parity remain UNKNOWN / NEEDS-DISCOVERY.
- **Configuration:** No configuration value, credential, or connection string was read or changed.
- **External state:** Created GitHub #35–#38 only. No database, Jenkins, Hop, AIMS, device, or production system was changed.
- **Risks / next action:** Complete documentation and issue-body updates, verify documentation and tests, then submit #35 through a PR to `develop`. Issue #38 requires DBA/SQL-owner review before any procedure execution or deployment decision.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-26 08:xx +08:00; Codex issue-backlog and workflow session.
- **Issue:** GitHub #33, `[docs] make issue-led develop integration workflow explicit`, assigned to `it20pepito`. GitHub #1 is closed: its durable-state implementation merged through PR #2 and CI recovery through PR #3.
- **Git state:** `codex/33-issue-led-develop-workflow` worktree, created from local and remote `develop` at `54107c9fb1a52f9f3d01d66f471e9103f8f7002f`; documentation change is in progress and uncommitted.
- **Scope:** Created five assigned parent epics (#5–#9) and 23 detailed child issues (#10–#32). They cover every functional requirement, FR-001 through FR-030; #23, #24, and #30–#32 remain explicitly blocked pending their documented external contracts. Issue #33 makes issue-led development, meaningful `develop`-based branches, commit/push, `develop` merge, and local fast-forward pull explicit.
- **Evidence:** Compared the unique `FR-###` identifiers from `docs/SPECIFICATION.md` with the GitHub epic/child bodies: all 30 identifiers are covered. Baseline verification in this worktree using the existing root Python 3.12 virtual environment: `python -m pytest -q` → 37 passed, 1 skipped.
- **Configuration:** No configuration value was read or changed. `ESL_TEST_DATABASE_URL` remains external to source control.
- **External state:** Created/edited GitHub issues and labels only; created a local issue worktree. No production, database, Jenkins, Hop, AIMS, or ESL system was changed.
- **Risks / next action:** `develop` still lacks required-check enforcement. Complete #33 through a PR to `develop`, pull the resulting remote `develop` locally, then select the highest-priority unblocked child issue for implementation.

### Previous handoff checkpoint

- **Timestamp / owner:** 2026-08-26 08:14 +08:00; Codex Task 3 merge-recovery session.
- **Issue:** GitHub #1, `Task 3: add durable PostgreSQL workflow state`, closed by merged PR #2; PR #3 delivered its CI recovery fix.
- **Git state:** remote and root local `develop` are synchronized at `a44d32c7176450e72cf1083137ca703fe666eee2`. PR #2 merged Task 3; PR #3 merged the CI fixture fix. This documentation-only branch is uncommitted pending review.
- **Scope:** FR-017 durable state is merged into `develop`. The integration fixture now skips only when `ESL_TEST_DATABASE_URL` is absent, while the dedicated local PostgreSQL run still proves exclusive store-scope ownership. No scheduler, CSV adapter, AIMS, SQL Server, or production behavior was added.
- **Evidence:** PR #2 initially failed CI because no database URL is available in GitHub Actions. PR #3 GitHub Actions checks passed: Python test/lint/type checks and frontend typecheck/test/build. Local CI-equivalent Python verification recorded 37 passed/1 skipped; local PostgreSQL integration recorded 1 passed.
- **Configuration:** `ESL_TEST_DATABASE_URL` remains external to source control and is required only for the dedicated PostgreSQL integration run. CI intentionally receives no database URL and skips that integration test.
- **External state:** remote `develop` was created from `origin/main`; PRs #2 and #3 merged automatically into it, and the root local checkout was fast-forwarded after each merge. No production system was changed.
- **Risks / next action:** `develop` does not yet enforce required GitHub checks, so PR #2 merged before its initial CI failure completed. Obtain repository-owner approval to protect `develop` with the `verify` check before the next feature PR.

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

- Approved authoritative data model merged through PR #41 at 1d1ca8d; root develop is synchronized and post-merge verification passed.
- Inspected all supplied current-system evidence under `docs/sql-server/` and `docs/hop-jenkins-pipeline/`.
- Established the approved target direction: one modular service with durable execution state, rather than a microservice platform or another ETL script.
- Documented a temporary read-only AIMS PostgreSQL compatibility adapter for the first cutover; vendor-supported APIs remain the preferred long-term boundary.
- Created the initial source-of-truth specification, architecture, and operations documents.
- Created the approved foundation-and-shadow implementation plan at `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`.
- Implemented and independently reviewed Task 1: Python/React/Tailwind project scaffold and CI.
- Implemented and independently reviewed Task 2: validated settings and the production DPAPI secret boundary, including Windows Service identity, known-folder, owner, and ACL safeguards.

# In Progress

- GitHub #42 is producing the focused AD-016 implementation plan; no application or database implementation is authorized in this issue.
- BR-005 promotion precedence is on hold pending POS/merchandising decision.
- The GitHub requirement backlog is ready: parent epics #5–#9 and assigned child issues #10–#32. Implement only a selected, unblocked child issue at a time through the issue-led `develop` workflow.
- Promotion-rule child issues #36 and #37 are ready under epic #5; formal winner priority, same-economic campaign terms, non-CLR conversion, and final weekday policy remain unresolved.
- P0 issue #38 blocks source-adapter parity until SQL-owner review removes or explains the direct procedure self-invocation and supplies non-production boundary evidence.
- Discovery of the unknown operational and business contracts listed below.

# Next

1. Retain/export complete Jenkins job definitions and broader history if available; the supplied screenshots/logs are manual evidence snapshots.
2. Retain/export SQL Agent history with calendar dates and root-cause detail if it becomes available; the supplied history is a limited snapshot.
3. Identify the legacy CSV consumer owner and validate the approved automated file/manifest/acknowledgement contract in non-production.
4. Obtain SOLUM-supported API documentation and decide the retirement path for compatibility reads.
5. Confirm promotion precedence, same-economic campaign terms, non-CLR UOM conversion, and final weekday-metadata policy.
6. Measure workload, schedule, latency, failure, and recovery baselines.
7. Obtain reference-policy approval and representative deployed parity cases for campaign status/date-time, cross-midnight, calculated-economic comparison, and existing-state selection; resolve #38 before source-adapter parity, then implement #36 and #37 through the issue-led `develop` workflow.

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
