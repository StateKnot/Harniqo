import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { setTimeout as sleep } from 'node:timers/promises';
import { test } from 'node:test';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { getConstructionPlans, PgBoss } from 'pg-boss';
import type { HermesJob, OpenAuthorizedExecution } from '../workbench-execution/index.ts';
import { createWorkbenchQueue, startWorkbenchProducer, WORKBENCH_QUEUE_SCHEMA, WORKBENCH_RUN_QUEUE } from '../workbench-queue/index.ts';
import { acceptRun, readRunSnapshot } from '../workbench-store/index.ts';
import { startHermesWorker } from './index.ts';
import type { HermesWorker, WorkerIssue } from './index.ts';

function database(url: URL) {
  return new Kysely<unknown>({ dialect: new PostgresDialect({
    pool: new Pool({ connectionString: url.href, max: 3, connectionTimeoutMillis: 3000, statement_timeout: 5000 }),
  }) });
}

async function eventually(condition: () => Promise<boolean>) {
  const deadline = Date.now() + 6000;
  while (!await condition()) {
    assert.ok(Date.now() < deadline, 'worker did not reach the expected durable state');
    await sleep(25);
  }
}

test('PostgreSQL worker resumes ACKed assignments and accepted Runs whose queue claim was lost', { timeout: 25_000 }, async () => {
  const adminUrl = new URL(process.env.HARNIQO_TEST_POSTGRES_URL!);
  assert.equal(adminUrl.protocol, 'postgresql:');
  assert.equal(adminUrl.hostname, '127.0.0.1');
  assert.equal(adminUrl.pathname, '/harniqo_workbench_worker_test');
  assert.equal(adminUrl.search, '');
  const roleUrls = ['harniqo_migrator', 'harniqo_control', 'harniqo_worker'].map(username => {
    const url = new URL(adminUrl);
    url.username = username;
    url.password = randomBytes(32).toString('hex');
    return url;
  });
  const [ownerUrl, controlUrl, workerUrl] = roleUrls as [URL, URL, URL];
  const admin = database(adminUrl);
  const owner = database(ownerUrl);
  const control = database(controlUrl);
  const worker = database(workerUrl);
  const scope = { organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID() };
  const session = randomUUID();
  const workspace = randomUUID();
  const target = randomUUID();
  const request = {
    deployment_revision_id: randomUUID(), configuration_revision_id: randomUUID(), input_snapshot_id: randomUUID(),
    budget_policy_id: randomUUID(), prompt: 'worker recovery test', required_capabilities: [],
  };
  let producer: PgBoss | undefined;
  let consumer: PgBoss | undefined;
  let loop: HermesWorker | undefined;
  const issues: WorkerIssue[] = [];
  let starts = 0;
  let allowTerminal = false;
  const open: OpenAuthorizedExecution = async identity => {
    const nativeRunId = 'run_' + identity.runId.replaceAll('-', '');
    return {
      identity,
      async assertLive() { return true; },
      async hasNativeSession() { return true; },
      async confirmTerminal(id) {
        return allowTerminal ? { nativeRunId: id, writerExited: true, servedModel: 'fixture-model' } : null;
      },
      hermes: {
        async attach(nativeSessionId) { return { nativeSessionId }; },
        async start(input) {
          assert.equal(input.idempotencyKey, identity.attemptId);
          starts++;
          return { nativeRunId, replayed: false };
        },
        async inspect(id, nativeSessionId) {
          assert.equal(id, nativeRunId);
          return allowTerminal ? { nativeRunId: id, nativeSessionId, status: 'completed', output: 'persisted worker reply' }
            : { nativeRunId: id, nativeSessionId, status: 'running' };
        },
        async readReply() { return 'persisted worker reply'; },
      },
    };
  };
  const state = async (runId: string) => (await readRunSnapshot(control, scope, runId) as { run: { state: string } }).run.state;
  try {
    const version = await sql<{ version: number }>`SELECT current_setting('server_version_num')::int AS version`.execute(admin);
    assert.ok(version.rows[0]!.version >= 170000 && version.rows[0]!.version < 180000);
    for (const role of roleUrls) {
      await sql`CREATE ROLE ${sql.id(role.username)} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD ${sql.lit(role.password)}`.execute(admin);
    }
    await sql`GRANT CREATE ON DATABASE harniqo_workbench_worker_test TO harniqo_migrator`.execute(admin);
    await sql.raw(getConstructionPlans(WORKBENCH_QUEUE_SCHEMA)).execute(owner);
    await owner.transaction().execute(async tx => {
      for (const file of ['../workbench-store/001-first-run.sql', '../workbench-queue/003-workbench-queue.sql',
        '../workbench-execution/004-hermes-execution.sql']) {
        await sql.raw(await readFile(new URL(file, import.meta.url), 'utf8')).execute(tx);
      }
      await sql`INSERT INTO harniqo.project_memberships VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, true, true)`.execute(tx);
      await sql`INSERT INTO harniqo.run_targets VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${session}, ${workspace}, ${randomUUID()},
          'Hermes', ${request.deployment_revision_id}, ${request.configuration_revision_id}, ${request.input_snapshot_id},
          ${request.budget_policy_id}, ${target}, true)`.execute(tx);
      await sql`INSERT INTO harniqo.workbench_organization_budgets VALUES (${scope.organizationId}, 10, 1, 0)`.execute(tx);
      await sql`INSERT INTO harniqo.workbench_project_budgets VALUES
        (${scope.organizationId}, ${scope.projectId}, ${request.budget_policy_id}, 10, 1, 0)`.execute(tx);
      await sql`INSERT INTO harniqo.hermes_execution_bindings VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${target}, ${request.deployment_revision_id},
          ${workspace}, ${randomUUID()}, ${randomUUID()}, ${randomUUID()}, 1, true, statement_timestamp() + interval '3 minutes')`.execute(tx);
    });
    producer = await startWorkbenchProducer(controlUrl.href);
    consumer = new PgBoss({ connectionString: workerUrl.href, schema: WORKBENCH_QUEUE_SCHEMA, migrate: false,
      createSchema: false, supervise: false, schedule: false, registerInstance: false });
    consumer.on('error', () => assert.fail('queue connection error'));
    await consumer.start();
    const options = { db: worker, queue: consumer, scopes: [scope], open, concurrency: 2, pollIntervalMs: 100,
      onIssue: (issue: WorkerIssue) => issues.push(issue) };
    await assert.rejects(startHermesWorker({ ...options, db: owner }), { code: 'worker_unavailable' });
    const reserve = createWorkbenchQueue(producer);
    const first = await acceptRun(control, scope, session, randomUUID(), request, reserve);
    loop = await startHermesWorker(options);
    await eventually(async () => await state(first.run_id) === 'running');
    await loop.stop();
    assert.equal(starts, 1);
    const queueEntry = await sql<{ state: string }>`SELECT state FROM harniqo_queue.job WHERE id = ${first.run_id}`.execute(owner);
    assert.equal(queueEntry.rows[0]?.state, 'completed');
    allowTerminal = true;
    loop = await startHermesWorker(options);
    await eventually(async () => await state(first.run_id) === 'succeeded');
    await loop.stop();
    assert.equal(starts, 1, 'restart must query the persisted native Run rather than submit a second one');

    const second = await acceptRun(control, scope, session, randomUUID(), request, reserve);
    const [orphaned] = await consumer.fetch<HermesJob>(WORKBENCH_RUN_QUEUE);
    assert.equal(orphaned?.id, second.run_id);
    loop = await startHermesWorker(options);
    await eventually(async () => await state(second.run_id) === 'succeeded');
    await loop.stop();
    assert.equal(starts, 2, 'recover the accepted Run despite an active queue claim from another process');

    const third = await acceptRun(control, scope, session, randomUUID(), request, reserve);
    const wrongScope = { ...scope, projectId: randomUUID() };
    loop = await startHermesWorker({ ...options, scopes: [wrongScope] });
    await eventually(async () => issues.some(issue => issue.code === 'scope_not_served' && issue.runId === third.run_id));
    await loop.stop();
    assert.equal(starts, 2, 'an unserved project must never reach the native authority');
    assert.equal(await state(third.run_id), 'accepted');
    loop = await startHermesWorker(options);
    await eventually(async () => await state(third.run_id) === 'succeeded');
    await Promise.all([loop.stop(), loop.stop()]);
    assert.equal(starts, 3);
    assert.deepEqual(issues.filter(issue => issue.code !== 'scope_not_served'), []);
  } finally {
    await loop?.stop();
    await consumer?.stop();
    await producer?.stop();
    await Promise.all([admin.destroy(), owner.destroy(), control.destroy(), worker.destroy()]);
  }
});
