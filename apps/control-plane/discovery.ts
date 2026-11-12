import Fastify from 'fastify';
import { authenticationDiscoveryNotConfigured, authenticationDiscoveryPath, parseAuthenticationDiscovery } from '@harniqo/contracts';

export function createControlPlane(discovery: unknown, tls: { key: Buffer; cert: Buffer } | null) {
  const metadata = discovery === undefined && tls === null ? undefined : parseAuthenticationDiscovery(discovery);
  const server = Fastify({
    ...(tls ? { https: { ...tls, minVersion: 'TLSv1.2' } } : {}),
    logger: false, trustProxy: false, exposeHeadRoutes: false,
    requestTimeout: 10_000, connectionTimeout: 10_000, keepAliveTimeout: 5000,
  });
  server.get(authenticationDiscoveryPath, (_request, reply) => {
    reply.type('application/json').header('cache-control', 'no-store');
    if (metadata === undefined) return reply.code(503).send({ error: authenticationDiscoveryNotConfigured });
    return reply.send(metadata);
  });
  server.setNotFoundHandler((_request, reply) => reply.code(404).send({ error: 'not_found' }));
  return server;
}
