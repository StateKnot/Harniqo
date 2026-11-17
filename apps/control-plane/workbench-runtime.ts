import { constants } from 'node:fs';
import { open, readFile } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { parseAuthenticationDiscovery } from '@harniqo/contracts';
import { contractValidator } from '@harniqo/contracts/validation';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { createWorkbenchAccess } from './workbench-access/index.ts';
import type { WorkbenchAccessConfig } from './workbench-access/index.ts';
import { createWorkbenchQueue, startWorkbenchProducer } from './workbench-queue/index.ts';
import { WorkbenchProblem } from './workbench-routes.ts';
import type { PlatformMetadata, WorkbenchOptions } from './workbench-routes.ts';
import type { ReserveAndEnqueue } from './workbench-store/index.ts';

type Configuration = { access: WorkbenchAccessConfig; platform: PlatformMetadata };
const validatePlatform = contractValidator<PlatformMetadata>('PlatformMetadata');

function fields(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function configuration(value: unknown): Configuration {
  const accessFields = ['organizationId', 'issuer', 'jwksUri', 'audience', 'desktopClientId', 'accessTokenProfile', 'algorithm'];
  if (!fields(value, ['access', 'platform']) || !fields(value.access, accessFields)
    || !Object.values(value.access).every(field => typeof field === 'string') || !validatePlatform(value.platform)) {
    throw new Error('invalid_workbench_configuration');
  }
  return { access: value.access as WorkbenchAccessConfig, platform: value.platform };
}

async function databaseConnectionString(path: string): Promise<string> {
  if (!isAbsolute(path)) throw new Error('invalid_workbench_database_file');
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 1 || stat.size > 8192
      || (process.platform !== 'win32' && ((stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.()))) {
      throw new Error('invalid_workbench_database_file');
    }
    const result = (await file.readFile('utf8')).trim();
    const url = URL.parse(result);
    if (!url || url.protocol !== 'postgresql:' || !url.hostname || !url.username || !url.password
      || url.pathname.length < 2 || url.hash || /\s/u.test(result)) throw new Error('invalid_workbench_database_url');
    const options = [...url.searchParams];
    if (new Set(options.map(([name]) => name)).size !== options.length
      || options.some(([name, value]) => name === 'sslmode' ? !['disable', 'verify-full'].includes(value)
        : name !== 'sslrootcert' || !isAbsolute(value))) throw new Error('invalid_workbench_database_options');
    if (!['127.0.0.1', '[::1]'].includes(url.hostname) && url.searchParams.get('sslmode') !== 'verify-full') {
      throw new Error('workbench_database_tls_required');
    }
    return result;
  } finally {
    await file.close();
  }
}

export async function loadWorkbench(
  discovery: unknown, configPath: string, databasePath: string,
): Promise<WorkbenchOptions> {
  const metadata = parseAuthenticationDiscovery(discovery);
  const config = configuration(JSON.parse(await readFile(configPath, 'utf8')));
  if (config.access.issuer !== metadata.issuer || config.access.desktopClientId !== metadata.client_id) {
    throw new Error('workbench_discovery_mismatch');
  }
  const connectionString = await databaseConnectionString(databasePath);
  const pool = new Pool({
    connectionString, max: 8, connectionTimeoutMillis: 3000, statement_timeout: 7000,
    lock_timeout: 4000, idle_in_transaction_session_timeout: 10000,
  });
  pool.on('error', () => console.error('Harniqo workbench lost an idle database connection.'));
  const db = new Kysely<unknown>({ dialect: new PostgresDialect({ pool }) });
  let producer: Awaited<ReturnType<typeof startWorkbenchProducer>> | undefined;
  let closing: Promise<void> | undefined;
  const close = () => closing ??= (async () => {
    try { await producer?.stop({ graceful: true, close: true }); }
    finally { await db.destroy(); }
  })();
  try {
    const authenticate = createWorkbenchAccess(db, config.access);
    const roleCheck = await sql<{ allowed: boolean }>`
      SELECT current_user = 'harniqo_control' AND NOT p.rolsuper AND NOT p.rolbypassrls
        AND to_regclass('harniqo.external_identities') IS NOT NULL
        AND to_regclass('harniqo.runs') IS NOT NULL
        AND to_regclass('harniqo.workbench_budget_reservations') IS NOT NULL
        AND to_regprocedure('harniqo.hermes_execution_ready(uuid,uuid,uuid,uuid,uuid,uuid,uuid)') IS NOT NULL
        AND NOT EXISTS (
          SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'harniqo' AND c.relkind IN ('r', 'p')
            AND pg_has_role(current_user, c.relowner, 'MEMBER')
        ) AS allowed
      FROM pg_roles p WHERE p.rolname = current_user`.execute(db);
    if (roleCheck.rows[0]?.allowed !== true) throw new Error('invalid_workbench_database_role');
    const started = await startWorkbenchProducer(connectionString);
    producer = started;
    const enqueue = createWorkbenchQueue(started);
    const reserveAndEnqueue: ReserveAndEnqueue = async (tx, pending) => {
      const { target } = pending;
      const readiness = await sql<{ ready: boolean }>`
        SELECT harniqo.hermes_execution_ready(
          ${target.organization_id}::uuid, ${target.project_id}::uuid, ${pending.scope.principalId}::uuid,
          ${target.execution_target_id}::uuid, ${target.deployment_revision_id}::uuid, ${target.workspace_id}::uuid,
          ${config.platform.platform_epoch}::uuid)
        AS ready`.execute(tx);
      if (readiness.rows[0]?.ready !== true) throw new WorkbenchProblem('UPSTREAM_UNAVAILABLE');
      await enqueue(tx, pending);
    };
    return { db, authenticate, reserveAndEnqueue, platform: config.platform, close };
  } catch (error) {
    await close();
    throw error;
  }
}
