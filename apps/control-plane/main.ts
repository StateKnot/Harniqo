import { readFile } from 'node:fs/promises';
import { createControlPlane } from './discovery.ts';

async function main() {
  const { HARNIQO_DISCOVERY_FILE, HARNIQO_TLS_KEY_FILE, HARNIQO_TLS_CERT_FILE,
    HARNIQO_WORKBENCH_FILE, HARNIQO_WORKBENCH_DATABASE_URL_FILE,
    HARNIQO_CONTROL_HOST = '127.0.0.1', HARNIQO_CONTROL_PORT = '10050',
    HARNIQO_CONTROL_PROTOCOL = 'https' } = process.env;
  if (!['http', 'https'].includes(HARNIQO_CONTROL_PROTOCOL)
    || !/^\d{1,5}$/.test(HARNIQO_CONTROL_PORT) || Number(HARNIQO_CONTROL_PORT) < 1
    || Number(HARNIQO_CONTROL_PORT) > 65535) throw new Error('invalid_control_plane_configuration');
  let tls: { key: Buffer; cert: Buffer } | null = null;
  if (HARNIQO_CONTROL_PROTOCOL === 'https') {
    if (!HARNIQO_DISCOVERY_FILE) throw new Error('discovery_file_required');
    if (!HARNIQO_TLS_KEY_FILE || !HARNIQO_TLS_CERT_FILE) throw new Error('tls_files_required');
    const [key, cert] = await Promise.all([readFile(HARNIQO_TLS_KEY_FILE), readFile(HARNIQO_TLS_CERT_FILE)]);
    tls = { key, cert };
  }
  const config: unknown = HARNIQO_DISCOVERY_FILE === undefined ? undefined
    : JSON.parse(await readFile(HARNIQO_DISCOVERY_FILE, 'utf8'));
  if (Boolean(HARNIQO_WORKBENCH_FILE) !== Boolean(HARNIQO_WORKBENCH_DATABASE_URL_FILE)
    || (HARNIQO_WORKBENCH_FILE && !tls)) throw new Error('invalid_workbench_startup');
  const workbench = HARNIQO_WORKBENCH_FILE && HARNIQO_WORKBENCH_DATABASE_URL_FILE
    ? await (await import('./workbench-runtime.ts')).loadWorkbench(config, HARNIQO_WORKBENCH_FILE, HARNIQO_WORKBENCH_DATABASE_URL_FILE)
    : undefined;
  try {
    const server = createControlPlane(config, tls, workbench);
    await server.listen({ host: HARNIQO_CONTROL_HOST, port: Number(HARNIQO_CONTROL_PORT) });
    const shutdown = () => { void server.close().catch(() => { process.exitCode = 1; }); };
    process.once('SIGTERM', shutdown);
    process.once('SIGINT', shutdown);
  } catch (error) {
    await workbench?.close?.();
    throw error;
  }
  console.log(config === undefined ? 'Harniqo HTTP discovery listening without identity configuration'
    : `Harniqo ${HARNIQO_CONTROL_PROTOCOL.toUpperCase()} ${workbench ? 'workbench' : 'discovery'} ready`);
}

try {
  await main();
} catch {
  console.error('Harniqo control plane startup failed. Check service configuration, TLS and configured database/queue.');
  process.exitCode = 1;
}
