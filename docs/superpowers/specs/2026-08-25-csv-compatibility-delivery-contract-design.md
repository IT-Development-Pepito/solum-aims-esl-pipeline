# CSV Compatibility Delivery Contract Design

## Status

**PROPOSED — approved direction pending document review.** This design defines the temporary automated compatibility delivery boundary for SKU CSV output. It does not authorize a production cutover or direct AIMS database access.

## Context and decision

The supplied Hop evidence verifies that the legacy process writes `ESL_SKU_<store>.csv`, but no downstream consumer or acknowledgement mechanism has been identified. A filesystem file existing is not proof of consumption. The replacement must retain automated behavior while preserving durable, queryable proof of completion.

The approved direction is an **automated, ACL-restricted file-drop handshake**. It needs no target HTTP API and therefore makes no HTTP authentication assumption. The replacement's internal operations API remains authenticated under FR-029; this delivery contract does not weaken that boundary.

## Scope and non-goals

In scope:

- Automated delivery and acknowledgement for the temporary SKU CSV compatibility adapter.
- Durable target-state records for each delivery and acknowledgement.
- Configuration of the destination, consumer identity, polling interval, timeout, retention, and contract version.

Out of scope:

- Treating CSV as workflow state, comparison evidence, or an AIMS interface.
- AIMS writes, SQL Server writes, manual file handling, unaudited re-sends, and a public or unauthenticated operations API.
- Defining the business rules that populate CSV content.

## Contract

### Identities and access

The target service and consumer run under distinct non-interactive Windows service identities. The configured delivery root grants the target identity write-only access to `outbox/` and read access to `ack/`; the consumer receives read access to `outbox/` and write-only access to `ack/`. Administrators retain audited break-glass access. The paths and identities are environment configuration, never repository values.

### Automatic producer protocol

For every eligible delivery, the scheduler creates one immutable `delivery_id` and persists the intended payload hash, row count, store, execution ID, and contract version before writing files.

1. Write the CSV to a producer-private temporary filename below `outbox/`.
2. Atomically rename it to `outbox/<delivery_id>.csv`.
3. Atomically write `outbox/<delivery_id>.ready.json` containing `delivery_id`, execution ID, store, row count, SHA-256, creation timestamp, and contract version.
4. Poll `ack/<delivery_id>.ack.json` on the configured schedule until acknowledgement or timeout.

The consumer must ignore partial files and process only CSV files with a matching ready manifest. Publishing a ready manifest is a submission event, not completion.

### Automatic consumer acknowledgement

After successful processing, the consumer atomically writes `ack/<delivery_id>.ack.json`. The acknowledgement contains the same `delivery_id`, SHA-256, row count, consumer timestamp, contract version, and one of:

- `ACCEPTED`; or
- `REJECTED` with a safe reason code.

The target accepts an acknowledgement only when the delivery ID, SHA-256, row count, and contract version match the durable submission record. A matching `ACCEPTED` transitions the action to acknowledged. A rejection, malformed/mismatched acknowledgement, or configured timeout transitions it to `UNRESOLVED` and raises an operator-visible event; it never triggers a blind automatic re-send.

### Idempotency, retention, and recovery

`delivery_id` is the idempotency key. Replaying a workflow reuses the existing acknowledged delivery or creates a separately audited new delivery only through an authorized replay decision. The target retains the CSV, manifest, acknowledgement, and durable records for configured retention. Cleanup is allowed only after durable acknowledgement or an explicitly recorded unresolved disposition.

After a restart, the scheduler resumes acknowledgement polling from durable state. It does not infer completion from the presence of a CSV file.

## Data flow

```text
scheduled shadow/compatibility workflow
  -> persist intended delivery and hash
  -> CSV + ready manifest (atomic publish)
  -> consumer automatically processes file
  -> matching acknowledgement file
  -> durable ACKNOWLEDGED or UNRESOLVED state
```

## Acceptance and traceability

| Requirement | Acceptance evidence |
| --- | --- |
| FR-021, FR-022 | Delivery and acknowledgement are queryable by execution ID and delivery ID, including automated timeout/rejection outcomes. |
| FR-027 | Canonical comparisons and hashes are persisted independently of CSV files. |
| FR-028 | A matching automated acknowledgement is required before delivery completion; consumer identity, retention, and decommission gate are documented. |
| FR-029 | Human operations remain authenticated; the automated file contract has no public HTTP surface. |
| NFR-006, NFR-007, NFR-009, NFR-012 | Tests cover atomic publish, valid/mismatched acknowledgement, timeout/restart recovery, idempotency, ACL configuration validation, and environment-isolated configuration. |

## Open acceptance gate

Before enabling the adapter outside a controlled non-production test, the CSV consumer owner must validate the filename/schema, service identity/ACLs, acknowledgement writer, timeout, retention, and rollback behavior. Until then, CSV delivery remains disabled and shadow runs record `INTENDED` actions only.
