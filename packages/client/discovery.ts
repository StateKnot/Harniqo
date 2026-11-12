import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { authenticationDiscoveryNotConfigured, authenticationDiscoveryPath, parseAuthenticationDiscovery } from '@harniqo/contracts';
import type { AuthenticationDiscovery } from '@harniqo/contracts';

const maxDiscoveryBytes = 32 * 1024;

export class DiscoveryError extends Error {
  readonly code: DiscoveryErrorCode;

  constructor(code: DiscoveryErrorCode) {
    super(code);
    this.name = 'DiscoveryError';
    this.code = code;
  }
}

export type DiscoveryErrorCode = 'invalid_platform_origin' | 'discovery_unreachable' | 'discovery_timeout'
  | 'discovery_http_status' | 'invalid_discovery' | 'discovery_too_large' | 'certificate_rejected'
  | typeof authenticationDiscoveryNotConfigured;

const certificateErrors = new Set([
  'ERR_TLS_CERT_ALTNAME_INVALID', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'SELF_SIGNED_CERT_IN_CHAIN',
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'UNABLE_TO_GET_ISSUER_CERT', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
  'CERT_HAS_EXPIRED', 'CERT_NOT_YET_VALID', 'CERT_REVOKED',
]);

export async function discoverPlatform(origin: string, options: { ca?: Buffer; signal?: AbortSignal } = {}): Promise<AuthenticationDiscovery> {
  const platform = URL.parse(origin);
  if (!platform?.hostname || platform.pathname !== '/'
    || !/^https?:\/\/[^/@?#\\\u0000-\u0020\u007f]+\/?$/u.test(origin) || /\s/u.test(origin)) {
    throw new DiscoveryError('invalid_platform_origin');
  }
  const secure = platform.protocol === 'https:';
  const request = secure ? httpsRequest : httpRequest;
  const deadline = AbortSignal.timeout(5000);
  const signal = options.signal ? AbortSignal.any([deadline, options.signal]) : deadline;
  const result = await new Promise<{ status: 200 | 503; body: Buffer }>((resolve, reject) => {
    const outgoing = request(new URL(authenticationDiscoveryPath, platform), {
      method: 'GET', agent: false,
      headers: { accept: 'application/json' }, signal, maxHeaderSize: 8192,
      ...(secure ? { rejectUnauthorized: true, minVersion: 'TLSv1.2', ca: options.ca } : {}),
    }, async response => {
      try {
        const status = response.statusCode;
        if (status !== 200 && status !== 503) throw new DiscoveryError('discovery_http_status');
        if (!/^application\/json(?:\s*;|$)/i.test(response.headers['content-type'] ?? '')) {
          throw new DiscoveryError(status === 200 ? 'invalid_discovery' : 'discovery_http_status');
        }
        const chunks: Buffer[] = [];
        let size = 0;
        for await (const chunk of response) {
          const bytes: Buffer = chunk;
          size += bytes.length;
          if (size > maxDiscoveryBytes) throw new DiscoveryError('discovery_too_large');
          chunks.push(bytes);
        }
        if (!response.complete) throw new DiscoveryError('discovery_unreachable');
        resolve({ status, body: Buffer.concat(chunks, size) });
      } catch (error) {
        reject(error instanceof DiscoveryError ? error
          : new DiscoveryError(deadline.aborted ? 'discovery_timeout' : 'discovery_unreachable'));
        response.destroy();
      }
    });
    outgoing.once('error', (error: NodeJS.ErrnoException) => {
      const code = deadline.aborted ? 'discovery_timeout'
        : certificateErrors.has(error.code ?? '') ? 'certificate_rejected' : 'discovery_unreachable';
      reject(new DiscoveryError(code));
    });
    outgoing.end();
  });
  const malformed = result.status === 200 ? 'invalid_discovery' : 'discovery_http_status';
  let metadata: unknown;
  try { metadata = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(result.body)); }
  catch { throw new DiscoveryError(malformed); }
  if (result.status === 503) {
    if (metadata && typeof metadata === 'object' && 'error' in metadata
      && metadata.error === authenticationDiscoveryNotConfigured && Object.keys(metadata).length === 1) {
      throw new DiscoveryError(authenticationDiscoveryNotConfigured);
    }
    throw new DiscoveryError('discovery_http_status');
  }
  try { return parseAuthenticationDiscovery(metadata); }
  catch { throw new DiscoveryError('invalid_discovery'); }
}
