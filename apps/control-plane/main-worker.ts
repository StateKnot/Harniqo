import { loadHermesWorker } from './hermes-worker-runtime.ts';

try {
  const { HARNIQO_WORKER_FILE, HARNIQO_WORKER_DATABASE_URL_FILE } = process.env;
  if (!HARNIQO_WORKER_FILE || !HARNIQO_WORKER_DATABASE_URL_FILE) throw new Error('worker_configuration_required');
  const worker = await loadHermesWorker(HARNIQO_WORKER_FILE, HARNIQO_WORKER_DATABASE_URL_FILE);
  const stop = () => { void worker.stop().catch(() => { process.exitCode = 1; }); };
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  console.log('Harniqo Hermes worker ready');
} catch {
  console.error('Harniqo Hermes worker startup failed. Check its configuration, scoped role, queue and connector certificates.');
  process.exitCode = 1;
}
