import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
import { Kysely, PostgresDialect, sql } from 'kysely';
import pg from 'pg';
import { createAccessTokenVerifier, createWorkbenchAccess } from './index.ts';
import type { WorkbenchAccessConfig } from './index.ts';

const config = {
  organizationId: randomUUID(),
  issuer: 'https://issuer.example.invalid/realms/harniqo',
  jwksUri: 'https://issuer.example.invalid/realms/harniqo/protocol/openid-connect/certs',
  audience: 'harniqo-platform',
  desktopClientId: 'harniqo-desktop',
  accessTokenProfile: 'keycloak',
  algorithm: 'RS256',
} as const satisfies WorkbenchAccessConfig;

async function signer(kid: string) {
  const pair = await generateKeyPair(config.algorithm);
  const jwk = { ...await exportJWK(pair.publicKey), kid, alg: config.algorithm, use: 'sig' };
  return {
    jwk,
    async bearer(claims: Record<string, unknown> = {}, typ = 'JWT') {
      const now = Math.floor(Date.now() / 1000);
      const token = await new SignJWT({
        iss: config.issuer, sub: 'Case-Sensitive-Subject', aud: [config.audience, 'account'],
        iat: now - 1, exp: now + 60, typ: 'Bearer', azp: config.desktopClientId, ...claims,
      }).setProtectedHeader({ alg: config.algorithm, kid, typ }).sign(pair.privateKey);
      return `Bearer ${token}`;
    },
  };
}

test('verifies the configured Keycloak bearer and refreshes cached keys on rotation', async t => {
  const first = await signer('first');
  const second = await signer('second');
  let keys = [first.jwk];
  let calls = 0;
  const verify = createAccessTokenVerifier(config, async (input, init) => {
    assert.equal(String(input), config.jwksUri);
    assert.equal(init?.redirect, 'error');
    assert.equal(init?.credentials, 'omit');
    calls++;
    return Response.json({ keys });
  });
  t.mock.timers.enable({ apis: ['Date'], now: Date.now() });
  const bearer = await first.bearer();
  assert.equal(await verify(bearer), 'Case-Sensitive-Subject');
  assert.equal(await verify(bearer), 'Case-Sensitive-Subject');
  assert.equal(calls, 1);
  keys = [second.jwk];
  await assert.rejects(verify(await second.bearer()), { code: 'UNAUTHENTICATED' });
  assert.equal(calls, 1);
  t.mock.timers.tick(10_001);
  assert.equal(await verify(await second.bearer()), 'Case-Sensitive-Subject');
  assert.equal(calls, 2);
  await assert.rejects(verify(bearer), { code: 'UNAUTHENTICATED' });
});

test('rejects a signed ID token, wrong issuer/audience/client and unusable bearer credentials', async () => {
  const key = await signer('chosen');
  const forged = await signer('chosen');
  const verify = createAccessTokenVerifier(config, async () => Response.json({ keys: [key.jwk] }));
  const now = Math.floor(Date.now() / 1000);
  for (const claims of [
    { typ: 'ID' }, { typ: 'Refresh' }, { cnf: { jkt: 'sender-bound' } }, { sub: 'control\u0000subject' },
    { iss: 'https://different.example.invalid' }, { aud: 'another-resource' }, { azp: 'another-client' },
    { iat: now + 60, exp: now + 120 }, { iat: now - 60, exp: now - 30 }, { iat: null }, { exp: null },
  ]) {
    await assert.rejects(verify(await key.bearer(claims)), { code: 'UNAUTHENTICATED' });
  }
  await assert.rejects(verify(await forged.bearer()), { code: 'UNAUTHENTICATED' });
  await assert.rejects(verify(undefined), { code: 'UNAUTHENTICATED' });
  await assert.rejects(verify(`${await key.bearer()}, ${await key.bearer()}`), { code: 'UNAUTHENTICATED' });
});

test('the external issuer profile requires a typed RFC 9068 token', async () => {
  const key = await signer('external');
  const verify = createAccessTokenVerifier({ ...config, accessTokenProfile: 'rfc9068' },
    async () => Response.json({ keys: [key.jwk] }));
  const claims = { client_id: config.desktopClientId, jti: 'fixture-access-token', typ: null, azp: null };
  for (const type of ['at+jwt', 'application/at+jwt']) assert.equal(await verify(await key.bearer(claims, type)), 'Case-Sensitive-Subject');
  await assert.rejects(verify(await key.bearer(claims, 'JWT')), { code: 'UNAUTHENTICATED' });
  await assert.rejects(verify(await key.bearer({ ...claims, client_id: 'other' }, 'at+jwt')), { code: 'UNAUTHENTICATED' });
  await assert.rejects(verify(await key.bearer({ ...claims, jti: null }, 'at+jwt')), { code: 'UNAUTHENTICATED' });
});

test('rejects ambiguous in-process configuration and bounds unavailable key responses', async () => {
  const key = await signer('jwks');
  const bearer = await key.bearer();
  for (const change of [
    { issuer: 'http://issuer.example.invalid/realms/harniqo' }, { jwksUri: 'https://@issuer.example.invalid/keys' },
    { jwksUri: 'https://issuer.example.invalid/keys?target=alternate' }, { audience: config.desktopClientId },
  ]) {
    assert.throws(() => createAccessTokenVerifier({ ...config, ...change }), { message: 'INVALID_WORKBENCH_ACCESS_CONFIG' });
  }
  const endpointFailures = [
    () => new Response(null, { status: 302, headers: { location: 'https://other.example.invalid' } }),
    () => Response.json({ keys: [], overflow: 'x'.repeat(256 * 1024) }),
    () => new Response('{', { headers: { 'content-type': 'application/json' } }),
    () => Response.json({ notKeys: [] }),
  ];
  for (const response of endpointFailures) {
    const verify = createAccessTokenVerifier(config, async () => response());
    await assert.rejects(verify(bearer), { code: 'UPSTREAM_UNAVAILABLE' });
  }
});

test('PostgreSQL resolves only an active exact identity in the configured organization', {
  skip: !process.env.HARNIQO_ACCESS_POSTGRES_URL, timeout: 20_000,
}, async () => {
  const url = new URL(process.env.HARNIQO_ACCESS_POSTGRES_URL!);
  assert.equal(url.protocol, 'postgresql:');
  assert.equal(url.hostname, '127.0.0.1');
  assert.equal(url.pathname, '/harniqo_access_test');
  const connect = (connectionString: string) => new Kysely<unknown>({
    dialect: new PostgresDialect({ pool: new pg.Pool({ connectionString, max: 1, connectionTimeoutMillis: 3000 }) }),
  });
  const admin = connect(url.href);
  const principalId = randomUUID();
  const password = randomBytes(32).toString('hex');
  let runtime: Kysely<unknown> | undefined;
  try {
    const store = await readFile(new URL('../workbench-store/001-first-run.sql', import.meta.url), 'utf8');
    const identities = await readFile(new URL('./002-external-identities.sql', import.meta.url), 'utf8');
    await admin.transaction().execute(async tx => {
      await sql`CREATE ROLE harniqo_migrator NOLOGIN NOSUPERUSER NOBYPASSRLS`.execute(tx);
      await sql`CREATE ROLE harniqo_control LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD ${sql.lit(password)}`.execute(tx);
      await sql`GRANT CREATE ON DATABASE harniqo_access_test TO harniqo_migrator`.execute(tx);
      await sql`SET LOCAL ROLE harniqo_migrator`.execute(tx);
      await sql.raw(store).execute(tx);
      await sql.raw(identities).execute(tx);
      await sql`INSERT INTO harniqo.external_identities VALUES
        (${config.issuer}, 'Case-Sensitive-Subject', ${config.organizationId}, ${principalId}, true),
        (${config.issuer}, 'different-organization', ${randomUUID()}, ${randomUUID()}, true)`.execute(tx);
    });
    url.username = 'harniqo_control';
    url.password = password;
    runtime = connect(url.href);
    const key = await signer('database');
    const authenticate = createWorkbenchAccess(runtime, config, async () => Response.json({ keys: [key.jwk] }));
    const bearer = await key.bearer({ organization_id: randomUUID(), email: 'ignored@example.invalid' });
    assert.deepEqual(await authenticate(bearer), { organizationId: config.organizationId, principalId });
    for (const sub of ['case-sensitive-subject', 'different-organization', 'unmapped-subject']) {
      await assert.rejects(authenticate(await key.bearer({ sub })), { code: 'FORBIDDEN' });
    }
    assert.deepEqual((await sql`SELECT principal_id FROM harniqo.external_identities`.execute(runtime)).rows, []);
    await sql`UPDATE harniqo.external_identities SET enabled = false WHERE principal_id = ${principalId}`.execute(admin);
    await assert.rejects(authenticate(bearer), { code: 'FORBIDDEN' });
  } finally {
    await runtime?.destroy();
    await admin.destroy();
  }
});
