import { lookup } from 'node:dns/promises';
import { createHash } from 'node:crypto';
import { request as httpsRequest, Agent } from 'node:https';
import { validateHeaderName, validateHeaderValue } from 'node:http';
import type { ClientRequest, IncomingMessage } from 'node:http';
import type { RequestOptions } from 'node:https';
import { isIP } from 'node:net';
import { Transform, Writable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { checkServerIdentity, getCACertificates } from 'node:tls';
import type { TLSSocket } from 'node:tls';
import { AddressPolicy, canonicalAddress, PolicyError } from './policy.ts';
import type { Address, Destination, ExchangeRequest, Limits, Receipt, ResponseHeaders, Route, SecretHeaders, TransportOptions } from './types.ts';
export type * from './types.ts';

const defaults: Limits = Object.freeze({ maxConcurrent: 8, maxConcurrentPerRoute: 4,
  maxRequestBytes: 1024 * 1024, maxResponseBytes: 16 * 1024 * 1024, maxHeaderBytes: 16384,
  idleTimeoutMs: 10000, totalTimeoutMs: 120000 });
const guestHeaders = new Set(['accept', 'content-type', 'anthropic-version', 'anthropic-beta', 'openai-beta']);
const secretHeaders = new Set(['authorization', 'x-api-key', 'api-key', 'openai-organization', 'openai-project']);
const hopHeaders = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade']);
const responsePrivateHeaders = new Set(['authorization', 'proxy-authorization', 'set-cookie', 'www-authenticate']);

type FrozenRoute = { id: string; hostname: string; port: number; origin: string;
  operations: ReadonlySet<string>; secretRef: string; exceptions: ReadonlySet<string>; ca: readonly string[] | Buffer };
type MutableReceipt = { -readonly [P in keyof Receipt]: Receipt[P] };

export class EgressTransportError extends Error {
  readonly code: string;
  readonly receipt: Receipt;
  constructor(code: string, receipt: Receipt) {
    super(code); this.name = 'EgressTransportError'; this.code = code;
    this.receipt = Object.freeze({ ...receipt, outcome: 'failed' });
  }
}

function validPath(value: unknown): value is string {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')
    && !/[\u0000-\u0020\u007f\\#]/u.test(value) && value.length <= 2048;
}
function operation(method: unknown, path: unknown): string {
  if ((method !== 'GET' && method !== 'POST') || !validPath(path)) throw new PolicyError('request_operation_invalid');
  return method + ' ' + path;
}
function headerBytes(headers: Record<string, string | string[]>): number {
  return Object.entries(headers).reduce((total, [key, value]) => total + Buffer.byteLength(key)
    + (Array.isArray(value) ? value.reduce((size, part) => size + Buffer.byteLength(part) + 4, 0) : Buffer.byteLength(value) + 4), 2);
}
function copyHeaders(input: unknown, allowed: ReadonlySet<string>, code: string): Record<string, string> {
  if (typeof input !== 'object' || input === null || Array.isArray(input)) throw new PolicyError(code);
  const result: Record<string, string> = Object.create(null) as Record<string, string>;
  const entries = Object.entries(input);
  if (entries.length > 64) throw new PolicyError(code);
  for (const [original, value] of entries) {
    const name = original.toLowerCase();
    if (!allowed.has(name) || Object.hasOwn(result, name) || typeof value !== 'string') throw new PolicyError(code);
    try { validateHeaderName(original); validateHeaderValue(original, value); } catch { throw new PolicyError(code); }
    result[name] = value;
  }
  return result;
}
function filteredHeaders(response: IncomingMessage, maximum: number): ResponseHeaders {
  const nominated = new Set((response.headers.connection ?? '').split(',').map(value => value.trim().toLowerCase()));
  const result: Record<string, string | string[]> = Object.create(null) as Record<string, string | string[]>;
  for (const [name, value] of Object.entries(response.headers)) {
    if (value !== undefined && !hopHeaders.has(name) && !nominated.has(name) && !responsePrivateHeaders.has(name) && !secretHeaders.has(name)) {
      result[name] = Array.isArray(value) ? [...value] : value;
    }
  }
  if (headerBytes(result) > maximum) throw new PolicyError('response_headers_too_large');
  return Object.freeze(result);
}

export class UpstreamTransport {
  readonly configurationSha256: string;
  private readonly routes = new Map<string, FrozenRoute>();
  private readonly limits: Limits;
  private readonly getSecret: TransportOptions['getSecret'];
  private readonly resolve: NonNullable<TransportOptions['resolve']>;
  private readonly policy: AddressPolicy;
  private active = 0;
  private readonly activeRoutes = new Map<string, number>();

  constructor(options: TransportOptions) {
    this.limits = Object.freeze({ ...defaults, ...options.limits });
    for (const [name, value] of Object.entries(this.limits)) {
      if (!Object.hasOwn(defaults, name)) throw new PolicyError('limits_invalid');
      const maximum = name.endsWith('TimeoutMs') ? 300000
        : name === 'maxHeaderBytes' ? 65536
          : name === 'maxConcurrent' || name === 'maxConcurrentPerRoute' ? 64 : 64 * 1024 * 1024;
      if (!Number.isSafeInteger(value) || value < 1 || value > maximum) throw new PolicyError('limits_invalid');
    }
    if (typeof options.getSecret !== 'function' || (options.resolve !== undefined && typeof options.resolve !== 'function')) {
      throw new PolicyError('dependencies_invalid');
    }
    this.getSecret = options.getSecret;
    this.resolve = options.resolve ?? (async hostname => {
      const values = await lookup(hostname, { all: true, verbatim: true });
      return values.map(value => ({ address: value.address, family: value.family as 4 | 6 }));
    });
    if (options.deniedCidrs !== undefined && (!Array.isArray(options.deniedCidrs) || options.deniedCidrs.length > 128)) throw new PolicyError('denied_cidr_invalid');
    const denied = [...(options.deniedCidrs ?? [])];
    if (denied.length > 128) throw new PolicyError('denied_cidr_invalid');
    this.policy = new AddressPolicy(denied);
    if (!Array.isArray(options.routes) || options.routes.length === 0 || options.routes.length > 128) throw new PolicyError('routes_invalid');
    const descriptions: unknown[] = [];
    for (const source of options.routes) {
      const route = this.freezeRoute(source);
      if (this.routes.has(route.id)) throw new PolicyError('route_duplicate');
      this.routes.set(route.id, route);
      descriptions.push({ id: route.id, origin: route.origin, operations: [...route.operations].sort(),
        secretRef: route.secretRef, privateAddressExceptions: [...route.exceptions].sort(),
        caSha256: createHash('sha256').update(Buffer.isBuffer(route.ca) ? route.ca : route.ca.join('\n')).digest('hex') });
    }
    this.configurationSha256 = createHash('sha256').update(JSON.stringify({ version: 1,
      routes: descriptions, deniedCidrs: denied.sort(), limits: this.limits })).digest('hex');
  }

  private freezeRoute(source: Route): FrozenRoute {
    if (!source || typeof source.id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(source.id)
        || typeof source.secretRef !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_./:-]{0,127}$/.test(source.secretRef)
        || typeof source.origin !== 'string' || /[\u0000-\u0020\u007f\\]/u.test(source.origin)) throw new PolicyError('route_invalid');
    let origin: URL;
    try { origin = new URL(source.origin); } catch { throw new PolicyError('route_origin_invalid'); }
    const port = Number(origin.port || 443);
    if (!Number.isSafeInteger(port) || port < 1 || port > 65535 || origin.protocol !== 'https:' || origin.username || origin.password || origin.pathname !== '/'
        || origin.search || origin.hash || origin.hostname.endsWith('.') || isIP(origin.hostname.replace(/^\[|\]$/g, '')) !== 0) {
      throw new PolicyError('route_origin_invalid');
    }
    if (!Array.isArray(source.operations) || !source.operations.length || source.operations.length > 64) throw new PolicyError('route_invalid');
    const operations = new Set(source.operations.map(value => operation(value.method, value.path)));
    let ca: readonly string[] | Buffer;
    if (source.ca === undefined) ca = Object.freeze([...getCACertificates('bundled')]);
    else {
      if ((!Buffer.isBuffer(source.ca) && typeof source.ca !== 'string') || Buffer.byteLength(source.ca) > 1024 * 1024 || Buffer.byteLength(source.ca) === 0) {
        throw new PolicyError('route_ca_invalid');
      }
      ca = Buffer.from(source.ca);
    }
    return { id: source.id, hostname: origin.hostname, port, origin: origin.origin,
      secretRef: source.secretRef, operations, exceptions: this.policy.exceptions(source.privateAddressExceptions ?? []), ca };
  }

  exchange(input: ExchangeRequest, destination: Destination, signal?: AbortSignal): Promise<Receipt> {
    const state: MutableReceipt = { routeId: '',
      requestExposure: 'not_sent', requestBodyBytes: 0, responseBodyBytes: 0, upstreamStatus: null, outcome: 'failed' };
    const reject = (code: string) => Promise.reject<Receipt>(new EgressTransportError(code, state));
    let route: FrozenRoute;
    let headers: Record<string, string>;
    let body: Buffer;
    let method: 'GET' | 'POST';
    let path: string;
    let target: Destination;
    try {
      const found = this.routes.get(input?.routeId);
      if (!found) return reject('route_unknown');
      route = found; state.routeId = route.id;
      method = input.method; path = input.path;
      if (!route.operations.has(operation(method, path))) return reject('request_not_allowed');
      headers = copyHeaders(input.headers ?? {}, guestHeaders, 'request_header_rejected');
      if (headerBytes(headers) > this.limits.maxHeaderBytes) return reject('request_headers_too_large');
      const suppliedBody = input.body;
      if (suppliedBody !== undefined && !Buffer.isBuffer(suppliedBody)) return reject('request_body_invalid');
      if ((suppliedBody?.length ?? 0) > this.limits.maxRequestBytes) return reject('request_body_too_large');
      body = Buffer.from(suppliedBody ?? Buffer.alloc(0));
      if (method === 'GET' && body.length) return reject('request_body_invalid');
      state.requestBodyBytes = body.length;
      if (!destination || typeof destination.onHeaders !== 'function' || !(destination.body instanceof Writable)
          || destination.body.destroyed || destination.body.writableEnded || destination.body.writableObjectMode) return reject('destination_invalid');
      target = { onHeaders: destination.onHeaders, body: destination.body };
      if (signal?.aborted) return reject('aborted');
    } catch (error) { return reject(error instanceof PolicyError ? error.code : 'request_invalid'); }
    const count = this.activeRoutes.get(route.id) ?? 0;
    if (this.active >= this.limits.maxConcurrent || count >= this.limits.maxConcurrentPerRoute) return reject('concurrency_exceeded');
    this.active++; this.activeRoutes.set(route.id, count + 1);

    return new Promise<Receipt>((resolve, rejectPromise) => {
      let failure: EgressTransportError | undefined;
      let settled = false;
      let headersCommitted = false;
      let request: ClientRequest | undefined;
      let response: IncomingMessage | undefined;
      let agent: Agent | undefined;
      let idleTimer: NodeJS.Timeout | undefined;
      const fail = (code: string) => {
        if (settled) return;
        failure = new EgressTransportError(code, state);
        settled = true;
        clearTimeout(totalTimer); if (idleTimer) clearTimeout(idleTimer);
        request?.destroy(); response?.destroy(); agent?.destroy();
        if (headersCommitted && !target.body.destroyed) target.body.destroy();
        rejectPromise(failure);
      };
      const abort = () => fail('aborted');
      const sinkError = (error: Error) => fail(error instanceof PolicyError ? error.code : 'destination_failed');
      const sinkClose = () => { if (!target.body.writableFinished) fail('destination_closed'); };
      const totalTimer = setTimeout(() => fail('total_timeout'), this.limits.totalTimeoutMs);
      const guard = () => { if (failure) throw failure; };
      const idle = () => {
        if (settled) return;
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(() => fail('idle_timeout'), this.limits.idleTimeoutMs);
      };
      signal?.addEventListener('abort', abort, { once: true });
      target.body.on('error', sinkError); target.body.on('close', sinkClose);
      // A cancellation race after attaching the listener must not start work.
      if (signal?.aborted) abort();
      const run = async () => {
        try {
          guard();
          let answers: readonly Address[];
          try { answers = await this.resolve(route.hostname); } catch { guard(); throw new PolicyError('dns_failed'); }
          guard();
          const pinned = this.policy.checkAll(answers, route.exceptions);
          let credential: SecretHeaders;
          try { credential = await this.getSecret(route.secretRef); } catch { guard(); throw new PolicyError('secret_unavailable'); }
          guard();
          const auth = copyHeaders(credential, secretHeaders, 'secret_invalid');
          const outbound = { ...headers, ...auth, host: new URL(route.origin).host, connection: 'close', 'content-length': String(body.length) };
          if (headerBytes(outbound) + Buffer.byteLength(method + ' ' + path + ' HTTP/1.1\r\n') > this.limits.maxHeaderBytes) throw new PolicyError('request_headers_too_large');
          agent = new Agent({ keepAlive: false, maxSockets: 1, maxTotalSockets: 1, maxCachedSessions: 0, proxyEnv: {} });
          const options: RequestOptions & { autoSelectFamily: false } = { hostname: route.hostname, port: route.port, path,
            method, headers: outbound, agent, family: pinned.family, autoSelectFamily: false,
            lookup: (_hostname, _options, callback) => callback(null, pinned.address, pinned.family),
            servername: route.hostname, rejectUnauthorized: true, checkServerIdentity,
            ca: Buffer.isBuffer(route.ca) ? route.ca : [...route.ca], minVersion: 'TLSv1.2',
            maxHeaderSize: this.limits.maxHeaderBytes, insecureHTTPParser: false };
          idle();
          response = await new Promise<IncomingMessage>((resolveResponse, rejectResponse) => {
            request = httpsRequest(options, resolveResponse);
            request.maxHeadersCount = 0; // Byte cap rejects rather than silently truncating headers.
            request.on('error', () => { rejectResponse(new PolicyError('upstream_failed')); });
            request.on('upgrade', (_message, socket) => { socket.destroy(); rejectResponse(new PolicyError('upstream_upgrade_rejected')); });
            request.on('connect', (_message, socket) => { socket.destroy(); rejectResponse(new PolicyError('upstream_upgrade_rejected')); });
            request.on('socket', socket => {
              socket.once('connect', () => {
                try {
                  if (!socket.remoteAddress || canonicalAddress(socket.remoteAddress).address !== pinned.address) {
                    fail('peer_address_mismatch'); return;
                  }
                  idle();
                } catch { fail('peer_address_mismatch'); }
              });
              (socket as TLSSocket).once('secureConnect', idle);
            });
            guard();
            state.requestExposure = 'may_have_been_sent';
            request.end(body);
          });
          guard(); idle();
          const status = response.statusCode;
          if (status === undefined || status < 200 || status > 599) throw new PolicyError('upstream_status_invalid');
          state.upstreamStatus = status;
          if (status >= 300 && status < 400) throw new PolicyError('upstream_redirect_rejected');
          const forwarded = filteredHeaders(response, this.limits.maxHeaderBytes);
          const length = response.headers['content-length'];
          if (length !== undefined && (!/^[0-9]+$/.test(length) || Number(length) > this.limits.maxResponseBytes)) {
            throw new PolicyError('response_too_large');
          }
          headersCommitted = true;
          let callbackResult: unknown;
          try { callbackResult = target.onHeaders(status, forwarded); } catch { throw new PolicyError('destination_failed'); }
          if (callbackResult !== null && typeof callbackResult === 'object' && 'then' in callbackResult) {
            try { await Promise.resolve(callbackResult); } catch { /* Unsupported async callbacks still retain their permit until settlement. */ }
            guard();
            throw new PolicyError('destination_callback_async');
          }
          guard();
          const maximum = this.limits.maxResponseBytes;
          const counter = new Transform({ transform(chunk: Buffer, _encoding, done) {
            state.responseBodyBytes += chunk.length;
            if (state.responseBodyBytes > maximum) done(new PolicyError('response_too_large'));
            else { idle(); done(null, chunk); }
          } });
          await pipeline(response, counter, target.body);
          guard();
          if (!response.complete || !target.body.writableFinished) throw new PolicyError('response_incomplete');
          state.outcome = 'completed'; settled = true;
          resolve(Object.freeze({ ...state }));
        } catch (error) {
          if (!settled) fail(error instanceof PolicyError ? error.code : 'upstream_failed');
        } finally {
          clearTimeout(totalTimer); if (idleTimer) clearTimeout(idleTimer);
          signal?.removeEventListener('abort', abort);
          target.body.off('error', sinkError); target.body.off('close', sinkClose);
          request?.destroy(); response?.destroy(); agent?.destroy();
          this.active--; const remaining = (this.activeRoutes.get(route.id) ?? 1) - 1;
          if (remaining) this.activeRoutes.set(route.id, remaining); else this.activeRoutes.delete(route.id);
        }
      };
      // Uncancellable DNS/secret work owns its permit until the worker settles,
      // even when the caller has already received a deadline/abort rejection.
      void run();
    });
  }
}
