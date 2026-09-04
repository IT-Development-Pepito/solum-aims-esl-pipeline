# Cloning the AIMS databases locally

A local copy of `AIMS_PORTAL_DB` and `AIMS_CORE_DB` is what lets #24 be developed and tested without touching production AIMS (AD-003, NFR-011). This document records the procedure that worked and every failure met on the way to it, so the clone can be rebuilt on a new machine or refreshed after a vendor schema change without repeating the diagnosis.

Read the whole document before starting. Two of the failures below look like different problems and are the same one.

## Safety

- The dump reads **production AIMS**. It is a read-only operation permitted by AD-003, but it puts load on the vendor database. Run it off-peak.
- The restore target is **always a local PostgreSQL**. Never point `pg_restore` at any host other than `localhost`.
- No production host address, username, or password belongs in this document, in the repository, or in a shell history. Supply them at the prompt.
- Do not use `pg_dumpall`. It requires superuser and dumps every role in the cluster.

## What you need

- The local PostgreSQL 18 client tools, which are not on `PATH` by default:
  `C:\Program Files\PostgreSQL\18\bin`. `pg_dump` can read a server older than itself but refuses one newer, so check `SELECT version();` on AIMS if a version error appears.
- A read-only account on production AIMS with `SELECT` on every table in `public`. **VERIFIED 2026-09-04:** the `readonly` account holds CONNECT and schema `USAGE` but not `SELECT` on `public.enddevice`, so check the grant before attempting a refresh; the #24 reader cannot run against production until it exists.
- A local `postgres` superuser for the restore.

Set these once per session in PowerShell:

```powershell
$PG    = "C:\Program Files\PostgreSQL\18\bin"
$OUT   = "D:\Downloads"
$AHOST = "<aims host>"
$APORT = "9010"                # VERIFIED 2026-09-04: production listens here, not 5432
$AUSER = "<read-only user>"    # production uses `readonly`; `esl_aims_reader` is the role
                               # this procedure creates locally, and they are not the same
```

## 1. Dump, complete

Dump each database with **no restricting flag**. Without `--data-only`, `--schema-only`, `--section`, `--no-owner`, or `--no-privileges`, `pg_dump` includes tables, columns, indexes, constraints, sequences, views, functions, triggers, comments, ownership, privileges, and large objects. Ownership and privileges are dropped at restore time instead, so the archive stays a faithful copy.

```powershell
& "$PG\pg_dump.exe" --host=$AHOST --port=$APORT --username=$AUSER `
  --dbname=AIMS_PORTAL_DB --format=custom --encoding=UTF8 `
  --quote-all-identifiers --verbose --file="$OUT\AIMS_PORTAL_DB.dump"

& "$PG\pg_dump.exe" --host=$AHOST --port=$APORT --username=$AUSER `
  --dbname=AIMS_CORE_DB --format=custom --encoding=UTF8 `
  --quote-all-identifiers --verbose --file="$OUT\AIMS_CORE_DB.dump"
```

Name the files `.dump`, not `.sql`. Custom-format archives are binary; a `.sql` extension invites someone to run them through `psql -f`, which fails.

## 2. Verify the archive before restoring

This is the step that was missing from every failed attempt.

```powershell
& "$PG\pg_restore.exe" --list "$OUT\AIMS_PORTAL_DB.dump" |
  Select-String -Pattern " TABLE ", " INDEX ", " CONSTRAINT ", " SEQUENCE " |
  Select-Object -First 15
```

`TABLE`, `INDEX`, and `CONSTRAINT` lines must appear. If only `TABLE DATA` and `SEQUENCE SET` lines appear, the archive is data-only and cannot rebuild a schema. Stop and redo step 1.

## 3. Restore into an empty database

```powershell
& "$PG\psql.exe" -U postgres -c 'DROP DATABASE IF EXISTS "AIMS_PORTAL_DB";'
& "$PG\psql.exe" -U postgres -c 'CREATE DATABASE "AIMS_PORTAL_DB";'

& "$PG\pg_restore.exe" -U postgres --dbname=AIMS_PORTAL_DB `
  --no-owner --no-privileges --verbose "$OUT\AIMS_PORTAL_DB.dump"
```

Repeat for `AIMS_CORE_DB`. `--no-owner --no-privileges` discard references to the vendor's `aims` role, which does not exist locally; structure, indexes, constraints, sequences, and data are unaffected.

Expect **exactly one error** on `AIMS_PORTAL_DB`, on the last step:

```
pg_restore: error: could not execute query: ERROR: password or GSSAPI delegated credentials required
Command was: REFRESH MATERIALIZED VIEW "public"."enddevice_info";
```

That is the FDW dependency described next, and it does not mean the restore failed. Everything else is in place.

## 4. Verify the result

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

These match `docs/hop-jenkins-pipeline/DDL_AIMS_*_DB.sql`. A much smaller count means part of the archive did not restore.

Use `pg_class`, not `information_schema`. `information_schema` filters by the querying role's privileges and returns **zero rows** for a role with no grants, which looks exactly like the tables being absent.

## 5. Portal depends on Core through postgres_fdw

`AIMS_PORTAL_DB` is not self-contained. Its restore log shows:

```
creating EXTENSION "postgres_fdw"
creating SERVER "fdw_aims_core_db"
creating USER MAPPING "USER MAPPING aims SERVER fdw_aims_core_db"
creating FOREIGN TABLE "public.enddevice"
creating FOREIGN TABLE "public.slabel"
creating FOREIGN TABLE "public.station"
creating MATERIALIZED VIEW "public.enddevice_info"
```

`enddevice`, `slabel`, and `station` are Core tables exposed into Portal, and `enddevice_info` is a materialized view over them. On a fresh clone the foreign server still points at production Core, so the view cannot refresh.

**Leave it unpopulated.** The adapter reads both local databases directly rather than through the vendor's FDW, so nothing in this project depends on the view. Repointing the server at the local Core is possible but adds nothing:

```sql
ALTER SERVER fdw_aims_core_db OPTIONS (SET host 'localhost', SET port '5432', SET dbname 'AIMS_CORE_DB');
ALTER ROLE aims WITH PASSWORD '<local only>';
ALTER USER MAPPING FOR aims SERVER fdw_aims_core_db OPTIONS (ADD password '<local only>');
REFRESH MATERIALIZED VIEW public.enddevice_info;
```

**Naming trap.** Portal holds both `end_device`, its own table, and `enddevice`, a foreign table from Core. One underscore apart, different sources. `end_device.station_code` carries the real store code; `end_device_templates.station_code` holds the literal `DEFAULT_STATION_CODE` on every row.

## 6. A read-only role that mirrors production

Create the identity the adapter will use in production, with `SELECT` only, so #24's least-privilege criterion can be exercised locally:

```sql
CREATE ROLE esl_aims_reader LOGIN PASSWORD '<local only>';

GRANT CONNECT ON DATABASE "AIMS_PORTAL_DB" TO esl_aims_reader;
GRANT USAGE  ON SCHEMA public TO esl_aims_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO esl_aims_reader;
```

Run the three `GRANT` statements again connected to `AIMS_CORE_DB`. Grants are per database; forgetting the second one produces `permission denied for table enddevice` on Core while Portal works, which was mistaken for a restore problem once.

Deliberately grant no `INSERT`, `UPDATE`, or `DELETE`. Prove it the way #78 and #24 expect: `esl-admin check-connections --target aims-portal=postgresql://esl_aims_reader@localhost:5432/AIMS_PORTAL_DB#aims.portal.password` after storing the password with `esl-admin secrets set aims.portal.password`.

## Failure modes met, in the order they were met

| Symptom | Cause | Fix |
| --- | --- | --- |
| `command not found` for `pg_dump` | The client tools are not on `PATH`. | Use the full path under `C:\Program Files\PostgreSQL\18\bin`. |
| `server version mismatch` | pgAdmin runs the `pg_dump` bundled with itself, which may be older than the AIMS server. | Point pgAdmin's binary path at the PostgreSQL 18 `bin`, or use the CLI. |
| Restore into an existing schema: `violates foreign key constraint ... Key (station_id)=(...) is not present in table "station"` on nearly every table | The archive is **data-only** (`implied no-schema restore` appears near the top of the log). Data loads in alphabetical order with foreign keys already active, so `accesspoint` fails before `station` exists, and every dependent table fails after it. | Redo the dump with no restricting flag and restore into an **empty** database, so constraints are created after the data. |
| Restore into an empty database: `relation "public.accesspoint" does not exist` on every table, plus `relation "public.<name>_sequence" does not exist` | Same archive, same cause. Emptying the target changed the symptom, not the problem. | Same fix. Verify with `pg_restore --list` first. |
| `password or GSSAPI delegated credentials required` on `REFRESH MATERIALIZED VIEW "public"."enddevice_info"` | Portal's `postgres_fdw` server points at production Core, and the restored `aims` user mapping has no password because production trusts the connection by `pg_hba.conf`. | Ignore it, or repoint the server locally as in section 5. |
| `password authentication failed for user "aims"` after repointing the FDW | The local `aims` role was created without a password and the mapping has none. | Give the role a local password and add it to the user mapping, section 5. |
| `information_schema.columns` returns no rows for a column that exists | `information_schema` filters by privilege; the querying role had no grants yet. | Query `pg_catalog` (`pg_attribute`, `pg_class`), or grant `SELECT` first. |
| `permission denied for table enddevice` on Core while Portal works | The `GRANT` statements were run on Portal only. | Run them on `AIMS_CORE_DB` too. |

## Refreshing the clone

Repeat sections 1 to 4. The clone is a snapshot: any schema change AIMS ships, and any data the parity comparison needs to be current, requires a fresh dump. There is no defined refresh cadence yet; it becomes necessary when the `page` question recorded in `PROGRESS.md` is settled and shadow-mode comparison starts.
