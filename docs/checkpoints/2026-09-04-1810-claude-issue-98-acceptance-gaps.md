# 2026-09-04 18:10 — Claude — #98 follow-up: close the three unmet acceptance criteria

## Timestamp and owner

2026-09-04 18:10 local. Claude (Opus 5), on the owner's instruction to continue #98 after PR #129
merged.

## Issue

GitHub #98, "[runtime] issue and rotate API tokens with one esl-admin command for scripted
environment setup". Labels `type:feature`, `priority:p2`, `area:operations`, `area:runtime`.
Assignee `it20pepito`. The issue stayed open after PR #129 because a pull request targeting
`develop` does not close by keyword (AD-013/AD-014); re-reading its acceptance criteria against
the merged work found three genuinely unmet, which is what this checkpoint closes.

## Git state

Branch `claude/98-issue-token-acceptance`, created with `gh issue develop` so it links in the
Development sidebar. Worktree
`D:\Documents\Dev\solum-aims-esl-pipeline\.worktrees\issue-98-acceptance`, base `297350e`
(the merge of PR #129). Files changed: `src/esl_service/runtime/cli.py`,
`tests/unit/runtime/test_cli.py`, `tests/unit/runtime/test_issue_token_end_to_end.py`,
`tests/unit/web/test_routes.py`, `docs/WORKFLOW.md`, `docs/PROGRESS.md`, this checkpoint.

## Scope

The three gaps, each stated as the issue states it, and what closed it.

**1. "A token issued this way is accepted by `GET /runs` after a service start (integration test
with the app factory and a fake bundle)."** The merged work proved the token through
`BearerTokenAuthenticator` only, which is a lookup, not a request. Two tests now drive the CLI,
build the app through `create_app`, and call `GET /runs`: the issued token is accepted, and after
a rotation the superseded token is refused `401` with its value absent from the response. This
exercises the bundle on disk, `tokens_from_bundle`, the authenticator, the app factory, and the
principal dependency in one pass — five places a token can be stored correctly and still be
refused. `build()` in `tests/unit/web/test_routes.py` gained an optional `authenticator`
parameter so the test wires in the authenticator the service host builds, rather than assembling
a second copy of the app that could drift from it. Every call carries a bounded query
(`store_code`), because `GET /runs` refuses an unbounded one with `422` before any role check.

**2. "audited as `secret.set` with `rotated: true` evidence."** The merged work audited the
action and the key but no evidence, so two entries for one account were indistinguishable: the
ledger could not tell a first provisioning from a rotation that invalidated a token already in
use. `_record_audit` and `_audit_or_warn` now take `after_evidence`, and `issue-token` passes
`{"rotated": <bool>}`. It goes through the repository's existing `_sanitized`, and
`after_evidence` persistence is already proven against the real database by
`tests/integration/test_aims_compatibility_reader.py`.

**3. "prints it to stdout when stdout is a terminal or `--stdout` is given."** The merged work
required an explicit channel in every case. It now follows the issue: at a terminal the command
with neither option prints the token; when standard output is a pipe, a redirect, or a
transcript, naming a channel is still required, because a token written into a log is not a
reveal anyone chose. `_stdout_is_a_terminal()` is the seam a test drives both ways. Naming both
channels remains a refusal.

Non-goals unchanged from the merged work. No migration, no schema change, no new setting, no
change to any route or its authorization.

## Evidence

RED was observed for all five new tests before the change that made each pass. The two API tests
failed on the missing coverage (`422`, then a missing harness parameter); the rotation-evidence
test failed on a missing `after_evidence` key; the two terminal tests failed with
`AttributeError: module has no attribute '_stdout_is_a_terminal'`.

| Command | Result |
| --- | --- |
| `python -m pytest -q tests/unit/runtime/test_cli.py` (before) | 3 failed, 24 passed |
| `python -m pytest -q tests/unit/runtime/test_issue_token_end_to_end.py` (before) | 2 failed, 3 passed |
| `python -m pytest -q tests/unit/runtime tests/unit/web` (after) | 252 passed |
| `python -m ruff check src tests` | All checks passed |
| `python -m mypy src` | Success, no issues in 76 source files |
| `python -m pytest -q` with `ESL_TEST_*` exported | **1256 passed, 7 skipped** in 40.5 s |
| `git diff --check` | clean |

Environment: Python 3.12 on Windows 11, the worktree's own `.venv`, against the dedicated
non-production PostgreSQL test database.

## Configuration

Names only. `ESL_OPERATOR_ROLES`, `ESL_SERVICE_IDENTITY_SID`, `ESL_SECRET_BUNDLE_PATH`; bundle key
`api.token.<account>`. No new setting. No value, token, SID, or DPAPI blob appears here.

## External state

None. Temporary paths with a Base64 stand-in codec and a no-op protector, plus the dedicated
non-production PostgreSQL test database. No AIMS, no SQL Server, no staging or production host.

## Risks and next action

- **One acceptance criterion is deliberately not met as written, and this is the record of that
  decision.** The issue asks that `--out` create the file "with an ACL restricted to the named
  account and administrators". The implementation protects it with the *service identity's* ACL,
  exactly as the bundle is protected, so the file is readable by the issuing account,
  Administrators, and SYSTEM — not by the token holder. Granting the token holder would mean
  resolving a bare name from `ESL_OPERATOR_ROLES` to a SID, which adds a Windows lookup and a new
  failure mode in order to *widen* access to a file the procedure says to hand over by hand and
  then delete. If the owner wants the account holder to read the file directly, that is a
  deliberate change to make, not a defect to fix silently. Decision owner: the repository owner.
- **`install-service.ps1 -IssueTokensFor` is still not exercised on a staging host.** Unchanged
  from the previous checkpoint, still the owner's step, still INFERRED rather than VERIFIED.
- **A token written to `--out` is a plaintext secret on disk** and nothing deletes it for the
  operator. Deliberate — the file is the only copy — but a standing operational risk.
- Smallest safe next action: open the pull request against `develop` and let CI confirm on a clean
  checkout; then the issue can be closed by hand, which a `develop`-targeted PR will not do.
