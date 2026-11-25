import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { request as httpsRequest } from 'node:https';
import { isAbsolute } from 'node:path';
import { createSecureContext } from 'node:tls';
import type { AssignmentIdentity, OpenAuthorizedExecution } from '../workbench-execution/index.ts';
import {
  HERMES_CONNECTOR_MAX_BYTES, HERMES_CONNECTOR_PATH, HermesConnectorError,
  connectorObject, parseAssignmentIdentity, parseConnectorRequest, parseConnectorResponse,
} from './protocol.ts';
import type { ConnectorOperation, ConnectorResults } from './protocol.ts';

export { HermesConnectorError } from './protocol.ts';

export type HermesConnectorOptions = {
  origin: string;
  ca: Buffer;
  cert: Buffer;
  key: Buffer;
  timeoutMs?: number;
};

export type HermesConnectorFiles = {
  origin: string;
  caFile: string;
  certFile: string;
  keyFile: string;
  timeoutMs?: number;
};

const tlsCodes = new Set([
  'CERT_HAS_EXPIRED', 'CERT_NOT_YET_VALID', 'CERT_REVOKED', 'DEPTH_ZERO_SELF_SIGNED_CERT',
  'ERR_TLS_CERT_ALTNAME_INVALID', 'SELF_SIGNED_CERT_IN_CHAIN', 'UNABLE_TO_GET_ISSUER_CERT',
  'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
]);

function connectorOrigin(value: unknown): URL {
  if (typeof value !== 'string' || value.length > 2048 || !/^https:\/\/[^/@?#\\\s\p{Cc}]+\/?$/u.test(value)) {
    throw new HermesConnectorError('invalid_configuration');
  }
  const origin = URL.parse(value);
  if (!origin?.hostname || origin.protocol !== 'https:' || origin.pathname !== '/') {
    throw new HermesConnectorError('invalid_configuration');
  }
  return origin;
}

function credentials(options: HermesConnectorOptions) {
  if (!Buffer.isBuffer(options.ca) || !Buffer.isBuffer(options.cert) || !Buffer.isBuffer(options.key)
    || options.ca.length < 1 || options.ca.length > 256 * 1024 || options.cert.length < 1
    || options.cert.length > 64 * 1024 || options.key.length < 1 || options.key.length > 32 * 1024) {
    throw new HermesConnectorError('invalid_configuration');
  }
  const tls = { ca: Buffer.from(options.ca), cert: Buffer.from(options.cert), key: Buffer.from(options.key), minVersion: 'TLSv1.2' as const };
  try { createSecureContext(tls); }
  catch { throw new HermesConnectorError('invalid_configuration'); }
  return tls;
}

export type HermesConnectorClient = OpenAuthorizedExecution & {
  ready(signal: AbortSignal): Promise<{ binding: Record<string, unknown>; ready: boolean }>;
};

export function createHermesConnector(options: HermesConnectorOptions): HermesConnectorClient {
  const origin = connectorOrigin(options.origin);
  const tls = credentials(options);
  const timeoutMs = options.timeoutMs ?? 8000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 25_000) {
    throw new HermesConnectorError('invalid_configuration');
  }

  async function exchange(message: unknown, signal: AbortSignal): Promise<unknown> {
    const data = message === undefined ? undefined : Buffer.from(JSON.stringify(message));
    if (data && data.length > HERMES_CONNECTOR_MAX_BYTES) throw new HermesConnectorError('invalid_request');
    const timeout = AbortSignal.timeout(timeoutMs);
    const effective = AbortSignal.any([signal, timeout]);
    const transportCode = () => signal.aborted ? 'aborted' : timeout.aborted ? 'timeout' : 'unavailable';
    return new Promise<unknown>((resolve, reject) => {
      if (effective.aborted) { reject(new HermesConnectorError(transportCode())); return; }
      const outgoing = httpsRequest(new URL(data ? HERMES_CONNECTOR_PATH : '/connector/v1/ready', origin), {
        ...tls, rejectUnauthorized: true, method: data ? 'POST' : 'GET', agent: false, maxHeaderSize: 8192, signal: effective,
        headers: { 'content-type': 'application/json', accept: 'application/json', ...(data ? { 'content-length': data.length } : {}),
          'cache-control': 'no-store', connection: 'close' },
      }, async response => {
        try {
          if (response.statusCode === 401 || response.statusCode === 403) throw new HermesConnectorError('access_denied');
          if (response.statusCode !== 200 || !/^application\/json(?:\s*;|$)/iu.test(response.headers['content-type'] ?? '')) {
            throw new HermesConnectorError('invalid_response');
          }
          if (Number(response.headers['content-length']) > HERMES_CONNECTOR_MAX_BYTES) throw new HermesConnectorError('invalid_response');
          let size = 0;
          const chunks: Buffer[] = [];
          for await (const chunk of response) {
            const bytes: Buffer = chunk;
            size += bytes.length;
            if (size > HERMES_CONNECTOR_MAX_BYTES) throw new HermesConnectorError('invalid_response');
            chunks.push(bytes);
          }
          if (!response.complete || effective.aborted) throw new HermesConnectorError(transportCode());
          let json: unknown;
          try { json = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks, size))); }
          catch { throw new HermesConnectorError('invalid_response'); }
          resolve(json);
        } catch (error) {
          response.destroy();
          reject(error instanceof HermesConnectorError ? error : new HermesConnectorError(transportCode()));
        }
      });
      outgoing.once('error', (error: NodeJS.ErrnoException) => {
        reject(new HermesConnectorError(tlsCodes.has(error.code ?? '') ? 'tls_rejected' : transportCode()));
      });
      outgoing.end(data);
    });
  }

  const openExecution: OpenAuthorizedExecution = async (untrustedIdentity, signal) => {
    const assignment = Object.freeze(parseAssignmentIdentity(untrustedIdentity));
    const invoke = async <O extends ConnectorOperation>(operation: O, payload: unknown, operationSignal: AbortSignal) => {
      const parsed = parseConnectorRequest({ version: 1, assignment, operation, payload });
      return parseConnectorResponse<O>({ ...parsed, operation }, await exchange(parsed, operationSignal));
    };
    if (!(await invoke('open', {}, signal)).allowed) return null;
    return {
      identity: assignment,
      assertLive: async operationSignal => (await invoke('assert_live', {}, operationSignal)).live,
      hasNativeSession: async operationSignal => (await invoke('has_native_session', {}, operationSignal)).exists,
      confirmTerminal: async (nativeRunId, operationSignal) => (await invoke('confirm_terminal', { nativeRunId }, operationSignal)).proof,
      hermes: {
        attach: (nativeSessionId, title, operationSignal = signal) => invoke('attach', {
          nativeSessionId, ...(title === undefined ? {} : { title }),
        }, operationSignal),
        start: (input, operationSignal = signal) => invoke('start', input, operationSignal),
        inspect: (nativeRunId, nativeSessionId, operationSignal = signal) => invoke('inspect', { nativeRunId, nativeSessionId }, operationSignal),
        readReply: async (run, operationSignal = signal) => (await invoke('read_reply', { run }, operationSignal)).reply ?? undefined,
      },
    };
  };
  return Object.assign(openExecution, {
    async ready(signal: AbortSignal) {
      const result = connectorObject(await exchange(undefined, signal));
      const binding = connectorObject(result?.binding);
      if (!result || result.version !== 1 || typeof result.ready !== 'boolean'
        || Object.keys(result).sort().join(',') !== 'binding,ready,version' || !binding) {
        throw new HermesConnectorError('invalid_response');
      }
      const identity = parseAssignmentIdentity({
        ...binding, runId: '00000000-0000-4000-8000-000000000001',
        commandId: '00000000-0000-4000-8000-000000000002', attemptId: '00000000-0000-4000-8000-000000000003',
        leaseGeneration: 1, nativeSessionId: 'status',
      });
      if (Object.keys(binding).length !== Object.keys(identity).length - 5) throw new HermesConnectorError('invalid_response');
      return { binding, ready: result.ready };
    },
  });
}

async function readCredential(path: string, secret: boolean): Promise<Buffer> {
  if (!isAbsolute(path)) throw new HermesConnectorError('invalid_configuration');
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 1 || stat.size > 256 * 1024
      || (process.platform !== 'win32' && ((stat.mode & (secret ? 0o077 : 0o022)) !== 0
        || (secret && stat.uid !== process.getuid?.())))) throw new HermesConnectorError('invalid_configuration');
    return await file.readFile();
  } finally {
    await file.close();
  }
}

export async function loadHermesConnector(options: HermesConnectorFiles): Promise<HermesConnectorClient> {
  try {
    const [ca, cert, key] = await Promise.all([
      readCredential(options.caFile, false), readCredential(options.certFile, false), readCredential(options.keyFile, true),
    ]);
    return createHermesConnector({ origin: options.origin, ca, cert, key,
      ...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }) });
  } catch {
    throw new HermesConnectorError('invalid_configuration');
  }
}
