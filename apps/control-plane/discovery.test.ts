import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:https';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { discoverPlatform, DiscoveryError } from '@harniqo/client';
import { createControlPlane } from './discovery.ts';

const metadata = {
  issuer: 'https://identity.example.invalid/realms/harniqo',
  client_id: 'harniqo-desktop',
  required_scopes: ['openid', 'profile'],
  authorization_code_pkce_required: true,
};
let directory: string;
let tls: { key: Buffer; cert: Buffer };
const platform = 'https://localhost:10050';

before(async () => {
  directory = await mkdtemp(join(tmpdir(), 'harniqo-discovery-'));
  const cert = join(directory, 'cert.pem');
  const key = join(directory, 'key.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
    '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost',
    '-addext', 'subjectAltName=DNS:localhost'], { stdio: 'ignore', timeout: 20_000 });
  tls = { key: await readFile(key), cert: await readFile(cert) };
});
after(async () => {
  if (directory) await rm(directory, { recursive: true, force: true });
});

test('discovery returns only the configured public DTO over verified TLS', async t => {
  const server = createControlPlane(metadata, tls);
  t.after(() => server.close());
  await server.listen({ host: '127.0.0.1', port: 10050 });
  const discovery = await discoverPlatform(platform, { ca: tls.cert });
  assert.deepEqual(discovery, metadata);
  assert.equal(Object.isFrozen(discovery.required_scopes), true);
  await assert.rejects(discoverPlatform(platform), { code: 'discovery_unreachable' });
  await assert.rejects(discoverPlatform('https://127.0.0.1:10050', { ca: tls.cert }), { code: 'discovery_unreachable' });
  for (const [method, url] of [
    ['GET', '/v1/meta'], ['POST', '/v1/handshakes'], ['POST', '/runner/v1/handshakes'],
  ] as const) {
    assert.equal((await server.inject({ method, url })).statusCode, 404);
  }
});

test('startup rejects private fields, malformed issuer, missing OpenID scope and disabled PKCE', () => {
  for (const change of [
    { client_secret: 'fixture-only' }, { issuer: 'https://guest:secret@identity.example.invalid' },
    { issuer: 'https://identity.example.invalid/?debug=1' }, { issuer: 'https://identity.example.invalid/#' },
    { required_scopes: ['profile'] }, { required_scopes: ['openid', 'two scopes'] },
    { authorization_code_pkce_required: false },
  ]) {
    assert.throws(() => createControlPlane({ ...metadata, ...change }, tls), { message: 'invalid_discovery' });
  }
});

test('client rejects ambiguous or non-HTTPS origins before sending requests', async () => {
  for (const origin of ['http://localhost', 'https://localhost/subpath', 'https://localhost/?',
    'https://localhost#', 'https://@localhost', ' https://localhost', 'https://localhost\\', 'https://localhost\u0000']) {
    await assert.rejects(discoverPlatform(origin), { code: 'invalid_platform_origin' });
  }
});

test('client stops at redirects, rejects malformed JSON and bounds the TLS response', async t => {
  let calls = 0;
  const server = createServer(tls, (_request, response) => {
    calls++;
    if (calls === 1) {
      response.writeHead(302, { location: '/v1/auth/discovery' }).end();
    } else if (calls === 2) {
      response.writeHead(200, { 'content-type': 'application/json' }).end('{');
    } else {
      response.writeHead(200, { 'content-type': 'application/json' }).end('x'.repeat(32 * 1024 + 1));
    }
  });
  t.after(() => new Promise<void>(resolve => server.close(() => resolve())));
  server.listen(10051, '127.0.0.1');
  await once(server, 'listening');
  for (const [index, code] of ['discovery_http_status', 'invalid_discovery', 'discovery_too_large'].entries()) {
    await assert.rejects(discoverPlatform('https://localhost:10051', { ca: tls.cert }), error =>
      error instanceof DiscoveryError && error.code === code);
    assert.equal(calls, index + 1);
  }
});
