import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { PgBoss, getConstructionPlans } from 'pg-boss';
import { acceptRun, lookupRunSubmission } from '../workbench-store/index.ts';
import { createWorkbenchQueue, startWorkbenchProducer, WORKBENCH_QUEUE_SCHEMA, WORKBENCH_RUN_QUEUE } from './index.ts';
import type { WorkbenchRunJob } from './index.ts';

function database(connectionString: string): Kysely<unknown> {
  return new Kysely({ dialect: new PostgresDialect({
    pool: new Pool({ connectionString, max: 3, connectionTimeoutMillis: 3000, statement_timeout: 8000 }),
  }) });
}

test('PostgreSQL 17 commits or rolls back authentic pg-boss jobs with the workbench Run and budget', { timeout: 20_000 }, async () => {
  const adminUrl = new URL(process.env.HARNIQO_TEST_POSTGRES_URL!);
  assert.equal(adminUrl.protocol, 'postgresql:');
  assert.equal(adminUrl.hostname, '127.0.0.1');
  assert.equal(adminUrl.pathname, '/harniqo_workbench_queue_test');
  assert.equal(adminUrl.search, '');
  const admin = database(adminUrl.href);
  function roleUrl(role: string): URL {
    const url = new URL(adminUrl.href);
    url.username = role;
    url.password = randomBytes(32).toString('hex');
    return url;
  }
  const ownerUrl = roleUrl('harniqo_migrator');
  const producerUrl = roleUrl('harniqo_control');
  const workerUrl = roleUrl('harniqo_worker');
  const owner = database(ownerUrl.href);
  const control = database(producerUrl.href);
  const execution = database(workerUrl.href);
  let producer: PgBoss | undefined;
  let worker: PgBoss | undefined;
  const organizationId = randomUUID();
  const users = [1, 2].map(() => ({
    scope: { organizationId, projectId: randomUUID(), principalId: randomUUID() },
    sessionId: randomUUID(), key: randomUUID(),
    request: {
      deployment_revision_id: randomUUID(), configuration_revision_id: randomUUID(), input_snapshot_id: randomUUID(),
      budget_policy_id: randomUUID(), prompt: 'synthetic prompt', required_capabilities: [],
    },
  }));
  const first = users[0]!;
  const second = users[1]!;
  async function counters() {
    const result = await sql<{ runs: number; holds: number; jobs: number; events: number; admitted: number }>`
      SELECT (SELECT count(*) FROM harniqo.runs)::int AS runs,
        (SELECT count(*) FROM harniqo.workbench_budget_reservations)::int AS holds,
        (SELECT count(*) FROM harniqo_queue.job)::int AS jobs,
        (SELECT count(*) FROM harniqo.run_events)::int AS events,
        (SELECT admitted_runs FROM harniqo.workbench_organization_budgets WHERE organization_id = ${organizationId}) AS admitted
      `.execute(owner);
    return result.rows[0];
  }
  try {
    const version = await sql<{ version: number }>`SELECT current_setting('server_version_num')::int AS version`.execute(admin);
    assert.ok(version.rows[0]!.version >= 170000 && version.rows[0]!.version < 180000);
    for (const url of [ownerUrl, producerUrl, workerUrl]) {
      await sql`CREATE ROLE ${sql.id(url.username)} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD ${sql.lit(url.password)}`.execute(admin);
    }
    await sql`GRANT CREATE ON DATABASE harniqo_workbench_queue_test TO harniqo_migrator`.execute(admin);
    await sql.raw(getConstructionPlans(WORKBENCH_QUEUE_SCHEMA)).execute(owner);
    // Runtime checks should neither create a missing queue nor leave a producer alive.
    await assert.rejects(startWorkbenchProducer(producerUrl.href), { code: 'UPSTREAM_UNAVAILABLE' });
    const migrations = await Promise.all([
      readFile(new URL('../workbench-store/001-first-run.sql', import.meta.url), 'utf8'),
      readFile(new URL('./003-workbench-queue.sql', import.meta.url), 'utf8'),
    ]);
    await owner.transaction().execute(async (tx) => {
      for (const migration of migrations) await sql.raw(migration).execute(tx);
      // The execution Module supplies the Run read grant. This isolates the budget policy test.
      await sql`GRANT SELECT ON harniqo.runs TO harniqo_worker`.execute(tx);
      await sql`CREATE POLICY fixture_worker_read ON harniqo.runs FOR SELECT TO harniqo_worker USING (
        organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
        AND principal_id = nullif(current_setting('app.principal_id', true), '')::uuid
      )`.execute(tx);
      await sql`INSERT INTO harniqo.workbench_organization_budgets (organization_id, run_limit, concurrent_limit)
        VALUES (${organizationId}, 2, 1)`.execute(tx);
      for (const { scope, sessionId, request } of users) {
        const { projectId, principalId } = scope;
        await sql`INSERT INTO harniqo.project_memberships VALUES (${organizationId}, ${projectId}, ${principalId}, true, true)`
          .execute(tx);
        await sql`INSERT INTO harniqo.run_targets (
          organization_id, project_id, principal_id, session_id, workspace_id, deployment_id,
          deployment_name, deployment_revision_id, configuration_revision_id, input_snapshot_id, budget_policy_id,
          execution_target_id
        ) VALUES (${organizationId}, ${projectId}, ${principalId}, ${sessionId}, ${randomUUID()}, ${randomUUID()},
          'Hermes test', ${request.deployment_revision_id}, ${request.configuration_revision_id},
          ${request.input_snapshot_id}, ${request.budget_policy_id}, ${randomUUID()})`.execute(tx);
        await sql`INSERT INTO harniqo.workbench_project_budgets
          (organization_id, project_id, budget_policy_id, run_limit, concurrent_limit)
          VALUES (${organizationId}, ${projectId}, ${request.budget_policy_id}, 2, 1)`.execute(tx);
      }
    });
    producer = await startWorkbenchProducer(producerUrl.href);
    const reserveAndEnqueue = createWorkbenchQueue(producer);
    await assert.rejects(acceptRun(control, first.scope, first.sessionId, first.key, first.request, async (tx, run) => {
      await reserveAndEnqueue(tx, run);
      assert.deepEqual(await counters(), { runs: 0, holds: 0, jobs: 0, events: 0, admitted: 0 });
      throw new Error('forced rollback after pg-boss send');
    }), /forced rollback/);
    assert.deepEqual(await counters(), { runs: 0, holds: 0, jobs: 0, events: 0, admitted: 0 });
    await assert.rejects(lookupRunSubmission(control, first.scope, first.sessionId, first.key), { code: 'NOT_FOUND' });

    const same = () => acceptRun(control, first.scope, first.sessionId, first.key, first.request, reserveAndEnqueue);
    const competing = () => acceptRun(control, second.scope, second.sessionId, second.key, second.request, reserveAndEnqueue);
    const reachedQueue = Promise.withResolvers<void>();
    const winner = acceptRun(control, first.scope, first.sessionId, first.key, first.request, async (tx, run) => {
      try {
        await reserveAndEnqueue(tx, run);
        reachedQueue.resolve();
        await sql`SELECT pg_sleep(0.1)`.execute(tx);
      } catch (error) {
        reachedQueue.reject(error);
        throw error;
      }
    });
    const [receipt, retry] = await Promise.all([
      winner, reachedQueue.promise.then(same), reachedQueue.promise.then(() =>
        assert.rejects(competing(), { code: 'QUOTA_EXCEEDED' })),
    ]);
    assert.deepEqual(retry, receipt);
    assert.deepEqual(await counters(), { runs: 1, holds: 1, jobs: 1, events: 1, admitted: 1 });
    await sql`UPDATE harniqo.workbench_organization_budgets SET run_limit = 1 WHERE organization_id = ${organizationId}`.execute(owner);
    await assert.rejects(competing(), { code: 'BUDGET_EXCEEDED' });
    await assert.rejects(sql`SELECT * FROM harniqo.workbench_budget_reservations`.execute(control), { code: '42501' });

    worker = new PgBoss({ connectionString: workerUrl.href, schema: WORKBENCH_QUEUE_SCHEMA, migrate: false,
      createSchema: false, schedule: false, supervise: false, registerInstance: false });
    await worker.start();
    const [job] = await worker.fetch<WorkbenchRunJob>(WORKBENCH_RUN_QUEUE);
    assert.ok(job);
    assert.equal(job.id, receipt.run_id);
    assert.deepEqual(job.data, { ...first.scope, runId: receipt.run_id, commandId: receipt.command_id });
    assert.equal((await worker.complete(WORKBENCH_RUN_QUEUE, receipt.run_id)).affected, 1);

    // The queue ACK must never be mistaken for proof that the Run is terminal.
    function release(projectId: string) {
      return execution.transaction().execute(async (tx) => {
        await sql`SELECT set_config('app.organization_id', ${organizationId}, true),
          set_config('app.project_id', ${projectId}, true),
          set_config('app.principal_id', ${first.scope.principalId}, true)`.execute(tx);
        return sql`UPDATE harniqo.workbench_budget_reservations SET finalized_at = statement_timestamp()
          WHERE run_id = ${receipt.run_id}`.execute(tx);
      });
    }
    assert.equal((await release(second.scope.projectId)).numAffectedRows, 0n);
    await assert.rejects(release(first.scope.projectId), { code: '42501' });
    await sql`UPDATE harniqo.runs SET state = 'failed', ended_at = statement_timestamp() WHERE id = ${receipt.run_id}`.execute(owner);
    assert.equal((await release(first.scope.projectId)).numAffectedRows, 1n);
    assert.equal((await release(first.scope.projectId)).numAffectedRows, 0n);
    await assert.rejects(competing(), { code: 'BUDGET_EXCEEDED' });
    await sql`UPDATE harniqo.workbench_organization_budgets SET run_limit = 2 WHERE organization_id = ${organizationId}`.execute(owner);
    assert.notEqual((await competing()).run_id, receipt.run_id);
    assert.deepEqual(await counters(), { runs: 2, holds: 2, jobs: 2, events: 2, admitted: 2 });
  } finally {
    await Promise.allSettled([
      producer?.stop({ graceful: false }), worker?.stop({ graceful: false }),
      admin.destroy(), owner.destroy(), control.destroy(), execution.destroy(),
    ]);
  }
});
