import type { HermesAdapter } from '@harniqo/hermes-adapter';
import {
  authorizeStart, beginProvisioning, HermesExecutionError, isHermesJob, markReconciling, readAssignedRun,
  recordStarted, recordTerminal,
} from './store.ts';
import type { AssignmentIdentity, Db } from './store.ts';

export { assignHermesJob, HermesExecutionError, listHermesAssignments } from './store.ts';
export type { AssignmentIdentity, HermesJob } from './store.ts';

export type AuthorizedExecution = {
  readonly identity: Readonly<AssignmentIdentity>;
  readonly hermes: Pick<HermesAdapter, 'attach' | 'start' | 'inspect' | 'readReply'>;
  // Implemented by the authority for the isolated Runner or dedicated external deployment.
  // Check the current assignment, fenced writer and bounded model/tool grant on every call.
  assertLive(signal: AbortSignal): Promise<boolean>;
  // Native continuation must exist before calling attach, which can otherwise create a session.
  hasNativeSession(signal: AbortSignal): Promise<boolean>;
  confirmTerminal(nativeRunId: string, signal: AbortSignal): Promise<{
    nativeRunId: string;
    writerExited: true;
    servedModel?: string;
  } | null>;
};

export type OpenAuthorizedExecution = (identity: Readonly<AssignmentIdentity>, signal: AbortSignal) =>
  Promise<AuthorizedExecution | null>;
type PollResult = 'pending' | 'reconciling' | 'terminal';

function matchesAssignment(expected: AssignmentIdentity, result: AssignmentIdentity): boolean {
  return (Object.keys(expected) as (keyof AssignmentIdentity)[]).every(key => result[key] === expected[key]);
}

function validModel(model: string | undefined): model is string {
  return typeof model === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model) && !model.includes('://');
}

export async function executeHermesAssignment(db: Db, untrustedJob: unknown, open: OpenAuthorizedExecution,
  signal?: AbortSignal): Promise<PollResult> {
  if (!isHermesJob(untrustedJob)) throw new HermesExecutionError('invalid_job');
  const job = untrustedJob;
  const run = await readAssignedRun(db, job);
  if (!run) return 'terminal';
  const identity = Object.freeze(run.assignment);
  const attemptId = identity.attemptId;
  const deadline = AbortSignal.timeout(25_000);
  const operation = signal ? AbortSignal.any([deadline, signal]) : deadline;
  const reconcile = async (reason: string) => {
    await markReconciling(db, job, attemptId, reason);
    return 'reconciling' as const;
  };
  if (!run.bindingLive) return run.state === 'queued' ? 'pending' : reconcile('execution_binding_unavailable');
  if (run.startAuthorizedAt && !run.nativeRunId) {
    const expired = Date.now() - run.startAuthorizedAt.getTime() >= 60_000;
    if (expired || run.state === 'reconciling') return reconcile('native_start_unconfirmed');
    return 'pending';
  }

  let startIssued = !!run.startAuthorizedAt;
  try {
    const provisioning = run.nativeRunId ? undefined : await beginProvisioning(db, job, attemptId);
    if (provisioning === 'terminal') return 'terminal';
    if (!run.nativeRunId && !provisioning) return 'pending';
    const execution = await open(identity, operation);
    if (!execution || !matchesAssignment(identity, execution.identity) || !await execution.assertLive(operation)) {
      return startIssued ? await reconcile('execution_binding_unavailable') : 'pending';
    }
    const { hermes } = execution;
    if (!run.nativeRunId) {
      if (provisioning === 'native_resume' && !await execution.hasNativeSession(operation)) {
        return reconcile('native_session_unavailable');
      }
      if (!await authorizeStart(db, job, attemptId)) return 'pending';
      startIssued = true;
      if (!await execution.assertLive(operation)) return reconcile('execution_binding_unavailable');
      operation.throwIfAborted();
      const attached = await hermes.attach(identity.nativeSessionId, undefined, operation);
      if (attached.nativeSessionId !== identity.nativeSessionId) return reconcile('native_session_identity_mismatch');
      if (!await execution.assertLive(operation)) return reconcile('execution_binding_unavailable');
      operation.throwIfAborted();
      const started = await hermes.start({
        nativeSessionId: identity.nativeSessionId,
        text: run.prompt,
        idempotencyKey: attemptId,
      }, operation);
      if (started.replayed || !/^run_[0-9a-f]{32}$/.test(started.nativeRunId)) {
        return reconcile('native_start_receipt_unexpected');
      }
      await recordStarted(db, job, attemptId, started.nativeRunId);
      return 'pending';
    }

    const native = await hermes.inspect(run.nativeRunId, identity.nativeSessionId, operation);
    if (native.nativeRunId !== run.nativeRunId || native.nativeSessionId !== identity.nativeSessionId) {
      return reconcile('native_run_identity_mismatch');
    }
    if (native.status === 'queued' || native.status === 'running') return 'pending';
    if (native.status !== 'failed' && native.status !== 'completed') return reconcile('native_result_unconfirmed');

    if (!await execution.assertLive(operation)) return reconcile('execution_binding_unavailable');
    const reply = native.status === 'completed' ? await hermes.readReply(native, operation) : undefined;
    if (native.status === 'completed' && (!reply || reply.length > 256 * 1024)) {
      return reconcile('native_reply_unconfirmed');
    }
    const proof = await execution.confirmTerminal(run.nativeRunId, operation);
    if (!proof || proof.writerExited !== true || proof.nativeRunId !== run.nativeRunId) {
      return reconcile('execution_writer_unconfirmed');
    }
    const servedModel = proof.servedModel;
    if (native.status === 'completed' && (!validModel(servedModel) || (native.model && native.model !== servedModel))) {
      return reconcile('served_model_unconfirmed');
    }
    const persisted = await recordTerminal(db, job, attemptId, {
      nativeRunId: run.nativeRunId,
      state: native.status === 'completed' ? 'succeeded' : 'failed',
      ...(reply ? { reply } : {}),
      ...(validModel(servedModel) ? { servedModel } : {}),
    });
    return persisted ? 'terminal' : await reconcile('execution_binding_unavailable');
  } catch (error) {
    if (error instanceof HermesExecutionError) throw error;
    return startIssued ? await reconcile('native_result_unconfirmed') : 'pending';
  }
}
