import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomUUID, X509Certificate } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer, request } from 'node:https';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { TLSSocket } from 'node:tls';
import type { AssignmentIdentity } from '../workbench-execution/index.ts';
import { createHermesConnector, loadHermesConnector } from './index.ts';
import { HERMES_CONNECTOR_MAX_BYTES, HERMES_CONNECTOR_PATH, parseConnectorRequest } from './protocol.ts';

let directory: string;
let ca: Buffer;
let serverCert: Buffer;
let serverKey: Buffer;
let clientCert: Buffer;
let clientKey: Buffer;
const origin = 'https://127.0.0.1:10084';
const controller = new AbortController();
const identity: AssignmentIdentity = {
  organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID(), runId: randomUUID(),
  commandId: randomUUID(), attemptId: randomUUID(), workspaceId: randomUUID(), executionTargetId: randomUUID(),
  deploymentRevisionId: randomUUID(), configurationRevisionId: randomUUID(), inputSnapshotId: randomUUID(),
  budgetPolicyId: randomUUID(), runtimeInstanceId: randomUUID(), harnessReleaseId: randomUUID(), platformEpoch: randomUUID(),
  instanceGeneration: 1, leaseGeneration: 2, nativeSessionId: 'harniqo_' + randomUUID(),
};
const nativeRunId = 'run_' + 'a'.repeat(32);
const answer = 'persisted native reply';

before(async () => {
  directory = await mkdtemp(join(tmpdir(), 'harniqo-connector-client-'));
  const openssl = (...args: string[]) => execFileSync('openssl', args, { stdio: 'ignore', timeout: 15_000 });
  openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(directory, 'ca.key'),
    '-out', join(directory, 'ca.pem'), '-days', '1', '-subj', '/CN=Connector test CA',
    '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign');
  for (const [name, eku] of [['server', 'serverAuth'], ['client', 'clientAuth']]) {
    openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(directory, name + '.key'),
      '-out', join(directory, name + '.csr'), '-subj', '/CN=' + name);
    const extensions = join(directory, name + '.ext');
    await writeFile(extensions, 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage='
      + eku + '\nsubjectAltName=IP:127.0.0.1\n');
    openssl('x509', '-req', '-in', join(directory, name + '.csr'), '-CA', join(directory, 'ca.pem'),
      '-CAkey', join(directory, 'ca.key'), '-CAcreateserial', '-out', join(directory, name + '.pem'),
      '-days', '1', '-extfile', extensions);
  }
  const files = await Promise.all([
    'ca.pem', 'server.pem', 'server.key', 'client.pem', 'client.key',
  ].map(file => readFile(join(directory, file))));
  ca = files[0]!;
  serverCert = files[1]!;
  serverKey = files[2]!;
  clientCert = files[3]!;
  clientKey = files[4]!;
});

after(async () => { if (directory) await rm(directory, { recursive: true, force: true }); });

type ResponseMode = 'normal' | 'mismatched_identity' | 'redirect' | 'oversize' | 'stall' | 'denied';

test('the mTLS connector drives exact assignment operations and fails closed on changed identity, redirects and size', async t => {
  let mode: ResponseMode = 'normal';
  const calls: string[] = [];
  const clientFingerprint = new X509Certificate(clientCert).fingerprint256;
  const server = createServer({ ca, cert: serverCert, key: serverKey, requestCert: true, rejectUnauthorized: true }, async (req, res) => {
    const socket = req.socket;
    assert.ok(socket instanceof TLSSocket);
    assert.equal(socket.authorized, true);
    assert.equal(socket.getPeerCertificate().fingerprint256, clientFingerprint);
    assert.equal(req.url, HERMES_CONNECTOR_PATH);
    assert.equal(req.method, 'POST');
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    const message = parseConnectorRequest(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    calls.push(message.operation);
    if (mode === 'redirect') { res.writeHead(302, { location: origin + '/unexpected' }).end(); return; }
    if (mode === 'denied') { res.writeHead(403).end(); return; }
    res.writeHead(200, { 'content-type': 'application/json' });
    if (mode === 'oversize') { res.end(' '.repeat(HERMES_CONNECTOR_MAX_BYTES + 1)); return; }
    if (mode === 'stall') { res.write('{'); return; }
    const result = {
      open: { allowed: true }, assert_live: { live: true }, has_native_session: { exists: true },
      attach: { nativeSessionId: identity.nativeSessionId }, start: { nativeRunId, replayed: false },
      inspect: { nativeRunId, nativeSessionId: identity.nativeSessionId, status: 'completed', output: answer },
      read_reply: { reply: answer }, confirm_terminal: { proof: { nativeRunId, writerExited: true, servedModel: 'fixture-model' } },
    }[message.operation];
    res.end(JSON.stringify({ version: 1, assignment: { ...message.assignment,
      ...(mode === 'mismatched_identity' ? { leaseGeneration: 999 } : {}) }, result }));
  });
  t.after(async () => { await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }); });
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(10084, '127.0.0.1', resolve); });
  const connector = createHermesConnector({ origin, ca, cert: clientCert, key: clientKey });
  const execution = await connector(identity, controller.signal);
  assert.ok(execution);
  assert.deepEqual(execution.identity, identity);
  assert.equal(Object.isFrozen(execution.identity), true);
  assert.equal(await execution.assertLive(controller.signal), true);
  assert.equal(await execution.hasNativeSession(controller.signal), true);
  assert.deepEqual(await execution.hermes.attach(identity.nativeSessionId), { nativeSessionId: identity.nativeSessionId });
  assert.deepEqual(await execution.hermes.start({ nativeSessionId: identity.nativeSessionId, text: 'instruction', idempotencyKey: identity.attemptId }),
    { nativeRunId, replayed: false });
  const run = await execution.hermes.inspect(nativeRunId, identity.nativeSessionId);
  assert.equal(run.status, 'completed');
  if (run.status !== 'completed') assert.fail('completion expected');
  assert.equal(await execution.hermes.readReply(run), answer);
  assert.deepEqual(await execution.confirmTerminal(nativeRunId, controller.signal), { nativeRunId, writerExited: true, servedModel: 'fixture-model' });
  assert.deepEqual(calls, ['open', 'assert_live', 'has_native_session', 'attach', 'start', 'inspect', 'read_reply', 'confirm_terminal']);
  const beforeBadRequest = calls.length;
  await assert.rejects(execution.hermes.start({ nativeSessionId: identity.nativeSessionId, text: 'instruction', idempotencyKey: randomUUID() }),
    { code: 'invalid_request' });
  assert.equal(calls.length, beforeBadRequest);
  mode = 'mismatched_identity';
  await assert.rejects(execution.assertLive(controller.signal), { code: 'identity_mismatch' });
  mode = 'redirect';
  await assert.rejects(execution.assertLive(controller.signal), { code: 'invalid_response' });
  mode = 'oversize';
  await assert.rejects(execution.assertLive(controller.signal), { code: 'invalid_response' });
  mode = 'denied';
  await assert.rejects(execution.assertLive(controller.signal), { code: 'access_denied' });
  mode = 'stall';
  const short = createHermesConnector({ origin, ca, cert: clientCert, key: clientKey, timeoutMs: 50 });
  await assert.rejects(short(identity, controller.signal), { code: 'timeout' });
  const cancelled = new AbortController();
  cancelled.abort();
  await assert.rejects(connector(identity, cancelled.signal), { code: 'aborted' });
});

test('TLS identity, client authentication and secret files are enforced before a connector grant', async t => {
  let applicationCalls = 0;
  const server = createServer({ ca, cert: serverCert, key: serverKey, requestCert: true, rejectUnauthorized: true }, (_req, res) => {
    applicationCalls++;
    res.writeHead(500).end();
  });
  t.after(async () => { await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }); });
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(10084, '127.0.0.1', resolve); });
  assert.throws(() => createHermesConnector({ origin: 'http://127.0.0.1:10084', ca, cert: clientCert, key: clientKey }),
    { code: 'invalid_configuration' });
  const wrongHost = createHermesConnector({ origin: 'https://localhost:10084', ca, cert: clientCert, key: clientKey });
  await assert.rejects(wrongHost(identity, controller.signal), { code: 'tls_rejected' });
  const anonymous = new Promise((resolve, reject) => {
    const outgoing = request(origin, { ca, rejectUnauthorized: true, method: 'POST', agent: false }, response => {
      response.resume();
      resolve(response.statusCode);
    });
    outgoing.once('error', reject);
    outgoing.end('{}');
  });
  await assert.rejects(anonymous);
  assert.equal(applicationCalls, 0);
  const unsafeKeyPath = join(directory, 'unsafe.key');
  await writeFile(unsafeKeyPath, clientKey, { mode: 0o644 });
  await assert.rejects(loadHermesConnector({ origin, caFile: join(directory, 'ca.pem'), certFile: join(directory, 'client.pem'), keyFile: unsafeKeyPath }),
    { code: 'invalid_configuration' });
});
