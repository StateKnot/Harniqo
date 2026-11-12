import Fastify from 'fastify';
import { authenticationDiscoveryPath, parseAuthenticationDiscovery } from '@harniqo/contracts';

export function createControlPlane(discovery: unknown, tls: { key: Buffer; cert: Buffer } | null) {
  const metadata = parseAuthenticationDiscovery(discovery);
  const server = Fastify({
    ...(tls ? { https: { ...tls, minVersion: 'TLSv1.2' } } : {}),
    logger: false, trustProxy: false, exposeHeadRoutes: false,
    requestTimeout: 10_000, connectionTimeout: 10_000, keepAliveTimeout: 5000,
  });
  server.get(authenticationDiscoveryPath, (_request, reply) => {
    return reply.type('application/json').header('cache-control', 'no-store').send(metadata);
  });
  server.setNotFoundHandler((_request, reply) => reply.code(404).send({ error: 'not_found' }));
  return server;
}
