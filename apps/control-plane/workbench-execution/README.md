# Hermes workbench execution

This module takes an authorized workbench command through a durable assignment, one native start, and a confirmed reply. Run the store, access, and queue migrations before applying `004-hermes-execution.sql` as the same migration owner. The trusted control worker uses the separate non-owner `harniqo_worker` role with `NOSUPERUSER NOBYPASSRLS`.

- Provision an exact organization, project, principal, target, deployment revision and workspace binding for the dedicated Hermes runtime. Only a trusted deployment authority can enable it or refresh `ready_until` after checking the live runtime. `harniqo_control` can read readiness for its authorized project and never updates or grants it.
- `assignHermesJob` verifies the durable command, current project membership, frozen binding and budget hold, then records an Attempt and exclusive Workspace lease. A queue consumer can acknowledge the job after this transaction commits. Call `listHermesAssignments` for each scope served by that consumer to recover pending assignments independently of the queue receipt.
- `executeHermesAssignment` requires an authority that verifies the exact active Attempt and isolated native runtime, confirms any existing session, and supplies the scoped Hermes Adapter. Before each native mutation the coordinator checks the live grant. One PostgreSQL transaction durably reserves the right to create a session and submit the native instruction. A lost receipt keeps the lease and budget hold in `reconciling`. There is no automatic second native submission.
- The terminal transaction checks the exact native Run, a stopped writer, and any reported served-model evidence for replies. It commits the persisted reply, ordered state events, workspace release and budget hold release together. Committed events and snapshots use the existing canonical read contracts.

The injected authority is part of the production execution boundary. It must come from an approved isolated Runner or dedicated external deployment, check the live workspace and lease, and record its own terminal proof and served-model receipt. Use neither a general Hermes host nor the old qualification container to provide that authority. The [mTLS connector](../workbench-connector/README.md) and [background worker](../workbench-worker/README.md) connect this flow. Missing served-model metadata stays unknown and does not change a confirmed execution lifecycle result. Provider attribution, grant renewal and recovery of lost native start receipts remain to be accepted.

Run the integration test against an empty disposable PostgreSQL 17 instance on loopback. Use the database name `harniqo_workbench_execution_test` and set its administrative URL in `HARNIQO_TEST_POSTGRES_URL`.

```sh
node --test apps/control-plane/workbench-execution/index.test.ts
```

The test uses PostgreSQL, pg-boss and distinct runtime roles with a controlled Hermes authority. It covers concurrent starts, authorization revocation, workspace conflict, lost receipt and canonical terminal reads. The controlled authority verifies coordinator behavior without using an external model.
