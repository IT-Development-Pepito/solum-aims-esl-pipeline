# Specification — SOLUM ESL / AIMS Pipeline Replacement

## 1. Purpose

Replace the ESL processing responsibilities currently divided between SQL Server stored procedures/SQL Server Agent and Apache Hop/Jenkins with an operationally reliable service. SQL Server may remain a source or system of record, but it must not remain the principal runtime for business orchestration. The target must preserve verified business outcomes while making rules, state, operations, and recovery explicit.

This specification is implementation-neutral except where it records an approved architectural boundary. Evidence classifications used throughout are:

- **VERIFIED** — directly confirmed from supplied files.
- **INFERRED** — strongly indicated but not directly confirmed.
- **UNKNOWN / NEEDS-DISCOVERY** — not established by the supplied evidence.

## 2. Scope

### In scope

- Replacement of ESL-related stored-procedure business processing and SQL Server Agent execution.
- Replacement of SOLUM-relevant Hop pipelines/workflows and Jenkins orchestration.
- SQL Server ingestion, validation, transformation, domain-rule execution, state, scheduling, retries, reconciliation, and operations.
- AIMS integration, including a temporary read-only PostgreSQL compatibility path for the first cutover.
- Shadowing, parity comparison, controlled cutover, rollback, and legacy decommissioning.

### Out of scope

- Rewriting SOLUM AIMS, its gateway, its physical ESL firmware, or unrelated AIMS components.
- Altering AIMS database schemas or writing directly to AIMS databases.
- Unrelated retail/POS or enterprise data-platform replacement.
- Production configuration, production credentials, and production changes during this phase.

## 3. Current-state problem statement

### Observed facts

- **VERIFIED:** `dbo.RefreshESL_New` computes product, stock, price, promotion, and display-related data, then mutates `ESL.dbo.tb_ESL` in a transaction.
- **VERIFIED:** The newly supplied `ESL_Promotion_Business_Logic_and_Business_Rules_Reference.md` states the reference-directed target policy for date/time eligibility, category-`001` regular price, PFS exclusion, UOM handling, atomic promotion state, and observable ambiguity. It is not by itself evidence of deployed production parity; it identifies promotion priority, non-CLR conversion, and a final weekday-metadata policy as unresolved.
- **VERIFIED:** The latest supplied `RefreshESL_New` text contains Patch 2.5 promotion-pipeline changes, but is explicitly marked “REVIEW/TEST ONLY” and directly invokes `dbo.RefreshESL_New` inside its own procedure body. This is an apparent unbounded-recursion defect if executed; no successful execution evidence was supplied. Treat it as source text requiring SQL-owner review, not as proof of safe deployment or parity.
- **VERIFIED:** SQL Agent job `Refresh ESL Data` runs `EXEC dbo.RefreshESL_New` every 30 minutes from 07:00 through 23:59 daily. Its sole step has zero configured retries and quits reporting failure when the procedure fails.
- **VERIFIED:** The supplied Hop solution has a SKU diff/CSV branch and a promotion/page-management branch; the supplied runbook identifies Jenkins as their trigger.
- **VERIFIED:** Hop reads SQL Server and AIMS PostgreSQL, writes local CSV files, and posts page changes to an AIMS Dashboard endpoint.
- **VERIFIED:** The deployed AIMS Dashboard OpenAPI document (v3.1.0; Dashboard Service 4.9.0.RELEASE) documents `POST /common/labels/page` (`changeDisplayPage`) with required `store` query parameter, `pageChangeList` entries containing `labelCode` and integer `page`, and response fields `responseCode`, `responseMessage`, and `customBatchId`.
- **VERIFIED:** The inspected OpenAPI operation declares no API security scheme. This is not evidence that the endpoint is safe for unrestricted access; the target must compensate with network controls and use documented authentication if the vendor provides it.
- **VERIFIED:** The supplied workflow aborts on database-check or child-pipeline failure, but the supplied artifacts do not show an explicit retry/backoff policy or durable cross-run state.
- **VERIFIED:** Direct reads of AIMS `article`, `end_device_articles`, and `enddevice` structures occur in Hop.
- **VERIFIED:** The newest supplied `backup-pipeline/esl-sku-promo-multi-page.hpl` enables the Page 3 REST hop and uses `STORE_CODE_PARAM` consistently for its Page 3 endpoint. An older `backup pipeline/` snapshot differs and must be retained only as historical evidence.

### Architectural assumptions to validate

- **INFERRED:** Business logic is difficult to analyse because it is split across dynamic SQL, table state, Hop transforms, variables, files, and AIMS data.
- **INFERRED:** The supplied artifacts may omit production jobs, schedules, supporting scripts, runtime configuration, and manual recovery practices.
- **UNKNOWN / NEEDS-DISCOVERY:** Whether the current runtime meets the business reliability and recovery expectations, because run history and SLOs were not supplied.

## 4. Stakeholders and actors

| Actor | Interest / responsibility |
| --- | --- |
| Retail operations and merchandising | Correct, timely public shelf price/promotion display. |
| ESL administrators | Label/page operation, physical-device issue triage. |
| Application and data engineers | Service behavior, rule implementation, release quality. |
| Database administrators | SQL Server access, performance, backup, least privilege. |
| Infrastructure / SRE / operations | Hosting, monitoring, incident response, deployment, recovery. |
| SOLUM AIMS/vendor | Supported integration contracts and platform behavior. |
| Monitoring / alerting systems | Health, failure, latency, and escalation integration. |

## 5. System boundaries

| Boundary | Responsibility | Contract rule |
| --- | --- | --- |
| Internal target application | Scheduling, orchestration, rules, validation, durable state, audit, reconciliation, observability. | Owns no retail source-of-record data or AIMS data. |
| External SQL Server | Retail product, price, stock, promotion, and legacy ESL source data. | Read only by default; no direct production mutation by the target unless separately approved. |
| External SOLUM AIMS | Vendor-owned article/label/gateway domain. | Use supported API for writes. A first-cutover read-only PostgreSQL compatibility adapter is allowed, isolated, and temporary. |
| External monitoring / operations | Metrics, logs, alerts, secret platform, deployment platform. | Integrate through approved organization tooling. |

## 6. Functional requirements

### Ingestion and transformation

| ID | Requirement | Acceptance condition |
| --- | --- | --- |
| FR-001 | Connect to configured SQL Server sources with a least-privilege identity. | Connection/configuration test succeeds without exposing credentials. |
| FR-002 | Extract a configured, reproducible processing window per store/workflow. | Run audit records source watermark/window and query/configuration version. |
| FR-003 | Validate source schema, required fields, key uniqueness, data types, configured range/domain rules, structured promotion values, category-`001` regular-price availability, and UOM resolvability before side effects. | Invalid records receive deterministic anomaly/rejection reasons and do not reach AIMS actions. |
| FR-004 | Produce deterministic canonical transformations and promotion-decision outcomes from the same input, rule version, and configuration version. | Repeat test returns identical canonical output/hash, selected-state or unresolved outcome, and anomaly classification. |
| FR-005 | Separate domain rules from SQL access, AIMS access, scheduling, and interface code. | Rule tests run with fakes and name the rule ID. |
| FR-006 | Support record-level rejection/quarantine and explicitly unresolved decision outcomes where the workflow can safely continue. | Valid records complete; rejected or unresolved records are counted, traceable, and replayable only after correction or approved policy. |
| FR-026 | Support the current two-store operation and additional configured stores in the future without a code change or a separate workflow definition per store; promotion evaluation keys include store, item, and selling UOM. | Integration test processes two configured stores; capacity test demonstrates a third configured store under the documented per-store concurrency policy without cross-store/UOM candidate leakage. |

### Workflow, scheduling, and state

| ID | Requirement | Acceptance condition |
| --- | --- | --- |
| FR-007 | Model dependencies, ordering, conditions, and terminal states explicitly. | State transition test verifies success, skipped, retrying, failed, cancelled, and recovered behavior. |
| FR-008 | Provide configured recurring schedules, enable/disable control, and auditable manual execution. | A disabled schedule launches no run; authorized manual launch creates a run record. |
| FR-009 | Prevent accidental overlapping executions for the same workflow/store scope. | Concurrent launch test results in one owner and one explicit rejected/queued outcome. |
| FR-010 | Persist execution state, checkpoints, input scope, configuration/rule version, and per-record outcome across service/server restart. | Restart test resumes/reconciles without losing or duplicating completed work. |
| FR-011 | Allow a safe retry of a failed run and an explicit replay of a specified source window. | Both operations require an operator identity/reason and are audit-visible. |
| FR-012 | Provide workflow status by execution ID, workflow, store, and time range. | Operator can retrieve current state and terminal reason without parsing raw logs. |

### Idempotency, failures, and concurrency

| ID | Requirement | Acceptance condition |
| --- | --- | --- |
| FR-013 | Make the same logical workload safely retryable without duplicate/corrupt target effects. | Repeated submission/restart test produces one logical outcome per idempotency key. |
| FR-014 | Classify failures as retryable, non-retryable, or operator-action-required. | Classification matrix is implemented and tested per dependency. |
| FR-015 | Apply configured retry limits, bounded exponential backoff with jitter, timeout, and terminal failure handling. | Failure-injection test proves no retry beyond limit and records each attempt. |
| FR-016 | Recover safely from SQL unavailability, AIMS/API unavailability, network interruption, malformed data, partial completion, application restart, and server restart. | A failure/recovery test exists for each named event. |
| FR-017 | Define and enforce priority for scheduled versus manual operations on the same scope. | Concurrency test verifies documented policy; initial policy is no simultaneous ownership. The initial policy is symmetric: neither a scheduled nor a manual request displaces a live owner, and a contended request is rejected without creating an execution rather than queued. Any preference between the two remains UNKNOWN / NEEDS-DISCOVERY until a business owner approves one. |

### AIMS, reconciliation, audit, and operations

| ID | Requirement | Acceptance condition |
| --- | --- | --- |
| FR-018 | Encapsulate AIMS access behind adapters. | Domain/orchestration code has no direct database/API implementation dependency. |
| FR-019 | Use vendor-supported AIMS interfaces for mutations whenever technically available. | Integration contract test uses approved interface documentation or vendor confirmation. |
| FR-020 | Permit only read-only, least-privilege PostgreSQL compatibility queries in first cutover while alternatives are investigated. | Identity cannot modify AIMS data; every use is metrics/audit-visible. |
| FR-021 | Reconcile expected source records, eligible records, promotion anomalies, successes, rejections, submissions, acknowledgements, and unresolved outcomes. | Reconciliation report balances counts and enumerates exceptions, including unsupported UOM and promotion-ambiguity outcomes. |
| FR-022 | Record who/what/when/why/configuration/input/outcome/retry evidence for every execution and material record action, including promotion candidate/selection evidence. | Audit query answers the project-brief questions for a sampled run, including selected/eligible campaigns, price, UOM, calculation, and fallback/ambiguity evidence. |
| FR-023 | Provide safe manual operations: trigger, status, retry, replay, disable/enable scheduling, reconciliation, and fallback. | Role/authorization and procedure tests cover each operation. |
| FR-024 | Expose liveness, readiness, and dependency-health information without leaking secrets. | Health test distinguishes process alive, able to accept work, and dependency degraded. |
| FR-025 | Externalize configuration from business logic with versioning and startup validation. | Invalid/missing configuration prevents readiness and identifies the key, not its secret value. |
| FR-027 | Compare source and target representations in memory using canonical records and deterministic hashes/diff rules; do not use physical CSV files as the comparison, workflow-state, or audit mechanism. | A restart/retry test reproduces the same comparison from durable inputs/state without relying on a local CSV file. |
| FR-028 | Retain CSV generation only as a bounded compatibility delivery adapter when an identified external consumer requires it. | The automated contract in `docs/superpowers/specs/2026-08-25-csv-compatibility-delivery-contract-design.md` is implemented and accepted by the named consumer owner; CSV output is not completed without a matching acknowledgement, and retention/decommission gates are documented. |
| FR-029 | Provide the Python application as an internal web-based system with an authenticated operations UI/API, while supporting both Windows Service and command-line execution. | Authorized users can use the web interface for status/manual operations; the service lifecycle supports safe start/stop/pause/resume; CLI execution supports development, diagnostics, and controlled administration without bypassing authorization/audit. |
| FR-030 | Implement the browser UI as a React + TypeScript application using Tailwind CSS, with FastAPI as its exclusive data/action boundary. Google Stitch exports are approved design handoffs, not deployable application authority. | A selected Stitch screen has a recorded handoff reference and visual baseline; its React implementation obtains operational data only through authenticated FastAPI endpoints, has no direct SQL Server/AIMS access, and passes the frontend build, type, and test checks. |

## 7. Business-rule inventory

Critical rules must have a stable rule ID, requirement reference, implementation location, and independent test. The following inventory records current behavior, not necessarily approved business behavior.

| Rule ID | Existing location | Business meaning | Evidence | Target component |
| --- | --- | --- | --- | --- |
| BR-001 | `RefreshESL_New` | Stores `075` and `084` are iterated. | VERIFIED | Store-scope configuration. |
| BR-002 | `RefreshESL_New` | Only active items are selected and price/stock/product metadata is assembled. | VERIFIED | Product eligibility/mapping rules. |
| BR-003 | `RefreshESL_New` | Stock is aggregated from three current/offline movement sources. | VERIFIED | Stock calculation rule. |
| BR-004 | `RefreshESL_New`; stakeholder confirmation | For scalable `KGS` items, the source price is per kilogram and the ESL display price is per 100 grams (for example, `50,000/kg` displays as `5,000/100GR`). | VERIFIED | Weighted-item display rule. |
| BR-005 | Promotion reference; review-only `RefreshESL_New` | The reference-directed target policy uses operational campaign records and current date/time as the primary eligibility input; campaign status and warehouse metadata are supporting evidence, not the primary authority. Cross-midnight windows require an explicit boundary test. | VERIFIED within newly supplied reference; review-only procedure still filters status and does not prove cross-midnight parity, so legacy behavior remains NEEDS-DISCOVERY | Campaign eligibility rule. |
| BR-006 | Promotion reference; `RefreshESL_New` Patch 2.5 | Use active category `001` as the physical-store regular price. Missing or ambiguous values are data-quality anomalies; no alternate price-category fallback is allowed. | VERIFIED within newly supplied reference and review-only procedure | Regular-price validation rule. |
| BR-007 | Hop pipeline | Page 1 normal, Page 2 fixed price, Page 3 percent, Page 4 out of stock. | VERIFIED | Display-page decision policy. |
| BR-008 | Hop pipeline | Page action is skipped when current page equals destination. | VERIFIED | Idempotency/display action policy. |
| BR-009 | Hop pipeline | New/changed SKU comparison uses only selected fields; deleted records are identified but not exported. | VERIFIED | SKU-diff policy; deletion behavior NEEDS-DISCOVERY. |
| BR-010 | Latest `backup-pipeline/esl-sku-promo-multi-page.hpl` | Page 3 percent-promotion transport is enabled and uses `STORE_CODE_PARAM`, consistent with Page 2/Page 4. | VERIFIED | Page 3 transport and parity test. |
| BR-011 | Promotion reference | `DISC_TEXT` is manually maintained display/audit input. Preserve raw text and do not parse it as the primary logic source or reject a fixed pipe-field count. | VERIFIED within newly supplied reference | Campaign-display rule. |
| BR-012 | Promotion reference; `RefreshESL_New` Patch 2.5 | Exclude PFS/member-promotion candidates explicitly; do not introduce a generic `MEMBER` filter. | VERIFIED within newly supplied reference and review-only procedure | Member-promotion exclusion rule. |
| BR-013 | Promotion reference; `RefreshESL_New` Patch 2.5 | Resolve `CLR` to the item’s actual selling UOM. Do not invent non-CLR conversion; record `UOM_RULE_REQUIRED`/equivalent anomaly. | VERIFIED within newly supplied reference and review-only procedure | UOM-resolution rule. |
| BR-014 | Promotion reference; `RefreshESL_New` Patch 2.5 | Reject non-positive structured fixed/percent values; treat fixed promotion price greater than regular category-`001` price as no promotion. | VERIFIED within newly supplied reference and review-only procedure | Promotion-value validation rule. |
| BR-015 | Promotion reference | For scalable KGS items, evaluate on the selling UOM, then convert regular/effective promotion price to `/100GR` display values. | VERIFIED within newly supplied reference | Scalable-item rule. |
| BR-016 | Promotion reference; `RefreshESL_New` Patch 2.5 | Build the final promotion state atomically from one candidate; never mix text, value, type, group, or dates from different campaigns. | VERIFIED within newly supplied reference and review-only procedure | Promotion-state builder. |
| BR-017 | Promotion reference; `RefreshESL_New` Patch 2.5 | Missing weekday metadata retains a date/time-eligible candidate as a compatibility fallback, while explicit inactive metadata is distinct and observable. | VERIFIED within newly supplied reference and review-only procedure; final policy remains NEEDS-DISCOVERY | Weekday-metadata rule. |
| BR-018 | Promotion reference | Promotion evaluation is isolated by logical key `STORE_CODE + ITEM_CODE + SELLING_UOM`; no candidate may cross those boundaries. | VERIFIED within newly supplied reference | Store-scope/canonical-key rule. |
| BR-019 | Promotion reference; review-only `RefreshESL_New` | The reference-directed target requires `PROMO_PRIORITY_DIFFERENT_ECONOMIC` for different calculated outcomes and `DISPLAY_PRIORITY_SAME_ECONOMIC` for equal calculated outcomes with different terms/display. Effective-price/rounding comparison and legacy procedure parity need explicit tests. | VERIFIED within newly supplied reference; review-only procedure compares raw type/price/percent and does not establish the required calculated-outcome parity; winner policy remains NEEDS-DISCOVERY | Promotion-ambiguity rule. The implemented compatibility strategy (`compatibility-v1`) selects only where selecting is not a business decision: a single eligible candidate, an existing state that already equals an eligible candidate's outbound state, or several eligible candidates whose outbound states are identical apart from campaign identity. Every other multi-candidate case stays unresolved with its ambiguity code recorded, and the code is recorded even when a candidate is chosen. Effective prices are compared as exact unrounded decimals because no rounding policy is approved. No lowest-price, fixed-price, percent, newest-campaign, or clearance priority is implemented. |
| BR-020 | Latest supplied procedure text | The procedure is marked review/test only and directly invokes itself in its body. The source indicates apparent unbounded recursion if executed; deployment/execution safety is not established by the supplied evidence. | VERIFIED source-text observation | SQL-review gate #38; no target runtime dependency. |

Required evidence to resolve unknown rules: approved merchandising/POS promotion-priority and same-economic-term rule source, authoritative non-CLR UOM conversion data, final weekday-metadata policy, representative production cases, current business-owner confirmation, procedure execution review, and parity tests against the accepted baseline. No code may silently treat existing behavior as the desired policy.

## 8. Non-functional requirements

| ID | Requirement | Acceptance condition |
| --- | --- | --- |
| NFR-001 | Reliability must be equal to or better than the accepted legacy baseline. | Baseline and target failure/recovery comparison is approved before cutover. |
| NFR-002 | Recovery must resume or reconcile an interrupted run from durable state. | Tested recovery point and operator procedure meet agreed RTO/RPO; values are `NEEDS-MEASUREMENT` until baseline capture. |
| NFR-003 | Availability target must reflect ESL business windows, not an assumed 24x7 SLA. | Stakeholders approve an availability target and maintenance window before production acceptance. |
| NFR-004 | Performance must meet measured volume, duration, freshness, and peak-window targets. | Load/performance suite meets targets; each target is established from a measured baseline. |
| NFR-005 | Scale through controlled per-store concurrency before considering distributed architecture. | Capacity test documents safe worker/store concurrency and queue/lock behavior. |
| NFR-006 | Protect against duplicate effects, lost updates, partial write ambiguity, stale actions, and incorrect replay. | Idempotency, concurrency, and reconciliation tests pass. |
| NFR-007 | Persist queryable structured execution and event logs in the target database, with execution/correlation IDs and record-safe context. | An authorized operator can query a run, step, retry, record outcome, and error by execution ID, workflow, store, and time range; logs contain no secrets or unnecessary PII. |
| NFR-008 | Emit workflow duration, counts, rejection, retry, terminal-failure, dependency-health, and backlog metrics. | Dashboard/alert test verifies each metric and an actionable alert route. |
| NFR-009 | Apply least privilege, approved secret storage/rotation, TLS where supported, network restriction, and audit logging. | Security review signs off before production; secrets scan finds none in tracked files/log samples. |
| NFR-010 | Maintain clear modular separation: domain, application/orchestration, adapters, state, configuration, observability, runtime. | Architecture and dependency tests/review show prohibited cross-boundary access is absent. |
| NFR-011 | Make rules and transformations testable without live SOLUM wherever practical. | Unit suite uses fakes/fixtures; live dependencies are reserved for integration tests. |
| NFR-012 | Support separate development, staging, and production environments with repeatable, version-controlled deployment, promotion, and rollback. | Development, staging, and production use isolated configuration, credentials, and data/integration controls; deployment promotion and rollback rehearsal succeeds in staging before production release. |
| NFR-013 | Remain deployable on supported Windows/on-premise infrastructure unless discovery establishes another required platform. | Deployment design validates service account, paths, network, and operations requirements. |
| NFR-014 | Preserve required compatibility during migration without using undocumented AIMS database writes. | Shadow/dual-run tests achieve approved parity threshold. |
| NFR-015 | Run production workloads on the available high-spec Windows 10 host under explicit operational risk acceptance, with supported patching, dedicated service account, automatic service recovery, backup, and recovery controls. | Production-host readiness review records the Windows release/support status, patching, service account, disk capacity, backup, power/restart behavior, and recovery rehearsal. |
| NFR-016 | Deploy versioned immutable artifacts built and tested through GitHub Actions, with isolated dev/staging/production approval gates and a deployment method that does not require the production host to reach GitHub. | Build/test evidence and artifact hash are promoted from dev to staging to production; staging deploy/rollback succeeds; production installation uses the approved signed/hashed artifact-transfer and local administrator procedure. |

## 9. Operational reliability parity matrix

| Capability | Current SQL/Agent/Hop/Jenkins | Target requirement | Verification |
| --- | --- | --- | --- |
| Scheduling | SQL Agent runs every 30 minutes, 07:00–23:59 daily; Jenkins trigger role verified but its schedule remains unknown. | Durable schedules with enable/disable/audit. | Schedule and job-definition review; functional test. |
| Retry | SQL Agent step has zero retries; no Hop retry/backoff found. | Classified, bounded retry/backoff. | Failure injection. |
| Logging | Hop logs and CSV artifacts verified. | Structured, correlated, retained logs. | Trace a run end-to-end. |
| Workflow state | Hop in-process/result variables/files. | Durable run/record/checkpoint state. | Restart recovery test. |
| Manual rerun | Unknown. | Authorized trigger/retry/replay. | Operator acceptance test. |
| Failure recovery | Abort hops and SQL transaction verified. | Recover/reconcile partial effects. | Dependency/crash tests. |
| Monitoring | Recommendations present; deployed monitoring unknown. | Metrics, health, alert integration. | Alert drill. |
| Audit trail | CSV/current-page files and SQL prints verified. | Durable queryable audit. | Audit evidence review. |
| Deployment | Unknown. | Repeatable deploy/rollback. | Rehearsal. |
| Concurrency control | Unknown. | Per-workflow/store ownership. | Concurrent launch test. |
| Data reconciliation | Partial comparison/files verified. | Balanced source-to-outcome report. | Parity/reconciliation test. |

## 10. Migration and cutover requirements

1. Reverse engineer each legacy path and record verified inputs, outputs, rules, triggers, and failures.
2. Convert representative legacy behavior into approved executable baselines; resolve rule ambiguities before claiming parity.
3. Implement only after the architecture and plan are approved.
4. Shadow each target workflow using production-like input, with no AIMS mutations or physical label effects.
5. Compare canonical records, deterministic hashes, and business outcomes at record level; investigate every material difference. Do not use physical CSV files as the primary comparison evidence.
6. Dual-run only where safety review permits it; one system must be authoritative for each external action.
7. Cut over a controlled store/workflow scope with active monitoring and retained legacy fallback.
8. Observe against approved duration/error/reconciliation criteria.
9. Decommission legacy jobs only after formal acceptance and rollback window closure.

### Cutover gates and rollback

- Shadow parity threshold, sample size, duration, error budget, and performance baseline are **NEEDS-MEASUREMENT** and require owner approval. The recommended starting measurement plan is below; its proposed thresholds are not approved targets until the owners review measured results.
- Roll back if a target data-integrity violation, unreconciled external effect, failed recovery test, unapproved material parity difference, or service-level breach occurs.
- Rollback means disable target scheduling, preserve target audit/state, restore the previously approved legacy schedule/entry point, reconcile the cutover window, and open an incident record. Do not delete target evidence.
- Do not remove stored procedures, SQL Agent jobs, Hop files, or Jenkins jobs until all acceptance criteria pass and the rollback period closes.

### Recommended baseline and shadow-measurement plan

| Measure | How to capture it | Proposed initial gate, pending approval |
| --- | --- | --- |
| Workload | Record records/store, changed SKU count, page actions, and peak hourly/daily volume for each workflow. | Capture at least 14 consecutive calendar days, including a promotion period, for both current stores. |
| Duration / freshness | Record legacy trigger, start/end, source data timestamp, CSV delivery, and AIMS acknowledgement times. | Target p95 target duration no worse than legacy p95; agree an explicit freshness objective after baseline review. |
| Business parity | Compare canonical records, SKU outputs, page decisions, page payloads, rejections, and AIMS outcomes by store/run. | Zero unexplained differences in price, promotion, stock, SKU eligibility, or page decision; every expected discrepancy is approved and traceable. |
| Reliability / error budget | Record run success/failure, retries, dependency outages, and unresolved outcomes. | No target-caused data-integrity event or unresolved AIMS action during the shadow window; set numerical run-success/error-budget target from baseline. |
| Recovery | Inject/rehearse restart, SQL outage, API timeout, and duplicate-trigger scenarios in staging. | All required recovery, idempotency, and reconciliation tests pass before production cutover. |
| Capacity | Replay representative peak input in staging with planned store concurrency. | Meet approved p95 duration/freshness target while preserving correct per-store locks and reconciliation. |

The final cutover gate should be approved by retail/business owner, ESL/AIMS owner, operations/SRE, and technical owner after this evidence is reviewed.

## 11. Acceptance criteria / Definition of Done

- Every required legacy workflow has a documented target equivalent and traceability.
- Approved business-output parity is demonstrated in shadow execution.
- Failure, retry, timeout, restart, idempotency, concurrency, and reconciliation behavior are tested.
- Operations documentation, monitoring, alerting, audit, security review, deployment, and rollback rehearsal are complete.
- Performance and availability targets are measured and met.
- AIMS contract, compatibility-read retirement decision, and legacy disable plan are approved.
- Legacy execution can be disabled safely and is not deleted until the defined observation/rollback period completes.

## 12. Traceability model

Maintain the following mapping as implementation begins:

`Business rule → FR/NFR → architecture component → implementation unit → automated/integration/acceptance test → evidence`.

Each test and release record must cite the relevant requirement/rule ID. A requirements traceability matrix will be created with the implementation plan.

## 13. Testing strategy

The future implementation must provide unit, rule, transformation, database-adapter, AIMS-adapter, integration, failure-injection, idempotency, restart/recovery, reconciliation, performance, migration-parity, end-to-end acceptance, security, and deployment/rollback tests. Test doubles are required for business rules and AIMS behavior where practical; no automated test may target production without explicit approval.
