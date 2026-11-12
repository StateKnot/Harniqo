import { once } from 'node:events';
import { createServer, type ServerResponse } from 'node:http';
import * as oauth from 'oauth4webapi';

export type LoginMetadata = {
  issuer: string;
  client_id: string;
  required_scopes: readonly string[];
};

type LoginAccount = {
  issuer: string;
  subject: string;
  displayName: string;
  expiresAt: number;
};

export type OidcLoginErrorCode = 'oidc_invalid_provider' | 'oidc_browser_failed'
  | 'oidc_callback_failed' | 'oidc_token_rejected' | 'oidc_cancelled' | 'oidc_timed_out';

export class OidcLoginError extends Error {
  constructor(readonly code: OidcLoginErrorCode) {
    super(code);
    this.name = 'OidcLoginError';
  }
}

const loopbackHost = '127.0.0.1';
const callbackPath = '/oauth/callback';

function httpsEndpoint(value: unknown): URL {
  const url = typeof value === 'string' && /^https:\/\/[^/@?#\\\s]+(?:[/?][^#\\\s]*)?$/iu.test(value)
    ? URL.parse(value)
    : null;
  if (!url || url.protocol !== 'https:' || !url.hostname) throw new OidcLoginError('oidc_invalid_provider');
  return url;
}

function reply(response: ServerResponse, status: number, message: string, done?: () => void): void {
  response.writeHead(status, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    Connection: 'close',
  });
  if (done) response.once('close', done);
  response.end(message);
}

async function browserAuthorization(
  provider: oauth.AuthorizationServer,
  client: oauth.Client,
  authorizationUrl: URL,
  openBrowser: (url: string) => Promise<void>,
  signal: AbortSignal,
): Promise<{ parameters: URLSearchParams; redirectUri: string }> {
  const state = oauth.generateRandomState();
  const server = createServer({ maxHeaderSize: 8192, headersTimeout: 5000, requestTimeout: 5000 });
  server.maxConnections = 8;
  let onAbort: (() => void) | undefined;

  try {
    signal.throwIfAborted();
    server.listen({ host: loopbackHost, port: 0, signal });
    await once(server, 'listening', { signal });
    const address = server.address();
    if (address === null || typeof address === 'string') throw new OidcLoginError('oidc_callback_failed');
    const redirect = new URL(callbackPath, `http://${loopbackHost}:${address.port}`);
    authorizationUrl.searchParams.set('redirect_uri', redirect.href);
    authorizationUrl.searchParams.set('state', state);
    signal.throwIfAborted();
    const parameters = await new Promise<URLSearchParams>((resolve, reject) => {
      let received = false;
      onAbort = () => reject(signal.reason);
      signal.addEventListener('abort', onAbort, { once: true });
      server.on('error', () => reject(new OidcLoginError('oidc_callback_failed')));
      server.on('request', (request, response) => {
        if (request.method !== 'GET' || request.headers.host !== redirect.host
          || !request.url?.startsWith(callbackPath + '?')) {
          reply(response, 404, '找不到此登录回调。');
          return;
        }
        const query = new URLSearchParams(request.url.slice(callbackPath.length + 1));
        if (received || query.getAll('state').length !== 1 || query.get('state') !== state) {
          reply(response, 400, '此登录回调无效。');
          return;
        }
        received = true;
        try {
          const parameters = oauth.validateAuthResponse(provider, client, query, state);
          if (!parameters.get('code')) throw new OidcLoginError('oidc_callback_failed');
          reply(response, 200, '请返回 Harniqo 查看登录结果。', () => resolve(parameters));
        } catch {
          reply(response, 400, '登录未完成，请返回 Harniqo 重试。', () => {
            reject(new OidcLoginError('oidc_callback_failed'));
          });
        }
      });
      void Promise.resolve().then(() => {
        signal.throwIfAborted();
        return openBrowser(authorizationUrl.href);
      }).catch(() => {
        if (!received) reject(new OidcLoginError('oidc_browser_failed'));
      });
    });
    return { parameters, redirectUri: redirect.href };
  } finally {
    if (onAbort) signal.removeEventListener('abort', onAbort);
    server.close();
    server.closeAllConnections();
  }
}

export async function signIn(
  metadata: LoginMetadata,
  openBrowser: (url: string) => Promise<void>,
  signal: AbortSignal,
): Promise<LoginAccount> {
  const expiry = AbortSignal.timeout(180_000);
  const attempt = AbortSignal.any([signal, expiry]);
  const httpOptions = { signal: () => AbortSignal.any([attempt, AbortSignal.timeout(12_000)]) };
  let stage: OidcLoginErrorCode = 'oidc_invalid_provider';

  try {
    attempt.throwIfAborted();
    const issuer = httpsEndpoint(metadata.issuer);
    if (metadata.issuer.includes('?')) throw new OidcLoginError('oidc_invalid_provider');
    const discovery = await oauth.discoveryRequest(issuer, httpOptions);
    const provider = await oauth.processDiscoveryResponse(issuer, discovery);
    const advertisedChallenges = provider.code_challenge_methods_supported;
    if (provider.issuer !== metadata.issuer || (advertisedChallenges !== undefined
      && (!Array.isArray(advertisedChallenges) || !advertisedChallenges.includes('S256')))) {
      throw new OidcLoginError('oidc_invalid_provider');
    }
    const authorizationUrl = httpsEndpoint(provider.authorization_endpoint);
    httpsEndpoint(provider.token_endpoint);
    httpsEndpoint(provider.jwks_uri);

    const client = { client_id: metadata.client_id };
    const verifier = oauth.generateRandomCodeVerifier();
    const nonce = oauth.generateRandomNonce();
    authorizationUrl.searchParams.set('client_id', client.client_id);
    authorizationUrl.searchParams.set('response_type', 'code');
    authorizationUrl.searchParams.set('response_mode', 'query');
    authorizationUrl.searchParams.set('scope', metadata.required_scopes.join(' '));
    authorizationUrl.searchParams.set('nonce', nonce);
    authorizationUrl.searchParams.set('code_challenge', await oauth.calculatePKCECodeChallenge(verifier));
    authorizationUrl.searchParams.set('code_challenge_method', 'S256');

    stage = 'oidc_callback_failed';
    const { parameters, redirectUri } = await browserAuthorization(provider, client, authorizationUrl, openBrowser, attempt);

    stage = 'oidc_token_rejected';
    const response = await oauth.authorizationCodeGrantRequest(
      provider, client, oauth.None(), parameters, redirectUri, verifier, httpOptions,
    );
    const result = await oauth.processAuthorizationCodeResponse(provider, client, response, {
      expectedNonce: nonce,
      requireIdToken: true,
    });
    await oauth.validateApplicationLevelSignature(provider, response, httpOptions);
    attempt.throwIfAborted();
    const claims = oauth.getValidatedIdTokenClaims(result);
    const expiresAt = claims ? claims.exp * 1000 : NaN;
    if (!claims || !Number.isSafeInteger(expiresAt) || expiresAt <= Date.now() || claims.sub.length > 255) {
      throw new OidcLoginError('oidc_token_rejected');
    }
    const name = typeof claims.name === 'string' ? claims.name : claims.preferred_username;
    const displayName = typeof name === 'string'
      ? Array.from(name.replace(/[\p{Cc}\p{Cf}]/gu, '').trim()).slice(0, 80).join('')
      : '';
    return { issuer: provider.issuer, subject: claims.sub, displayName: displayName || 'OIDC 用户', expiresAt };
  } catch (error) {
    if (signal.aborted) throw new OidcLoginError('oidc_cancelled');
    if (expiry.aborted || (error instanceof DOMException && error.name === 'TimeoutError')) {
      throw new OidcLoginError('oidc_timed_out');
    }
    // Library errors can include credentials and callback URLs in their causes.
    if (error instanceof OidcLoginError) throw error;
    throw new OidcLoginError(stage);
  }
}
