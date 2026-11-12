import { readFile } from 'node:fs/promises';
import { createControlPlane } from './discovery.ts';

async function main() {
  const { HARNIQO_DISCOVERY_FILE, HARNIQO_TLS_KEY_FILE, HARNIQO_TLS_CERT_FILE,
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
  const server = createControlPlane(config, tls);
  await server.listen({ host: HARNIQO_CONTROL_HOST, port: Number(HARNIQO_CONTROL_PORT) });
  process.once('SIGTERM', () => void server.close());
  process.once('SIGINT', () => void server.close());
  console.log(config === undefined ? 'Harniqo HTTP discovery listening without identity configuration'
    : `Harniqo ${HARNIQO_CONTROL_PROTOCOL.toUpperCase()} discovery ready`);
}

try {
  await main();
} catch {
  console.error('Harniqo discovery startup failed. Check discovery settings, protocol, TLS files and bind address.');
  process.exitCode = 1;
}
