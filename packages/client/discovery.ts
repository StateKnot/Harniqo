import { request } from 'node:https';
import { authenticationDiscoveryPath, parseAuthenticationDiscovery } from '@harniqo/contracts';
import type { AuthenticationDiscovery } from '@harniqo/contracts';

const maxDiscoveryBytes = 32 * 1024;

export class DiscoveryError extends Error {
  constructor(readonlyCode: 'invalid_platform_origin' | 'discovery_unreachable' | 'discovery_timeout'
    | 'discovery_http_status' | 'invalid_discovery' | 'discovery_too_large') {
    super(readonlyCode);
    this.name = 'DiscoveryError';
    this.code = readonlyCode;
  }
  readonly code;
}

export async function discoverPlatform(origin: string, options: { ca?: Buffer; signal?: AbortSignal } = {}): Promise<AuthenticationDiscovery> {
  const platform = URL.parse(origin);
  if (!platform?.hostname || platform.protocol !== 'https:' || platform.pathname !== '/'
    || !/^https:\/\/[^/@?#\\\u0000-\u0020\u007f]+\/?$/u.test(origin) || /\s/u.test(origin)) {
    throw new DiscoveryError('invalid_platform_origin');
  }
  const deadline = AbortSignal.timeout(5000);
  const signal = options.signal ? AbortSignal.any([deadline, options.signal]) : deadline;
  const result = await new Promise<Buffer>((resolve, reject) => {
    const outgoing = request(new URL(authenticationDiscoveryPath, platform), {
      method: 'GET', agent: false, rejectUnauthorized: true, minVersion: 'TLSv1.2',
      headers: { accept: 'application/json' }, signal, maxHeaderSize: 8192,
      ...(options.ca ? { ca: options.ca } : {}),
    }, async response => {
      try {
        if (response.statusCode !== 200) throw new DiscoveryError('discovery_http_status');
        if (!/^application\/json(?:\s*;|$)/i.test(response.headers['content-type'] ?? '')) {
          throw new DiscoveryError('invalid_discovery');
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
        resolve(Buffer.concat(chunks, size));
      } catch (error) {
        reject(error instanceof DiscoveryError ? error
          : new DiscoveryError(deadline.aborted ? 'discovery_timeout' : 'discovery_unreachable'));
        response.destroy();
      }
    });
    outgoing.once('error', () => reject(new DiscoveryError(deadline.aborted ? 'discovery_timeout' : 'discovery_unreachable')));
    outgoing.end();
  });
  try {
    return parseAuthenticationDiscovery(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(result)));
  } catch {
    throw new DiscoveryError('invalid_discovery');
  }
}
