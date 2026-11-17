import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { contractValidator } from '@harniqo/contracts/validation';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { PgBoss, getConstructionPlans } from 'pg-boss';
import { createWorkbenchQueue, startWorkbenchProducer, WORKBENCH_QUEUE_SCHEMA, WORKBENCH_RUN_QUEUE } from '../workbench-queue/index.ts';
import { acceptRun, readRunEvents, readRunSnapshot } from '../workbench-store/index.ts';
import { assignHermesJob, executeHermesAssignment, listHermesAssignments } from './index.ts';
import type { HermesJob, OpenAuthorizedExecution } from './index.ts';

function database(url: URL) {
  return new Kysely<unknown>({ dialect: new PostgresDialect({
    pool: new Pool({ connectionString: url.href, max: 4, connectionTimeoutMillis: 3000, statement_timeout: 8000 }),
  }) });
}

test('assigned workbench Runs make at most one start and commit a confirmed Hermes reply', { timeout: 25_000 }, async () => {
  const adminUrl = new URL(process.env.HARNIQO_TEST_POSTGRES_URL!);
  assert.equal(adminUrl.protocol, 'postgresql:');
  assert.equal(adminUrl.hostname, '127.0.0.1');
  assert.equal(adminUrl.pathname, '/harniqo_workbench_execution_test');
  assert.equal(adminUrl.search, '');
  const connection = (username: string) => {
    const url = new URL(adminUrl.href);
    url.username = username;
    url.password = randomBytes(32).toString('hex');
    return url;
  };
  const ownerUrl = connection('harniqo_migrator');
  const controlUrl = connection('harniqo_control');
  const workerUrl = connection('harniqo_worker');
  const admin = database(adminUrl);
  const owner = database(ownerUrl);
  const control = database(controlUrl);
  const worker = database(workerUrl);
  let producer: PgBoss | undefined;
  let consumer: PgBoss | undefined;
  const scope = { organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID() };
  const session = randomUUID();
  const secondarySession = randomUUID();
  const workspace = randomUUID();
  const target = randomUUID();
  const runtime = randomUUID();
  const request = {
    deployment_revision_id: randomUUID(), configuration_revision_id: randomUUID(), input_snapshot_id: randomUUID(),
    budget_policy_id: randomUUID(), prompt: 'Hermes test', required_capabilities: [],
  };
  try {
    const version = await sql<{ number: number }>`SELECT current_setting('server_version_num')::int AS number`.execute(admin);
    assert.ok(version.rows[0]!.number >= 170000 && version.rows[0]!.number < 180000);
    for (const url of [ownerUrl, controlUrl, workerUrl]) {
      await sql`CREATE ROLE ${sql.id(url.username)} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD ${sql.lit(url.password)}`.execute(admin);
    }
    await sql`GRANT CREATE ON DATABASE harniqo_workbench_execution_test TO harniqo_migrator`.execute(admin);
    await sql.raw(getConstructionPlans(WORKBENCH_QUEUE_SCHEMA)).execute(owner);
    await owner.transaction().execute(async tx => {
      for (const file of ['../workbench-store/001-first-run.sql', '../workbench-queue/003-workbench-queue.sql',
        './004-hermes-execution.sql']) {
        await sql.raw(await readFile(new URL(file, import.meta.url), 'utf8')).execute(tx);
      }
      await sql`INSERT INTO harniqo.project_memberships VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, true, true)`.execute(tx);
      for (const id of [session, secondarySession]) {
        await sql`INSERT INTO harniqo.run_targets VALUES (
          ${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${id}, ${workspace}, ${randomUUID()},
          'Hermes', ${request.deployment_revision_id}, ${request.configuration_revision_id}, ${request.input_snapshot_id},
          ${request.budget_policy_id}, ${target}, true)`.execute(tx);
      }
      await sql`INSERT INTO harniqo.workbench_organization_budgets VALUES (${scope.organizationId}, 10, 3, 0)`.execute(tx);
      await sql`INSERT INTO harniqo.workbench_project_budgets VALUES
        (${scope.organizationId}, ${scope.projectId}, ${request.budget_policy_id}, 10, 3, 0)`.execute(tx);
      await sql`INSERT INTO harniqo.hermes_execution_bindings VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${target}, ${request.deployment_revision_id},
          ${workspace}, ${runtime}, ${randomUUID()}, ${randomUUID()}, 1, true, statement_timestamp() + interval '3 minutes')`.execute(tx);
    });
    producer = await startWorkbenchProducer(controlUrl.href);
    consumer = new PgBoss({ connectionString: workerUrl.href, schema: WORKBENCH_QUEUE_SCHEMA, migrate: false,
      createSchema: false, schedule: false, supervise: false, registerInstance: false });
    await consumer.start();
    const reserve = createWorkbenchQueue(producer);
    const submit = async (sessionId: string): Promise<HermesJob> => {
      const receipt = await acceptRun(control, scope, sessionId, randomUUID(), request, reserve);
      const [job] = await consumer!.fetch<HermesJob>(WORKBENCH_RUN_QUEUE);
      assert.equal(job?.id, receipt.run_id);
      assert.deepEqual(job.data, { ...scope, runId: receipt.run_id, commandId: receipt.command_id });
      return job.data;
    };
    const job = await submit(session);
    assert.deepEqual(await Promise.all([assignHermesJob(worker, job), assignHermesJob(worker, job)]), ['assigned', 'assigned']);
    await consumer.complete(WORKBENCH_RUN_QUEUE, job.runId);
    assert.deepEqual(await listHermesAssignments(worker, scope), [job]);
    await assert.rejects(assignHermesJob(worker, { ...job, projectId: randomUUID() }), { code: 'assignment_unavailable' });
    const another = await submit(secondarySession);
    await assert.rejects(assignHermesJob(worker, another), { code: 'workspace_busy' });

    let startCalls = 0;
    let attachCalls = 0;
    let loseReceipt = false;
    let allowTerminal = false;
    const nativeRunId = 'run_' + 'a'.repeat(32);
    const nativeOutput = 'Hermes result from persisted history';
    const open: OpenAuthorizedExecution = async identity => ({
      identity,
      hermes: {
        async attach(nativeSessionId) {
          attachCalls++;
          return { nativeSessionId };
        },
        async start(input) {
          assert.equal(input.idempotencyKey, identity.attemptId);
          assert.equal(input.nativeSessionId, identity.nativeSessionId);
          assert.equal(input.text, request.prompt);
          startCalls++;
          if (loseReceipt) throw new Error('start response was lost');
          return { nativeRunId, replayed: false };
        },
        async inspect(id, nativeSessionId) {
          assert.equal(id, nativeRunId);
          assert.equal(nativeSessionId, identity.nativeSessionId);
          return { nativeRunId, nativeSessionId, status: 'completed', output: nativeOutput };
        },
        async readReply() { return nativeOutput; },
      },
      async assertLive() {
        assert.equal(identity.configurationRevisionId, request.configuration_revision_id);
        assert.equal(identity.inputSnapshotId, request.input_snapshot_id);
        assert.equal(identity.budgetPolicyId, request.budget_policy_id);
        return true;
      },
      async hasNativeSession() { return true; },
      async confirmTerminal(id) {
        return allowTerminal ? { nativeRunId: id, writerExited: true, servedModel: 'fixture-model' } : null;
      },
    });
    assert.equal(await executeHermesAssignment(worker, job, async (...args) => {
      const session = await open(...args);
      return session && { ...session, identity: { ...session.identity, workspaceId: randomUUID() } };
    }), 'pending');
    assert.equal(startCalls, 0);
    await Promise.all([executeHermesAssignment(worker, job, open), executeHermesAssignment(worker, job, open)]);
    assert.equal(startCalls, 1);
    assert.equal(attachCalls, 1);
    assert.equal((await readRunSnapshot(control, scope, job.runId) as { run: { state: string } }).run.state, 'running');
    assert.equal(await executeHermesAssignment(worker, job, open), 'reconciling');
    assert.equal((await readRunSnapshot(control, scope, job.runId) as { run: { state: string } }).run.state, 'reconciling');
    allowTerminal = true;
    assert.equal(await executeHermesAssignment(worker, job, open), 'terminal');
    assert.equal(await executeHermesAssignment(worker, job, open), 'terminal');
    const snapshot = await readRunSnapshot(control, scope, job.runId) as {
      last_seq: number; run: { state: string; native_session: { native_id: string } };
    };
    assert.equal(snapshot.run.state, 'succeeded');
    assert.equal(snapshot.run.native_session.native_id, 'harniqo_' + session);
    const history = await readRunEvents(control, scope, job.runId) as {
      events: { seq: number; source?: { type: string; payload: { text?: string } }; platform?: { payload: { state: string } } }[];
    };
    for (const [definition, data] of [['RunSnapshot', snapshot], ['EventPage', history]] as const) {
      const validates = contractValidator(definition);
      assert.ok(validates(data), JSON.stringify(validates.errors));
    }
    assert.deepEqual(history.events.map(e => e.source?.type ?? e.platform?.payload.state),
      ['accepted', 'queued', 'provisioning', 'running', 'reconciling', 'text.delta', 'attempt.result_observed', 'succeeded']);
    assert.equal(history.events[5]!.source?.payload.text, nativeOutput);
    assert.equal(history.events.at(-1)?.seq, snapshot.last_seq);
    const settled = await sql<{ finalized: boolean; model: string }>`SELECT r.finalized_at IS NOT NULL AS finalized,
      a.served_model AS model FROM harniqo.workbench_budget_reservations r JOIN harniqo.hermes_attempts a USING (run_id)
      WHERE r.run_id = ${job.runId}`.execute(owner);
    assert.deepEqual(settled.rows[0], { finalized: true, model: 'fixture-model' });

    const revoked = await submit(session);
    assert.equal(await assignHermesJob(worker, revoked), 'assigned');
    await sql`UPDATE harniqo.project_memberships SET can_submit = false WHERE principal_id = ${scope.principalId}`.execute(owner);
    assert.equal(await executeHermesAssignment(worker, revoked, async () => {
      assert.fail('a revoked principal must not open an execution');
    }), 'terminal');
    assert.equal((await readRunSnapshot(control, scope, revoked.runId) as { run: { state: string } }).run.state, 'failed');
    await sql`UPDATE harniqo.project_memberships SET can_submit = true WHERE principal_id = ${scope.principalId}`.execute(owner);

    const next = await submit(session);
    assert.equal(await assignHermesJob(worker, next), 'assigned');
    loseReceipt = true;
    assert.equal(await executeHermesAssignment(worker, next, open), 'reconciling');
    assert.equal(await executeHermesAssignment(worker, next, open), 'reconciling');
    assert.equal(startCalls, 2);
    assert.deepEqual(await listHermesAssignments(worker, scope), []);
    await assert.rejects(assignHermesJob(worker, another), { code: 'workspace_busy' });
  } finally {
    await Promise.allSettled([
      producer?.stop({ graceful: false }), consumer?.stop({ graceful: false }),
      admin.destroy(), owner.destroy(), control.destroy(), worker.destroy(),
    ]);
  }
});
