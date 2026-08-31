# System Architecture — SOLUM ESL / AIMS Pipeline Replacement

## 1. Architecture status and evidence

This document defines the approved target shape and records current-state evidence. It does not authorize application implementation. AIMS remains an external vendor boundary; its database layout is not a stable target contract.

## 2. Current architecture

```mermaid
flowchart LR
    Retail["Retail / POS data"] --> SP["SQL Server: RefreshESL_New"]
    SP --> ESL["SQL Server: ESL.dbo.tb_ESL"]
    Agent["SQL Server Agent\n30-minute schedule, 07:00–23:59\nzero retry"] --> SP
    Jenkins["Jenkins\nVERIFIED trigger role"] --> SKU["Hop SKU branch"]
    Jenkins --> Promo["Hop promotion branch"]
    ESL --> SKU
    ESL --> Promo
    AIMSDB["AIMS PostgreSQL\narticle / mappings / devices"] --> SKU
    AIMSDB --> Promo
    SKU --> CSV["SKU CSV files"]
    Promo --> API["AIMS Dashboard page-change HTTP API"]
    API --> Gateway["SOLUM gateway / ESL"]
```

### Verified dependency map

```text
Jenkins (job names/schedules UNKNOWN)
  ├─ esl-master-sku-updater.hpl
  │    └─ esl-sku-update-daily-new.hwf
  │         ├─ database connection check
  │         ├─ esl-compare-diff.hpl
  │         └─ esl-sku-update-to-csv.hpl
  └─ esl-master-promo-runner.hpl
       └─ esl-promo-sub-workflow-delay.hwf
            ├─ esl-sku-revert-to-normal-oos.hpl
            ├─ esl-sku-revert-to-normal-price.hpl
            └─ esl-sku-promo-multi-page.hpl
```

### Current-state inventory

| Artifact | Location | Type / purpose | Inputs / outputs | Trigger / failure handling | Class | Replacement area |
| --- | --- | --- | --- | --- | --- | --- |
| `RefreshESL_New` | `docs/sql-server/Store_Procedure_Refresh_ESL.sql` | Stored procedure builds ESL record state; latest supplied text contains Patch 2.5 promotion changes. | Retail tables → `tb_ESL`. | Procedure text has transaction/catch handling but is marked review/test only and contains an apparent direct self-invocation; safe production execution is not established. | VERIFIED source text / safe deployment NEEDS-DISCOVERY | Ingestion, rules, persistence orchestration. |
| Promotion business-rule reference | `docs/sql-server/ESL_Promotion_Business_Logic_and_Business_Rules_Reference.md` | Current compatibility baseline for promotion eligibility, pricing, UOM, selection, and audit. | Operational campaigns + supporting metadata → promotion state. | Distinguishes confirmed rules from unresolved policy. | VERIFIED | Domain-rule extraction and parity tests. |
| `Refresh ESL Data` | `docs/sql-server/job_refresh_esl.sql` | SQL Agent job executes the stored procedure. | `EXEC dbo.RefreshESL_New`. | Every 30 min, 07:00–23:59 daily; zero retries; step fails job. | VERIFIED | Scheduler/retry replacement. |
| `tb_ESL` DDL | `docs/sql-server/tb_ESL_DDL.sql` | Legacy ESL data store. | Product/price/stock/promo record. | Existing table/indexes. | VERIFIED | SQL read model/source compatibility. |
| Discount troubleshooting report | `docs/sql-server/...Discount_Mismatch...md` | Business-rule evidence and risks. | Case study. | N/A. | VERIFIED | Rule-discovery baseline. |
| `esl-master-sku-updater.hpl` | Hop backup | SKU master entry pipeline. | `tb_ESL` stores → workflow. | Jenkins asserted by runbook. | VERIFIED / INFERRED trigger | Scheduling/orchestration. |
| `esl-sku-update-daily-new.hwf` | Hop backup | SKU DB check, diff, export. | SQL/AIMS reads → CSV. | Abort on child/check failure. | VERIFIED | SKU workflow. |
| `esl-compare-diff.hpl` | Hop backup | Compares source and AIMS article fields. | `tb_ESL`, AIMS `article` → changed item list. | No retry found. | VERIFIED | Diff/reconciliation rule. |
| `esl-sku-update-to-csv.hpl` | Hop backup | Exports new/changed SKU CSV. | Diff list + SQL → file. | Legacy consumer identity unknown; target automated acknowledgement contract approved. | VERIFIED / NEEDS-CONSUMER-ACCEPTANCE | SKU delivery adapter. |
| `esl-master-promo-runner.hpl` | Hop backup | Promotion entry; supplied SQL limits store `084`. | `tb_ESL` → workflow. | Jenkins asserted by runbook. | VERIFIED / INFERRED trigger | Scheduling/orchestration. |
| `esl-promo-sub-workflow-delay.hwf` | Hop backup | Ordered page workflow. | Store parameter → three pipelines. | Abort hops after final two steps. | VERIFIED | Page workflow. |
| OOS/normal reversion pipelines | Hop backup | Send recovered labels to Page 1. | SQL + AIMS mappings/pages → API, log CSV. | REST active. | VERIFIED | Display decision + AIMS write adapter. |
| promo multi-page pipeline | Latest `backup-pipeline` Hop artifact | Page 2 fixed, Page 3 percent, Page 4 OOS. | SQL + AIMS mappings/pages → API, files. | Page 3 REST is enabled and uses `STORE_CODE_PARAM`. Older `backup pipeline` snapshot differs. | VERIFIED | Display decision + AIMS write adapter. |
| RDBMS metadata/config | Hop backup metadata | Connection settings/reference. | SQL Server, AIMS Portal/Core. | Secrets intentionally omitted. | VERIFIED | Configuration/secrets migration. |
| AIMS DDL/config | Hop/Jenkins evidence | Schema/config reference. | Vendor context. | Contract support unknown. | VERIFIED | Compatibility discovery only. |
| AIMS Dashboard diagnostic + OpenAPI | `docs/hop-jenkins-pipeline/AIMS-Dashboard-Diagnostic-*`; deployed `/doc/json/common` | Read-only API discovery and documented page-change contract. | Article GET and Page-change API schema. | OpenAPI exposes `POST /common/labels/page`; vendor support lifecycle/auth policy still needs confirmation. | VERIFIED / NEEDS-DISCOVERY | AIMS API adapter. |

### Known gaps

Jenkins job configuration/schedules/command lines, runtime identity, production history, legacy CSV consumer identity/acceptance, vendor support lifecycle/authentication policy, exact gateway topology, and workload baseline are **UNKNOWN / NEEDS-DISCOVERY**. Promotion-priority policy, same-economic campaign-term selection, authoritative non-CLR UOM conversion, and the final missing-weekday-metadata policy are also **UNKNOWN / NEEDS-DISCOVERY**. The target automated CSV acknowledgement contract is approved; its environment-specific timeout and retention remain disabled pending consumer acceptance.

## 3. Business and data flow

The desired business flow, subject to rule confirmation, is:

```text
Retail price/SKU/stock/promotion change
  → determine eligible ESL change
  → validate and apply approved business rules
  → determine required AIMS action
  → submit through supported AIMS interface
  → record acknowledgement/outcome
  → reconcile and alert exceptions
```

The target technical data flow is:

```text
source snapshot/window → extraction → validation → canonical transformation
→ domain decision → idempotent AIMS/file action → acknowledgement
→ durable audit/reconciliation → metrics/logs/alerts
```

### Promotion decision boundary

The reference-directed target domain layer evaluates the latest operational campaign source on every run. It uses date/time (including cross-midnight windows) as the primary eligibility rule, category `001` as regular price, an explicit PFS exclusion, actual selling-UOM resolution, and scalable-item display transformation only after economic calculation. `FactCampaign` weekday data and campaign status are supporting metadata rather than the primary authority. Missing weekday metadata is recorded and follows the reference-directed fallback; explicit inactive weekday metadata is distinct. The review-only procedure still filters status and does not establish date-boundary parity, so representative cases are required before claiming legacy compatibility.

The result is exactly one atomic `PromotionState` for a `STORE_CODE + ITEM_CODE + SELLING_UOM` key, or a deterministic rejection/unresolved outcome. Raw `DISC_TEXT` is retained as display/audit information and is not used to infer structured logic. The reference-directed conservative selection strategy must not invent a campaign winner: different calculated economic outcomes and same-economic/different-term outcomes are observable anomaly classes until a business policy is approved. The supplied review-only procedure compares raw type/price/percent rather than calculated effective outcome, so it is not a parity authority for this classification.

## 4. Target architecture

```mermaid
flowchart TB
    Operator["Operator / Scheduler"] --> Ui["React + TypeScript operations UI\nStitch-informed, Tailwind CSS"]
    Ui --> Ops["FastAPI internal operations API + CLI\nmanual trigger, retry, replay, status"]
    Ops --> Orchestrator["Workflow orchestrator\ndurable state and scope locks"]
    Scheduler["Internal scheduler"] --> Orchestrator
    Orchestrator --> App["Application services\nvalidation, reconciliation, policies"]
    App --> Domain["Domain rules\nproduct, pricing, promotion, display-page"]
    App --> SQL["SQL Server adapter\nleast-privilege reads"]
    App --> AimsApi["AIMS supported API adapter\npreferred write path"]
    App --> Compat["AIMS PostgreSQL compatibility adapter\nread-only / temporary"]
    App --> File["CSV compatibility delivery adapter\nonly if external consumer requires it"]
    Orchestrator --> State["Execution state + audit store"]
    App --> Observe["Structured logs, metrics, tracing, health"]
    AimsApi --> AIMS["SOLUM AIMS boundary"]
    Compat --> AIMS
    Observe --> Monitoring["Monitoring / alerting"]
```

### Component responsibilities

| Component | Responsibility | Must not do |
| --- | --- | --- |
| Scheduler / operations interface | Timed/manual invocation, authorization, status, disable/enable. | Implement business rules or bypass locks. |
| Workflow orchestrator | State machine, dependency order, locks, checkpoints, retry scheduling. | Embed SQL/AIMS transport details. |
| Domain rules | Deterministic eligibility, category-`001` price validation, PFS exclusion, UOM resolution, promotion-state construction, ambiguity classification, mapping, page decision, and canonical idempotency input. | Query databases, issue HTTP, parse manual display text as a primary rule source, or invent campaign priority/conversion policy. |
| SQL Server adapter | Snapshot/query source data and map to domain records. | Own workflow state or rules. |
| AIMS supported-API adapter | Mutate/confirm AIMS through documented vendor interface. | Reach into AIMS database tables. |
| Compatibility adapter | Bounded read-only queries needed for cutover parity. | Write, become a general AIMS repository, or leak schema to domain. |
| CSV compatibility delivery adapter | Produce/acknowledge files only if an identified legacy consumer remains required. | Act as workflow state, comparison evidence, audit system, or treat directory presence as success. |
| Execution state/audit store | Durable run state, locks, attempts, complete immutable canonical snapshots, hashes/diffs, action ledger, configurations, and evidence. | Become a second retail/AIMS source of record or rely on local files for recovery. |
| Reconciliation service | Compare expected, processed, rejected, promotion-anomaly, submitted, acknowledged, and unresolved records; retain candidate/selection evidence. | Retry external actions without orchestration policy or suppress unresolved ambiguity. |
| Observability/configuration | Correlated telemetry, health, validated external configuration/secrets references. | Store plaintext secrets in logs or repository. |

## 5. Authoritative target data model

### 5.1 Authority, status, and ownership

This section is the authoritative reference for PostgreSQL persistence models and application-level domain, API, and frontend types. It is a logical and physical-contract model, not authorization to create a migration or production schema. Each implementation issue must cite the applicable FR/NFR/BR identifiers and preserve the boundaries below.

Model status has the following meaning:

- **IMPLEMENTED BASELINE** — present in the immutable 0001_operational_state migration and current SQLAlchemy models; later migrations may extend it.
- **APPROVED TARGET** — approved architecture that must be implemented through a separately reviewed issue and additive Alembic migration.
- **UNKNOWN / NEEDS-DISCOVERY** — a value or policy that cannot be encoded until evidence and owner approval exist.

The target PostgreSQL database owns workflow configuration metadata, execution state, immutable processing evidence, action history, audit, and reconciliation. SQL Server remains authoritative for retail product/price/stock/promotion source data, and SOLUM AIMS remains authoritative for vendor-owned article, label, device, and gateway state. Canonical snapshots are retained evidence for deterministic replay and audit; they do not create a second retail or AIMS master. Credentials and secret values are not part of this model.

The model is divided into five bounded areas:

1. Configuration: stores, schedules, and immutable non-secret configuration versions.
2. Workflow operation: executions, leases, steps, checkpoints, events, and operator audit.
3. Canonical business evidence: snapshots, promotions, processing outcomes, issues, and differences.
4. External effects: intended actions, attempts, acknowledgements, and optional CSV compatibility delivery.
5. Reconciliation: balanced execution summaries and enumerated exceptions.

### 5.2 Relationship model

~~~mermaid
erDiagram
    STORE_CONFIGURATION ||--o{ WORKFLOW_SCHEDULE : configures
    CONFIGURATION_VERSION ||--o{ WORKFLOW_EXECUTION : governs
    WORKFLOW_SCHEDULE o|--o{ WORKFLOW_EXECUTION : launches
    WORKFLOW_EXECUTION ||--o| SCOPE_LEASE : owns
    WORKFLOW_EXECUTION ||--o{ EXECUTION_STEP : contains
    EXECUTION_STEP ||--o{ EXECUTION_CHECKPOINT : checkpoints
    WORKFLOW_EXECUTION ||--o{ EXECUTION_EVENT : emits
    WORKFLOW_EXECUTION o|--o{ AUDIT_ENTRY : concerns
    WORKFLOW_EXECUTION ||--o{ SNAPSHOT_SET : captures
    SNAPSHOT_SET ||--o{ CANONICAL_RECORD_SNAPSHOT : contains
    CANONICAL_RECORD_SNAPSHOT ||--o| PROMOTION_EVALUATION : evaluates
    PROMOTION_EVALUATION ||--o{ PROMOTION_CANDIDATE_SNAPSHOT : considers
    CANONICAL_RECORD_SNAPSHOT ||--o| RECORD_PROCESSING_RESULT : produces
    RECORD_PROCESSING_RESULT ||--o{ RECORD_ISSUE : explains
    WORKFLOW_EXECUTION ||--o{ RECORD_DIFFERENCE : detects
    CANONICAL_RECORD_SNAPSHOT o|--o{ RECORD_DIFFERENCE : compares
    RECORD_PROCESSING_RESULT ||--o{ RECORD_ACTION : requests
    RECORD_ACTION ||--o{ ACTION_ATTEMPT : attempts
    WORKFLOW_EXECUTION ||--o{ COMPATIBILITY_DELIVERY : delivers
    WORKFLOW_EXECUTION ||--o{ RECONCILIATION_REPORT : reconciles
    RECONCILIATION_REPORT ||--o{ RECONCILIATION_EXCEPTION : enumerates
~~~

The canonical business key is store_code + item_code + selling_uom (BR-018). It prevents promotion candidates and decisions crossing store, item, or UOM boundaries. The legacy tb_ESL identity store_code + item_code is current-state evidence, not the target identity. A label code is also not part of product identity: one product can map to multiple labels, and each label-specific external effect is a separate action.

### 5.3 Type and storage conventions

| Concept | PostgreSQL representation | Rule |
| --- | --- | --- |
| Aggregate identifier | UUID | Generated by the application; stable across APIs and audit. |
| Source/business identifier | Bounded VARCHAR | Preserve leading zeroes and source formatting; never coerce store/item/barcode to integers. |
| Timestamp | TIMESTAMPTZ | Persist UTC; convert only for store/operator display. |
| Business date/time | DATE / TIME after validation | Retain rejected raw values only in sanitized source evidence. |
| Money | NUMERIC(19,4) plus ISO currency code | Never use binary floating point; initial currency is IDR. |
| Quantity/weight | NUMERIC(19,4) | Supports fractional stock and future measured items. |
| Percentage | Fixed-precision NUMERIC | Calculation and rounding version must be traceable. |
| Controlled state/reason | Bounded VARCHAR plus CHECK and application enum | Avoid PostgreSQL native enums so additive policy evolution remains manageable. |
| Structured evolving evidence | JSONB | Requires a typed schema name/version, allowlisted fields, and validation before persistence. |
| Canonical hash | 64-character SHA-256 hex | Calculated from canonical UTF-8 JSON with deterministic key ordering and number formatting. |

All versioned payloads must define unknown-field handling and backwards-compatibility tests. JSONB is not an untyped dumping ground and cannot contain credentials, tokens, connection strings, authorization headers, DPAPI blobs, or unrestricted request/response bodies.

### 5.4 Configuration and workflow-operation entities

| Entity | Status | Key and required content | Invariants / relationships |
| --- | --- | --- | --- |
| store_configuration | APPROVED TARGET | id; unique store_code; display name; timezone; enabled state; non-secret operational options; created/updated metadata. | Adding a store is configuration, not a code or workflow-definition change (FR-026). It owns no retail master data. |
| configuration_version | APPROVED TARGET | id; environment; configuration schema version; content hash; sanitized JSONB snapshot; activation time/actor. | Immutable and secret-free. Every execution references exactly one configuration version (FR-002, FR-010, FR-025). |
| workflow_schedule | IMPLEMENTED BASELINE, target extension approved | id; workflow; store; cron expression; timezone; enabled; created/updated metadata; configuration version. | Unique active schedule identity per configured workflow/store. Enable/disable changes are audited (FR-008). A cadence is a five-field cron expression evaluated to the minute in the schedule's own timezone; unsupported syntax is rejected when the schedule is defined rather than silently never firing. |
| workflow_execution | IMPLEMENTED BASELINE, target extension approved | id; workflow; store; trigger type; SHADOW/ACTIVE mode; status; correlation ID; source window/watermark; configuration/rule versions; operator/reason; retry/replay parent; start/end; terminal reason. | One attempt for one workflow/store scope. Retry/replay creates a linked execution and never overwrites history (FR-010–FR-012). |
| scope_lease | IMPLEMENTED BASELINE, target extension approved | scope_key; execution FK; acquired, heartbeat, expiry, release times; lease version. | At most one current owner per workflow/store scope. Expired ownership is recovered only after checkpoint/action reconciliation (FR-009, FR-017). |
| execution_step | APPROVED TARGET | id; execution FK; stable step name; dependency outcome; attempt; state; failure class; start/end. | Makes ordering and terminal behavior explicit (FR-007, FR-014–FR-016). |
| execution_checkpoint | APPROVED TARGET | id; step FK; unique checkpoint key/version; watermark/position; payload schema version/hash; sanitized JSONB state; time. | Append-only checkpoints make restart/replay possible without a local CSV file (FR-010, FR-027). |
| execution_event | IMPLEMENTED BASELINE, target extension approved | id; execution FK; time; severity; event type; step; correlation ID; optional record key; schema-versioned sanitized payload. | Append-only queryable structured log for NFR-007; it is not a replacement for status/summary columns. |
| audit_entry | APPROVED TARGET | id; optional execution FK; actor; action; reason; resource type/key; configuration version; correlation ID; outcome; sanitized before/after evidence; time. | Append-only; supports schedule/config/manual actions that may not create an execution (FR-008, FR-011, FR-022, FR-023). |

Execution states are controlled as follows:

~~~text
QUEUED
  -> RUNNING
       -> RETRY_WAIT -> RUNNING
       -> RECOVERING -> RUNNING
       -> SUCCEEDED
       -> SUCCEEDED_WITH_EXCEPTIONS
       -> FAILED
       -> CANCELLED
       -> SKIPPED
~~~

Every state transition must be validated and append a structured event; manual transitions additionally append an audit entry. Recovery provenance is retained even when the final state is successful.

### 5.5 Canonical snapshot contract

| Entity | Status | Key and required content | Invariants / relationships |
| --- | --- | --- | --- |
| snapshot_set | APPROVED TARGET | id; execution FK; representation kind (SOURCE_EXPECTED, LEGACY_BASELINE, or AIMS_OBSERVED); adapter; source window/watermark; canonical schema version; capture time; record count; aggregate hash. | One immutable capture of one representation; identifies how and when it was obtained. |
| canonical_record_snapshot | APPROVED TARGET | id; snapshot-set FK; store, item, selling UOM; canonical schema version/hash; validated full JSONB payload; captured time. | Unique by snapshot set and canonical business key. Immutable complete snapshot retained under the approved policy. |
| promotion_evaluation | APPROVED TARGET | id; snapshot FK; rule/calculation versions; outcome; nullable selected-candidate FK; atomic resulting state; evaluation time. | Exactly one per relevant snapshot. Selection is allowed only when approved rules produce one deterministic candidate. |
| promotion_candidate_snapshot | APPROVED TARGET | id; evaluation FK; source campaign identity; structured type/value; raw DISC_TEXT; validity/weekday evidence; category-001 price; source/resolved UOM; calculated outcome; eligibility/fallback/reason evidence. | Immutable candidate evidence. It cannot cross the canonical key or silently apply an unresolved priority/conversion policy. |
| record_processing_result | APPROVED TARGET | id; execution and canonical-snapshot FKs; validation/eligibility/promotion outcomes; current/desired page; action decision; terminal category; time. | One per execution/business key. Multiple label actions may be related to one result. |
| record_issue | APPROVED TARGET | id; result FK; rule/requirement ID; issue code; severity; classification; sanitized evidence; resolution metadata. | One row per rejection, anomaly, unsupported UOM, or unresolved decision so multiple issues are queryable independently. |
| record_difference | APPROVED TARGET | id; execution; left/right snapshot FKs and hashes; difference type; changed paths; typed old/new JSONB values; diff schema/rule version. | Deterministic comparison evidence; physical CSV is never an input to the comparison (FR-004, FR-027). |

The versioned canonical payload contains these typed sections:

| Section | Canonical content | Relevant legacy evidence / rule |
| --- | --- | --- |
| identity | Store, item, selling UOM, barcode. | STORE_CODE, ITEM_CODE, UOM, BARCODE; BR-018. |
| product | Item names, product/NFC references, brand and classification, consignment/returnable/red-list flags. | ITEM_NAME, ITEM_SHORTNAME, PRODUCT_URL, NFC_URL, DIVISION, DEPARTMENT, CLASS, SUBCLASS, BRAND, CLASS_ROTATION, CONSIGMENT, RETURNABLE, REDLIST. Canonical spelling corrects legacy CONSIGMENT without altering evidence. |
| pricing | Currency; source regular/member price evidence; source price basis; display regular price; display basis; calculation/rounding version. | SALES_PRICE, MEMBER_PRICE, PER_GRM_SELL_PRICE; BR-004, BR-006, BR-012, BR-015. Member price is preserved evidence and is not an inferred winner rule. |
| inventory | Stock on hand, product weight, minimum/maximum/display quantities. | SOH, PROD_WEIGHT, MIN_QTY, MAX_QTY, DISPLAY_QTY; BR-003. |
| expiry | Validated early-expiry date and expiry days. | EARLY_EXPIRY_DATE, EXPIRY_DAYS. |
| promotion_state | Exactly one complete selected state or no promotion: candidate/campaign, type, group, structured value, effective/display price, percentage, saving, raw text, and validity window. | DISC_PRICE, DISC_PERCENT, DISC_TEXT, PROMO_FLAG, PER_GRM_PROMO_PRICE, PROMOTION_TYPE, CAMPAIGN_GROUP, SAVE_AMT, PROMO_START_DATE, PROMO_END_DATE, PROMO_START_TIME, PROMO_END_TIME; BR-005–BR-006 and BR-011–BR-019. |
| display_decision | Current page when observed, desired page, and reason. | BR-007–BR-010. Label-specific submission remains in the action ledger. |
| provenance | Adapter, source watermark/references, original update time, configuration/rule/schema versions. | LAST_UPDATED_DATE, CREATED_DATE; SYNC_REC is retained only as legacy source evidence, not target workflow state. |

For scalable KGS items, source economic value and display value are different fields. For example, 50,000 / KG is retained alongside 5,000 / 100GR; conversion occurs only after selling-UOM economic evaluation (BR-004, BR-015). Source floating-point values are normalized to decimal canonical values while sanitized raw evidence remains available for parity diagnosis.

Promotion evaluation outcomes are NO_PROMOTION, SELECTED, AMBIGUOUS, REJECTED, or UNRESOLVED. At minimum the model preserves PROMO_PRIORITY_DIFFERENT_ECONOMIC, DISPLAY_PRIORITY_SAME_ECONOMIC, and UOM_RULE_REQUIRED. Formal winner priority, calculated effective-price/rounding comparison, authoritative non-CLR conversion, and final weekday policy remain UNKNOWN / NEEDS-DISCOVERY and cannot be represented by silent defaults.

### 5.6 External action and delivery model

| Entity | Status | Key and required content | Invariants / relationships |
| --- | --- | --- | --- |
| record_action | IMPLEMENTED BASELINE, target extension approved | id; execution/result FKs; canonical business key; optional label; action type; desired state/page; unique idempotency key; request hash; state; acknowledgement/batch identifiers; times. | Durable logical action ledger. Shadow runs may reach only INTENDED or SKIPPED_IDEMPOTENT. |
| action_attempt | APPROVED TARGET | id; action FK; unique attempt number; start/end; retry class; HTTP/result code; sanitized response evidence; delivery certainty. | Append-only. An unknown submission is reconciled before any resend (FR-013–FR-016). |
| compatibility_delivery | APPROVED TARGET, disabled pending consumer acceptance | id; execution FK; logical delivery ID; manifest/content hashes; publication and acknowledgement states/times; consumer reference; retention deadline. | Records the contract and acknowledgement, not CSV content as authoritative state. File presence alone never means completion (FR-028). |

Action states are:

~~~text
INTENDED
  -> SKIPPED_IDEMPOTENT
  -> SUBMITTING
       -> ACKNOWLEDGED
       -> REJECTED
       -> FAILED_RETRYABLE -> SUBMITTING
       -> FAILED_TERMINAL
       -> OUTCOME_UNKNOWN
~~~

OUTCOME_UNKNOWN is operator-action-required and blocks blind resubmission. The idempotency key is derived from the approved adapter contract, logical business/action key, desired state, rule/configuration versions, and reproducible source window; secret or volatile transport values are excluded.

### 5.7 Reconciliation model

| Entity | Status | Key and required content | Invariants / relationships |
| --- | --- | --- | --- |
| reconciliation_report | APPROVED TARGET | id; execution FK; unique revision; mode; generated time; counts for extracted, valid, rejected, ineligible, eligible, unchanged, ambiguous, intended, skipped-idempotent, submitted, acknowledged, rejected-by-AIMS, failed, and unresolved; status. | Immutable after finalization. New reconciliation creates another revision rather than overwriting evidence. |
| reconciliation_exception | APPROVED TARGET | id; report FK; category; canonical record/action reference; expected/actual evidence; resolution status/actor/reason/time. | Every imbalance and unresolved external effect is enumerated, not hidden in aggregate counts. |

Reconciliation is stage-based so record transformation and potentially multiple label actions are not collapsed incorrectly:

~~~text
extracted = rejected + valid
valid = ineligible + eligible

ACTIVE terminal action balance:
eligible = unchanged + skipped_idempotent + acknowledged
         + rejected_by_aims + failed + unresolved

SHADOW terminal action balance:
eligible = unchanged + skipped_idempotent + intended + unresolved
~~~

Ambiguous is a diagnostic subset of unresolved rather than an additional balancing category. Submitted is an in-flight observation and cannot appear in a terminal balanced report; an execution with a lingering submission becomes OUTCOME_UNKNOWN/unresolved. Promotion ambiguity and unsupported UOM are counted as unresolved and retain issue/candidate evidence. Any formula change is a business/operational change that must update the specification, architecture, workflow, and tests together.

### 5.8 Immutability, retention, and deletion

Configuration versions, snapshot sets, canonical snapshots, promotion evaluations/candidates, differences, issues, events, audit entries, action attempts, and finalized reconciliation revisions are append-only. State-bearing executions, steps, leases, schedules, actions, and deliveries change only through validated transitions; each material change also appends an event or audit entry.

Durable evidence foreign keys must use RESTRICT, not cascading deletion. The current Task 3 cascade constraints are an IMPLEMENTED BASELINE limitation and must be replaced by an additive migration before retention/purge is enabled. The original migration remains immutable. Scope leases may be explicitly released after their recovery evidence is recorded.

Exact retention durations are UNKNOWN / NEEDS-DISCOVERY until workload volume, audit needs, incident windows, and rollback gates are measured and approved. Configuration supplies per-environment values for these classes without a code change:

- **Audit core:** executions, version hashes, audit, actions/acknowledgements, and reconciliation summaries.
- **Detailed processing evidence:** canonical snapshots, promotion candidates, differences, issues, checkpoints, and detailed events.
- **Compatibility evidence:** manifests, hashes, publication/acknowledgement metadata, and sanitized attempts under the approved consumer contract.
- **Physical compatibility files:** separate consumer-contract retention; never a database-state substitute.

Purging requires a terminal execution, finalized reconciliation, no unresolved action, expiry of rollback/audit gates, authorized operation, and an audit entry. Development/staging may have shorter approved durations than production.

**IMPLEMENTED LIMITATION (issue #64).** The implemented purge covers record differences, promotion evaluations and candidates, record issues, execution checkpoints and steps, and detailed events. It does **not** yet remove canonical snapshots, snapshot sets, or record processing results, because `record_action.record_processing_result_id` and `record_processing_result.canonical_record_snapshot_id` are `NOT NULL` with `RESTRICT`: retaining the audit core therefore pins the detailed rows beneath it. Canonical snapshots are the largest class by volume, so most of the storage benefit of retention is unavailable until issue #64 relaxes those two links through an additive migration. Purge remains disabled by default regardless, because no retention duration is approved.

### 5.9 Constraints, query paths, and scale

Required uniqueness includes one canonical record per snapshot set/business key, one promotion evaluation per snapshot/rule version, one source campaign identity per evaluation, one processing result per execution/business key, one logical action per idempotency key, one current lease owner per scope, and one reconciliation revision number per execution.

Indexes must support:

- executions by workflow/store/status/start time;
- events by execution/time, correlation/time, severity/time, and record key;
- snapshots/results/issues by execution and canonical key;
- promotion evidence by store/item/UOM, campaign, outcome, and execution;
- actions by idempotency key, execution, state, label, and acknowledgement batch;
- audit by actor/action/resource/time; and
- reconciliation exceptions by execution/category/resolution state.

Add JSONB indexes only for measured query paths. Time partitioning is not selected until retained volumes and query plans justify it. Initial scaling remains bounded worker concurrency with per-store leases (NFR-005).

### 5.10 Application-model mapping and change control

| Layer | Authority and responsibility |
| --- | --- |
| Domain model | Immutable business concepts/rules with no SQLAlchemy, FastAPI, or transport dependency. |
| Persistence model | PostgreSQL columns, constraints, indexes, relationships, and schema-versioned payload storage. |
| API model | Authorized Pydantic request/response contract; internal payloads are not exposed automatically. |
| Frontend type | Generated from or checked against the API contract; never database authority. |

Business and model changes follow a documentation-first contract:

1. Identify affected requirement and business-rule IDs.
2. Update SPECIFICATION.md first when behavior or acceptance changes.
3. Update this section's entities, invariants, ownership, lifecycle, and compatibility impact.
4. Obtain documentation review before application/schema implementation.
5. Add a new Alembic migration; never rewrite an applied migration.
6. Update SQLAlchemy, domain/Pydantic, API, and exposed TypeScript models as applicable.
7. Add requirement/rule-traceable tests and migration evidence.
8. Record the issue checkpoint, schema state, evidence, and risks in PROGRESS.md.

A pull request cannot introduce an application model or database semantic change absent from this architecture. Emergency fixes must reconcile documentation in the same pull request before merge. This governance summary remains here; the repeatable developer procedure belongs in WORKFLOW.md.

### 5.11 Verification contract

The data model requires empty-database and prior-schema Alembic tests; SQLAlchemy constraint integration tests; canonical schema/hash tests; BR-004/BR-015 price-basis tests; BR-016 atomic-state tests; BR-018 scope-isolation tests; BR-019 ambiguity tests; restart/replay tests from retained snapshots; lease/idempotency/retry/unknown-outcome tests; reconciliation-balance tests; retention/audit safety tests; NFR-007 operator-query tests; and API/frontend contract-drift checks. No automated test may mutate production dependencies.

## 6. Deployment architecture

Initial deployment is one modular **Python + PostgreSQL web application** (or active/passive only if availability discovery requires it) on supported Windows/on-premise infrastructure. Its internal browser UI is a React + TypeScript application built with Vite and Tailwind CSS; the Python backend is FastAPI. The UI uses authenticated FastAPI endpoints only and never connects directly to SQL Server, PostgreSQL, or AIMS. Google Stitch is the design/handoff source: approved exports and screenshots guide the React implementation, but are not deployed without code review, type checks, automated tests, and API-contract integration. The same application supports command-line execution for development, diagnostics, and controlled administration. For continuous production operation it can run under Windows Service Control Manager using a dedicated Windows Service account, so authorized administrators can start, stop, pause, and resume it. Pause must quiesce scheduling and allow in-flight work to checkpoint or reach a documented safe state rather than abruptly killing it. PostgreSQL is the durable execution-state, audit, reconciliation, and queryable structured event-log store. The service replaces Jenkins and Hop as runtime scheduler/orchestrator.

The service must have isolated **development**, **staging**, and **production** environments. Each environment has separately managed configuration, credentials, database/schema or equivalent isolation, AIMS integration controls, and monitoring labels. Production promotion requires a versioned staging deployment, automated/integration evidence, an approved migration/release record, and a rollback-tested artifact. Staging must not drive physical production ESL effects.

### Hosting, secrets, deployment, and firewall baseline

- **Host:** use the available high-spec Windows 10 PC for production under explicit operational risk acceptance. Keep Windows Server or an organization-approved server platform as a future improvement, not a cutover blocker. Require current security patches, automatic restart/recovery configuration, sufficient disk/CPU/RAM capacity, backup, and documented recovery testing.
- **Service identity:** one non-administrator Windows service account per environment; deny interactive logon unless operations specifically requires it.
- **Secrets:** initially use a machine-scoped Windows DPAPI-protected secret bundle stored outside the repository under `%ProgramData%`, protected by NTFS ACLs to the service identity and designated administrators. Provision and rotate it through an approved administrator runbook. Do not place secret values in GitHub Actions, repository files, or logs. Replace this implementation with an organization-managed secret platform when one becomes available.
- **Build/release:** GitHub Actions builds, tests, scans, and produces a versioned immutable artifact. GitHub Environments provide dev/staging/production approval gates. Until the production host receives approved GitHub network access, it receives the signed/hashed artifact through the organization's controlled transfer method and a local administrator runs the deployment procedure. If GitHub access is enabled later, require a security review and allowlist only required HTTPS destinations; the host must never retrieve production secrets from GitHub.
- **Network:** deny public inbound access. Allow only approved administrator/deployment access from the management network. The service needs private, allowlisted routes to configured SQL Server, AIMS API, and its PostgreSQL state store; it does not require Internet access. Restrict the currently HTTP AIMS API to an allowlisted internal route; require TLS/reverse-proxy or vendor-supported secure transport if available before production acceptance. Browser-only human access is not a usable network contract for the background service.

Within a run, adapters return canonical records that are transformed and compared in memory. The service persists the source scope, complete immutable canonical snapshots under configurable retention, canonical hashes/differences, checkpoints, and delivery/action ledger in its own durable state/audit database. This provides deterministic comparison, queryable evidence, restart recovery, and idempotency without relying on physical CSV files. A CSV remains only an outbound compatibility adapter until its consumer contract is replaced or decommissioned.

Containers, Kubernetes, message brokers, and distributed workflow platforms are deliberately not selected: current evidence does not demonstrate a scale or availability requirement that justifies their operational cost. Scaling starts with measured, bounded workers and per-store scope locks.

## 7. Technology evaluation and decision gates

The approved implementation stack is Python 3.12, FastAPI, SQLAlchemy 2, Alembic, and PostgreSQL for the service, with React, TypeScript, Vite, and Tailwind CSS for the browser UI. A change requires an ADR showing a material operational, support, security, or integration advantage and a migration plan for existing models/contracts.

No microservice split, container platform, broker, or distributed workflow engine is permitted without measured scale/availability evidence and explicit architecture approval.

## 8. Failure architecture

| Dependency / failure | Detection | Retry / recovery | Data-consistency rule | Operator action |
| --- | --- | --- | --- | --- |
| SQL Server unavailable/timeout | Dependency health + adapter error. | Retryable within policy; retain window/checkpoint. | No AIMS action from incomplete/invalid source snapshot. | Restore dependency; retry/replay approved window. |
| AIMS/API unavailable/network interruption | Timeout, response classification, health. | Retryable only with idempotency key. | Persist ambiguous submission; reconcile before resend. | Escalate/vendor check if terminal. |
| AIMS rejection/unexpected response | Contract validation/error code. | Usually non-retryable until corrected. | Preserve payload/result; do not substitute direct DB write. | Quarantine/review. |
| Compatibility DB unavailable/schema drift | Query/schema validation. | Retry only availability errors. | No mutation depends solely on unverified read. | Disable adapter/use approved fallback; investigate vendor contract. |
| Malformed source/configuration | Validation/startup check. | Non-retryable until corrected. | Quarantine; no side effect. | Correct approved config/data. |
| Promotion ambiguity or unsupported UOM | Domain validation/ambiguity record with candidate evidence. | Non-retryable until a correction or approved policy exists. | Do not invent a conversion/winner or submit a new ambiguous promotion state. | Review source, preserve compatibility evidence, escalate business-rule decision. |
| Process crash/server reboot | Startup recovery scans active leases/checkpoints. | Resume or reconcile. | Never assume unknown external submission failed. | Review recovery report. |
| Disk exhaustion / logging outage | Host/telemetry health. | Stop new work safely when durability/audit cannot be ensured. | Preserve state before action. | Restore capacity. |
| Expired/rotated credential | Authentication health. | Retry after approved rotation only. | No fallback to embedded credentials. | Rotate secret and validate. |

## 9. Security architecture

Trust boundaries are SQL Server, AIMS API, optional read-only AIMS PostgreSQL, filesystem consumer, secrets platform, and monitoring platform. Each receives a separate identity and least privilege. The compatibility identity may SELECT only the explicitly approved views/tables and must lack write/DDL privileges. Target AIMS mutations require approved API authentication, TLS where supported, request/response audit, and allowlisted network routes. Credentials, encrypted blobs, and host-specific production configuration are never copied into documentation or source control.

## 10. Architectural decisions

| ID | Decision | Status / rationale |
| --- | --- | --- |
| AD-001 | Use a modular single service with durable state. | Approved; replaces runtime components without premature distributed complexity. |
| AD-002 | Keep domain rules separate from adapters and orchestration. | Approved; enables traceable testable rule extraction. |
| AD-003 | Treat AIMS as an external vendor boundary; prohibit direct AIMS DB writes. | Approved; avoids undocumented schema coupling. |
| AD-004 | Allow a temporary read-only AIMS PostgreSQL compatibility adapter for first cutover. | Approved by stakeholder; retire/replace after vendor API investigation. |
| AD-005 | Use shadow-first migration and preserve legacy fallback. | Approved; avoids big-bang cutover. |
| AD-006 | Do not create repository-local skills yet. | Verified compatible repository-local convention was not established from official documentation; no justified repeatable workflow exists in this phase. |
| AD-007 | Process canonical records in memory and persist complete immutable snapshots under configurable retention plus comparison/delivery state in the target state/audit database. | Approved; CSV files are not a target state or parity mechanism and remain only as temporary compatibility delivery if required. |
| AD-008 | Use Python + PostgreSQL as an internal web application with browser UI/API, CLI, and optional Windows Service hosting. | Stakeholder clarified that the system is web based and may run as a Windows Service or by command line; PostgreSQL owns target state/audit/log queries, not retail/AIMS source data. |
| AD-009 | Use Windows DPAPI-protected per-environment secret bundles and GitHub Actions artifact promotion with controlled local deployment. | Chosen for the currently known Windows/GitHub and potentially Internet-isolated production environment; production does not need GitHub connectivity. |
| AD-010 | Accept the high-spec Windows 10 PC as the production host under operational risk acceptance; deny public ingress and use least-privilege private network routes. | This removes the unavailable Windows Server dependency while recording the operational controls and future-platform improvement path. |
| AD-011 | Use FastAPI for the internal backend/API and React + TypeScript + Vite + Tailwind CSS for the browser UI; use Google Stitch as a versioned design handoff. | The stack supports typed API contracts, controlled Windows deployment, and systematic conversion of Stitch exports into maintainable, tested components. |
| AD-012 | Use an automated ACL-restricted CSV/ready-manifest/acknowledgement handshake for bounded compatibility delivery. | The legacy consumer is unknown, so PostgreSQL remains authoritative and file presence is never completion. The adapter is disabled until consumer acceptance and adds no HTTP surface. |
| AD-013 | Use remote develop as the issue-led integration branch. | Approved workflow: issue branches target develop, merge only after review/check evidence, and local develop is fast-forwarded after each merge. |
| AD-014 | Trace every implementation or documentation change to an assigned GitHub issue and isolated branch/worktree. | Approved; keeps scope, test evidence, handoff, and external effects reviewable across agents. |
| AD-015 | Extract promotion behavior into explicit, independently testable domain rules and retain a reference-directed conservative selection strategy until business priority is approved. | The new business-rule reference defines target eligibility, pricing, UOM, atomic-state, and anomaly boundaries; deployed parity remains a separate evidence gate because the supplied procedure is review-only, filters status, and is not safely executable as supplied. |
| AD-016 | Use a hybrid relational plus versioned-JSONB model and retain complete immutable canonical snapshots under configurable retention. | Approved by stakeholder. Stable identities, states, relationships, and query fields remain relational; evolving evidence is typed, versioned, hashed JSONB. Documentation changes precede app/schema changes, and snapshots remain audit/replay evidence rather than a retail/AIMS master. |
