import { once } from 'node:events';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { createServer } from 'node:https';
import { isIPv4 } from 'node:net';
import type { Socket } from 'node:net';
import { PassThrough } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { EgressTransportError } from './transport.ts';
import type { UpstreamTransport } from './transport.ts';

const MAX_BODY_BYTES = 1024 * 1024;
const REQUEST_TIMEOUT_MS = 120_000;
const READER_TIMEOUT_MS = 10_000;
const FORWARDED_HEADERS = new Set(['accept', 'content-type', 'anthropic-version', 'anthropic-beta', 'openai-beta']);
const LOCAL_HEADERS = new Set(['host', 'authorization', 'connection', 'content-length', 'transfer-encoding', 'user-agent', 'accept-encoding']);

export type GuestInvocation = Readonly<{
  credential: string;
  peerAddress: string;
  method: 'GET' | 'POST';
  path: string;
  headers: Readonly<Record<string, string>>;
  body: Buffer;
}>;

export type ExecutionPermit = Readonly<{
  routeId: string;
  expiresAtMs: number;
  revoked: AbortSignal;
  /** Checks the same decision against current authority before sending bytes. */
  confirmCurrent: (signal: AbortSignal) => Promise<boolean>;
}>;

export type GuestIngressOptions = Readonly<{
  host: string;
  port: number;
  key: Buffer;
  cert: Buffer;
  transport: UpstreamTransport;
  /** Returns a fresh decision for this exact invocation or null if it is denied. */
  authorize: (invocation: GuestInvocation, signal: AbortSignal) => Promise<ExecutionPermit | null>;
}>;

class RequestError extends Error {
  readonly status: number;
  constructor(status: number) {
    super('guest_request_rejected');
    this.status = status;
  }
}

function readHeaders(request: IncomingMessage) {
  const headers: Record<string, string> = Object.create(null);
  for (let i = 0; i < request.rawHeaders.length; i += 2) {
    const name = request.rawHeaders[i]!.toLowerCase();
    const value = request.rawHeaders[i + 1]!;
    if (Object.hasOwn(headers, name) || (!FORWARDED_HEADERS.has(name) && !LOCAL_HEADERS.has(name))) {
      throw new RequestError(400);
    }
    headers[name] = value;
  }
  const authorization = headers.authorization;
  const match = authorization && /^Bearer ([A-Za-z0-9._~+/-]{1,4096}={0,2})$/i.exec(authorization);
  if (!match || !match[1]) throw new RequestError(401);
  if (headers.connection !== undefined && !/^(close|keep-alive)$/i.test(headers.connection)) throw new RequestError(400);
  if (headers['transfer-encoding'] !== undefined && headers['transfer-encoding'].toLowerCase() !== 'chunked') {
    throw new RequestError(400);
  }
  const declared = headers['content-length'];
  if (declared !== undefined) {
    if (!/^[0-9]+$/.test(declared)) throw new RequestError(400);
    if (Number(declared) > MAX_BODY_BYTES) throw new RequestError(413);
  }
  const forwarded = Object.fromEntries(Object.entries(headers).filter(([name]) => FORWARDED_HEADERS.has(name)));
  return { credential: match[1], forwarded };
}

async function readBody(request: IncomingMessage): Promise<Buffer> {
  const parts: Buffer[] = [];
  let length = 0;
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    const bytes: Buffer = chunk;
    length += bytes.length;
    if (length > MAX_BODY_BYTES) throw new RequestError(413);
    parts.push(bytes);
  }
  if (!request.complete || request.rawTrailers.length || (request.method === 'GET' && length)) throw new RequestError(400);
  return Buffer.concat(parts, length);
}

function reject(response: ServerResponse, status: number) {
  if (response.destroyed || response.writableEnded) return;
  if (response.headersSent) {
    response.destroy();
    return;
  }
  const codes: Record<number, string> = {
    400: 'invalid_request', 401: 'credential_required', 403: 'execution_not_authorized',
    405: 'method_not_allowed', 408: 'request_timeout', 413: 'request_too_large',
    417: 'expectation_unsupported', 429: 'ingress_busy', 502: 'upstream_failed', 503: 'authority_unavailable',
  };
  response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store', connection: 'close' });
  response.end(JSON.stringify({ error: { code: codes[status] } }));
}

export async function listenGuestEgress(options: GuestIngressOptions): Promise<{ close(): Promise<void> }> {
  if (!isIPv4(options.host) || options.host === '0.0.0.0' || !Number.isInteger(options.port)
      || options.port < 1 || options.port > 65535 || typeof options.authorize !== 'function') {
    throw new TypeError('guest_ingress_configuration_invalid');
  }
  const authorize = options.authorize;
  const transport = options.transport;
  const sockets = new Set<Socket>();
  let active = 0;
  let closing: Promise<void> | undefined;
  const server = createServer({
    key: options.key, cert: options.cert, minVersion: 'TLSv1.2', ALPNProtocols: ['http/1.1'],
    insecureHTTPParser: false, handshakeTimeout: READER_TIMEOUT_MS,
    maxHeaderSize: 16384, requestTimeout: READER_TIMEOUT_MS,
    headersTimeout: READER_TIMEOUT_MS, connectionsCheckingInterval: 1000,
  });
  server.maxHeadersCount = 0; // The parser's byte ceiling applies before duplicate checking.
  server.maxRequestsPerSocket = 1;
  server.maxConnections = 16;
  server.on('connection', (socket: Socket) => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  });
  server.on('connect', (_request, socket) => socket.destroy());
  server.on('upgrade', (_request, socket) => socket.destroy());
  server.on('checkContinue', (_request, response) => reject(response, 417));
  server.on('checkExpectation', (_request, response) => reject(response, 417));

  server.on('request', async (request, response) => {
    if (active >= 8) {
      reject(response, 429);
      return;
    }
    active++;
    const abort = new AbortController();
    const timedOut = setTimeout(() => {
      abort.abort();
      reject(response, 408);
    }, REQUEST_TIMEOUT_MS);
    let revoked: AbortSignal | undefined;
    let expires: NodeJS.Timeout | undefined;
    const deny = () => {
      abort.abort();
      reject(response, 403);
    };
    response.once('close', () => {
      if (!response.writableFinished) abort.abort();
    });
    let phase: 'request' | 'authority' | 'upstream' = 'request';
    let delivery: Promise<void> | undefined;
    let downstream: PassThrough | undefined;
    try {
      const { method, url: path } = request;
      if (method !== 'GET' && method !== 'POST') throw new RequestError(405);
      if (!path || !/^\/(?!\/)[^\x00-\x20\x7f\\#]{0,2047}$/.test(path)) throw new RequestError(400);
      const peerAddress = request.socket.remoteAddress;
      if (!peerAddress || !isIPv4(peerAddress)) throw new RequestError(400);
      const { credential, forwarded } = readHeaders(request);
      const body = await readBody(request);
      abort.signal.throwIfAborted();
      phase = 'authority';
      // A trusted authority can inspect bytes but cannot change what goes upstream.
      const permit = await authorize({ credential, peerAddress, method, path,
        headers: Object.freeze(forwarded), body: Buffer.from(body) }, abort.signal);
      abort.signal.throwIfAborted();
      if (permit === null) throw new RequestError(403);
      const { routeId, expiresAtMs, revoked: signal, confirmCurrent } = permit;
      if (!(signal instanceof AbortSignal) || !Number.isSafeInteger(expiresAtMs)
          || typeof confirmCurrent !== 'function' || typeof routeId !== 'string'
          || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(routeId)) {
        throw new RequestError(503);
      }
      const remaining = expiresAtMs - Date.now();
      if (remaining <= 0 || signal.aborted) throw new RequestError(403);
      revoked = signal;
      revoked.addEventListener('abort', deny, { once: true });
      expires = setTimeout(deny, Math.min(remaining, REQUEST_TIMEOUT_MS));
      phase = 'upstream';
      downstream = new PassThrough();
      const sink = downstream;
      await transport.exchange({ routeId, method, path, headers: forwarded, body }, {
        body: sink,
        onHeaders(status, headers) {
          response.writeHead(status, { ...headers, connection: 'close', 'cache-control': 'no-store' });
          delivery = pipeline(sink, response);
          void delivery.catch(() => abort.abort());
        },
      }, abort.signal, async () => {
        if (abort.signal.aborted || Date.now() >= expiresAtMs) return false;
        const confirmed = await confirmCurrent(abort.signal);
        return confirmed === true && !abort.signal.aborted && Date.now() < expiresAtMs;
      });
      await delivery;
    } catch (error) {
      if (!abort.signal.aborted) {
        let status = phase === 'authority' ? 503 : phase === 'request' ? 400 : 502;
        if (error instanceof RequestError) status = error.status;
        if (error instanceof EgressTransportError && error.code === 'dispatch_not_authorized') status = 403;
        if (error instanceof EgressTransportError && error.code === 'dispatch_authority_unavailable') status = 503;
        reject(response, status);
      }
    } finally {
      clearTimeout(timedOut);
      clearTimeout(expires);
      revoked?.removeEventListener('abort', deny);
      downstream?.destroy();
      active--;
    }
  });

  const listening = once(server, 'listening');
  server.listen({ host: options.host, port: options.port, exclusive: true });
  await listening;
  return {
    close() {
      if (!closing) {
        closing = new Promise<void>((resolve, rejectClose) => {
          server.close(error => error ? rejectClose(error) : resolve());
          for (const socket of sockets) socket.destroy();
        });
      }
      return closing;
    },
  };
}
