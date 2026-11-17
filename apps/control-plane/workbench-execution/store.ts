import { randomUUID } from 'node:crypto';
import { sql } from 'kysely';
import type { Kysely, Transaction } from 'kysely';

export type HermesJob = {
  organizationId: string;
  projectId: string;
  principalId: string;
  runId: string;
  commandId: string;
};

export type AssignmentIdentity = HermesJob & {
  attemptId: string;
  workspaceId: string;
  executionTargetId: string;
  deploymentRevisionId: string;
  configurationRevisionId: string;
  inputSnapshotId: string;
  budgetPolicyId: string;
  runtimeInstanceId: string;
  harnessReleaseId: string;
  platformEpoch: string;
  instanceGeneration: number;
  leaseGeneration: number;
  nativeSessionId: string;
};

type State = 'accepted' | 'queued' | 'provisioning' | 'running' | 'stopping' |
  'waiting_approval' | 'waiting_input' | 'reconciling' | 'succeeded' | 'failed' | 'cancelled';
type TerminalState = 'succeeded' | 'failed' | 'cancelled';

export type Db = Kysely<unknown>;
type Tx = Transaction<unknown>;

type RunRow = {
  id: string;
  session_id: string;
  configuration_revision_id: string;
  input_snapshot_id: string;
  budget_policy_id: string;
  state: State;
  revision: string;
  last_seq: string;
  active_attempt_id: string | null;
  prompt: string;
  target_enabled: boolean;
  reserved: boolean;
  can_submit: boolean;
};
type AssignedRun = RunRow & {
  assignment: AssignmentIdentity;
  bindingLive: boolean;
  nativeRunId: string | null;
  startAuthorizedAt: Date | null;
};

export class HermesExecutionError extends Error {
  readonly code: 'invalid_job' | 'assignment_unavailable' | 'workspace_busy' | 'missing_reservation';
  constructor(code: HermesExecutionError['code']) {
    super(code);
    this.code = code;
  }
}

const idPattern = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;

export function isHermesJob(job: unknown): job is HermesJob {
  if (!job || typeof job !== 'object') return false;
  const record = job as Record<string, unknown>;
  return ['organizationId', 'projectId', 'principalId', 'runId', 'commandId'].every(name =>
    typeof record[name] === 'string' && idPattern.test(record[name]));
}

export function isTerminal(state: State): state is TerminalState {
  return state === 'succeeded' || state === 'failed' || state === 'cancelled';
}

async function withScope<T>(db: Db, scope: Pick<HermesJob, 'organizationId' | 'projectId' | 'principalId'>,
  work: (tx: Tx) => Promise<T>): Promise<T> {
  return db.transaction().execute(async tx => {
    await sql`SELECT set_config('app.organization_id', ${scope.organizationId}, true),
      set_config('app.project_id', ${scope.projectId}, true),
      set_config('app.principal_id', ${scope.principalId}, true)`.execute(tx);
    return work(tx);
  });
}

async function lockRun(tx: Tx, job: HermesJob): Promise<RunRow> {
  const { rows } = await sql<RunRow>`
    SELECT r.id, r.session_id, r.state, r.revision, r.last_seq, r.active_attempt_id, r.prompt,
      r.configuration_revision_id, r.input_snapshot_id, r.budget_policy_id,
      t.enabled AS target_enabled,
      EXISTS (SELECT 1 FROM harniqo.project_memberships m WHERE
        (m.organization_id, m.project_id, m.principal_id) = (r.organization_id, r.project_id, r.principal_id)
        AND m.can_submit) AS can_submit,
      EXISTS (SELECT 1 FROM harniqo.workbench_budget_reservations br
        WHERE br.organization_id = r.organization_id AND br.project_id = r.project_id
          AND br.principal_id = r.principal_id AND br.run_id = r.id
          AND br.budget_policy_id = r.budget_policy_id AND br.command_id = c.id
          AND br.finalized_at IS NULL) AS reserved
    FROM harniqo.runs r
    JOIN harniqo.commands c ON (c.organization_id, c.project_id, c.principal_id, c.resource_id) =
      (r.organization_id, r.project_id, r.principal_id, r.id)
    JOIN harniqo.run_targets t ON (t.organization_id, t.project_id, t.principal_id, t.session_id) =
      (r.organization_id, r.project_id, r.principal_id, r.session_id)
    WHERE (r.organization_id, r.project_id, r.principal_id, r.id, c.id) =
      (${job.organizationId}, ${job.projectId}, ${job.principalId}, ${job.runId}, ${job.commandId})
      AND c.operation = 'submit_run' AND c.state IN ('accepted', 'applied')
    FOR UPDATE OF r`.execute(tx);
  const run = rows[0];
  if (!run) throw new HermesExecutionError('assignment_unavailable');
  return run;
}

const assignmentFields = sql`a.attempt_id AS "attemptId", a.workspace_id AS "workspaceId",
  a.execution_target_id AS "executionTargetId", a.deployment_revision_id AS "deploymentRevisionId",
  a.runtime_instance_id AS "runtimeInstanceId", a.harness_release_id AS "harnessReleaseId",
  a.platform_epoch AS "platformEpoch", a.instance_generation::float8 AS "instanceGeneration",
  a.lease_generation::float8 AS "leaseGeneration", a.native_session_id AS "nativeSessionId"`;

function scopedJob(job: HermesJob): HermesJob {
  const { organizationId, projectId, principalId, runId, commandId } = job;
  return { organizationId, projectId, principalId, runId, commandId };
}

async function readAttempt(tx: Tx, job: HermesJob, run: RunRow): Promise<AssignedRun> {
  type Row = Omit<AssignmentIdentity, keyof HermesJob | 'configurationRevisionId' | 'inputSnapshotId' | 'budgetPolicyId'> & {
    live: boolean; native_run_id: string | null; start_authorized_at: Date | null;
  };
  const { rows } = await sql<Row>`
    SELECT ${assignmentFields}, a.native_run_id, a.start_authorized_at,
      EXISTS (SELECT 1 FROM harniqo.hermes_execution_bindings b WHERE b.enabled AND b.ready_until > statement_timestamp()
        AND (b.organization_id, b.project_id, b.principal_id, b.execution_target_id, b.deployment_revision_id,
          b.workspace_id, b.runtime_instance_id, b.harness_release_id, b.platform_epoch, b.instance_generation) =
        (a.organization_id, a.project_id, a.principal_id, a.execution_target_id, a.deployment_revision_id,
          a.workspace_id, a.runtime_instance_id, a.harness_release_id, a.platform_epoch, a.instance_generation)) AS live
    FROM harniqo.hermes_attempts a
    WHERE a.run_id = ${job.runId} AND a.attempt_id = ${run.active_attempt_id} AND a.released_at IS NULL`.execute(tx);
  const row = rows[0];
  if (!row) throw new HermesExecutionError('assignment_unavailable');
  const { live, native_run_id, start_authorized_at, ...binding } = row;
  return {
    ...run,
    assignment: {
      ...scopedJob(job),
      ...binding,
      configurationRevisionId: run.configuration_revision_id,
      inputSnapshotId: run.input_snapshot_id,
      budgetPolicyId: run.budget_policy_id,
    },
    bindingLive: live && run.target_enabled && run.reserved,
    nativeRunId: native_run_id,
    startAuthorizedAt: start_authorized_at,
  };
}

function nativeSession(identity: AssignmentIdentity, recoveryKind: 'new' | 'native_resume') {
  return {
    harness_release_id: identity.harnessReleaseId,
    workspace_id: identity.workspaceId,
    runtime_instance_id: identity.runtimeInstanceId,
    instance_generation: identity.instanceGeneration,
    native_id: identity.nativeSessionId,
    recovery_kind: recoveryKind,
  };
}

async function appendEvent(tx: Tx, job: HermesJob, sequence: number, kind: 'platform' | 'source', event: object) {
  await sql`INSERT INTO harniqo.run_events
    (organization_id, project_id, principal_id, event_id, run_id, seq, persisted_at, ${sql.ref(kind)})
    VALUES (${job.organizationId}, ${job.projectId}, ${job.principalId}, ${randomUUID()}, ${job.runId},
      ${sequence}, statement_timestamp(), ${JSON.stringify(event)}::jsonb)`.execute(tx);
}

async function transition(tx: Tx, job: HermesJob, run: RunRow, state: State, options: {
  attemptId?: string;
  native?: ReturnType<typeof nativeSession>;
  reason?: string;
  writtenEvents?: number;
} = {}) {
  const revision = Number(run.revision) + 1;
  const sequence = Number(run.last_seq) + (options.writtenEvents ?? 0) + 1;
  await sql`UPDATE harniqo.runs SET state = ${state}, revision = ${revision}, last_seq = ${sequence},
    active_attempt_id = CASE WHEN ${isTerminal(state)} THEN NULL ELSE COALESCE(${options.attemptId ?? null}, active_attempt_id) END,
    native_session = COALESCE(${options.native ? JSON.stringify(options.native) : null}::jsonb, native_session),
    started_at = CASE WHEN ${state === 'running'} THEN COALESCE(started_at, statement_timestamp()) ELSE started_at END,
    ended_at = CASE WHEN ${isTerminal(state)} THEN statement_timestamp() ELSE ended_at END
    WHERE id = ${job.runId}`.execute(tx);
  await appendEvent(tx, job, sequence, 'platform', {
    type: 'run.status_changed',
    payload: {
      previous_state: run.state,
      state,
      run_revision: revision,
      ...(options.reason ? { reason: options.reason } : {}),
    },
  });
}

async function finalizeReservation(tx: Tx, job: HermesJob) {
  const finalized = await sql`UPDATE harniqo.workbench_budget_reservations SET finalized_at = statement_timestamp()
    WHERE run_id = ${job.runId} AND command_id = ${job.commandId} AND finalized_at IS NULL RETURNING run_id`.execute(tx);
  if (!finalized.rows.length) throw new HermesExecutionError('missing_reservation');
}

export async function assignHermesJob(db: Db, job: unknown): Promise<'assigned' | 'terminal'> {
  if (!isHermesJob(job)) throw new HermesExecutionError('invalid_job');
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (isTerminal(run.state)) return 'terminal';
    if (run.active_attempt_id) {
      await readAttempt(tx, job, run);
      return 'assigned';
    }
    if (!run.reserved) throw new HermesExecutionError('missing_reservation');
    if (run.state !== 'accepted' || !run.target_enabled) throw new HermesExecutionError('assignment_unavailable');
    if (!run.can_submit) {
      await transition(tx, job, run, 'failed', { reason: 'project_submission_revoked' });
      await finalizeReservation(tx, job);
      return 'terminal';
    }
    const attemptId = randomUUID();
    const result = await sql<{ created: boolean }>`
      INSERT INTO harniqo.hermes_attempts (organization_id, project_id, principal_id, run_id, attempt_id,
        workspace_id, execution_target_id, deployment_revision_id, runtime_instance_id, harness_release_id,
        platform_epoch, instance_generation, native_session_id)
      SELECT r.organization_id, r.project_id, r.principal_id, r.id, ${attemptId}, b.workspace_id,
        b.execution_target_id, b.deployment_revision_id, b.runtime_instance_id, b.harness_release_id,
        b.platform_epoch, b.instance_generation, ${'harniqo_' + run.session_id}
      FROM harniqo.runs r JOIN harniqo.hermes_execution_bindings b ON
        (b.organization_id, b.project_id, b.principal_id, b.execution_target_id, b.deployment_revision_id, b.workspace_id) =
        (r.organization_id, r.project_id, r.principal_id, r.execution_target_id, r.deployment_revision_id, r.workspace_id)
      WHERE r.id = ${job.runId} AND b.enabled AND b.ready_until > statement_timestamp()
      ON CONFLICT (workspace_id) WHERE released_at IS NULL DO NOTHING RETURNING TRUE AS created`.execute(tx);
    if (!result.rows[0]) {
      const { rows } = await sql`SELECT 1 FROM harniqo.runs r
        JOIN harniqo.hermes_execution_bindings b ON
          (b.organization_id, b.project_id, b.principal_id, b.execution_target_id, b.deployment_revision_id, b.workspace_id) =
          (r.organization_id, r.project_id, r.principal_id, r.execution_target_id, r.deployment_revision_id, r.workspace_id)
        WHERE r.id = ${job.runId} AND b.enabled AND b.ready_until > statement_timestamp()`.execute(tx);
      throw new HermesExecutionError(rows.length ? 'workspace_busy' : 'assignment_unavailable');
    }
    await transition(tx, job, run, 'queued', { attemptId });
    return 'assigned';
  });
}

export async function listHermesAssignments(db: Db,
  scope: Pick<HermesJob, 'organizationId' | 'projectId' | 'principalId'>): Promise<readonly HermesJob[]> {
  return withScope(db, scope, async tx => {
    const { rows } = await sql<HermesJob>`SELECT r.organization_id AS "organizationId",
      r.project_id AS "projectId", r.principal_id AS "principalId", r.id AS "runId", c.id AS "commandId"
      FROM harniqo.runs r
      JOIN harniqo.commands c ON (c.organization_id, c.project_id, c.principal_id, c.resource_id) =
        (r.organization_id, r.project_id, r.principal_id, r.id)
      JOIN harniqo.hermes_attempts a ON (a.organization_id, a.project_id, a.principal_id, a.run_id, a.attempt_id) =
        (r.organization_id, r.project_id, r.principal_id, r.id, r.active_attempt_id)
      WHERE a.released_at IS NULL AND c.operation = 'submit_run' AND c.state IN ('accepted', 'applied')
        AND r.state IN ('queued', 'provisioning', 'running', 'reconciling') AND
        (a.start_authorized_at IS NULL OR a.native_run_id IS NOT NULL OR r.state <> 'reconciling')
      ORDER BY r.accepted_at, r.id LIMIT 100`.execute(tx);
    return rows;
  });
}

export function readAssignedRun(db: Db, job: HermesJob): Promise<AssignedRun | undefined> {
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (isTerminal(run.state)) return;
    return readAttempt(tx, job, run);
  });
}

async function resolvedSession(tx: Tx, job: HermesJob, run: AssignedRun) {
  const { rows } = await sql<{ native_session: ReturnType<typeof nativeSession> }>`
    SELECT native_session FROM harniqo.runs WHERE session_id = ${run.session_id}
    AND id <> ${job.runId} AND native_session IS NOT NULL ORDER BY accepted_at DESC, id DESC LIMIT 1`.execute(tx);
  const previous = rows[0]?.native_session;
  const session = nativeSession(run.assignment, previous ? 'native_resume' : 'new');
  if (previous && (previous.native_id !== session.native_id || previous.harness_release_id !== session.harness_release_id ||
    previous.workspace_id !== session.workspace_id || previous.runtime_instance_id !== session.runtime_instance_id ||
    previous.instance_generation !== session.instance_generation)) return;
  return session;
}

export function beginProvisioning(db: Db, job: HermesJob, attemptId: string):
  Promise<'new' | 'native_resume' | 'terminal' | undefined> {
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (run.active_attempt_id !== attemptId || !['queued', 'provisioning'].includes(run.state)) return;
    const attempt = await readAttempt(tx, job, run);
    if (!attempt.bindingLive || attempt.startAuthorizedAt) return;
    if (!run.can_submit) {
      await transition(tx, job, run, 'failed', { reason: 'project_submission_revoked' });
      await sql`UPDATE harniqo.hermes_attempts SET released_at = statement_timestamp()
        WHERE run_id = ${job.runId} AND attempt_id = ${attemptId}`.execute(tx);
      await finalizeReservation(tx, job);
      return 'terminal';
    }
    const session = await resolvedSession(tx, job, attempt);
    if (!session) {
      await transition(tx, job, run, 'reconciling', { reason: 'native_session_binding_changed' });
      return;
    }
    if (run.state === 'queued') await transition(tx, job, run, 'provisioning');
    return session.recovery_kind;
  });
}

export function authorizeStart(db: Db, job: HermesJob, attemptId: string): Promise<boolean> {
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (run.state !== 'provisioning' || run.active_attempt_id !== attemptId) return false;
    const attempt = await readAttempt(tx, job, run);
    if (!attempt.bindingLive || attempt.startAuthorizedAt || !run.can_submit) return false;
    const session = await resolvedSession(tx, job, attempt);
    if (!session) return false;
    await sql`UPDATE harniqo.hermes_attempts SET start_authorized_at = statement_timestamp()
      WHERE run_id = ${job.runId} AND attempt_id = ${attemptId}`.execute(tx);
    await sql`UPDATE harniqo.runs SET native_session = ${JSON.stringify(session)}::jsonb
      WHERE id = ${job.runId}`.execute(tx);
    return true;
  });
}

export function recordStarted(db: Db, job: HermesJob, attemptId: string, nativeRunId: string): Promise<void> {
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (run.active_attempt_id !== attemptId || !['provisioning', 'reconciling'].includes(run.state)) return;
    const attempt = await readAttempt(tx, job, run);
    if (!attempt.startAuthorizedAt || (attempt.nativeRunId && attempt.nativeRunId !== nativeRunId)) {
      throw new HermesExecutionError('assignment_unavailable');
    }
    await sql`UPDATE harniqo.hermes_attempts SET native_run_id = ${nativeRunId}
      WHERE run_id = ${job.runId} AND attempt_id = ${attemptId}`.execute(tx);
    if (attempt.bindingLive) await transition(tx, job, run, 'running');
    else if (run.state === 'provisioning') {
      await transition(tx, job, run, 'reconciling', { reason: 'execution_binding_unavailable' });
    }
  });
}

export function markReconciling(db: Db, job: HermesJob, attemptId: string, reason: string): Promise<void> {
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (run.active_attempt_id === attemptId && ['provisioning', 'running'].includes(run.state)) {
      await transition(tx, job, run, 'reconciling', { reason });
    }
  });
}

export type ConfirmedResult = {
  nativeRunId: string;
  state: 'succeeded' | 'failed';
  reply?: string;
  servedModel?: string;
};

export function recordTerminal(db: Db, job: HermesJob, attemptId: string, result: ConfirmedResult): Promise<boolean> {
  return withScope(db, job, async tx => {
    const run = await lockRun(tx, job);
    if (isTerminal(run.state)) return true;
    if (run.active_attempt_id !== attemptId || !['running', 'reconciling'].includes(run.state)) return false;
    const attempt = await readAttempt(tx, job, run);
    if (!attempt.bindingLive || attempt.nativeRunId !== result.nativeRunId) return false;
    const identity = attempt.assignment;
    const source = {
      contract_version: '0.1.0',
      scope: { organization_id: job.organizationId, project_id: job.projectId },
      run_id: job.runId,
      attempt_id: attemptId,
      runtime_instance_id: identity.runtimeInstanceId,
      binding: { platform_epoch: identity.platformEpoch, lease_generation: identity.leaseGeneration },
      stream_id: attemptId,
      observed_at: new Date().toISOString(),
    };
    let count = 0;
    if (result.reply) {
      const blockId = randomUUID();
      const parts = result.reply.match(/[\s\S]{1,16000}/gu) ?? [];
      for (const text of parts) {
        await appendEvent(tx, job, Number(run.last_seq) + ++count, 'source', {
          ...source, source_seq: count, type: 'text.delta', payload: { block_id: blockId, text },
        });
      }
    }
    await appendEvent(tx, job, Number(run.last_seq) + ++count, 'source', {
      ...source, source_seq: count, type: 'attempt.result_observed',
      payload: { outcome: result.state === 'succeeded' ? 'completed' : 'failed' },
    });
    await transition(tx, job, run, result.state, { writtenEvents: count });
    await sql`UPDATE harniqo.hermes_attempts SET released_at = statement_timestamp(),
      served_model = ${result.servedModel ?? null} WHERE run_id = ${job.runId} AND attempt_id = ${attemptId}`.execute(tx);
    await finalizeReservation(tx, job);
    return true;
  });
}
