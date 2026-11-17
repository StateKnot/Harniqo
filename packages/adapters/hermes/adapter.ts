import { request as httpRequest } from 'node:http';
import type { ClientRequest, IncomingMessage } from 'node:http';
import { request as httpsRequest } from 'node:https';
import type { RequestOptions } from 'node:https';

const sessionIdPattern = /^[A-Za-z0-9_:-][A-Za-z0-9_.:-]{0,159}$/;
const runIdPattern = /^run_[0-9a-f]{32}$/;
const requestKeyPattern = /^[A-Za-z0-9._:-]{16,128}$/;
const maxRequestBytes = 64 * 1024;
const maxResponseBytes = 2 * 1024 * 1024;
const historyLimit = 32;

export type HermesAdapterErrorCode = 'invalid_origin' | 'invalid_input' | 'invalid_response'
  | 'identity_mismatch' | 'redirect_rejected' | 'response_too_large' | 'unauthorized'
  | 'not_found' | 'conflict' | 'upstream_http_error' | 'network_error' | 'timeout' | 'aborted';

export class HermesAdapterError extends Error {
  readonly code: HermesAdapterErrorCode;
  readonly status: number | undefined;

  constructor(code: HermesAdapterErrorCode, status?: number) {
    super(code);
    this.name = 'HermesAdapterError';
    this.code = code;
    this.status = status;
  }
}

type Request = (url: URL, options: RequestOptions, callback: (response: IncomingMessage) => void) => ClientRequest;
type JsonResponse = { status: number; body: Record<string, unknown> };

export type HermesAdapterOptions = Readonly<{
  origin: string;
  token: string;
  timeoutMs?: number;
  request?: Request;
}>;

export type HermesStartInput = Readonly<{
  nativeSessionId: string;
  text: string;
  idempotencyKey: string;
}>;

const nonCompletedStatuses = [
  'queued', 'running', 'waiting_for_approval', 'stopping', 'failed', 'cancelled', 'interrupted', 'unknown',
] as const;
type NonCompletedStatus = typeof nonCompletedStatuses[number];
const nonCompletedStatusSet: ReadonlySet<unknown> = new Set(nonCompletedStatuses);

type RunIdentity = Readonly<{ nativeRunId: string; nativeSessionId: string; model?: string }>;
export type HermesCompletedRun = RunIdentity & Readonly<{ status: 'completed'; output: string }>;
export type HermesRun = HermesCompletedRun | (RunIdentity & Readonly<{ status: NonCompletedStatus }>);

function object(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function parseOrigin(value: string): URL {
  const origin = URL.parse(value);
  const exactOrigin = /^https?:\/\/[^/@?#\\\s\u0000-\u001f\u007f]+\/?$/u;
  const loopbackHttp = /^http:\/\/(?:127\.0\.0\.1|\[::1\])(?::\d+)?\/?$/u;
  if (!origin?.hostname || !exactOrigin.test(value) || origin.pathname !== '/'
    || (origin.protocol === 'http:' && !loopbackHttp.test(value))) {
    throw new HermesAdapterError('invalid_origin');
  }
  return origin;
}

function json(raw: Buffer, status: number): Record<string, unknown> {
  try {
    const decoded = object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)));
    if (decoded) return decoded;
  } catch {
    // Keep upstream text and transport diagnostics out of errors passed to callers.
  }
  throw new HermesAdapterError('invalid_response', status);
}

function httpFailure(status: number): HermesAdapterError {
  if (status >= 300 && status < 400) return new HermesAdapterError('redirect_rejected', status);
  if (status === 401 || status === 403) return new HermesAdapterError('unauthorized', status);
  if (status === 404) return new HermesAdapterError('not_found', status);
  if (status === 409) return new HermesAdapterError('conflict', status);
  return new HermesAdapterError('upstream_http_error', status);
}

function assistantText(content: unknown): string | undefined {
  if (typeof content === 'string') return content.trim() ? content : undefined;
  if (!Array.isArray(content)) return undefined;
  const parts = content.flatMap((value: unknown) => {
    if (typeof value === 'string') return [value];
    const part = object(value);
    if (part && (part.type === 'text' || part.type === 'output_text') && typeof part.text === 'string') {
      return [part.text];
    }
    return [];
  });
  const text = parts.join('\n');
  return text.trim() ? text : undefined;
}

function comparableText(text: string): string {
  return text.replaceAll('\r\n', '\n').trim();
}

function visibleToolCalls(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  // Some serialized messages carry JSON empty lists in this field.
  return value != null && value !== '' && value !== '[]';
}

export class HermesAdapter {
  readonly #origin: URL;
  readonly #token: string;
  readonly #timeoutMs: number;
  readonly #request: Request;

  constructor(options: HermesAdapterOptions) {
    this.#origin = parseOrigin(options.origin);
    this.#timeoutMs = options.timeoutMs ?? 10_000;
    if (!/^[\x21-\x7e]{1,1024}$/.test(options.token)
      || !Number.isSafeInteger(this.#timeoutMs) || this.#timeoutMs < 1 || this.#timeoutMs > 60_000) {
      throw new HermesAdapterError('invalid_input');
    }
    this.#token = options.token;
    this.#request = options.request ?? (this.#origin.protocol === 'https:' ? httpsRequest : httpRequest);
  }

  // The caller allocates and persists this ID within the deployment's authorized scope before attaching.
  async attach(nativeSessionId: string, title?: string, signal?: AbortSignal): Promise<{ nativeSessionId: string }> {
    this.#checkSessionId(nativeSessionId);
    if (title !== undefined && [...title].length > 100) throw new HermesAdapterError('invalid_input');
    const created = await this.#json('/api/sessions', [201, 409], signal, { id: nativeSessionId, title });
    let session = created.body.session;
    if (created.status === 409) {
      if (object(created.body.error)?.code !== 'session_exists') throw httpFailure(409);
      const found = await this.#json(`/api/sessions/${encodeURIComponent(nativeSessionId)}`, [200], signal);
      session = found.body.session;
    }
    if (object(session)?.id !== nativeSessionId) throw new HermesAdapterError('identity_mismatch');
    return { nativeSessionId };
  }

  async start(input: HermesStartInput, signal?: AbortSignal): Promise<{ nativeRunId: string; replayed: boolean }> {
    this.#checkSessionId(input.nativeSessionId);
    if (input.text.length > maxRequestBytes || !input.text.trim() || !requestKeyPattern.test(input.idempotencyKey)) {
      throw new HermesAdapterError('invalid_input');
    }
    const { body } = await this.#json('/v1/runs', [202], signal, { session_id: input.nativeSessionId, input: input.text },
      input.idempotencyKey);
    if (typeof body.run_id !== 'string' || !runIdPattern.test(body.run_id) || typeof body.replayed !== 'boolean') {
      throw new HermesAdapterError('invalid_response', 202);
    }
    return { nativeRunId: body.run_id, replayed: body.replayed };
  }

  async inspect(nativeRunId: string, nativeSessionId: string, signal?: AbortSignal): Promise<HermesRun> {
    this.#checkSessionId(nativeSessionId);
    if (!runIdPattern.test(nativeRunId)) throw new HermesAdapterError('invalid_input');
    const { body } = await this.#json(`/v1/runs/${nativeRunId}`, [200], signal);
    if (body.run_id !== nativeRunId || body.session_id !== nativeSessionId) {
      throw new HermesAdapterError('identity_mismatch', 200);
    }
    const servedModel = object(body.runtime)?.model;
    const model = typeof servedModel === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(servedModel)
      && !servedModel.includes('://') ? servedModel : undefined;
    const identity = { nativeRunId, nativeSessionId, ...(model ? { model } : {}) };
    if (body.status === 'completed') {
      if (typeof body.output !== 'string' || (Object.hasOwn(body, 'completed') && body.completed !== true)
        || (Object.hasOwn(body, 'partial') && body.partial !== false)
        || (Object.hasOwn(body, 'interrupted') && body.interrupted !== false)) {
        throw new HermesAdapterError('invalid_response', 200);
      }
      return { ...identity, status: 'completed', output: body.output };
    }
    if (!nonCompletedStatusSet.has(body.status)) throw new HermesAdapterError('invalid_response', 200);
    return { ...identity, status: body.status as NonCompletedStatus };
  }

  async readReply(run: HermesCompletedRun, signal?: AbortSignal): Promise<string | undefined> {
    this.#checkSessionId(run.nativeSessionId);
    if (!runIdPattern.test(run.nativeRunId) || run.status !== 'completed') throw new HermesAdapterError('invalid_input');
    const expected = comparableText(run.output);
    if (!expected) return undefined;
    const { body } = await this.#json(
      `/api/sessions/${encodeURIComponent(run.nativeSessionId)}/messages?limit=${historyLimit}&order=latest`,
      [200], signal);
    if (body.session_id !== run.nativeSessionId) throw new HermesAdapterError('identity_mismatch', 200);
    if (!Array.isArray(body.data) || body.data.length > historyLimit) {
      throw new HermesAdapterError('invalid_response', 200);
    }
    // Hermes returns latest pages in chronological order. Verify the most recent visible
    // assistant reply matches this run's output and stop at the next visible user boundary.
    for (let index = body.data.length - 1; index >= 0; index--) {
      const message = object(body.data[index]);
      if (!message || typeof message.role !== 'string') throw new HermesAdapterError('invalid_response', 200);
      if (message.display_kind === 'hidden') continue;
      if (message.role === 'user') break;
      if (message.role !== 'assistant' || visibleToolCalls(message.tool_calls)) continue;
      const text = assistantText(message.content);
      if (text !== undefined) return comparableText(text) === expected ? text : undefined;
    }
    return undefined;
  }

  #checkSessionId(nativeSessionId: string): void {
    if (!sessionIdPattern.test(nativeSessionId)) throw new HermesAdapterError('invalid_input');
  }

  #json(path: string, expected: readonly number[], signal?: AbortSignal, body?: Record<string, unknown>,
    idempotencyKey?: string): Promise<JsonResponse> {
    const payload = body && Buffer.from(JSON.stringify(body));
    if (payload && payload.length > maxRequestBytes) throw new HermesAdapterError('invalid_input');
    const deadline = AbortSignal.timeout(this.#timeoutMs);
    const effectiveSignal = signal ? AbortSignal.any([signal, deadline]) : deadline;
    const errorCode = (): HermesAdapterErrorCode => signal?.aborted ? 'aborted'
      : deadline.aborted ? 'timeout' : 'network_error';
    const headers = { accept: 'application/json', authorization: `Bearer ${this.#token}`,
      ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}),
      ...(idempotencyKey ? { 'idempotency-key': idempotencyKey } : {}) };

    return new Promise<JsonResponse>((resolve, reject) => {
      if (effectiveSignal.aborted) {
        reject(new HermesAdapterError(errorCode()));
        return;
      }
      const outgoing = this.#request(new URL(path, this.#origin), {
        method: payload ? 'POST' : 'GET', headers, signal: effectiveSignal,
        agent: false, maxHeaderSize: 8192, rejectUnauthorized: true, minVersion: 'TLSv1.2',
      }, async response => {
        try {
          const status = response.statusCode;
          if (!status) throw new HermesAdapterError('invalid_response');
          if (!expected.includes(status)) throw httpFailure(status);
          if (!/^application\/json(?:\s*;|$)/i.test(response.headers['content-type'] ?? '')) {
            throw new HermesAdapterError('invalid_response', status);
          }
          if (Number(response.headers['content-length']) > maxResponseBytes) {
            throw new HermesAdapterError('response_too_large', status);
          }
          const chunks: Buffer[] = [];
          let size = 0;
          for await (const chunk of response) {
            const bytes: Buffer = chunk;
            size += bytes.length;
            if (size > maxResponseBytes) throw new HermesAdapterError('response_too_large', status);
            chunks.push(bytes);
          }
          if (!response.complete || effectiveSignal.aborted) throw new HermesAdapterError(errorCode());
          resolve({ status, body: json(Buffer.concat(chunks, size), status) });
        } catch (error) {
          response.destroy();
          reject(error instanceof HermesAdapterError ? error : new HermesAdapterError(errorCode()));
        }
      });
      outgoing.once('error', () => reject(new HermesAdapterError(errorCode())));
      outgoing.end(payload);
    }).catch((error: unknown) => {
      throw error instanceof HermesAdapterError ? error : new HermesAdapterError(errorCode());
    });
  }
}
