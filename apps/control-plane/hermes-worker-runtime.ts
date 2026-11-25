import { readFile } from 'node:fs/promises';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { setTimeout as sleep } from 'node:timers/promises';
import { loadHermesConnector } from './workbench-connector/index.ts';
import type { AssignmentIdentity, OpenAuthorizedExecution } from './workbench-execution/index.ts';
import { startWorkbenchProducer } from './workbench-queue/index.ts';
import { databaseConnectionString } from './workbench-runtime.ts';
import { startHermesWorker } from './workbench-worker/index.ts';

const bindingKeys = [
  'organizationId', 'projectId', 'principalId', 'workspaceId', 'executionTargetId',
  'deploymentRevisionId', 'configurationRevisionId', 'inputSnapshotId', 'budgetPolicyId',
  'runtimeInstanceId', 'harnessReleaseId', 'platformEpoch', 'instanceGeneration',
] as const;
type BindingKey = typeof bindingKeys[number];
type Target = {
  binding: Pick<AssignmentIdentity, BindingKey>;
  origin: string;
  caFile: string;
  certFile: string;
  keyFile: string;
};
type WorkerConfig = { targets: Target[]; concurrency: number; pollIntervalMs: number };

function record(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(name => Object.hasOwn(value, name));
}

function parseConfiguration(value: unknown): WorkerConfig {
  if (!record(value, ['targets', 'concurrency', 'pollIntervalMs']) || !Array.isArray(value.targets)
    || value.targets.length < 1 || value.targets.length > 64
    || typeof value.concurrency !== 'number' || !Number.isInteger(value.concurrency) || value.concurrency < 1 || value.concurrency > 8
    || typeof value.pollIntervalMs !== 'number' || !Number.isInteger(value.pollIntervalMs)
    || value.pollIntervalMs < 100 || value.pollIntervalMs > 60000) throw new Error('invalid_hermes_worker_config');
  const seen = new Set<string>();
  const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;
  for (const target of value.targets) {
    if (!record(target, ['binding', 'origin', 'caFile', 'certFile', 'keyFile']) || !record(target.binding, bindingKeys)
      || !['origin', 'caFile', 'certFile', 'keyFile'].every(name => typeof target[name] === 'string')) {
      throw new Error('invalid_hermes_worker_target');
    }
    const binding = target.binding;
    if (bindingKeys.some(name => name === 'instanceGeneration'
      ? typeof binding[name] !== 'number' || !Number.isSafeInteger(binding[name]) || binding[name] < 1
      : typeof binding[name] !== 'string' || !uuid.test(binding[name]))) throw new Error('invalid_hermes_worker_binding');
    const identity = JSON.stringify(bindingKeys.map(name => binding[name]));
    if (seen.has(identity)) throw new Error('duplicate_hermes_worker_target');
    seen.add(identity);
  }
  return value as WorkerConfig;
}

export async function loadHermesWorker(configPath: string, databasePath: string): Promise<{ stop(): Promise<void> }> {
  const config = parseConfiguration(JSON.parse(await readFile(configPath, 'utf8')));
  const targets = await Promise.all(config.targets.map(async target => ({
    binding: target.binding,
    open: await loadHermesConnector({ origin: target.origin, caFile: target.caFile,
      certFile: target.certFile, keyFile: target.keyFile }),
  })));
  const scopes = [...new Map(targets.map(({ binding }) => [
    JSON.stringify([binding.organizationId, binding.projectId, binding.principalId]),
    { organizationId: binding.organizationId, projectId: binding.projectId, principalId: binding.principalId },
  ])).values()];
  const open: OpenAuthorizedExecution = async (assignment, signal) => {
    const target = targets.find(candidate => bindingKeys.every(key => assignment[key] === candidate.binding[key]));
    return target ? target.open(assignment, signal) : null;
  };
  const connectionString = await databaseConnectionString(databasePath);
  const pool = new Pool({ connectionString, max: config.concurrency + 2, connectionTimeoutMillis: 3000,
    statement_timeout: 7000, lock_timeout: 4000, idle_in_transaction_session_timeout: 10000 });
  pool.on('error', () => console.error('Harniqo Hermes worker lost an idle database connection.'));
  const db = new Kysely<unknown>({ dialect: new PostgresDialect({ pool }) });
  let queue: Awaited<ReturnType<typeof startWorkbenchProducer>> | undefined;
  let worker: Awaited<ReturnType<typeof startHermesWorker>> | undefined;
  const healthStopping = new AbortController();
  let healthLoop: Promise<void> | undefined;
  let stopping: Promise<void> | undefined;
  const stop = () => stopping ??= (async () => {
    healthStopping.abort();
    try { await worker?.stop(); await healthLoop; }
    finally {
      try { await queue?.stop({ graceful: true, close: true }); }
      finally { await db.destroy(); }
    }
  })();
  try {
    queue = await startWorkbenchProducer(connectionString);
    const renewReadiness = async () => {
      await Promise.all(targets.map(async target => {
        const b = target.binding;
        let healthy = false;
        try {
          const status = await target.open.ready(AbortSignal.any([healthStopping.signal, AbortSignal.timeout(3000)]));
          healthy = status.ready && bindingKeys.every(key => status.binding[key] === b[key]);
        } catch { /* A failed probe expires availability while execution remains fenced separately. */ }
        await db.transaction().execute(async tx => {
          await sql`SELECT set_config('app.organization_id', ${b.organizationId}, true),
            set_config('app.project_id', ${b.projectId}, true), set_config('app.principal_id', ${b.principalId}, true)`.execute(tx);
          await sql`UPDATE harniqo.hermes_execution_bindings SET ready_until =
            CASE WHEN ${healthy && !healthStopping.signal.aborted} THEN statement_timestamp() + interval '10 seconds' ELSE NULL END
            WHERE organization_id = ${b.organizationId} AND project_id = ${b.projectId} AND principal_id = ${b.principalId}
              AND execution_target_id = ${b.executionTargetId} AND workspace_id = ${b.workspaceId}
              AND deployment_revision_id = ${b.deploymentRevisionId} AND runtime_instance_id = ${b.runtimeInstanceId}
              AND harness_release_id = ${b.harnessReleaseId} AND platform_epoch = ${b.platformEpoch}
              AND instance_generation = ${b.instanceGeneration} AND enabled`.execute(tx);
        });
      }));
    };
    await renewReadiness();
    worker = await startHermesWorker({ db, queue, scopes, open, concurrency: config.concurrency,
      pollIntervalMs: config.pollIntervalMs, onIssue: issue => {
        console.error(JSON.stringify({ event: 'hermes_worker_issue', code: issue.code,
          ...(issue.runId ? { runId: issue.runId } : {}) }));
      } });
    healthLoop = (async () => {
      while (!healthStopping.signal.aborted) {
        try {
          await sleep(3000, undefined, { signal: healthStopping.signal });
          await renewReadiness();
        } catch {
          if (!healthStopping.signal.aborted) console.error('Harniqo Hermes runtime readiness could not be renewed.');
        }
      }
    })();
    return { stop };
  } catch (error) {
    await stop();
    throw error;
  }
}
