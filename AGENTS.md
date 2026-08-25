# SOLUM ESL / AIMS Pipeline Replacement

## Purpose and phase

This repository defines the replacement of the ESL SQL Server, SQL Server Agent, Apache Hop, and Jenkins processing path. It is in the **architecture and specification phase**. Do not add production application code until the documents below are reviewed and an implementation plan is approved.

## Read first

- Every task: `docs/PROGRESS.md`.
- Requirements, acceptance, migration: `docs/SPECIFICATION.md`.
- Boundaries and design decisions: `docs/SYSTEM_ARCHITECTURE.md`.
- Operator-facing procedures: `docs/WORKFLOW.md`.
- Current-system evidence: `docs/sql-server/` and `docs/hop-jenkins-pipeline/`.

## Non-negotiable rules

- `docs/` is the project source of truth. Resolve documentation/implementation conflicts explicitly before changing code.
- Classify evidence as **VERIFIED**, **INFERRED**, or **UNKNOWN / NEEDS-DISCOVERY**. Do not invent business rules.
- Keep SOLUM AIMS behind an adapter. The initial cutover permits a dedicated, read-only PostgreSQL compatibility adapter; direct AIMS database writes are forbidden.
- Never modify production databases, SQL Agent, Jenkins, Hop, AIMS, or physical ESLs during investigation. Never expose credentials or copy secrets into documentation.
- Externalize configuration and use approved secret storage. No credentials in source control or logs.
- Update the relevant documents and `docs/PROGRESS.md` when a meaningful decision, discovery, risk, or implementation state changes.
- Test business rules without production dependencies where practical. New implementation must include the requirement ID and test traceability.

## Repository layout

- `docs/` — authoritative specifications, architecture, workflow, and progress.
- `docs/sql-server/` — read-only current-state SQL evidence.
- `docs/hop-jenkins-pipeline/` — read-only current-state Hop/Jenkins/AIMS evidence.

## Selected implementation stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, and Typer.
- Frontend: React, TypeScript, Vite, and Tailwind CSS.
- Google Stitch is the UI-design and handoff tool. Its exports are visual/source references; production UI components must be implemented, tested, and reviewed in `frontend/`.

Build, test, and lint commands are defined in `pyproject.toml` and `frontend/package.json` once the scaffold is added. CI must run the Python and frontend checks.

## Agent workflow

Discover available skills before starting a task and use applicable skills. Keep this file concise; place detailed architecture and operational content in `docs/`.
