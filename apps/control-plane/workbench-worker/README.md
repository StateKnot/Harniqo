# Hermes workbench worker

`startHermesWorker` consumes the dedicated workbench queue and reconciles persisted accepted Runs and active assignments. Pass caller-owned Kysely and pg-boss connections to the same database, the trusted `OpenAuthorizedExecution` connector, and the exact organization/project/principal scopes assigned to this worker.

Startup requires the separate non-owner `harniqo_worker` role, no superuser or RLS bypass privileges, and the existing pg-boss schema version 46 partition. Apply the existing store, budget/queue, and Hermes execution migrations first. The worker uses their scoped permissions without running migrations.

Each fetched job is validated against its Run ID and configured scope. The worker acknowledges that exact queue activation only after the assignment transaction commits or the Run is already terminal. Accepted Runs are also recovered directly from PostgreSQL so a process exit after fetching a queue job cannot leave an unassigned Run waiting for queue expiry. Keyset pages and round-robin scopes keep recovery bounded. Native dispatch and lost-receipt behavior remain controlled by the existing durable execution coordinator.

Default concurrency is two operations, with a one-second pause between passes. Configure one through eight concurrent operations and a pause from 100 through 60,000 milliseconds. Up to 64 distinct scopes can be configured. Use bounded database connection and statement timeouts, as in the control-plane runtime. `onIssue` receives stable error codes and optional Run IDs only. It must report operational failures without throwing or logging credentials.

Call `stop()` before closing the caller's queue and database connections. It aborts pending connector requests, waits for the current bounded pass, and can be awaited more than once. Persisted executions continue to be reconciled after a new worker starts. Actual runtime shutdown and stopping evidence are handled by the trusted connector and execution coordinator.

The queue supervisor and retention maintenance are separate from this least-privileged worker. Active or expired queue jobs do not determine whether a native process has stopped. The worker never releases a workspace or budget hold based on a queue ACK.

Run the integration test against a disposable PostgreSQL 17 database named `harniqo_workbench_worker_test` on loopback. Set the administrative connection URL in private `HARNIQO_TEST_POSTGRES_URL`, then run from the repository root.

```sh
node --test apps/control-plane/workbench-worker/index.test.ts
```

The regression uses real PostgreSQL and pg-boss plus a controlled execution authority. It covers restarting after queue acknowledgement, recovering an accepted Run with an orphaned active queue claim, scoped dispatch denial, and idempotent shutdown. A real Hermes deployment and actual model call require their own acceptance.
