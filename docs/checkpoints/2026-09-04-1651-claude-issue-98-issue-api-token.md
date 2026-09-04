# 2026-09-04 16:51 — Claude — #98 issue and rotate API tokens with one command

## Timestamp and owner

2026-09-04 16:51 local. Claude (Opus 5), working under the owner's explicit instruction to start #98.

## Issue

GitHub #98, "[runtime] issue and rotate API tokens with one esl-admin command for scripted
environment setup". Labels `type:feature`, `priority:p2`, `area:operations`, `area:runtime`.
Assignee `it20pepito`.

## Git state

Branch `claude/98-issue-api-token`, created with `gh issue develop` so the issue links in the
pull request's Development sidebar (AD-013/AD-014). Worktree
`D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-api-token`, base `0e72338` on
`develop` (the merge of PR #126). No pull request opened at the time of writing this
checkpoint; it follows immediately. Uncommitted before the commit: `src/esl_service/runtime/cli.py`,
`tests/unit/runtime/test_cli.py`, `tests/unit/runtime/test_issue_token_end_to_end.py` (new),
`scripts/install-service.ps1`, `docs/WORKFLOW.md`, `README.md`, `docs/PROGRESS.md`, and this file.

## Scope

AD-017 (DPAPI bundle, user scope), AD-019 (per-account bearer tokens under `api.token.<account>`),
NFR-016 (repeatable environment setup).

Completed behaviour:

- `esl-admin secrets issue-token <account> --reason <ticket>` generates a token with
  `secrets.token_urlsafe(32)`, stores it under `api.token.<account>` in the bundle through the
  existing `SecretBundleStore`, and reveals it exactly once through the one channel the caller
  names: `--out <path>` writes a file and applies the bundle's own ACL through `FileProtector`,
  `--stdout` prints it. Naming both, or neither, exits 2 without touching the bundle.
- Rotation is the same command run again. The command reads the store's key listing first and,
  when the key existed, tells the operator that the previous token no longer authenticates and
  that the service must be restarted to reload the bundle.
- Four refusals: an existing `--out` file is never overwritten (it may hold a token still in
  use); a bundle that cannot be read is never overwritten; an account name the bundle key
  grammar rejects is refused with the grammar named; and the identity guard of `secrets set`
  applies unchanged, so in staging and production the command must run as the account in
  `ESL_SERVICE_IDENTITY_SID`.
- A warning, not a failure, when the account holds no role in `ESL_OPERATOR_ROLES`: such a token
  authenticates and is then refused for every operation. Assignments are read from settings when
  they load and from the environment when they do not, because on a development machine the rest
  of the configuration is usually absent and the warning is still worth having.
- The audit entry is the existing `secret.set` action naming only the key. The token value
  reaches no log, no audit row, and no error message.
- `scripts/install-service.ps1` gained `-IssueTokensFor <accounts>` and `-TokenDirectory <dir>`,
  running the new command once per account after registration and writing `<dir>\<account>.token`.
  It refuses a missing `-TokenDirectory`, a directory that does not exist, and a missing
  `esl-admin` next to the interpreter, and it stops on the first non-zero exit code.

Explicit non-goals: no change to how tokens are read (`tokens_from_bundle` and
`BearerTokenAuthenticator` are untouched), no new bundle key kind, no revocation command
(`secrets remove` already does it), no migration, no schema change, and no change to any HTTP
route or its authorization.

Files changed: `src/esl_service/runtime/cli.py`, `tests/unit/runtime/test_cli.py`,
`tests/unit/runtime/test_issue_token_end_to_end.py` (new), `scripts/install-service.ps1`,
`docs/WORKFLOW.md`, `README.md`, `docs/PROGRESS.md`, this checkpoint.

## Evidence

Test-first throughout: each behaviour was observed failing before the production change that made
it pass.

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/unit/runtime/test_cli.py` | 24 passed |
| `python -m pytest -q tests/unit/runtime/test_issue_token_end_to_end.py` | 3 passed |
| `python -m ruff check src tests` | All checks passed |
| `python -m mypy src` | Success, no issues in 76 source files |
| `python -m pytest -q` (no test database configured) | 1054 passed, 204 skipped |
| `python -m pytest -q` (with `ESL_TEST_*` exported) | **1251 passed, 7 skipped** in 39.4 s |
| `git diff --check` | clean |
| PowerShell AST parse of `scripts/install-service.ps1` | parses cleanly |

Environment: Python 3.12 on Windows 11, the worktree's own `.venv`. The dedicated non-production
PostgreSQL test database supplied the 197 database-backed tests that are otherwise skipped.

The round-trip coverage is the part that matters and is worth naming: storing a value in the
bundle proves nothing about whether the API would accept it. `test_issue_token_end_to_end.py`
drives the CLI, then builds the authenticator the way the service host does, and asserts that the
issued token authenticates as its own account with its role, that a second issue invalidates the
first value, and that two accounts resolve to two distinct tokens.

## Configuration

Names only. `ESL_OPERATOR_ROLES` (read for the role warning), `ESL_SERVICE_IDENTITY_SID` (the
identity guard), `ESL_SECRET_BUNDLE_PATH` (the bundle location). Bundle key `api.token.<account>`.
No new setting was introduced. No value, token, SID, path with a credential, or DPAPI blob appears
in this checkpoint or in any output the command produces.

## External state

None. Every test ran against temporary paths with a Base64 stand-in codec and a no-op file
protector, or against the dedicated non-production PostgreSQL test database. No AIMS database, no
SQL Server, no production or staging host, and no real DPAPI bundle was read or written.

## Risks and next action

- **The install script's token pass was not exercised.** The acceptance criterion asks for
  `-IssueTokensFor` to be run manually on the staging host with the result recorded. This
  repository reaches no staging host, so the block was verified by parse and by review only, and
  running it stays the owner's step. Decision owner: the repository owner. Until it runs, treat
  `-IssueTokensFor` as INFERRED-correct rather than VERIFIED.
- **A token written to `--out` is a plaintext secret on disk.** The command applies the bundle's
  ACL and the documentation says to hand the file over and delete it, but nothing deletes it for
  the operator, and nothing detects that it was left behind. That is deliberate — the file is the
  only copy, so the tool must not remove it — but it is a real operational risk worth naming in
  any review of the setup procedure.
- Smallest safe next action: open the pull request against `develop` and let CI (Windows `verify`
  plus Ubuntu `database-verify`) confirm the suite on a clean checkout.
