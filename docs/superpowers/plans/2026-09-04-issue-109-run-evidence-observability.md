# Issue #109 per-run evidence and observability implementation plan

> **For implementation agents:** Follow this plan with strict test-first changes. Stop if the issue contract conflicts with the source-of-truth documents.

**Goal:** Give authorized operators one read-only CLI/API contract for issue evidence, the latest reconciliation report, step timing/checkpoint counts, recovery guidance, and bounded Prometheus trend metrics without querying PostgreSQL directly.

**Requirement traceability:** GitHub #109; FR-012, FR-022; NFR-007, NFR-008, NFR-009. NFR-008 is included because the accepted issue explicitly requires Prometheus metrics even though its issue-body traceability line omitted that requirement ID.

**Approved design:** Add an application-level read service that authorizes every request through the existing `status` operation, reads immutable evidence through a dedicated persistence query port, sanitizes evidence a second time at the presentation boundary, and returns typed immutable application views. CLI and FastAPI adapt the same views. `/metrics` exposes aggregate counters and duration totals for the latest configurable number of runs per workflow/store; execution IDs are deliberately not metric labels. The default is `ESL_METRICS_RUN_LIMIT=20`.

**Persistence contract:** Read existing tables only. There is no Alembic migration. The latest reconciliation revision is selected before exceptions. Keyless `RECORD_EXCLUDED` events participate in issue summaries and drill-down. Metric selection is bounded independently for each `(workflow_name, store_code)` scope.

## Task 1: Lock the contracts with failing tests

- Add pure application tests for filtering, grouping, pagination, latest-report selection data, duration/count derivation, recovery inclusion, and evidence sanitization.
- Extend schema tests so every new response rejects extra fields and secret-like evidence cannot cross the boundary.
- Extend API and CLI tests for authorization, filters, output parity, report drill-down, step evidence, recovery fields, and `/metrics`.
- Add one PostgreSQL integration test that persists a #104 fixture and proves issue/query parity including a keyless exclusion and a legacy mismatch.
- Run each focused test and record the expected initial failure before implementation.

## Task 2: Implement the shared read service and persistence queries

- Add `src/esl_service/application/run_evidence.py` with typed queries/views and the authorized read service.
- Add `src/esl_service/persistence/run_evidence_repository.py` with bounded, stable, read-only queries over executions, record issues, keyless events, latest reconciliation reports/exceptions, steps/checkpoints, and unresolved actions.
- Wire a transactional evidence port in `src/esl_service/runtime/host.py`.
- Keep AIMS, source adapters, action mutation, and existing migrations untouched.

## Task 3: Expose strict API, CLI, and metrics adapters

- Extend `src/esl_service/web/audit_schemas.py` with `extra="forbid"` response models for issue, report, step, recovery, and metrics-facing data.
- Extend `src/esl_service/web/app.py` with `GET /runs/{id}/issues`, `GET /runs/{id}/report`, the detailed `GET /runs/{id}`, and token-protected `GET /metrics`.
- Extend `src/esl_service/runtime/cli_operations.py` with `runs issues`, `runs report`, and richer `runs show` output.
- Add `metrics_run_limit` to `src/esl_service/config.py` and both example environment files.
- Add `prometheus-client` to `pyproject.toml` for standards-compliant exposition.

## Task 4: Operator documentation, checkpoint, and delivery

- Replace the database-query placeholders in the two required `docs/WORKFLOW.md` procedures with the new commands/endpoints and explain filters, pagination, reconciliation evidence, and metrics scope.
- Record focused and full verification results in a new immutable `docs/checkpoints/` file and update `docs/PROGRESS.md` only for material issue state/contract changes.
- Run Ruff, mypy, the complete pytest suite, frontend typecheck/test/build, and `git diff --check`.
- Commit, push, open a PR to `develop`, request cross-agent review, wait for required CI, merge, fast-forward local `develop`, and add the post-merge checkpoint.

**Expected files:** `src/esl_service/application/run_evidence.py`, `src/esl_service/persistence/run_evidence_repository.py`, `src/esl_service/runtime/{host,cli_operations}.py`, `src/esl_service/web/{app,audit_schemas}.py`, `src/esl_service/config.py`, `.env.dev.example`, `.env.production.example`, `pyproject.toml`, focused unit/integration tests, `docs/WORKFLOW.md`, `docs/PROGRESS.md`, and new immutable checkpoint files.

