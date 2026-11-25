import { setTimeout as sleep } from 'node:timers/promises';
import { sql } from 'kysely';
import type { Kysely } from 'kysely';
import type { PgBoss } from 'pg-boss';
import { assignHermesJob, executeHermesAssignment } from '../workbench-execution/index.ts';
import type { HermesJob, OpenAuthorizedExecution } from '../workbench-execution/index.ts';
import { isHermesJob } from '../workbench-execution/store.ts';
import { WORKBENCH_RUN_QUEUE } from '../workbench-queue/index.ts';

export type WorkerScope = Readonly<Pick<HermesJob, 'organizationId' | 'projectId' | 'principalId'>>;
export type WorkerIssue = Readonly<{
  code: 'queue_unavailable' | 'invalid_queue_job' | 'scope_not_served' | 'database_unavailable'
    | 'assignment_unavailable' | 'execution_unavailable';
  runId?: string;
}>;

type Queue = Pick<PgBoss, 'fetch' | 'complete' | 'getQueue' | 'schemaVersion'>;

export type HermesWorkerOptions = {
  db: Kysely<unknown>;
  queue: Queue;
  scopes: readonly WorkerScope[];
  open: OpenAuthorizedExecution;
  concurrency?: number;
  pollIntervalMs?: number;
  onIssue?: (issue: WorkerIssue) => void;
};

export type HermesWorker = { stop: () => Promise<void> };

export class HermesWorkerError extends Error {
  readonly code: 'invalid_worker_configuration' | 'worker_unavailable';

  constructor(code: HermesWorkerError['code']) {
    super(code);
    this.name = 'HermesWorkerError';
    this.code = code;
  }
}

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;

function scopeKey(scope: WorkerScope): string {
  return `${scope.organizationId}/${scope.projectId}/${scope.principalId}`;
}

function configuration(options: HermesWorkerOptions) {
  const concurrency = options.concurrency ?? 2;
  const pollIntervalMs = options.pollIntervalMs ?? 1000;
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8
    || !Number.isInteger(pollIntervalMs) || pollIntervalMs < 100 || pollIntervalMs > 60_000
    || !Array.isArray(options.scopes) || options.scopes.length < 1 || options.scopes.length > 64) {
    throw new HermesWorkerError('invalid_worker_configuration');
  }
  const scopes = options.scopes.map(scope => {
    if (!scope || !uuid.test(scope.organizationId) || !uuid.test(scope.projectId) || !uuid.test(scope.principalId)) {
      throw new HermesWorkerError('invalid_worker_configuration');
    }
    return Object.freeze({ organizationId: scope.organizationId, projectId: scope.projectId, principalId: scope.principalId });
  });
  if (new Set(scopes.map(scopeKey)).size !== scopes.length) throw new HermesWorkerError('invalid_worker_configuration');
  return { concurrency, pollIntervalMs, scopes };
}

async function verifyRuntime(db: Kysely<unknown>, queue: Queue): Promise<void> {
  const result = await sql<{ permitted: boolean }>`
    SELECT current_user = 'harniqo_worker' AND NOT r.rolsuper AND NOT r.rolbypassrls
      AND NOT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('harniqo', 'harniqo_queue') AND c.relkind IN ('r', 'p')
          AND pg_has_role(current_user, c.relowner, 'MEMBER')) AS permitted
    FROM pg_roles r WHERE r.rolname = current_user`.execute(db);
  if (result.rows[0]?.permitted !== true) throw new HermesWorkerError('worker_unavailable');
  const [version, definition] = await Promise.all([queue.schemaVersion(), queue.getQueue(WORKBENCH_RUN_QUEUE)]);
  if (version !== 46 || !definition?.partition || definition.policy !== 'standard') {
    throw new HermesWorkerError('worker_unavailable');
  }
}

// Recover the command from its persisted scope even when the queue activation was
// lost before assignment or after its ACK. A keyset cursor prevents older long
// Running Runs from permanently hiding the remainder of this scope's work.
async function recoverable(db: Kysely<unknown>, scope: WorkerScope, after: string | undefined, limit: number) {
  return db.transaction().execute(async tx => {
    await sql`SELECT set_config('app.organization_id', ${scope.organizationId}, true),
      set_config('app.project_id', ${scope.projectId}, true),
      set_config('app.principal_id', ${scope.principalId}, true)`.execute(tx);
    const result = await sql<HermesJob>`SELECT r.organization_id AS "organizationId",
      r.project_id AS "projectId", r.principal_id AS "principalId", r.id AS "runId", c.id AS "commandId"
      FROM harniqo.runs r JOIN harniqo.commands c ON
        (c.organization_id, c.project_id, c.principal_id, c.resource_id) =
        (r.organization_id, r.project_id, r.principal_id, r.id)
      LEFT JOIN harniqo.hermes_attempts a ON
        (a.organization_id, a.project_id, a.principal_id, a.run_id, a.attempt_id) =
        (r.organization_id, r.project_id, r.principal_id, r.id, r.active_attempt_id)
      WHERE r.organization_id = ${scope.organizationId} AND r.project_id = ${scope.projectId}
        AND r.principal_id = ${scope.principalId} AND c.operation = 'submit_run'
        AND c.state IN ('accepted', 'applied')
        AND r.state IN ('accepted', 'queued', 'provisioning', 'running', 'reconciling')
        AND (r.state = 'accepted' OR (a.released_at IS NULL AND a.attempt_id IS NOT NULL
          AND (a.start_authorized_at IS NULL OR a.native_run_id IS NOT NULL OR r.state <> 'reconciling')))
        ${after ? sql`AND r.id > ${after}::uuid` : sql``}
      ORDER BY r.id LIMIT ${limit}`.execute(tx);
    return result.rows;
  });
}

export async function startHermesWorker(options: HermesWorkerOptions): Promise<HermesWorker> {
  const { concurrency, pollIntervalMs, scopes } = configuration(options);
  const { db, queue, open, onIssue } = options;
  try { await verifyRuntime(db, queue); }
  catch { throw new HermesWorkerError('worker_unavailable'); }
  const stopping = new AbortController();
  const served = new Set(scopes.map(scopeKey));
  const cursors = new Map<string, string>();
  let scopeIndex = 0;

  function issue(code: WorkerIssue['code'], job?: HermesJob): void {
    try { onIssue?.(Object.freeze({ code, ...(job ? { runId: job.runId } : {}) })); }
    catch { /* A logging failure must not change queue or native execution semantics. */ }
  }

  async function advance(job: HermesJob): Promise<void> {
    if (stopping.signal.aborted) return;
    try {
      const assigned = await assignHermesJob(db, job);
      if (assigned === 'assigned' && !stopping.signal.aborted) {
        await executeHermesAssignment(db, job, open, stopping.signal);
      }
    } catch {
      if (!stopping.signal.aborted) issue('execution_unavailable', job);
    }
  }

  async function cycle(): Promise<void> {
    const pending = new Map<string, HermesJob>();
    try {
      const messages = await queue.fetch<unknown>(WORKBENCH_RUN_QUEUE, { batchSize: concurrency });
      await Promise.all(messages.map(async message => {
        const job = message.data;
        if (!isHermesJob(job) || message.id !== job.runId) { issue('invalid_queue_job'); return; }
        if (!served.has(scopeKey(job))) { issue('scope_not_served', job); return; }
        if (stopping.signal.aborted) return;
        try {
          const assigned = await assignHermesJob(db, job);
          if (assigned === 'assigned') pending.set(job.runId, job);
          try {
            // Settle only the activation we fetched. Its replacement remains
            // eligible even if the original completion response was delayed.
            await queue.complete(WORKBENCH_RUN_QUEUE, { id: message.id, retryCount: message.retryCount });
          } catch { issue('queue_unavailable', job); }
        } catch { issue('assignment_unavailable', job); }
      }));
    } catch { issue('queue_unavailable'); }
    if (stopping.signal.aborted) return;
    const scope = scopes[scopeIndex]!;
    scopeIndex = (scopeIndex + 1) % scopes.length;
    const key = scopeKey(scope);
    try {
      const rows = await recoverable(db, scope, cursors.get(key), concurrency);
      const last = rows.at(-1);
      if (last) cursors.set(key, last.runId);
      else cursors.delete(key);
      for (const job of rows) pending.set(job.runId, job);
    } catch { issue('database_unavailable'); }
    const jobs = [...pending.values()];
    for (let offset = 0; offset < jobs.length && !stopping.signal.aborted; offset += concurrency) {
      await Promise.all(jobs.slice(offset, offset + concurrency).map(advance));
    }
  }

  const running = (async () => {
    while (!stopping.signal.aborted) {
      try { await cycle(); }
      catch { issue('execution_unavailable'); }
      try { await sleep(pollIntervalMs, undefined, { signal: stopping.signal }); }
      catch { if (stopping.signal.aborted) break; }
    }
  })();
  return Object.freeze({
    async stop() {
      stopping.abort();
      await running;
    },
  });
}
