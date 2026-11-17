import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { RunStoreError, acceptRun, listRunTargets, lookupRunSubmission, readRunEvents, readRunSnapshot } from './index.ts';
import type { ProjectPrincipal } from './index.ts';

function database(connectionString: string): Kysely<unknown> {
  return new Kysely({ dialect: new PostgresDialect({
    pool: new Pool({ connectionString, max: 2, connectionTimeoutMillis: 3000, statement_timeout: 8000 }),
  }) });
}

test('PostgreSQL 17 stores one authorized command and rolls back the whole failed transaction', { timeout: 20_000 }, async (t) => {
  const url = new URL(process.env.HARNIQO_TEST_POSTGRES_URL!);
  assert.equal(url.protocol, 'postgresql:');
  assert.equal(url.hostname, '127.0.0.1');
  assert.equal(url.pathname, '/harniqo_workbench_test');
  const admin = database(url.href);
  let runtime: Kysely<unknown> | undefined;
  const scope: ProjectPrincipal = {
    organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID(),
  };
  const other = { ...scope, principalId: randomUUID() };
  const session = randomUUID();
  const workspace = randomUUID();
  const deployment = randomUUID();
  const secondSession = randomUUID();
  const secondDeployment = randomUUID();
  const request = {
    deployment_revision_id: randomUUID(), configuration_revision_id: randomUUID(),
    input_snapshot_id: randomUUID(), budget_policy_id: randomUUID(), prompt: '测试 Hermes 指令',
    required_capabilities: ['new_session'],
  };
  const executionTarget = randomUUID();
  const key = randomUUID();
  try {
    const { rows } = await sql<{ version: number }>`
      SELECT current_setting('server_version_num')::integer AS version`.execute(admin);
    const version = rows[0]!.version;
    assert.ok(version >= 170000 && version < 180000);
    const migration = await readFile(new URL('./001-first-run.sql', import.meta.url), 'utf8');
    const runtimePassword = randomBytes(32).toString('hex');
    await admin.transaction().execute(async (tx) => {
      await sql`CREATE ROLE harniqo_migrator NOLOGIN NOSUPERUSER NOBYPASSRLS`.execute(tx);
      await sql`CREATE ROLE harniqo_control LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD ${sql.lit(runtimePassword)}`.execute(tx);
      await sql`GRANT CREATE ON DATABASE harniqo_workbench_test TO harniqo_migrator`.execute(tx);
      await sql`SET LOCAL ROLE harniqo_migrator`.execute(tx);
      await sql.raw(migration).execute(tx);
      await sql`INSERT INTO harniqo.project_memberships VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, true, true),
        (${other.organizationId}, ${other.projectId}, ${other.principalId}, true, true)`.execute(tx);
      await sql`INSERT INTO harniqo.run_targets (
        organization_id, project_id, principal_id, session_id, workspace_id, deployment_id,
        deployment_name, deployment_revision_id, configuration_revision_id, input_snapshot_id,
        budget_policy_id, execution_target_id
      ) VALUES
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${session}, ${workspace},
         ${deployment}, 'Hermes', ${request.deployment_revision_id}, ${request.configuration_revision_id},
         ${request.input_snapshot_id}, ${request.budget_policy_id}, ${executionTarget}),
        (${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${secondSession}, ${randomUUID()},
         ${secondDeployment}, 'Hermes 2', ${request.deployment_revision_id}, ${request.configuration_revision_id},
         ${request.input_snapshot_id}, ${request.budget_policy_id}, ${executionTarget})`.execute(tx);
      await sql`CREATE TABLE harniqo.transaction_probe (run_id uuid PRIMARY KEY)`.execute(tx);
      await sql`GRANT INSERT ON harniqo.transaction_probe TO harniqo_control`.execute(tx);
    });

    url.username = 'harniqo_control';
    url.password = runtimePassword;
    runtime = database(url.href);
    const expected = {
      id: deployment, name: 'Hermes', harness: 'hermes', organization_id: scope.organizationId,
      project_id: scope.projectId, session_id: session, deployment_revision_id: request.deployment_revision_id,
      configuration_revision_id: request.configuration_revision_id, input_snapshot_id: request.input_snapshot_id,
      budget_policy_id: request.budget_policy_id,
    };
    assert.deepEqual(await listRunTargets(runtime, scope), [
      expected, { ...expected, id: secondDeployment, name: 'Hermes 2', session_id: secondSession },
    ]);
    assert.deepEqual(await listRunTargets(runtime, other), []);
    let reservations = 0;
    const reserveAndEnqueue: Parameters<typeof acceptRun>[5] = async (tx, run) => {
      reservations++;
      assert.equal(run.target.execution_target_id, executionTarget);
      await sql`INSERT INTO harniqo.transaction_probe VALUES (${run.runId})`.execute(tx);
      await sql`SELECT pg_sleep(0.1)`.execute(tx);
    };
    const receipts = await Promise.all([
      acceptRun(runtime, scope, session, key, request, reserveAndEnqueue),
      acceptRun(runtime, scope, session, key, request, reserveAndEnqueue),
    ]);
    const receipt = receipts[0];
    assert.deepEqual(receipts[1], receipt);
    assert.equal(reservations, 1);
    assert.deepEqual(await lookupRunSubmission(runtime, scope, session, key), receipt);
    await assert.rejects(acceptRun(runtime, scope, session, key, { ...request, prompt: '改用了其他输入' }, reserveAndEnqueue),
      { code: 'IDEMPOTENCY_CONFLICT' });
    await assert.rejects(acceptRun(runtime, scope, session, randomUUID(), {
      ...request, required_capabilities: ['strong_stop'],
    }, reserveAndEnqueue), { code: 'CAPABILITY_UNSUPPORTED' });
    await assert.rejects(acceptRun(runtime, other, session, randomUUID(), request, reserveAndEnqueue),
      { code: 'NOT_FOUND' });
    await assert.rejects(readRunSnapshot(runtime, other, receipt.run_id), { code: 'NOT_FOUND' });
    assert.partialDeepStrictEqual(await readRunSnapshot(runtime, scope, receipt.run_id), {
      run: { id: receipt.run_id, state: 'accepted', revision: 1, native_session: null, active_attempt_id: null },
      last_seq: 1,
    });
    assert.partialDeepStrictEqual(await readRunEvents(runtime, scope, receipt.run_id, 0, 1), {
      run_id: receipt.run_id,
      events: [{ seq: 1, platform: { type: 'run.status_changed', payload: { state: 'accepted', run_revision: 1 } } }],
      next_after: 1,
      has_more: false,
    });
    assert.deepEqual(await readRunEvents(runtime, scope, receipt.run_id, 1, 1), {
      run_id: receipt.run_id, events: [], next_after: 1, has_more: false,
    });
    assert.deepEqual((await sql`SELECT id FROM harniqo.runs`.execute(runtime)).rows, []);
    await assert.rejects(sql`UPDATE harniqo.runs SET prompt = 'forbidden'`.execute(runtime), { code: '42501' });
    await assert.rejects(admin.transaction().execute(async (tx) => {
      await sql`SET LOCAL ROLE harniqo_migrator`.execute(tx);
      await sql`UPDATE harniqo.run_targets SET workspace_id = ${randomUUID()} WHERE session_id = ${session}`.execute(tx);
    }), { code: '23503' });

    const failedKey = randomUUID();
    let failedRunId: string | undefined;
    await assert.rejects(acceptRun(runtime, scope, secondSession, failedKey, request, async (tx, run) => {
      failedRunId = run.runId;
      await sql`INSERT INTO harniqo.transaction_probe VALUES (${run.runId})`.execute(tx);
      throw new Error('enqueue failed');
    }), /enqueue failed/);
    assert.ok(failedRunId);
    await assert.rejects(lookupRunSubmission(runtime, scope, secondSession, failedKey), { code: 'NOT_FOUND' });
    await assert.rejects(readRunSnapshot(runtime, scope, failedRunId), { code: 'NOT_FOUND' });
    await assert.rejects(readRunEvents(runtime, scope, failedRunId), { code: 'NOT_FOUND' });
    const probe = await sql<{ run_id: string }>`SELECT run_id FROM harniqo.transaction_probe`.execute(admin);
    assert.deepEqual(probe.rows, [{ run_id: receipt.run_id }]);

    const client = runtime;
    await t.test('two distinct commands for the same Session accept exactly one Run', async () => {
      const keys = [randomUUID(), randomUUID()];
      const results = await Promise.allSettled(keys.map((commandKey) =>
        acceptRun(client, scope, secondSession, commandKey, request, reserveAndEnqueue)));
      const success = results.findIndex((result) => result.status === 'fulfilled');
      assert.notEqual(success, -1);
      const rejected = results[1 - success]!;
      assert.equal(rejected.status, 'rejected');
      assert.ok(rejected.reason instanceof RunStoreError);
      assert.equal(rejected.reason.code, 'QUOTA_EXCEEDED');
      assert.equal(reservations, 2);
      await assert.rejects(lookupRunSubmission(client, scope, secondSession, keys[1 - success]!),
        { code: 'NOT_FOUND' });
    });

    await admin.transaction().execute(async (tx) => {
      await sql`SET LOCAL ROLE harniqo_migrator`.execute(tx);
      await sql`UPDATE harniqo.project_memberships SET can_read = false, can_submit = false
        WHERE organization_id = ${scope.organizationId} AND project_id = ${scope.projectId}
          AND principal_id = ${scope.principalId}`.execute(tx);
    });
    await assert.rejects(acceptRun(runtime, scope, session, key, request, reserveAndEnqueue), { code: 'FORBIDDEN' });
    await assert.rejects(readRunSnapshot(runtime, scope, receipt.run_id), { code: 'FORBIDDEN' });
  } finally {
    await runtime?.destroy();
    await admin.destroy();
  }
});
