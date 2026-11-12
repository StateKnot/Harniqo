import { readFile } from 'node:fs/promises';
import { createControlPlane } from './discovery.ts';

async function main() {
  const { HARNIQO_DISCOVERY_FILE, HARNIQO_TLS_KEY_FILE, HARNIQO_TLS_CERT_FILE,
    HARNIQO_CONTROL_HOST = '127.0.0.1', HARNIQO_CONTROL_PORT = '10050' } = process.env;
  if (!HARNIQO_DISCOVERY_FILE || !HARNIQO_TLS_KEY_FILE || !HARNIQO_TLS_CERT_FILE
    || !/^\d{1,5}$/.test(HARNIQO_CONTROL_PORT) || Number(HARNIQO_CONTROL_PORT) < 1
    || Number(HARNIQO_CONTROL_PORT) > 65535) throw new Error('invalid_control_plane_configuration');
  const [config, key, cert] = await Promise.all([
    readFile(HARNIQO_DISCOVERY_FILE, 'utf8'), readFile(HARNIQO_TLS_KEY_FILE), readFile(HARNIQO_TLS_CERT_FILE),
  ]);
  const server = createControlPlane(JSON.parse(config), { key, cert });
  await server.listen({ host: HARNIQO_CONTROL_HOST, port: Number(HARNIQO_CONTROL_PORT) });
  process.once('SIGTERM', () => void server.close());
  process.once('SIGINT', () => void server.close());
  console.log('Harniqo HTTPS discovery ready');
}

try {
  await main();
} catch {
  console.error('Harniqo HTTPS discovery startup failed. Check discovery settings, TLS files and bind address.');
  process.exitCode = 1;
}
