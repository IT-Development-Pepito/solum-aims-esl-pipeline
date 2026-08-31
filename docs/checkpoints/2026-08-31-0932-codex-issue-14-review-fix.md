# Issue #14 review correction checkpoint

## Scope and evidence

- Issue: GitHub #14, FR-007 explicit workflow states and dependencies.
- Authoritative source: `docs/SYSTEM_ARCHITECTURE.md` section 5.4.
- Review finding: the first implementation admitted four transitions absent from the authoritative state graph and omitted `RUNNING -> SKIPPED`.
- Evidence classification: **VERIFIED** from the repository source of truth; no current-system behavior was inferred.

## Test-driven correction

- RED: the focused workflow suite reported 5 failed and 11 passed after adding architecture-traceable regressions.
- GREEN: `python -m pytest tests/unit/domain/test_workflow.py -q` — 16 passed.
- `python -m ruff check src/esl_service/domain/workflow.py tests/unit/domain/test_workflow.py` — passed.
- `python -m mypy src/esl_service/domain/workflow.py` — passed.

The corrected graph permits `QUEUED -> RUNNING`; terminal outcomes including `SKIPPED` and `CANCELLED` from `RUNNING`; and only a return to `RUNNING` from `RETRY_WAIT` or `RECOVERING`.

## Safety and external state

- No migrations or persistence changes.
- No PostgreSQL, SQL Server, AIMS, Hop, Jenkins, CSV delivery, ESL device, or production-system access.
- No configuration values, credentials, connection strings, or DPAPI material read or changed.

## Next action

Commit and push the correction, rerun the complete verification gate at the new branch tip, and obtain follow-up review before merge.
