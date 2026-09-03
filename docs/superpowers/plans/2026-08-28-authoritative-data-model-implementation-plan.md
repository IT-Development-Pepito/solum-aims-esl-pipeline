# Authoritative Data Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Implement AD-016 as typed application models and an additive PostgreSQL schema that retains complete immutable canonical snapshots, durable workflow/action evidence, reconciliation, and query-safe audit without inventing unresolved business policy.

**Architecture:** Stable identifiers, states, relationships, constraints, and indexed query fields are relational. Complete records and evolving evidence use validated, schema-versioned JSONB with deterministic SHA-256 hashes. Frozen domain dataclasses remain independent of SQLAlchemy and FastAPI; persistence models own PostgreSQL concerns; Pydantic read models expose sanitized fields only.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, PostgreSQL JSONB/NUMERIC/TIMESTAMPTZ, psycopg, Pydantic/FastAPI schemas, pytest, Ruff, mypy, GitHub Actions, React/TypeScript contract consumers.

**Spec:** docs/SYSTEM_ARCHITECTURE.md section 5; docs/SPECIFICATION.md; docs/WORKFLOW.md; docs/PROGRESS.md

## Global Constraints

- Read AGENTS.md, docs/PROGRESS.md, the assigned issue, and SYSTEM_ARCHITECTURE.md section 5 before each task.
- Use one assigned issue branch/worktree from current remote develop per WORKFLOW.md. Do not combine unrelated issues.
- Preserve alembic/versions/0001_operational_state.py byte-for-byte. Every schema change is a later migration.
- SQL Server and AIMS remain external sources of record. No task authorizes writes to their databases, an AIMS HTTP mutation, CSV publication, a physical ESL effect, or a production database change.
- Use canonical key store_code + item_code + selling_uom. Business identifiers are strings; timestamps are UTC TIMESTAMPTZ.
- Use Decimal and NUMERIC(19,4), never binary float. Canonical JSON encodes Decimal as normalized strings and temporal values as ISO-8601 strings.
- Every JSONB object has a schema name/version, allowlisted typed constructor, and deterministic hash where applicable. Reject secret-like keys and unrestricted transport bodies.
- Retain complete immutable canonical snapshots under configured retention. CSV is never state, replay input, audit, or parity evidence.
- Promotion evidence cannot cross canonical keys. Preserve raw DISC_TEXT and do not invent a winner, rounding rule, non-CLR conversion, or weekday policy.
- Retention durations and CSV consumer acceptance remain UNKNOWN / NEEDS-DISCOVERY. Purge stays disabled without explicit values.
- Shadow actions stop at INTENDED or SKIPPED_IDEMPOTENT. OUTCOME_UNKNOWN requires reconciliation before resubmission.
- Durable evidence FKs use RESTRICT. Purge is explicit, child-first, authorized, and audited.
- Update PROGRESS.md after each red/green cycle, migration, review, and merge. Never record secret values.
- Run focused tests, Ruff, mypy, full pytest, database migration tests when ESL_TEST_DATABASE_URL is configured, frontend checks for exposed contracts, and git diff --check before each PR.
- Merge each task PR and fast-forward local develop before starting the next task.

## Supersession and issue order

This plan supersedes the pre-AD-016 domain description in Task 4, persistence/reconciliation clauses in Task 6, and data-query clauses in Task 7 of the 2026-08-25 foundation plan. Completed Tasks 1–3 remain historical evidence; adapter, runtime, UI, staging, and deployment work remains valid.

The CSV delivery plan remains authoritative after this core plan and consumer acceptance. Its persistence revision follows 0008, expected 0009_compatibility_delivery.py when no intervening revision exists.

| Order | Existing issue | Deliverable |
| --- | --- | --- |
| 1 | #10 | Canonical types, serialization, hash, diff, and KGS price bases. |
| 2 | #13 | Model-package split, store/config versions, snapshots, and differences. |
| 3 | #18 | Execution lifecycle, steps, checkpoints, leases, restrictive FKs. |
| 4 | #36 | Promotion candidate/evaluation evidence without winner policy. |
| 5 | #12 | Record processing results and independently queryable issues. |
| 6 | #19 | Idempotent action lifecycle and append-only attempts. |
| 7 | #25 then #27 | Audit/reconciliation, retention safety, queries, Pydantic reads. |
| 8 | #21 | Migration chain, replay, CI database gate, traceability. |

Issue #37 consumes the finished promotion evidence model after Task 8. Blocked issue #30 uses the CSV plan only after named consumer acceptance.

## Planned file structure

~~~text
src/esl_service/domain/{canonical,serialization,diff,promotion_evidence,outcomes,actions,reconciliation}.py
src/esl_service/persistence/models/{base,configuration,execution,evidence,outcomes,actions,reconciliation}.py
src/esl_service/persistence/{snapshot,evidence,action,reconciliation}_repository.py
src/esl_service/persistence/retention.py
src/esl_service/web/{audit_schemas,audit_queries}.py
alembic/versions/0002_configuration_and_snapshots.py
alembic/versions/0003_execution_recovery.py
alembic/versions/0004_promotion_evidence.py
alembic/versions/0005_record_outcomes.py
alembic/versions/0006_action_lifecycle.py
alembic/versions/0007_audit_reconciliation.py
alembic/versions/0008_authoritative_model_gate.py
tests/unit/domain/
tests/unit/persistence/
tests/unit/web/
tests/integration/
docs/DATA_MODEL_TRACEABILITY.md
~~~

Task 2 replaces persistence/models.py with a models package that re-exports Base and all existing classes through the unchanged esl_service.persistence.models path. repository.py remains the ExecutionRepository compatibility entry point.

## Physical migration contract

| Revision | Tables/changes | Required constraints |
| --- | --- | --- |
| 0002 | store_configuration, configuration_version, snapshot_set, canonical_record_snapshot, record_difference; extend workflow_schedule. | Unique store/config hash/snapshot key; 64-char hashes; JSON objects; RESTRICT. |
| 0003 | Extend workflow_execution/scope_lease; execution_step, execution_checkpoint; replace evidence CASCADE. | Controlled states; unique correlation/step/checkpoint; valid lease interval; RESTRICT. |
| 0004 | promotion_evaluation, promotion_candidate_snapshot. | One evaluation per snapshot/rule/calculation; candidate uniqueness; selected candidate FK; RESTRICT. |
| 0005 | record_processing_result, record_issue. | One result per execution/key; multiple issues; controlled statuses; RESTRICT. |
| 0006 | Extend record_action; action_attempt. | Unique idempotency key; controlled transitions; unique attempt; hash checks; RESTRICT. |
| 0007 | audit_entry, reconciliation_report, reconciliation_exception. | Append-only audit; unique report revision; nonnegative counts; RESTRICT. |
| 0008 | Preflight, required config refs, final indexes/checks. | Abort on historical null/invalid state; fabricate no evidence; retain RESTRICT. |

Migration downgrade is tested only in a disposable non-production database. Production recovery is forward-only with retained evidence.

---

### Task 1: Define immutable canonical domain contracts (#10)

**Files:**
- Create: src/esl_service/domain/__init__.py
- Create: src/esl_service/domain/canonical.py
- Create: src/esl_service/domain/serialization.py
- Create: src/esl_service/domain/diff.py
- Create: tests/factories.py
- Create: tests/unit/domain/test_canonical.py
- Create: tests/unit/domain/test_diff.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Produces CanonicalKey, PriceBasis, ProductState, PricingState, InventoryState, ExpiryState, PromotionStateData, DisplayDecision, Provenance, CanonicalEslRecord.
- Produces canonical_payload(value), canonical_hash(value), and diff_records(left, right).
- Consumed by Tasks 2–8 and issues #11, #13, #36, and #37.

- [ ] **Step 1: Write failing canonical tests**

~~~python
def test_kgs_preserves_source_and_display_basis() -> None:
    record = canonical_record(
        source_regular_price=Decimal("50000"),
        display_regular_price=Decimal("5000"),
        source_price_basis=PriceBasis.KG,
        display_price_basis=PriceBasis.HUNDRED_GRAMS,
    )
    assert record.pricing.source_regular_price == Decimal("50000")
    assert record.pricing.display_regular_price == Decimal("5000")

def test_diff_names_changed_paths() -> None:
    left = canonical_record()
    right = replace(
        left,
        pricing=replace(left.pricing, display_regular_price=Decimal("4500")),
    )
    assert [item.path for item in diff_records(left, right)] == [
        "pricing.display_regular_price"
    ]
~~~

tests/factories.py builds a complete store 084/item 101024011793/KGS/IDR record with every section and canonical-v1/config-v1/rules-v1 provenance.

- [ ] **Step 2: Run and confirm red**

~~~powershell
python -m pytest tests/unit/domain/test_canonical.py tests/unit/domain/test_diff.py -v
~~~

Expected: ModuleNotFoundError for esl_service.domain.

- [ ] **Step 3: Implement exact immutable types**

~~~python
@dataclass(frozen=True)
class CanonicalKey:
    store_code: str
    item_code: str
    selling_uom: str

class PriceBasis(StrEnum):
    EACH = "EACH"
    KG = "KG"
    HUNDRED_GRAMS = "100GR"

@dataclass(frozen=True)
class PricingState:
    currency: str
    source_regular_price: Decimal | None
    source_member_price: Decimal | None
    source_price_basis: PriceBasis
    display_regular_price: Decimal | None
    display_price_basis: PriceBasis
    calculation_version: str

@dataclass(frozen=True)
class CanonicalEslRecord:
    key: CanonicalKey
    schema_version: str
    product: ProductState
    pricing: PricingState
    inventory: InventoryState
    expiry: ExpiryState
    promotion_state: PromotionStateData | None
    display_decision: DisplayDecision
    provenance: Provenance
~~~

ProductState contains all product/classification/flag fields in architecture 5.5. InventoryState contains stock/weight/min/max/display quantities. ExpiryState contains early_expiry_date/expiry_days. PromotionStateData contains one candidate's complete campaign/type/value/effective/display/discount/saving/raw-text/window state. DisplayDecision contains current_page, desired_page, reason_code. Provenance contains adapter, watermark, source update, configuration/rule versions, source references. __post_init__ rejects blank keys and negative pages without adding business policy.

- [ ] **Step 4: Implement canonical JSON/hash/diff**

~~~python
def _json_value(value: object) -> JSONValue:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")

def canonical_hash(value: object) -> str:
    payload = canonical_payload(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
~~~

FieldDifference is frozen with path, old_value, new_value. diff_records recursively compares payloads and returns sorted paths.

- [ ] **Step 5: Verify and submit #10**

~~~powershell
python -m pytest tests/unit/domain/test_canonical.py tests/unit/domain/test_diff.py -v
python -m ruff check src tests
python -m mypy src
python -m pytest -q
git diff --check
git add src/esl_service/domain tests/factories.py tests/unit/domain docs/PROGRESS.md
git commit -m "feat(domain): define canonical ESL record contract (#10)"
~~~

Push a #10 PR to develop, merge after checks/review, and fast-forward develop.
---

### Task 2: Persist configuration, snapshots, and differences (#13)

**Files:**
- Delete: src/esl_service/persistence/models.py
- Create: src/esl_service/persistence/models/__init__.py
- Create: src/esl_service/persistence/models/base.py
- Create: src/esl_service/persistence/models/configuration.py
- Create: src/esl_service/persistence/models/execution.py
- Create: src/esl_service/persistence/models/evidence.py
- Create: src/esl_service/persistence/snapshot_repository.py
- Create: alembic/versions/0002_configuration_and_snapshots.py
- Create: tests/integration/conftest.py
- Create: tests/integration/test_configuration_snapshots.py
- Modify: alembic/env.py
- Modify: src/esl_service/persistence/repository.py
- Modify: tests/integration/test_repository.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Preserves imports for Base, WorkflowExecution, ScopeLease, ExecutionEvent, RecordAction, WorkflowSchedule.
- Produces StoreConfiguration, ConfigurationVersion, SnapshotSet, CanonicalRecordSnapshot, RecordDifference.
- Produces SnapshotRepository.create_snapshot_set(), append_record(), append_difference(), list_records().
- Consumes Task 1 canonical types and serializers.

- [ ] **Step 1: Write failing PostgreSQL tests**

~~~python
def test_snapshot_round_trip_preserves_complete_record(
    session: Session,
    snapshot_repository: SnapshotRepository,
) -> None:
    execution = create_execution(session)
    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark="2026-08-28T07:00:00Z",
        canonical_schema_version="canonical-v1",
    )
    source = canonical_record()
    snapshot_repository.append_record(snapshot_set.id, source)
    session.flush()
    session.expire_all()
    loaded = snapshot_repository.list_records(snapshot_set.id)[0]
    assert loaded.canonical_hash == canonical_hash(source)
    assert loaded.payload == canonical_payload(source)
    assert (loaded.store_code, loaded.item_code, loaded.selling_uom) == (
        "084", "101024011793", "KGS"
    )

def test_snapshot_key_is_unique_per_set(
    snapshot_repository: SnapshotRepository,
) -> None:
    snapshot_set = create_snapshot_set(snapshot_repository)
    snapshot_repository.append_record(snapshot_set.id, canonical_record())
    with pytest.raises(IntegrityError):
        snapshot_repository.append_record(snapshot_set.id, canonical_record())
~~~

conftest.py skips only when ESL_TEST_DATABASE_URL is absent, refuses database names postgres/template0/template1 or configured production name, migrates the dedicated DB through 0002, and rolls back test data.

- [ ] **Step 2: Run and confirm red**

~~~powershell
$env:ESL_DATABASE_URL=$env:ESL_TEST_DATABASE_URL
python -m pytest tests/integration/test_configuration_snapshots.py -v
~~~

Expected: missing revision and repository. A skipped red phase is not evidence; configure the dedicated test DB first.

- [ ] **Step 3: Split models while preserving imports**

models/base.py owns only DeclarativeBase. models/execution.py initially moves the five current classes unchanged. models/__init__.py imports every class so Base.metadata is complete:

~~~python
from esl_service.persistence.models.base import Base
from esl_service.persistence.models.configuration import ConfigurationVersion, StoreConfiguration
from esl_service.persistence.models.evidence import (
    CanonicalRecordSnapshot,
    RecordDifference,
    SnapshotSet,
)
from esl_service.persistence.models.execution import (
    ExecutionEvent,
    RecordAction,
    ScopeLease,
    WorkflowExecution,
    WorkflowSchedule,
)
~~~

Run current tests after the split before adding schema behavior.

- [ ] **Step 4: Implement 0002 models/migration/repository**

store_configuration: UUID id, unique store_code, display_name, timezone, enabled, options_schema_version, options JSONB object, created_at, updated_at.

configuration_version: UUID id, environment, schema_version, content_hash, sanitized_snapshot JSONB object, activated_at/by; unique environment/content_hash and 64-char hash.

snapshot_set: UUID id, execution FK RESTRICT, representation kind, adapter, source window/watermark, canonical schema version, captured_at, record_count, aggregate_hash; unique execution/representation/adapter/watermark.

canonical_record_snapshot: UUID id, snapshot_set FK RESTRICT, store/item/UOM, schema version/hash, payload JSONB, captured_at; unique set/store/item/UOM, hash length and JSON-object checks.

record_difference: UUID id, execution/left/right snapshot FKs RESTRICT, hashes, type, changed_paths ARRAY(Text), values_payload JSONB, diff/rule versions, created_at.

workflow_schedule gains updated_at and nullable configuration_version_id. New application writes require a version; 0008 makes it non-null after explicit backfill/preflight.

~~~python
def append_record(
    self, snapshot_set_id: UUID, record: CanonicalEslRecord
) -> CanonicalRecordSnapshot:
    stored = CanonicalRecordSnapshot(
        snapshot_set_id=snapshot_set_id,
        store_code=record.key.store_code,
        item_code=record.key.item_code,
        selling_uom=record.key.selling_uom,
        canonical_schema_version=record.schema_version,
        canonical_hash=canonical_hash(record),
        payload=canonical_payload(record),
    )
    self._session.add(stored)
    self._session.flush()
    return stored
~~~

Repository methods do not commit caller transactions.

- [ ] **Step 5: Verify and submit #13**

~~~powershell
$env:ESL_DATABASE_URL=$env:ESL_TEST_DATABASE_URL
python -m alembic downgrade 0001_operational_state
python -m alembic upgrade 0002_configuration_and_snapshots
python -m pytest tests/integration/test_repository.py tests/integration/test_configuration_snapshots.py -v
python -m alembic downgrade 0001_operational_state
python -m alembic upgrade head
python -m ruff check src tests
python -m mypy src
python -m pytest -q
git diff --check
git add src/esl_service/persistence alembic tests/integration docs/PROGRESS.md
git commit -m "feat(persistence): persist canonical snapshot evidence (#13)"
~~~

Merge #13 and fast-forward develop.

---

### Task 3: Add restart-safe execution state (#18)

**Files:**
- Create: src/esl_service/domain/outcomes.py
- Create: alembic/versions/0003_execution_recovery.py
- Create: tests/integration/test_execution_recovery.py
- Modify: src/esl_service/persistence/models/execution.py
- Modify: src/esl_service/persistence/models/__init__.py
- Modify: src/esl_service/persistence/repository.py
- Modify: tests/integration/test_repository.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Produces ExecutionMode, TriggerType, ExecutionStatus, StepStatus, FailureClass, NewExecution.
- Produces ExecutionStep and ExecutionCheckpoint.
- Extends ExecutionRepository with create_execution(NewExecution), claim/heartbeat/release scope, start_step(), append_checkpoint(), recoverable_executions(), transition_execution().

- [ ] **Step 1: Write failing recovery and delete-safety tests**

~~~python
def test_checkpoint_survives_new_session(session_factory: sessionmaker[Session]) -> None:
    execution_id = create_running_execution(session_factory)
    with session_factory.begin() as session:
        repository = ExecutionRepository(session)
        step = repository.start_step(execution_id, "canonicalize", attempt=1)
        repository.append_checkpoint(
            step.id,
            checkpoint_key="last-record",
            checkpoint_version=1,
            watermark="084:101024011793:KGS",
            payload={"record_count": 1},
        )
    with session_factory() as session:
        recovered = ExecutionRepository(session).recoverable_executions()
        assert recovered[0].id == execution_id
        assert recovered[0].steps[0].checkpoints[0].watermark.endswith(":KGS")

def test_execution_delete_is_restricted_with_evidence(session: Session) -> None:
    execution = create_execution_with_event(session)
    with pytest.raises(IntegrityError):
        session.delete(execution)
        session.flush()
~~~

Also test one lease owner, heartbeat extension, expiry discovery, retry/replay parent IDs, UTC windows, and invalid transitions.

- [ ] **Step 2: Run and confirm red**

~~~powershell
$env:ESL_DATABASE_URL=$env:ESL_TEST_DATABASE_URL
python -m pytest tests/integration/test_execution_recovery.py tests/integration/test_repository.py -v
~~~

Expected: missing lifecycle fields/tables and restrictive FKs.

- [ ] **Step 3: Implement execution input/state types**

~~~python
class ExecutionMode(StrEnum):
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"

class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_WITH_EXCEPTIONS = "SUCCEEDED_WITH_EXCEPTIONS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"

@dataclass(frozen=True)
class NewExecution:
    workflow_name: str
    store_code: str
    trigger_type: TriggerType
    mode: ExecutionMode
    correlation_id: UUID
    source_window_start: datetime
    source_window_end: datetime
    configuration_version_id: UUID
    rule_version: str
    requested_by: str | None
    reason: str | None
    retry_of_execution_id: UUID | None = None
    replay_of_execution_id: UUID | None = None
~~~

ExecutionStep is unique execution/step_name/attempt. ExecutionCheckpoint is unique step/checkpoint_key/version and stores watermark, payload schema/hash, JSON object, occurred_at.

- [ ] **Step 4: Implement 0003 and transition guards**

Extend workflow_execution with all NewExecution fields, ended_at, terminal_reason. Extend scope_lease with heartbeat_at, expires_at, released_at, lease_version and expires > acquired. Replace execution_event and record_action CASCADE FKs with RESTRICT.

~~~python
_ALLOWED_TRANSITIONS = {
    ExecutionStatus.QUEUED: {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED, ExecutionStatus.SKIPPED},
    ExecutionStatus.RUNNING: {
        ExecutionStatus.RETRY_WAIT, ExecutionStatus.RECOVERING,
        ExecutionStatus.SUCCEEDED, ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS,
        ExecutionStatus.FAILED, ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.RETRY_WAIT: {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED},
    ExecutionStatus.RECOVERING: {ExecutionStatus.RUNNING, ExecutionStatus.FAILED},
}
~~~

transition_execution uses compare-and-set UPDATE and appends an event in the same transaction. Migrate existing create_execution call sites to NewExecution; do not retain a positional overload.

- [ ] **Step 5: Verify and submit #18**

Run migration directions, focused tests, Ruff, mypy, full pytest, and diff check. Commit:

~~~powershell
git add src/esl_service/domain/outcomes.py src/esl_service/persistence alembic/versions/0003_execution_recovery.py tests/integration docs/PROGRESS.md
git commit -m "feat(workflow): persist restart-safe execution state (#18)"
~~~

Merge #18 and fast-forward develop.
---

### Task 4: Persist promotion evidence without a winner policy (#36)

**Files:**
- Create: src/esl_service/domain/promotion_evidence.py
- Create: src/esl_service/persistence/evidence_repository.py
- Create: alembic/versions/0004_promotion_evidence.py
- Create: tests/unit/domain/test_promotion_evidence.py
- Create: tests/integration/test_promotion_evidence_repository.py
- Modify: src/esl_service/persistence/models/evidence.py
- Modify: src/esl_service/persistence/models/__init__.py
- Modify: tests/factories.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Produces PromotionOutcome, CandidateEligibility, WeekdayEvidence, PromotionCandidateEvidence, PromotionEvaluationEvidence.
- Produces PromotionEvidenceRepository.record_evaluation() and get_evaluation().
- Produces no select_winner function. Issue #37 consumes these contracts after Task 8.

- [ ] **Step 1: Write failing invariant/round-trip tests**

~~~python
def test_selected_outcome_requires_local_candidate() -> None:
    candidate = promotion_candidate(store_code="084", selling_uom="KGS")
    with pytest.raises(ValueError, match="selected candidate"):
        PromotionEvaluationEvidence(
            key=candidate.key,
            rule_version="rules-v1",
            calculation_version="calc-v1",
            outcome=PromotionOutcome.SELECTED,
            candidates=(candidate,),
            selected_candidate_id="different",
            resulting_state=None,
        )

def test_ambiguous_evaluation_retains_all_candidates(
    promotion_repository: PromotionEvidenceRepository,
) -> None:
    stored = promotion_repository.record_evaluation(
        snapshot_id(), ambiguous_promotion_evaluation()
    )
    assert stored.outcome == "AMBIGUOUS"
    assert {row.source_campaign_id for row in stored.candidates} == {"A", "B"}
    assert stored.selected_candidate_id is None
~~~

Add cases for raw DISC_TEXT, category-001 price, source/resolved UOM, missing versus inactive weekday, PFS exclusion, invalid values, KGS post-economic display values, and cross-key rejection.

- [ ] **Step 2: Run and confirm red**

Run focused unit then PostgreSQL tests. Expected: missing promotion module, repository, and revision.

- [ ] **Step 3: Implement typed evidence**

~~~python
class PromotionOutcome(StrEnum):
    NO_PROMOTION = "NO_PROMOTION"
    SELECTED = "SELECTED"
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"

@dataclass(frozen=True)
class PromotionCandidateEvidence:
    candidate_id: str
    key: CanonicalKey
    source_campaign_id: str
    campaign_group: str | None
    promotion_type: str
    structured_value: Decimal
    raw_disc_text: str | None
    starts_at: datetime
    ends_at: datetime
    weekday_evidence: WeekdayEvidence
    category_001_regular_price: Decimal
    source_uom: str
    resolved_selling_uom: str | None
    calculated_effective_price: Decimal | None
    display_price: Decimal | None
    eligibility: CandidateEligibility
    reason_codes: tuple[str, ...]
    fallback_codes: tuple[str, ...]

@dataclass(frozen=True)
class PromotionEvaluationEvidence:
    key: CanonicalKey
    rule_version: str
    calculation_version: str
    outcome: PromotionOutcome
    candidates: tuple[PromotionCandidateEvidence, ...]
    selected_candidate_id: str | None
    resulting_state: PromotionStateData | None
~~~

Validate key equality, candidate uniqueness, selected membership only for SELECTED, and atomic resulting state from that candidate. Do not choose a candidate.

- [ ] **Step 4: Implement 0004 and repository**

promotion_evaluation stores snapshot FK, rule/calculation versions, outcome, nullable selected candidate FK, resulting-state JSONB, evaluated_at; unique snapshot/rule/calculation.

promotion_candidate_snapshot stores evaluation FK, source campaign, group/type/value/raw text/window/weekday/category-001/source-resolved UOM/calculated prices/eligibility/reasons/fallbacks/evidence payload; unique evaluation/source campaign. Use NUMERIC and RESTRICT.

Insert evaluation, flush, insert candidates, then set selected_candidate_id only after membership validation; commit nothing.

- [ ] **Step 5: Verify and submit #36**

Run focused tests, migration directions, full Python checks, and diff check. Commit:

~~~powershell
git add src/esl_service/domain/promotion_evidence.py src/esl_service/persistence alembic/versions/0004_promotion_evidence.py tests docs/PROGRESS.md
git commit -m "feat(domain): persist promotion decision evidence (#36)"
~~~

Merge #36, fast-forward develop, and do not claim deployed parity; #38 remains the gate.

---

### Task 5: Persist record outcomes and issues (#12)

**Files:**
- Create: src/esl_service/persistence/models/outcomes.py
- Create: alembic/versions/0005_record_outcomes.py
- Create: tests/unit/domain/test_outcomes.py
- Create: tests/integration/test_outcome_repository.py
- Modify: src/esl_service/domain/outcomes.py
- Modify: src/esl_service/domain/serialization.py
- Modify: src/esl_service/persistence/evidence_repository.py
- Modify: src/esl_service/persistence/models/__init__.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Produces ProcessingStatus: REJECTED, UNRESOLVED, INELIGIBLE, UNCHANGED, ACTION_REQUIRED.
- Produces RecordIssueEvidence and RecordProcessingEvidence.
- Produces EvidenceRepository.record_processing_result().

- [ ] **Step 1: Write failing multi-issue/security tests**

~~~python
def test_record_retains_multiple_independent_issues(
    evidence_repository: EvidenceRepository,
) -> None:
    result = record_processing_evidence(
        status=ProcessingStatus.UNRESOLVED,
        issues=(
            issue("BR-013", "UOM_RULE_REQUIRED"),
            issue("BR-019", "PROMO_PRIORITY_DIFFERENT_ECONOMIC"),
        ),
    )
    stored = evidence_repository.record_processing_result(
        execution_id(), snapshot_id(), result
    )
    assert [row.issue_code for row in stored.issues] == [
        "UOM_RULE_REQUIRED",
        "PROMO_PRIORITY_DIFFERENT_ECONOMIC",
    ]

@pytest.mark.parametrize("key", ["password", "token", "authorization", "database_url"])
def test_issue_evidence_rejects_secret_like_keys(key: str) -> None:
    with pytest.raises(ValueError, match="forbidden evidence key"):
        issue("FR-003", "INVALID", evidence={key: "value"})
~~~

Also test unique execution/key and reject ACTION_REQUIRED when validation status is rejected.

- [ ] **Step 2: Run and confirm red**

Expected: missing contracts, sanitizer, and 0005.

- [ ] **Step 3: Implement contracts/sanitizer**

~~~python
@dataclass(frozen=True)
class RecordIssueEvidence:
    rule_id: str
    issue_code: str
    severity: str
    classification: str
    evidence: Mapping[str, JSONValue]

@dataclass(frozen=True)
class RecordProcessingEvidence:
    key: CanonicalKey
    validation_status: str
    eligibility_status: str
    promotion_outcome: PromotionOutcome | None
    current_page: int | None
    desired_page: int | None
    action_decision: str
    processing_status: ProcessingStatus
    issues: tuple[RecordIssueEvidence, ...]
~~~

sanitize_evidence recursively rejects case-insensitive key fragments password, passwd, secret, token, authorization, connection_string, database_url, dpapi.

- [ ] **Step 4: Implement 0005 and persistence**

record_processing_result stores execution/snapshot FKs, relational canonical key, validation/eligibility/promotion outcomes, current/desired page, action decision, processing status, occurred_at; unique execution/key.

record_issue stores result FK, rule ID, issue code, severity, classification, evidence schema version/object, resolution metadata, created_at. Multiple issues are allowed; FKs RESTRICT.

- [ ] **Step 5: Verify and submit #12**

Run focused tests, migration directions, full Python checks, diff check. Commit:

~~~powershell
git add src/esl_service/domain src/esl_service/persistence alembic/versions/0005_record_outcomes.py tests docs/PROGRESS.md
git commit -m "feat(ingestion): persist record outcomes and issues (#12)"
~~~

Merge #12 and fast-forward develop.

---

### Task 6: Enforce action and attempt lifecycles (#19)

**Files:**
- Create: src/esl_service/domain/actions.py
- Create: src/esl_service/persistence/models/actions.py
- Create: src/esl_service/persistence/action_repository.py
- Create: alembic/versions/0006_action_lifecycle.py
- Create: tests/unit/domain/test_actions.py
- Create: tests/integration/test_action_repository.py
- Modify: src/esl_service/persistence/models/execution.py
- Modify: src/esl_service/persistence/models/__init__.py
- Modify: src/esl_service/persistence/repository.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Produces ActionState, DeliveryCertainty, NewRecordAction, ActionAttemptEvidence, build_idempotency_key().
- Produces ActionRepository.create_intended(), transition(), append_attempt(), unresolved_actions().
- ExecutionRepository.record_action forwards to ActionRepository for one release and is then removed by its own issue.

- [ ] **Step 1: Write failing lifecycle tests**

~~~python
def test_duplicate_action_returns_existing(action_repository: ActionRepository) -> None:
    request = intended_page_action(mode=ExecutionMode.ACTIVE)
    first = action_repository.create_intended(request)
    second = action_repository.create_intended(request)
    assert second.id == first.id

def test_shadow_action_cannot_submit(action_repository: ActionRepository) -> None:
    action = action_repository.create_intended(
        intended_page_action(mode=ExecutionMode.SHADOW)
    )
    with pytest.raises(InvalidActionTransition):
        action_repository.transition(action.id, ActionState.SUBMITTING)

def test_unknown_outcome_requires_reconciliation(
    action_repository: ActionRepository,
) -> None:
    action = submitted_action(action_repository)
    action_repository.append_attempt(action.id, attempt(DeliveryCertainty.UNKNOWN))
    assert action_repository.unresolved_actions() == [action.id]
~~~

- [ ] **Step 2: Run and confirm red**

Expected: missing action domain/repository and 0006.

- [ ] **Step 3: Implement states and key**

~~~python
class ActionState(StrEnum):
    INTENDED = "INTENDED"
    SKIPPED_IDEMPOTENT = "SKIPPED_IDEMPOTENT"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

def build_idempotency_key(action: NewRecordAction) -> str:
    return canonical_hash({
        "contract_version": action.contract_version,
        "key": action.key,
        "label_code": action.label_code,
        "action_type": action.action_type,
        "desired_state": action.desired_state,
        "rule_version": action.rule_version,
        "configuration_hash": action.configuration_hash,
        "source_window_start": action.source_window_start,
        "source_window_end": action.source_window_end,
    })
~~~

Implement architecture 5.6 transitions exactly; OUTCOME_UNKNOWN never returns automatically to SUBMITTING.

- [ ] **Step 4: Implement 0006/repository**

Extend record_action with result FK, canonical key, label, desired page/state, idempotency/request hashes, state, mode, acknowledgement batch, updated/terminal timestamps. Existing rows become INTENDED/SHADOW only; do not claim submission. Unique idempotency and hash checks.

action_attempt stores action FK, attempt number, timing, retry class, HTTP/result code, response schema/object, delivery certainty, error class; unique action/attempt and RESTRICT.

create_intended uses INSERT ON CONFLICT DO NOTHING then SELECT. transition uses compare-and-set UPDATE. Repository methods do not commit.

- [ ] **Step 5: Verify and submit #19**

Run concurrency/idempotency tests, migration directions, full checks. Commit:

~~~powershell
git add src/esl_service/domain/actions.py src/esl_service/persistence alembic/versions/0006_action_lifecycle.py tests docs/PROGRESS.md
git commit -m "feat(workflow): enforce idempotent action lifecycle (#19)"
~~~

Merge #19 and fast-forward develop.
---

### Task 7: Add audit, reconciliation, retention, and read contracts (#25 then #27)

Use sequential issue worktrees/PRs when #25 and #27 have different reviewers. #25 owns persistence/query; #27 owns retention configuration and read schemas.

**Files:**
- Create: src/esl_service/domain/reconciliation.py
- Create: src/esl_service/persistence/models/reconciliation.py
- Create: src/esl_service/persistence/reconciliation_repository.py
- Create: src/esl_service/persistence/retention.py
- Create: src/esl_service/web/__init__.py
- Create: src/esl_service/web/audit_schemas.py
- Create: src/esl_service/web/audit_queries.py
- Create: alembic/versions/0007_audit_reconciliation.py
- Create: tests/unit/domain/test_reconciliation.py
- Create: tests/unit/persistence/test_retention.py
- Create: tests/unit/web/test_audit_schemas.py
- Create: tests/integration/test_reconciliation_repository.py
- Modify: src/esl_service/config.py
- Modify: src/esl_service/persistence/models/__init__.py
- Modify: docs/PROGRESS.md

**Interfaces:**
- Produces ReconciliationCounts, ReconciliationMode, validate_balance().
- Produces AuditEntry, ReconciliationReport, ReconciliationException.
- Produces ReconciliationRepository.finalize_report(), list_exceptions(), query_events(), query_audit().
- Produces RetentionPolicy with no duration defaults and RetentionService.find_eligible()/purge_execution().
- Produces sanitized Pydantic ExecutionAuditResponse and RecordEvidenceResponse; routes remain #26/#29.

- [ ] **Step 1: Write failing balance tests**

~~~python
def test_active_terminal_balance() -> None:
    counts = ReconciliationCounts(
        extracted=10, rejected=1, valid=9, ineligible=2, eligible=7,
        unchanged=1, skipped_idempotent=1, intended=0,
        acknowledged=3, rejected_by_aims=1, failed=0,
        unresolved=1, submitted=0,
    )
    validate_balance(ReconciliationMode.ACTIVE, counts)

def test_shadow_terminal_balance() -> None:
    counts = ReconciliationCounts(
        extracted=4, rejected=0, valid=4, ineligible=1, eligible=3,
        unchanged=1, skipped_idempotent=0, intended=1,
        acknowledged=0, rejected_by_aims=0, failed=0,
        unresolved=1, submitted=0,
    )
    validate_balance(ReconciliationMode.SHADOW, counts)

def test_submitted_blocks_terminal_report() -> None:
    with pytest.raises(UnbalancedReconciliation, match="submitted"):
        validate_balance(ReconciliationMode.ACTIVE, active_counts(submitted=1))
~~~

ambiguous_count is a diagnostic subset of unresolved and is never added twice.

- [ ] **Step 2: Write failing retention/read tests**

~~~python
def test_unknown_action_blocks_purge(retention_service: RetentionService) -> None:
    terminal_execution_with_unknown_action()
    assert retention_service.find_eligible(now=utc_now(), limit=100) == []

def test_retention_requires_explicit_duration() -> None:
    with pytest.raises(ValueError, match="detailed_evidence_days"):
        RetentionPolicy(
            audit_core_days=365,
            detailed_evidence_days=None,
            compatibility_days=None,
        )

def test_audit_response_excludes_internal_payload() -> None:
    response = ExecutionAuditResponse.model_validate(query_execution_audit())
    assert "payload" not in response.model_dump()
    assert "authorization" not in response.model_dump_json().lower()
~~~

- [ ] **Step 3: Run and confirm red**

Run the three unit files and reconciliation integration file. Expected: missing modules and 0007.

- [ ] **Step 4: Implement reconciliation/0007 (#25)**

~~~python
def validate_balance(mode: ReconciliationMode, counts: ReconciliationCounts) -> None:
    if counts.extracted != counts.rejected + counts.valid:
        raise UnbalancedReconciliation("extracted")
    if counts.valid != counts.ineligible + counts.eligible:
        raise UnbalancedReconciliation("valid")
    if counts.submitted:
        raise UnbalancedReconciliation("submitted actions are not terminal")
    if mode is ReconciliationMode.ACTIVE:
        terminal = (
            counts.unchanged + counts.skipped_idempotent + counts.acknowledged
            + counts.rejected_by_aims + counts.failed + counts.unresolved
        )
    else:
        terminal = (
            counts.unchanged + counts.skipped_idempotent
            + counts.intended + counts.unresolved
        )
    if counts.eligible != terminal:
        raise UnbalancedReconciliation("eligible")
~~~

audit_entry stores optional execution FK, actor/action/reason/resource, configuration version, correlation, outcome, evidence schema, sanitized before/after objects, occurred_at.

reconciliation_report stores execution, revision, mode, generated/finalized time, every count, ambiguous count, status; unique execution/revision and nonnegative checks. reconciliation_exception stores report/category/record/action refs, expected/actual objects, resolution fields. Use RESTRICT.

- [ ] **Step 5: Implement retention/read schemas (#27)**

Add settings retention_purge_enabled=False and optional positive audit_core_days, detailed_evidence_days, compatibility_days. When purge is enabled, all applicable values are mandatory; no durations are defaulted.

~~~python
def find_eligible(self, now: datetime, limit: int) -> list[UUID]:
    statement = (
        select(WorkflowExecution.id)
        .join(ReconciliationReport)
        .where(
            WorkflowExecution.status.in_(TERMINAL_STATUSES),
            ReconciliationReport.status == "FINALIZED",
            ReconciliationReport.unresolved == 0,
            WorkflowExecution.ended_at < now - self._policy.detailed_evidence_age,
            ~exists().where(
                RecordAction.execution_id == WorkflowExecution.id,
                RecordAction.state == ActionState.OUTCOME_UNKNOWN,
            ),
        )
        .order_by(WorkflowExecution.ended_at)
        .limit(limit)
    )
    return list(self._session.scalars(statement))
~~~

purge_execution refuses disabled/ineligible runs, deletes eligible detailed children in documented order, retains audit core, appends audit_entry, commits nothing.

ExecutionAuditResponse exposes IDs, workflow/store/time/status/config/rule versions, sanitized event summaries, counts, terminal reason. RecordEvidenceResponse exposes key/hashes/outcome/issue codes/candidate summaries/action states, never unrestricted JSONB.

- [ ] **Step 6: Verify and submit #25 then #27**

~~~powershell
python -m pytest tests/unit/domain/test_reconciliation.py tests/unit/persistence/test_retention.py tests/unit/web/test_audit_schemas.py tests/integration/test_reconciliation_repository.py -v
python -m ruff check src tests
python -m mypy src
python -m pytest -q
git diff --check
git add src/esl_service/domain/reconciliation.py src/esl_service/persistence alembic/versions/0007_audit_reconciliation.py tests docs/PROGRESS.md
git commit -m "feat(operations): persist audit and reconciliation evidence (#25)"
~~~

Merge #25. From fresh develop, commit #27:

~~~powershell
git add src/esl_service/config.py src/esl_service/persistence/retention.py src/esl_service/web tests docs/PROGRESS.md
git commit -m "feat(operations): add safe retention and audit read models (#27)"
~~~

Merge #27 and fast-forward develop.

---

### Task 8: Enforce migration/replay/CI gate (#21)

> **2026-09-03:** #21 delivered the `0008` gate (preflight, NOT NULL schedule version, active-scope unique index, `retry_not_before`, `execution_step.sequence`) and the FR-016 recovery scenarios. The remaining items of this task were split by the owner: snapshot replay to #114, the Linux database-verify job and conditional `pywin32` to #115, and `DATA_MODEL_TRACEABILITY.md` with the supersession notices to #116. The step text below is kept as written, for history.

**Files:**
- Create: alembic/versions/0008_authoritative_model_gate.py
- Create: tests/integration/test_authoritative_migration_chain.py
- Create: tests/integration/test_restart_replay.py
- Create: docs/DATA_MODEL_TRACEABILITY.md
- Modify: .github/workflows/ci.yml
- Modify: pyproject.toml
- Modify: tests/integration/conftest.py
- Modify: docs/PROGRESS.md
- Modify: the 2026-08-25 foundation and CSV implementation plans

**Interfaces:**
- Produces immutable-0001-to-0008 migration evidence.
- Produces replay from retained SOURCE_EXPECTED snapshot without live SQL/AIMS/CSV.
- Produces database-verify CI job and entity-to-requirement/test matrix.
- Establishes next CSV revision as 0009 when no intervening migration exists.

- [ ] **Step 1: Write failing migration/replay tests**

~~~python
def test_upgrade_from_0001_to_authoritative_head(
    disposable_database_url: str,
) -> None:
    alembic_downgrade(disposable_database_url, "base")
    alembic_upgrade(disposable_database_url, "0001_operational_state")
    assert schema_fingerprint(disposable_database_url) == load_fixture("0001-schema.json")
    alembic_upgrade(disposable_database_url, "head")
    assert_authoritative_tables_and_constraints(disposable_database_url)

def test_replay_uses_retained_snapshot_not_live_source(
    session: Session,
    source_adapter: NeverCallSourceAdapter,
) -> None:
    original = persisted_execution_with_snapshot(session)
    replay = ReplayService(session, source_adapter).from_execution(original.id)
    assert replay.source_snapshot_hash == original.source_snapshot_hash
    assert source_adapter.call_count == 0
~~~

The 0001 fingerprint includes only table/column/constraint names.

- [ ] **Step 2: Run and confirm red**

Expected: missing 0008 gate, replay service/fixture, DB CI job, and traceability matrix.

- [ ] **Step 3: Implement 0008 preflight**

Before constraints, abort if any execution/schedule lacks configuration_version_id, hashes have wrong length, actions have unknown states, or selected promotion references a candidate outside its evaluation. Insert no fake configuration.

After preflight, make config FKs non-null, add architecture 5.9 indexes/checks, preserve RESTRICT. downgrade removes only 0008 indexes/constraints.

- [ ] **Step 4: Implement replay and database CI**

ReplayService creates a linked replay execution from retained SOURCE_EXPECTED snapshots and identical config/rule versions. It refuses purged evidence or unresolved reconciliation.

Make pywin32 conditional on sys_platform == win32. Add Ubuntu database-verify with PostgreSQL 17 service, Python 3.12, ephemeral local credentials, Alembic upgrade and integration tests. Keep Windows verify unchanged.

- [ ] **Step 5: Add traceability/supersession**

DATA_MODEL_TRACEABILITY.md maps all 21 entities to migration/model/domain/repository/API paths, FR/NFR/BR, issue, and test. Mark compatibility_delivery planned/blocked by #30.

Add dated notices to older plans; do not rewrite completed history. Foundation notice names Task 4/6/7 clauses replaced. CSV notice starts persistence after 0008 using the models package and next revision, expected 0009.

- [ ] **Step 6: Run complete gate**

~~~powershell
$env:ESL_DATABASE_URL=$env:ESL_TEST_DATABASE_URL
python -m alembic downgrade base
python -m alembic upgrade 0001_operational_state
python -m pytest tests/integration/test_authoritative_migration_chain.py -v
python -m alembic upgrade head
python -m pytest tests/integration/test_restart_replay.py -v
python -m ruff check src tests
python -m mypy src
python -m pytest -q
Set-Location frontend
npm ci
npm run typecheck
npm run test -- --run
npm run build
Set-Location ..
git diff --check
~~~

Expected: all pass with no production/external dependency contact.

- [ ] **Step 7: Submit #21**

~~~powershell
git add alembic/versions/0008_authoritative_model_gate.py src tests .github/workflows/ci.yml pyproject.toml docs
git commit -m "test(persistence): enforce authoritative model gate (#21)"
~~~

Require Windows verify and database-verify, merge after review, fast-forward develop, checkpoint merge SHA.

## Deferred compatibility entity

compatibility_delivery is implemented by Task 2 of the automated CSV plan under blocked #30. Before consumer acceptance it is absent and disabled. After acceptance use the next revision after 0008, expected 0009, with delivery ID, contract version, target reference, manifest/content hash, row count, publish/ack states/times, consumer reference, retention deadline. File contents remain non-authoritative.

## Self-Review

### Specification coverage

- FR-002/004/005/026/027 and BR-004/015/018: Tasks 1–2.
- FR-007–017: Tasks 3, 6, 8.
- FR-003/006 and BR-005–019 evidence without invented policy: Tasks 4–5.
- FR-021/022/025 and NFR-007: Task 7.
- NFR-002/005/006/009/010/011/012/014: constraints plus Tasks 3, 6–8.
- FR-028 compatibility_delivery: existing CSV plan after Task 8 and consumer acceptance.
- API/frontend drift: Task 7 creates Pydantic reads; authenticated routes/generated TypeScript remain #26/#29 and may not expose unrestricted payloads.

### Placeholder scan

No unresolved scaffolding marker, vague error-handling instruction, invented duration, or invented promotion policy exists. UNKNOWN / NEEDS-DISCOVERY is a deliberate blocking classification.

### Type consistency

CanonicalKey, CanonicalEslRecord, PromotionEvaluationEvidence, RecordProcessingEvidence, NewRecordAction, ReconciliationCounts and repository names are introduced before use. Revisions are unique 0002–0008; compatibility delivery receives the next revision.

## Execution Handoff

Execute only in a separate implementation task, one existing issue/worktree/PR at a time in the listed order. Recommended: subagent-driven development with review between tasks. Inline execution is available only in that implementation task. This preparation task writes plans only.
