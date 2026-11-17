# First Hermes Run admission

The desktop Run store accepts an injected `createWorkbenchQueue(producer)` callback. It reserves both organization and project capacity and sends the pg-boss job using `fromKysely(tx)`. The Run, Command, first event, budget reservation and queue job commit together. The queue carries only scope, Run and Command identifiers. The job ID is the Run ID. [pg-boss transaction adapters](https://pgboss.io/api/adapters)

Use PostgreSQL 17 with the Run store and queue in the same database. Provision `harniqo_control` and `harniqo_worker` as separate non-owner, `NOSUPERUSER NOBYPASSRLS` roles. Apply the store and identity migrations before `003-workbench-queue.sql`. Install the pinned pg-boss 12.38.2 construction plan into the dedicated `harniqo_queue` schema as the same migration owner. Generate it from the locked workspace dependency. [Pinned upstream release and schema version 46](https://github.com/timgit/pg-boss/releases/tag/12.38.2)

```sh
pnpm --filter @harniqo/control-plane exec node --input-type=module -e \
  'import { getConstructionPlans } from "pg-boss"; console.log(getConstructionPlans("harniqo_queue"));'
```

Apply the generated SQL with `psql` and `ON_ERROR_STOP=1`. Apply `003-workbench-queue.sql` in one transaction. It requires an empty dedicated queue schema and creates the single partitioned queue. Application startup only reads the expected schema version and queue metadata. Producer and worker can access their assigned queue partition. Keep operating credentials and provisioned project data in private deployment configuration.

Provision one row per allowed organization in `workbench_organization_budgets` and one row per allowed project in `workbench_project_budgets`. Use the exact budget policy UUID already bound to the target session. `run_limit` is the cumulative admission allowance and `concurrent_limit` is the cap on outstanding reservations. Concurrent submissions lock the organization then the project. A missing policy or exhausted allowance rejects admission. A verified Run terminal transition releases only the concurrent reservation, leaving the cumulative admission charged. Currency accounting and provider consumption stay pending under M2-08.

Both application roles run with queue migration, automatic scheduling and supervision disabled. A separate queue maintainer is required to reconcile expired claims and maintain retention for production. M2-05 also covers recovery of unresolved Runs when queue retries are exhausted.

Use a disposable PostgreSQL 17 database named `harniqo_workbench_queue_test` on loopback to run the integration test. Supply its administrative URL through `HARNIQO_TEST_POSTGRES_URL` in private environment configuration. The test provisions roles and schemas in that database.

```sh
node --test apps/control-plane/workbench-queue/index.test.ts
```
