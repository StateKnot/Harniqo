import { createHash } from 'node:crypto';
import { createServer } from 'node:https';
import type { Server } from 'node:https';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { TLSSocket } from 'node:tls';
import type { HermesAdapter } from '../../../packages/adapters/hermes/adapter.ts';
import {
  HERMES_CONNECTOR_MAX_BYTES, HERMES_CONNECTOR_PATH, HermesConnectorError,
  parseAssignmentIdentity, parseConnectorRequest, parseConnectorResponse,
} from '../../control-plane/workbench-connector/protocol.ts';
import type {
  ConnectorRequest, ConnectorTerminalProof,
} from '../../control-plane/workbench-connector/protocol.ts';
import type { AssignmentIdentity } from '../../control-plane/workbench-execution/index.ts';
import { ConnectorJournal } from './journal.ts';

export { initializeConnectorState } from './journal.ts';
export { createDockerHermesAuthority } from './docker-authority.ts';
export type { DockerHermesAuthorityOptions } from './docker-authority.ts';

export type ConnectorBinding = Omit<AssignmentIdentity,
  'runId' | 'commandId' | 'attemptId' | 'leaseGeneration' | 'nativeSessionId'>;

export type ConnectorAuthority = {
  ready?(signal: AbortSignal): Promise<boolean>;
  authorize(assignment: Readonly<AssignmentIdentity>, signal: AbortSignal): Promise<{ expiresAt: number } | null>;
  hasNativeSession(assignment: Readonly<AssignmentIdentity>, signal: AbortSignal): Promise<boolean>;
  confirmTerminal(assignment: Readonly<AssignmentIdentity>, nativeRunId: string, signal: AbortSignal):
    Promise<ConnectorTerminalProof | null>;
};

export type HermesConnectorOptions = {
  tls: { key: string | Buffer; cert: string | Buffer; ca: string | Buffer };
  controllerFingerprint256: string;
  binding: ConnectorBinding;
  stateDirectory: string;
  hermes: Pick<HermesAdapter, 'attach' | 'start' | 'inspect' | 'readReply'>;
  authority: ConnectorAuthority;
};

export type HermesConnector = { server: Server; close(): Promise<void> };

async function jsonRequest(request: IncomingMessage): Promise<unknown> {
  if (!/^application\/json(?:\s*;|$)/iu.test(String(request.headers['content-type'] ?? ''))
    || request.headers['content-encoding'] !== undefined
    || Number(request.headers['content-length']) > HERMES_CONNECTOR_MAX_BYTES) {
    throw new HermesConnectorError('invalid_request');
  }
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const item of request) {
    const chunk = Buffer.isBuffer(item) ? item : Buffer.from(item);
    size += chunk.byteLength;
    if (size > HERMES_CONNECTOR_MAX_BYTES) throw new HermesConnectorError('invalid_request');
    chunks.push(chunk);
  }
  if (!request.complete) throw new HermesConnectorError('invalid_request');
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks, size))); }
  catch { throw new HermesConnectorError('invalid_request'); }
}

function fingerprint(value: string): string {
  const result = value.replaceAll(':', '').toUpperCase();
  if (!/^[A-F0-9]{64}$/u.test(result)) throw new HermesConnectorError('invalid_configuration');
  return result;
}

function response(response: ServerResponse, status: number, value: unknown): void {
  if (response.destroyed) return;
  const content = JSON.stringify(value);
  if (Buffer.byteLength(content) > HERMES_CONNECTOR_MAX_BYTES) throw new HermesConnectorError('invalid_response');
  response.writeHead(status, {
    'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(content),
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', Connection: 'close',
  });
  response.end(content);
}

export function openHermesConnector(options: HermesConnectorOptions): HermesConnector {
  const pin = fingerprint(options.controllerFingerprint256);
  const binding = Object.freeze({ ...options.binding });
  const knownIds = {
    ...binding,
    runId: '00000000-0000-4000-8000-000000000001',
    commandId: '00000000-0000-4000-8000-000000000002',
    attemptId: '00000000-0000-4000-8000-000000000003',
    leaseGeneration: 1, nativeSessionId: 'configuration-validation',
  };
  const validBinding = parseAssignmentIdentity(knownIds);
  if (Object.keys(binding).length !== Object.keys(validBinding).length - 5) {
    throw new HermesConnectorError('invalid_configuration');
  }
  const journal = new ConnectorJournal(options.stateDirectory);
  const shutdown = new AbortController();
  let queued = 0;
  let chain: Promise<void> = Promise.resolve();
  let closed = false;
  let closing: Promise<void> | undefined;

  const bound = (assignment: AssignmentIdentity) =>
    (Object.keys(binding) as (keyof ConnectorBinding)[]).every(key => assignment[key] === binding[key]);
  async function grantExpiry(assignment: Readonly<AssignmentIdentity>, signal: AbortSignal): Promise<number | undefined> {
    if (!bound(assignment)) return;
    const grant = await options.authority.authorize(assignment, signal);
    signal.throwIfAborted();
    if (grant && Number.isSafeInteger(grant.expiresAt)
      && grant.expiresAt > Date.now() && grant.expiresAt <= Date.now() + 120_000) return grant.expiresAt;
  }

  async function execute(message: ConnectorRequest, signal: AbortSignal): Promise<unknown> {
    const assignment = Object.freeze(message.assignment);
    if (!bound(assignment)) {
      if (message.operation === 'open') return { allowed: false };
      if (message.operation === 'assert_live') return { live: false };
      throw new HermesConnectorError('access_denied');
    }
    const completed = journal.read(assignment);
    if (completed?.phase === 'terminal') {
      if (message.operation === 'open') return { allowed: true };
      if (message.operation === 'assert_live') return { live: true };
      if (message.operation === 'inspect' && completed.nativeRunId === message.payload.nativeRunId) {
        return completed.observedRun;
      }
      if (message.operation === 'read_reply' && completed.nativeRunId === message.payload.run.nativeRunId
        && completed.observedRun?.status === 'completed' && completed.observedRun.output === message.payload.run.output) {
        return { reply: completed.reply ?? null };
      }
      if (message.operation === 'confirm_terminal' && completed.nativeRunId === message.payload.nativeRunId) {
        return { proof: completed.terminalProof ?? null };
      }
      throw new HermesConnectorError('access_denied');
    }
    const expiresAt = await grantExpiry(assignment, signal);
    const allowed = expiresAt !== undefined;
    if (message.operation === 'open') return { allowed: allowed && journal.admit(assignment) };
    if (message.operation === 'assert_live') return { live: allowed && !!journal.read(assignment) };
    if (!allowed) throw new HermesConnectorError('access_denied');
    signal = AbortSignal.any([signal, AbortSignal.timeout(Math.max(0, expiresAt - Date.now()))]);
    const stored = journal.read(assignment);
    if (!stored) throw new HermesConnectorError('access_denied');
    switch (message.operation) {
      case 'has_native_session':
        return { exists: await options.authority.hasNativeSession(assignment, signal) };
      case 'attach':
        if (stored.phase === 'attached') return { nativeSessionId: assignment.nativeSessionId };
        if (stored.phase !== 'admitted') throw new HermesConnectorError('unavailable');
        journal.update({ ...stored, phase: 'attaching' });
        if (await grantExpiry(assignment, signal) === undefined) throw new HermesConnectorError('access_denied');
        const attached = await options.hermes.attach(message.payload.nativeSessionId, message.payload.title, signal);
        if (attached.nativeSessionId !== assignment.nativeSessionId) throw new HermesConnectorError('identity_mismatch');
        journal.update({ ...stored, phase: 'attached' });
        signal.throwIfAborted();
        return attached;
      case 'start': {
        const digest = createHash('sha256').update(JSON.stringify([
          message.payload.nativeSessionId, message.payload.text, message.payload.idempotencyKey,
        ])).digest('hex');
        if (stored.startHash && stored.startHash !== digest) throw new HermesConnectorError('identity_mismatch');
        if (stored.nativeRunId) return { nativeRunId: stored.nativeRunId, replayed: true };
        if (stored.phase !== 'attached') throw new HermesConnectorError('unavailable');
        journal.update({ ...stored, phase: 'starting', startHash: digest });
        if (await grantExpiry(assignment, signal) === undefined) throw new HermesConnectorError('access_denied');
        const started = await options.hermes.start(message.payload, signal);
        const result = { version: 1, assignment, result: started };
        parseConnectorResponse(message, result);
        journal.update({ ...stored, phase: 'running', startHash: digest, nativeRunId: started.nativeRunId });
        signal.throwIfAborted();
        return started;
      }
      case 'inspect': {
        if (stored.nativeRunId !== message.payload.nativeRunId) throw new HermesConnectorError('identity_mismatch');
        if (stored.observedRun?.status === 'completed' || stored.phase === 'terminal') return stored.observedRun;
        const native = await options.hermes.inspect(message.payload.nativeRunId, message.payload.nativeSessionId, signal);
        parseConnectorResponse(message, { version: 1, assignment, result: native });
        journal.update({ ...stored, observedRun: native });
        return native;
      }
      case 'read_reply': {
        const run = stored.observedRun;
        if (!run || run.status !== 'completed' || run.nativeRunId !== message.payload.run.nativeRunId
          || run.nativeRunId !== stored.nativeRunId || run.output !== message.payload.run.output) {
          throw new HermesConnectorError('identity_mismatch');
        }
        if (stored.reply !== undefined) return { reply: stored.reply };
        const reply = await options.hermes.readReply(run, signal);
        if (reply !== undefined) {
          parseConnectorResponse(message, { version: 1, assignment, result: { reply } });
          journal.update({ ...stored, reply });
        }
        return { reply: reply ?? null };
      }
      case 'confirm_terminal': {
        if (stored.nativeRunId !== message.payload.nativeRunId) throw new HermesConnectorError('identity_mismatch');
        if (stored.terminalProof) return { proof: stored.terminalProof };
        if (!stored.observedRun || !['failed', 'completed'].includes(stored.observedRun.status)
          || (stored.observedRun.status === 'completed' && !stored.reply)) return { proof: null };
        const proof = await options.authority.confirmTerminal(assignment, message.payload.nativeRunId, signal);
        parseConnectorResponse(message, { version: 1, assignment, result: { proof } });
        if (proof) {
          journal.update({ ...stored, phase: 'terminal', terminalProof: proof });
        }
        return { proof };
      }
    }
  }

  const server = createServer({
    ...options.tls, requestCert: true, rejectUnauthorized: true, minVersion: 'TLSv1.2',
    handshakeTimeout: 5000, maxHeaderSize: 8192, headersTimeout: 5000,
    requestTimeout: 10_000, keepAliveTimeout: 1000,
  }, (request, reply) => {
    const socket = request.socket;
    if (closed || !(socket instanceof TLSSocket) || !socket.authorized
      || socket.getPeerCertificate().fingerprint256?.replaceAll(':', '').toUpperCase() !== pin) {
      response(reply, 403, { error: 'access_denied' });
      return;
    }
    if (request.method === 'GET' && request.url === '/connector/v1/ready') {
      const signal = AbortSignal.any([shutdown.signal, AbortSignal.timeout(5000)]);
      void Promise.resolve().then(() => options.authority.ready?.(signal) ?? false).then(
        ready => response(reply, 200, { version: 1, binding, ready }),
        () => response(reply, 503, { error: 'unavailable' }),
      );
      return;
    }
    if (request.method !== 'POST' || request.url !== HERMES_CONNECTOR_PATH) {
      response(reply, 404, { error: 'not_found' });
      return;
    }
    if (queued >= 8) { response(reply, 503, { error: 'unavailable' }); return; }
    queued++;
    const deadline = AbortSignal.timeout(25_000);
    const aborted = new AbortController();
    const signal = AbortSignal.any([shutdown.signal, deadline, aborted.signal]);
    reply.once('close', () => { if (!reply.writableFinished) aborted.abort(); });
    const body = jsonRequest(request);
    // One local native workspace has one serialized mutator. A slow request cannot
    // make another assignment pass the durable active-writer guard.
    const operation = chain.then(async () => {
      signal.throwIfAborted();
      const message = parseConnectorRequest(await body);
      const result = await execute(message, signal);
      const envelope = { version: 1, assignment: message.assignment, result };
      parseConnectorResponse(message, envelope);
      response(reply, 200, envelope);
    });
    chain = operation.catch(() => {});
    void operation.catch(error => {
      const code = error instanceof HermesConnectorError ? error.code : 'unavailable';
      response(reply, code === 'access_denied' ? 403 : code === 'invalid_request' ? 400 : 503, { error: code });
    }).finally(() => { queued--; });
    void body.catch(() => {});
  });
  server.maxConnections = 16;
  server.maxRequestsPerSocket = 16;
  server.on('tlsClientError', () => {});
  return {
    server,
    close: () => closing ??= (async () => {
      closed = true;
      shutdown.abort();
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
      await chain;
      journal.close();
    })(),
  };
}
