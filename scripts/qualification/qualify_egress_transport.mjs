#!/usr/bin/env node
/** Real bounded TLS/HTTP qualification of the production upstream transport.
 * No model requests, NIC/VM/firewall changes or production credentials.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { createServer as httpsServer, request as httpsRequest, Agent } from 'node:https';
import { createServer as netServer } from 'node:net';
import { Writable } from 'node:stream';
import { spawnSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { UpstreamTransport, EgressTransportError } from '../../apps/model-egress/transport.ts';

if (process.platform !== 'linux' || process.getuid?.() === 0 || process.version !== 'v24.22.1') {
  throw new Error('Require ordinary Linux operator and pinned Node v24.22.1');
}
process.umask(0o077);
const root = path.resolve(process.argv[2] ?? process.cwd());
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
assert.equal(root, repo);
const ports = [10020, 10021];
const rid = randomBytes(6).toString('hex');
const folder = path.join(root, '.private/qualification', 'egress-transport-' + rid);
await fs.mkdir(folder, { recursive: true, mode: 0o700 });
for (const parent of [path.join(root, '.private'), path.dirname(folder), folder]) {
  const stat = await fs.lstat(parent);
  assert.ok(stat.isDirectory() && !stat.isSymbolicLink() && stat.uid === process.getuid() && (stat.mode & 0o777) === 0o700);
}
assert.equal(process.env.NODE_USE_ENV_PROXY, '1');
assert.equal(process.env.HTTPS_PROXY, 'http://127.0.0.1:10021');
assert.equal(process.env.NO_PROXY, '');
const report = { environment_proxy_enabled_fixture: true, schema_version: 1, qualification: 'M0-05-https-upstream-transport', run_id: rid,
  started_at: new Date().toISOString(), versions: { node: process.version, openssl: process.versions.openssl },
  checks: {}, receipts: [], errors: [], cleanup_errors: [], real_model_calls: 0,
  production_egress_verified: false, execution_authorization_verified: false,
  budget_accounting_verified: false, kernel_egress_verified: false, vm_network_verified: false,
  dedicated_service_identity_verified: false, milestone_complete: false };
const save = (name, data) => fs.writeFile(path.join(folder, name), JSON.stringify(data, null, 2) + '\n', { mode: 0o600 });
const digest = data => createHash('sha256').update(data).digest('hex');
const sources = [fileURLToPath(import.meta.url), ...['types.ts', 'policy.ts', 'transport.ts', 'package.json', 'package-lock.json', 'tsconfig.json'].map(name => path.join(root, 'apps/model-egress', name))];
report.source_sha256 = {};
for (const file of sources) {
  const data = await fs.readFile(file);
  const name = path.relative(root, file);
  report.source_sha256[name] = digest(data);
  const target = path.join(folder, 'source', name);
  await fs.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
  await fs.writeFile(target, data, { mode: 0o600 });
}
const servers = new Set();
const sockets = new Set();
const timers = new Set();
const meters = { main_http: 0, peer_http: 0, forbidden_http: 0, authorized_http: 0, tls_client_errors: 0 };
const token = randomBytes(24).toString('hex');
const payload = Buffer.from(JSON.stringify({ nonce: randomBytes(16).toString('hex'), fixture: 'bounded-native-only' }));
const streamPayload = Buffer.alloc(256 * 1024, 0x61);
let stage = 'prepare_tls';
function openssl(args) {
  const result = spawnSync('openssl', args, { cwd: folder, encoding: 'utf8', timeout: 20000, maxBuffer: 1024 * 1024 });
  if (result.status !== 0) throw new Error('fixture_certificate_generation_failed');
}
async function certificates() {
  const filename = name => path.join(folder, name);
  openssl(['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.pem', '-subj', '/CN=Harniqo qualification CA', '-days', '1']);
  openssl(['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'other-ca.key', '-out', 'other-ca.pem', '-subj', '/CN=Harniqo other qualification CA', '-days', '1']);
  for (const [label, hostname] of [['server', 'localhost'], ['wrong-san', 'wrong.invalid']]) {
    openssl(['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', label + '.key', '-out', label + '.csr', '-subj', '/CN=' + hostname, '-addext', 'subjectAltName=DNS:' + hostname + (label === 'server' ? ',DNS:fixture.invalid' : '')]);
    await fs.writeFile(filename(label + '.ext'), 'subjectAltName=DNS:' + hostname + (label === 'server' ? ',DNS:fixture.invalid' : '') + '\n', { mode: 0o600 });
    openssl(['x509', '-req', '-in', label + '.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-out', label + '.pem', '-days', '1', '-extfile', label + '.ext']);
  }
  await fs.mkdir(filename('certificates'), { mode: 0o700 });
  await fs.writeFile(filename('index'), '', { mode: 0o600 });
  await fs.writeFile(filename('serial'), '1000\n', { mode: 0o600 });
  const config = `[ca]\ndefault_ca=native\n[native]\ndatabase=${filename('index')}\nserial=${filename('serial')}\nnew_certs_dir=${filename('certificates')}\ncertificate=${filename('ca.pem')}\nprivate_key=${filename('ca.key')}\ndefault_md=sha256\ndefault_days=1\npolicy=names\ncopy_extensions=copy\nunique_subject=no\n[names]\ncommonName=supplied\n`;
  await fs.writeFile(filename('ca.cnf'), config, { mode: 0o600 });
  openssl(['ca', '-batch', '-config', 'ca.cnf', '-in', 'server.csr', '-out', 'expired.pem', '-startdate', '20200101000000Z', '-enddate', '20200102000000Z']);
  return { ca: await fs.readFile(filename('ca.pem')), other: await fs.readFile(filename('other-ca.pem')),
    key: await fs.readFile(filename('server.key')), cert: await fs.readFile(filename('server.pem')),
    wrongKey: await fs.readFile(filename('wrong-san.key')), wrongCert: await fs.readFile(filename('wrong-san.pem')),
    expired: await fs.readFile(filename('expired.pem')) };
}
function track(server) {
  servers.add(server);
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  server.on('tlsClientError', () => meters.tls_client_errors++);
  return server;
}
async function listen(server, port) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => { server.removeListener('error', reject); resolve(); });
  });
}
async function close(server) {
  if (!server?.listening) return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture_close_deadline')), 3000);
    server.close(error => { clearTimeout(timer); error ? reject(error) : resolve(); });
    if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
  });
}
function mainHandler(request, response) {
  meters.main_http++;
  if (request.headers.authorization !== 'Bearer ' + token) { response.writeHead(401).end(); return; }
  meters.authorized_http++;
  const chunks = []; let incoming = 0;
  request.on('data', chunk => { incoming += chunk.length; if (incoming > 128 * 1024) request.destroy(); else chunks.push(chunk); });
  request.on('end', () => dispatchResponse(request, response, Buffer.concat(chunks)));
}
function dispatchResponse(request, response, body) {
  if (request.method === 'POST') meters.post_body_valid = digest(body) === digest(payload);
  if (request.headers.host === 'fixture.invalid:' + ports[0]) meters.pinned_hostname_preserved = true;
  if (request.url === '/ok') response.writeHead(200, { 'content-type': 'application/json', 'x-native-fixture': 'yes', 'connection': 'close' }).end(payload);
  else if (request.url === '/status') response.writeHead(429, { 'content-type': 'application/json' }).end(payload);
  else if (request.url === '/redirect') response.writeHead(302, { location: 'https://localhost:' + ports[1] + '/forbidden' }).end();
  else if (request.url === '/headers') response.writeHead(200, { 'x-large-fixture': 'x'.repeat(32 * 1024) }).end(payload);
  else if (request.url === '/oversized') response.writeHead(200).end(Buffer.alloc(768 * 1024, 0x62));
  else if (request.url === '/truncated') { response.writeHead(200, { 'content-length': '1024' }); response.flushHeaders(); response.write('partial'); const timer = setTimeout(() => { timers.delete(timer); response.socket?.destroy(); }, 30); timers.add(timer); }
  else if (request.url === '/stream') {
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    let offset = 0;
    const next = () => {
      if (response.destroyed) return;
      if (offset === streamPayload.length) { response.end(); return; }
      const chunk = streamPayload.subarray(offset, offset + 16384); offset += chunk.length;
      if (!response.write(chunk)) response.once('drain', next); else setImmediate(next);
    };
    next();
  } else if (['/stall', '/cancel', '/hold'].includes(request.url)) {
    response.writeHead(200, { 'content-type': 'text/event-stream' }); response.flushHeaders(); response.write('first-native-chunk\n');
    if (request.url !== '/stall') {
      const timer = setInterval(() => { if (!response.destroyed) response.write('native-chunk\n'); }, 20);
      timers.add(timer); response.on('close', () => { clearInterval(timer); timers.delete(timer); });
    }
  } else if (request.url === '/upgrade') response.writeHead(101, { connection: 'Upgrade', upgrade: 'websocket' }).flushHeaders();
  else response.writeHead(404).end();
}
function destination({ slow = false, onFirst, onHeaders } = {}) {
  const chunks = []; let size = 0; let maximum = 0; let calls = 0; let status; let headers;
  const body = new Writable({ highWaterMark: 1024, write(chunk, _encoding, callback) {
    calls++; maximum = Math.max(maximum, this.writableLength); chunks.push(Buffer.from(chunk)); size += chunk.length;
    if (calls === 1) onFirst?.();
    slow ? setTimeout(callback, 20) : callback();
  } });
  body.on('error', () => {});
  return { target: { onHeaders(s, h) { status = s; headers = h; onHeaders?.(); }, body },
    data: () => Buffer.concat(chunks), size: () => size, maximum: () => maximum, calls: () => calls,
    status: () => status, headers: () => headers };
}
let ca;
const paths = ['/ok', '/stream', '/status', '/redirect', '/headers', '/oversized', '/truncated', '/stall', '/cancel', '/hold', '/upgrade'];
const route = (id, port = ports[0], trust = ca, exceptions = ['127.0.0.1', '::1']) => ({ id, origin: 'https://localhost:' + port,
  operations: [...paths.map(path => ({ method: 'GET', path })), { method: 'POST', path: '/ok' }], secretRef: 'qualification-only', privateAddressExceptions: exceptions, ca: trust });
const transport = (routes, overrides = {}) => new UpstreamTransport({ routes,
  getSecret: async () => ({ authorization: 'Bearer ' + token }), limits: { maxConcurrent: 2, maxConcurrentPerRoute: 2,
    maxRequestBytes: 64 * 1024, maxResponseBytes: 512 * 1024, maxHeaderBytes: 16384, idleTimeoutMs: 250, totalTimeoutMs: 4000, ...overrides } });
const exchange = (client, id, name, dest, signal, body) => client.exchange({ routeId: id, method: body === undefined ? 'GET' : 'POST', path: name, body }, dest.target, signal);
const expectedCodes = { default_private_denied: ['address_denied'], mixed_dns_denied: ['address_denied'], wrong_ca: ['upstream_failed'], wrong_san: ['upstream_failed'], expired_certificate: ['upstream_failed'], redirect_rejected: ['upstream_redirect_rejected'], request_bound: ['request_body_too_large'], response_bound: ['response_too_large'], header_bound: ['upstream_failed', 'response_headers_too_large'], truncated_response: ['upstream_failed', 'response_incomplete', 'destination_failed', 'destination_closed'], idle_timeout: ['idle_timeout'], caller_cancel: ['aborted'], concurrency_guard: ['concurrency_exceeded'], upgrade_rejected: ['upstream_upgrade_rejected', 'upstream_status_invalid'], request_header_rejected: ['request_header_rejected'], absolute_path_rejected: ['request_operation_invalid'], total_timeout: ['total_timeout'], dns_total_deadline: ['total_timeout'], dns_permit_retained: ['concurrency_exceeded'] };
async function rejectCase(name, operation, { before, zeroHttp = false } = {}) {
  let error;
  try { await operation(); } catch (value) { error = value; }
  assert.ok(error instanceof EgressTransportError);
  assert.ok(expectedCodes[name]?.includes(error.code));
  assert.equal(error.receipt.outcome, 'failed');
  assert.ok(['not_sent', 'may_have_been_sent'].includes(error.receipt.requestExposure));
  if (zeroHttp) assert.equal(meters.main_http, before);
  report.receipts.push({ case: name, code: error.code, ...error.receipt });
  report.checks[name] = true;
}
async function peerControl(port, trust) {
  const agent = new Agent({ keepAlive: false, maxSockets: 1, maxCachedSessions: 0, proxyEnv: {} });
  try {
    const data = await new Promise((resolve, reject) => {
      const request = httpsRequest({ hostname: 'localhost', port, path: '/control', ca: trust, servername: 'localhost', family: 4, autoSelectFamily: false, agent }, response => {
        const chunks = []; response.on('data', chunk => chunks.push(chunk)); response.on('end', () => resolve(Buffer.concat(chunks)));
      });
      request.on('error', reject); request.setTimeout(2000, () => request.destroy()); request.end();
    });
    assert.equal(data.toString(), 'peer-control');
  } finally { agent.destroy(); }
}
let main; let peer;
try {
  const tls = await certificates(); ca = tls.ca;
  main = track(httpsServer({ key: tls.key, cert: tls.cert }, mainHandler));
  await listen(main, ports[0]);
  peer = track(httpsServer({ key: tls.wrongKey, cert: tls.wrongCert }, (_req, res) => { meters.peer_http++; res.end('unexpected'); }));
  await listen(peer, ports[1]);
  stage = 'zero_port_rejected';
  assert.throws(() => transport([{ ...route('zero'), origin: 'https://localhost:0' }]), error => error?.code === 'route_origin_invalid');
  report.checks.zero_port_rejected = true;
  stage = 'tls_success';
  const client = transport([route('native')]);
  report.configuration_sha256 = client.configurationSha256;
  const good = destination(); const completed = await exchange(client, 'native', '/ok', good);
  assert.deepEqual(good.data(), payload); assert.equal(good.status(), 200); assert.equal(good.headers().connection, undefined);
  assert.equal(completed.outcome, 'completed'); assert.equal(completed.requestExposure, 'may_have_been_sent');
  report.receipts.push({ case: 'tls_success', ...completed }); report.checks.tls_success = true;
  stage = 'request_body_preserved'; const posted = destination();
  const postReceipt = await exchange(client, 'native', '/ok', posted, undefined, payload);
  assert.equal(meters.post_body_valid, true); assert.deepEqual(posted.data(), payload);
  report.receipts.push({ case: stage, ...postReceipt }); report.checks[stage] = true;
  stage = 'pinned_dns_dial'; let resolutionCalls = 0;
  const rebindRoute = { ...route('pin'), origin: 'https://fixture.invalid:' + ports[0], privateAddressExceptions: ['127.0.0.1'] };
  const rebinding = new UpstreamTransport({ routes: [rebindRoute], getSecret: async () => ({ authorization: 'Bearer ' + token }),
    resolve: async () => { resolutionCalls++; return [{ address: resolutionCalls === 1 ? '127.0.0.1' : '169.254.169.254', family: 4 }]; } });
  const pinned = destination(); const pinReceipt = await exchange(rebinding, 'pin', '/ok', pinned);
  assert.equal(resolutionCalls, 1); assert.equal(meters.pinned_hostname_preserved, true); assert.deepEqual(pinned.data(), payload);
  report.receipts.push({ case: stage, ...pinReceipt }); report.checks[stage] = true;
  stage = 'mixed_dns_denied';
  const mixed = new UpstreamTransport({ routes: [rebindRoute], getSecret: async () => ({ authorization: 'Bearer ' + token }),
    resolve: async () => [{ address: '127.0.0.1', family: 4 }, { address: '169.254.169.254', family: 4 }] });
  await rejectCase(stage, () => exchange(mixed, 'pin', '/ok', destination()), { before: meters.main_http, zeroHttp: true });
  stage = 'default_private_denied';
  await rejectCase(stage, () => exchange(transport([route('deny', ports[0], ca, [])]), 'deny', '/ok', destination()), { before: meters.main_http, zeroHttp: true });
  stage = 'wrong_ca';
  await rejectCase(stage, () => exchange(transport([route('wrong-ca', ports[0], tls.other)]), 'wrong-ca', '/ok', destination()), { before: meters.main_http, zeroHttp: true });
  stage = 'wrong_san';
  const peerBefore = meters.peer_http;
  await rejectCase(stage, () => exchange(transport([route('wrong-san', ports[1])]), 'wrong-san', '/ok', destination()));
  assert.equal(meters.peer_http, peerBefore);
  await close(peer);
  peer = track(httpsServer({ key: tls.key, cert: tls.expired }, (_req, res) => { meters.peer_http++; res.end('unexpected'); })); await listen(peer, ports[1]);
  stage = 'expired_certificate';
  await rejectCase(stage, () => exchange(transport([route('expired', ports[1])]), 'expired', '/ok', destination()));
  assert.equal(meters.peer_http, peerBefore);
  await close(peer);
  peer = track(httpsServer({ key: tls.key, cert: tls.cert }, (req, res) => { meters.peer_http++; if (req.url === '/forbidden') meters.forbidden_http++; res.end('peer-control'); })); await listen(peer, ports[1]);
  await peerControl(ports[1], ca); report.checks.redirect_target_positive_control = true;
  stage = 'redirect_rejected';
  await rejectCase(stage, () => exchange(client, 'native', '/redirect', destination())); assert.equal(meters.forbidden_http, 0);
  stage = 'stream_backpressure';
  const slow = destination({ slow: true }); const began = performance.now();
  const streamed = await exchange(client, 'native', '/stream', slow);
  assert.deepEqual(slow.data(), streamPayload); assert.ok(slow.calls() > 2 && performance.now() - began >= 40); assert.ok(slow.maximum() <= 128 * 1024);
  report.stream = { bytes: slow.size(), writable_peak_bytes: slow.maximum(), write_calls: slow.calls(), sha256: digest(slow.data()) };
  report.receipts.push({ case: stage, ...streamed }); report.checks[stage] = true;
  stage = 'upstream_status_preserved';
  const status = destination(); const receipt = await exchange(client, 'native', '/status', status);
  assert.equal(status.status(), 429); assert.deepEqual(status.data(), payload); report.receipts.push({ case: stage, ...receipt }); report.checks[stage] = true;
  stage = 'request_header_rejected';
  await rejectCase(stage, () => client.exchange({ routeId: 'native', method: 'GET', path: '/ok', headers: { authorization: 'qualification-spoof-only' } }, destination().target), { before: meters.main_http, zeroHttp: true });
  stage = 'absolute_path_rejected';
  await rejectCase(stage, () => client.exchange({ routeId: 'native', method: 'GET', path: 'https://localhost:' + ports[1] + '/forbidden' }, destination().target), { before: meters.main_http, zeroHttp: true });
  stage = 'request_bound';
  await rejectCase(stage, () => exchange(client, 'native', '/ok', destination(), undefined, Buffer.alloc(65 * 1024)), { before: meters.main_http, zeroHttp: true });
  stage = 'response_bound';
  const large = destination(); await rejectCase(stage, () => exchange(client, 'native', '/oversized', large)); assert.ok(large.size() <= 512 * 1024);
  stage = 'header_bound'; await rejectCase(stage, () => exchange(client, 'native', '/headers', destination()));
  stage = 'truncated_response'; await rejectCase(stage, () => exchange(client, 'native', '/truncated', destination()));
  stage = 'idle_timeout'; const idleStart = performance.now();
  await rejectCase(stage, () => exchange(client, 'native', '/stall', destination())); assert.ok(performance.now() - idleStart < 3000);
  stage = 'total_timeout'; const totalStart = performance.now();
  await rejectCase(stage, () => exchange(transport([route('total')], { totalTimeoutMs: 600 }), 'total', '/hold', destination()));
  assert.ok(performance.now() - totalStart < 2500);
  stage = 'dns_total_deadline'; let releaseDns;
  const dependency = new Promise(resolve => { releaseDns = resolve; });
  const delayed = new UpstreamTransport({ routes: [rebindRoute], getSecret: async () => ({ authorization: 'Bearer ' + token }), resolve: async () => dependency,
    limits: { maxConcurrent: 1, maxConcurrentPerRoute: 1, totalTimeoutMs: 100, idleTimeoutMs: 100 } });
  const beforeDependency = meters.main_http;
  await rejectCase(stage, () => exchange(delayed, 'pin', '/ok', destination()), { before: beforeDependency, zeroHttp: true });
  stage = 'dns_permit_retained';
  await rejectCase(stage, () => exchange(delayed, 'pin', '/ok', destination()), { before: beforeDependency, zeroHttp: true });
  releaseDns([{ address: '127.0.0.1', family: 4 }]); await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(meters.main_http, beforeDependency); report.checks.late_dns_cannot_dial = true;
  stage = 'caller_cancel'; const controller = new AbortController(); const cancel = destination({ onFirst: () => setImmediate(() => controller.abort()) });
  await rejectCase(stage, () => exchange(client, 'native', '/cancel', cancel, controller.signal)); assert.ok(cancel.size() > 0);
  stage = 'concurrency_guard'; const single = transport([route('single')], { maxConcurrent: 1, maxConcurrentPerRoute: 1 });
  const abort = new AbortController(); let ready; const headers = new Promise(resolve => { ready = resolve; });
  const holding = exchange(single, 'single', '/hold', destination({ onHeaders: () => ready() }), abort.signal).catch(error => error);
  await headers;
  await rejectCase(stage, () => exchange(single, 'single', '/ok', destination()), { before: meters.main_http, zeroHttp: true });
  abort.abort(); assert.ok(await holding instanceof EgressTransportError);
  stage = 'upgrade_rejected'; await rejectCase(stage, () => exchange(client, 'native', '/upgrade', destination()));
  report.checks.fixture_authentication = meters.authorized_http === meters.main_http;
} catch (error) {
  report.errors.push({ stage, code: error instanceof EgressTransportError ? error.code : 'qualification_predicate_failed' });
} finally {
  for (const timer of timers) { clearInterval(timer); clearTimeout(timer); } timers.clear();
  for (const socket of sockets) socket.destroy();
  for (const server of servers) {
    try { await close(server); } catch { report.cleanup_errors.push('listener_close_unconfirmed'); }
  }
  for (const port of ports) {
    const probe = netServer();
    try { await listen(probe, port); await close(probe); } catch { report.cleanup_errors.push('owned_port_not_released'); }
  }
  report.fixture_counters = { ...meters };
  report.evidence_scopes = { real_tls_http: true, configuration_rejection_cases: ['zero_port_rejected'], controlled_resolver_policy_and_dependency_cases: ['pinned_dns_dial', 'mixed_dns_denied', 'dns_total_deadline', 'dns_permit_retained', 'late_dns_cannot_dial'], real_provider_model_requests: false };
  report.sources_unchanged = true;
  for (const [name, hash] of Object.entries(report.source_sha256)) {
    if (digest(await fs.readFile(path.join(root, name))) !== hash) report.sources_unchanged = false;
  }
  report.checks.sources_unchanged = report.sources_unchanged;
  report.checks.owned_ports_released = report.cleanup_errors.length === 0;
  report.finished_at = new Date().toISOString();
  const required = ['zero_port_rejected', 'tls_success', 'request_body_preserved', 'pinned_dns_dial', 'mixed_dns_denied', 'default_private_denied', 'wrong_ca', 'wrong_san', 'expired_certificate', 'redirect_target_positive_control',
    'redirect_rejected', 'stream_backpressure', 'upstream_status_preserved', 'request_bound', 'response_bound', 'header_bound', 'truncated_response',
    'idle_timeout', 'total_timeout', 'dns_total_deadline', 'dns_permit_retained', 'late_dns_cannot_dial', 'request_header_rejected', 'absolute_path_rejected', 'caller_cancel', 'concurrency_guard', 'upgrade_rejected', 'fixture_authentication', 'sources_unchanged', 'owned_ports_released'];
  report.passed = report.errors.length === 0 && report.cleanup_errors.length === 0 && required.every(name => report.checks[name] === true);
  await save('report.json', report);
  console.log(JSON.stringify({ qualification: report.qualification, passed: report.passed, checks: Object.keys(report.checks).length,
    errors: report.errors.length, cleanup_errors: report.cleanup_errors.length, report: path.relative(root, path.join(folder, 'report.json')) }));
}
process.exitCode = report.passed ? 0 : 1;
