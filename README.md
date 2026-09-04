# solum-aims-esl-pipeline

Replacement for the ESL processing path that today runs through SQL Server stored procedures, SQL Server Agent, Apache Hop, and Jenkins. One Python + PostgreSQL service reads the retail sources, computes label state in explicit domain rules, and drives SOLUM AIMS through adapters, with durable execution state, audit, and reconciliation.

`docs/` is the source of truth. Start with `AGENTS.md`, then `docs/PROGRESS.md`, `docs/SPECIFICATION.md`, `docs/SYSTEM_ARCHITECTURE.md`, and `docs/WORKFLOW.md`. This README covers the two setup tasks a developer meets first: provisioning credentials, and cloning the AIMS databases locally.

---

## 1. Setting up credentials

### How credentials work

No password is ever placed in an environment variable or a `.env` file that the application reads. Configuration names **where** a database is and **as whom** to connect; the password itself lives in one file, the **secret bundle**, encrypted with Windows DPAPI under **user scope** (AD-017). Only the Windows account that wrote the bundle can read it.

The bundle on disk is ciphertext. You never edit it by hand. You populate it with `esl-admin secrets set`, which prompts for the value, encrypts it in memory, and writes the file with a restrictive ACL. Decrypted, the bundle is a JSON object of four string values:

```json
{
  "state.password": "...",
  "source.sql.password": "...",
  "aims.portal.password": "...",
  "aims.core.password": "..."
}
```

### The four keys

| Key | Password of this user | On this database | Named in configuration by |
| --- | --- | --- | --- |
| `state.password` | the user in `ESL_DATABASE_URL` | the service's own PostgreSQL (execution state, audit, reconciliation) | `ESL_DATABASE_URL` |
| `source.sql.password` | `ESL_SOURCE_SQL_USERNAME` (e.g. `esl_reader`) | every SQL Server tier: `DBWH_8555`, `ESL`, `PEPITO_HO`, and each store's local iRetail server | `ESL_SOURCE_SQL_HOST`, `ESL_SOURCE_PEPITO_HO_HOST` |
| `aims.portal.password` | `ESL_AIMS_PORTAL_USERNAME` | `AIMS_PORTAL_DB` | `ESL_AIMS_HOST`, `ESL_AIMS_PORTAL_DATABASE` |
| `aims.core.password` | `ESL_AIMS_CORE_USERNAME` | `AIMS_CORE_DB` | `ESL_AIMS_HOST`, `ESL_AIMS_CORE_DATABASE` |

One read-only account covers every SQL Server tier, so one key serves all of them, including per-store servers whose addresses are read from `DimStore` at run time.

A fifth kind of key is not a password: `api.token.<account>` holds the bearer token one account uses to call the internal operations API (#28, AD-019). You do not generate this one yourself: `esl-admin secrets issue-token <account> --reason <ticket> --out <path>` creates it, stores it, and reveals it once, and running it again rotates it. Give the account a role in `ESL_OPERATOR_ROLES` too; see `docs/WORKFLOW.md`, "Provision an API token".

### Raw or encoded?

There are exactly two places a password can go, and the rule is different for each.

| Where | Which passwords | How to write them |
| --- | --- | --- |
| **Secret bundle**, via `esl-admin secrets set` | all four keys above | **raw** — type the password exactly as it is, no encoding of any kind |
| **`.env`**, only the three `ESL_TEST_*_URL` variables used by the test suite | the test database and the local AIMS clone | **percent-encoded**, because they sit inside a URL: `@`→`%40`, `:`→`%3A`, `/`→`%2F`, `#`→`%23`, `?`→`%3F`, `%`→`%25`, space→`%20` |

Every other variable in `.env` carries no password. A password made only of letters, digits, `-`, `_`, and `.` needs no encoding even in a URL. To encode one safely:

```powershell
python -c "from urllib.parse import quote; print(quote(input(), safe=''))"
```

The startup gate refuses an `ESL_DATABASE_URL` that still embeds a password, so the bundle is the single source of truth for that credential.

### Setting up a new environment, in order

```
1. create the database accounts        esl_pipeline_*, esl_reader, esl_aims_reader
2. create the bundle directory         C:\ProgramData\SOLUM\ESL, ACL: service account, Administrators, SYSTEM
3. esl-admin secrets set  x 4          one command per key, see below
4. alembic upgrade head                migrates the state store; password comes from state.password
5. esl-admin check-connections         every target must be REACHABLE or UNCONFIGURED
6. esl-admin secrets issue-token       one per API account; or -IssueTokensFor at install time
7. start the service
```

Step 4 needs no password in `ESL_DATABASE_URL`: Alembic resolves `state.password` from the bundle exactly as the service does. Until it has run, `secrets set` still stores the secret but warns that the audit entry could not be recorded because the schema is not migrated.

Step 2 applies to staging and production. The tool refuses to write into a missing directory when a service identity is configured, because a folder it created itself would carry inherited permissions that the startup validator rejects. On a development machine, where no service identity is configured, `esl-admin` creates the directory for you and says so.

Step 2 is run **as the Windows Service account** in staging and production. Under user-scope DPAPI a bundle written by any other account is unreadable by the service, so `esl-admin` refuses to write when `ESL_SERVICE_IDENTITY_SID` is configured and does not match the running account. On a development machine that variable is unset; the tool prints `Identity check skipped` and proceeds under your own account.

Install the CLI once with `pip install -e .`; it is the `esl-admin` script.

### Running `esl-admin secrets set`

One command per key. The value is typed at a hidden, confirmed prompt; it is never accepted as a command-line argument, so it cannot reach shell history or a process listing.

```
PS> esl-admin secrets set state.password --reason "CHG-1042 initial provisioning"
Identity check skipped: no service identity is configured.
Secret value:               <- type the raw password; nothing is echoed
Repeat for confirmation:    <- type it again
Stored secret 'state.password' in C:\ProgramData\SOLUM\ESL\secrets.dpapi.
```

Repeat for `source.sql.password`, `aims.portal.password`, and `aims.core.password`. The path defaults to `C:\ProgramData\SOLUM\ESL\secrets.dpapi` (`ESL_SECRET_BUNDLE_PATH`); pass `--bundle <path>` to use another. `--reason` is required and is recorded in the audit trail together with the actor and the key name, never the value.

To supply the value from a script instead of typing it, pipe it with `--stdin`:

```powershell
Get-Content .\pw.txt | esl-admin secrets set aims.portal.password --reason "CHG-1042" --stdin
Remove-Item .\pw.txt
```

### Verifying

```
PS> esl-admin secrets list
aims.core.password
aims.portal.password
source.sql.password
state.password
```

`list` shows names only. Setting a secret proves only that it is readable; proving it is **correct** requires using it:

```
PS> esl-admin check-connections
state-store          REACHABLE            esl_pipeline_service
warehouse            REACHABLE            esl_reader
legacy-baseline      REACHABLE            esl_reader
pepito-ho            REACHABLE            esl_reader
aims-portal          REACHABLE            esl_aims_reader
aims-core            REACHABLE            esl_aims_reader
```

| Outcome | Meaning | What to do |
| --- | --- | --- |
| `REACHABLE` | connected; the identity the server reports is shown | nothing |
| `UNCONFIGURED` | host, database, or username is blank for that tier | configure it, or leave it if that tier is not in use yet; not counted as a failure |
| `SECRET_UNAVAILABLE` | the bundle has no entry for that key | `esl-admin secrets set <key>` |
| `CREDENTIAL_REJECTED` | the server answered and refused the password | the value in the bundle is wrong or the account's password changed; set it again |
| `UNREACHABLE` | no answer from host, port, or database | route, firewall, hostname, or the database name |
| `DRIVER_MISSING` | the ODBC driver named in `ESL_SOURCE_SQL_DRIVER` is not installed; the message names it | install the driver, or fix the setting — a value like `ODBC+Driver+18+for+SQL+Server` is URL-encoded and must be written with spaces |

The command exits non-zero when any target is neither `REACHABLE` nor `UNCONFIGURED`, so it can run unattended. Output never contains a password or a connection string.

### When to run `secrets set` again

It is a manual action, never part of application startup and never scheduled. Run it when:

- **a new environment is being set up** — all four keys, before the first start;
- **a database password is rotated** — only the key that changed;
- **an administrator resets the service account's Windows password** — all four keys, because the bundle becomes permanently unreadable (the service reports `secret bundle is unavailable`, indistinguishable from a missing bundle). Changing the password *as the account*, knowing the old one, does not cause this;
- **a new source is added** that needs its own credential — the new key only.

An existing bundle that cannot be read is never overwritten by `set`, so a mistake cannot silently discard the other keys.

---

## 2. Cloning the AIMS databases locally

All AIMS adapter work runs against a **local copy** of `AIMS_PORTAL_DB` and `AIMS_CORE_DB`, never against production. The full reference, including every failure met while producing the first clone, is `docs/development/aims-local-clone.md`; this is the walkthrough.

### Safety

- The dump **reads production AIMS**. It is read-only and permitted by AD-003, but it loads the vendor database. Run it off-peak.
- The restore target is **always local**. Never point `pg_restore` at any host other than `localhost`.
- No production host, user, or password belongs in the repository or in shell history. Supply them at the prompt.
- Do not use `pg_dumpall`.

### Prerequisites

- PostgreSQL 18 client tools at `C:\Program Files\PostgreSQL\18\bin` (not on `PATH` by default).
- A read-only account on production AIMS with `SELECT` on every table in `public`.
- A local `postgres` superuser.

```powershell
$PG    = "C:\Program Files\PostgreSQL\18\bin"
$OUT   = "D:\Downloads"
$AHOST = "<aims host>"
$AUSER = "<read-only user>"
```

### Step 1 — dump both databases, complete

No restricting flag: no `--data-only`, `--schema-only`, `--section`, `--no-owner`, or `--no-privileges`. Ownership and privileges are dropped at restore time instead, so the archive is a faithful copy.

```powershell
& "$PG\pg_dump.exe" --host=$AHOST --port=5432 --username=$AUSER `
  --dbname=AIMS_PORTAL_DB --format=custom --encoding=UTF8 `
  --quote-all-identifiers --verbose --file="$OUT\AIMS_PORTAL_DB.dump"

& "$PG\pg_dump.exe" --host=$AHOST --port=5432 --username=$AUSER `
  --dbname=AIMS_CORE_DB --format=custom --encoding=UTF8 `
  --quote-all-identifiers --verbose --file="$OUT\AIMS_CORE_DB.dump"
```

If you use pgAdmin's Backup dialog instead, leave **Only data** and **Only schema** unticked. Name the files `.dump`: custom-format archives are binary and cannot be run through `psql -f`.

### Step 2 — verify the archive before restoring

This step was missing from every failed attempt.

```powershell
& "$PG\pg_restore.exe" --list "$OUT\AIMS_PORTAL_DB.dump" |
  Select-String -Pattern " TABLE ", " INDEX ", " CONSTRAINT ", " SEQUENCE " |
  Select-Object -First 15
```

`TABLE`, `INDEX`, and `CONSTRAINT` lines must appear. If only `TABLE DATA` and `SEQUENCE SET` appear, the archive is data-only and cannot rebuild a schema: go back to step 1. A data-only archive fails later with foreign-key violations against an existing schema, or with `relation "public.accesspoint" does not exist` against an empty one — different symptoms, same cause, both announced by `implied no-schema restore` near the top of the log.

### Step 3 — restore into an empty database

```powershell
& "$PG\psql.exe" -U postgres -c 'DROP DATABASE IF EXISTS "AIMS_PORTAL_DB";'
& "$PG\psql.exe" -U postgres -c 'CREATE DATABASE "AIMS_PORTAL_DB";'

& "$PG\pg_restore.exe" -U postgres --dbname=AIMS_PORTAL_DB `
  --no-owner --no-privileges --verbose "$OUT\AIMS_PORTAL_DB.dump"
```

Repeat for `AIMS_CORE_DB`. Restoring into an empty database is what lets `pg_restore` load data before it creates the foreign keys.

Expect **exactly one error** on `AIMS_PORTAL_DB`, on its last step:

```
pg_restore: error: could not execute query: ERROR: password or GSSAPI delegated credentials required
Command was: REFRESH MATERIALIZED VIEW "public"."enddevice_info";
```

Portal reaches into Core through `postgres_fdw` (foreign tables `enddevice`, `slabel`, `station`, and the view `enddevice_info` over them), and on a fresh clone that foreign server still points at production. **Ignore it.** Everything else restored, and the adapter reads both local databases directly rather than through the vendor's FDW. The reference document shows how to repoint it if you ever need the view.

### Step 4 — verify the result

```powershell
& "$PG\psql.exe" -U postgres -d AIMS_PORTAL_DB -c "
SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r') AS tables,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='f') AS foreign_tables;"
```

| Database | Tables | Foreign tables |
| --- | --- | --- |
| `AIMS_PORTAL_DB` | 56 | 3 |
| `AIMS_CORE_DB` | 20 | 0 |

Query `pg_class`, not `information_schema`: the latter filters by the querying role's privileges and returns zero rows for a role with no grants, which looks exactly like missing tables.

### Step 5 — create the read-only role, on both databases

This mirrors the production identity, so the least-privilege proof in the test suite runs locally. Grants are **per database**; run the three `GRANT`s connected to `AIMS_PORTAL_DB`, then again connected to `AIMS_CORE_DB`. Forgetting the second one produces `permission denied for table enddevice` on Core while Portal works.

```sql
CREATE ROLE esl_aims_reader LOGIN PASSWORD '<local only>';

-- run these connected to AIMS_PORTAL_DB, then again connected to AIMS_CORE_DB
GRANT CONNECT ON DATABASE "AIMS_PORTAL_DB" TO esl_aims_reader;   -- "AIMS_CORE_DB" the second time
GRANT USAGE  ON SCHEMA public TO esl_aims_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO esl_aims_reader;
```

Deliberately grant no `INSERT`, `UPDATE`, or `DELETE`.

### Step 6 — point the application at the clone

In `.env` (see `.env.dev.example`):

```
ESL_AIMS_HOST=localhost
ESL_AIMS_PORT=5432
ESL_AIMS_PORTAL_DATABASE=AIMS_PORTAL_DB
ESL_AIMS_PORTAL_USERNAME=esl_aims_reader
ESL_AIMS_CORE_DATABASE=AIMS_CORE_DB
ESL_AIMS_CORE_USERNAME=esl_aims_reader
```

Then store the role's password raw in the bundle and prove it:

```powershell
esl-admin secrets set aims.portal.password --reason "DEV-1 local clone"
esl-admin secrets set aims.core.password   --reason "DEV-1 local clone"
esl-admin check-connections
```

Both `aims-portal` and `aims-core` must report `REACHABLE` as `esl_aims_reader`.

For the integration tests, add the same two connections as URLs — these are the only place the password is percent-encoded:

```
ESL_TEST_AIMS_PORTAL_URL=postgresql+psycopg://esl_aims_reader:<encoded>@localhost:5432/AIMS_PORTAL_DB
ESL_TEST_AIMS_CORE_URL=postgresql+psycopg://esl_aims_reader:<encoded>@localhost:5432/AIMS_CORE_DB
```

Tests that need the clone skip when these are absent, which is what happens in CI.

### Refreshing

Repeat steps 1 to 4. The clone is a snapshot; any AIMS schema change, and any data the parity comparison needs current, requires a fresh dump. Ownership and the `esl_aims_reader` grants survive a `DROP DATABASE` only if you recreate them, so repeat step 5 after a refresh.
