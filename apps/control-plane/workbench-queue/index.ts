import { sql } from 'kysely';
import { fromKysely, PgBoss } from 'pg-boss';
import type { acceptRun } from '../workbench-store/index.ts';

export const WORKBENCH_QUEUE_SCHEMA = 'harniqo_queue';
export const WORKBENCH_RUN_QUEUE = 'harniqo-workbench-run';

export type WorkbenchRunJob = {
  runId: string;
  commandId: string;
  organizationId: string;
  projectId: string;
  principalId: string;
};

export class WorkbenchQueueError extends Error {
  readonly code: 'FORBIDDEN' | 'BUDGET_EXCEEDED' | 'QUOTA_EXCEEDED' | 'UPSTREAM_UNAVAILABLE';

  constructor(code: WorkbenchQueueError['code']) {
    super(code);
    this.code = code;
    this.name = 'WorkbenchQueueError';
  }
}

export async function startWorkbenchProducer(connectionString: string): Promise<PgBoss> {
  let boss: PgBoss | undefined;
  try {
    boss = new PgBoss({
      connectionString, schema: WORKBENCH_QUEUE_SCHEMA, max: 2, connectionTimeoutMillis: 3000,
      migrate: false, createSchema: false, supervise: false, schedule: false, registerInstance: false,
    });
    // pg-boss background errors can contain connection diagnostics. Keep them out of logs.
    boss.on('error', () => console.error('Harniqo workbench queue background error'));
    await boss.start();
    const queue = await boss.getQueue(WORKBENCH_RUN_QUEUE);
    if (await boss.schemaVersion() !== 46 || !queue?.partition || queue.policy !== 'standard') {
      throw new WorkbenchQueueError('UPSTREAM_UNAVAILABLE');
    }
    return boss;
  } catch {
    await boss?.stop({ graceful: false, timeout: 3000 }).catch(() => {});
    throw new WorkbenchQueueError('UPSTREAM_UNAVAILABLE');
  }
}

export function createWorkbenchQueue(boss: PgBoss): Parameters<typeof acceptRun>[5] {
  return async (tx, { scope, runId, commandId }) => {
    try {
      const { rows } = await sql<{ result: string }>`
        SELECT harniqo.reserve_workbench_run(${runId}::uuid, ${commandId}::uuid) AS result`.execute(tx);
      const result = rows[0]?.result;
      if (result === 'FORBIDDEN' || result === 'BUDGET_EXCEEDED' || result === 'QUOTA_EXCEEDED') {
        throw new WorkbenchQueueError(result);
      }
      if (result !== 'reserved') throw new WorkbenchQueueError('UPSTREAM_UNAVAILABLE');
      const job: WorkbenchRunJob = {
        runId, commandId, organizationId: scope.organizationId, projectId: scope.projectId,
        principalId: scope.principalId,
      };
      const jobId = await boss.send(WORKBENCH_RUN_QUEUE, job, { id: runId, db: fromKysely(tx) });
      if (jobId !== runId) throw new WorkbenchQueueError('UPSTREAM_UNAVAILABLE');
    } catch (error) {
      if (error instanceof WorkbenchQueueError) throw error;
      throw new WorkbenchQueueError('UPSTREAM_UNAVAILABLE');
    }
  };
}
