# End-of-Week Testing Readiness Plan

> **For agentic workers:** Execute this plan only from the issue-specific branch/worktree created under `docs/WORKFLOW.md`. Record every result and handoff in `docs/PROGRESS.md`.

**Goal:** By the end of the week ending 2026-08-30, establish reproducible foundation-test evidence, a safe non-production PostgreSQL test path, and an evidence-based decision on whether enough agent-token capacity remains for the next implementation task.

**Architecture:** This is a readiness plan, not a claim that the complete ESL replacement is testable. It validates only Tasks 1 and 2, verifies the CI/repository workflow, and prepares the isolated database required for Task 3. No task contacts production systems or AIMS/SQL Server endpoints.

**Tech Stack:** Python 3.12, pytest, Ruff, mypy, Node 22, npm, Vitest, Vite, PostgreSQL, GitHub Actions, GitHub Issues/PRs.

**Spec:** `docs/SPECIFICATION.md`, `docs/WORKFLOW.md`, `docs/PROGRESS.md`, `docs/superpowers/plans/2026-08-25-esl-platform-foundation-and-shadow-plan.md`

## Global constraints

- Do not use production credentials, databases, AIMS, SQL Server, Hop, Jenkins, or ESLs.
- Use a dedicated non-production PostgreSQL database for migration/integration checks.
- CI must use Python 3.12 and Node 22; record local versions used.
- Keep `.env` files and all secret values out of Git, logs, issues, PRs, and checkpoints.
- Do not infer unseen agent-token quotas. Measure the displayed remaining weekly quota and reset time at each checkpoint.
- The full shadow workflow cannot receive an end-of-week pass/fail result until Tasks 3-8 are implemented.

## Token-capacity ledger

At the beginning and end of every agent session, add this row to the active issue or `docs/PROGRESS.md`. Read values from the active agent product UI/account; never estimate a hidden quota.

| Timestamp | Agent/session | Weekly quota | Used this week | Remaining | Reset time | Planned work before reset | Reserve | Decision |
| --- | --- | ---: | ---: | ---: | --- | --- | ---: | --- |
| 2026-08-25 | First test-readiness session | Record displayed value | Record displayed value | Record displayed value | Record displayed time | Foundation verification + Task 3 planning | 30% of weekly quota | Start only if remaining covers planned work plus reserve. |

1. Keep at least 30% of the visible weekly quota unallocated for review, test failures, documentation, and handoff.
2. Before starting work expected to use more than 20% of visible remaining quota, split it at an independently testable boundary or wait for quota reset.
3. At 40% remaining, stop starting new implementation tasks; finish current verification, update the checkpoint, and hand off.
4. At 30% remaining, perform only review, tests, documentation, and safe handoff work.
5. The decision is **insufficient** if no displayed quota/reset information is available; record that limitation and do not claim end-of-week capacity is adequate.

### Task 1: Verify local foundation tooling

**Files:**
- Modify: `docs/PROGRESS.md` (checkpoint only)

- [ ] **Step 1: Record capacity before work**

Record the token ledger row, current branch/commit, Python version, Node version, and npm version in `docs/PROGRESS.md`.

- [ ] **Step 2: Create a Python 3.12 environment**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected: Python reports `3.12.x`; dependencies install without recording credentials.

- [ ] **Step 3: Verify Python foundation checks**

```powershell
.\.venv\Scripts\python.exe -m pytest -v --basetemp .test-tmp -o cache_dir=.test-tmp\pytest-cache
.\.venv\Scripts\python.exe -m ruff check src tests --no-cache
.\.venv\Scripts\python.exe -m mypy src --cache-dir .test-tmp\mypy-cache
```

Expected: all checks pass. Record exact output counts in the checkpoint.

- [ ] **Step 4: Verify frontend foundation checks**

```powershell
Set-Location frontend
npm ci
npm run typecheck
npm run test -- --run
npm run build
Set-Location ..
```

Expected: typecheck, Vitest, and Vite build pass using Node 22.

- [ ] **Step 5: Record result and commit only if documents changed**

Record commands/results and token capacity in `docs/PROGRESS.md`. Commit the checkpoint with `docs(progress): record foundation verification`.

### Task 2: Prepare isolated PostgreSQL test database

**Files:**
- Modify: `docs/PROGRESS.md` (checkpoint only)
- Create locally only: `.env` copied from `.env.dev.example`

- [ ] **Step 1: Create the non-production identity**

Create a database and role that are not shared with production. The role may create/migrate only the ESL test schema/database and must have no SQL Server, AIMS, production PostgreSQL, or Windows-administrator privilege.

- [ ] **Step 2: Store the test URL outside Git**

Copy `.env.dev.example` to `.env` and set `ESL_TEST_DATABASE_URL` locally. Do not commit `.env`, copy its value to a checkpoint, or paste it into an issue/PR.

- [ ] **Step 3: Prove safe connectivity**

Run a Task 3 test-only connectivity/migration command only after its implementation provides that command. Record database name (not URL/password), outcome, migration revision, and whether test data was removed.

- [ ] **Step 4: Record capacity and result**

Update the token ledger and handoff checkpoint. If remaining quota is below the reserve rule, defer Task 3 implementation to the next quota window.

### Task 3: Confirm CI and GitHub evidence path

**Files:**
- Modify: `docs/PROGRESS.md` (checkpoint only)

- [ ] **Step 1: Create or use a documentation/test-readiness issue**

Create an issue titled `[ci] verify end-of-week foundation readiness`, label and assign it using `docs/WORKFLOW.md`.

- [ ] **Step 2: Verify the branch/PR workflow**

Create an issue branch/worktree, make only the checkpoint change, open a PR using `.github/pull_request_template.md`, and confirm GitHub Actions reports the Python 3.12 and Node 22 checks.

- [ ] **Step 3: Record end-of-week decision**

| Outcome | Conditions |
| --- | --- |
| Foundation ready; continue Task 3 | Python/frontend checks pass, test database is isolated, and visible remaining quota covers Task 3 estimate plus 30% reserve. |
| Foundation ready; wait for quota reset | Technical checks pass but the capacity rule is not met. |
| Not ready | A required check, database isolation, CI result, or documented capacity measurement is missing/fails. |

## Self-review

The plan covers local foundation checks, safe database preparation, CI evidence, and token-capacity measurement. It explicitly excludes unimplemented Task 3-8 behavior and all production/external operational systems. No secrets or invented quota values appear in the plan.
