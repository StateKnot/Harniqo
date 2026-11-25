import type { HermesCompletedRun, HermesRun } from '@harniqo/hermes-adapter';
import type { AssignmentIdentity } from '../workbench-execution/index.ts';

export const HERMES_CONNECTOR_PATH = '/connector/v1/hermes';
export const HERMES_CONNECTOR_MAX_BYTES = 2 * 1024 * 1024;
export const HERMES_CONNECTOR_REPLY_LIMIT = 256 * 1024;

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;
const runId = /^run_[0-9a-f]{32}$/u;
const sessionId = /^[A-Za-z0-9_:-][A-Za-z0-9_.:-]{0,159}$/u;
const modelId = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/u;
const identityIds = [
  'organizationId', 'projectId', 'principalId', 'runId', 'commandId', 'attemptId', 'workspaceId',
  'executionTargetId', 'deploymentRevisionId', 'configurationRevisionId', 'inputSnapshotId',
  'budgetPolicyId', 'runtimeInstanceId', 'harnessReleaseId', 'platformEpoch',
] as const;
const identityKeys = [...identityIds, 'instanceGeneration', 'leaseGeneration', 'nativeSessionId'];
const statuses = ['queued', 'running', 'waiting_for_approval', 'stopping', 'completed', 'failed',
  'cancelled', 'interrupted', 'unknown'];

export type ConnectorRequest = {
  version: 1;
  assignment: AssignmentIdentity;
} & (
  | { operation: 'open' | 'assert_live' | 'has_native_session'; payload: Record<string, never> }
  | { operation: 'attach'; payload: { nativeSessionId: string; title?: string } }
  | { operation: 'start'; payload: { nativeSessionId: string; text: string; idempotencyKey: string } }
  | { operation: 'inspect'; payload: { nativeRunId: string; nativeSessionId: string } }
  | { operation: 'read_reply'; payload: { run: HermesCompletedRun } }
  | { operation: 'confirm_terminal'; payload: { nativeRunId: string } }
);

export type ConnectorOperation = ConnectorRequest['operation'];
export type ConnectorTerminalProof = { nativeRunId: string; writerExited: true; servedModel?: string };
export type ConnectorResults = {
  open: { allowed: boolean };
  assert_live: { live: boolean };
  has_native_session: { exists: boolean };
  attach: { nativeSessionId: string };
  start: { nativeRunId: string; replayed: boolean };
  inspect: HermesRun;
  read_reply: { reply: string | null };
  confirm_terminal: { proof: ConnectorTerminalProof | null };
};

export class HermesConnectorError extends Error {
  readonly code: 'invalid_configuration' | 'invalid_request' | 'invalid_response' | 'identity_mismatch'
    | 'access_denied' | 'unavailable' | 'timeout' | 'aborted' | 'tls_rejected';

  constructor(code: HermesConnectorError['code']) {
    super(code);
    this.code = code;
    this.name = 'HermesConnectorError';
  }
}

export function connectorObject(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

function exactKeys(value: Record<string, unknown>, required: readonly string[], optional: readonly string[] = []): boolean {
  return required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
}

export function parseAssignmentIdentity(value: unknown): AssignmentIdentity {
  const identity = connectorObject(value);
  if (!identity || !exactKeys(identity, identityKeys)
    || !identityIds.every(key => typeof identity[key] === 'string' && uuid.test(identity[key]))
    || typeof identity.nativeSessionId !== 'string' || !sessionId.test(identity.nativeSessionId)
    || !['instanceGeneration', 'leaseGeneration'].every(key => Number.isSafeInteger(identity[key]) && Number(identity[key]) > 0)) {
    throw new HermesConnectorError('invalid_request');
  }
  return { ...identity } as AssignmentIdentity;
}

export function sameAssignment(expected: AssignmentIdentity, received: AssignmentIdentity): boolean {
  return identityKeys.every(key => expected[key as keyof AssignmentIdentity] === received[key as keyof AssignmentIdentity]);
}

function nativeRun(value: unknown, assignment: AssignmentIdentity): HermesRun | undefined {
  const run = connectorObject(value);
  if (!run || !exactKeys(run, ['nativeRunId', 'nativeSessionId', 'status'], ['output', 'model'])
    || typeof run.nativeRunId !== 'string' || !runId.test(run.nativeRunId)
    || run.nativeSessionId !== assignment.nativeSessionId || !statuses.includes(String(run.status))) return;
  if (run.model !== undefined && (typeof run.model !== 'string' || !modelId.test(run.model) || run.model.includes('://'))) return;
  if (run.status === 'completed' ? typeof run.output !== 'string' || run.output.length > HERMES_CONNECTOR_REPLY_LIMIT
    : run.output !== undefined) return;
  return { ...run } as HermesRun;
}

export function parseConnectorRequest(value: unknown): ConnectorRequest {
  const message = connectorObject(value);
  if (!message || !exactKeys(message, ['version', 'operation', 'assignment', 'payload']) || message.version !== 1) {
    throw new HermesConnectorError('invalid_request');
  }
  const assignment = parseAssignmentIdentity(message.assignment);
  const payload = connectorObject(message.payload);
  if (!payload) throw new HermesConnectorError('invalid_request');
  let valid = false;
  switch (message.operation) {
    case 'open':
    case 'assert_live':
    case 'has_native_session':
      valid = exactKeys(payload, []);
      break;
    case 'attach':
      valid = exactKeys(payload, ['nativeSessionId'], ['title']) && payload.nativeSessionId === assignment.nativeSessionId
        && (payload.title === undefined || (typeof payload.title === 'string' && [...payload.title].length <= 100));
      break;
    case 'start':
      valid = exactKeys(payload, ['nativeSessionId', 'text', 'idempotencyKey'])
        && payload.nativeSessionId === assignment.nativeSessionId && payload.idempotencyKey === assignment.attemptId
        && typeof payload.text === 'string' && !!payload.text.trim() && payload.text.isWellFormed()
        && Buffer.byteLength(JSON.stringify({ session_id: payload.nativeSessionId, input: payload.text })) <= 64 * 1024;
      break;
    case 'inspect':
      valid = exactKeys(payload, ['nativeRunId', 'nativeSessionId']) && payload.nativeSessionId === assignment.nativeSessionId
        && typeof payload.nativeRunId === 'string' && runId.test(payload.nativeRunId);
      break;
    case 'read_reply': {
      const run = nativeRun(payload.run, assignment);
      valid = exactKeys(payload, ['run']) && run?.status === 'completed';
      break;
    }
    case 'confirm_terminal':
      valid = exactKeys(payload, ['nativeRunId']) && typeof payload.nativeRunId === 'string' && runId.test(payload.nativeRunId);
      break;
  }
  if (!valid) throw new HermesConnectorError('invalid_request');
  return { ...message, assignment, payload: { ...payload } } as ConnectorRequest;
}

export function parseConnectorResponse<O extends ConnectorOperation>(request: {
  assignment: AssignmentIdentity; operation: O; payload: unknown;
}, value: unknown): ConnectorResults[O] {
  const response = connectorObject(value);
  if (!response || !exactKeys(response, ['version', 'assignment', 'result']) || response.version !== 1) {
    throw new HermesConnectorError('invalid_response');
  }
  let returned: AssignmentIdentity;
  try { returned = parseAssignmentIdentity(response.assignment); }
  catch { throw new HermesConnectorError('invalid_response'); }
  if (!sameAssignment(request.assignment, returned)) throw new HermesConnectorError('identity_mismatch');
  const result = connectorObject(response.result);
  if (!result) throw new HermesConnectorError('invalid_response');
  let valid = false;
  switch (request.operation) {
    case 'open':
      valid = exactKeys(result, ['allowed']) && typeof result.allowed === 'boolean';
      break;
    case 'assert_live':
      valid = exactKeys(result, ['live']) && typeof result.live === 'boolean';
      break;
    case 'has_native_session':
      valid = exactKeys(result, ['exists']) && typeof result.exists === 'boolean';
      break;
    case 'attach':
      valid = exactKeys(result, ['nativeSessionId']) && result.nativeSessionId === request.assignment.nativeSessionId;
      break;
    case 'start':
      valid = exactKeys(result, ['nativeRunId', 'replayed']) && typeof result.nativeRunId === 'string'
        && runId.test(result.nativeRunId) && typeof result.replayed === 'boolean';
      break;
    case 'inspect': {
      const run = nativeRun(result, request.assignment);
      valid = !!run && run.nativeRunId === (request.payload as { nativeRunId: string }).nativeRunId;
      break;
    }
    case 'read_reply':
      valid = exactKeys(result, ['reply']) && (result.reply === null
        || (typeof result.reply === 'string' && result.reply.length <= HERMES_CONNECTOR_REPLY_LIMIT));
      break;
    case 'confirm_terminal': {
      const proof = connectorObject(result.proof);
      valid = exactKeys(result, ['proof']) && (result.proof === null || !!proof
        && exactKeys(proof, ['nativeRunId', 'writerExited'], ['servedModel'])
        && proof.nativeRunId === (request.payload as { nativeRunId: string }).nativeRunId && proof.writerExited === true
        && (proof.servedModel === undefined || typeof proof.servedModel === 'string'
          && modelId.test(proof.servedModel) && !proof.servedModel.includes('://')));
      break;
    }
  }
  if (!valid) throw new HermesConnectorError('invalid_response');
  return { ...result } as ConnectorResults[O];
}
