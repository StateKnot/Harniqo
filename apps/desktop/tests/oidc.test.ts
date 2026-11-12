import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { get } from 'node:http';
import { test } from 'node:test';
import { signIn } from '../src/main/oidc.js';

const issuer = 'https://identity.example.invalid/realms/harniqo';
const clientId = 'harniqo-desktop';

function visitCallback(url: URL): Promise<number | undefined> {
  return new Promise((resolve, reject) => {
    const request = get(url, response => {
      response.resume();
      response.once('end', () => resolve(response.statusCode));
      response.once('error', reject);
    });
    request.once('error', reject);
    request.setTimeout(2000, () => request.destroy(new Error('callback timed out')));
  });
}

const loginCases = [
  { name: 'a wrong-state callback cannot exchange a code or consume the browser login', signatureValid: true },
  { name: 'an ID token with an invalid signature cannot sign in', signatureValid: false },
];

for (const { name, signatureValid } of loginCases) test(name, async t => {
  const keys = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  let authorizationUrl: URL | undefined;
  let exchanged = 0;
  const issuedAt = Math.floor(Date.now() / 1000);
  const expiresAt = issuedAt + 300;

  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, options?: RequestInit) => {
    assert.equal(options?.redirect, 'manual');
    switch (String(input)) {
      case issuer + '/.well-known/openid-configuration':
        return Response.json({
          issuer,
          authorization_endpoint: issuer + '/authorize',
          token_endpoint: issuer + '/token',
          jwks_uri: issuer + '/jwks',
          id_token_signing_alg_values_supported: ['ES256'],
        });
      case issuer + '/jwks':
        return Response.json({ keys: [{ ...keys.publicKey.export({ format: 'jwk' }), kid: 'fixture', alg: 'ES256' }] });
      case issuer + '/token': {
        assert(authorizationUrl);
        assert.equal(options?.method, 'POST');
        assert(options?.body instanceof URLSearchParams);
        assert.equal(options.body.get('code'), 'correct-code');
        assert.equal(options.body.get('client_id'), clientId);
        assert.equal(options.body.get('redirect_uri'), authorizationUrl.searchParams.get('redirect_uri'));
        const challenge = createHash('sha256').update(options.body.get('code_verifier') ?? '').digest('base64url');
        assert.equal(challenge, authorizationUrl.searchParams.get('code_challenge'));
        exchanged++;
        const header = Buffer.from(JSON.stringify({ alg: 'ES256', kid: 'fixture' })).toString('base64url');
        const claims = Buffer.from(JSON.stringify({
          iss: issuer, aud: clientId, sub: 'account-42', name: '测试账号',
          iat: issuedAt, exp: expiresAt, nonce: authorizationUrl.searchParams.get('nonce'),
        })).toString('base64url');
        const signature = sign('sha256', Buffer.from(header + '.' + claims), {
          key: keys.privateKey, dsaEncoding: 'ieee-p1363',
        });
        if (!signatureValid) signature[0] = signature[0]! ^ 1;
        return Response.json({
          access_token: 'access-token', token_type: 'Bearer',
          id_token: header + '.' + claims + '.' + signature.toString('base64url'),
        });
      }
      default:
        throw new Error('unexpected endpoint');
    }
  });

  const browse = async (url: string) => {
    authorizationUrl = new URL(url);
    assert.equal(authorizationUrl.origin + authorizationUrl.pathname, issuer + '/authorize');
    assert.equal(authorizationUrl.searchParams.get('code_challenge_method'), 'S256');
    assert(authorizationUrl.searchParams.get('nonce'));
    const redirect = new URL(authorizationUrl.searchParams.get('redirect_uri') ?? '');
    assert.equal(redirect.hostname, '127.0.0.1');
    assert.equal(redirect.protocol, 'http:');
    assert.equal(redirect.pathname, '/oauth/callback');
    redirect.searchParams.set('code', 'wrong-code');
    redirect.searchParams.set('state', 'wrong-state');
    assert.equal(await visitCallback(redirect), 400);
    assert.equal(exchanged, 0);
    redirect.searchParams.set('code', 'correct-code');
    redirect.searchParams.set('state', authorizationUrl.searchParams.get('state') ?? '');
    assert.equal(await visitCallback(redirect), 200);
  };
  let browserActivity: Promise<void> | undefined;
  const login = signIn({ issuer, client_id: clientId, required_scopes: ['openid', 'profile'] }, url => {
    browserActivity = browse(url);
    return browserActivity;
  }, AbortSignal.timeout(5000));

  if (signatureValid) {
    assert.deepEqual(await login, { issuer, subject: 'account-42', displayName: '测试账号', expiresAt: expiresAt * 1000 });
  } else {
    await assert.rejects(login, { code: 'oidc_token_rejected' });
  }
  await browserActivity;
  assert.equal(exchanged, 1);
});
