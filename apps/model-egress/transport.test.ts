import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'node:https';
import { mkdtemp, readFile, rm, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { Writable } from 'node:stream';
import type { Duplex } from 'node:stream';
import type { IncomingHttpHeaders } from 'node:http';
import { UpstreamTransport, EgressTransportError } from './transport.ts';
import { AddressPolicy, canonicalAddress, PolicyError } from './policy.ts';
import type { Destination, Limits, Route, ExchangeRequest, TransportOptions } from './types.ts';

let folder: string;
let ca: Buffer;
const port = 10040;
const sockets = new Set<Duplex>();
const seen: { method: string | undefined; path: string | undefined; body: Buffer; headers: IncomingHttpHeaders; socket: Duplex }[] = [];
let server: ReturnType<typeof createServer>;
const operations: Route['operations'] = ['/ok', '/error', '/sse', '/redirect', '/large', '/chunked-large', '/idle', '/stream-idle', '/upgrade']
  .flatMap(path => [{ method: 'GET' as const, path }, { method: 'POST' as const, path }]);

before(async () => {
  folder = await mkdtemp(join(tmpdir(), 'harniqo-egress-tls-'));
  await chmod(folder, 0o700);
  const key = join(folder, 'key.pem'); const cert = join(folder, 'cert.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
    '-out', cert, '-days', '2', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], { stdio: 'ignore' });
  await chmod(key, 0o600); await chmod(cert, 0o600);
  ca = await readFile(cert);
  server = createServer({ key: await readFile(key), cert: ca }, (request, response) => {
    const parts: Buffer[] = [];
    request.on('data', chunk => parts.push(Buffer.from(chunk)));
    request.on('end', () => {
      seen.push({ method: request.method, path: request.url, body: Buffer.concat(parts), headers: request.headers, socket: request.socket });
      if (request.url === '/redirect') { response.writeHead(302, { location: 'https://elsewhere.invalid/ok' }); response.end('redirect'); }
      else if (request.url === '/error') {
        response.writeHead(429, { 'content-type': 'application/json', 'retry-after': '3',
          connection: 'keep-alive, x-hop', 'x-hop': 'removed', 'set-cookie': 'removed', 'x-api-key': 'removed' });
        response.end('{"error":"rate_limit"}');
      } else if (request.url === '/sse') {
        response.writeHead(200, { 'content-type': 'text/event-stream' });
        response.write('data: first\n\n');
        setTimeout(() => response.end('data: [DONE]\n\n'), 15);
      } else if (request.url === '/large') { response.writeHead(200, { 'content-length': '128' }); response.end('x'.repeat(128)); }
      else if (request.url === '/chunked-large') { response.writeHead(200); response.write('x'.repeat(64)); setTimeout(() => response.end('y'.repeat(64)), 10); }
      else if (request.url === '/idle') { /* Client must end this request by its deadline. */ }
      else if (request.url === '/stream-idle') { response.writeHead(200); response.write('first'); }
      else if (request.url === '/upgrade') { response.writeHead(101, { connection: 'Upgrade', upgrade: 'test' }); response.end(); }
      else { response.writeHead(200, { 'content-type': 'application/octet-stream' }); response.end(Buffer.concat(parts).length ? Buffer.concat(parts) : Buffer.from('ok')); }
    });
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  server.on('tlsClientError', () => undefined);
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
});
after(async () => {
  for (const socket of sockets) socket.destroy();
  await new Promise<void>(resolve => server.close(() => resolve()));
  await rm(folder, { recursive: true, force: true });
});

function route(changes: Partial<Route> = {}): Route {
  return { id: 'fixture', origin: 'https://localhost:' + port, operations, secretRef: 'provider/test',
    privateAddressExceptions: ['127.0.0.1', '::1'], ca, ...changes };
}
function transport(limits: Partial<Limits> = {}, options: Partial<TransportOptions> = {}): UpstreamTransport {
  return new UpstreamTransport({ routes: [route()], getSecret: async () => ({ authorization: 'Bearer fixture-only-value' }),
    limits: { idleTimeoutMs: 300, totalTimeoutMs: 2000, ...limits }, ...options });
}
function destination(delay = 0): Destination & { chunks: Buffer[]; status: number | null; headers: Record<string, string | string[]> } {
  const chunks: Buffer[] = [];
  const value = { chunks, status: null as number | null, headers: {} as Record<string, string | string[]>,
    body: new Writable({ write(chunk: Buffer, _encoding, callback) { chunks.push(Buffer.from(chunk)); setTimeout(callback, delay); } }),
    onHeaders(status: number, headers: Readonly<Record<string, string | string[]>>) { value.status = status; value.headers = { ...headers }; } };
  return value;
}
const input = (path = '/ok', body = Buffer.alloc(0)): ExchangeRequest => ({ routeId: 'fixture', method: 'POST', path, body, headers: { 'content-type': 'application/json' } });
async function errorCode(task: Promise<unknown>, code: string) {
  await assert.rejects(task, error => error instanceof EgressTransportError && error.code === code);
}
const tick = () => new Promise<void>(resolve => setTimeout(resolve, 20));

test('real TLS preserves exact request/SSE bytes, hostname and fresh provider credentials', async () => {
  let version = 0;
  const client = transport({}, { getSecret: async () => ({ authorization: 'Bearer version-' + ++version }) });
  const first = destination(); const body = Buffer.from('{"model":"fixed","messages":[]}');
  const receipt = await client.exchange(input('/ok', body), first);
  assert.equal(receipt.outcome, 'completed'); assert.equal(receipt.requestExposure, 'may_have_been_sent');
  assert.deepEqual(Buffer.concat(first.chunks), body); assert.equal(first.status, 200);
  assert.equal(seen.at(-1)?.headers.authorization, 'Bearer version-1'); assert.equal(seen.at(-1)?.headers.host, 'localhost:' + port);
  const socket = seen.at(-1)?.socket;
  const second = destination(10); await client.exchange(input('/sse'), second);
  assert.equal(Buffer.concat(second.chunks).toString(), 'data: first\n\ndata: [DONE]\n\n');
  assert.equal(seen.at(-1)?.headers.authorization, 'Bearer version-2'); assert.notEqual(seen.at(-1)?.socket, socket);
  assert.equal(second.body.writableFinished, true);
});

test('request operation/body/header rejection happens before DNS or provider access', async () => {
  let calls = 0;
  const client = transport({ maxRequestBytes: 8 }, { resolve: async () => { calls++; return []; }, getSecret: async () => { calls++; return {}; } });
  for (const header of ['authorization', 'host', 'connection', 'proxy-authorization', 'x-unknown']) {
    await errorCode(client.exchange({ ...input(), headers: { [header]: 'not-forwarded' } }, destination()), 'request_header_rejected');
  }
  await errorCode(client.exchange(input('/ok', Buffer.alloc(9)), destination()), 'request_body_too_large');
  await errorCode(client.exchange(input('https://elsewhere.invalid/'), destination()), 'request_operation_invalid');
  await errorCode(client.exchange(input('/not-configured'), destination()), 'request_not_allowed');
  await assert.rejects(client.exchange({ ...input(), routeId: 'https://secret.invalid' }, destination()), error =>
    error instanceof EgressTransportError && error.code === 'route_unknown' && error.receipt.routeId === '');
  assert.equal(calls, 0);
});

test('mixed DNS answers, mapped IPv6 and deployment denials cannot be bypassed', async () => {
  const policy = new AddressPolicy([]);
  for (const address of ['127.0.0.1', '169.254.169.254', '10.0.0.1', '100.64.0.1', '192.0.2.1', '198.18.0.1', '::1', 'fe80::1', 'fc00::1', '64:ff9b::a00:1', '2001:db8::1', '2002::1', '3fff::1']) {
    const value = canonicalAddress(address);
    assert.throws(() => policy.checkAll([value], new Set()), PolicyError);
  }
  assert.throws(() => canonicalAddress('::ffff:1.1.1.1'), PolicyError);
  assert.throws(() => canonicalAddress('fe80::1%lo0'), PolicyError);
  assert.throws(() => policy.exceptions(['169.254.169.254']), PolicyError);
  let secretCalls = 0;
  const client = transport({}, { routes: [route({ privateAddressExceptions: [] })],
    resolve: async () => [{ address: '1.1.1.1', family: 4 }, { address: '127.0.0.1', family: 4 }],
    getSecret: async () => { secretCalls++; return {}; } });
  await errorCode(client.exchange(input(), destination()), 'address_denied'); assert.equal(secretCalls, 0);
  assert.throws(() => transport({}, { deniedCidrs: ['127.0.0.0/8'] }), PolicyError);
  const blocked = new AddressPolicy(['2600:1234::/32']);
  assert.throws(() => blocked.checkAll([canonicalAddress('2600:1234::1')], new Set()), PolicyError);
});

test('TLS name and CA errors fail with no raw exception, URL or secret in receipts', async () => {
  const previous = seen.length;
  const wrongName = transport({}, { routes: [route({ origin: 'https://wrong.invalid:' + port })], resolve: async () => [{ address: '127.0.0.1', family: 4 }] });
  await errorCode(wrongName.exchange(input(), destination()), 'upstream_failed');
  const { ca: _omitted, ...withoutCA } = route();
  const wrongCA = transport({}, { routes: [withoutCA] });
  await assert.rejects(wrongCA.exchange(input(), destination()), error => {
    assert.ok(error instanceof EgressTransportError);
    assert.equal(error.code, 'upstream_failed');
    assert.ok(!JSON.stringify(error).includes('fixture-only-value')); assert.ok(!JSON.stringify(error).includes('localhost'));
    return true;
  });
  assert.equal(seen.length, previous);
});

test('redirects and upgrades are refused; native provider errors remain transparent', async () => {
  const previous = seen.length; const client = transport();
  await errorCode(client.exchange(input('/redirect'), destination()), 'upstream_redirect_rejected');
  assert.equal(seen.length, previous + 1);
  await errorCode(client.exchange(input('/upgrade'), destination()), 'upstream_upgrade_rejected');
  const sink = destination(); const receipt = await client.exchange(input('/error'), sink);
  assert.equal(receipt.upstreamStatus, 429); assert.equal(receipt.outcome, 'completed');
  assert.equal(Buffer.concat(sink.chunks).toString(), '{"error":"rate_limit"}');
  assert.equal(sink.headers['retry-after'], '3');
  for (const field of ['connection', 'x-hop', 'set-cookie', 'x-api-key']) assert.equal(sink.headers[field], undefined);
});

test('response byte ceilings abort before declared oversize or during chunked transfer', async () => {
  const client = transport({ maxResponseBytes: 100 }); const large = destination();
  await errorCode(client.exchange(input('/large'), large), 'response_too_large'); assert.equal(large.status, null);
  const chunked = destination();
  await errorCode(client.exchange(input('/chunked-large'), chunked), 'response_too_large'); assert.equal(chunked.body.destroyed, true);
});

test('idle/deadline and active cancellation close work without claiming no upstream spend', async () => {
  await errorCode(transport({ idleTimeoutMs: 30 }).exchange(input('/idle'), destination()), 'idle_timeout');
  const sink = destination(); await errorCode(transport({ idleTimeoutMs: 30 }).exchange(input('/stream-idle'), sink), 'idle_timeout');
  assert.equal(sink.body.destroyed, true);
  const controller = new AbortController(); const task = transport().exchange(input('/stream-idle'), destination(), controller.signal);
  setTimeout(() => controller.abort(), 30);
  await assert.rejects(task, error => error instanceof EgressTransportError && error.code === 'aborted' && error.receipt.requestExposure === 'may_have_been_sent');
});

test('timed-out uncancellable dependency retains its permit until settlement, then cannot dial late', async () => {
  let release!: (value: { address: string; family: 4 }[]) => void;
  let calls = 0; const previous = seen.length;
  const client = transport({ maxConcurrent: 1, totalTimeoutMs: 100 }, { resolve: async () => {
    calls++; if (calls === 1) return await new Promise(resolve => { release = resolve; });
    return [{ address: '127.0.0.1', family: 4 }];
  } });
  await errorCode(client.exchange(input(), destination()), 'total_timeout');
  await errorCode(client.exchange(input(), destination()), 'concurrency_exceeded');
  assert.equal(calls, 1); release([{ address: '127.0.0.1', family: 4 }]); await tick();
  assert.equal(seen.length, previous);
  await client.exchange(input(), destination()); assert.equal(seen.length, previous + 1);
});

test('body/method/path and destination are snapshotted before asynchronous resolution', async () => {
  let release!: (value: { address: string; family: 4 }[]) => void;
  const client = transport({}, { resolve: async () => await new Promise(resolve => { release = resolve; }) });
  const body = Buffer.from('before');
  const request = { ...input('/ok', body) }; const sink = destination();
  const target = { onHeaders: sink.onHeaders, body: sink.body };
  const task = client.exchange(request, target);
  request.path = 'https://elsewhere.invalid/'; body.fill(120); target.body = destination().body;
  release([{ address: '127.0.0.1', family: 4 }]); await task;
  assert.equal(seen.at(-1)?.path, '/ok'); assert.equal(seen.at(-1)?.body.toString(), 'before');
  assert.equal(Buffer.concat(sink.chunks).toString(), 'before');
});

test('route configuration is frozen and origins/exceptions cannot express a general proxy', () => {
  for (const origin of ['http://localhost:' + port, 'https://provider.example:0', 'https://127.0.0.1:' + port, 'https://user:pass@localhost:' + port, 'https://localhost:' + port + '/path', 'https://localhost.:' + port]) {
    assert.throws(() => transport({}, { routes: [route({ origin })] }), PolicyError);
  }
  const first = transport(); const second = transport();
  assert.equal(first.configurationSha256, second.configurationSha256);
  assert.notEqual(first.configurationSha256, transport({ maxConcurrent: 1 }).configurationSha256);
});

test('uncancellable Secret lookup retains concurrency after outward cancellation and cannot dial late', async () => {
  let release!: (value: Record<string, string>) => void;
  let calls = 0; const previous = seen.length;
  const client = transport({ maxConcurrent: 1, totalTimeoutMs: 100 }, { getSecret: async () => {
    calls++; if (calls === 1) return await new Promise(resolve => { release = resolve; });
    return {};
  } });
  await errorCode(client.exchange(input(), destination()), 'total_timeout');
  await errorCode(client.exchange(input(), destination()), 'concurrency_exceeded');
  release({ authorization: 'Bearer settled-too-late' }); await tick();
  assert.equal(seen.length, previous);
  await client.exchange(input(), destination()); assert.equal(calls, 2);
});

test('already-aborted admission and invalid provider/header budgets never contact upstream', async () => {
  const previous = seen.length; let calls = 0;
  const controller = new AbortController(); controller.abort();
  const preAborted = transport({}, { resolve: async () => { calls++; return []; } });
  await errorCode(preAborted.exchange(input(), destination(), controller.signal), 'aborted'); assert.equal(calls, 0);
  await errorCode(transport({}, { getSecret: async () => ({ host: 'not-permitted' }) }).exchange(input(), destination()), 'secret_invalid');
  await errorCode(transport({}, { getSecret: async () => ({ authorization: 'Bearer bad\r\nheader' }) }).exchange(input(), destination()), 'secret_invalid');
  await errorCode(transport({ maxHeaderBytes: 128 }, { getSecret: async () => ({ authorization: 'x'.repeat(150) }) }).exchange(input(), destination()), 'request_headers_too_large');
  assert.equal(seen.length, previous);
});

test('async header callbacks cannot release a slot while their work is still unsettled', async () => {
  let finish!: () => void;
  const client = transport({ maxConcurrent: 1, totalTimeoutMs: 100 });
  const sink = destination();
  const target = { body: sink.body, onHeaders: async () => await new Promise<void>(resolve => { finish = resolve; }) };
  await errorCode(client.exchange(input(), target), 'total_timeout');
  await errorCode(client.exchange(input(), destination()), 'concurrency_exceeded');
  finish(); await tick();
  await client.exchange(input(), destination());
});

test('downstream failure and early close abort the provider stream with stable codes', async () => {
  const broken = { onHeaders: () => undefined, body: new Writable({ write(_chunk, _encoding, done) { done(new Error('private downstream detail')); } }) };
  await errorCode(transport().exchange(input('/sse'), broken), 'destination_failed');
  const early = destination(); const controller = new AbortController();
  const task = transport().exchange(input('/stream-idle'), early, controller.signal);
  setTimeout(() => early.body.destroy(), 20);
  await errorCode(task, 'destination_closed');
});
