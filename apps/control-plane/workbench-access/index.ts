import { createRemoteJWKSet, customFetch, decodeProtectedHeader, jwtVerify } from 'jose';
import { sql } from 'kysely';
import type { Kysely } from 'kysely';

export type WorkbenchAccessConfig = Readonly<{
  organizationId: string;
  issuer: string;
  jwksUri: string;
  audience: string;
  desktopClientId: string;
  accessTokenProfile: 'keycloak' | 'rfc9068';
  algorithm: 'RS256' | 'ES256';
}>;

export type WorkbenchPrincipal = Readonly<{ organizationId: string; principalId: string }>;
export type AuthenticateWorkbench = (authorization: string | undefined) => Promise<WorkbenchPrincipal>;
type AccessErrorCode = 'UNAUTHENTICATED' | 'FORBIDDEN' | 'UPSTREAM_UNAVAILABLE';

export class WorkbenchAccessError extends Error {
  readonly code: AccessErrorCode;

  constructor(code: AccessErrorCode) {
    super(code);
    this.name = 'WorkbenchAccessError';
    this.code = code;
  }
}

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
const secondsOfClockSkew = 5;

function httpsEndpoint(value: unknown): URL {
  if (typeof value !== 'string' || value.length > 2048 || value.trim() !== value
    || !/^https:\/\/[^/?#@\\\s\p{Cc}]+(?:\/[^?#\\\s\p{Cc}]*)?$/u.test(value)) {
    throw new Error('INVALID_WORKBENCH_ACCESS_CONFIG');
  }
  const url = URL.parse(value);
  if (!url || url.protocol !== 'https:' || url.username || url.password) {
    throw new Error('INVALID_WORKBENCH_ACCESS_CONFIG');
  }
  return url;
}

function tokenString(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= maxLength
    && !/[\s\p{Cc}\p{Cf}\p{Cs}]/u.test(value);
}

async function boundedJwksResponse(response: Response): Promise<Response> {
  const maxBytes = 256 * 1024;
  if (response.status !== 200 || !response.body
    || !/^application\/(?:json|jwk-set\+json)(?:\s*;|$)/i.test(response.headers.get('content-type') ?? '')
    || Number(response.headers.get('content-length')) > maxBytes) {
    await response.body?.cancel();
    throw new WorkbenchAccessError('UPSTREAM_UNAVAILABLE');
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new WorkbenchAccessError('UPSTREAM_UNAVAILABLE');
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  // Validate encoding as well as JSON. Replacement characters could turn an
  // invalid key ID into a valid one during response parsing.
  const body: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  return Response.json(body);
}

export function createAccessTokenVerifier(config: WorkbenchAccessConfig, fetchJwks: typeof fetch = fetch) {
  httpsEndpoint(config.issuer);
  const endpoint = httpsEndpoint(config.jwksUri);
  if (!uuid.test(config.organizationId) || !tokenString(config.desktopClientId, 256) || !tokenString(config.audience, 2048)
    || config.audience === config.desktopClientId
    || !['keycloak', 'rfc9068'].includes(config.accessTokenProfile) || !['RS256', 'ES256'].includes(config.algorithm)) {
    throw new Error('INVALID_WORKBENCH_ACCESS_CONFIG');
  }

  const { issuer, audience, desktopClientId, accessTokenProfile, algorithm } = config;
  const keys = createRemoteJWKSet(endpoint, {
    timeoutDuration: 3000,
    cacheMaxAge: 300_000,
    cooldownDuration: 10_000,
    [customFetch]: async (url, init) => {
      try {
        return await boundedJwksResponse(await fetchJwks(url, { ...init, redirect: 'error', credentials: 'omit' }));
      } catch {
        throw new WorkbenchAccessError('UPSTREAM_UNAVAILABLE');
      }
    },
  });
  const isKeycloak = accessTokenProfile === 'keycloak';

  return async (authorization: string | undefined): Promise<string> => {
    if (!authorization || authorization.length > 8199) throw new WorkbenchAccessError('UNAUTHENTICATED');
    const match = /^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$/i.exec(authorization);
    const token = match?.[1];
    if (!token || match[0] !== authorization) throw new WorkbenchAccessError('UNAUTHENTICATED');
    try {
      const header = decodeProtectedHeader(token);
      if (!tokenString(header.kid, 256) || header.alg !== algorithm) throw new WorkbenchAccessError('UNAUTHENTICATED');

      const now = new Date();
      const { payload } = await jwtVerify(token, keys, {
        issuer, audience, algorithms: [algorithm], currentDate: now, clockTolerance: secondsOfClockSkew,
        requiredClaims: isKeycloak ? ['iat', 'exp', 'sub', 'azp', 'typ'] : ['iat', 'exp', 'sub', 'client_id', 'jti'],
        ...(!isKeycloak && { typ: 'at+jwt' }),
      });
      const { sub, exp, iat } = payload;
      if (!tokenString(sub, 255) || typeof iat !== 'number' || !Number.isSafeInteger(iat) || iat <= 0
        || iat > Math.floor(now.getTime() / 1000) + secondsOfClockSkew
        || typeof exp !== 'number' || !Number.isSafeInteger(exp) || exp <= iat
        || payload.cnf !== undefined) {
        throw new WorkbenchAccessError('UNAUTHENTICATED');
      }
      if (isKeycloak ? payload.typ !== 'Bearer' || payload.azp !== desktopClientId
        : payload.client_id !== desktopClientId || !tokenString(payload.jti, 255)) {
        throw new WorkbenchAccessError('UNAUTHENTICATED');
      }
      return sub;
    } catch (error) {
      if (error instanceof WorkbenchAccessError) throw error;
      if (error && typeof error === 'object' && 'code' in error
        && ['ERR_JWKS_INVALID', 'ERR_JWKS_TIMEOUT', 'ERR_JWKS_MULTIPLE_MATCHING_KEYS'].some(code => code === error.code)) {
        throw new WorkbenchAccessError('UPSTREAM_UNAVAILABLE');
      }
      throw new WorkbenchAccessError('UNAUTHENTICATED');
    }
  };
}

export function createWorkbenchAccess(
  db: Kysely<unknown>, config: WorkbenchAccessConfig, fetchJwks?: typeof fetch,
): AuthenticateWorkbench {
  const verify = createAccessTokenVerifier(config, fetchJwks);
  const { issuer } = config;
  const organizationId = config.organizationId.toLowerCase();
  return async authorization => {
    const subject = await verify(authorization);
    let principalId: string | undefined;
    try {
      principalId = await db.transaction().execute(async tx => {
        await sql`SELECT set_config('app.organization_id', ${organizationId}, true),
          set_config('app.oidc_issuer', ${issuer}, true), set_config('app.oidc_subject', ${subject}, true)`.execute(tx);
        const result = await sql<{ principal_id: string }>`
          SELECT principal_id FROM harniqo.external_identities
          WHERE organization_id = ${organizationId}::uuid AND issuer = ${issuer}
            AND subject = ${subject} AND enabled`.execute(tx);
        return result.rows[0]?.principal_id;
      });
    } catch {
      throw new WorkbenchAccessError('UPSTREAM_UNAVAILABLE');
    }
    if (!principalId) throw new WorkbenchAccessError('FORBIDDEN');
    return { organizationId, principalId };
  };
}
