# Automated CSV Compatibility Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Sequencing notice (2026-08-28):** This plan remains blocked by consumer acceptance under issue #30. Its persistence task follows `docs/superpowers/plans/2026-08-28-authoritative-data-model-implementation-plan.md`, uses the new persistence models package, and takes the next Alembic revision after `0008_authoritative_model_gate.py` (expected `0009_compatibility_delivery.py`).

**Goal:** Implement an automated, durable, acknowledgement-based SKU CSV compatibility adapter without using CSV as workflow state or exposing a new HTTP API.

**Architecture:** Persist one immutable delivery record before atomically publishing a legacy-compatible CSV and ready manifest to an ACL-restricted outbox. A scheduler-owned poller validates atomic acknowledgement files and records `ACKNOWLEDGED` or `UNRESOLVED` outcomes in PostgreSQL; it never treats file presence as completion and never blindly resends. Keep the adapter disabled until the consumer owner validates the contract in non-production.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, PostgreSQL, pytest, Windows filesystem/NTFS ACLs, and standard-library `csv`, `hashlib`, `json`, `os`, and `pathlib`.

**Spec:** `docs/superpowers/specs/2026-08-25-csv-compatibility-delivery-contract-design.md`

## Global Constraints

- Complete foundation-plan Tasks 3, 6, and 7 before starting this plan; it consumes their repository, workflow, scheduler, and operations interfaces.
- Preserve the verified legacy payload: UTF-8, comma delimiter, DOS `CRLF` endings, no header, and the exact 42-column order in Task 1.
- CSV, manifest, and acknowledgement files are transport artifacts only; PostgreSQL owns workflow, audit, and recovery state.
- Publish and acknowledge automatically under the scheduler. Normal operation has no human file-handling step.
- The file contract has no HTTP surface. FR-029 authentication for the internal operations API remains unchanged.
- Use distinct non-interactive Windows identities and validate NTFS ACL boundaries before readiness.
- Persist intent before publication. Accept only acknowledgements whose delivery ID, SHA-256, row count, and contract version match durable state.
- Rejected, malformed, mismatched, or timed-out acknowledgement becomes `UNRESOLVED`; never blindly resend.
- Keep paths, SIDs, poll interval, timeout, retention, and enabled state in environment-specific configuration. Never commit host values or credentials.
- Keep the adapter disabled by default and outside production until the consumer acceptance gate passes.
- Cite FR-021, FR-022, FR-027, FR-028, FR-029, NFR-006, NFR-007, NFR-009, or NFR-012 in each relevant test.

---

### Task 1: Define the versioned transport contract

**Files:**

- Create: `src/esl_service/application/file_delivery.py`
- Create: `tests/unit/test_file_delivery_contract.py`

**Interfaces:**

- Produces: `CSV_CONTRACT_VERSION`, `LEGACY_SKU_COLUMNS`, `DeliveryState`, `AckStatus`, `CsvConsumerContract`, `DeliveryManifest`, `DeliveryAcknowledgement`, `FileDeliveryProtocol`, `CsvDeliveryDisabled`, and `CsvDeliveryContractError`.

- [ ] **Step 1: Write failing contract tests**

```python
# FR-028, NFR-012
def test_contract_is_disabled_by_default(tmp_path: Path) -> None:
    assert CsvConsumerContract(delivery_root=tmp_path).enabled is False


def test_legacy_columns_preserve_verified_hop_order() -> None:
    assert LEGACY_SKU_COLUMNS == EXPECTED_LEGACY_SKU_COLUMNS


def test_rejected_acknowledgement_requires_safe_reason() -> None:
    with pytest.raises(CsvDeliveryContractError, match="reason_code"):
        DeliveryAcknowledgement(
            delivery_id=uuid4(), payload_sha256="a" * 64, row_count=1,
            contract_version="sku-csv-v1", status=AckStatus.REJECTED,
            consumer_timestamp=datetime.now(UTC), reason_code=None,
        )
```

Define the test literal:

```python
EXPECTED_LEGACY_SKU_COLUMNS = (
    "STORE_CODE", "ITEM_CODE", "BARCODE", "ITEM_NAME", "ITEM_SHORTNAME",
    "SALES_PRICE", "DISC_PRICE", "DISC_PERCENT", "DISC_TEXT", "MEMBER_PRICE",
    "SOH", "EARLY_EXPIRY_DATE", "PROD_WEIGHT", "MIN_QTY", "MAX_QTY",
    "PRODUCT_URL", "DIVISION", "DEPARTMENT", "CLASS", "SUBCLASS", "BRAND",
    "CLASS_ROTATION", "NFC_URL", "CONSIGMENT", "RETURNABLE", "EXPIRY_DAYS",
    "DISPLAY_QTY", "LAST_UPDATED_DATE", "SYNC_REC", "UOM", "PROMO_FLAG",
    "PER_GRM_PROMO_PRICE", "PER_GRM_SELL_PRICE", "PROMOTION_TYPE",
    "CAMPAIGN_GROUP", "REDLIST", "SAVE_AMT", "CREATED_DATE",
    "PROMO_START_DATE", "PROMO_END_DATE", "PROMO_START_TIME", "PROMO_END_TIME",
)
```

- [ ] **Step 2: Verify the expected failure**

Run: `python -m pytest tests/unit/test_file_delivery_contract.py -v`

Expected: FAIL because `esl_service.application.file_delivery` does not exist.

- [ ] **Step 3: Implement immutable validated types**

```python
CSV_CONTRACT_VERSION = "sku-csv-v1"
LEGACY_SKU_COLUMNS = (
    "STORE_CODE", "ITEM_CODE", "BARCODE", "ITEM_NAME", "ITEM_SHORTNAME",
    "SALES_PRICE", "DISC_PRICE", "DISC_PERCENT", "DISC_TEXT", "MEMBER_PRICE",
    "SOH", "EARLY_EXPIRY_DATE", "PROD_WEIGHT", "MIN_QTY", "MAX_QTY",
    "PRODUCT_URL", "DIVISION", "DEPARTMENT", "CLASS", "SUBCLASS", "BRAND",
    "CLASS_ROTATION", "NFC_URL", "CONSIGMENT", "RETURNABLE", "EXPIRY_DAYS",
    "DISPLAY_QTY", "LAST_UPDATED_DATE", "SYNC_REC", "UOM", "PROMO_FLAG",
    "PER_GRM_PROMO_PRICE", "PER_GRM_SELL_PRICE", "PROMOTION_TYPE",
    "CAMPAIGN_GROUP", "REDLIST", "SAVE_AMT", "CREATED_DATE",
    "PROMO_START_DATE", "PROMO_END_DATE", "PROMO_START_TIME", "PROMO_END_TIME",
)

class DeliveryState(StrEnum):
    INTENDED = "INTENDED"
    PUBLISHED = "PUBLISHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNRESOLVED = "UNRESOLVED"

class AckStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"

@dataclass(frozen=True)
class CsvConsumerContract:
    delivery_root: Path
    contract_version: str = CSV_CONTRACT_VERSION
    poll_interval: timedelta = timedelta(seconds=30)
    acknowledgement_timeout: timedelta = timedelta(minutes=15)
    retention: timedelta = timedelta(days=30)
    enabled: bool = False
```

Copy the literal tuple from the failing test into `LEGACY_SKU_COLUMNS`; do not import the test constant. Implement frozen manifest and acknowledgement dataclasses. Validate non-negative row counts, lowercase 64-character hexadecimal hashes, timezone-aware timestamps, positive durations, and a mandatory safe reason for `REJECTED`.

Define `FileDeliveryProtocol` with read-only `delivery_id`, `payload_sha256`, `row_count`, and `contract_version`. Implement `DeliveryAcknowledgement.matches(delivery)` to compare exactly those four values without importing SQLAlchemy models.

- [ ] **Step 4: Verify green**

Run: `python -m pytest tests/unit/test_file_delivery_contract.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/esl_service/application/file_delivery.py tests/unit/test_file_delivery_contract.py
git commit -m "feat(delivery): define automated CSV contract"
```

### Task 2: Persist delivery lifecycle and idempotency

**Files:**

- Modify: `src/esl_service/persistence/models.py`
- Modify: `src/esl_service/persistence/repository.py`
- Create: `alembic/versions/0002_file_delivery.py`
- Create: `tests/integration/test_file_delivery_repository.py`

**Interfaces:**

- Consumes: foundation Task 3 `WorkflowExecution`, session handling, `DeliveryState`, `DeliveryManifest`, and `DeliveryAcknowledgement`.
- Produces: `FileDelivery` and `ExecutionRepository.create_file_delivery()`, `mark_file_delivery_published()`, `acknowledge_file_delivery()`, `mark_file_delivery_unresolved()`, `list_pending_file_deliveries()`, and `defer_file_delivery_poll()`.

- [ ] **Step 1: Write the failing idempotency test**

```python
# FR-022, FR-028, NFR-006
def test_same_execution_and_payload_is_one_logical_delivery(repository, manifest) -> None:
    execution = repository.create_execution("sku-compat", "084", "2026-08-25T07:00:00Z")
    first = repository.create_file_delivery(execution.id, manifest)
    second = repository.create_file_delivery(execution.id, manifest)
    assert second.delivery_id == first.delivery_id
    assert second.state is DeliveryState.INTENDED
```

- [ ] **Step 2: Verify the expected failure**

Before running, set `ESL_TEST_DATABASE_URL` through the approved secret boundary to the dedicated non-production PostgreSQL database.

Run: `python -m pytest tests/integration/test_file_delivery_repository.py -v`

Expected: FAIL because the schema and repository methods do not exist.

- [ ] **Step 3: Add the model, migration, and transitions**

Create `file_delivery` with UUID primary key, workflow-execution foreign key, store, contract version, payload SHA-256, row count, state, relative CSV/manifest/ack filenames, created/published/acknowledged/deadline/next-poll/files-deleted timestamps, acknowledgement status/reason, and updated timestamp. Add uniqueness on `(execution_id, payload_sha256, contract_version)` plus indexes on `(state, next_poll_at, deadline_at)` and `(execution_id, created_at)`.

Use compare-and-set updates so only `INTENDED -> PUBLISHED` and `PUBLISHED -> ACKNOWLEDGED|UNRESOLVED` succeed. Duplicate creation returns the existing row. State changes append execution events without CSV contents or secrets. `defer_file_delivery_poll()` advances `next_poll_at` without changing state.

```python
def acknowledge_file_delivery(
    self, delivery_id: UUID, acknowledgement: DeliveryAcknowledgement
) -> bool:
    delivery = self.get_file_delivery(delivery_id)
    if not acknowledgement.matches(delivery):
        return self.mark_file_delivery_unresolved(delivery_id, "ACK_MISMATCH")
    return self._transition(delivery_id, DeliveryState.PUBLISHED, DeliveryState.ACKNOWLEDGED)
```

- [ ] **Step 4: Apply migration and verify green**

Run: `python -m alembic upgrade head; python -m pytest tests/integration/test_file_delivery_repository.py -v`

Expected: PASS against the dedicated non-production database.

- [ ] **Step 5: Commit**

```bash
git add src/esl_service/persistence alembic/versions/0002_file_delivery.py tests/integration/test_file_delivery_repository.py
git commit -m "feat(persistence): add durable CSV delivery state"
```

### Task 3: Atomically publish CSV and ready manifest

**Files:**

- Modify: `src/esl_service/adapters/delivery.py`
- Create: `src/esl_service/adapters/atomic_files.py`
- Create: `tests/unit/test_csv_delivery_adapter.py`

**Interfaces:**

- Consumes: `CsvConsumerContract`, `DeliveryManifest`, `LEGACY_SKU_COLUMNS`, and Task 2 repository methods.
- Produces: `CsvDeliveryAdapter.publish(execution_id: UUID, store_code: str, rows: Sequence[Mapping[str, object]]) -> UUID`, `atomic_write_bytes(path: Path, payload: bytes)`, and `CsvDeliveryInvalidValue`.

- [ ] **Step 1: Write failing publication tests**

```python
# FR-027, FR-028, NFR-006
def test_publish_writes_csv_then_matching_manifest(tmp_path, repository, legacy_row) -> None:
    delivery_id = enabled_adapter(tmp_path, repository).publish(execution_id, "084", [legacy_row])
    csv_bytes = (tmp_path / "outbox" / f"{delivery_id}.csv").read_bytes()
    manifest = json.loads((tmp_path / "outbox" / f"{delivery_id}.ready.json").read_text("utf-8"))
    assert csv_bytes.endswith(b"\r\n")
    assert not csv_bytes.startswith(b"STORE_CODE")
    assert manifest["payload_sha256"] == hashlib.sha256(csv_bytes).hexdigest()
    assert repository.get_file_delivery(delivery_id).state is DeliveryState.PUBLISHED


def test_publish_rejects_unenclosed_delimiter(tmp_path, repository, legacy_row) -> None:
    invalid = {**legacy_row, "ITEM_NAME": "unsafe,name"}
    with pytest.raises(CsvDeliveryInvalidValue, match="ITEM_NAME"):
        enabled_adapter(tmp_path, repository).publish(execution_id, "084", [invalid])
```

Make `legacy_row` a literal mapping containing all 42 keys; use empty strings for nullable text, zero for numeric values, `"084"` for store, and `"item-1"` for item code.

- [ ] **Step 2: Verify the expected failure**

Run: `python -m pytest tests/unit/test_csv_delivery_adapter.py -v`

Expected: FAIL because atomic publication is absent.

- [ ] **Step 3: Implement exact serialization and publication**

Serialize `LEGACY_SKU_COLUMNS` as UTF-8, comma-separated unquoted values with `\r\n` endings and no header. Reject comma, carriage return, or line feed in any value. Compute SHA-256 over exact CSV bytes.

`atomic_write_bytes()` writes a unique temporary sibling, flushes, calls `os.fsync()`, and uses `os.replace()` for the final name. `publish()` persists `INTENDED`, writes `<delivery_id>.csv`, writes sorted-key `<delivery_id>.ready.json`, and transitions to `PUBLISHED`. When disabled, raise `CsvDeliveryDisabled` before touching repository or filesystem.

```python
def atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
```

- [ ] **Step 4: Verify green**

Run: `python -m pytest tests/unit/test_csv_delivery_adapter.py -v`

Expected: PASS with no temporary files remaining.

- [ ] **Step 5: Commit**

```bash
git add src/esl_service/adapters/delivery.py src/esl_service/adapters/atomic_files.py tests/unit/test_csv_delivery_adapter.py
git commit -m "feat(delivery): atomically publish SKU CSV"
```

### Task 4: Poll and validate acknowledgements automatically

**Files:**

- Create: `src/esl_service/application/delivery_acknowledgement.py`
- Create: `tests/unit/test_delivery_acknowledgement.py`

**Interfaces:**

- Consumes: Task 2 repository methods, `CsvConsumerContract`, `DeliveryAcknowledgement`, and `AckStatus`.
- Produces: `AcknowledgementPoller.poll(now: datetime) -> list[UUID]`.

- [ ] **Step 1: Write failing acknowledgement tests**

```python
# FR-021, FR-022, FR-028
def test_matching_accepted_ack_completes_delivery(tmp_path, repository, published_delivery) -> None:
    write_matching_ack(tmp_path, published_delivery, AckStatus.ACCEPTED)
    assert poller(tmp_path, repository).poll(now) == [published_delivery.delivery_id]
    assert repository.get_file_delivery(published_delivery.delivery_id).state is DeliveryState.ACKNOWLEDGED


def test_missing_ack_after_deadline_is_unresolved(tmp_path, repository, published_delivery) -> None:
    poller(tmp_path, repository).poll(published_delivery.deadline_at + timedelta(seconds=1))
    assert repository.get_file_delivery(published_delivery.delivery_id).state is DeliveryState.UNRESOLVED


@pytest.mark.parametrize("field", ["delivery_id", "payload_sha256", "row_count", "contract_version"])
def test_mismatched_ack_is_unresolved(tmp_path, repository, published_delivery, field) -> None:
    write_mismatched_ack(tmp_path, published_delivery, field)
    poller(tmp_path, repository).poll(now)
    assert repository.get_file_delivery(published_delivery.delivery_id).state is DeliveryState.UNRESOLVED
```

Test helpers must write strict acknowledgement JSON to the final `.ack.json` name using `atomic_write_bytes()` and literal expected fields; they must not call production parsing code.

- [ ] **Step 2: Verify the expected failure**

Run: `python -m pytest tests/unit/test_delivery_acknowledgement.py -v`

Expected: FAIL because `AcknowledgementPoller` does not exist.

- [ ] **Step 3: Implement strict polling**

For each `PUBLISHED` row with `next_poll_at <= now`, read only final `<delivery_id>.ack.json`. Reject unknown JSON fields. Validate status, timezone-aware timestamp, delivery ID, hash, row count, and version. Matching `ACCEPTED` becomes acknowledged. `REJECTED`, malformed, mismatched, or expired becomes unresolved with a safe reason event. Missing before deadline remains published and defers `next_poll_at` by the configured interval.

```python
def poll(self, now: datetime) -> list[UUID]:
    changed: list[UUID] = []
    for delivery in self.repository.list_pending_file_deliveries(now):
        path = self.contract.delivery_root / "ack" / f"{delivery.delivery_id}.ack.json"
        if path.exists():
            self._apply(delivery, self._load_strict(path))
            changed.append(delivery.delivery_id)
        elif now > delivery.deadline_at:
            self.repository.mark_file_delivery_unresolved(delivery.delivery_id, "ACK_TIMEOUT")
            changed.append(delivery.delivery_id)
        else:
            self.repository.defer_file_delivery_poll(
                delivery.delivery_id, now + self.contract.poll_interval
            )
    return changed
```

- [ ] **Step 4: Verify green**

Run: `python -m pytest tests/unit/test_delivery_acknowledgement.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/esl_service/application/delivery_acknowledgement.py tests/unit/test_delivery_acknowledgement.py
git commit -m "feat(delivery): validate automatic acknowledgements"
```

### Task 5: Validate ACL configuration and integrate scheduler polling

**Files:**

- Modify: `src/esl_service/config.py`
- Modify: `src/esl_service/runtime/scheduler.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_scheduler.py`

**Interfaces:**

- Consumes: Task 1 contract, foundation Task 2 Windows ACL/identity readers, Task 4 poller, and foundation Task 7 scheduler.
- Produces: validated `Settings.csv_delivery_*` fields and scheduler-owned acknowledgement polling on each non-paused tick.

- [ ] **Step 1: Write failing configuration and scheduler tests**

```python
# NFR-009, NFR-012
def test_enabled_delivery_requires_distinct_identities(settings_data) -> None:
    settings_data.update(csv_delivery_enabled=True, csv_producer_sid="S-1-5-80-1", csv_consumer_sid="S-1-5-80-1")
    with pytest.raises(ValidationError, match="distinct"):
        Settings.model_validate(settings_data)


# FR-028
def test_scheduler_polls_acknowledgements_automatically(now, scheduler, acknowledgement_poller) -> None:
    scheduler.tick(now)
    acknowledgement_poller.poll.assert_called_once_with(now)


def test_paused_scheduler_does_not_poll(now, scheduler, acknowledgement_poller) -> None:
    scheduler.pause()
    scheduler.tick(now)
    acknowledgement_poller.poll.assert_not_called()
```

- [ ] **Step 2: Verify the expected failure**

Run: `python -m pytest tests/unit/test_config.py tests/unit/test_scheduler.py -v`

Expected: FAIL because CSV settings and polling integration are absent.

- [ ] **Step 3: Implement configuration and lifecycle integration**

Add disabled-by-default settings for delivery root, version, producer SID, consumer SID, poll seconds, acknowledgement-timeout seconds, retention days, and enabled state. When enabled, require an absolute non-repository path, distinct canonical SIDs, positive timing values, timeout longer than poll interval, and ACL validation matching the approved producer/consumer boundaries. Error logs name the setting key but not configured path or SID values.

Inject the poller into `Scheduler`. A non-paused tick polls acknowledgements before claiming new executions; a paused scheduler does neither. Poll failure appends a dependency/audit event and must not create a duplicate delivery.

- [ ] **Step 4: Verify green and regressions**

Run: `python -m pytest tests/unit/test_config.py tests/unit/test_scheduler.py tests/unit/test_delivery_acknowledgement.py -v; python -m ruff check src tests; python -m mypy src`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/esl_service/config.py src/esl_service/runtime/scheduler.py tests/unit/test_config.py tests/unit/test_scheduler.py
git commit -m "feat(runtime): automate CSV acknowledgement polling"
```

### Task 6: Add retention, operations, and non-production acceptance

**Files:**

- Create: `src/esl_service/application/file_delivery_retention.py`
- Modify: `src/esl_service/persistence/repository.py`
- Create: `tests/unit/test_file_delivery_retention.py`
- Modify: `docs/WORKFLOW.md`
- Modify: `docs/PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-25-csv-compatibility-delivery-contract-design.md`

**Interfaces:**

- Produces: `ExecutionRepository.list_file_deliveries_for_cleanup()`, `mark_file_delivery_files_deleted()`, and `FileDeliveryRetentionService.clean(now: datetime) -> list[UUID]`.

- [ ] **Step 1: Write failing retention tests**

```python
# FR-022, NFR-007
def test_cleanup_removes_only_expired_acknowledged_files(tmp_path, repository) -> None:
    removed = retention_service(tmp_path, repository).clean(now)
    assert removed == [expired_acknowledged.delivery_id]
    assert not csv_path(expired_acknowledged).exists()
    assert csv_path(unresolved_delivery).exists()


def test_cleanup_preserves_durable_audit(tmp_path, repository) -> None:
    retention_service(tmp_path, repository).clean(now)
    delivery = repository.get_file_delivery(expired_acknowledged.delivery_id)
    assert delivery.files_deleted_at == now
    assert delivery.state is DeliveryState.ACKNOWLEDGED
```

- [ ] **Step 2: Verify the expected failure**

Run: `python -m pytest tests/unit/test_file_delivery_retention.py -v`

Expected: FAIL because retention cleanup is absent.

- [ ] **Step 3: Implement cleanup and procedures**

Query only acknowledged deliveries older than retention with null `files_deleted_at`. Delete CSV, manifest, and acknowledgement files, then persist `files_deleted_at` and append an event. Repeated cleanup is idempotent. Never automatically delete unresolved artifacts.

Update `docs/WORKFLOW.md` with automatic publish/poll, readiness, mismatch/timeout handling, restart recovery, and rollback that disables publication while preserving state and unresolved files. Mark the design `APPROVED — implementation planned`. Update `docs/PROGRESS.md` with issue, branch, commit, exact checks, configuration variable names only, non-production filesystem scope, and next action.

- [ ] **Step 4: Run full checks and controlled acceptance**

Run: `python -m pytest -v; python -m ruff check src tests; python -m mypy src`

Run a non-production fixture using ACL-restricted temporary outbox/ack directories and a fake consumer that automatically writes a matching acknowledgement. Verify atomic publication, no partial consumption, automatic acknowledgement, restart-resumed polling, mismatch/timeout unresolved behavior, and unresolved-file retention. Do not use production paths or AIMS/ESL endpoints.

Expected: all checks pass; accepted reaches `ACKNOWLEDGED`, mismatch and timeout reach `UNRESOLVED`, and no production side effect occurs.

- [ ] **Step 5: Commit**

```bash
git add src/esl_service/application/file_delivery_retention.py src/esl_service/persistence/repository.py tests/unit/test_file_delivery_retention.py docs/WORKFLOW.md docs/PROGRESS.md docs/superpowers/specs/2026-08-25-csv-compatibility-delivery-contract-design.md
git commit -m "feat(delivery): complete automated CSV lifecycle"
```

## Deferred production gate

Production enablement is outside this plan. It requires a named consumer owner to validate field semantics, acknowledgement writer, service identities/ACLs, timeout, retention, rollback, and staging evidence. Until approved, `csv_delivery_enabled` remains false and shadow runs record intended actions only.

## Self-Review

### Specification coverage

Task 1 defines the exact contract and payload. Task 2 makes lifecycle/idempotency durable. Task 3 provides atomic publication. Task 4 validates automatic acknowledgements and timeouts. Task 5 validates configuration/ACLs and scheduler integration without adding HTTP. Task 6 adds safe retention, operations, traceability, and non-production acceptance. Together they cover the approved design and FR-021, FR-022, FR-027, FR-028, FR-029, NFR-006, NFR-007, NFR-009, and NFR-012.

### Placeholder scan

The plan has no unfinished code steps or unspecified behaviors. Production paths, SIDs, and secrets are intentionally supplied only through validated environment configuration.

### Type consistency

`CSV_CONTRACT_VERSION`, `LEGACY_SKU_COLUMNS`, `CsvConsumerContract`, `DeliveryState`, `AckStatus`, `DeliveryManifest`, `DeliveryAcknowledgement`, `FileDelivery`, `CsvDeliveryAdapter`, `AcknowledgementPoller`, and `FileDeliveryRetentionService` are introduced before use and retain the same names and roles.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-25-automated-csv-compatibility-delivery-plan.md`.

Execution is deferred until foundation Tasks 3, 6, and 7 and the consumer acceptance gate are complete. Then choose:

1. **Subagent-Driven (recommended)** — fresh implementer and review per task.
2. **Inline Execution** — one executing-plans session with checkpoints and review gates.
