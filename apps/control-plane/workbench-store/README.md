# First Hermes run persistence

This store persists a provisioned Hermes target, a principal's submission receipt, its Run and the first committed event. The normal caller resolves a verified OIDC issuer and subject to an internal principal ID, then passes the exact route scope and a schema-validated request.

Apply `001-first-run.sql` transactionally as the migration owner to a new Harniqo PostgreSQL database. Provision `harniqo_control` first as a separate `NOSUPERUSER NOBYPASSRLS` role with no migration role membership. The migration owner provisions project membership and an enabled target with a private session and isolated workspace. A changed target is provisioned with fresh binding IDs. Disable the old row to retain existing receipts and Run history.

`acceptRun` owns its transaction. The caller must implement the required callback using that same Kysely transaction for budget reservation and the pg-boss transactional adapter. Propagate quota and enqueue errors so they roll back command acceptance. A same-key replay checks current membership and returns the existing receipt. A distinct submission for a Session with a nonterminal Run returns `QUOTA_EXCEEDED` before invoking the callback.

This module covers storage and first-event retrieval. Queue consumption, current execution authorization, workspace writer leases, native result persistence and desktop-to-Hermes acceptance remain follow-up work. Run snapshots contain empty pending-interaction lists until the interaction store is wired.

Run the PostgreSQL regression against a disposable, otherwise empty PostgreSQL 17 instance on loopback with a database named `harniqo_workbench_test`. Supply its administrative connection URL through private environment configuration.

```sh
node --test apps/control-plane/workbench-store/index.test.ts
```

The test exercises forced RLS, scoped membership, resource isolation, concurrent submission, readback and rollback with a real transaction-side probe row. Production pg-boss, budget policy and native execution require separate acceptance.
