import Fastify from 'fastify';
import { authenticationDiscoveryNotConfigured, authenticationDiscoveryPath, parseAuthenticationDiscovery } from '@harniqo/contracts';
import type { WorkbenchOptions } from './workbench-routes.ts';

export function createControlPlane(
  discovery: unknown,
  tls: { key: Buffer; cert: Buffer } | null,
  workbench?: WorkbenchOptions,
) {
  if (workbench && !tls) throw new Error('workbench_tls_required');
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
  if (workbench) server.register(async instance => {
    const { registerWorkbench } = await import('./workbench-routes.ts');
    await registerWorkbench(instance, workbench);
  });
  return server;
}
