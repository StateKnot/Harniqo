import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import type { ClientRequest, IncomingMessage } from 'node:http';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { HermesAdapter } from './adapter.ts';
import type { HermesAdapterOptions, HermesCompletedRun } from './adapter.ts';

type NativeResponse = { status: number; data?: unknown; raw?: Buffer; hold?: boolean };
const nativeSessionId = 'harniqo_0123456789abcdef';
const nativeRunId = 'run_0123456789abcdef0123456789abcdef';
const input = { nativeSessionId, text: '请输出测试回执', idempotencyKey: 'harniqo-0123456789abcdef' };
const completed = { nativeRunId, nativeSessionId, status: 'completed', output: '回执\n就绪' } satisfies HermesCompletedRun;

function harness(...responses: NativeResponse[]) {
  const requests: { path: string; method: unknown; headers: unknown; body: unknown }[] = [];
  const request: NonNullable<HermesAdapterOptions['request']> = (url, options, callback) => {
    const outgoing = new EventEmitter();
    return Object.assign(outgoing, {
      end(payload?: Buffer) {
        requests.push({ path: url.pathname + url.search, method: options.method,
          headers: options.headers, body: payload ? JSON.parse(payload.toString()) : undefined });
        const fixture = responses.shift();
        assert.ok(fixture, 'unexpected native request');
        const body = new PassThrough();
        const response = Object.assign(body, {
          statusCode: fixture.status, headers: { 'content-type': 'application/json; charset=utf-8' },
          complete: !fixture.hold,
        }) as unknown as IncomingMessage;
        options.signal?.addEventListener('abort', () => body.destroy(new Error('network details withheld')), { once: true });
        callback(response);
        if (!fixture.hold) body.end(fixture.raw ?? JSON.stringify(fixture.data));
      },
    }) as unknown as ClientRequest;
  };
  return { requests, adapter: new HermesAdapter({ origin: 'http://127.0.0.1:10010', token: 'synthetic-test-token', request }) };
}

test('creates or attaches the exact session and preserves native run and replay identity', async () => {
  const { requests, adapter } = harness(
    { status: 201, data: { object: 'hermes.session', session: { id: nativeSessionId } } },
    { status: 409, data: { error: { code: 'session_exists' } } },
    { status: 200, data: { object: 'hermes.session', session: { id: nativeSessionId } } },
    { status: 202, data: { run_id: nativeRunId, status: 'queued', replayed: false } },
    { status: 202, data: { run_id: nativeRunId, status: 'completed', replayed: true } },
  );
  assert.deepEqual(await adapter.attach(nativeSessionId, '测试'), { nativeSessionId });
  assert.deepEqual(await adapter.attach(nativeSessionId, '测试'), { nativeSessionId });
  assert.deepEqual(await adapter.start(input), { nativeRunId, replayed: false });
  assert.deepEqual(await adapter.start(input), { nativeRunId, replayed: true });
  assert.deepEqual(requests.map(({ path, method, body }) => [method, path, body]), [
    ['POST', '/api/sessions', { id: nativeSessionId, title: '测试' }],
    ['POST', '/api/sessions', { id: nativeSessionId, title: '测试' }],
    ['GET', `/api/sessions/${nativeSessionId}`, undefined],
    ['POST', '/v1/runs', { session_id: nativeSessionId, input: input.text }],
    ['POST', '/v1/runs', { session_id: nativeSessionId, input: input.text }],
  ]);
  for (const request of requests.slice(-2)) assert.partialDeepStrictEqual(request.headers, {
    authorization: 'Bearer synthetic-test-token', 'idempotency-key': input.idempotencyKey,
  });
});

test('reads only the persisted assistant answer that matches the completed native run', async () => {
  const { adapter } = harness(
    { status: 200, data: { run_id: nativeRunId, session_id: nativeSessionId, status: 'completed',
      output: completed.output, model: 'virtual-model', runtime: { model: 'provider/model-v42' } } },
    { status: 200, data: { session_id: nativeSessionId, data: [
      { role: 'user', content: input.text },
      { role: 'assistant', content: 'Checking', tool_calls: [{ id: 'tool_1' }] },
      { role: 'tool', content: completed.output },
      { role: 'assistant', content: [{ type: 'text', text: '回执' }, { type: 'image_url' }, { type: 'output_text', text: '就绪' }] },
      { role: 'assistant', display_kind: 'hidden', content: 'internal diagnostic' },
    ] } },
  );
  const run = await adapter.inspect(nativeRunId, nativeSessionId);
  assert.deepEqual(run, { ...completed, model: 'provider/model-v42' });
  assert.equal(run.status === 'completed' && await adapter.readReply(run), completed.output);
});

test('does not substitute requested model, an earlier answer, or the payload of a tool', async () => {
  const { adapter } = harness(
    { status: 200, data: { run_id: nativeRunId, session_id: nativeSessionId, status: 'unknown', model: 'requested-model' } },
    { status: 200, data: { session_id: nativeSessionId, data: [
      { role: 'assistant', content: completed.output }, { role: 'user', content: '下一轮' },
      { role: 'tool', content: completed.output },
    ] } },
    { status: 200, data: { session_id: nativeSessionId, data: [{ role: 'assistant', content: '另一个回复' }] } },
  );
  assert.deepEqual(await adapter.inspect(nativeRunId, nativeSessionId), { nativeRunId, nativeSessionId, status: 'unknown' });
  assert.equal(await adapter.readReply(completed), undefined);
  assert.equal(await adapter.readReply(completed), undefined);
});

test('rejects deleted IDs, conflicting run identities, plaintext remote origins and redirects', async () => {
  const { requests, adapter } = harness(
    { status: 409, data: { error: { code: 'session_deleted', message: 'private diagnostics' } } },
    { status: 200, data: { run_id: nativeRunId, session_id: 'unrelated', status: 'running' } },
    { status: 307, data: { message: 'private diagnostics' } },
  );
  assert.throws(() => new HermesAdapter({ origin: 'http://192.168.2.11:10010', token: 'synthetic' }), { code: 'invalid_origin' });
  assert.throws(() => new HermesAdapter({ origin: 'https://example.test/../', token: 'synthetic' }), { code: 'invalid_origin' });
  await assert.rejects(adapter.attach(nativeSessionId), { code: 'conflict', status: 409, message: 'conflict' });
  await assert.rejects(adapter.inspect(nativeRunId, nativeSessionId), { code: 'identity_mismatch', status: 200 });
  await assert.rejects(adapter.start(input), { code: 'redirect_rejected', status: 307 });
  assert.equal(requests.length, 3);
});

test('bounds streamed JSON bytes and respects cancellation during the response body', async () => {
  const { adapter } = harness({ status: 201, raw: Buffer.alloc(2 * 1024 * 1024 + 1) }, { status: 200, hold: true });
  await assert.rejects(adapter.attach(nativeSessionId), { code: 'response_too_large', status: 201 });
  const cancellation = new AbortController();
  const pending = adapter.inspect(nativeRunId, nativeSessionId, cancellation.signal);
  cancellation.abort('private reason');
  await assert.rejects(pending, { code: 'aborted', message: 'aborted' });
});
