import { createHash, randomUUID } from 'node:crypto';
import { sql } from 'kysely';
import type { Kysely, Transaction } from 'kysely';

type Db = Kysely<unknown>;
type Tx = Transaction<unknown>;
export type Principal = { organizationId: string; principalId: string };
export type ProjectPrincipal = Principal & { projectId: string };
export type RunTarget = {
  organization_id: string;
  project_id: string;
  session_id: string;
  workspace_id: string;
  deployment_id: string;
  deployment_name: string;
  deployment_revision_id: string;
  configuration_revision_id: string;
  input_snapshot_id: string;
  budget_policy_id: string;
  execution_target_id: string;
};
type WorkbenchDeploymentTarget = Omit<RunTarget, 'workspace_id' | 'execution_target_id' | 'deployment_id' | 'deployment_name'> & {
  id: string;
  name: string;
  harness: 'hermes';
};
export type SubmitRun = {
  deployment_revision_id: string;
  configuration_revision_id: string;
  input_snapshot_id: string;
  budget_policy_id: string;
  prompt: string;
  required_capabilities: string[];
};
export type AcceptedRun = {
  run_id: string;
  command_id: string;
  initial_state: 'accepted';
  accepted_at: string;
};
export type ReserveAndEnqueue = (
  tx: Tx,
  run: {
    scope: ProjectPrincipal;
    runId: string;
    commandId: string;
    target: RunTarget;
    request: SubmitRun;
  },
) => Promise<void>;

export class RunStoreError extends Error {
  readonly code:
    'FORBIDDEN' | 'NOT_FOUND' | 'VALIDATION_FAILED' | 'IDEMPOTENCY_CONFLICT' | 'CAPABILITY_UNSUPPORTED' | 'QUOTA_EXCEEDED';
  constructor(code: RunStoreError['code']) {
    super(code);
    this.code = code;
  }
}

function inScope<T>(db: Db, principal: Principal, work: (tx: Tx) => Promise<T>): Promise<T> {
  return db.transaction().execute(async (tx) => {
    await sql`SELECT
      set_config('app.organization_id', ${principal.organizationId}, true),
      set_config('app.principal_id', ${principal.principalId}, true)`.execute(tx);
    return work(tx);
  });
}

const targetFields = sql`t.organization_id, t.project_id, t.session_id, t.workspace_id,
  t.deployment_id, t.deployment_name, t.deployment_revision_id, t.configuration_revision_id,
  t.input_snapshot_id, t.budget_policy_id, t.execution_target_id`;

export function listRunTargets(db: Db, principal: Principal, currentEpoch?: string): Promise<readonly WorkbenchDeploymentTarget[]> {
  return inScope(db, principal, async (tx) => {
    const { rows } = await sql<WorkbenchDeploymentTarget>`
      SELECT t.deployment_id AS id, t.deployment_name AS name, 'hermes' AS harness,
        t.organization_id, t.project_id, t.session_id, t.deployment_revision_id,
        t.configuration_revision_id, t.input_snapshot_id, t.budget_policy_id
      FROM harniqo.run_targets t
      JOIN harniqo.project_memberships m USING (organization_id, project_id, principal_id)
      WHERE t.organization_id = ${principal.organizationId}
        AND t.principal_id = ${principal.principalId}
        AND m.can_submit AND t.enabled
        ${currentEpoch ? sql`AND harniqo.hermes_execution_ready(t.organization_id, t.project_id, t.principal_id,
          t.execution_target_id, t.deployment_revision_id, t.workspace_id, ${currentEpoch}::uuid)` : sql``}
      ORDER BY t.deployment_name, t.deployment_id LIMIT 100`.execute(tx);
    return rows;
  });
}

async function checkMembership(tx: Tx, scope: ProjectPrincipal, submit: boolean): Promise<void> {
  const { rows } = await sql`SELECT 1 FROM harniqo.project_memberships
    WHERE organization_id = ${scope.organizationId} AND project_id = ${scope.projectId}
      AND principal_id = ${scope.principalId} AND can_read AND (NOT ${submit} OR can_submit)`.execute(tx);
  if (rows.length === 0) throw new RunStoreError('FORBIDDEN');
}

async function findCommand(tx: Tx, scope: ProjectPrincipal, key: string) {
  const { rows } = await sql<AcceptedRun & { sha256: string; session_id: string }>`
    SELECT c.resource_id AS run_id, c.id AS command_id, 'accepted' AS initial_state,
      to_char(r.accepted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS accepted_at,
      c.canonical_request_sha256 AS sha256, r.session_id
    FROM harniqo.commands c
    JOIN harniqo.runs r ON (r.organization_id, r.project_id, r.principal_id, r.id) =
      (c.organization_id, c.project_id, c.principal_id, c.resource_id)
    WHERE c.organization_id = ${scope.organizationId} AND c.project_id = ${scope.projectId}
      AND c.principal_id = ${scope.principalId}
      AND c.operation = 'submit_run' AND c.idempotency_key = ${key}`.execute(tx);
  return rows[0];
}

export function acceptRun(
  db: Db, scope: ProjectPrincipal, sessionId: string, key: string, request: SubmitRun,
  reserveAndEnqueue: ReserveAndEnqueue,
): Promise<AcceptedRun> {
  const sha256 = createHash('sha256').update(JSON.stringify([
    sessionId.toLowerCase(), request.deployment_revision_id.toLowerCase(),
    request.configuration_revision_id.toLowerCase(), request.input_snapshot_id.toLowerCase(),
    request.budget_policy_id.toLowerCase(), request.prompt, [...request.required_capabilities].sort(),
  ])).digest('hex');
  return inScope(db, scope, async (tx) => {
    await checkMembership(tx, scope, true);
    // Serialize requests for this exact command key. Hash collisions only make
    // unrelated transactions wait, since the command lookup uses the full key.
    const lockKey = JSON.stringify([
      scope.organizationId.toLowerCase(), scope.projectId.toLowerCase(),
      scope.principalId.toLowerCase(), 'submit_run', key,
    ]);
    await sql`SELECT pg_advisory_xact_lock(hashtextextended(${lockKey}, 0))`.execute(tx);
    await checkMembership(tx, scope, true);
    const existing = await findCommand(tx, scope, key);
    if (existing) {
      if (existing.sha256 !== sha256) throw new RunStoreError('IDEMPOTENCY_CONFLICT');
      const { sha256: _hash, session_id: _session, ...receipt } = existing;
      return receipt;
    }
    if (request.required_capabilities.some((capability) => capability !== 'new_session')) {
      throw new RunStoreError('CAPABILITY_UNSUPPORTED');
    }
    const { rows } = await sql<RunTarget>`
      SELECT ${targetFields} FROM harniqo.run_targets t
      WHERE t.organization_id = ${scope.organizationId} AND t.project_id = ${scope.projectId}
        AND t.principal_id = ${scope.principalId} AND t.session_id = ${sessionId} AND t.enabled
        AND t.deployment_revision_id = ${request.deployment_revision_id}
        AND t.configuration_revision_id = ${request.configuration_revision_id}
        AND t.input_snapshot_id = ${request.input_snapshot_id}
        AND t.budget_policy_id = ${request.budget_policy_id}`.execute(tx);
    const target = rows[0];
    if (!target) throw new RunStoreError('NOT_FOUND');
    const runId = randomUUID();
    const commandId = randomUUID();
    const acceptedAt = new Date().toISOString();
    const inserted = await sql<{ id: string }>`INSERT INTO harniqo.runs (
      organization_id, project_id, principal_id, id, session_id, workspace_id, deployment_revision_id,
      configuration_revision_id, input_snapshot_id, budget_policy_id, execution_target_id,
      required_capabilities, prompt, state, revision, last_seq, accepted_at
    ) VALUES (
      ${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${runId},
      ${target.session_id}, ${target.workspace_id}, ${target.deployment_revision_id},
      ${target.configuration_revision_id}, ${target.input_snapshot_id}, ${target.budget_policy_id},
      ${target.execution_target_id}, ${request.required_capabilities},
      ${request.prompt}, 'accepted', 1, 1, ${acceptedAt}
    ) ON CONFLICT (session_id) WHERE ended_at IS NULL DO NOTHING
    RETURNING id`.execute(tx);
    if (inserted.rows.length === 0) throw new RunStoreError('QUOTA_EXCEEDED');
    await sql`INSERT INTO harniqo.commands (
      organization_id, project_id, principal_id, id, operation, idempotency_key,
      canonical_request_sha256, state, resource_id, created_at
    ) VALUES (
      ${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${commandId}, 'submit_run',
      ${key}, ${sha256}, 'accepted', ${runId}, ${acceptedAt}
    )`.execute(tx);
    await sql`INSERT INTO harniqo.run_events (
      organization_id, project_id, principal_id, event_id, run_id, seq, persisted_at, platform
    ) VALUES (
      ${scope.organizationId}, ${scope.projectId}, ${scope.principalId}, ${randomUUID()}, ${runId}, 1,
      ${acceptedAt}, '{"type":"run.status_changed","payload":{"state":"accepted","run_revision":1}}'
    )`.execute(tx);
    await reserveAndEnqueue(tx, { scope, runId, commandId, target, request });
    return { run_id: runId, command_id: commandId, initial_state: 'accepted', accepted_at: acceptedAt };
  });
}

export function lookupRunSubmission(
  db: Db, scope: ProjectPrincipal, sessionId: string, key: string,
): Promise<AcceptedRun> {
  return inScope(db, scope, async (tx) => {
    await checkMembership(tx, scope, true);
    const existing = await findCommand(tx, scope, key);
    if (!existing || existing.session_id !== sessionId.toLowerCase()) throw new RunStoreError('NOT_FOUND');
    const { sha256: _hash, session_id: _session, ...receipt } = existing;
    return receipt;
  });
}

export function readRunSnapshot(db: Db, scope: ProjectPrincipal, runId: string): Promise<object> {
  return inScope(db, scope, async (tx) => {
    await checkMembership(tx, scope, false);
    const { rows } = await sql<{ snapshot: object }>`
      SELECT jsonb_build_object(
        'run', jsonb_strip_nulls(jsonb_build_object(
          'id', r.id, 'scope', jsonb_build_object('organization_id', r.organization_id, 'project_id', r.project_id),
          'purpose', 'interactive', 'session_id', r.session_id, 'workspace_id', r.workspace_id,
          'configuration_revision_id', r.configuration_revision_id, 'input_snapshot_id', r.input_snapshot_id,
          'deployment_revision_id', r.deployment_revision_id, 'state', r.state, 'revision', r.revision,
          'accepted_at', to_char(r.accepted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
          'started_at', to_char(r.started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
          'ended_at', to_char(r.ended_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
        )) || jsonb_build_object('native_session', r.native_session, 'active_attempt_id', r.active_attempt_id),
        'last_seq', r.last_seq,
        'snapshot_at', to_char(statement_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
        'pending_approvals', '[]'::jsonb, 'pending_inputs', '[]'::jsonb
      ) AS snapshot
      FROM harniqo.runs r
      WHERE organization_id = ${scope.organizationId} AND project_id = ${scope.projectId}
        AND principal_id = ${scope.principalId} AND id = ${runId}`.execute(tx);
    const row = rows[0];
    if (!row) throw new RunStoreError('NOT_FOUND');
    return row.snapshot;
  });
}

export async function readRunEvents(
  db: Db, scope: ProjectPrincipal, runId: string, after = 0, limit = 100,
): Promise<object> {
  if (!Number.isSafeInteger(after) || after < 0 || !Number.isInteger(limit) || limit < 1 || limit > 256) {
    throw new RunStoreError('VALIDATION_FAILED');
  }
  return inScope(db, scope, async (tx) => {
    await checkMembership(tx, scope, false);
    const { rows } = await sql<{ events: (object & { seq: number })[] }>`
      SELECT COALESCE((
        SELECT jsonb_agg(to_jsonb(e) ORDER BY e.seq) FROM (
          SELECT v.event_id, v.run_id, v.seq,
            to_char(v.persisted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS persisted_at,
            v.platform, v.source
          FROM harniqo.run_events v
          WHERE (v.organization_id, v.project_id, v.principal_id, v.run_id) =
            (r.organization_id, r.project_id, r.principal_id, r.id) AND v.seq > ${after}
          ORDER BY v.seq LIMIT ${limit + 1}
        ) e
      ), '[]'::jsonb) AS events
      FROM harniqo.runs r
      WHERE r.organization_id = ${scope.organizationId} AND r.project_id = ${scope.projectId}
        AND r.principal_id = ${scope.principalId} AND r.id = ${runId}`.execute(tx);
    const row = rows[0];
    if (!row) throw new RunStoreError('NOT_FOUND');
    const page = row.events.slice(0, limit);
    const events = page.map((event) => Object.fromEntries(Object.entries(event).filter(([, value]) => value !== null)));
    return { run_id: runId.toLowerCase(), events, next_after: page.at(-1)?.seq ?? after, has_more: row.events.length > limit };
  });
}
