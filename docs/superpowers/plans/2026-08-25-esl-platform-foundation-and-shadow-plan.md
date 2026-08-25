# ESL Platform Foundation and Shadow Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the internal Python web application foundation that safely observes ESL source/AIMS state, persists durable workflow evidence, and executes shadow workflows without external side effects.

**Architecture:** A React + TypeScript + Vite + Tailwind browser UI calls a FastAPI internal API; the CLI shares FastAPI's application layer rather than the browser interface. Google Stitch exports are versioned visual/source handoffs converted into reviewed React components, never treated as deployable backend or data-access code. PostgreSQL owns schedules, execution state, scope leases, per-record outcomes, reconciliation, and queryable structured event logs. SQL Server and AIMS stay behind adapters; shadow mode uses read-only access and a dry-run page-action adapter.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2, Alembic, Pydantic Settings, psycopg, pyodbc, httpx, Typer, structlog, pywin32, pytest, Ruff, mypy, React, TypeScript, Vite, Tailwind CSS, Vitest, GitHub Actions, PostgreSQL.

**Spec:** `docs/SPECIFICATION.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/WORKFLOW.md`, `docs/PROGRESS.md`

## Global Constraints

- Provide an internal browser UI/API plus CLI; support Windows Service and command-line execution (FR-029).
- Build the browser UI in React + TypeScript + Tailwind; it may call only authenticated FastAPI endpoints (FR-030).
- Treat Stitch exports as versioned handoff evidence. Do not copy generated sample data, direct data access, credentials, or unreviewed production code from a design export.
- PostgreSQL is the target state, audit, reconciliation, and queryable event-log database (NFR-007).
- Never write to SQL Server or an AIMS database. AIMS actions use only its documented HTTP API.
- Shadow mode sends no page-change request and records actions as `INTENDED`.
- Compatibility reads use least-privilege read-only AIMS PostgreSQL access.
- Persist canonical hashes/diffs and action state; do not use CSV as workflow state, audit, or parity evidence.
- Disable CSV delivery unless an approved consumer contract exists.
- Retain secrets outside source control in an ACL-restricted DPAPI-protected bundle.
- Build artifacts in GitHub Actions and transfer them through the approved controlled process.
- BR-005 promotion precedence is **ON HOLD**. Do not implement promotion-winner selection or production promotion actions.

## File Structure

```text
pyproject.toml                               Project tooling
frontend/                                    React + TypeScript + Vite + Tailwind browser UI
docs/ui/STITCH_HANDOFF.md                    Stitch screen/version and API mapping record
src/esl_service/config.py                    Settings and DPAPI secret boundary
src/esl_service/domain/models.py             Canonical records
src/esl_service/domain/diff.py               Stable hashes and field differences
src/esl_service/domain/promotion.py          BR-005 hold boundary
src/esl_service/application/contracts.py     Adapter ports and result types
src/esl_service/application/workflows.py     Shadow workflow
src/esl_service/application/reconcile.py     Reconciliation report
src/esl_service/adapters/sqlserver.py        Read-only SQL Server reader
src/esl_service/adapters/aims_read.py        Read-only AIMS PostgreSQL reader
src/esl_service/adapters/aims_api.py         AIMS HTTP and dry-run page clients
src/esl_service/adapters/delivery.py         Guarded CSV delivery
src/esl_service/persistence/models.py        SQLAlchemy operational tables
src/esl_service/persistence/repository.py    State, lease, event, action access
src/esl_service/runtime/scheduler.py         Durable scheduling and pause/resume
src/esl_service/runtime/windows_service.py   Windows Service host
src/esl_service/web/routes.py                Internal operations routes
src/esl_service/main.py                      Application factory
src/esl_service/cli.py                       CLI
alembic/                                     PostgreSQL migration files
tests/unit/                                  Unit tests
tests/integration/                           Repository and adapter tests
scripts/install-service.ps1                  Service installation
scripts/deploy-artifact.ps1                  Controlled deployment
docs/BASELINE_COLLECTION.md                  Shadow baseline procedure
.github/workflows/ci.yml                     CI
```

### Task 1: Scaffold the project and CI

**Files:**
- Create: `pyproject.toml`, `src/esl_service/__init__.py`, `tests/unit/test_project_import.py`, `.github/workflows/ci.yml`
- Create: `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/app.test.tsx`

**Interfaces:**
- Produces the importable `esl_service` package, the React/Vite UI shell, and Python/frontend checks.

- [ ] **Step 1: Write the failing test**

```python
def test_package_imports() -> None:
    import esl_service
    assert esl_service.__name__ == "esl_service"
```

```tsx
it("shows the ESL operations application title", () => {
  render(<App />)
  expect(screen.getByRole("heading", { name: "ESL Operations" })).toBeInTheDocument()
})
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/unit/test_project_import.py -v` and, in `frontend/`, `npm run test -- --run src/app.test.tsx`

Expected: the Python test fails because the package does not exist; the frontend test fails because the Vite/React test configuration and application shell do not exist.

- [ ] **Step 3: Add project configuration and CI**

```toml
[project]
name = "solum-aims-esl-service"
requires-python = ">=3.12"
dependencies = ["fastapi", "uvicorn[standard]", "sqlalchemy", "alembic", "pydantic-settings", "psycopg[binary]", "pyodbc", "httpx", "typer", "structlog", "pywin32"]
[project.optional-dependencies]
dev = ["pytest", "ruff", "mypy", "respx"]
```

```yaml
name: ci
on: [push, pull_request]
jobs:
  verify:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - run: python -m pip install -e ".[dev]"
      - run: npm ci
        working-directory: frontend
      - run: python -m ruff check src tests
      - run: python -m mypy src
      - run: python -m pytest -v
      - run: npm run typecheck
        working-directory: frontend
      - run: npm run test -- --run
        working-directory: frontend
      - run: npm run build
        working-directory: frontend
```

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest -v; python -m ruff check src tests; python -m mypy src; npm run typecheck; npm run test -- --run; npm run build` (the last three commands run in `frontend/`).

Expected: PASS.

```bash
git add pyproject.toml src tests frontend .github/workflows/ci.yml
git commit -m "chore: scaffold ESL service"
```

### Task 2: Add settings and secrets boundary

**Files:**
- Create: `src/esl_service/config.py`, `src/esl_service/runtime/secrets.py`, `tests/unit/test_config.py`, `tests/unit/test_secrets.py`

**Interfaces:**
- Produces `Settings`, `SecretProvider`, and `DpapiSecretProvider`.

- [ ] **Step 1: Write the failing test**

```python
from esl_service.config import Settings

def test_production_requires_internal_host() -> None:
    Settings.model_validate({"environment": "production", "database_url": "postgresql://state", "internal_host": ""})
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/unit/test_config.py -v`

Expected: FAIL because `Settings` is absent.

- [ ] **Step 3: Implement the settings and secret interface**

```python
class SecretProvider(Protocol):
    def get(self, name: str) -> str: ...

class Settings(BaseSettings):
    environment: Literal["development", "staging", "production"]
    database_url: str
    internal_host: str
    shadow_mode: bool = True
```

Implement `DpapiSecretProvider.get(name)` with `win32crypt.CryptUnprotectData`. It reads the encrypted bundle path from settings, returns only the requested value, and does not log secret values.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/unit/test_config.py tests/unit/test_secrets.py -v`

Expected: PASS.

```bash
git add src/esl_service/config.py src/esl_service/runtime/secrets.py tests/unit
git commit -m "feat: add settings and DPAPI secret boundary"
```

### Task 3: Create durable state, audit, and scope leases

**Files:**
- Create: `src/esl_service/persistence/models.py`, `src/esl_service/persistence/db.py`, `src/esl_service/persistence/repository.py`
- Create: `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_operational_state.py`
- Create: `tests/integration/test_repository.py`

**Interfaces:**
- Produces `ExecutionRepository.create_execution()`, `claim_scope()`, `append_event()`, `record_action()`, and `list_events()`.
- Produces `workflow_execution`, `scope_lease`, `execution_event`, `record_action`, and `workflow_schedule` tables.

- [ ] **Step 1: Write the failing lease test**

```python
def test_only_one_execution_claims_store_scope(repository) -> None:
    first = repository.create_execution("sku-shadow", "084", "2026-08-25T07:00:00Z")
    assert repository.claim_scope(first.id, "sku-shadow:084") is True
    second = repository.create_execution("sku-shadow", "084", "2026-08-25T07:00:00Z")
    assert repository.claim_scope(second.id, "sku-shadow:084") is False
```

- [ ] **Step 2: Run it**

Run: `TEST_DATABASE_URL=<approved-staging-postgresql-url> python -m pytest tests/integration/test_repository.py -v`

Expected: FAIL because the state schema is absent.

- [ ] **Step 3: Implement tables, migration, and repository**

```python
def claim_scope(self, execution_id: UUID, scope_key: str) -> bool:
    result = self.session.execute(
        insert(ScopeLease).values(scope_key=scope_key, execution_id=execution_id).on_conflict_do_nothing()
    )
    return result.rowcount == 1

def append_event(self, execution_id: UUID, event_type: str, payload: Mapping[str, object]) -> None:
    self.session.add(ExecutionEvent(execution_id=execution_id, event_type=event_type, payload=dict(payload)))
```

Add indexes for `scope_key`, `(execution_id, occurred_at)`, and `(workflow_name, store_code, started_at)`.

- [ ] **Step 4: Verify and commit**

Run: `alembic upgrade head; TEST_DATABASE_URL=<approved-staging-postgresql-url> python -m pytest tests/integration/test_repository.py -v`

Expected: PASS.

```bash
git add src/esl_service/persistence alembic alembic.ini tests/integration/test_repository.py
git commit -m "feat: add durable workflow state and audit"
```

### Task 4: Add canonical diffing and BR-005 hold boundary

**Files:**
- Create: `src/esl_service/domain/models.py`, `src/esl_service/domain/diff.py`, `src/esl_service/domain/promotion.py`
- Create: `tests/unit/test_diff.py`, `tests/unit/test_promotion_hold.py`

**Interfaces:**
- Produces `CanonicalEslRecord`, `canonical_hash()`, `diff_records()`, `PromotionCandidate`, and `PromotionSelectionOnHold`.

- [ ] **Step 1: Write failing domain tests**

```python
def test_hash_is_stable_when_mapping_order_changes() -> None:
    assert canonical_hash({"item_code": "1", "price": 5000}) == canonical_hash({"price": 5000, "item_code": "1"})

def test_promotion_winner_is_held() -> None:
    with pytest.raises(PromotionSelectionOnHold):
        select_promotion([PromotionCandidate("A"), PromotionCandidate("B")])
```

- [ ] **Step 2: Run them**

Run: `python -m pytest tests/unit/test_diff.py tests/unit/test_promotion_hold.py -v`

Expected: FAIL because domain modules are absent.

- [ ] **Step 3: Implement pure functions**

```python
def canonical_hash(value: Mapping[str, JSONValue]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def select_promotion(candidates: Sequence[PromotionCandidate]) -> PromotionCandidate:
    raise PromotionSelectionOnHold("BR-005 promotion precedence is on hold")
```

Define `JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]`, `CanonicalEslRecord` as a frozen dataclass keyed by `store_code` and `item_code`, and `PromotionCandidate(campaign_id: str)` as a frozen dataclass before these functions.

Model `source_price_per_kg` and `display_price_per_100gr`; add a test for `50_000` per kilogram becoming `5_000` per 100 grams.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/unit/test_diff.py tests/unit/test_promotion_hold.py -v`

Expected: PASS.

```bash
git add src/esl_service/domain tests/unit/test_diff.py tests/unit/test_promotion_hold.py
git commit -m "feat: add canonical diffing and promotion hold"
```

### Task 5: Implement source and AIMS adapters

**Files:**
- Create: `src/esl_service/application/contracts.py`
- Create: `src/esl_service/adapters/sqlserver.py`, `src/esl_service/adapters/aims_read.py`, `src/esl_service/adapters/aims_api.py`
- Create: `tests/unit/test_aims_api.py`, `tests/integration/test_sqlserver_adapter.py`

**Interfaces:**
- Produces `EslSourceReader.fetch_snapshot(store_code, window)`, `AimsReadModelReader.fetch_labels(store_code)`, and `AimsPageClient.change_pages(store_code, changes, idempotency_key)`.
- Produces `PageChangeReceipt(response_code, response_message, custom_batch_id)`.

- [ ] **Step 1: Write the failing API-contract test**

```python
def test_page_change_posts_documented_payload(respx_mock) -> None:
    respx_mock.post("http://aims/dashboardservice/common/labels/page?store=084").respond(
        200, json={"responseCode": "0", "responseMessage": "OK", "customBatchId": "batch-1"}
    )
    receipt = client.change_pages("084", [PageChange("label-1", 3)], "key-1")
    assert receipt.custom_batch_id == "batch-1"
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/unit/test_aims_api.py -v`

Expected: FAIL because adapter modules are absent.

- [ ] **Step 3: Implement adapters and a dry-run page client**

```python
async def change_pages(self, store_code: str, changes: Sequence[PageChange], idempotency_key: str) -> PageChangeReceipt:
    payload = {"pageChangeList": [{"labelCode": c.label_code, "page": c.page} for c in changes]}
    response = await self.client.post("/common/labels/page", params={"store": store_code}, json=payload)
    response.raise_for_status()
    body = response.json()
    return PageChangeReceipt(body["responseCode"], body["responseMessage"], body.get("customBatchId"))
```

Define the adapter contract before this method:

```python
@dataclass(frozen=True)
class PageChange:
    label_code: str
    page: int

@dataclass(frozen=True)
class PageChangeReceipt:
    response_code: str
    response_message: str
    custom_batch_id: str | None
```

Use parameterized SQL only. Give SQL Server and AIMS PostgreSQL reader identities no write privilege. `DryRunAimsPageClient` records intended actions and never opens HTTP.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/unit/test_aims_api.py tests/integration/test_sqlserver_adapter.py -v`

Expected: PASS against approved non-production endpoints.

```bash
git add src/esl_service/application/contracts.py src/esl_service/adapters tests
git commit -m "feat: add source and AIMS adapters"
```

### Task 6: Build shadow workflow, reconciliation, and CSV guard

**Files:**
- Create: `src/esl_service/application/workflows.py`, `src/esl_service/application/reconcile.py`, `src/esl_service/adapters/delivery.py`
- Create: `tests/unit/test_shadow_workflow.py`, `tests/unit/test_reconcile.py`

**Interfaces:**
- Produces `ShadowWorkflow.run(request) -> ExecutionSummary` and `reconcile(summary) -> ReconciliationReport`.
- Produces `CsvDeliveryAdapter.deliver()`, requiring `CsvConsumerContract`.

- [ ] **Step 1: Write failing no-side-effect tests**

```python
def test_shadow_run_records_intended_action_without_http_call(fake_source, dry_run_client, repository) -> None:
    summary = ShadowWorkflow(fake_source, dry_run_client, repository).run(ShadowRequest("084", window))
    assert summary.intended_actions == 1
    assert dry_run_client.http_call_count == 0

def test_csv_delivery_requires_contract() -> None:
    with pytest.raises(CsvDeliveryDisabled):
        CsvDeliveryAdapter(None).deliver(payload)
```

- [ ] **Step 2: Run them**

Run: `python -m pytest tests/unit/test_shadow_workflow.py tests/unit/test_reconcile.py -v`

Expected: FAIL because workflow modules are absent.

- [ ] **Step 3: Implement orchestration**

```python
def reconcile(summary: ExecutionSummary) -> ReconciliationReport:
    unresolved = summary.eligible - summary.skipped_idempotent - summary.acknowledged - summary.rejected_by_aims - summary.failed
    return ReconciliationReport(extracted=summary.extracted, valid=summary.valid, unresolved=unresolved)
```

Define `ExecutionSummary` with integer fields `extracted`, `valid`, `eligible`, `skipped_idempotent`, `acknowledged`, `rejected_by_aims`, `failed`, and `intended_actions`. Define `ReconciliationReport(extracted: int, valid: int, unresolved: int)`. Define `ShadowRequest(store_code: str, window: ProcessingWindow)` before `ShadowWorkflow.run()`.

Persist state transitions and canonical hashes/diffs. Shadow actions are always `INTENDED`, never `SUBMITTED`.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/unit/test_shadow_workflow.py tests/unit/test_reconcile.py -v`

Expected: PASS.

```bash
git add src/esl_service/application src/esl_service/adapters/delivery.py tests/unit
git commit -m "feat: add shadow workflow and reconciliation"
```

### Task 7: Add durable scheduler, FastAPI operations API, React operations UI, CLI, and Windows Service host

**Files:**
- Create: `src/esl_service/runtime/scheduler.py`, `src/esl_service/runtime/windows_service.py`
- Create: `src/esl_service/main.py`, `src/esl_service/web/routes.py`, `src/esl_service/cli.py`
- Create: `frontend/src/api/client.ts`, `frontend/src/features/operations/OperationsDashboard.tsx`, `frontend/src/features/operations/OperationsDashboard.test.tsx`
- Create: `docs/ui/STITCH_HANDOFF.md`
- Create: `tests/unit/test_scheduler.py`, `tests/unit/test_routes.py`, `tests/unit/test_cli.py`
- Create: `scripts/install-service.ps1`

**Interfaces:**
- Produces `Scheduler.tick(now)`, `pause()`, `resume()`, `create_app()`, and CLI `status`, `runs show`, `runs shadow`.
- Produces `GET /health/live`, `GET /health/ready`, `GET /runs/{execution_id}`, `POST /runs/shadow`, and schedule pause/resume routes; the React dashboard uses only those typed API client calls.

- [ ] **Step 1: Write failing scheduler, route, and CLI tests**

```python
def test_paused_scheduler_claims_no_execution(scheduler) -> None:
    scheduler.pause()
    assert scheduler.tick(now) == []

def test_shadow_endpoint_requires_operator(client) -> None:
    assert client.post("/runs/shadow", json={"workflow": "sku-shadow", "store": "084"}).status_code == 401

def test_status_command_prints_readiness(runner) -> None:
    assert runner.invoke(app, ["status"]).exit_code == 0
```

- [ ] **Step 2: Run them**

Run: `python -m pytest tests/unit/test_scheduler.py tests/unit/test_routes.py tests/unit/test_cli.py -v`

Expected: FAIL because runtime and operations modules are absent.

- [ ] **Step 3: Implement lifecycle-safe scheduling and audited interfaces**

```python
def tick(self, now: datetime) -> list[UUID]:
    if self._paused:
        return []
    return [self._start(schedule, now) for schedule in self.repository.due_schedules(now) if self._can_claim(schedule)]

@router.post("/runs/shadow", status_code=202)
def start_shadow_run(request: ShadowRunRequest, operator: Operator = Depends(require_operator)) -> RunResponse:
    return RunResponse(execution_id=workflow_service.start_shadow(request.workflow, request.store, operator.subject), state="QUEUED")
```

Define `ShadowRunRequest(workflow: str, store: str, reason: str)`, `RunResponse(execution_id: UUID, state: str)`, and `Operator(subject: str)` as Pydantic models before registering the route. `require_operator` must return an `Operator` only after the configured internal authentication adapter validates the request.

Bind the web listener only to the configured internal host. Service Control Manager pause/stop must quiesce scheduling, wait for checkpoint deadline, and record a lifecycle event. API/CLI manual actions append operator identity and reason to the audit event.

Create a typed TypeScript API client from the documented FastAPI OpenAPI shape. Implement the React operations dashboard from the approved Stitch handoff without copying generated mock data or direct data-access code. `docs/ui/STITCH_HANDOFF.md` must record each approved screen's Stitch reference/export version, screenshot location, intended user role, required loading/empty/error states, and API endpoint mapping.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/unit/test_scheduler.py tests/unit/test_routes.py tests/unit/test_cli.py -v; npm run typecheck; npm run test -- --run; npm run build` (frontend commands run in `frontend/`).

Expected: PASS.

```bash
git add src/esl_service/runtime src/esl_service/web src/esl_service/main.py src/esl_service/cli.py frontend docs/ui/STITCH_HANDOFF.md tests scripts/install-service.ps1
git commit -m "feat: add operations interfaces and runtime host"
```

### Task 8: Prepare staging deployment and baseline collection

**Files:**
- Create: `scripts/deploy-artifact.ps1`, `docs/BASELINE_COLLECTION.md`
- Modify: `docs/WORKFLOW.md`, `docs/PROGRESS.md`

**Interfaces:**
- Produces artifact checksum verification, migration, restart, readiness check, rollback steps, and a 14-day PostgreSQL baseline report by execution/store/time range.

- [ ] **Step 1: Write the deployment acceptance checks**

```markdown
- Artifact SHA-256 matches the approved release record.
- Alembic migration completes successfully.
- Readiness reports state store, SQL Server, and AIMS read dependencies separately.
- A shadow run for store 084 records INTENDED actions only.
- Pause/resume and restart preserve execution state.
```

- [ ] **Step 2: Add the checksum guard**

```powershell
if ((Get-FileHash -Algorithm SHA256 $ArtifactPath).Hash -ne $ExpectedSha256) {
    throw "Artifact checksum does not match the approved release record."
}
```

- [ ] **Step 3: Implement controlled deployment and baseline document**

```powershell
& $PythonPath -m alembic upgrade head
Restart-Service -Name $ServiceName
Invoke-WebRequest -UseBasicParsing "http://$InternalHost/health/ready" | Out-Null
```

Document 14 consecutive days for stores 075 and 084: source window, extracted/valid/rejected/eligible/intended counts, duration, dependency errors, and unresolved records. Query PostgreSQL; do not compare physical files.

- [ ] **Step 4: Verify and commit**

Run: `powershell -ExecutionPolicy RemoteSigned -File scripts/deploy-artifact.ps1 -ArtifactPath <approved-artifact> -ExpectedSha256 <approved-sha256>`

Expected: PASS only in staging after all acceptance checks pass.

```bash
git add scripts/deploy-artifact.ps1 docs/BASELINE_COLLECTION.md docs/WORKFLOW.md docs/PROGRESS.md
git commit -m "docs: add staging deployment and baseline collection"
```

## Deferred Follow-on Plans

1. Promotion policy and activation after POS/merchandising approves BR-005 campaign precedence, time/day eligibility, and public/member rules.
2. CSV consumer replacement after consumer, acknowledgement semantics, and retention are known.
3. Production cutover after staged shadow evidence meets approved parity, recovery, and performance gates.

## Self-Review

### Specification coverage

Tasks 2–5 cover configuration, read-only ingestion, canonical transformation, AIMS boundaries, and the confirmed kilogram-to-100-gram rule. Tasks 3, 6, and 7 cover durable state, leases, scheduler control, audit, recovery, and reconciliation. Tasks 6 and 8 cover shadow comparison and baseline collection. Task 7 covers internal web UI/API, CLI, and Windows Service execution. Tasks 1 and 8 cover GitHub build verification and controlled artifact deployment. BR-005 is deliberately isolated in Task 4.

### Placeholder scan

Every task names files, tests, commands, interfaces, and completion evidence. Controlled infrastructure values are supplied only at execution time.

### Type consistency

`Settings`, `ExecutionRepository`, `CanonicalEslRecord`, `PageChange`, `PageChangeReceipt`, `ExecutionSummary`, `ReconciliationReport`, `ShadowWorkflow`, and `Scheduler` are introduced before use and keep the same names throughout.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, with checkpoints for review.
