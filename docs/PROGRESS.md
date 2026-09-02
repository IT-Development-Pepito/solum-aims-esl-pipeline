# Current Phase

The **engineering phase began on 2026-08-25** after the dedicated local non-production PostgreSQL test connection was verified. Foundation workflow, validation, promotion-evidence, action-lifecycle, audit/reconciliation, health/configuration, safe retention/audit-read, promotion compatibility selection, failure classification and bounded retry, AIMS adapter boundaries, recurring schedules with auditable manual launch, per-scope launch ownership, and operator status, retry, and bounded replay controls work through PR #84 are merged into `develop`. A local clone of both AIMS databases now exists, so vendor-facing work can proceed without touching production AIMS. The automated CSV compatibility contract remains approved but its follow-on implementation is deferred until its foundation dependencies and the consumer acceptance gate are complete.


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
- GitHub #37 merged through PR #69 at 873202e: the versioned `compatibility-v1` promotion selection strategy and its two BR-019 ambiguity reason codes, mapped into the record-issue trail. No campaign winner is invented, and deployed parity remains unclaimed pending #38 and representative cases.
- GitHub #20 merged through PR #70 at 7b2c717: the SYSTEM_ARCHITECTURE section 8 failure classification matrix and a bounded retry policy with externalised attempts, timeout, backoff, and jitter, recorded in the sanitized configuration snapshot. An unrecognised dependency and failure pair raises rather than defaulting.
- GitHub #22 merged through PR #71 at 35c2986: FR-018 AIMS adapter ports in `application/contracts.py` with typed outcomes for reconciliation and retry, plus AST-based architecture tests that reject direct AIMS implementation imports. Those tests caught and corrected a real domain-to-configuration layering inversion introduced by #20.
- GitHub #15 merged through PR #72 at cc964f4: FR-008 configured recurring cadences evaluated to the minute in each schedule's own timezone, enable/disable control, and manual launch carrying operator identity and reason, with schedule configuration, enable/disable changes, and launch source all audit-visible.
- GitHub #16 merged through PR #77 at f2f0bc8: FR-011 and FR-012 bounded execution status queries, a failed-only linked retry that refuses an unresolved external action, and an explicit bounded source-window replay, all requiring operator identity and reason and all audit-visible. Implemented by Codex, which stopped at a usage limit after pushing; the pull request was opened and independently re-verified rather than trusted from its checkpoint.
- GitHub #17 merged through PR #73 at 902a77a: FR-009 and FR-017 per-scope ownership taken at launch under the versioned `no-simultaneous-ownership-v1` policy. A contended launch is rejected and creates no execution, and neither a scheduled nor a manual request displaces a live owner.
- AD-016 Task 2 merged through PR #45 at 4ce8eba, closing #13: configuration, canonical snapshot, and difference persistence with additive migration 0002_configuration_and_snapshots. GitHub Actions verified the branch (Ruff, mypy for 16 source files, pytest 55 passed / 12 skipped without a configured test database, frontend typecheck, Vitest, and Vite build). The same suite reports 67 passed with no skip against the dedicated non-production database, which now rests at 0002.
- Approved authoritative data model merged through PR #41 at 1d1ca8d; root develop is synchronized and post-merge verification passed.
- Inspected all supplied current-system evidence under `docs/sql-server/` and `docs/hop-jenkins-pipeline/`.
- Established the approved target direction: one modular service with durable execution state, rather than a microservice platform or another ETL script.
- Documented a temporary read-only AIMS PostgreSQL compatibility adapter for the first cutover; vendor-supported APIs remain the preferred long-term boundary.
- Created the initial source-of-truth specification, architecture, and operations documents.
- Created the approved foundation-and-shadow implementation plan at `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`.
- Implemented and independently reviewed Task 1: Python/React/Tailwind project scaffold and CI.
- Implemented and independently reviewed Task 2: validated settings and the production DPAPI secret boundary, including Windows Service identity, known-folder, owner, and ACL safeguards.

# Open Work / Blockers

- BR-005 promotion precedence is on hold pending POS/merchandising decision.
- The GitHub requirement backlog and its later follow-on issues are assigned and tracked through the issue-led `develop` workflow. Implement only a selected, unblocked issue at a time.
- Promotion-rule issue #37 is merged, but it deliberately retains unresolved ambiguity rather than inventing winner priority. #38 and representative cases remain required before any deployed-compatibility claim.
- P0 issue #38 blocks source-adapter parity until SQL-owner review removes or explains the direct procedure self-invocation and supplies non-production boundary evidence.
- P2 issue #58 is blocked until merchandising/POS and data owners provide configured range/domain thresholds and severity policy. P1 issue #21 remains blocked by the unimplemented adapters in #11, #23, and #24; #20 and the #22 adapter ports are complete. P2 issue #64 follows only after #21 resolves the reserved migration slot.
- Retry limits, timeout, backoff bounds, and jitter ratio shipped with #20 are **provisional operational defaults**, not measured targets. NFR-004 requires each target to come from a measured baseline and no workload baseline has been captured, so these values must be reviewed against measured latency and failure data before production acceptance.
- Concrete adapter fault mapping for #20 is deferred: translating a specific SQL Server, AIMS API, or compatibility-read error into a documented failure signal belongs with the concrete adapters #11, #23, and #24. The classification matrix, retry policy, and the #22 ports that carry a `FailureSignal` are complete and independent of them.
- The #22 AIMS ports are synchronous, matching every existing module; the service has no async runtime. The approved foundation plan's Task 5 sketch shows an `async def` page-change adapter. Adapter issues #23 and #24 must either implement synchronously or raise the async decision explicitly before the port signature changes.
- The AIMS label read model is modelled in #22 with only the fields the page-change boundary uses. Its remaining columns, and the vendor's valid page range, are UNKNOWN / NEEDS-DISCOVERY and are deliberately not constrained; supported API documentation is still outstanding.
- The architecture requires a unique active schedule identity per workflow and store, and the `workflow_schedule` table does not yet enforce it. #15 did not add the constraint: the AD-016 plan assigns final indexes and checks to the reserved `0008_authoritative_model_gate` migration under #21, which also makes the schedule's configuration version non-null after an explicit backfill. Until #21 lands, duplicate schedules for one scope are prevented by convention rather than by the database.
- #15 records the operator identity and reason on every manual launch but does not evaluate authorization. Role checking is FR-023 and belongs to #26, which depends on #15, #16, and the #28 runtime interfaces.
- The #15 scheduler evaluates cadences but nothing ticks them yet. The timed loop that calls `due_schedules` once a minute is part of the #28 Windows Service and CLI hosting work; until then a schedule launches only when a caller supplies the instant.
- #17 implements the initial no-simultaneous-ownership policy as `no-simultaneous-ownership-v1`, which is deliberately symmetric: neither a scheduled nor a manual request displaces a live owner. FR-017 asks for a defined priority between the two, and no document approves one, so any preference remains UNKNOWN / NEEDS-DISCOVERY pending a business owner. The version is recorded on every decision so a later approved policy is distinguishable in the audit trail.
- A contended launch is rejected rather than queued. Queueing was not chosen because the approved state graph allows only `QUEUED -> RUNNING`, so a queued contender could never be cancelled or expired, and nothing yet starts queued work. If a business owner later approves queueing, both the state graph and a deferred-launch worker are prerequisites.
- The `ScopeContention` guard in the launch path, which rolls back the execution insert when the atomic claim loses a race after the ownership check passed, is defensive and is not covered by an automated test. Reproducing it needs two committed concurrent transactions, which the rollback-based integration fixtures exclude by design. The durable guarantee remains the claim's own conditional predicate.
- Issue #11's scope assumes a single SQL Server source. The discovered topology is three tiers with a runtime-discovered set of per-store servers, so the issue must be rewritten and probably split before it is selected. Its non-goal "Writing to SQL Server" still holds and is reinforced: the replacement writes only to its own PostgreSQL.
- The boundary between "read logic to preserve" and "business logic to move into code" is decided as follows: adapters fetch raw rows bounded only by store and source window, and the domain applies every predicate that carries business meaning. Filtering in the adapter would discard rejected rows without a trace, which is the troubleshooting difficulty the replacement exists to remove; filtering in the domain lets each rejection be recorded as a `record_issue` with its reason.
- Credential handling for the new sources is tracked by #78, which is blocked by #79 because the secret bundle has a reader but no writer. The DPAPI scope decision, user or machine, is required before #79 can be implemented and cannot be deferred: the existing reader passes flag `0`, so scope is fixed at encryption time.
- A local clone of `AIMS_PORTAL_DB` (56 tables plus 3 foreign tables) and `AIMS_CORE_DB` (20 tables) now exists, so #24 can be developed and its least-privilege criterion exercised without touching production AIMS. The procedure and the failure modes hit while producing it are tracked by #82; a `SELECT`-only `esl_aims_reader` role mirrors the production identity, and the grant for `AIMS_CORE_DB` is still outstanding.
- #79 delivers `esl-admin secrets set|remove|list` and `esl-admin check-connections`, so a credential can be provisioned into the DPAPI bundle and proven to work without the service running. DPAPI scope is **user scope** (AD-017): the bundle must be written by the Windows Service account, and an administrator resetting that account's password makes it permanently unreadable, which `WORKFLOW.md` now documents with its recovery. Three limits are recorded rather than hidden: the audit entry for a secret change is best-effort, because the state store's own password is provisioned this way and may not be reachable yet; the SQL Server probe is classified by SQLSTATE and unit-tested against synthetic errors only, since no SQL Server is reachable from the development machine; and the ACL writer is proven against the production validator's own reader on Windows, which is also where CI runs. Once #79 merges, #78 is unblocked.
- The local AIMS clone procedure is undocumented and took several attempts to get right, so #82 records it. Without it the next person repeats a multi-attempt diagnosis whose failures all present with misleading symptoms.
- `AimsLabel` and the `fetch_labels` contract described in #22 need correcting before #24 consumes them. The displayed page is a JSON field in Core rather than the integer column in Portal, `-1` is an unassigned sentinel rather than a page number, and AIMS tracks three page concepts where the port models one. Corrections are recorded on #22 and #24.
- Discovery of the unknown operational and business contracts listed below.

# Next

1. Retain/export complete Jenkins job definitions and broader history if available; the supplied screenshots/logs are manual evidence snapshots.
2. Retain/export SQL Agent history with calendar dates and root-cause detail if it becomes available; the supplied history is a limited snapshot.
3. Identify the legacy CSV consumer owner and validate the approved automated file/manifest/acknowledgement contract in non-production.
4. Obtain SOLUM-supported API documentation and decide the retirement path for compatibility reads.
5. Confirm promotion precedence, same-economic campaign terms, non-CLR UOM conversion, and final weekday-metadata policy.
6. Measure workload, schedule, latency, failure, and recovery baselines.
7. Do not select Task 8 / #21 until its remaining adapter dependencies #11, #23, and #24 are complete; #20 and the #22 ports are done. Then preserve #21's reserved `0008` authoritative-model-gate migration before #64. #37 is merged, but its deployed-parity evaluation still follows Task 8, and deployed compatibility must not be claimed until #38 and representative parity cases are complete.
8. #79 is the only unblocked implementable issue and needs no database access, so it can proceed while read-only identities and network routes are still being arranged. It unblocks #78, which in turn is a prerequisite for verifying #11 and #24 against real data. #82 is documentation and can run alongside.
9. Both P0 issues are blocked on human input rather than engineering: #23 and #24 need SOLUM-supported API documentation, and #38 needs SQL-owner review. One vendor document and one review would unblock four issues, which is a larger return than any single implementable issue currently offers.

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
- **VERIFIED:** The procedure reads **three source tiers**, not one SQL Server. `DBWH_8555` is reached by three-part name, so it is another database on the same instance as `ESL`, and supplies `DimStore`, `DimItemMapping`, and `FactCampaign`. `PEPITO_HO`, the central iRetail database, is reached by four-part name through a linked server and supplies `ITEM_UOM_MAPPING_MST`. Each store runs a local iRetail SQL Server reached by four-part name through a per-store linked server, supplying twelve campaign, item, stock, and movement tables. Sixteen source objects are live in total.
- **VERIFIED:** Per-store connection targets are **data-driven, not configured**. The procedure reads `ORG_IP` and `ORG_DB` from `DBWH_8555.dbo.DimStore` for each store code and builds four-part names by string concatenation, so the set of store servers is discovered at runtime. `ORG_IP` holds a bare IP address with no port. This is consistent with FR-026: adding a store is data, not code.
- **VERIFIED:** Three referenced objects are **dead code**, sitting inside commented-out blocks and never executed: the `STORE_OPS_APP` linked server, the `benchmark_master_selected_items` table it fronts, and `DimItem`. All three appear only in the disabled `REDLIST` update, so none is a dependency of the replacement.
- **VERIFIED:** Because that block is disabled, `tb_ESL.REDLIST` is never populated and retains the empty string it is initialised with. Whether the field is permanently retired or temporarily switched off is **UNKNOWN / NEEDS-DISCOVERY**; the replacement replicates the current behaviour and invents nothing.
- **VERIFIED:** The AIMS label read model lives in `AIMS_PORTAL_DB.end_device_templates (label_code, name, page, station_code)`, keyed `(label_code, page)`. Its `station_code` is **not** a store code: all 10,834 rows hold the literal `DEFAULT_STATION_CODE`. The real store code is on `end_device` and `end_device_articles`, so reading labels for a store requires joining `end_device` to `end_device_templates` rather than filtering the template table.
- **VERIFIED:** `AIMS_PORTAL_DB` reaches into `AIMS_CORE_DB` through `postgres_fdw`, using the server `fdw_aims_core_db`, an `aims` user mapping, and the foreign tables `enddevice`, `slabel`, and `station`, with a materialized view `enddevice_info` built on them. Portal therefore holds both `end_device`, its own table, and `enddevice`, a foreign table from Core; the two are one underscore apart and carry different data. The replacement reads both databases directly rather than depending on the vendor's internal FDW configuration.
- **VERIFIED:** Store `075` is effectively not fitted with labels. AIMS holds 15,010 `article` rows for it but only 2 `end_device` rows, against 10,832 for store `084`. This matches the existing evidence that the Hop pipeline constrains its SQL to `084`: store `075` is computed by SQL Server but never reaches AIMS. Confirmed intentional by the source owner, so a parity comparison must expect `075` to be absent on the AIMS side rather than treat it as a defect.
- **VERIFIED:** One AIMS row carries a malformed store code. `article_id = 101066052139` has `station_code = '"075'`, four characters with a leading double quote, which silently escapes an exact-match filter on `075`. It is a real production data defect and a useful fixture for the structural validation built in #12.
- **VERIFIED:** The displayed page is held in `AIMS_CORE_DB.enddevice.pages`, not in Portal. It is JSON inside a `varchar(100)`, shaped `{"currentPage":N,"returnPage":N,"exceptionPage":N}`. `Portal.end_device_templates.page` is a template slot and is `0` on every row, so it is not the displayed page. Reading a label's page therefore requires both databases: the store code exists only in Portal and the current page only in Core.
- **VERIFIED:** Current page values are `1` on 10,518 devices, `-1` on 4,629, `2` on 333, `4` on 28, and `3` on exactly one. The single page-3 device matches the documented behaviour of the Hop pipeline `esl-sku-promo-multi-page.hpl`, so multi-page is genuinely used but rare. `-1` correlates with `state = UNASSIGNED` (4,629 against 4,675) and is a sentinel for a device with no assignment, not a page number; treating it as one would misreport unassigned devices.
- **UNKNOWN / NEEDS-DISCOVERY:** The meaning of `returnPage` and `exceptionPage`, and which of the three the AIMS `POST /common/labels/page` endpoint actually sets. The #22 port models a single page, which is sufficient only if the endpoint drives `currentPage` alone.
- **VERIFIED:** Business rules are embedded inside the procedure's SQL predicates rather than expressed separately, for example campaign status, campaign type membership, validity dates, and the PFS exclusion. That entanglement is the stated reason the current pipeline is difficult to troubleshoot.
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
